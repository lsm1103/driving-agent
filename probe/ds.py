"""DeepSeek 结构化调用封装 —— 校验/重试层。

存在的理由（全部经实测验证，见 docs/05）：
  1. 严格 JSON Schema 不可用 → schema 必须自己校验
  2. thinking 模式不支持强制 tool_choice → 只能 auto，模型可能不调用工具
  3. 推理 token 吃 max_tokens → 耗尽时 content 静默返回空，不报错

三次失败不静默兜底：抛 StructuredCallFailed。失败本身是实验数据。
"""
import json, os, time, pathlib, urllib.request

from jsonschema import Draft7Validator

BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-v4-pro"

# docs/03 §3.7：算力按决策重要性分配，不按成本分配
EFFORT = {"intake": "low", "propose": "high", "compete": "xhigh", "reckon": "high"}

_USAGE_LOG = pathlib.Path("fixtures/usage.jsonl")


class StructuredCallFailed(RuntimeError):
    """重试耗尽。调用方必须处理，不得用默认值糊过去。"""


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
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())


def _log_usage(step: str, resp: dict) -> None:
    """记录 model 与完整 usage —— docs/05 §8 的漂移探测依赖这个。"""
    u = resp.get("usage", {})
    _USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with _USAGE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "step": step,
            "model": resp.get("model"),
            "prompt_tokens": u.get("prompt_tokens"),
            "completion_tokens": u.get("completion_tokens"),
            "reasoning_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
            "cache_hit": u.get("prompt_cache_hit_tokens"),
            "cache_miss": u.get("prompt_cache_miss_tokens"),
        }, ensure_ascii=False) + "\n")


def call_structured(step, system, user, tool_name, schema,
                    effort=None, max_tokens=8000, retries=3):
    """返回经 schema 校验的 dict。失败抛 StructuredCallFailed。"""
    validator = Draft7Validator(schema)
    tool = {"type": "function", "function": {
        "name": tool_name, "description": f"提交 {step} 的结构化结果", "parameters": schema}}
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    problems = []

    for attempt in range(1, retries + 1):
        resp = _post({
            "model": MODEL,
            "max_tokens": max_tokens,          # 给足：推理吃额度，不够会静默返空
            "reasoning_effort": effort or EFFORT.get(step, "high"),
            "tools": [tool],
            "tool_choice": "auto",             # 强制不可用，只能靠提示词施压
            "messages": msgs,
        })
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
