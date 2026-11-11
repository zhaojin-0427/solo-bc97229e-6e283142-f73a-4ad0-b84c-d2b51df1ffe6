"""SQLite 连接与建表。

归属部分只追加（append-only）：revisions / attribution_segments 两张表均不做
UPDATE/DELETE，任一历史修订的可见数据因此永久不变。

工时入仓部分：
- timesheet_batches / timesheet_entries 保存原始批次（只追加），批次号为幂等键；
- official_timesheets 为正式工时表，只插入不改写，行内保留归属所用修订号；
- quarantine_entries 为隔离区，仅 status 等状态字段会随转正更新；
- recheck_requests 为待重查标记，与人事补录在同一事务落库，
  后台重查线程据此在归属写事务之外完成转正，重启后不漏查。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# 允许通过环境变量指定数据库文件路径（测试与 Docker 都会用到）。
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "attribution.db"


def get_db_path() -> Path:
    return Path(os.environ.get("ATTRIBUTION_DB_PATH", str(DEFAULT_DB_PATH)))


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path),
        timeout=30.0,
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    conn.row_factory = sqlite3.Row
    # WAL 提升读写并发；外键约束开启。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """请求级连接：正常结束提交，异常回滚并关闭。"""
    conn = _connect(get_db_path())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS revisions (
    revision_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    entry_count      INTEGER NOT NULL,
    note             TEXT
);

CREATE TABLE IF NOT EXISTS attribution_segments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id     INTEGER NOT NULL REFERENCES revisions(revision_id),
    employee_id     TEXT    NOT NULL,
    cost_center     TEXT    NOT NULL,
    effective_from  TEXT    NOT NULL,  -- ISO yyyy-mm-dd，区间左端点（含）
    effective_to    TEXT    NOT NULL,  -- ISO yyyy-mm-dd，区间右端点（不含）
    CHECK (effective_from < effective_to)
);

-- 查询路径：按员工 + 日期点 + 修订号过滤后取最大修订号
CREATE INDEX IF NOT EXISTS idx_segments_emp_dates_rev
    ON attribution_segments (employee_id, effective_from, effective_to, revision_id);

CREATE INDEX IF NOT EXISTS idx_segments_revision
    ON attribution_segments (revision_id);

-- ======================= 工时入仓 =======================

-- 原始批次头：批次号为幂等键；content_hash 判定“同内容重传”；
-- pinned_revision 为导入时固定的归属修订号；计数为导入时刻快照（重传原样返回）。
CREATE TABLE IF NOT EXISTS timesheet_batches (
    batch_no          TEXT PRIMARY KEY,
    content_hash      TEXT    NOT NULL,
    pinned_revision   INTEGER NOT NULL,
    row_count         INTEGER NOT NULL,
    official_count    INTEGER NOT NULL,
    quarantined_count INTEGER NOT NULL,
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 原始批文明细（只追加，不改写），批内 timesheet_id 唯一。
CREATE TABLE IF NOT EXISTS timesheet_entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no     TEXT    NOT NULL REFERENCES timesheet_batches(batch_no),
    timesheet_id TEXT    NOT NULL,
    seq          INTEGER NOT NULL,          -- 批内顺序，便于审计回放
    employee_id  TEXT    NOT NULL,
    work_date    TEXT    NOT NULL,          -- ISO yyyy-mm-dd
    hours        REAL    NOT NULL CHECK (hours > 0),
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (batch_no, timesheet_id)
);

-- 正式工时表：下游唯一读取口径。只插入不改写，
-- attribution_revision 固定为归属所用修订号，后续补录不回写。
CREATE TABLE IF NOT EXISTS official_timesheets (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no             TEXT    NOT NULL REFERENCES timesheet_batches(batch_no),
    timesheet_id         TEXT    NOT NULL,
    employee_id          TEXT    NOT NULL,
    work_date            TEXT    NOT NULL,
    hours                REAL    NOT NULL CHECK (hours > 0),
    cost_center          TEXT    NOT NULL,
    attribution_revision INTEGER NOT NULL,  -- 归属计算所用的修订号（导入或转正时固定）
    matched_revision_id  INTEGER NOT NULL,  -- 命中区间所属的修订号（审计）
    source               TEXT    NOT NULL CHECK (source IN ('IMPORTED', 'PROMOTED')),
    created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (batch_no, timesheet_id)         -- 防重复转正/重复入仓
);

-- 隔离区：未归属行在此等待人事补录；转正时 status PENDING -> PROMOTED，
-- 状态更新与写入正式表在同一事务提交。
CREATE TABLE IF NOT EXISTS quarantine_entries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no            TEXT    NOT NULL REFERENCES timesheet_batches(batch_no),
    timesheet_id        TEXT    NOT NULL,
    employee_id         TEXT    NOT NULL,
    work_date           TEXT    NOT NULL,
    hours               REAL    NOT NULL CHECK (hours > 0),
    status              TEXT    NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING', 'PROMOTED')),
    first_seen_revision INTEGER NOT NULL,   -- 入隔离区时固定的修订号
    resolved_revision   INTEGER,            -- 转正时所用修订号
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (batch_no, timesheet_id)
);

-- 待重查标记：与人事补录（revisions）在同一事务落库；
-- 后台线程据此重查隔离行，处理完成置 DONE。服务重启后 PENDING 仍在，不漏查。
CREATE TABLE IF NOT EXISTS recheck_requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id    INTEGER NOT NULL REFERENCES revisions(revision_id),
    status         TEXT    NOT NULL DEFAULT 'PENDING'
                   CHECK (status IN ('PENDING', 'DONE')),
    promoted_count INTEGER,                 -- 本次重查转正行数
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    processed_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_entries_batch      ON timesheet_entries (batch_no);
CREATE INDEX IF NOT EXISTS idx_official_batch     ON official_timesheets (batch_no);
CREATE INDEX IF NOT EXISTS idx_official_emp_date  ON official_timesheets (employee_id, work_date);
CREATE INDEX IF NOT EXISTS idx_quarantine_status  ON quarantine_entries (status);
CREATE INDEX IF NOT EXISTS idx_recheck_status     ON recheck_requests (status);
"""


def init_db() -> None:
    """创建表与索引（幂等）。"""
    with get_conn() as conn:
        conn.executescript(SCHEMA_SQL)
