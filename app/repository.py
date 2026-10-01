"""领域逻辑：校准判定、版本更正、发布、快照漂移重算。"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from psycopg.errors import CheckViolation, ExclusionViolation, UniqueViolation

from .errors import blocked, conflict, not_found

# ---- 判定原因码 ----------------------------------------------------------------
# 放行成功
REASON_PASSED = "PASSED_CALIBRATION_COVERAGE"
# 检测时间命中该仪器“未撤销”的禁用区间（即使校准通过也阻断）
REASON_DISABLED = "INSTRUMENT_DISABLED"
# 该仪器当前没有任何校准版本
REASON_NO_CALIBRATION = "NO_CALIBRATION"
# 存在当前校准版本，但检测时间不落在任何 [valid_from, valid_to) 内
REASON_OUTSIDE_PERIOD = "OUTSIDE_VALID_PERIOD"
# 覆盖检测时间的当前校准版本结论为未通过
REASON_NOT_PASSED = "CALIBRATION_NOT_PASSED"

# 会话级咨询锁命名空间（int4）：同仪器的“禁用变更 / 发布”共用一把锁。
# 键为 (ADVISORY_LOCK_NS, hashtext(instrument))，hashtext 由数据库计算
# （见 app.db.instrument_lock_key，在事务外取键）。
ADVISORY_LOCK_NS = 0xD15A  # “DISA”
# 报告级会话咨询锁命名空间（int4）：同一链尾的“撤回 / 复测接替”共用一把锁，
# 键为 (REPORT_LOCK_NS, 报告 ID)（见 app.db.report_lock_key，在事务外取键）。
REPORT_LOCK_NS = 0x5E2A  # “WD”

_COVERAGE_SQL = """
    SELECT *
    FROM calibration_versions
    WHERE instrument = %s
      AND is_current = TRUE
      AND valid_from <= %s
      AND (valid_to IS NULL OR %s < valid_to)
"""

_NEAREST_SQL = """
    SELECT *
    FROM calibration_versions
    WHERE instrument = %s AND is_current = TRUE
    ORDER BY (valid_from <= %s) DESC,
             CASE WHEN valid_from <= %s THEN valid_to END DESC NULLS LAST,
             valid_from ASC
"""


def _cal_brief(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "chain_id": row["chain_id"],
        "version": row["version"],
        "valid_from": row["valid_from"].isoformat(),
        "valid_to": row["valid_to"].isoformat() if row["valid_to"] else None,
        "passed": row["passed"],
    }


# ---- 禁用区间判定 --------------------------------------------------------------

_DISABLE_COVERAGE_SQL = """
    SELECT *
    FROM instrument_disable_events
    WHERE instrument = %s
      AND revoked = FALSE
      AND disabled_from <= %s
      AND (disabled_to IS NULL OR %s < disabled_to)
"""


def disable_brief(row: dict[str, Any]) -> dict[str, Any]:
    """禁用依据（出现在阻断响应与受影响报告查询中）。"""
    return {
        "event_no": row["event_no"],
        "instrument": row["instrument"],
        "disabled_from": row["disabled_from"].isoformat(),
        "disabled_to": row["disabled_to"].isoformat() if row["disabled_to"] else None,
        "reason": row["reason"],
    }


def find_active_disable(conn, instrument: str, at: datetime, *, lock: bool):
    """返回命中该时点的未撤销禁用区间（至多一条，排他约束保证），无则 None。

    ``lock=True`` 时命中行加 FOR UPDATE；未命中时锁定该仪器全部未撤销行，
    与“登记禁用 / 撤销禁用”事务互斥，保证判定可串行化。
    """
    sql = _DISABLE_COVERAGE_SQL + (" FOR UPDATE" if lock else "")
    with conn.cursor() as cur:
        cur.execute(sql, (instrument, at, at))
        rows = cur.fetchall()
        if rows:
            return rows[0]
        if lock:
            cur.execute(
                "SELECT id FROM instrument_disable_events "
                "WHERE instrument = %s AND revoked = FALSE FOR UPDATE",
                (instrument,),
            )
            cur.fetchall()
    return None


def determine(conn, instrument: str, tested_at: datetime, *, lock: bool) -> dict[str, Any]:
    """返回该时点的校准判定。

    ``lock=True`` 时对覆盖行加 FOR UPDATE 行锁，并在无覆盖时锁定该仪器全部
    当前版本 —— 与校准更正事务互斥，保证“发布”与“更正”可串行化。
    """
    sql = _COVERAGE_SQL + (" FOR UPDATE" if lock else "")
    with conn.cursor() as cur:
        cur.execute(sql, (instrument, tested_at, tested_at))
        covering = cur.fetchall()

    if covering:
        row = covering[0]  # 排除约束保证同仪器当前版本区间不重叠，至多一条
        if row["passed"]:
            return {"releasable": True, "reason_code": REASON_PASSED,
                    "calibration": row, "reason_message": None}
        return {
            "releasable": False, "reason_code": REASON_NOT_PASSED,
            "calibration": row,
            "reason_message": f"检测时间 {tested_at.isoformat()} 被校准版本 "
                              f"v{row['version']} 覆盖，但其结论为未通过",
        }

    # 无覆盖：给出可操作的阻断原因
    with conn.cursor() as cur:
        if lock:
            cur.execute(
                "SELECT id FROM calibration_versions "
                "WHERE instrument = %s AND is_current = TRUE FOR UPDATE",
                (instrument,),
            )
            cur.fetchall()
        cur.execute(
            "SELECT count(*) AS n FROM calibration_versions "
            "WHERE instrument = %s AND is_current = TRUE",
            (instrument,),
        )
        count = cur.fetchone()["n"]
        nearest = None
        if count:
            cur.execute(_NEAREST_SQL, (instrument, tested_at, tested_at))
            nearest = cur.fetchone()

    if count == 0:
        return {"releasable": False, "reason_code": REASON_NO_CALIBRATION,
                "calibration": None,
                "reason_message": f"仪器 {instrument} 当前不存在任何校准记录"}

    if nearest["valid_from"] > tested_at:
        msg = (f"检测时间 {tested_at.isoformat()} 早于最近校准生效时间 "
               f"{nearest['valid_from'].isoformat()}（左闭右开区间）")
    else:
        end = nearest["valid_to"]
        msg = (f"检测时间 {tested_at.isoformat()} 晚于最近校准失效时间 "
               f"{end.isoformat() if end else '开放区间'}（左闭右开区间）")
    return {"releasable": False, "reason_code": REASON_OUTSIDE_PERIOD,
            "calibration": None, "nearest_calibration": nearest,
            "reason_message": msg}


# ---- 校准写入 ------------------------------------------------------------------

def create_calibration(conn, payload) -> dict[str, Any]:
    """创建原始校准记录（新版本链的 v1）。"""
    if payload.valid_to is not None and payload.valid_to <= payload.valid_from:
        raise blocked("INVALID_INTERVAL",
                      "有效区间必须满足 valid_from < valid_to（左闭右开）")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO calibration_chains (instrument) VALUES (%s) RETURNING id",
            (payload.instrument,),
        )
        chain_id = cur.fetchone()["id"]
        try:
            cur.execute(
                """
                INSERT INTO calibration_versions
                    (chain_id, version, instrument, valid_from, valid_to,
                     passed, corrects_id, is_current)
                VALUES (%s, 1, %s, %s, %s, %s, NULL, TRUE)
                RETURNING *
                """,
                (chain_id, payload.instrument, payload.valid_from,
                 payload.valid_to, payload.passed),
            )
        except ExclusionViolation:
            raise conflict("INTERVAL_OVERLAP",
                           "与该仪器另一条当前校准版本的有效区间重叠",
                           instrument=payload.instrument)
        return cur.fetchone()


def correct_calibration(conn, payload) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """提交更正版本。返回 (新版本, 受影响的已发布报告漂移列表)。"""
    if payload.valid_to is not None and payload.valid_to <= payload.valid_from:
        raise blocked("INVALID_INTERVAL",
                      "有效区间必须满足 valid_from < valid_to（左闭右开）")

    with conn.cursor() as cur:
        # 锁定被引用记录
        cur.execute("SELECT * FROM calibration_versions WHERE id = %s FOR UPDATE",
                    (payload.corrects_id,))
        original = cur.fetchone()
        if original is None:
            raise not_found("校准记录", payload.corrects_id)

        # 锁版本链：同一链上的并发更正在此串行
        cur.execute("SELECT id FROM calibration_chains WHERE id = %s FOR UPDATE",
                    (original["chain_id"],))

        cur.execute(
            "SELECT * FROM calibration_versions WHERE chain_id = %s AND is_current = TRUE FOR UPDATE",
            (original["chain_id"],),
        )
        current = cur.fetchone()
        # 唯一部分索引保证至多一条；防御性处理
        assert current is not None, "版本链缺少最新版本，数据异常"

        expected_version = current["version"] + 1
        if payload.version != expected_version:
            raise conflict(
                "VERSION_CONFLICT",
                f"版本号冲突：当前最新版本为 v{current['version']}，"
                f"更正必须提交连续版本号 v{expected_version}，收到 v{payload.version}",
                current_version=current["version"],
                expected_version=expected_version,
            )

        instrument = current["instrument"]

        # 先下线旧版，再插入新版（避免与自身触发区间约束）
        cur.execute(
            "UPDATE calibration_versions SET is_current = FALSE "
            "WHERE id = %s RETURNING id",
            (current["id"],),
        )
        try:
            cur.execute(
                """
                INSERT INTO calibration_versions
                    (chain_id, version, instrument, valid_from, valid_to,
                     passed, corrects_id, is_current)
                VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
                RETURNING *
                """,
                (current["chain_id"], payload.version, instrument,
                 payload.valid_from, payload.valid_to, payload.passed,
                 payload.corrects_id),
            )
        except UniqueViolation:
            raise conflict("VERSION_CONFLICT",
                           "版本冲突：该版本号已存在或链上已存在最新版")
        except ExclusionViolation:
            raise conflict("INTERVAL_OVERLAP",
                           "新版本与该仪器另一条当前校准版本的有效区间重叠",
                           instrument=instrument)
        new_row = cur.fetchone()

    drift = _drift_for_instrument(conn, instrument)
    return new_row, drift


# ---- 报告发布 ------------------------------------------------------------------

def _decide_and_insert(conn, instrument: str, tested_at: datetime,
                       reference: str | None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """按“禁用优先 + 当前通过校准覆盖”判定并落库，返回 (报告行, 所据校准行, 快照)。

    首次提交与复测接替共用同一套放行规则；阻断（禁用/无校准/区间外/未通过）
    抛 422，编号冲突抛 409，调用方事务整体回滚，不留任何痕迹。
    """
    # 禁用优先：命中未撤销禁用区间即拒绝放行，即使当前校准通过。
    active_disable = find_active_disable(conn, instrument, tested_at, lock=True)
    if active_disable is not None:
        raise blocked(
            REASON_DISABLED,
            f"检测时间 {tested_at.isoformat()} 命中仪器 "
            f"{instrument} 的未撤销禁用区间（事件 {active_disable['event_no']}）："
            f"{active_disable['reason']}",
            instrument=instrument,
            tested_at=tested_at.isoformat(),
            disable=disable_brief(active_disable),
        )

    decision = determine(conn, instrument, tested_at, lock=True)
    basis = decision["calibration"]
    snapshot: dict[str, Any] = {
        "released": decision["releasable"],
        "reason_code": decision["reason_code"],
        "instrument": instrument,
        "tested_at": tested_at.isoformat(),
    }
    if basis is not None:
        snapshot["basis_calibration"] = _cal_brief(basis)
    if not decision["releasable"]:
        snapshot["reason_message"] = decision["reason_message"]
        raise blocked(
            decision["reason_code"],
            decision["reason_message"],
            instrument=instrument,
            tested_at=tested_at.isoformat(),
            nearest_calibration=(
                _cal_brief(decision["nearest_calibration"])
                if decision.get("nearest_calibration") else None
            ),
        )

    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                INSERT INTO reports
                    (reference, instrument, tested_at, released,
                     basis_calibration_id, snapshot)
                VALUES (%s, %s, %s, TRUE, %s, %s)
                RETURNING *
                """,
                (reference, instrument, tested_at,
                 basis["id"], json.dumps(snapshot)),
            )
        except UniqueViolation:
            raise conflict("REPORT_REFERENCE_EXISTS",
                           f"报告编号已存在: {reference}")
        report = cur.fetchone()
    return report, basis, snapshot


def publish_report(conn, payload) -> dict[str, Any]:
    report, basis, snapshot = _decide_and_insert(
        conn, payload.instrument, payload.tested_at, payload.reference)
    return {
        "released": True,
        "reason_code": REASON_PASSED,
        "basis_calibration": _cal_brief(basis),
        "report_id": report["id"],
        "reference": report["reference"],
        "snapshot": snapshot,
    }


# ---- 报告撤回（样本身份错误登记） ------------------------------------------------

# 链状态：区分“链尾标识”与“当前可使用标识”，并给出目标报告自身的状态。
# 撤回/接替并发失败时，败方据此拿到已提交后的最新链状态。
_CHAIN_STATUS_SQL = """
    WITH RECURSIVE members(id) AS (
        SELECT %s::bigint
        UNION
        SELECT CASE WHEN s.old_report_id = m.id THEN s.new_report_id
                    ELSE s.old_report_id END
        FROM report_supersessions s, members m
        WHERE m.id = s.old_report_id OR m.id = s.new_report_id
    )
    SELECT m.id AS report_id,
           NOT EXISTS (SELECT 1 FROM report_supersessions s2
                       WHERE s2.old_report_id = m.id) AS is_chain_tip,
           (w.id IS NOT NULL) AS is_withdrawn,
           w.id AS withdrawal_id, w.withdrawal_no, w.reason AS withdrawal_reason,
           w.created_at AS withdrawn_at
    FROM members m
    LEFT JOIN report_withdrawals w ON w.report_id = m.id
"""


def _chain_status_rows(conn, report_id: int) -> list[dict[str, Any]] | None:
    """目标报告不存在时返回 None；否则返回链上每份报告的链尾/撤回状态。"""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM reports WHERE id = %s", (report_id,))
        if cur.fetchone() is None:
            return None
        cur.execute(_CHAIN_STATUS_SQL, (report_id,))
        return cur.fetchall()


def chain_status(conn, report_id: int) -> dict[str, Any] | None:
    """目标报告的链状态快照（供并发败方与错误响应返回当前链状态）。

    - 目标报告不存在 -> None
    - tip_report_id：链尾报告 ID（链尾标识，可能已撤回）
    - usable_report_id：当前可使用报告 ID；链已撤回时为 None
    - report：目标报告自身状态（is_chain_tip/is_withdrawn/撤回单）
    """
    rows = _chain_status_rows(conn, report_id)
    if rows is None:
        return None

    def _member(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "report_id": r["report_id"],
            "is_chain_tip": r["is_chain_tip"],
            "is_withdrawn": r["is_withdrawn"],
            "withdrawal_no": r["withdrawal_no"],
            "withdrawal_reason": r["withdrawal_reason"],
            "withdrawn_at": r["withdrawn_at"].isoformat() if r["withdrawn_at"] else None,
        }

    members = [_member(r) for r in rows]
    tip = next(m for m in members if m["is_chain_tip"])
    return {
        "report_id": report_id,
        "tip_report_id": tip["report_id"],
        "usable_report_id": None if tip["is_withdrawn"] else tip["report_id"],
        "chain_withdrawn": tip["is_withdrawn"],
        "members": members,
        "report": next(m for m in members if m["report_id"] == report_id),
    }


def withdraw_report(conn, report_id: int, payload) -> dict[str, Any]:
    """登记撤回：仅“链尾且尚未撤回”的报告可撤回。

    - 报告不存在 -> 404 NOT_FOUND
    - 已被接替（非链尾）-> 409 REPORT_ALREADY_SUPERSEDED
    - 已撤回 -> 409 REPORT_ALREADY_WITHDRAWN（撤回单号不得复用）
    - 撤回单号已用于其他报告 -> 409 WITHDRAWAL_NO_EXISTS（不写撤回）
    成功后该链不再有可使用的现行报告；报告行与发布快照原样保留，仅追加撤回登记。
    """
    with conn.cursor() as cur:
        # 行锁：与“同报告的并发撤回”及接替事务的行锁互斥；
        # 链尾报告锁（咨询锁）在路由层先行获取，保证撤回与接替严格按先后串行
        cur.execute("SELECT * FROM reports WHERE id = %s FOR UPDATE", (report_id,))
        report = cur.fetchone()
        if report is None:
            raise not_found("报告", report_id)

        cur.execute(
            "SELECT * FROM report_withdrawals WHERE report_id = %s FOR UPDATE",
            (report_id,),
        )
        prior = cur.fetchone()
        if prior is not None:
            raise conflict(
                "REPORT_ALREADY_WITHDRAWN",
                f"报告 {report_id} 已撤回（撤回单 {prior['withdrawal_no']}），"
                "撤回不可重复登记",
                report_id=report_id,
                withdrawal_no=prior["withdrawal_no"],
            )

        cur.execute(
            "SELECT * FROM report_supersessions WHERE old_report_id = %s",
            (report_id,),
        )
        superseded = cur.fetchone()
        if superseded is not None:
            raise conflict(
                "REPORT_ALREADY_SUPERSEDED",
                f"报告 {report_id} 已被报告 {superseded['new_report_id']} 接替，"
                "仅链尾报告可以撤回",
                report_id=report_id,
                superseded_by=superseded["new_report_id"],
            )

        try:
            cur.execute(
                """
                INSERT INTO report_withdrawals (report_id, withdrawal_no, reason)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (report_id, payload.withdrawal_no, payload.reason),
            )
        except UniqueViolation:
            # 同号撤回单不得转给其他报告（全局唯一，撤回单号不可复用）
            raise conflict(
                "WITHDRAWAL_NO_EXISTS",
                f"撤回单号已存在（不得转给其他报告，也不可复用）: {payload.withdrawal_no}",
                withdrawal_no=payload.withdrawal_no,
            )
        except CheckViolation:
            # 并发接替已先提交：触发器拒绝“撤回已接替报告”，本事务整体回滚
            raise conflict(
                "REPORT_ALREADY_SUPERSEDED",
                f"报告 {report_id} 在撤回期间已被并发接替，本次撤回未生效",
                report_id=report_id,
            )
        withdrawal = cur.fetchone()

    status = chain_status(conn, report_id)
    return {"withdrawal": withdrawal, "report": report, "chain_status": status}


# ---- 报告接替（复测） ------------------------------------------------------------

def supersede_report(conn, report_id: int, payload) -> dict[str, Any]:
    """复测并接替已发布报告：原子发布新报告 + 记录接替关系。

    - 旧报告不存在 -> 404；已被接替 -> 409 REPORT_ALREADY_SUPERSEDED（链不分叉）
    - 旧报告已撤回（含撤回后的链尾）-> 409 REPORT_WITHDRAWN，拒绝接替且不生成新报告
    - 放行规则与首次提交完全一致（禁用优先 + 当前通过校准覆盖）；
      被阻断或新编号冲突时整体回滚：旧报告仍有效，不留下接替记录
    - 新报告编号沿用全局唯一约束，旧编号不得转移或复用（唯一索引保证）
    """
    with conn.cursor() as cur:
        # 行锁使“同一旧报告”的接替/撤回串行；唯一索引与触发器兜底
        cur.execute("SELECT * FROM reports WHERE id = %s FOR UPDATE", (report_id,))
        old = cur.fetchone()
        if old is None:
            raise not_found("报告", report_id)
        cur.execute(
            "SELECT * FROM report_supersessions WHERE old_report_id = %s",
            (report_id,),
        )
        existing = cur.fetchone()
        cur.execute(
            "SELECT * FROM report_withdrawals WHERE report_id = %s",
            (report_id,),
        )
        withdrawn = cur.fetchone()
    if existing is not None:
        raise conflict(
            "REPORT_ALREADY_SUPERSEDED",
            f"报告 {report_id} 已被报告 {existing['new_report_id']} 接替，"
            "只有尚未被接替的当前有效报告才能被接替",
            old_report_id=report_id,
            superseded_by=existing["new_report_id"],
            # 直接后继即新链尾（链不分叉）；路由层另以只读事务补全链状态
            current_report_id=existing["new_report_id"],
        )
    if withdrawn is not None:
        # 撤回后复测接替必须拒绝，且不生成新报告（调用方事务整体回滚）
        raise conflict(
            "REPORT_WITHDRAWN",
            f"报告 {report_id} 已撤回（撤回单 {withdrawn['withdrawal_no']}），"
            "撤回后不能再以复测接替生成新报告",
            old_report_id=report_id,
            withdrawal_no=withdrawn["withdrawal_no"],
        )

    report, basis, snapshot = _decide_and_insert(
        conn, payload.instrument, payload.tested_at, payload.reference)

    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                INSERT INTO report_supersessions (old_report_id, new_report_id, reason)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (report_id, report["id"], payload.reason),
            )
        except UniqueViolation:
            # 并发接替同一旧报告：唯一索引保证至多一个成功；本事务整体回滚，
            # 新报告与接替记录都不留，旧报告仍有效
            raise conflict(
                "REPORT_ALREADY_SUPERSEDED",
                f"报告 {report_id} 已被并发接替，本次接替未生效",
                old_report_id=report_id,
            )
        except CheckViolation:
            # 并发撤回已先提交：触发器拒绝“接替已撤回报告”，
            # 本事务整体回滚，新报告不留
            raise conflict(
                "REPORT_WITHDRAWN",
                f"报告 {report_id} 已被并发撤回，本次复测接替未生效且未生成新报告",
                old_report_id=report_id,
            )
        supersession = cur.fetchone()

    return {
        "released": True,
        "reason_code": REASON_PASSED,
        "basis_calibration": _cal_brief(basis),
        "report_id": report["id"],
        "reference": report["reference"],
        "snapshot": snapshot,
        "superseded_report_id": report_id,
        "current_report_id": report["id"],
        "supersession": {
            "old_report_id": supersession["old_report_id"],
            "new_report_id": supersession["new_report_id"],
            "reason": supersession["reason"],
            "created_at": supersession["created_at"].isoformat(),
        },
    }


# 从链内任一报告出发，沿接替边双向遍历取全链成员。
# 接替边恒由新报告指向旧报告（id 单调），链为无环单路径，递归必然终止。
_CHAIN_MEMBERS_CTE = """
    WITH RECURSIVE members(id) AS (
        SELECT %s::bigint
        UNION
        SELECT CASE WHEN s.old_report_id = m.id THEN s.new_report_id
                    ELSE s.old_report_id END
        FROM report_supersessions s, members m
        WHERE m.id = s.old_report_id OR m.id = s.new_report_id
    )
"""


def chain_tip_id(conn, report_id: int) -> int:
    """该报告所在接替链的当前有效报告（未被接替的链尾）。"""
    with conn.cursor() as cur:
        cur.execute(
            _CHAIN_MEMBERS_CTE + """
            SELECT m.id
            FROM members m
            WHERE NOT EXISTS (
                SELECT 1 FROM report_supersessions s
                WHERE s.old_report_id = m.id
            )
            """,
            (report_id,),
        )
        row = cur.fetchone()
    # 唯一索引保证链不分叉，链尾至多一个；报告存在时恰有一个
    return row["id"] if row else report_id


def report_lineage(conn, report_id: int) -> dict[str, Any] | None:
    """完整沿革：链内全部报告（含各自不可变快照）、每次接替与撤回登记、
    链尾标识与当前可使用标识。

    报告不存在返回 None。只读，不改写任何报告与快照。撤回后链尾不变，
    但不再有可使用的现行报告（usable_report_id 为 None）。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM reports WHERE id = %s", (report_id,))
        if cur.fetchone() is None:
            return None
        cur.execute(
            _CHAIN_MEMBERS_CTE + """
            SELECT r.* FROM reports r JOIN members m ON m.id = r.id ORDER BY r.id
            """,
            (report_id,),
        )
        raw_reports = cur.fetchall()
        cur.execute(
            _CHAIN_MEMBERS_CTE + """
            SELECT s.* FROM report_supersessions s
            JOIN members m ON m.id = s.old_report_id
            ORDER BY s.id
            """,
            (report_id,),
        )
        supersessions = cur.fetchall()
        cur.execute(
            _CHAIN_MEMBERS_CTE + """
            SELECT w.* FROM report_withdrawals w
            JOIN members m ON m.id = w.report_id
            ORDER BY w.id
            """,
            (report_id,),
        )
        withdrawals = cur.fetchall()
    superseded = {s["old_report_id"] for s in supersessions}
    # 链尾 = 未被接替的报告（唯一索引保证链不分叉，链尾恰一个）
    tip = next(r for r in raw_reports if r["id"] not in superseded)
    tip_withdrawal = next(
        (w for w in withdrawals if w["report_id"] == tip["id"]), None)
    # 各报告的链尾/可使用标识单独返回（区分“链尾”与“当前可使用”）；
    # reports 行保持原始字段集合，历史内容不改
    report_flags = {
        r["id"]: {
            "tip_report_id": tip["id"],
            "is_chain_tip": r["id"] == tip["id"],
            "is_usable": r["id"] == tip["id"] and tip_withdrawal is None,
        }
        for r in raw_reports
    }
    return {"reports": raw_reports, "report_flags": report_flags,
            "supersessions": supersessions,
            "withdrawals": withdrawals,
            "tip_report_id": tip["id"],
            # 兼容既有调用：current_report_id 即链尾标识（撤回后仍指向链尾，
            # 是否“当前可使用”请看 usable_report_id）
            "current_report_id": tip["id"],
            # 链尾被撤回后，该链不再有可使用的现行报告
            "usable_report_id": None if tip_withdrawal else tip["id"]}


# ---- 报告列表/详情（含链尾与当前可使用标识、撤回登记） ----------------------------

# 为每份报告附带：is_chain_tip（链尾标识）、is_withdrawn/is_usable（当前可使用标识）。
# 撤回只追加撤回登记，不改写报告行与快照；撤回的链尾仍为链尾，但不再可使用。
_REPORTS_WITH_FLAGS_CTE = """
    WITH RECURSIVE chain(orig_id, id) AS (
        SELECT r.id, r.id
        FROM reports r
        UNION
        SELECT c.orig_id,
               CASE WHEN s.old_report_id = c.id THEN s.new_report_id
                    ELSE s.old_report_id END
        FROM chain c
        JOIN report_supersessions s
          ON c.id = s.old_report_id OR c.id = s.new_report_id
    ),
    tip AS (
        SELECT c.orig_id, c.id AS tip_report_id
        FROM chain c
        WHERE NOT EXISTS (SELECT 1 FROM report_supersessions s2
                          WHERE s2.old_report_id = c.id)
    )
    SELECT r.*,
           (r.id = t.tip_report_id) AS is_chain_tip,
           t.tip_report_id,
           (wo.id IS NOT NULL) AS is_withdrawn,
           wo.id AS withdrawal_id, wo.withdrawal_no,
           wo.reason AS withdrawal_reason, wo.created_at AS withdrawn_at,
           wt.withdrawal_no AS tip_withdrawal_no,
           (r.id = t.tip_report_id AND wt.id IS NULL) AS is_usable
    FROM reports r
    JOIN tip t ON t.orig_id = r.id
    LEFT JOIN report_withdrawals wo ON wo.report_id = r.id
    LEFT JOIN report_withdrawals wt ON wt.report_id = t.tip_report_id
"""


def list_reports_flags(conn, instrument: str | None) -> list[dict[str, Any]]:
    """全部报告（含已撤回/已接替）及链尾、可使用标识与链尾撤回登记。

    原有报告内容（含快照）原样返回，仅追加链状态字段。
    """
    sql = _REPORTS_WITH_FLAGS_CTE
    params: list[Any] = []
    if instrument:
        sql += " WHERE r.instrument = %s"
        params.append(instrument)
    sql += " ORDER BY r.id"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def get_report_flags(conn, report_id: int) -> dict[str, Any] | None:
    """单份报告（含已撤回）及链尾、可使用标识与链尾撤回登记。"""
    with conn.cursor() as cur:
        cur.execute(
            _REPORTS_WITH_FLAGS_CTE + " WHERE r.id = %s",
            (report_id,),
        )
        return cur.fetchone()


# ---- 漂移重算 ------------------------------------------------------------------
def _drift_for_instrument(conn, instrument: str) -> list[dict[str, Any]]:
    return _drift(conn, instrument=instrument)


def compute_drift(conn, instrument: str | None = None) -> list[dict[str, Any]]:
    return _drift(conn, instrument=instrument)


def _drift(conn, instrument: str | None) -> list[dict[str, Any]]:
    """对已发布报告按当前校准重算；仅返回结论与快照不同的报告。

    快照本身绝不改写。判定不加锁（只读重算）。
    """
    with conn.cursor() as cur:
        if instrument:
            cur.execute(
                "SELECT * FROM reports WHERE released = TRUE AND instrument = %s "
                "ORDER BY id",
                (instrument,),
            )
        else:
            cur.execute("SELECT * FROM reports WHERE released = TRUE ORDER BY id")
        reports = cur.fetchall()

    drifted: list[dict[str, Any]] = []
    for report in reports:
        now_decision = determine(conn, report["instrument"], report["tested_at"],
                                 lock=False)
        snap = report["snapshot"]
        snap_basis = (snap or {}).get("basis_calibration") or {}

        if now_decision["releasable"]:
            now_basis = now_decision["calibration"]
            if now_basis["id"] == snap_basis.get("id"):
                continue  # 结论与依据版本均未变化
            change = "NOW_COVERED_BY_OTHER_VERSION" if snap.get("released") else "NOW_RELEASEABLE"
            drifted.append({
                "report_id": report["id"],
                "reference": report["reference"],
                "instrument": report["instrument"],
                "tested_at": report["tested_at"].isoformat(),
                "change": change,
                "snapshot": snap,
                "current_decision": {
                    "released": True,
                    "reason_code": REASON_PASSED,
                    "basis_calibration": _cal_brief(now_basis),
                },
            })
        else:
            drifted.append({
                "report_id": report["id"],
                "reference": report["reference"],
                "instrument": report["instrument"],
                "tested_at": report["tested_at"].isoformat(),
                "change": "NOW_BLOCKED",
                "snapshot": snap,
                "current_decision": {
                    "released": False,
                    "reason_code": now_decision["reason_code"],
                    "reason_message": now_decision["reason_message"],
                    "basis_calibration": (
                        _cal_brief(now_decision["calibration"])
                        if now_decision["calibration"] else None
                    ),
                },
            })
    return drifted


# ---- 禁用区间：登记 / 撤销 / 查询 ----------------------------------------------

def register_disable(conn, payload) -> dict[str, Any]:
    """登记禁用区间。无效区间 -> 422；重复事件号/区间重叠 -> 409。"""
    if payload.disabled_to is not None and payload.disabled_to <= payload.disabled_from:
        raise blocked("INVALID_INTERVAL",
                      "禁用区间必须满足 disabled_from < disabled_to（左闭右开）")
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                INSERT INTO instrument_disable_events
                    (event_no, instrument, disabled_from, disabled_to, reason)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (payload.event_no, payload.instrument, payload.disabled_from,
                 payload.disabled_to, payload.reason),
            )
        except UniqueViolation:
            raise conflict("DISABLE_EVENT_EXISTS",
                           f"禁用事件号已存在（不可重复登记，含已撤销事件）: {payload.event_no}",
                           event_no=payload.event_no)
        except ExclusionViolation:
            raise conflict("DISABLE_INTERVAL_OVERLAP",
                           "与该仪器另一条未撤销禁用区间重叠（边界相接允许）",
                           instrument=payload.instrument,
                           event_no=payload.event_no)
        return cur.fetchone()


def revoke_disable(conn, event_no: str, payload) -> dict[str, Any]:
    """按事件号撤销禁用；未知事件 -> 404，已撤销 -> 409。记录保留。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM instrument_disable_events WHERE event_no = %s FOR UPDATE",
            (event_no,),
        )
        row = cur.fetchone()
        if row is None:
            raise not_found("禁用事件", event_no)
        if row["revoked"]:
            raise conflict("DISABLE_ALREADY_REVOKED",
                           f"禁用事件已撤销，不能重复撤销: {event_no}",
                           event_no=event_no,
                           revoked_at=row["revoked_at"].isoformat())
        cur.execute(
            """
            UPDATE instrument_disable_events
            SET revoked = TRUE, revoked_at = now(), revoke_reason = %s
            WHERE id = %s
            RETURNING *
            """,
            (payload.reason, row["id"]),
        )
        return cur.fetchone()


def get_disable(conn, event_no: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM instrument_disable_events WHERE event_no = %s",
            (event_no,),
        )
        return cur.fetchone()


def list_disables(conn, instrument: str | None, include_revoked: bool) -> list[dict[str, Any]]:
    sql = "SELECT * FROM instrument_disable_events"
    where, params = [], []
    if instrument:
        where.append("instrument = %s")
        params.append(instrument)
    if not include_revoked:
        where.append("revoked = FALSE")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY instrument, disabled_from, id"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def affected_reports(conn, instrument: str | None) -> list[dict[str, Any]]:
    """按当前禁用状态，列出检测时间命中未撤销禁用区间的已发布报告及禁用依据。

    追溯登记 / 撤销禁用后结果随之变化；已发布报告与其快照不改写。
    只读重算，不加锁（运行在 SERIALIZABLE 只读事务中）。
    """
    sql = """
        SELECT r.id AS report_id, r.reference, r.instrument, r.tested_at,
               r.basis_calibration_id, r.snapshot, r.created_at AS report_created_at,
               d.event_no, d.reason, d.disabled_from, d.disabled_to
        FROM reports r
        JOIN instrument_disable_events d
          ON d.instrument = r.instrument
         AND d.revoked = FALSE
         AND d.disabled_from <= r.tested_at
         AND (d.disabled_to IS NULL OR r.tested_at < d.disabled_to)
    """
    params: list[Any] = []
    if instrument:
        sql += " WHERE r.instrument = %s"
        params.append(instrument)
    sql += " ORDER BY r.instrument, r.tested_at, r.id"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        items.append({
            "report_id": row["report_id"],
            "reference": row["reference"],
            "instrument": row["instrument"],
            "tested_at": row["tested_at"].isoformat(),
            "basis_calibration_id": row["basis_calibration_id"],
            "snapshot": row["snapshot"],
            "disable": {
                "event_no": row["event_no"],
                "instrument": row["instrument"],
                "disabled_from": row["disabled_from"].isoformat(),
                "disabled_to": row["disabled_to"].isoformat() if row["disabled_to"] else None,
                "reason": row["reason"],
            },
        })
    return items
