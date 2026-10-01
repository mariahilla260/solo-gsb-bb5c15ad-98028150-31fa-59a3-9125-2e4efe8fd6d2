"""检测报告接口。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import db
from ..errors import not_found
from ..models import ReportSubmit, SupersedeSubmit, serialize_supersession
from ..repository import (
    chain_tip_id,
    compute_drift,
    publish_report,
    report_lineage,
    supersede_report,
)
from ..transactions import run_tx

router = APIRouter(prefix="/reports", tags=["reports"])


def _serialize_report(row: dict) -> dict:
    return {
        "id": row["id"],
        "reference": row["reference"],
        "instrument": row["instrument"],
        "tested_at": row["tested_at"].isoformat(),
        "released": row["released"],
        "basis_calibration_id": row["basis_calibration_id"],
        "snapshot": row["snapshot"],
        "created_at": row["created_at"].isoformat(),
    }


@router.post("", status_code=201)
def submit_report(payload: ReportSubmit):
    """提交检测报告并尝试放行。

    判定顺序：先检查仪器禁用区间（命中未撤销区间即 422 INSTRUMENT_DISABLED
    并返回禁用依据，即使校准通过）；再检查校准——检测时间被该仪器一条“结论
    为通过”的当前校准版本覆盖才放行（左闭右开，仅最新版参与判定）。

    - 放行：201，返回所据校准版本及不可变判定快照
    - 命中禁用 / 缺失校准 / 区间外 / 校准未通过：422，detail 内含阻断原因码与说明
    """
    return run_tx(
        lambda conn: publish_report(conn, payload),
        advisory_lock=db.instrument_lock_key(payload.instrument),
    )


# 注意：静态路由需在 /{report_id} 之前声明
@router.get("/drift/recompute")
def report_drift(instrument: str | None = None):
    items = run_tx(lambda conn: compute_drift(conn, instrument), readonly=True)
    return {"items": items, "count": len(items)}


@router.post("/{report_id}/supersede", status_code=201)
def supersede(report_id: int, payload: SupersedeSubmit):
    """复测并接替一份已发布报告（当前有效报告切换为新版）。

    - 仅“尚未被接替”的已发布报告可被接替；复测放行规则与首次提交完全一致
      （禁用优先于校准，检测时间须被一条“通过”的当前校准版本覆盖）
    - 成功：201，新报告与接替关系原子落库；旧报告与其发布快照永久保留
    - 旧报告不存在 -> 404；已被接替（含并发接替败方）-> 409
      REPORT_ALREADY_SUPERSEDED，detail 内附现行报告标识 current_report_id
    - 复测被阻断（命中禁用/无校准/区间外/未通过）-> 422，新报告编号冲突
      （含沿用旧编号）-> 409 REPORT_REFERENCE_EXISTS；两者均整体回滚，
      旧报告仍有效，不留下接替记录
    """
    try:
        return run_tx(
            lambda conn: supersede_report(conn, report_id, payload),
            advisory_lock=db.instrument_lock_key(payload.instrument),
        )
    except HTTPException as exc:
        detail = exc.detail
        if (exc.status_code == 409 and isinstance(detail, dict)
                and detail.get("code") == "REPORT_ALREADY_SUPERSEDED"):
            # 败方返回现行报告标识：以全新只读事务取链尾，
            # 保证反映的是已提交状态（冲突事务自身的快照可能过旧）
            detail["current_report_id"] = run_tx(
                lambda conn: chain_tip_id(conn, report_id), readonly=True)
        raise


@router.get("/{report_id}/lineage")
def get_lineage(report_id: int):
    """按链内任一报告 ID 查询完整沿革。

    返回该链全部报告（按接替顺序，含各自发布快照）、每次接替的原因与时间，
    以及当前有效报告（链尾）。只读，快照永不改写。
    """
    lineage = run_tx(lambda conn: report_lineage(conn, report_id), readonly=True)
    if lineage is None:
        raise not_found("报告", report_id)
    return {
        "report_id": report_id,
        "current_report_id": lineage["current_report_id"],
        "reports": [_serialize_report(r) for r in lineage["reports"]],
        "supersessions": [serialize_supersession(s) for s in lineage["supersessions"]],
        "count": len(lineage["reports"]),
    }


@router.get("")
def list_reports(instrument: str | None = None):
    assert db.pool is not None
    with db.pool.connection() as conn:
        if instrument:
            rows = conn.execute(
                "SELECT * FROM reports WHERE instrument = %s ORDER BY id",
                (instrument,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM reports ORDER BY id").fetchall()
    return {"items": [_serialize_report(r) for r in rows]}


@router.get("/{report_id}")
def get_report(report_id: int):
    assert db.pool is not None
    with db.pool.connection() as conn:
        row = conn.execute(
            "SELECT * FROM reports WHERE id = %s", (report_id,)
        ).fetchone()
    if row is None:
        raise not_found("报告", report_id)
    return {"report": _serialize_report(row)}
