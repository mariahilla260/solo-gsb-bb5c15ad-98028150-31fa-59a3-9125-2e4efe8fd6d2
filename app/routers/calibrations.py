"""校准记录接口。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from .. import db
from ..models import CalibrationCorrect, CalibrationCreate, serialize_calibration
from ..repository import compute_drift, correct_calibration, create_calibration
from ..transactions import run_tx

router = APIRouter(prefix="/calibrations", tags=["calibrations"])


@router.post("", status_code=201)
def create(payload: CalibrationCreate):
    """登记原始校准记录（版本链 v1）。

    区间冲突（同仪器当前版本 [valid_from, valid_to) 重叠）返回 409。
    """
    row = run_tx(lambda conn: create_calibration(conn, payload))
    return {"calibration": serialize_calibration(row)}


@router.post("/corrections", status_code=201)
def correct(payload: CalibrationCorrect):
    """更正校准：引用原记录并提交连续版本号。

    - 版本号不连续/已过期 -> 409 VERSION_CONFLICT
    - 新版本区间与该仪器其他当前版本重叠 -> 409 INTERVAL_OVERLAP
    - 成功后顺带返回“结论与发布快照不同”的已发布报告列表（不修改快照）
    """
    new_row, drift = run_tx(lambda conn: correct_calibration(conn, payload))
    return {"calibration": serialize_calibration(new_row),
            "affected_report_drift": drift}


@router.get("")
def list_calibrations(
    instrument: str | None = Query(None),
    current_only: bool = Query(False, description="仅返回各链最新版本"),
):
    assert db.pool is not None
    sql = "SELECT * FROM calibration_versions"
    where, params = [], []
    if instrument:
        where.append("instrument = %s")
        params.append(instrument)
    if current_only:
        where.append("is_current = TRUE")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY instrument, chain_id, version"
    with db.pool.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {"items": [serialize_calibration(r) for r in rows]}


@router.get("/{calibration_id}")
def get_calibration(calibration_id: int):
    assert db.pool is not None
    with db.pool.connection() as conn:
        row = conn.execute(
            "SELECT * FROM calibration_versions WHERE id = %s", (calibration_id,)
        ).fetchone()
    if row is None:
        from ..errors import not_found
        raise not_found("校准记录", calibration_id)
    return {"calibration": serialize_calibration(row)}


@router.get("/drift/recompute")
def recompute_drift(instrument: str | None = Query(None)):
    """按当前校准有效性重算全部已发布报告，列出与快照结论不同者。

    只读操作，绝不改写任何报告快照。
    """
    items = run_tx(lambda conn: compute_drift(conn, instrument), readonly=True)
    return {"items": items, "count": len(items)}
