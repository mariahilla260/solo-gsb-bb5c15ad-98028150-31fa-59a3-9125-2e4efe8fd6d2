"""仪器禁用区间接口。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from .. import db
from ..errors import not_found
from ..models import DisableCreate, DisableRevoke, serialize_disable
from ..repository import (
    affected_reports,
    get_disable,
    list_disables,
    register_disable,
    revoke_disable,
)
from ..transactions import run_tx

router = APIRouter(prefix="/disables", tags=["disables"])


@router.post("", status_code=201)
def create(payload: DisableCreate):
    """登记仪器禁用区间（唯一事件号、左闭右开，终点可为空）。

    - 无效区间（终点 <= 起点）-> 422 INVALID_INTERVAL
    - 事件号重复（含已撤销）-> 409 DISABLE_EVENT_EXISTS
    - 与同仪器未撤销区间重叠 -> 409 DISABLE_INTERVAL_OVERLAP（边界相接允许）
    """
    row = run_tx(
        lambda conn: register_disable(conn, payload),
        advisory_lock=db.instrument_lock_key(payload.instrument),
    )
    return {"disable_event": serialize_disable(row)}


# 注意：静态路径需在 /{event_no} 之前声明
@router.get("/affected-reports")
def get_affected_reports(instrument: str | None = Query(None)):
    """按当前禁用状态查询受影响的已发布报告及对应禁用依据。

    追溯登记 / 撤销禁用后结果随之变化；原放行记录与快照不改写。
    """
    items = run_tx(lambda conn: affected_reports(conn, instrument), readonly=True)
    return {"items": items, "count": len(items)}


@router.get("")
def list_events(
    instrument: str | None = Query(None),
    include_revoked: bool = Query(True, description="是否包含已撤销事件（默认包含，供追溯）"),
):
    """列出禁用事件；撤销记录默认保留可见。"""
    rows = run_tx(lambda conn: list_disables(conn, instrument, include_revoked),
                  readonly=True)
    return {"items": [serialize_disable(r) for r in rows]}


@router.post("/{event_no}/revoke", status_code=200)
def revoke(event_no: str, payload: DisableRevoke | None = None):
    """按事件号撤销禁用。

    - 未知事件 -> 404 NOT_FOUND
    - 已撤销事件 -> 409 DISABLE_ALREADY_REVOKED
    - 撤销后记录保留，受影响报告查询随即不再包含命中该区间的报告
    """
    payload = payload or DisableRevoke()
    # 先确定事件所属仪器，以便与该仪器的“登记/发布”共用同一把顺序锁
    probe = run_tx(lambda conn: get_disable(conn, event_no), readonly=True)
    if probe is None:
        raise not_found("禁用事件", event_no)
    row = run_tx(
        lambda conn: revoke_disable(conn, event_no, payload),
        advisory_lock=db.instrument_lock_key(probe["instrument"]),
    )
    return {"disable_event": serialize_disable(row)}


@router.get("/{event_no}")
def get_event(event_no: str):
    """按事件号查看单条禁用事件（含已撤销，供追溯）。"""
    row = run_tx(lambda conn: get_disable(conn, event_no), readonly=True)
    if row is None:
        raise not_found("禁用事件", event_no)
    return {"disable_event": serialize_disable(row)}
