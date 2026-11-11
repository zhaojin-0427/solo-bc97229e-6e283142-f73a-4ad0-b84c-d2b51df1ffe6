"""Pydantic 请求/响应模型。

日期统一使用 ISO 8601（yyyy-mm-dd），SQLite 内以 TEXT 存储；
ISO 日期字符串的字典序与时间序一致，可直接做半开区间比较。
"""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, field_validator


class SegmentIn(BaseModel):
    """人事导出的一条员工-成本中心生效区间（左闭右开）。"""

    employee_id: str = Field(..., min_length=1, description="员工编号", examples=["E001"])
    cost_center: str = Field(..., min_length=1, description="成本中心编号", examples=["CC-SH-01"])
    effective_from: date = Field(..., description="生效起日（含）", examples=["2026-01-01"])
    effective_to: date = Field(..., description="生效止日（不含）", examples=["2026-04-01"])

    @field_validator("employee_id", "cost_center")
    @classmethod
    def _strip_and_require(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v


class RevisionSubmitRequest(BaseModel):
    """一批补录/归属区间。整批要么全部成功，要么整批拒绝。"""

    entries: list[SegmentIn] = Field(..., min_length=1, description="本批归属区间（至少一条）")
    note: str | None = Field(default=None, description="备注，如人事调岗补录批次号", examples=["HR 调岗补录 2026-11"])


class RevisionOut(BaseModel):
    revision_id: int
    entry_count: int
    note: str | None
    created_at: str


class WorkHourRecord(BaseModel):
    """工时系统导出的一条工时记录。"""

    employee_id: str = Field(..., min_length=1, examples=["E001"])
    work_date: date = Field(..., description="工作日期", examples=["2026-02-15"])
    hours: float = Field(..., description="小时数（原样回显，不参与归属判断）", examples=[8.0])

    @field_validator("employee_id")
    @classmethod
    def _strip_employee(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v


class AttributionQueryRequest(BaseModel):
    """按指定修订号的口径查询工时归属。"""

    revision_id: int = Field(..., ge=0, description="月结口径修订号；0 表示尚无任何补录的空口径")
    records: list[WorkHourRecord] = Field(..., min_length=1, description="待归属的工时记录")


class AttributionResult(BaseModel):
    employee_id: str
    work_date: date
    hours: float
    revision_id: int
    cost_center: str | None = Field(..., description="成本中心；None 表示未归属")
    attributed: bool
    matched_revision_id: int | None = Field(..., description="命中区间所属的修订号，便于审计")


class SegmentsQueryRequest(BaseModel):
    employee_id: str | None = Field(default=None, description="可选，按员工过滤")


# ======================= 工时入仓 =======================


class TimesheetEntryIn(BaseModel):
    """工时系统导出的一条工时明细。timesheet_id 在批内唯一。"""

    timesheet_id: str = Field(..., min_length=1, description="批内唯一的工时ID", examples=["TS-0001"])
    employee_id: str = Field(..., min_length=1, description="员工编号", examples=["E001"])
    work_date: date = Field(..., description="工作日期", examples=["2026-02-15"])
    hours: float = Field(..., gt=0, allow_inf_nan=False, description="小时数（必须为正）", examples=[8.0])

    @field_validator("timesheet_id", "employee_id")
    @classmethod
    def _strip_and_require(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v


class TimesheetBatchImportRequest(BaseModel):
    """一批工时明细。批次号为幂等键：同批同内容重传返回原批，异内容拒绝。"""

    batch_no: str = Field(..., min_length=1, description="批次号（客户端幂等键）", examples=["TS-2026-11-0001"])
    entries: list[TimesheetEntryIn] = Field(..., min_length=1, description="批内工时明细（至少一条）")

    @field_validator("batch_no")
    @classmethod
    def _strip_batch_no(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v


class TimesheetBatchReceipt(BaseModel):
    """导入回执。重传同内容批次时原样返回（replayed=true，HTTP 200）。"""

    batch_no: str
    content_hash: str = Field(..., description="规范化内容哈希（SHA-256）")
    pinned_revision: int = Field(..., description="导入时固定的归属修订号")
    row_count: int
    official_count: int = Field(..., description="导入时落入正式工时表的行数")
    quarantined_count: int = Field(..., description="导入时落入隔离区的行数")
    replayed: bool = Field(..., description="true 表示本次为同内容重传，未重复入仓")
    created_at: str


class OfficialTimesheetOut(BaseModel):
    """正式工时表中的一行（下游唯一读取口径）。"""

    batch_no: str
    timesheet_id: str
    employee_id: str
    work_date: date
    hours: float
    cost_center: str
    attribution_revision: int = Field(..., description="归属所用修订号，固定不改写")
    matched_revision_id: int = Field(..., description="命中区间所属修订号（审计）")
    source: str = Field(..., description="IMPORTED=导入即归属；PROMOTED=隔离转正")
    created_at: str


class QuarantineEntryOut(BaseModel):
    """隔离区中的一行。"""

    batch_no: str
    timesheet_id: str
    employee_id: str
    work_date: date
    hours: float
    status: str = Field(..., description="PENDING=待重查；PROMOTED=已转正")
    first_seen_revision: int = Field(..., description="入隔离区时固定的修订号")
    resolved_revision: int | None = Field(..., description="转正时所用修订号；未转正为 None")
    created_at: str
    updated_at: str


class TimesheetBatchSummary(BaseModel):
    """批次概要（导入时刻快照计数）。"""

    batch_no: str
    pinned_revision: int
    row_count: int
    official_count: int
    quarantined_count: int
    created_at: str


class TimesheetBatchDetail(BaseModel):
    """批次详情：概要 + 当前正式明细 + 当前隔离明细（含已转正标记）。"""

    batch_no: str
    pinned_revision: int
    row_count: int
    import_official_count: int = Field(..., description="导入时落入正式表的行数（快照）")
    import_quarantined_count: int = Field(..., description="导入时落入隔离区的行数（快照）")
    created_at: str
    official_entries: list[OfficialTimesheetOut] = Field(..., description="当前正式明细（含后续转正）")
    quarantine_entries: list[QuarantineEntryOut] = Field(..., description="当前隔离明细（含已转正行）")


class RecheckRequestOut(BaseModel):
    """一条待重查标记（与人事补录同事务落库）。"""

    id: int
    revision_id: int = Field(..., description="触发本次重查的补录修订号")
    status: str = Field(..., description="PENDING=待重查；DONE=已处理")
    promoted_count: int | None = Field(..., description="本次重查转正的行数；未处理为 None")
    created_at: str
    processed_at: str | None
