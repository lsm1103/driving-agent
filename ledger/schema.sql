-- 未闭合项账本（docs/03 §3.2）
--
-- 四条硬约束用 CHECK 落在 SQL 层，不放在 Python 里 ——
-- 放在 Python 里，任何一条新的写入路径都可能绕过去。
-- 这四条是让"偷懒得不到"落到代码里的地方。

PRAGMA foreign_keys = ON;

-- 未闭合项
CREATE TABLE IF NOT EXISTS open_loop (
  id              TEXT PRIMARY KEY,
  opened_day      INTEGER NOT NULL,
  kind            TEXT NOT NULL CHECK (kind IN ('exploration','maintenance','social','meaning')),
  statement       TEXT NOT NULL CHECK (length(trim(statement)) > 0),
  close_condition TEXT NOT NULL,
  due_day         INTEGER,
  status          TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open','closed','falsified','abandoned','stale')),
  closed_day      INTEGER,
  abandon_reason  TEXT,
  origin_signal   TEXT,
  related         TEXT NOT NULL DEFAULT '[]',

  -- 硬约束 1：不允许登记没有闭合条件的未闭合项
  CHECK (length(trim(close_condition)) >= 12),
  -- 硬约束 2：主动放弃必须写理由
  CHECK (status <> 'abandoned' OR length(trim(coalesce(abandon_reason,''))) > 0)
);

-- 承诺：Agent 给未来的自己制造的债（docs/03 §2.6）
CREATE TABLE IF NOT EXISTS commitment (
  id          TEXT PRIMARY KEY,
  made_day    INTEGER NOT NULL,
  due_day     INTEGER NOT NULL,
  statement   TEXT NOT NULL CHECK (length(trim(statement)) > 0),
  target_loop TEXT REFERENCES open_loop(id),
  status      TEXT NOT NULL DEFAULT 'pending'
              CHECK (status IN ('pending','kept','broken','renegotiated')),
  settled_day INTEGER,
  lateness    INTEGER,
  CHECK (due_day > made_day)
);

-- 预测：可证伪断言 + 做出时的状态快照
CREATE TABLE IF NOT EXISTS prediction (
  id             TEXT PRIMARY KEY,
  made_day       INTEGER NOT NULL,
  due_day        INTEGER NOT NULL,
  claim          TEXT NOT NULL CHECK (length(trim(claim)) > 0),
  verify_method  TEXT NOT NULL CHECK (length(trim(verify_method)) >= 12),
  confidence     REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  state_snapshot TEXT NOT NULL,
  outcome        TEXT CHECK (outcome IS NULL OR outcome IN ('true','false','moot')),
  settled_day    INTEGER,
  postmortem     TEXT,

  -- 硬约束 4：证伪必须写清楚为什么错了
  CHECK (outcome <> 'false' OR length(trim(coalesce(postmortem,''))) >= 12),
  CHECK (due_day > made_day)
);

-- 每日决策记录：被放弃的候选是机会成本存在过的唯一证据
CREATE TABLE IF NOT EXISTS decision (
  day            INTEGER NOT NULL,
  candidate_id   TEXT NOT NULL,
  proposal       TEXT NOT NULL CHECK (length(trim(proposal)) > 0),
  cost_slots     INTEGER NOT NULL CHECK (cost_slots > 0),
  closes_loops   TEXT NOT NULL DEFAULT '[]',
  chosen         INTEGER NOT NULL CHECK (chosen IN (0,1)),
  reject_reason  TEXT,
  drive_snapshot TEXT NOT NULL,
  PRIMARY KEY (day, candidate_id),

  -- 硬约束 3：未选中的必须写拒绝理由
  CHECK (chosen = 1 OR length(trim(coalesce(reject_reason,''))) >= 8)
);

-- 每日事件日志（追加式，docs/03 §3.2 增量持久化）
CREATE TABLE IF NOT EXISTS event (
  day    INTEGER NOT NULL,
  seq    INTEGER NOT NULL,
  step   TEXT NOT NULL,
  detail TEXT NOT NULL,
  PRIMARY KEY (day, seq)
);

-- 实验元信息：day 计数与真实日期的对应
CREATE TABLE IF NOT EXISTS day_log (
  day          INTEGER PRIMARY KEY,
  real_date    TEXT NOT NULL,
  slots_total  INTEGER NOT NULL,
  slots_used   INTEGER NOT NULL DEFAULT 0,
  cost_spent   REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_ol_status ON open_loop(status, due_day);
CREATE INDEX IF NOT EXISTS idx_cm_status ON commitment(status, due_day);
CREATE INDEX IF NOT EXISTS idx_pr_outcome ON prediction(outcome, due_day);
