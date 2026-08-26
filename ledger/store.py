"""未闭合项账本（docs/03 §3.2）。

三条设计约束在这里落地：
  1. 四条硬约束由 SQL CHECK 强制，任何写入路径都绕不过（见 schema.sql）
  2. 增量持久化：每次写入立即提交，不攒批（docs/03 §3.2 —— 花 $0.46 买的教训）
  3. summary() 的输出必须逐字节稳定，它是缓存前缀（docs/03 §3.8，命中差 30 倍）
"""
import json, sqlite3
from pathlib import Path

SCHEMA = Path(__file__).parent / "schema.sql"


class LedgerViolation(RuntimeError):
    """撞上硬约束。不要 catch 了糊过去 —— 这是设计要拦的东西。"""


_HINTS = {
    "close_condition": "未闭合项必须有 ≥12 字的可检验闭合条件（硬约束 1）",
    "abandon_reason": "主动放弃必须写理由（硬约束 2）",
    "reject_reason": "未选中的候选必须写拒绝理由 ≥8 字（硬约束 3）",
    "postmortem": "预测被证伪必须写 ≥12 字的 postmortem（硬约束 4）",
    "verify_method": "预测必须写明 ≥12 字的验证方法",
}


class Ledger:
    def __init__(self, path="fixtures/ledger.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA.read_text(encoding="utf-8"))
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(decision)")}
        if "rationale" not in cols:          # 老库迁移
            self.db.execute("ALTER TABLE decision ADD COLUMN rationale TEXT NOT NULL DEFAULT ''")
        self.db.commit()

    def _write(self, sql, params, ctx=""):
        try:
            self.db.execute(sql, params)
            self.db.commit()          # 立即落盘，不攒批
        except sqlite3.IntegrityError as e:
            for field, hint in _HINTS.items():
                if field in str(e):
                    raise LedgerViolation(f"{ctx}: {hint}  [{e}]") from e
            raise LedgerViolation(f"{ctx}: {e}") from e

    # ---------- 未闭合项 ----------
    def open_loop(self, id, day, kind, statement, close_condition,
                  due_day=None, origin_signal=None, related=()):
        self._write(
            "INSERT INTO open_loop(id,opened_day,kind,statement,close_condition,"
            "due_day,origin_signal,related) VALUES(?,?,?,?,?,?,?,?)",
            (id, day, kind, statement, close_condition, due_day, origin_signal,
             json.dumps(sorted(related))), f"登记 {id}")
        return id

    def close_loop(self, id, day, status="closed"):
        self._write("UPDATE open_loop SET status=?,closed_day=? WHERE id=?",
                    (status, day, id), f"闭合 {id}")

    def abandon_loop(self, id, day, reason):
        self._write("UPDATE open_loop SET status='abandoned',closed_day=?,abandon_reason=? "
                    "WHERE id=?", (day, reason, id), f"放弃 {id}")

    def mark_stale(self, day, after_days):
        """超过 K 天无进展 → stale，强制进入次日候选（docs/03 §3.5）。"""
        cur = self.db.execute(
            "UPDATE open_loop SET status='stale' WHERE status='open' AND ?-opened_day > ?",
            (day, after_days))
        self.db.commit()
        return cur.rowcount

    # ---------- 承诺：给未来的自己制造债 ----------
    def commit_to(self, id, day, due_day, statement, target_loop=None):
        self._write("INSERT INTO commitment(id,made_day,due_day,statement,target_loop) "
                    "VALUES(?,?,?,?,?)", (id, day, due_day, statement, target_loop),
                    f"承诺 {id}")
        return id

    def settle_commitment(self, id, day, status):
        row = self.db.execute("SELECT due_day FROM commitment WHERE id=?", (id,)).fetchone()
        if row is None:
            raise LedgerViolation(f"承诺 {id} 不存在")
        self._write("UPDATE commitment SET status=?,settled_day=?,lateness=? WHERE id=?",
                    (status, day, max(0, day - row["due_day"]), id), f"结清承诺 {id}")

    # ---------- 预测 ----------
    def predict(self, id, day, due_day, claim, verify_method, confidence, snapshot):
        self._write("INSERT INTO prediction(id,made_day,due_day,claim,verify_method,"
                    "confidence,state_snapshot) VALUES(?,?,?,?,?,?,?)",
                    (id, day, due_day, claim, verify_method, confidence,
                     json.dumps(snapshot, sort_keys=True, ensure_ascii=False)), f"预测 {id}")
        return id

    def settle_prediction(self, id, day, outcome, postmortem=None):
        self._write("UPDATE prediction SET outcome=?,settled_day=?,postmortem=? WHERE id=?",
                    (outcome, day, postmortem, id), f"对账预测 {id}")

    # ---------- 决策：被放弃的候选是机会成本的唯一证据 ----------
    def record_decisions(self, day, candidates, drive_snapshot):
        snap = json.dumps(drive_snapshot, sort_keys=True, ensure_ascii=False)
        for c in candidates:
            self._write(
                "INSERT OR REPLACE INTO decision(day,candidate_id,proposal,cost_slots,"
                "closes_loops,chosen,rationale,reject_reason,drive_snapshot) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (day, c["id"], c["proposal"], c["cost_slots"],
                 json.dumps(sorted(c.get("closes_loops", []))), int(c["chosen"]),
                 c.get("rationale", ""), c.get("reject_reason"), snap), f"决策 {c['id']}")

    # ---------- 续跑支持 ----------
    def finish_day(self, day, real_date, slots_total, slots_used):
        """标记这一天已完整跑完。只有写了这条，续跑才会跳过它。"""
        self._write("INSERT OR REPLACE INTO day_log(day,real_date,slots_total,slots_used) "
                    "VALUES(?,?,?,?)", (day, real_date, slots_total, slots_used), f"收尾第{day}天")

    def completed_days(self):
        return {r[0] for r in self.db.execute("SELECT day FROM day_log ORDER BY day")}

    def rollback_day(self, day):
        """清掉某天的部分写入。

        进程可能死在一天中间（余额耗尽、网络中断），此时账本里留着半天的数据。
        直接续跑会撞主键冲突，所以先回滚到干净状态再重跑这一天。
        """
        n = self.db.execute("SELECT count(*) FROM open_loop WHERE opened_day=?",
                            (day,)).fetchone()[0]
        # 撤销这一天做出的闭合与放弃，让那些项回到 open
        self.db.execute("UPDATE open_loop SET status='open',closed_day=NULL,abandon_reason=NULL "
                        "WHERE closed_day=?", (day,))
        for sql in ("DELETE FROM open_loop WHERE opened_day=?",
                    "DELETE FROM prediction WHERE made_day=?",
                    "DELETE FROM commitment WHERE made_day=?",
                    "DELETE FROM decision WHERE day=?",
                    "DELETE FROM event WHERE day=?"):
            self.db.execute(sql, (day,))
        # 撤销这一天做出的对账
        self.db.execute("UPDATE prediction SET outcome=NULL,settled_day=NULL,postmortem=NULL "
                        "WHERE settled_day=?", (day,))
        self.db.execute("UPDATE commitment SET status='pending',settled_day=NULL,lateness=NULL "
                        "WHERE settled_day=?", (day,))
        self.db.commit()
        return n

    def log(self, day, step, detail):
        seq = self.db.execute("SELECT coalesce(max(seq),0)+1 FROM event WHERE day=?",
                              (day,)).fetchone()[0]
        self._write("INSERT INTO event(day,seq,step,detail) VALUES(?,?,?,?)",
                    (day, seq, step, detail), "日志")

    # ---------- 读 ----------
    def due(self, day):
        """今日到期。架构保证它们被摆上台面；处不处理由 Agent 自己决定（docs/03 §3.5）。"""
        q = lambda sql: [dict(r) for r in self.db.execute(sql, (day,))]
        return {
            "predictions": q("SELECT * FROM prediction WHERE outcome IS NULL AND due_day<=? "
                             "ORDER BY due_day, id"),
            "commitments": q("SELECT * FROM commitment WHERE status='pending' AND due_day<=? "
                             "ORDER BY due_day, id"),
            "stale": q("SELECT * FROM open_loop WHERE status='stale' AND opened_day<=? "
                       "ORDER BY opened_day, id"),
        }

    def open_loops(self):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM open_loop WHERE status IN ('open','stale') ORDER BY opened_day, id")]

    def summary(self, day):
        """账本摘要 —— 这是缓存前缀，必须逐字节稳定（docs/03 §3.8）。

        所有查询显式 ORDER BY，字段顺序固定，不含时间戳与随机内容。
        同一天内 Intake/Propose/Compete/Reckon 四次调用共享同一份前缀。
        """
        loops = self.open_loops()
        lines = [f"账本状态（第 {day} 天）", f"未闭合项 {len(loops)} 条："]
        for l in loops:
            age = day - l["opened_day"]
            due = f" 到期第{l['due_day']}天" if l["due_day"] else ""
            lines.append(f"  [{l['id']}] ({l['kind']}, 第{l['opened_day']}天登记, "
                         f"已{age}天{due}) {l['statement']}")
            lines.append(f"      闭合条件: {l['close_condition']}")

        pend = [dict(r) for r in self.db.execute(
            "SELECT id,due_day,statement FROM commitment WHERE status='pending' "
            "ORDER BY due_day, id")]
        lines.append(f"未兑现承诺 {len(pend)} 条：")
        lines += [f"  [{c['id']}] 到期第{c['due_day']}天: {c['statement']}" for c in pend]

        unset = [dict(r) for r in self.db.execute(
            "SELECT id,due_day,claim,confidence FROM prediction WHERE outcome IS NULL "
            "ORDER BY due_day, id")]
        lines.append(f"待对账预测 {len(unset)} 条：")
        lines += [f"  [{p['id']}] 到期第{p['due_day']}天 (置信 {p['confidence']:.2f}): "
                  f"{p['claim']}" for p in unset]

        kept, broken = self.db.execute(
            "SELECT sum(status='kept'), sum(status='broken') FROM commitment").fetchone()
        lines.append(f"承诺可靠性: {kept or 0}/{(kept or 0)+(broken or 0)}")
        return "\n".join(lines)

    def stats(self):
        row = lambda sql: self.db.execute(sql).fetchone()[0]
        return {
            "open": row("SELECT count(*) FROM open_loop WHERE status IN ('open','stale')"),
            "closed": row("SELECT count(*) FROM open_loop WHERE status='closed'"),
            "abandoned": row("SELECT count(*) FROM open_loop WHERE status='abandoned'"),
            "commitments_pending": row("SELECT count(*) FROM commitment WHERE status='pending'"),
            "predictions_unsettled": row("SELECT count(*) FROM prediction WHERE outcome IS NULL"),
        }
