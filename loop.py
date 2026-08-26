"""日循环（docs/03 §4）—— 阶段 0 骨架。

阶段 0 的边界（docs/03 §6）：
  - Compete 用**固定规则**，不用模型。先证明"账本 + 到期机制"能产生跨天连续行为，
    再加 Drive。不先跑阶段 0 就加 Drive，最后无法归因。
  - Act 是**模拟执行**（明确标注）。阶段 0 要观察的是账本动力学
    （会不会膨胀、到期会不会占满预算），不是工作质量。真实 Harness 在阶段 2 接入。

用法：
  .venv/bin/python loop.py            # 回放全部 8 天
  .venv/bin/python loop.py 3          # 只跑前 3 天
"""
import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.ds import call_structured, StructuredCallFailed, InsufficientBalance
from ledger.store import Ledger, LedgerViolation
from world.replay import ReplayWorld

SLOTS = 5           # docs/03 §2.1 每日行动槽，不结转
STALE_AFTER = 10    # 超过 K 天无进展 → stale，强制进入次日候选
DB = "fixtures/ledger.db"

PROPOSE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"candidates": {"type": "array", "minItems": 1, "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "id": {"type": "string"},
            "proposal": {"type": "string", "minLength": 8},
            "cost_slots": {"type": "integer", "minimum": 1, "maximum": 5},
            "closes_loops": {"type": "array", "items": {"type": "string"}},
            "abandons": {"type": "array", "items": {"type": "string"}},
            "rationale": {"type": "string", "minLength": 8},
        }, "required": ["id", "proposal", "cost_slots", "closes_loops", "abandons",
                        "rationale"]}}},
    "required": ["candidates"]}

ACT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"results": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "candidate_id": {"type": "string"},
            "closed_loops": {"type": "array", "items": {"type": "string"}},
            "finding": {"type": "string", "minLength": 12},
            "prediction": {"type": ["object", "null"], "additionalProperties": False,
                "properties": {"claim": {"type": "string", "minLength": 12},
                               "verify_method": {"type": "string", "minLength": 12},
                               "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                               "due_in_days": {"type": "integer", "minimum": 2, "maximum": 20}},
                "required": ["claim", "verify_method", "confidence", "due_in_days"]},
        }, "required": ["candidate_id", "closed_loops", "finding", "prediction"]}}},
    "required": ["results"]}

RECKON_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"settlements": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "id": {"type": "string"},
            "outcome": {"type": "string", "enum": ["true", "false", "moot", "kept", "broken"]},
            "postmortem": {"type": "string"},
        }, "required": ["id", "outcome", "postmortem"]}}},
    "required": ["settlements"]}


def compete_fixed(candidates, due_ids):
    """阶段 0 的固定规则：到期项优先（成本减半），其次按闭合效率贪心装满预算。

    这不是"决策"，是排序 —— 阶段 0 刻意如此。若固定规则就能产生像样的跨天对账，
    那 Drive 层的增量价值必须单独证明（docs/03 §6）。
    """
    def key(c):
        is_due = bool(set(c["closes_loops"]) & due_ids)
        # 放弃与闭合同样是账本的出口，效率上等价计入
        eff = (len(c["closes_loops"]) + len(c.get("abandons", []))) / max(c["cost_slots"], 1)
        return (not is_due, -eff, c["cost_slots"])

    used, out = 0, []
    for c in sorted(candidates, key=key):
        cost = max(1, c["cost_slots"] // 2) if set(c["closes_loops"]) & due_ids else c["cost_slots"]
        if used + cost <= SLOTS:
            used += cost
            out.append({**c, "chosen": 1, "effective_cost": cost})
        else:
            out.append({**c, "chosen": 0, "effective_cost": cost,
                        "reject_reason": f"预算已满：已用 {used}/{SLOTS} 槽，本候选需 {cost} 槽"})
    return out, used


def run_day(L, W, day):
    real = W.real_date(day)
    print(f"\n{'='*64}\nDay {day}  ({real})   预算 {SLOTS} 槽")

    # ---- 1 Wake ----
    signals = W.diff(day)
    print(f"[Wake]   外生变化 {len(signals)} 条")

    # ---- 2 Intake（回放：复用已录制判断）----
    registered, ignored = W.recorded_intake(signals)
    if registered is None:
        print("  ⚠️ 无录制 Intake，阶段 0 回放跳过该天"); return
    for i, d in enumerate(registered, 1):
        try:
            L.open_loop(f"OL-{day:02d}{i:02d}", day, d["kind"],
                        d["reason"][:200], d["close_condition"], origin_signal=d["id"])
        except LedgerViolation as e:
            print(f"  ⚠️ 登记被拦: {e}")
    print(f"[Intake] 登记 {len(registered)} 条，忽略 {ignored} 条  (忽略率 "
          f"{ignored/max(len(signals),1):.1%})")

    # ---- 3 Due ----
    L.mark_stale(day, STALE_AFTER)
    due = L.due(day)
    due_ids = {d["target_loop"] for d in due["commitments"] if d["target_loop"]} | \
              {d["id"] for d in due["stale"]}
    n_due = sum(len(v) for v in due.values())
    print(f"[Due]    到期 {n_due} 项  (预测 {len(due['predictions'])} / "
          f"承诺 {len(due['commitments'])} / stale {len(due['stale'])})")

    # ---- 4 Read（阶段 0 不做 Drive）----
    loops = L.open_loops()
    snapshot = {"open_loops": len(loops), "due": n_due, "stage": 0}

    # ---- 5 Propose ----
    ctx = L.summary(day)     # 缓存前缀：同一天内多次调用共享
    due_txt = json.dumps({k: [x["id"] for x in v] for k, v in due.items()}, ensure_ascii=False)
    try:
        prop = call_structured(
            "propose",
            "你是一个自主 Agent 的 Propose 环节。基于账本状态生成今天的候选行动。\n"
            f"每个候选必须声明成本（1~{SLOTS} 槽）、预期闭合哪些未闭合项（用账本里的 OL 编号）。\n"
            "生成 4~7 个候选，允许包含'什么都不做/写小结'这类不闭合任何项的选项。\n\n"
            "⚠️ **proposal 只写做什么，不超过 25 个字。**\n"
            "动词开头，说清这一步的动作和对象，像跟同事交代活儿那样说。\n"
            "不要在 proposal 里塞理由、论证、方法细节和预期产出 —— 那些全部写进 rationale。\n"
            "  ✗ 反例：把四篇记忆/身份连续性工作（记忆权重机制、时间有效性、ECHO\n"
            "     consolidation/revision、MemGuard verifier 持久化）并成一次对照，逐项检验\n"
            "     是否真正处理'跨会话目标/承诺连续性'而非仅事实检索，产出统一可证伪清单。\n"
            "  ✓ 正例：把四篇记忆连续性论文并成一次横向对照\n"
            "     rationale：它们都在回答'过往能否被带回当下'，分开读会重复四遍同样的判断；\n"
            "     合并后能产出一张统一的可证伪清单。\n"
            "closes_loops 与 abandons 里只能填账本中真实存在的编号。\n\n"
            "⚠️ **放弃是一等行动，不是失败。**\n"
            "清理待办很大程度不是靠做完，而是靠承认『这个我不做了』。\n"
            "如果某条未闭合项已经不值得再追踪——偏离关切、闭合条件事后看不可检验、\n"
            "被更好的项取代、或积压太久已失去时效——就产出一个候选，\n"
            "把它填进 abandons（成本固定 1 槽），rationale 写清楚为什么放弃（会存进账本）。\n"
            "一个候选可以一次放弃多条。不要为了显得勤奋而留着永远不会碰的项。",
            f"{ctx}\n\n今日到期项：{due_txt}\n\n请生成今天的候选行动。",
            "submit_candidates", PROPOSE_SCHEMA, max_tokens=16000,
            extra_check=lambda d: next(
                (f"{c['id']} 引用了不存在的 OL 编号 {bad}" for c in d["candidates"]
                 if (bad := sorted((set(c["closes_loops"]) | set(c.get("abandons", [])))
                                   - {l['id'] for l in loops}))), None))
    except StructuredCallFailed as e:
        print(f"[Propose] ❌ 失败: {str(e)[:150]}"); return
    cands = prop["candidates"]
    print(f"[Propose] 候选 {len(cands)} 个")

    # ---- 6 Compete（阶段 0：固定规则）----
    decided, used = compete_fixed(cands, due_ids)
    chosen = [c for c in decided if c["chosen"]]
    L.record_decisions(day, decided, snapshot)

    # 放弃是记账动作，不需要执行，直接落账（硬约束 2 会守住理由非空）
    dropped = 0
    for c in chosen:
        for lid in c.get("abandons", []):
            try:
                L.abandon_loop(lid, day, c["rationale"])
                dropped += 1
            except LedgerViolation as e:
                print(f"  ⚠️ 放弃被拦: {e}")
    chosen = [c for c in chosen if not c.get("abandons")]
    if dropped:
        print(f"[Abandon] 主动放弃 {dropped} 条未闭合项")
    print(f"[Compete] 选中 {len(chosen)} 个，用 {used}/{SLOTS} 槽（固定规则）")
    for c in decided:
        mark = "✓" if c["chosen"] else "✗"
        print(f"    {mark} [{c['effective_cost']}槽] {c['proposal'][:56]}")

    # ---- 7-8 Act（模拟执行，阶段 0）----
    if chosen:
        try:
            act = call_structured(
                "act",
                "你是一个自主 Agent 的执行环节。⚠️ 这是**模拟执行**（阶段 0）：\n"
                "根据每个行动的内容与它要闭合的未闭合项的闭合条件，判断这次行动是否达成闭合。\n"
                "达成才写进 closed_loops，没达成就留空 —— 不要为了好看而声称闭合。\n\n"
                "⚠️ 关键：绝大多数闭合条件写的是「产出一条可证伪判断」。\n"
                "**闭合这类项，就等于必须给出那条判断本身** —— 填进 prediction。\n"
                "只把 loop 标成闭合、prediction 却填 null，等于没有真正闭合，不允许。\n"
                "prediction 要能在未来某天用事实检验：claim 是断言，verify_method 是怎么验，\n"
                "due_in_days 是几天后该回来对账。这是你给未来的自己制造的债。\n\n"
                "finding 写这次行动得到的具体结论。确实没闭合任何项时，prediction 可填 null。",
                f"{ctx}\n\n本次执行的行动：\n" +
                json.dumps([{k: c[k] for k in ('id', 'proposal', 'closes_loops')}
                            for c in chosen], ensure_ascii=False, indent=1),
                "submit_results", ACT_SCHEMA, max_tokens=16000,
                extra_check=lambda d: next(
                    (f"{r['candidate_id']} 闭合了 {r['closed_loops']} 但 prediction 为 null；"
                     "这些闭合条件要求产出可证伪判断，闭合就必须给出该判断"
                     for r in d["results"]
                     if r["closed_loops"] and not r.get("prediction")), None))
            for i, r in enumerate(act["results"], 1):
                for lid in r["closed_loops"]:
                    try:
                        L.close_loop(lid, day)
                    except LedgerViolation as e:
                        print(f"  ⚠️ {e}")
                if p := r.get("prediction"):
                    try:
                        L.predict(f"PR-{day:02d}{i:02d}", day, day + p["due_in_days"],
                                  p["claim"], p["verify_method"], p["confidence"], snapshot)
                        print(f"[Commit] 预测 PR-{day:02d}{i:02d} 到期第 {day+p['due_in_days']} 天: "
                              f"{p['claim'][:52]}")
                    except LedgerViolation as e:
                        print(f"  ⚠️ 预测被拦: {e}")
            closed = sum(len(r["closed_loops"]) for r in act["results"])
            print(f"[Act]    模拟执行 {len(chosen)} 个行动，闭合 {closed} 条未闭合项")
        except StructuredCallFailed as e:
            print(f"[Act]    ❌ 失败: {str(e)[:150]}")

    # ---- 9 Reckon ----
    if due["predictions"] or due["commitments"]:
        items = [{"id": p["id"], "type": "prediction", "claim": p["claim"],
                  "verify_method": p["verify_method"], "made_day": p["made_day"]}
                 for p in due["predictions"]]
        items += [{"id": c["id"], "type": "commitment", "statement": c["statement"],
                   "made_day": c["made_day"]} for c in due["commitments"]]
        try:
            rec = call_structured(
                "reckon",
                "你是一个自主 Agent 的 Reckon 环节：对今日到期的预测与承诺做对账。\n"
                "预测 outcome ∈ true/false/moot；承诺 outcome ∈ kept/broken。\n"
                "⚠️ outcome=false 时 postmortem 必须写清楚**为什么错了**（≥12 字），"
                "这是硬约束，写不出就不要判 false。其他情况 postmortem 可留空字符串。",
                f"{ctx}\n\n今日到期，逐条对账：\n" + json.dumps(items, ensure_ascii=False, indent=1),
                "submit_settlements", RECKON_SCHEMA, max_tokens=12000,
                extra_check=lambda d: next(
                    (f"{s['id']} 判 false 但 postmortem 少于 12 字" for s in d["settlements"]
                     if s["outcome"] == "false" and len(s["postmortem"].strip()) < 12), None))
            for s in rec["settlements"]:
                try:
                    if s["outcome"] in ("kept", "broken"):
                        L.settle_commitment(s["id"], day, s["outcome"])
                    else:
                        L.settle_prediction(s["id"], day, s["outcome"],
                                            s["postmortem"] or None)
                    lag = day - next(i["made_day"] for i in items if i["id"] == s["id"])
                    print(f"[Reckon] {s['id']} → {s['outcome']}  (滞后 {lag} 天)"
                          + (f"\n           {s['postmortem'][:80]}" if s["postmortem"] else ""))
                except LedgerViolation as e:
                    print(f"  ⚠️ 对账被拦: {e}")
        except StructuredCallFailed as e:
            print(f"[Reckon] ❌ 失败: {str(e)[:150]}")

    # ---- 10 Sleep ----
    st = L.stats()
    print(f"[Sleep]  账本: 未闭合 {st['open']} / 已闭合 {st['closed']} / "
          f"已放弃 {st['abandoned']} / 待对账预测 {st['predictions_unsettled']}")
    L.log(day, "sleep", json.dumps(st, sort_keys=True))
    L.finish_day(day, real, SLOTS, used)      # 只有走到这里才算这天完整跑完


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else None
    L, W = Ledger(DB), ReplayWorld()
    total = n or len(W)
    done = L.completed_days()
    if done:
        print(f"续跑：第 {min(done)}~{max(done)} 天已完成（共 {len(done)} 天），将跳过")

    for day in range(1, total + 1):
        if day in done:
            continue
        # 上次可能死在这一天中间（余额耗尽、网络中断），先回滚再重跑
        if dirty := L.rollback_day(day):
            print(f"⟲ 第 {day} 天有 {dirty} 条未完成的写入，已回滚后重跑")
        try:
            run_day(L, W, day)
        except InsufficientBalance as e:
            print(f"\n{'='*64}\n⛔ {e}")
            print(f"\n已完整跑完 {len(L.completed_days())} 天，结果全部保留在 {DB}。")
            print("充值后直接重跑同一条命令即可，已完成的天会自动跳过。")
            return 2
    print(f"\n{'='*64}\n最终账本: {json.dumps(L.stats(), ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
