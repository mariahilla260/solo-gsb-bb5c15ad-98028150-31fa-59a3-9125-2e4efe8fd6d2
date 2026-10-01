"""检测报告接口。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import db
from ..errors import not_found
from ..models import (
    ReportSubmit,
    SupersedeSubmit,
    WithdrawSubmit,
    serialize_supersession,
    serialize_withdrawal,
)
from ..repository import (
    chain_state,
    chain_tip_id,
    compute_drift,
    publish_report,
    report_lineage,
    supersede_report,
    withdraw_report,
)
from ..transactions import run_tx

router = APIRouter(prefix="/reports", tags=["reports"])

# 链尾操作（接替/撤回）失败：败方需补“当前链状态”（区分链尾标识与当前可使用标识）
_CHAIN_CONFLICT_CODES = {
    "REPORT_ALREADY_SUPERSEDED",
    "REPORT_ALREADY_WITHDRAWN",
    "REPORT_WITHDRAWN",
}


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


def _enrich_chain_conflict(exc: HTTPException, report_id: int) -> HTTPException:
    """链尾操作（接替/撤回）失败时，以全新只读事务补当前链状态。

    保证败方看到的是已提交状态（冲突事务自身的快照可能过旧）：
    tip_report_id 恒非空；current_report_id 在链尾撤回后为 NULL。
    """
    detail = exc.detail
    if exc.status_code == 409 and isinstance(detail, dict) \
            and detail.get("code") in _CHAIN_CONFLICT_CODES:
        state = run_tx(lambda conn: chain_state(conn, report_id), readonly=True)
        if state is not None:
            detail["tip_report_id"] = state["tip_report_id"]
            detail["current_report_id"] = state["current_report_id"]
            if state["withdrawal"] is not None:
                detail["withdrawal"] = serialize_withdrawal(state["withdrawal"])
    return exc


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

    - 仅“尚未被接替且尚未撤回”的链尾报告可被接替；复测放行规则与首次提交
      完全一致（禁用优先于校准，检测时间须被一条“通过”的当前校准版本覆盖）
    - 成功：201，新报告与接替关系原子落库；旧报告与其发布快照永久保留
    - 旧报告不存在 -> 404；已被接替（含并发接替败方）-> 409
      REPORT_ALREADY_SUPERSEDED；链尾已撤回（撤回后复测接替必须拒绝）-> 409
      REPORT_WITHDRAWN；detail 均附当前链状态（tip_report_id 与
      current_report_id，撤回后后者为 null）
    - 复测被阻断（命中禁用/无校准/区间外/未通过）-> 422，新报告编号冲突
      （含沿用旧编号）-> 409 REPORT_REFERENCE_EXISTS；两者均整体回滚，
      旧报告仍有效，不留下接替记录、不生成新报告
    """
    try:
        return run_tx(
            lambda conn: supersede_report(conn, report_id, payload),
            advisory_lock=db.instrument_lock_key(payload.instrument),
        )
    except HTTPException as exc:
        # 兼容旧约定：已接替败方额外给出“现行报告标识”（未撤回时即链尾）
        detail = exc.detail
        if (exc.status_code == 409 and isinstance(detail, dict)
                and detail.get("code") == "REPORT_ALREADY_SUPERSEDED"):
            detail["current_report_id"] = run_tx(
                lambda conn: chain_tip_id(conn, report_id), readonly=True)
        raise _enrich_chain_conflict(exc, report_id)


@router.post("/{report_id}/withdraw", status_code=201)
def withdraw(report_id: int, payload: WithdrawSubmit):
    """质控登记撤回一份已发布报告（样本身份错误等）。

    - 仅接替链**链尾且尚未撤回**的报告可撤回；成功后该链不再有可使用的
      现行报告（current_report_id 为 null），报告与其发布快照永久保留，
      沿革显示撤回单、原因与时间
    - 报告不存在 -> 404 NOT_FOUND；已被接替（非链尾）-> 409
      REPORT_ALREADY_SUPERSEDED；已撤回（含并发撤回败方）-> 409
      REPORT_ALREADY_WITHDRAWN；撤回单号已被使用（不得转给其他报告或复用）
      -> 409 WITHDRAWAL_NO_EXISTS；detail 附当前链状态
    - 任一失败均不写撤回记录；同一链尾的撤回与接替并发至多一方成功，
      败方返回当前链状态
    """
    try:
        result = run_tx(lambda conn: withdraw_report(conn, report_id, payload))
    except HTTPException as exc:
        raise _enrich_chain_conflict(exc, report_id)
    withdrawal = serialize_withdrawal(result["withdrawal"])
    # 成功时链尾即被撤回报告：tip_report_id 仍指向它，current_report_id 为 null
    return {
        "withdrawn": True,
        "tip_report_id": report_id,
        "current_report_id": None,
        "report_id": report_id,
        "withdrawal": withdrawal,
    }


@router.get("/{report_id}/lineage")
def get_lineage(report_id: int):
    """按链内任一报告 ID 查询完整沿革。

    返回该链全部报告（按接替顺序，含各自发布快照）、每次接替的原因与时间、
    撤回记录（撤回单/原因/时间）、链尾标识与当前可使用报告。
    链尾被撤回后 ``current_report_id`` 为 null；只读，快照永不改写。
    """
    lineage = run_tx(lambda conn: report_lineage(conn, report_id), readonly=True)
    if lineage is None:
        raise not_found("报告", report_id)
    return {
        "report_id": report_id,
        "tip_report_id": lineage["tip_report_id"],
        "current_report_id": lineage["current_report_id"],
        "reports": [_serialize_report(r) for r in lineage["reports"]],
        "supersessions": [serialize_supersession(s) for s in lineage["supersessions"]],
        "withdrawals": [serialize_withdrawal(w) for w in lineage["withdrawals"]],
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
