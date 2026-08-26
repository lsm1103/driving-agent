"""验证四条硬约束真的会拦。拦不住，"偷懒得不到"就是装饰（docs/03 §3.2）。"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ledger.store import Ledger, LedgerViolation

results = []


def expect_reject(name, fn):
    try:
        fn()
        results.append((name, False, "❌ 没有拦住 —— 约束失效"))
    except LedgerViolation as e:
        results.append((name, True, f"✅ 拦住了: {str(e).split(':')[1].strip()[:52]}"))


def expect_ok(name, fn):
    try:
        fn()
        results.append((name, True, "✅ 正常写入"))
    except LedgerViolation as e:
        results.append((name, False, f"❌ 误拦: {e}"))


with tempfile.TemporaryDirectory() as tmp:
    L = Ledger(f"{tmp}/t.db")

    # 硬约束 1：闭合条件
    expect_reject("① 闭合条件为空",
                  lambda: L.open_loop("OL-1", 1, "exploration", "读那篇论文", ""))
    expect_reject("① 闭合条件太短（伪条件）",
                  lambda: L.open_loop("OL-2", 1, "exploration", "读那篇论文", "读完就行"))
    expect_ok("① 可检验的闭合条件",
              lambda: L.open_loop("OL-3", 1, "exploration", "研究 HERO 的记忆组织",
                                  "与我的跨天连续性设计逐条对比，产出一条可证伪判断"))

    # 硬约束 2：放弃理由
    expect_reject("② 放弃不写理由", lambda: L.abandon_loop("OL-3", 5, ""))
    expect_ok("② 放弃写了理由",
              lambda: L.abandon_loop("OL-3", 5, "该方向与动机机制关系太远，本周已有 3 个未闭合探索项"))

    # 硬约束 3：拒绝理由
    expect_reject("③ 未选中不写拒绝理由", lambda: L.record_decisions(1, [
        {"id": "C1", "proposal": "跟进新论文", "cost_slots": 2, "chosen": 0}], {}))
    expect_ok("③ 未选中写了拒绝理由", lambda: L.record_decisions(1, [
        {"id": "C2", "proposal": "跟进新论文", "cost_slots": 2, "chosen": 0,
         "reject_reason": "本周已有 2 个未闭合探索项，再开会加剧漂移"}], {}))
    expect_ok("③ 选中的无需理由", lambda: L.record_decisions(1, [
        {"id": "C3", "proposal": "对账 PR-1", "cost_slots": 1, "chosen": 1}], {}))

    # 硬约束 4：postmortem
    L.predict("PR-1", 1, 8, "超过 5 层子 agent 会丢上下文",
              "实测搭 9 层子 agent，检查上下文完整性", 0.7, {"exploration": 0.8})
    expect_reject("④ 证伪不写 postmortem",
                  lambda: L.settle_prediction("PR-1", 8, "false"))
    expect_ok("④ 证伪写了 postmortem", lambda: L.settle_prediction(
        "PR-1", 8, "false", "实测 9 层仍正常。把上下文压缩误判成了丢失。"))

    # 附带：承诺到期日必须晚于做出日
    expect_reject("⑤ 承诺到期日不晚于做出日",
                  lambda: L.commit_to("CM-1", 5, 5, "三天内给个结论"))
    expect_ok("⑤ 正常承诺", lambda: L.commit_to("CM-2", 5, 8, "三天内给 OL-3 一个结论"))

print(f"\n{'='*62}")
for name, ok, msg in results:
    print(f"  {name:26s} {msg}")
passed = sum(ok for _, ok, _ in results)
print(f"{'='*62}\n{passed}/{len(results)} 通过")
sys.exit(0 if passed == len(results) else 1)
