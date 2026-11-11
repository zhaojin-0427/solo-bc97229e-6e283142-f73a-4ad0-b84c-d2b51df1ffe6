"""FastAPI 入口：工时入仓 + 归属修订 API 路由与应用装配。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from . import recheck
from .database import init_db
from .errors import AppError
from .schemas import (
    AttributionQueryRequest,
    AttributionResult,
    OfficialTimesheetOut,
    RecheckRequestOut,
    RevisionOut,
    RevisionSubmitRequest,
    SegmentsQueryRequest,
    TimesheetBatchDetail,
    TimesheetBatchImportRequest,
    TimesheetBatchReceipt,
    TimesheetBatchSummary,
)
from .service import attribute_hours, list_revisions, list_segments, submit_revision
from .timesheet import (
    get_batch_detail,
    import_batch,
    list_batches,
    list_official_timesheets,
    list_recheck_requests,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO)
    # 启动时确保表结构存在（幂等）；随后启动后台重查线程，
    # 启动即扫描遗留 PENDING 标记，服务重启不会漏查。
    init_db()
    recheck.start_worker()
    yield
    recheck.stop_worker()


app = FastAPI(
    title="工时入仓与归属修订 API",
    description=(
        "财务数仓工时入仓服务：持续接收工时系统明细，少数员工缺失归属不阻塞整批入仓。\n\n"
        "**演示动线（交互式文档中依次 Try it out）：**\n"
        "1. `POST /api/revisions` 提交人事归属（补录）；\n"
        "2. `POST /api/timesheet-batches` 导入工时批次：已归属行进正式工时表，"
        "未归属行进隔离区，导入时固定当前归属修订号；\n"
        "3. `GET /api/timesheet-batches/{batch_no}` 查看批内正式及隔离明细；\n"
        "4. 同批同内容重传返回原批回执（HTTP 200），异内容拒绝（HTTP 409）；\n"
        "5. 再次 `POST /api/revisions` 补录缺失员工的归属 → 后台自动重查隔离行，"
        "归属补齐即转正（数秒内可见）；\n"
        "6. `GET /api/official-timesheets` 下游只读取正式工时。\n\n"
        "归属口径：每批补录返回递增修订号，按修订号查询只可见该修订号及之前的数据，"
        "后续补录不改变旧修订查询结果；正式工时行保留归属所用修订号，后续补录不改写。"
    ),
    version="2.0.0",
    lifespan=lifespan,
)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                **({"details": exc.details} if exc.details else {}),
            }
        },
    )


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["运维"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


# ======================= 工时入仓 =======================


@app.post(
    "/api/timesheet-batches",
    response_model=TimesheetBatchReceipt,
    status_code=201,
    tags=["工时入仓"],
    summary="导入一批工时明细（幂等）",
    description=(
        "导入时固定当前归属修订号：已归属行进入正式工时表（保留所用修订号，后续补录不改写），"
        "未归属行留在隔离区等待补录后的自动重查。\n\n"
        "幂等规则：同批号同内容重传返回原批回执（HTTP 200，`replayed=true`）；"
        "同批号异内容拒绝（HTTP 409）。批内 `timesheet_id` 必须唯一。"
    ),
)
def create_timesheet_batch(
    req: TimesheetBatchImportRequest, response: Response
) -> TimesheetBatchReceipt:
    receipt, created = import_batch(req)
    if not created:
        response.status_code = 200
    return TimesheetBatchReceipt(**receipt)


@app.get(
    "/api/timesheet-batches",
    response_model=list[TimesheetBatchSummary],
    tags=["工时入仓"],
    summary="列出全部工时批次",
)
def get_timesheet_batches() -> list[TimesheetBatchSummary]:
    return [TimesheetBatchSummary(**row) for row in list_batches()]


@app.get(
    "/api/timesheet-batches/{batch_no}",
    response_model=TimesheetBatchDetail,
    tags=["工时入仓"],
    summary="查询批次内的正式及隔离明细",
    description=(
        "返回批次概要（含导入时快照计数）与当前明细："
        "`official_entries` 为正式工时（含后续转正行），"
        "`quarantine_entries` 为隔离明细（`status=PROMOTED` 表示已转正）。"
    ),
)
def get_timesheet_batch(batch_no: str) -> TimesheetBatchDetail:
    return TimesheetBatchDetail(**get_batch_detail(batch_no))


@app.get(
    "/api/official-timesheets",
    response_model=list[OfficialTimesheetOut],
    tags=["工时入仓"],
    summary="下游读取正式工时（隔离区不可见）",
    description="下游数仓唯一读取口径：只返回正式工时表内容，可按批次号、员工、工作日期区间过滤。",
)
def get_official_timesheets(
    batch_no: str | None = Query(default=None, description="按批次号过滤"),
    employee_id: str | None = Query(default=None, description="按员工过滤"),
    work_date_from: str | None = Query(default=None, description="工作日期起（含），ISO yyyy-mm-dd"),
    work_date_to: str | None = Query(default=None, description="工作日期止（含），ISO yyyy-mm-dd"),
) -> list[OfficialTimesheetOut]:
    rows = list_official_timesheets(
        batch_no=batch_no,
        employee_id=employee_id,
        work_date_from=work_date_from,
        work_date_to=work_date_to,
    )
    return [OfficialTimesheetOut(**row) for row in rows]


@app.get(
    "/api/recheck-requests",
    response_model=list[RecheckRequestOut],
    tags=["工时入仓"],
    summary="查看待重查标记（补录触发，后台自动处理）",
    description=(
        "每次人事补录都会在同一事务落一条 PENDING 标记；后台线程在归属写事务之外"
        "重查隔离行并转正，完成后标记 DONE。服务重启后 PENDING 标记仍会续查。"
    ),
)
def get_recheck_requests() -> list[RecheckRequestOut]:
    return [RecheckRequestOut(**row) for row in list_recheck_requests()]


# ======================= 归属修订 =======================


@app.post(
    "/api/revisions",
    response_model=RevisionOut,
    status_code=201,
    tags=["归属修订"],
    summary="提交一批员工-成本中心归属区间（人事补录）",
    description=(
        "整批校验通过后写入，返回递增修订号；同一事务内落待重查标记，"
        "提交成功后后台自动重查隔离区，归属补齐的隔离行转正。\n\n"
        "拒绝条件（整批回滚，数据与修订号均不变）：\n"
        "1. 起日不早于止日（`effective_from >= effective_to`）；\n"
        "2. 批内同一员工的区间相互重叠（首尾相接不算重叠）。"
    ),
)
def create_revision(req: RevisionSubmitRequest) -> RevisionOut:
    result = submit_revision(req)
    # 唤醒后台重查线程（轮询兜底之外的快速通道），重查计算在归属写事务之外进行。
    recheck.wake()
    return RevisionOut(**result)


@app.get("/api/revisions", tags=["归属修订"], summary="列出全部修订号")
def get_revisions() -> list[RevisionOut]:
    return [RevisionOut(**row) for row in list_revisions()]


@app.post(
    "/api/attribution/query",
    response_model=list[AttributionResult],
    tags=["归属查询"],
    summary="按指定修订号的口径逐条查询工时归属",
    description=(
        "对每条工时记录返回成本中心或未归属。只可见 `revision_id` 及之前的补录；"
        "同日期点存在多个历史区间时取修订号最大者。`revision_id=0` 表示空口径。"
    ),
)
def query_attribution(req: AttributionQueryRequest) -> list[AttributionResult]:
    return attribute_hours(req)


@app.post(
    "/api/revisions/{revision_id}/segments",
    tags=["归属修订"],
    summary="查看某个修订号下提交的原始区间",
)
def get_revision_segments(revision_id: int, req: SegmentsQueryRequest | None = None) -> list[dict]:
    employee_id = req.employee_id if req else None
    return list_segments(revision_id, employee_id)
