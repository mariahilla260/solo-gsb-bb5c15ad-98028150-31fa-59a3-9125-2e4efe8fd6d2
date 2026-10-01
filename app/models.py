"""Pydantic 请求/响应模型。时间统一按带时区时间戳处理。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


def as_utc(value: datetime) -> datetime:
    """无时区时间戳按 UTC 处理；有时区的统一转换到 UTC。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class CalibrationCreate(BaseModel):
    instrument: str = Field(..., min_length=1, description="仪器标识")
    valid_from: datetime
    valid_to: datetime | None = Field(None, description="为空表示开放区间（持续有效）")
    passed: bool

    _normalize_from = field_validator("valid_from")(lambda cls, v: as_utc(v))
    _normalize_to = field_validator("valid_to")(lambda cls, v: as_utc(v) if v else v)


class CalibrationCorrect(BaseModel):
    corrects_id: int = Field(..., description="被更正的任意历史版本 ID（通常传原记录/最新版 ID）")
    version: int = Field(..., ge=1, description="必须为该校准链当前最新版本号 + 1（连续版本号）")
    valid_from: datetime
    valid_to: datetime | None = None
    passed: bool

    _normalize_from = field_validator("valid_from")(lambda cls, v: as_utc(v))
    _normalize_to = field_validator("valid_to")(lambda cls, v: as_utc(v) if v else v)


class ReportSubmit(BaseModel):
    instrument: str = Field(..., min_length=1)
    tested_at: datetime = Field(..., description="检测时间，据此判断校准是否覆盖")
    reference: str | None = Field(None, description="可选业务报告编号；提供则需全局唯一")

    _normalize_tested = field_validator("tested_at")(lambda cls, v: as_utc(v))


class SupersedeSubmit(BaseModel):
    """复测接替：对“尚未被接替”的已发布报告发起复测，放行后原子接替。"""

    instrument: str = Field(..., min_length=1,
                            description="复测仪器（放行判定以该仪器当前状态为准，可与原报告不同）")
    tested_at: datetime = Field(..., description="复测检测时间")
    reference: str | None = Field(
        None, description="新报告编号（可选，全局唯一；旧报告编号不得转移或复用）")
    reason: str = Field(..., min_length=1, description="接替原因（非空，不允许纯空白）")

    _normalize_tested = field_validator("tested_at")(lambda cls, v: as_utc(v))

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("接替原因不能为空")
        return value


class DisableCreate(BaseModel):
    event_no: str = Field(..., min_length=1, description="唯一禁用事件号（业务侧生成，撤销后也不可复用）")
    instrument: str = Field(..., min_length=1, description="仪器标识")
    disabled_from: datetime = Field(..., description="禁用起点（含）")
    disabled_to: datetime | None = Field(None, description="禁用终点（不含）；为空表示持续禁用")
    reason: str = Field(..., min_length=1, description="禁用原因（如故障说明）")

    _normalize_from = field_validator("disabled_from")(lambda cls, v: as_utc(v))
    _normalize_to = field_validator("disabled_to")(lambda cls, v: as_utc(v) if v else v)


class DisableRevoke(BaseModel):
    reason: str | None = Field(None, description="撤销原因（可选）")


class ReleaseWindowQuery(BaseModel):
    """排班规划查询：非空且无重复的仪器标识 + 带时区的起止时间（起点早于终点）。"""

    instruments: list[str] = Field(
        ..., min_length=1, description="仪器标识列表（非空；服务端拒绝空标识与重复项）"
    )
    start: datetime = Field(..., description="查询范围起点（含），必须早于 end")
    end: datetime = Field(..., description="查询范围终点（不含），开放结论截于此")

    _normalize_start = field_validator("start")(lambda cls, v: as_utc(v))
    _normalize_end = field_validator("end")(lambda cls, v: as_utc(v))

    @field_validator("instruments")
    @classmethod
    def _validate_instruments(cls, value: list[str]) -> list[str]:
        if any(not item or not item.strip() for item in value):
            raise ValueError("仪器标识不能为空")
        if len(set(value)) != len(value):
            raise ValueError("仪器标识不能重复")
        return value

    @model_validator(mode="after")
    def _validate_order(self):
        if self.end <= self.start:
            raise ValueError("查询范围必须满足 start < end（左闭右开，非空时段）")
        return self


def serialize_supersession(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "old_report_id": row["old_report_id"],
        "new_report_id": row["new_report_id"],
        "reason": row["reason"],
        "created_at": row["created_at"].isoformat(),
    }


def serialize_disable(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "event_no": row["event_no"],
        "instrument": row["instrument"],
        "disabled_from": row["disabled_from"].isoformat(),
        "disabled_to": row["disabled_to"].isoformat() if row["disabled_to"] else None,
        "reason": row["reason"],
        "revoked": row["revoked"],
        "revoked_at": row["revoked_at"].isoformat() if row["revoked_at"] else None,
        "revoke_reason": row["revoke_reason"],
        "created_at": row["created_at"].isoformat(),
    }


def serialize_calibration(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "chain_id": row["chain_id"],
        "version": row["version"],
        "instrument": row["instrument"],
        "valid_from": row["valid_from"].isoformat(),
        "valid_to": row["valid_to"].isoformat() if row["valid_to"] else None,
        "passed": row["passed"],
        "is_current": row["is_current"],
        "corrects_id": row["corrects_id"],
        "created_at": row["created_at"].isoformat(),
    }
