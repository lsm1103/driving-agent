"""DeepSeek 结构化调用封装 —— 校验/重试层。

存在的理由（全部经实测验证，见 docs/05）：
  1. 严格 JSON Schema 不可用 → schema 必须自己校验
  2. thinking 模式不支持强制 tool_choice → 只能 auto，模型可能不调用工具
  3. 推理 token 吃 max_tokens → 耗尽时 content 静默返回空，不报错

三次失败不静默兜底：抛 StructuredCallFailed。失败本身是实验数据。
"""
import datetime, json, os, time, pathlib, urllib.error, urllib.request

from jsonschema import Draft7Validator

BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-v4-pro"

# docs/03 §3.7：算力按决策重要性分配，不按成本分配
EFFORT = {"intake": "low", "propose": "high", "compete": "xhigh", "reckon": "high"}

_USAGE_LOG = pathlib.Path("fixtures/usage.jsonl")


class StructuredCallFailed(RuntimeError):
    """重试耗尽。调用方必须处理，不得用默认值糊过去。"""


class InsufficientBalance(RuntimeError):
    """账户余额不足（402）。重试没有意义 —— 立刻停，等充值后续跑。"""


class TransientAPIError(RuntimeError):
    """限流或服务端错误。可以退避重试。"""


def _unquote(v: str) -> str:
    """.env 的值可能带引号；shell 会剥掉，朴素解析不会。"""
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def _key() -> str:
    if k := os.environ.get("deepseek_Key"):
        return _unquote(k)
    for line in pathlib.Path(".env").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("deepseek_Key="):
            return _unquote(line.split("=", 1)[1])
    raise RuntimeError("找不到 deepseek_Key（环境变量或 .env）")


def _post(payload: dict) -> dict:
    """把 HTTP 错误翻译成分类异常。

    这层原先不存在 —— 402 直接从重试层穿到顶把 27 天回放炸在第 4 天（docs/07 §9）。
    区分「重试有意义」和「重试没意义」是这层唯一的职责。
    """
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        if e.code == 402:
            raise InsufficientBalance(
                "DeepSeek 账户余额不足（HTTP 402）。充值后用同一条命令续跑，"
                f"已完成的天数会自动跳过。\n  接口返回: {body}") from e
        if e.code == 401:
            raise RuntimeError(f"鉴权失败（401），检查 .env 里的 deepseek_Key。{body}") from e
        if e.code == 429 or e.code >= 500:
            raise TransientAPIError(f"HTTP {e.code}: {body}") from e
        raise RuntimeError(f"HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise TransientAPIError(f"网络错误: {e.reason}") from e


def _log_usage(step: str, resp: dict) -> None:
    """记录 model 与完整 usage —— docs/05 §8 的漂移探测依赖这个。"""
    u = resp.get("usage", {})
    _USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with _USAGE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "step": step,
            "model": resp.get("model"),
            "prompt_tokens": u.get("prompt_tokens"),
            "completion_tokens": u.get("completion_tokens"),
            "reasoning_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
            "cache_hit": u.get("prompt_cache_hit_tokens"),
            "cache_miss": u.get("prompt_cache_miss_tokens"),
        }, ensure_ascii=False) + "\n")


def call_structured(step, system, user, tool_name, schema,
                    effort=None, max_tokens=8000, retries=3, extra_check=None):
    """返回经 schema + 业务校验的 dict。失败抛 StructuredCallFailed。

    extra_check(data) -> str | None
        schema 管不了的约束在这里查，返回错误描述即判定失败并回灌重试。
        最典型的是**引用完整性**：schema 只能验证 "id 是字符串"，
        验证不了 "这个 id 真的存在于输入集合里"。探针实测出现过 4 例
        id 转写错误（丢首字符、末位抄错），内容全对但标识符指向不存在的对象，
        schema 校验一路放行。见 docs/06 §3.3。
    """
    validator = Draft7Validator(schema)
    tool = {"type": "function", "function": {
        "name": tool_name, "description": f"提交 {step} 的结构化结果", "parameters": schema}}
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    problems = []

    for attempt in range(1, retries + 1):
        try:
            resp = _post({
                "model": MODEL,
                "max_tokens": max_tokens,      # 给足：推理吃额度，不够会静默返空
                "reasoning_effort": effort or EFFORT.get(step, "high"),
                "tools": [tool],
                "tool_choice": "auto",         # 强制不可用，只能靠提示词施压
                "messages": msgs,
            })
        except InsufficientBalance:
            raise                      # 重试没有意义，立刻停
        except TransientAPIError as e:
            if attempt == retries:
                raise StructuredCallFailed(f"{step}: {e}") from e
            wait = 5 * 2 ** attempt
            print(f"  ⏳ {step} 第{attempt}次遇到 {e}，{wait}s 后重试")
            time.sleep(wait)
            continue
        _log_usage(step, resp)
        choice = resp["choices"][0]
        why = None

        if choice.get("finish_reason") == "length":
            why = f"输出被截断（max_tokens={max_tokens} 被推理吃光）"
        elif not choice["message"].get("tool_calls"):
            why = "模型没有调用工具（tool_choice=auto 无法强制）"
        else:
            raw = choice["message"]["tool_calls"][0]["function"]["arguments"]
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                why = f"arguments 不是合法 JSON: {e}"
            else:
                errs = sorted(validator.iter_errors(data), key=lambda e: e.path)
                if errs:
                    why = "schema 校验失败: " + "; ".join(
                        f"{list(e.path)}: {e.message}" for e in errs[:5])
                elif extra_check and (bad := extra_check(data)):
                    why = f"业务校验失败: {bad}"
                else:
                    return data

        problems.append(f"第{attempt}次: {why}")
        print(f"  ⚠️  {step} 第{attempt}次失败 —— {why}")
        # 把失败原因回灌，让下一次有机会自我纠正
        msgs.append({"role": "user",
                     "content": f"上一次调用失败：{why}。请严格按工具 schema 重新调用 {tool_name}。"})
        if attempt < retries:
            time.sleep(2 ** attempt)

    raise StructuredCallFailed(f"{step} 重试 {retries} 次仍失败:\n  " + "\n  ".join(problems))
