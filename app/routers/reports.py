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
    chain_status,
    chain_tip_id,
    compute_drift,
    get_report_flags,
    list_reports_flags,
    publish_report,
    report_lineage,
    supersede_report,
    withdraw_report,
)
from ..transactions import run_tx

router = APIRouter(prefix="/reports", tags=["reports"])

# 撤回/接替并发失败时需要补“当前链状态”的原因码
_CHAIN_STATE_CODES = {
    "REPORT_ALREADY_SUPERSEDED",
    "REPORT_ALREADY_WITHDRAWN",
    "REPORT_WITHDRAWN",
}


def _withdrawal_obj(row: dict | None) -> dict | None:
    """沿革行上的撤回登记（报告本身被撤回时非空）。"""
    if row is None or row.get("withdrawal_id") is None:
        return None
    return {
        "id": row["withdrawal_id"],
        "report_id": row["id"],
        "withdrawal_no": row["withdrawal_no"],
        "reason": row["withdrawal_reason"],
        "created_at": row["withdrawn_at"].isoformat(),
    }


def _serialize_report(row: dict, *, chain_flags: bool = True) -> dict:
    """报告详情/列表：历史内容字段原样，默认追加链尾与可使用标识。

    沿革的 reports[] 沿用既有字段集合（向后兼容，不加额外字段）；
    链尾/可使用信息沿革以顶层 tip_report_id/usable_report_id 与
    withdrawals[] 表达。
    """
    item = {
        "id": row["id"],
        "reference": row["reference"],
        "instrument": row["instrument"],
        "tested_at": row["tested_at"].isoformat(),
        "released": row["released"],
        "basis_calibration_id": row["basis_calibration_id"],
        "snapshot": row["snapshot"],
        "created_at": row["created_at"].isoformat(),
    }
    if chain_flags:
        item["is_chain_tip"] = row["is_chain_tip"]
        item["tip_report_id"] = row["tip_report_id"]
        item["is_usable"] = row["is_usable"]
        item["is_withdrawn"] = row["is_withdrawn"]
        item["withdrawal"] = _withdrawal_obj(row)
    return item


def _attach_chain_state(detail: dict, report_id: int) -> None:
    """并发/状态冲突的败方：以全新只读事务返回当前链状态。"""
    status = run_tx(lambda conn: chain_status(conn, report_id), readonly=True)
    if status is not None:
        detail["chain_state"] = status
        detail["current_report_id"] = status["tip_report_id"]
        detail["usable_report_id"] = status["usable_report_id"]


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


@router.post("/{report_id}/withdraw", status_code=201)
def withdraw(report_id: int, payload: WithdrawSubmit):
    """登记撤回样本身份错误的已发布报告。

    - 仅链尾且尚未撤回的报告可撤回；成功后该链不再有可使用的现行报告，
      报告行与发布快照永久保留，沿革显示撤回单、原因与时间
    - 报告不存在 -> 404 NOT_FOUND；已接替（非链尾）-> 409
      REPORT_ALREADY_SUPERSEDED；已撤回 -> 409 REPORT_ALREADY_WITHDRAWN；
      撤回单号已存在（不得转给其他报告）-> 409 WITHDRAWAL_NO_EXISTS
    - 失败一律不写撤回；响应/错误体内附当前链状态（区分链尾与可使用标识）
    """
    try:
        result = run_tx(
            lambda conn: withdraw_report(conn, report_id, payload),
            advisory_lock=db.report_lock_key(report_id),
        )
    except HTTPException as exc:
        detail = exc.detail
        if (isinstance(detail, dict) and detail.get("code") in _CHAIN_STATE_CODES):
            _attach_chain_state(detail, report_id)
        raise
    status = result["chain_status"]
    return {
        "withdrawal": serialize_withdrawal(result["withdrawal"]),
        "report_id": report_id,
        "tip_report_id": status["tip_report_id"],
        "usable_report_id": status["usable_report_id"],
        "chain_state": status,
    }


@router.post("/{report_id}/supersede", status_code=201)
def supersede(report_id: int, payload: SupersedeSubmit):
    """复测并接替一份已发布报告（当前有效报告切换为新版）。

    - 仅“尚未被接替、尚未撤回”的链尾报告可被接替；复测放行规则与首次提交
      完全一致（禁用优先于校准，检测时间须被一条“通过”的当前校准版本覆盖）
    - 成功：201，新报告与接替关系原子落库；旧报告与其发布快照永久保留
    - 旧报告不存在 -> 404；已被接替（含并发接替败方）-> 409
      REPORT_ALREADY_SUPERSEDED；已撤回（含与撤回并发的败方）-> 409
      REPORT_WITHDRAWN，拒绝接替且不生成新报告；detail 内附当前链状态
    - 复测被阻断（命中禁用/无校准/区间外/未通过）-> 422，新报告编号冲突
      （含沿用旧编号）-> 409 REPORT_REFERENCE_EXISTS；两者均整体回滚，
      旧报告仍有效，不留下接替记录
    """
    try:
        # 统一锁序“报告链锁 -> 复测仪器锁”（撤回只取报告链锁）：
        # 链尾行锁只可能被持报告链锁的事务持有，因此先链锁后仪器锁
        # 不会与任何事务成环；同链尾撤回/接替据此严格串行，至多一方成功
        return run_tx(
            lambda conn: supersede_report(conn, report_id, payload),
            advisory_lock=[
                db.report_lock_key(report_id),
                db.instrument_lock_key(payload.instrument),
            ],
        )
    except HTTPException as exc:
        detail = exc.detail
        if (isinstance(detail, dict) and detail.get("code") in _CHAIN_STATE_CODES):
            # 败方返回当前链状态：以全新只读事务读取已提交状态
            _attach_chain_state(detail, report_id)
            if detail.get("code") == "REPORT_ALREADY_SUPERSEDED":
                # 兼容既有字段
                detail.setdefault(
                    "current_report_id",
                    run_tx(lambda conn: chain_tip_id(conn, report_id), readonly=True))
        raise


@router.get("/{report_id}/lineage")
def get_lineage(report_id: int):
    """按链内任一报告 ID 查询完整沿革。

    返回该链全部报告（按接替顺序，含各自发布快照与撤回登记）、每次接替/撤回
    的原因与时间，以及链尾标识与当前可使用标识。只读，快照永不改写；
    撤回后链尾仍是被撤回的报告，但不再有可使用的现行报告。
    """
    lineage = run_tx(lambda conn: report_lineage(conn, report_id), readonly=True)
    if lineage is None:
        raise not_found("报告", report_id)
    return {
        "report_id": report_id,
        "tip_report_id": lineage["tip_report_id"],
        "current_report_id": lineage["tip_report_id"],
        "usable_report_id": lineage["usable_report_id"],
        "reports": [_serialize_report(r, chain_flags=False)
                    for r in lineage["reports"]],
        "supersessions": [serialize_supersession(s) for s in lineage["supersessions"]],
        "withdrawals": [serialize_withdrawal(w) for w in lineage["withdrawals"]],
        "count": len(lineage["reports"]),
    }


@router.get("")
def list_reports(instrument: str | None = None):
    rows = run_tx(lambda conn: list_reports_flags(conn, instrument), readonly=True)
    return {"items": [_serialize_report(r) for r in rows]}


@router.get("/{report_id}")
def get_report(report_id: int):
    row = run_tx(lambda conn: get_report_flags(conn, report_id), readonly=True)
    if row is None:
        raise not_found("报告", report_id)
    return {"report": _serialize_report(row)}
