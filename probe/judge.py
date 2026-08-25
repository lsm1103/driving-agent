"""探针 B：在录制好的信号流上跑 Intake 判断。

回答第二个问题：面对一天上百条变化，它能不能只挑出该理会的那几条？
docs/04 的指标「Intake 忽略率 70%~95%」在这里第一次被实测。

Intake 的规矩（docs/03 §4 第 2 步）：只登记，不行动。
"""
import json, sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ds import call_structured, StructuredCallFailed

BATCH = 40  # 每批条数：太大易截断，太小浪费缓存
OUT = Path("fixtures/intake.jsonl")  # 追加式：每批立即落盘，中断不丢

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"decisions": {"type": "array", "minItems": 1, "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "id": {"type": "string"},
            "relevant": {"type": "boolean"},
            "reason": {"type": "string", "minLength": 4},
            "kind": {"type": "string",
                     "enum": ["exploration", "maintenance", "social", "meaning", "none"]},
        },
        "required": ["id", "relevant", "reason", "kind"]}}},
    "required": ["decisions"],
}


def system_prompt(concern: str) -> str:
    # 前缀必须逐字节稳定 —— 缓存命中差 30 倍（docs/03 §3.8）
    return f"""你是一个自主 Agent 的 Intake 环节。

你的关切范围（这是相关性判据，不是任务；它说什么与你有关，不说你去做什么）：
{concern}

现在给你一批外部世界的变化信号。对每一条判断：它与你的关切有没有关系？

规矩：
1. 只登记，不行动。你不是在决定做什么，只是在决定什么值得被记下来。
2. 默认忽略。绝大多数信号与你无关，这是正常的，不要为了显得勤奋而放宽标准。
3. relevant=true 的条目要能说清楚它与关切的哪一部分相关，reason 写具体的关联，
   不要写"与 AI 相关"这种套话。
4. relevant=false 的 kind 一律填 none。
5. 必须对每一条都给出判断，不能遗漏。

kind 含义：exploration=值得深入的新东西；maintenance=需要修的东西；
social=有人在等回应；meaning=与长期方向有关。"""


def judge_batch(concern, batch):
    lines = [f'[{s["id"]}] ({s["source"]}/{s["channel"]}) {s["title"]}\n    {s["summary"][:300]}'
             for s in batch]
    user = f"共 {len(batch)} 条信号，逐条判断：\n\n" + "\n\n".join(lines)
    data = call_structured("intake", system_prompt(concern), user,
                           "submit_intake", SCHEMA, max_tokens=16000)
    by_id = {d["id"]: d for d in data["decisions"]}
    missing = [s["id"] for s in batch if s["id"] not in by_id]
    return by_id, missing


def load_done() -> dict:
    """读已有结果 —— 支持续跑。"""
    if not OUT.exists():
        return {}
    return {d["id"]: d for d in
            (json.loads(l) for l in OUT.read_text(encoding="utf-8").splitlines() if l.strip())}


def report(signals, results):
    idx = {s["id"]: s for s in signals}
    judged = len(results)
    rel = [d for d in results.values() if d["relevant"]]
    print(f"\n{'='*56}")
    print(f"已判断 {judged}/{len(signals)} 条")
    if not judged:
        return
    ignore_rate = 1 - len(rel) / judged
    ok = "✅ 落在 docs/04 预期区间" if 0.70 <= ignore_rate <= 0.95 else "⚠️ 超出预期区间"
    print(f"\n忽略率 {ignore_rate:.1%}   （理会 {len(rel)} 条）   {ok}")

    per_day = defaultdict(lambda: [0, 0])
    for sid, d in results.items():
        if sid not in idx:
            continue
        row = per_day[idx[sid]["day"]]
        row[0] += 1; row[1] += bool(d["relevant"])
    print("\n按天：")
    for day in sorted(per_day):
        tot, hit = per_day[day]
        print(f"  {day}  {tot:4d} 条 → 理会 {hit:3d}  ({hit/tot:5.1%})")

    src = defaultdict(lambda: [0, 0])
    for sid, d in results.items():
        if sid not in idx:
            continue
        row = src[f'{idx[sid]["source"]}/{idx[sid]["channel"]}']
        row[0] += 1; row[1] += bool(d["relevant"])
    print("\n按来源理会率：")
    for k, (tot, hit) in sorted(src.items(), key=lambda kv: -kv[1][1]):
        print(f"  {k:34s} {hit:3d}/{tot:4d}  ({hit/tot:5.1%})")
    print(f"\nkind 分布：{dict(Counter(d['kind'] for d in rel))}")


def main():
    fx = json.loads(Path("fixtures/signals.json").read_text(encoding="utf-8"))
    signals, concern = fx["signals"], fx["concern"]
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        signals = signals[:int(args[0])]

    results = load_done()
    if "--report" in sys.argv:
        return report(signals, results)

    todo = [s for s in signals if s["id"] not in results]
    print(f"总计 {len(signals)} 条 | 已完成 {len(signals)-len(todo)} 条 | 待判断 {len(todo)} 条"
          f" | 每批 {BATCH}", flush=True)

    failed_batches = missing_total = 0
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        tag = f"[{i//BATCH + 1}/{(len(todo)+BATCH-1)//BATCH}]"
        try:
            by_id, missing = judge_batch(concern, batch)
            missing_total += len(missing)
            # 立即追加落盘 —— 中断只丢当前这一批，不丢全部
            with OUT.open("a", encoding="utf-8") as f:
                for sid, d in by_id.items():
                    f.write(json.dumps(d, ensure_ascii=False) + "\n")
                f.flush()
            results.update(by_id)
            hit = sum(1 for s in batch if by_id.get(s["id"], {}).get("relevant"))
            print(f"{tag} {len(batch)} 条 → 理会 {hit} 条"
                  + (f"  ⚠️ 漏判 {len(missing)}" if missing else ""), flush=True)
        except StructuredCallFailed as e:
            failed_batches += 1
            print(f"{tag} ❌ 批次失败（不静默兜底）: {str(e)[:160]}", flush=True)

    print(f"\n批次失败 {failed_batches} | 漏判 {missing_total}", flush=True)
    report(signals, results)


if __name__ == "__main__":
    main()
