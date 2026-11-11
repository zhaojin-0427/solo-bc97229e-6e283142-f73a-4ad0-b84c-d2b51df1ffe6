"""工时入仓：批次导入（幂等）、批次/正式/隔离明细查询。

导入流程（单个 BEGIN IMMEDIATE 事务内完成）：
  1. 批次号已存在 -> 比对内容哈希：同内容返回原批回执（幂等重传），异内容拒绝；
  2. 固定当前最大归属修订号（pinned_revision），与后续归属读取处于同一写锁下，
     不会被并发补录插队，保证“导入时固定当前归属修订”；
  3. 逐条按固定修订号归属：已归属行写入正式工时表（保留所用修订号，后续不改写），
     未归属行写入隔离区（PENDING，等待人事补录后的后台重查）；
  4. 原始批次头与批文明细只追加落库，任何失败整体回滚。
"""
from __future__ import annotations

import hashlib
import json

from .database import get_conn
from .errors import AppError
from .schemas import TimesheetBatchImportRequest, TimesheetEntryIn
from .service import lookup_attribution


def _content_hash(entries: list[TimesheetEntryIn]) -> str:
    """批内容的规范化哈希：按 timesheet_id 排序后序列化，字段顺序固定。

    同一批明细以不同顺序重传仍视为同内容；任一字段不同则哈希不同。
    """
    canonical = [
        {
            "timesheet_id": e.timesheet_id,
            "employee_id": e.employee_id,
            "work_date": e.work_date.isoformat(),
            "hours": e.hours,
        }
        for e in sorted(entries, key=lambda x: x.timesheet_id)
    ]
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _receipt(row, replayed: bool) -> dict:
    return {
        "batch_no": row["batch_no"],
        "content_hash": row["content_hash"],
        "pinned_revision": row["pinned_revision"],
        "row_count": row["row_count"],
        "official_count": row["official_count"],
        "quarantined_count": row["quarantined_count"],
        "replayed": replayed,
        "created_at": row["created_at"],
    }


def import_batch(req: TimesheetBatchImportRequest) -> tuple[dict, bool]:
    """导入一批工时，返回 (回执, 是否新批次)。

    - 同批同内容重传：返回原批回执（replayed=True），不产生任何写入；
    - 同批号异内容：抛出 409 BATCH_CONTENT_MISMATCH；
    - 批内 timesheet_id 重复：抛出 400 DUPLICATE_TIMESHEET_ID。
    """
    # 批内唯一性校验（先于哈希与落库；非法批次永远不会被接受，重传同样被拒绝）
    seen: set[str] = set()
    for idx, e in enumerate(req.entries):
        if e.timesheet_id in seen:
            raise AppError(
                code="DUPLICATE_TIMESHEET_ID",
                message=f"工时ID {e.timesheet_id} 在批次 {req.batch_no} 内重复",
                details={"batch_no": req.batch_no, "timesheet_id": e.timesheet_id, "index": idx},
            )
        seen.add(e.timesheet_id)

    content_hash = _content_hash(req.entries)

    with get_conn() as conn:
        # 写锁贯穿“幂等判定 + 固定修订号 + 归属 + 落库”，与并发补录/导入互斥。
        conn.execute("BEGIN IMMEDIATE")

        existing = conn.execute(
            "SELECT * FROM timesheet_batches WHERE batch_no = ?", (req.batch_no,)
        ).fetchone()
        if existing is not None:
            if existing["content_hash"] == content_hash:
                # 同批同内容重传：原样返回首次导入的回执，不重复入仓。
                return _receipt(existing, replayed=True), False
            raise AppError(
                code="BATCH_CONTENT_MISMATCH",
                message=(
                    f"批次号 {req.batch_no} 已存在但内容不一致："
                    "同批号仅允许同内容重传，异内容请使用新的批次号"
                ),
                status_code=409,
                details={
                    "batch_no": req.batch_no,
                    "existing_content_hash": existing["content_hash"],
                    "incoming_content_hash": content_hash,
                },
            )

        # 固定当前归属修订号：同一写锁下读取，并发补录不会插队。
        pinned_revision = conn.execute(
            "SELECT COALESCE(MAX(revision_id), 0) AS r FROM revisions"
        ).fetchone()["r"]

        # 逐条归属（计算在本事务的读快照内，正式行固定所用修订号）。
        resolved = [
            (e, *lookup_attribution(conn, pinned_revision, e.employee_id, e.work_date.isoformat()))
            for e in req.entries
        ]
        official_count = sum(1 for _, cc, _ in resolved if cc is not None)
        quarantined_count = len(resolved) - official_count

        # 原始批次头 + 批文明细（只追加）。
        conn.execute(
            """
            INSERT INTO timesheet_batches
                (batch_no, content_hash, pinned_revision, row_count,
                 official_count, quarantined_count)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (req.batch_no, content_hash, pinned_revision, len(resolved),
             official_count, quarantined_count),
        )
        for seq, (e, cost_center, matched_revision) in enumerate(resolved):
            conn.execute(
                """
                INSERT INTO timesheet_entries
                    (batch_no, timesheet_id, seq, employee_id, work_date, hours)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (req.batch_no, e.timesheet_id, seq, e.employee_id,
                 e.work_date.isoformat(), e.hours),
            )
            if cost_center is not None:
                # 已归属 -> 正式工时表，保留所用修订号，后续补录不改写。
                conn.execute(
                    """
                    INSERT INTO official_timesheets
                        (batch_no, timesheet_id, employee_id, work_date, hours,
                         cost_center, attribution_revision, matched_revision_id, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'IMPORTED')
                    """,
                    (req.batch_no, e.timesheet_id, e.employee_id, e.work_date.isoformat(),
                     e.hours, cost_center, pinned_revision, matched_revision),
                )
            else:
                # 未归属 -> 隔离区，等待补录后的后台重查。
                conn.execute(
                    """
                    INSERT INTO quarantine_entries
                        (batch_no, timesheet_id, employee_id, work_date, hours,
                         first_seen_revision)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (req.batch_no, e.timesheet_id, e.employee_id, e.work_date.isoformat(),
                     e.hours, pinned_revision),
                )

        row = conn.execute(
            "SELECT * FROM timesheet_batches WHERE batch_no = ?", (req.batch_no,)
        ).fetchone()
        return _receipt(row, replayed=False), True


def list_batches() -> list[dict]:
    """全部批次的概要（计数为导入时刻快照）。"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT batch_no, pinned_revision, row_count, official_count,
                   quarantined_count, created_at
            FROM timesheet_batches
            ORDER BY created_at, batch_no
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_batch_detail(batch_no: str) -> dict:
    """批次详情：概要 + 当前正式明细 + 当前隔离明细（含已转正行）。"""
    with get_conn() as conn:
        batch = conn.execute(
            "SELECT * FROM timesheet_batches WHERE batch_no = ?", (batch_no,)
        ).fetchone()
        if batch is None:
            raise AppError(
                code="BATCH_NOT_FOUND",
                message=f"批次 {batch_no} 不存在",
                status_code=404,
            )
        official = conn.execute(
            """
            SELECT batch_no, timesheet_id, employee_id, work_date, hours,
                   cost_center, attribution_revision, matched_revision_id, source, created_at
            FROM official_timesheets
            WHERE batch_no = ?
            ORDER BY timesheet_id
            """,
            (batch_no,),
        ).fetchall()
        quarantined = conn.execute(
            """
            SELECT batch_no, timesheet_id, employee_id, work_date, hours,
                   status, first_seen_revision, resolved_revision, created_at, updated_at
            FROM quarantine_entries
            WHERE batch_no = ?
            ORDER BY timesheet_id
            """,
            (batch_no,),
        ).fetchall()
    return {
        "batch_no": batch["batch_no"],
        "pinned_revision": batch["pinned_revision"],
        "row_count": batch["row_count"],
        "import_official_count": batch["official_count"],
        "import_quarantined_count": batch["quarantined_count"],
        "created_at": batch["created_at"],
        "official_entries": [dict(r) for r in official],
        "quarantine_entries": [dict(r) for r in quarantined],
    }


def list_official_timesheets(
    batch_no: str | None = None,
    employee_id: str | None = None,
    work_date_from: str | None = None,
    work_date_to: str | None = None,
) -> list[dict]:
    """下游读取口径：只暴露正式工时表，隔离区数据对下游不可见。"""
    sql = [
        """
        SELECT batch_no, timesheet_id, employee_id, work_date, hours,
               cost_center, attribution_revision, matched_revision_id, source, created_at
        FROM official_timesheets
        """
    ]
    clauses: list[str] = []
    params: list[str] = []
    if batch_no:
        clauses.append("batch_no = ?")
        params.append(batch_no)
    if employee_id:
        clauses.append("employee_id = ?")
        params.append(employee_id)
    if work_date_from:
        clauses.append("work_date >= ?")
        params.append(work_date_from)
    if work_date_to:
        clauses.append("work_date <= ?")
        params.append(work_date_to)
    if clauses:
        sql.append("WHERE " + " AND ".join(clauses))
    sql.append("ORDER BY work_date, employee_id, batch_no, timesheet_id")
    with get_conn() as conn:
        rows = conn.execute(" ".join(sql), params).fetchall()
    return [dict(r) for r in rows]


def list_recheck_requests() -> list[dict]:
    """待重查标记列表（审计/观测后台重查进度）。"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, revision_id, status, promoted_count, created_at, processed_at
            FROM recheck_requests
            ORDER BY id
            """
        ).fetchall()
    return [dict(r) for r in rows]
