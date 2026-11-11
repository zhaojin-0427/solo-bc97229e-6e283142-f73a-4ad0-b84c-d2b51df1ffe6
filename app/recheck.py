"""后台重查：人事补录后自动重查隔离行，归属补齐才转正。

设计要点：
- 补录事务只负责落「待重查标记」（recheck_requests，见 service.submit_revision），
  重查计算完全在归属写事务之外进行；
- 每轮处理：读快照（当前最大修订号 + 全部 PENDING 隔离行）-> 计算归属 ->
  写事务（BEGIN IMMEDIATE）内逐行转正并同事务更新隔离状态与标记，
  转正与隔离状态更新一起提交；
- 写事务内以 UPDATE ... WHERE status='PENDING' 的受影响行数作为转正闸门，
  配合 official_timesheets 的 UNIQUE(batch_no, timesheet_id)，
  并发重查或重复触发都不会重复转正；
- PENDING 标记持久化在 SQLite 中，服务重启后后台线程启动即扫尾，不漏查。
"""
from __future__ import annotations

import logging
import os
import threading

from .database import get_conn
from .service import lookup_attribution

logger = logging.getLogger("recheck")

_stop_event = threading.Event()
_wake_event = threading.Event()
_worker: threading.Thread | None = None


def _poll_interval_seconds() -> float:
    try:
        ms = int(os.environ.get("RECHECK_POLL_INTERVAL_MS", "500"))
    except ValueError:
        ms = 500
    return max(ms, 50) / 1000.0


def _process_one_request() -> dict | None:
    """处理最早一条 PENDING 标记；没有待处理标记时返回 None。

    读计算与写提交分离：先在读快照中按当前最大修订号算出可转正行，
    再在独立的写事务中逐行转正 + 更新隔离状态 + 标记 DONE，一起提交。
    """
    # ---- 读快照：标记、当前修订号、待转正计算（均在任何写事务之外） ----
    with get_conn() as conn:
        req = conn.execute(
            "SELECT id, revision_id FROM recheck_requests WHERE status = 'PENDING' ORDER BY id LIMIT 1"
        ).fetchone()
        if req is None:
            return None
        revision = conn.execute(
            "SELECT COALESCE(MAX(revision_id), 0) AS r FROM revisions"
        ).fetchone()["r"]
        rows = conn.execute(
            """
            SELECT id, batch_no, timesheet_id, employee_id, work_date, hours
            FROM quarantine_entries
            WHERE status = 'PENDING'
            ORDER BY id
            """
        ).fetchall()
        promotions = []
        for row in rows:
            cost_center, matched = lookup_attribution(
                conn, revision, row["employee_id"], row["work_date"]
            )
            if cost_center is not None:
                promotions.append((row, cost_center, matched))

    # ---- 写事务：转正 + 隔离状态更新 + 标记处理完成，一起提交 ----
    promoted = 0
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for row, cost_center, matched in promotions:
            # 闸门：仅当隔离行仍为 PENDING 才转正；并发/重复触发时
            # 受影响行数为 0，直接跳过，不会重复转正。
            cur = conn.execute(
                """
                UPDATE quarantine_entries
                SET status = 'PROMOTED',
                    resolved_revision = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ? AND status = 'PENDING'
                """,
                (revision, row["id"]),
            )
            if cur.rowcount == 0:
                continue
            conn.execute(
                """
                INSERT INTO official_timesheets
                    (batch_no, timesheet_id, employee_id, work_date, hours,
                     cost_center, attribution_revision, matched_revision_id, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PROMOTED')
                """,
                (row["batch_no"], row["timesheet_id"], row["employee_id"], row["work_date"],
                 row["hours"], cost_center, revision, matched),
            )
            promoted += 1
        conn.execute(
            """
            UPDATE recheck_requests
            SET status = 'DONE',
                promoted_count = ?,
                processed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ? AND status = 'PENDING'
            """,
            (promoted, req["id"]),
        )
    return {"request_id": req["id"], "revision": revision, "promoted": promoted}


def run_recheck_pass() -> dict:
    """处理全部 PENDING 标记，返回汇总（供后台线程与测试直接调用）。"""
    totals = {"processed_requests": 0, "promoted": 0}
    while True:
        result = _process_one_request()
        if result is None:
            break
        totals["processed_requests"] += 1
        totals["promoted"] += result["promoted"]
        logger.info(
            "重查完成：标记 #%s 按修订 %s 转正 %s 行",
            result["request_id"], result["revision"], result["promoted"],
        )
    return totals


def _loop() -> None:
    # 启动即先扫一轮：崩溃/重启前遗留的 PENDING 标记在此被消费，不漏查。
    while not _stop_event.is_set():
        try:
            run_recheck_pass()
        except Exception:  # noqa: BLE001 - 后台线程不允许退出，下一轮重试
            logger.exception("重查轮次异常，将在下一轮重试")
        _wake_event.wait(timeout=_poll_interval_seconds())
        _wake_event.clear()


def start_worker() -> None:
    """启动后台重查线程（幂等）。可用 RECHECK_WORKER_ENABLED=0 关闭（测试用）。"""
    global _worker
    if os.environ.get("RECHECK_WORKER_ENABLED", "1") != "1":
        logger.info("后台重查线程已禁用（RECHECK_WORKER_ENABLED=0）")
        return
    if _worker is not None and _worker.is_alive():
        wake()
        return
    _stop_event.clear()
    _worker = threading.Thread(target=_loop, name="recheck-worker", daemon=True)
    _worker.start()


def stop_worker() -> None:
    """停止后台重查线程（应用关闭时调用）。"""
    global _worker
    _stop_event.set()
    _wake_event.set()
    if _worker is not None:
        _worker.join(timeout=5)
        _worker = None


def wake() -> None:
    """补录提交后唤醒后台线程，立即重查（轮询兜底之外的快速通道）。"""
    _wake_event.set()
