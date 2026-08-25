"""探针 B：在录制好的信号流上跑 Intake 判断。

v2 判据（docs/06 §3.1 的修正）：
  不是问"它与我的关切有没有关系"，而是问
  "它值不值得占用账本、等待闭合、并在未来每天参与预算竞争"。

  强制门槛：登记就必须写得出可被事实检验的 close_condition。
  写不出来，就不该登记 —— 门槛自己会筛。

v1 结果保留在 intake.jsonl，v2 写入 intake_v2.jsonl，可直接对比判据变化的效果。
"""
import json, sys, threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ds import call_structured, StructuredCallFailed

CHUNK = 50                              # 单批上限；按天分批，超过则切块
WORKERS = 6                             # 批次间无依赖，可并发；日循环本身不能（见 docs/06 §10）
OUT = Path("fixtures/intake_v2.jsonl")  # 追加式：每批立即落盘，中断不丢
DAILY_SLOTS = 5                         # docs/03 §2.1

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"decisions": {"type": "array", "minItems": 1, "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "ref": {"type": "string"},
            "register": {"type": "boolean"},
            "reason": {"type": "string", "minLength": 4},
            "kind": {"type": "string",
                     "enum": ["exploration", "maintenance", "social", "meaning", "none"]},
            "close_condition": {"type": "string"},
        },
        "required": ["ref", "register", "reason", "kind", "close_condition"]}}},
    "required": ["decisions"],
}


def system_prompt(concern: str) -> str:
    # 前缀必须逐字节稳定 —— 缓存命中差 30 倍（docs/03 §3.8）
    return f"""你是一个自主 Agent 的 Intake 环节。

你的关切范围（这是相关性判据，不是任务；它说什么与你有关，不说你去做什么）：
{concern}

现在给你一批外部世界的变化信号。对每一条决定：**要不要把它登记成一条未闭合项。**

⚠️ 判据不是"它与我的关切有没有关系"，而是：
   **它值不值得占用我的账本、等待闭合、并且在未来每一天参与预算竞争。**

相关 ≠ 值得登记。一篇论文与动机机制有关，远不等于它值得被长期追踪。

强制门槛 —— 如果你要登记它，就必须写出 close_condition：
一个**可被事实检验**的闭合条件，说明什么情况下这条项算了结。
**写不出可检验的闭合条件，就不该登记。**

不可接受的闭合条件（模糊、无法判定）：
  "当我理解了它" / "读完之后" / "有空再看" / "跟进一下"
可接受的闭合条件（有明确的事实判定）：
  "复现其实验，确认 >5 层子 agent 时是否真的丢失上下文"
  "把它的驱动冲突解法与我的 Compete 步骤逐条对比，产出一条可证伪的判断"

参考约束：我每天只有 {DAILY_SLOTS} 个行动槽。
登记速率如果远超这个数，账本会堆积成永远处理不完的垃圾场。

规矩：
1. 只登记，不行动。你不是在决定做什么，只是在决定什么值得被记下来。
2. 默认不登记。绝大多数信号不该进账本，这是正常的。
3. register=false 时：kind 填 none，close_condition 填空字符串。
4. 必须对每一条给出判断，不能遗漏。
5. **ref 必须原样抄写给定的编号**（如 S01），不要改动、补齐或缩写。

kind 含义：exploration=值得深入的新东西；maintenance=需要修的东西；
social=有人在等回应；meaning=与长期方向有关。"""


def make_checker(refs: set):
    """schema 管不了的约束。核心是引用完整性 —— docs/06 §3.3。"""
    def check(data):
        got = [d["ref"] for d in data["decisions"]]
        if ghosts := sorted(set(got) - refs):
            return f"这些编号不存在于本批输入中（必须原样抄写）: {ghosts[:5]}"
        if dup := [r for r, n in Counter(got).items() if n > 1]:
            return f"编号重复: {dup[:5]}"
        for d in data["decisions"]:
            if d["register"]:
                if len(d["close_condition"].strip()) < 12:
                    return f"{d['ref']} 登记了但 close_condition 太短或缺失，无法检验"
                if d["kind"] == "none":
                    return f"{d['ref']} 登记了但 kind=none"
            elif d["kind"] != "none" or d["close_condition"].strip():
                return f"{d['ref']} 未登记，kind 必须为 none 且 close_condition 必须为空"
        return None
    return check


def judge_chunk(concern, chunk, day, day_total, part, parts):
    ref_of = {s["id"]: f"S{i:02d}" for i, s in enumerate(chunk, 1)}   # 批内短序号，抗转写错误
    sig_of = {v: k for k, v in ref_of.items()}
    lines = [f'[{ref_of[s["id"]]}] ({s["source"]}/{s["channel"]}) {s["title"]}\n     {s["summary"][:300]}'
             for s in chunk]
    user = (f"日期 {day}，这一天共 {day_total} 条信号，当前是第 {part}/{parts} 批（{len(chunk)} 条）。\n"
            f"逐条判断是否登记：\n\n" + "\n\n".join(lines))
    data = call_structured("intake", system_prompt(concern), user, "submit_intake", SCHEMA,
                           max_tokens=24000, extra_check=make_checker(set(sig_of)))
    out = {}
    for d in data["decisions"]:
        d = dict(d); d["id"] = sig_of[d.pop("ref")]; d["day"] = day
        out[d["id"]] = d
    return out, [s["id"] for s in chunk if s["id"] not in out]


def load_done() -> dict:
    if not OUT.exists():
        return {}
    return {d["id"]: d for d in (json.loads(l) for l in
            OUT.read_text(encoding="utf-8").splitlines() if l.strip())}


def report(signals, results):
    idx = {s["id"]: s for s in signals}
    reg = [d for d in results.values() if d["register"]]
    n = len([1 for i in results if i in idx])
    print(f"\n{'='*60}\n已判断 {n}/{len(signals)} 条   覆盖率 {n/len(signals):.2%}")
    print(f"幽灵编号: {len(results)-n}   （v1 实测 4 例，见 docs/06 §3.3）")
    if not n:
        return
    print(f"\n登记率 {len(reg)/n:.1%}   忽略率 {1-len(reg)/n:.1%}   （登记 {len(reg)} 条）")

    per_day = defaultdict(lambda: [0, 0])
    for sid, d in results.items():
        if sid in idx:
            r = per_day[idx[sid]["day"]]; r[0] += 1; r[1] += bool(d["register"])
    print(f"\n按天（预算 {DAILY_SLOTS} 槽/天，目标流入 3~6 条/天）：")
    ok_days = 0
    for day in sorted(per_day):
        tot, hit = per_day[day]
        flag = "✅" if 3 <= hit <= 6 else ("⚠️ 偏低" if hit < 3 else "⚠️ 偏高")
        ok_days += 3 <= hit <= 6
        print(f"  {day}  {tot:4d} 条 → 登记 {hit:3d}  ({hit/tot:5.1%})  {flag}")
    avg = len(reg) / len(per_day)
    print(f"\n日均登记 {avg:.1f} 条  |  预算 {DAILY_SLOTS} 槽  |  "
          f"净流入 {avg-DAILY_SLOTS:+.1f}/天  →  30 天累积 {(avg-DAILY_SLOTS)*30:+.0f} 条")
    print(f"落在 3~6 区间的天数：{ok_days}/{len(per_day)}")
    print(f"\nkind 分布：{dict(Counter(d['kind'] for d in reg))}")
    ex = sum(1 for d in reg if d["kind"] == "exploration")
    print(f"exploration 占比 {ex/max(len(reg),1):.0%}   （v1 实测 69%，docs/06 §3.2）")


def main():
    fx = json.loads(Path("fixtures/signals.json").read_text(encoding="utf-8"))
    signals, concern = fx["signals"], fx["concern"]
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        signals = signals[:int(args[0])]

    global OUT
    for a in sys.argv[1:]:
        if a.startswith("--out="):
            OUT = Path(a.split("=", 1)[1])

    results = load_done()
    if "--report" in sys.argv:
        return report(signals, results)

    by_day = defaultdict(list)
    for s in signals:
        by_day[s["day"]].append(s)

    todo = [(day, ss) for day, ss in sorted(by_day.items())
            if any(s["id"] not in results for s in ss)]
    print(f"总计 {len(signals)} 条 / {len(by_day)} 天 | 已完成 {len(results)} 条 | "
          f"按天分批，单批上限 {CHUNK}", flush=True)

    # 批次之间毫无依赖 —— 可以并发。注意：能并发的只有 Intake 这类批内无依赖的
    # 步骤；日循环本身不能，第 N 天的账本取决于第 N-1 天（docs/06 §10）。
    jobs = []
    for day, day_signals in todo:
        pending = [s for s in day_signals if s["id"] not in results]
        parts = (len(pending) + CHUNK - 1) // CHUNK
        for p in range(parts):
            jobs.append((day, len(day_signals), pending[p*CHUNK:(p+1)*CHUNK], p + 1, parts))

    print(f"共 {len(jobs)} 批，{WORKERS} 路并发", flush=True)
    failed = missing_total = 0
    lock = threading.Lock()

    def run(job):
        day, day_total, chunk, part, parts = job
        got, missing = judge_chunk(concern, chunk, day, day_total, part, parts)
        with lock:                                   # 立即落盘，中断只丢在途批次
            with OUT.open("a", encoding="utf-8") as f:
                for d in got.values():
                    f.write(json.dumps(d, ensure_ascii=False) + "\n")
                f.flush()
            results.update(got)
        return day, part, parts, len(chunk), got, missing

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(run, j): j for j in jobs}
        for i, fut in enumerate(as_completed(futures), 1):
            day, _, chunk, part, parts = futures[fut]
            try:
                day, part, parts, n, got, missing = fut.result()
                missing_total += len(missing)
                print(f"  [{i}/{len(jobs)}] {day} ({part}/{parts}) {n} 条 → 登记 "
                      f"{sum(1 for d in got.values() if d['register'])} 条"
                      + (f"  ⚠️ 漏判 {len(missing)}" if missing else ""), flush=True)
            except StructuredCallFailed as e:
                failed += 1
                print(f"  [{i}/{len(jobs)}] {day} ({part}/{parts}) ❌ 失败: {str(e)[:180]}", flush=True)

    print(f"\n批次失败 {failed} | 漏判 {missing_total}", flush=True)
    report(signals, results)


if __name__ == "__main__":
    main()
