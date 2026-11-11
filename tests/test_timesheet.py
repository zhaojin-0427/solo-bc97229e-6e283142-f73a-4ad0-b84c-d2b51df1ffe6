"""工时入仓端到端测试：导入、幂等重传、补录触发自动转正、并发/重启安全。

运行：pytest tests/test_timesheet.py
"""
from __future__ import annotations

import importlib
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """后台重查线程开启、轮询加速的客户端（验证自动转正）。"""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("ATTRIBUTION_DB_PATH", str(db_file))
    monkeypatch.setenv("RECHECK_POLL_INTERVAL_MS", "100")

    import app.database as database
    import app.main as main

    importlib.reload(database)
    importlib.reload(main)
    database.init_db()
    with TestClient(main.app) as c:
        yield c


@pytest.fixture()
def client_no_worker(tmp_path, monkeypatch):
    """关闭后台重查线程的客户端（确定性验证标记落库与手动重查）。"""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("ATTRIBUTION_DB_PATH", str(db_file))
    monkeypatch.setenv("RECHECK_WORKER_ENABLED", "0")

    import app.database as database
    import app.main as main

    importlib.reload(database)
    importlib.reload(main)
    database.init_db()
    with TestClient(main.app) as c:
        yield c


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def submit_revision(client, employee_id="E001", cost_center="CC-A",
                    start="2026-01-01", end="2026-04-01", note=None):
    body = {
        "entries": [{
            "employee_id": employee_id,
            "cost_center": cost_center,
            "effective_from": start,
            "effective_to": end,
        }]
    }
    if note:
        body["note"] = note
    r = client.post("/api/revisions", json=body)
    assert r.status_code == 201, r.text
    return r.json()["revision_id"]


def import_batch(client, batch_no="B1", entries=None):
    entries = entries or [
        {"timesheet_id": "T1", "employee_id": "E001", "work_date": "2026-02-10", "hours": 8},
        {"timesheet_id": "T2", "employee_id": "E002", "work_date": "2026-02-10", "hours": 7.5},
    ]
    return client.post("/api/timesheet-batches", json={"batch_no": batch_no, "entries": entries})


# ---------- 导入：固定修订 + 正式/隔离分流 ----------

def test_import_splits_official_and_quarantine(client):
    submit_revision(client, "E001", "CC-A")  # 修订 1：仅覆盖 E001
    r = import_batch(client)
    assert r.status_code == 201, r.text
    receipt = r.json()
    assert receipt["batch_no"] == "B1"
    assert receipt["pinned_revision"] == 1
    assert receipt["row_count"] == 2
    assert receipt["official_count"] == 1
    assert receipt["quarantined_count"] == 1
    assert receipt["replayed"] is False

    detail = client.get("/api/timesheet-batches/B1").json()
    assert detail["import_official_count"] == 1
    assert detail["import_quarantined_count"] == 1
    off = detail["official_entries"]
    qua = detail["quarantine_entries"]
    assert len(off) == 1 and len(qua) == 1
    assert off[0]["timesheet_id"] == "T1"
    assert off[0]["cost_center"] == "CC-A"
    assert off[0]["attribution_revision"] == 1
    assert off[0]["matched_revision_id"] == 1
    assert off[0]["source"] == "IMPORTED"
    assert qua[0]["timesheet_id"] == "T2"
    assert qua[0]["status"] == "PENDING"
    assert qua[0]["first_seen_revision"] == 1
    assert qua[0]["resolved_revision"] is None


def test_import_without_any_revision_pins_zero(client):
    r = import_batch(client)
    assert r.status_code == 201
    receipt = r.json()
    assert receipt["pinned_revision"] == 0
    assert receipt["official_count"] == 0
    assert receipt["quarantined_count"] == 2


def test_import_pins_current_revision(client):
    submit_revision(client, "E001", "CC-A")          # 修订 1
    submit_revision(client, "E002", "CC-B")          # 修订 2
    r = import_batch(client)
    assert r.json()["pinned_revision"] == 2
    assert r.json()["official_count"] == 2


# ---------- 正式行保留所用修订，后续补录不改写 ----------

def test_official_rows_keep_revision_after_backfill(client):
    submit_revision(client, "E001", "CC-OLD")        # 修订 1
    r = import_batch(client, entries=[
        {"timesheet_id": "T1", "employee_id": "E001", "work_date": "2026-02-10", "hours": 8},
    ])
    assert r.json()["official_count"] == 1

    # 修订 2：同一员工同一日期改派新成本中心
    submit_revision(client, "E001", "CC-NEW")

    # 已入仓的正式行不被改写
    rows = client.get("/api/official-timesheets", params={"batch_no": "B1"}).json()
    assert len(rows) == 1
    assert rows[0]["cost_center"] == "CC-OLD"
    assert rows[0]["attribution_revision"] == 1

    # 之后导入的新批次固定新修订，按新口径归属
    r2 = import_batch(client, batch_no="B2", entries=[
        {"timesheet_id": "T9", "employee_id": "E001", "work_date": "2026-02-10", "hours": 8},
    ])
    assert r2.json()["pinned_revision"] == 2
    rows2 = client.get("/api/official-timesheets", params={"batch_no": "B2"}).json()
    assert rows2[0]["cost_center"] == "CC-NEW"
    assert rows2[0]["attribution_revision"] == 2


# ---------- 幂等：同批同内容重传返回原批，异内容拒绝 ----------

def test_replay_same_content_returns_original_batch(client):
    submit_revision(client, "E001", "CC-A")
    first = import_batch(client)
    assert first.status_code == 201

    replay = import_batch(client)  # 同批同内容重传
    assert replay.status_code == 200
    body = replay.json()
    assert body["replayed"] is True
    # 回执关键字段与首次完全一致
    for key in ("batch_no", "content_hash", "pinned_revision", "row_count",
                "official_count", "quarantined_count", "created_at"):
        assert body[key] == first.json()[key]

    # 不产生重复数据
    detail = client.get("/api/timesheet-batches/B1").json()
    assert len(detail["official_entries"]) == 1
    assert len(detail["quarantine_entries"]) == 1
    assert len(client.get("/api/timesheet-batches").json()) == 1


def test_replay_reordered_same_content_is_same_batch(client):
    submit_revision(client, "E001", "CC-A")
    entries = [
        {"timesheet_id": "T1", "employee_id": "E001", "work_date": "2026-02-10", "hours": 8},
        {"timesheet_id": "T2", "employee_id": "E002", "work_date": "2026-02-10", "hours": 7.5},
    ]
    first = import_batch(client, entries=entries)
    replay = import_batch(client, entries=list(reversed(entries)))
    assert replay.status_code == 200
    assert replay.json()["content_hash"] == first.json()["content_hash"]


def test_replay_different_content_rejected(client):
    submit_revision(client, "E001", "CC-A")
    assert import_batch(client).status_code == 201

    changed = import_batch(client, entries=[
        {"timesheet_id": "T1", "employee_id": "E001", "work_date": "2026-02-10", "hours": 8},
        {"timesheet_id": "T2", "employee_id": "E002", "work_date": "2026-02-10", "hours": 6},  # 小时数变了
    ])
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "BATCH_CONTENT_MISMATCH"

    # 原批数据不受影响
    detail = client.get("/api/timesheet-batches/B1").json()
    assert detail["row_count"] == 2
    qua = detail["quarantine_entries"]
    assert qua[0]["hours"] == 7.5


def test_duplicate_timesheet_id_within_batch_rejected(client):
    r = import_batch(client, entries=[
        {"timesheet_id": "T1", "employee_id": "E001", "work_date": "2026-02-10", "hours": 8},
        {"timesheet_id": "T1", "employee_id": "E002", "work_date": "2026-02-11", "hours": 4},
    ])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "DUPLICATE_TIMESHEET_ID"
    assert client.get("/api/timesheet-batches").json() == []


def test_invalid_hours_rejected(client):
    r = import_batch(client, entries=[
        {"timesheet_id": "T1", "employee_id": "E001", "work_date": "2026-02-10", "hours": 0},
    ])
    assert r.status_code == 422
    r2 = client.post("/api/timesheet-batches", json={"batch_no": "B9", "entries": []})
    assert r2.status_code == 422


def test_batch_detail_404(client):
    r = client.get("/api/timesheet-batches/NOPE")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "BATCH_NOT_FOUND"


# ---------- 补录触发自动转正（后台线程） ----------

def test_backfill_triggers_auto_promotion(client):
    # 修订 1 只覆盖 E001；导入后 E002 的行进隔离区
    submit_revision(client, "E001", "CC-A")
    import_batch(client)
    detail = client.get("/api/timesheet-batches/B1").json()
    assert detail["quarantine_entries"][0]["status"] == "PENDING"

    # 人事补录 E002 归属 -> 后台自动重查 -> 隔离行转正
    submit_revision(client, "E002", "CC-B")

    def promoted():
        d = client.get("/api/timesheet-batches/B1").json()
        return d["quarantine_entries"][0]["status"] == "PROMOTED"

    assert wait_until(promoted), "隔离行未在补录后自动转正"

    detail = client.get("/api/timesheet-batches/B1").json()
    qua = detail["quarantine_entries"][0]
    assert qua["resolved_revision"] == 2
    off = {o["timesheet_id"]: o for o in detail["official_entries"]}
    assert set(off) == {"T1", "T2"}
    assert off["T2"]["cost_center"] == "CC-B"
    assert off["T2"]["source"] == "PROMOTED"
    assert off["T2"]["attribution_revision"] == 2
    assert off["T2"]["matched_revision_id"] == 2

    # 下游正式工时可见两行
    rows = client.get("/api/official-timesheets", params={"batch_no": "B1"}).json()
    assert len(rows) == 2


def test_recheck_marker_persisted_and_processed(client):
    submit_revision(client, "E001", "CC-A")
    import_batch(client)
    submit_revision(client, "E002", "CC-B")

    def done():
        reqs = client.get("/api/recheck-requests").json()
        return len(reqs) == 2 and all(q["status"] == "DONE" for q in reqs)

    assert wait_until(done)
    reqs = client.get("/api/recheck-requests").json()
    assert reqs[0]["revision_id"] == 1
    assert reqs[1]["revision_id"] == 2
    assert reqs[1]["promoted_count"] == 1
    assert reqs[0]["promoted_count"] == 0


# ---------- 关闭后台线程：标记落库 + 手动重查（模拟重启续查） ----------

def test_marker_persisted_with_backfill_then_recheck_promotes(client_no_worker):
    from app import recheck

    submit_revision(client_no_worker, "E001", "CC-A")
    import_batch(client_no_worker)
    submit_revision(client_no_worker, "E002", "CC-B")

    # 补录与待重查标记共同落库：线程未运行时标记保持 PENDING（重启后仍在）
    reqs = client_no_worker.get("/api/recheck-requests").json()
    assert [q["status"] for q in reqs] == ["PENDING", "PENDING"]
    assert reqs[1]["revision_id"] == 2
    detail = client_no_worker.get("/api/timesheet-batches/B1").json()
    assert detail["quarantine_entries"][0]["status"] == "PENDING"

    # 模拟服务重启后后台线程启动扫尾：重查计算在归属写事务之外进行
    totals = recheck.run_recheck_pass()
    assert totals["processed_requests"] == 2
    assert totals["promoted"] == 1

    # 转正与隔离状态更新一起提交：两侧同时可见
    detail = client_no_worker.get("/api/timesheet-batches/B1").json()
    qua = detail["quarantine_entries"][0]
    assert qua["status"] == "PROMOTED"
    assert qua["resolved_revision"] == 2
    off = detail["official_entries"]
    assert len(off) == 2
    assert {o["source"] for o in off} == {"IMPORTED", "PROMOTED"}

    reqs = client_no_worker.get("/api/recheck-requests").json()
    assert all(q["status"] == "DONE" for q in reqs)


def test_recheck_is_idempotent_no_double_promotion(client_no_worker):
    from app import recheck

    submit_revision(client_no_worker, "E001", "CC-A")
    import_batch(client_no_worker)
    submit_revision(client_no_worker, "E002", "CC-B")

    first = recheck.run_recheck_pass()
    assert first["promoted"] == 1
    # 再次重查（含重复触发/并发兜底）：不再转正，正式表无重复行
    second = recheck.run_recheck_pass()
    assert second["promoted"] == 0
    rows = client_no_worker.get("/api/official-timesheets").json()
    assert len(rows) == 2
    assert len({(r["batch_no"], r["timesheet_id"]) for r in rows}) == 2


def test_still_unattributed_rows_stay_quarantined(client_no_worker):
    from app import recheck

    submit_revision(client_no_worker, "E001", "CC-A")
    import_batch(client_no_worker)  # E002 无归属 -> 隔离
    # 补录了一个无关员工，E002 仍未归属
    submit_revision(client_no_worker, "E003", "CC-C")

    recheck.run_recheck_pass()
    detail = client_no_worker.get("/api/timesheet-batches/B1").json()
    assert detail["quarantine_entries"][0]["status"] == "PENDING"
    assert len(detail["official_entries"]) == 1


def test_quarantine_not_visible_to_downstream(client_no_worker):
    submit_revision(client_no_worker, "E001", "CC-A")
    import_batch(client_no_worker)
    rows = client_no_worker.get("/api/official-timesheets").json()
    assert len(rows) == 1
    assert rows[0]["timesheet_id"] == "T1"
    # 过滤参数
    assert client_no_worker.get(
        "/api/official-timesheets", params={"employee_id": "E002"}).json() == []
    ranged = client_no_worker.get(
        "/api/official-timesheets",
        params={"work_date_from": "2026-02-01", "work_date_to": "2026-02-28"}).json()
    assert len(ranged) == 1
    filtered = client_no_worker.get(
        "/api/official-timesheets",
        params={"work_date_from": "2026-03-01"}).json()
    assert filtered == []
