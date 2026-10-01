"""排班规划：仪器组“全组可放行”时间窗查询。

按同一已提交状态（一次 SERIALIZABLE 只读事务）读取当前未撤销禁用区间与
当前校准版本，把查询范围 [start, end) 切分为左闭右开时段：
禁用判定优先于校准判定；每段在“全组结论 + 各仪器原因码与依据”均相同时
与相邻段合并。结果仅供规划，不预占未来放行资格。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

# 与发布判定共用原因码（现有错误约定保持兼容）
from .repository import (
    REASON_DISABLED,
    REASON_NO_CALIBRATION,
    REASON_NOT_PASSED,
    REASON_OUTSIDE_PERIOD,
    REASON_PASSED,
    disable_brief,
)

# 每台仪器相关当前校准 / 当前未撤销禁用的装载 SQL。
# 校准取该仪器“全部”当前版本（即便完全落在查询范围外），以保证
# NO_CALIBRATION（该仪器当前无任何校准）与 OUTSIDE_VALID_PERIOD 的相邻
# 线索判定和发布判定完全一致；切分边界只取落在查询范围内的端点。
_CAL_LOAD_SQL = """
    SELECT *
    FROM calibration_versions
    WHERE is_current = TRUE
      AND instrument = ANY(%s)
    ORDER BY instrument, valid_from
"""

_DISABLE_LOAD_SQL = """
    SELECT *
    FROM instrument_disable_events
    WHERE revoked = FALSE
      AND instrument = ANY(%s)
    ORDER BY instrument, disabled_from
"""

PLANNING_NOTICE = (
    "结果仅用于排班规划，不预占未来放行资格；正式报告仍以提交时的当前状态判定。"
)


def _cal_brief(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "chain_id": row["chain_id"],
        "version": row["version"],
        "valid_from": row["valid_from"].isoformat(),
        "valid_to": row["valid_to"].isoformat() if row["valid_to"] else None,
        "passed": row["passed"],
    }


def _covering(rows: list[dict[str, Any]], at: datetime,
              from_key: str, to_key: str) -> dict[str, Any] | None:
    """左闭右开命中：from <= at < to（NULL 终点视为开放）。

    调用前已按 instrument 过滤；数据库 GiST 排他约束保证当前校准/未撤销
    禁用区间各自不重叠，故至多命中一条。
    """
    for row in rows:
        if row[from_key] <= at and (row[to_key] is None or at < row[to_key]):
            return row
    return None


def _instrument_state(
    cals: list[dict[str, Any]],
    disables: list[dict[str, Any]],
    at: datetime,
) -> dict[str, Any]:
    """与发布一致的单点判定（禁用优先，再校准），但只读、不抛 4xx。

    返回 releasable / reason_code，以及所据禁用事件或校准版本（缺失时给
    相邻校准线索，保持与现有 OUTSIDE_VALID_PERIOD 错误体相同的信息量）。
    """
    disable = _covering(disables, at, "disabled_from", "disabled_to")
    if disable is not None:
        return {
            "releasable": False,
            "reason_code": REASON_DISABLED,
            "basis_disable": disable_brief(disable),
            "basis_calibration": None,
        }

    cal = _covering(cals, at, "valid_from", "valid_to")
    if cal is not None:
        if cal["passed"]:
            return {
                "releasable": True,
                "reason_code": REASON_PASSED,
                "basis_disable": None,
                "basis_calibration": cal,
            }
        return {
            "releasable": False,
            "reason_code": REASON_NOT_PASSED,
            "basis_disable": None,
            "basis_calibration": cal,
        }

    # 无当前校准版本：整个查询范围内结论恒为 NO_CALIBRATION
    if not cals:
        return {
            "releasable": False,
            "reason_code": REASON_NO_CALIBRATION,
            "basis_disable": None,
            "basis_calibration": None,
        }

    # 有校准但该时点不被任何区间覆盖：取时间上相邻的当前版本作为线索。
    # 排序与发布判定的 _NEAREST_SQL 完全一致：
    #   ORDER BY (valid_from <= at) DESC,
    #            CASE WHEN valid_from <= at THEN valid_to END DESC NULLS LAST,
    #            valid_from ASC
    # 即：优先 valid_from <= at 的版本，其中取 valid_to 最大（NULL 最后）；
    # 否则取 valid_from 最早的未来版本。下面以 min() 键等价模拟该降序/NULL 规则。
    def _nearest_key(c: dict[str, Any]) -> tuple[Any, ...]:
        before = c["valid_from"] <= at
        end = c["valid_to"]
        if before:
            # valid_to DESC NULLS LAST：非 NULL 越大越优先（取负），NULL 置底
            end_rank = float("inf") if end is None else -end.timestamp()
            return (0, end_rank, c["valid_from"].timestamp())
        return (1, 0.0, c["valid_from"].timestamp())

    nearest = min(cals, key=_nearest_key)
    return {
        "releasable": False,
        "reason_code": REASON_OUTSIDE_PERIOD,
        "basis_disable": None,
        "basis_calibration": None,
        "nearest_calibration": nearest,
    }


def _state_signature(state: dict[str, Any]) -> tuple[Any, ...]:
    """合并依据：原因码 + 所据禁用事件号 / 校准版本 ID（及相邻线索）。"""
    return (
        state["reason_code"],
        (state["basis_disable"] or {}).get("event_no"),
        ((state.get("basis_calibration") or {}).get("id")),
        ((state.get("nearest_calibration") or {}).get("id")),
    )


def _serialize_instrument(instrument: str, state: dict[str, Any]) -> dict[str, Any]:
    return {
        "instrument": instrument,
        "releasable": state["releasable"],
        "reason_code": state["reason_code"],
        "basis_disable": state["basis_disable"],
        "basis_calibration": _cal_brief(state["basis_calibration"]),
        "nearest_calibration": _cal_brief(state.get("nearest_calibration")),
    }


def plan_release_windows(
    conn,
    instruments: list[str],
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """切分 [start, end) 并返回按时间排序的全组可放行时段。

    全程使用同一事务快照（调用方以 SERIALIZABLE 只读事务包裹），因此不会
    混用并发的更正 / 禁用登记 / 撤销前后状态。
    """
    with conn.cursor() as cur:
        cur.execute(_CAL_LOAD_SQL, (instruments,))
        cal_rows = cur.fetchall()
        cur.execute(_DISABLE_LOAD_SQL, (instruments,))
        dis_rows = cur.fetchall()

    cals_by: dict[str, list[dict[str, Any]]] = {i: [] for i in instruments}
    dis_by: dict[str, list[dict[str, Any]]] = {i: [] for i in instruments}
    for row in cal_rows:
        cals_by.setdefault(row["instrument"], []).append(row)
    for row in dis_rows:
        dis_by.setdefault(row["instrument"], []).append(row)

    # 所有可能改变任一仪器结论的边界：查询首尾 + 校准/禁用的起终点
    # （开放终点视为 +inf，不产生边界）。
    boundaries: set[datetime] = {start, end}
    for rows in (*cals_by.values(), *dis_by.values()):
        for row in rows:
            frm = row.get("valid_from", row.get("disabled_from"))
            to = row.get("valid_to", row.get("disabled_to"))
            if start < frm < end:
                boundaries.add(frm)
            if to is not None and start < to < end:
                boundaries.add(to)
    cuts = sorted(boundaries)

    segments: list[dict[str, Any]] = []
    for lo, hi in zip(cuts, cuts[1:]):
        if lo >= hi:
            continue
        per_instrument: dict[str, dict[str, Any]] = {}
        all_releasable = True
        for instrument in instruments:
            state = _instrument_state(cals_by.get(instrument, []),
                                      dis_by.get(instrument, []), lo)
            per_instrument[instrument] = state
            all_releasable = all_releasable and state["releasable"]

        signature = (
            all_releasable,
            tuple((inst, _state_signature(per_instrument[inst])) for inst in instruments),
        )
        segment = {
            "start": lo,
            "end": hi,
            "all_releasable": all_releasable,
            "signature": signature,
            "_per_instrument": per_instrument,
        }

        # 仅当全组结论与各仪器依据（原因码/所据事件/版本）均相同时合并相邻段
        if segments and segments[-1]["signature"] == signature and \
                segments[-1]["end"] == lo:
            segments[-1]["end"] = hi
            segments[-1]["_per_instrument"] = per_instrument
        else:
            segments.append(segment)

    items: list[dict[str, Any]] = []
    for seg in segments:
        items.append({
            "start": seg["start"].isoformat(),
            "end": seg["end"].isoformat(),
            "all_releasable": seg["all_releasable"],
            "instruments": [
                _serialize_instrument(inst, seg["_per_instrument"][inst])
                for inst in instruments
            ],
        })

    return {
        "instruments": instruments,
        "query_start": start.isoformat(),
        "query_end": end.isoformat(),
        "segments": items,
        "segment_count": len(items),
        "planning_only": True,
        "notice": PLANNING_NOTICE,
    }
