"""报告接替（复测）功能的正确性与并发验证（直连数据库，不走 HTTP）。

覆盖：
1. 基础接替：放行规则沿用（禁用优先 + 当前通过校准覆盖），原子发布新报告+接替关系，
   旧报告与快照保留，当前有效报告切换为新版
2. 仅未接替报告可被接替：已接替 -> 409 REPORT_ALREADY_SUPERSEDED 且返回现行报告标识；
   不存在 -> 404；空接替原因 -> 参数校验拒绝
3. 阻断（禁用/未通过/无校准）或编号冲突（含沿用旧编号）-> 旧报告仍有效，不留接替记录
4. 沿革查询：链内任一报告 ID 可查完整沿革、每次接替原因、当前有效报告；链不分叉不成环
5. 并发：同一旧报告并发接替至多成功一次（同仪器与跨仪器两种赛跑），败方拿到现行报告标识
6. 兼容：首次提交/单报告查询的响应结构不变，旧版报告快照永不改写
"""
import os

# 并发赛跑线程数超过默认连接池上限，测试进程内调大。
# 必须在导入 app.config 之前设置。
os.environ.setdefault("DB_POOL_MAX_SIZE", "30")
os.environ.setdefault("DB_POOL_MIN_SIZE", "5")

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import HTTPException
from pydantic import ValidationError

import app.db as db
from app.models import (
    CalibrationCreate,
    DisableCreate,
    ReportSubmit,
    SupersedeSubmit,
)
from app.repository import (
    chain_tip_id,
    create_calibration,
    publish_report,
    register_disable,
    report_lineage,
    supersede_report,
)
from app.transactions import run_tx

failures = []

db.open_pool()


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), "-", name, detail)
    if not cond:
        failures.append(name)


def reset():
    with db.pool.connection() as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    db.init_db()


def T(y, mo=1, d=1, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def http_call(fn, *args):
    try:
        result = run_tx(lambda conn: fn(conn, *args))
        return 200, result
    except HTTPException as e:
        return e.status_code, e.detail


def locked_publish(payload: ReportSubmit):
    """与生产路由一致：发布按仪器咨询锁串行。"""
    try:
        result = run_tx(lambda conn: publish_report(conn, payload),
                        advisory_lock=db.instrument_lock_key(payload.instrument))
        return 201, result
    except HTTPException as e:
        return e.status_code, e.detail


def locked_supersede(report_id: int, payload: SupersedeSubmit):
    """与生产路由一致：按复测仪器咨询锁串行；败方补现行报告标识。"""
    try:
        result = run_tx(lambda conn: supersede_report(conn, report_id, payload),
                        advisory_lock=db.instrument_lock_key(payload.instrument))
        return 201, result
    except HTTPException as e:
        detail = e.detail
        if (e.status_code == 409 and isinstance(detail, dict)
                and detail.get("code") == "REPORT_ALREADY_SUPERSEDED"):
            detail["current_report_id"] = run_tx(
                lambda conn: chain_tip_id(conn, report_id), readonly=True)
        return e.status_code, detail


def locked_register(payload: DisableCreate):
    try:
        result = run_tx(lambda conn: register_disable(conn, payload),
                        advisory_lock=db.instrument_lock_key(payload.instrument))
        return 200, result
    except HTTPException as e:
        return e.status_code, e.detail


def passed_calibration(instrument, frm=T(2025, 1, 1), to=T(2030, 1, 1)):
    status, row = http_call(create_calibration, CalibrationCreate(
        instrument=instrument, valid_from=frm, valid_to=to, passed=True))
    assert status == 200, row
    return row


def lineage(report_id):
    return run_tx(lambda conn: report_lineage(conn, report_id), readonly=True)


def tip(report_id):
    return run_tx(lambda conn: chain_tip_id(conn, report_id), readonly=True)


# ---------- 1. 基础接替流程 ----------
reset()
passed_calibration("S1")
status, rep1 = locked_publish(ReportSubmit(
    instrument="S1", tested_at=T(2026, 3, 15), reference="S1-R1"))
check("首次提交放行", status == 201 and rep1["released"] is True, str(status))
check("首次提交响应结构保持兼容",
      set(rep1.keys()) == {"released", "reason_code", "basis_calibration",
                           "report_id", "reference", "snapshot"},
      str(sorted(rep1.keys())))
old_id = rep1["report_id"]

status, rep2 = locked_supersede(old_id, SupersedeSubmit(
    instrument="S1", tested_at=T(2026, 4, 1),
    reference="S1-R2", reason="初测样品受污染，复测"))
check("接替成功 -> 201 并发布新报告",
      status == 201 and rep2["released"] is True and rep2["report_id"] != old_id, str(rep2))
new_id = rep2["report_id"]
check("接替响应含接替关系与双方标识",
      rep2["superseded_report_id"] == old_id
      and rep2["current_report_id"] == new_id
      and rep2["supersession"]["old_report_id"] == old_id
      and rep2["supersession"]["new_report_id"] == new_id
      and rep2["supersession"]["reason"] == "初测样品受污染，复测",
      str(rep2.get("supersession")))
check("新报告快照为独立发布快照（检测时间为复测时间）",
      rep2["snapshot"]["tested_at"].startswith("2026-04-01")
      and rep2["snapshot"]["released"] is True, str(rep2["snapshot"]))

lin_old = lineage(old_id)
lin_new = lineage(new_id)
check("从旧报告查沿革：全链两报告，当前有效为新版",
      [r["id"] for r in lin_old["reports"]] == [old_id, new_id]
      and lin_old["current_report_id"] == new_id, str(lin_old["current_report_id"]))
check("从新报告查沿革：同一链条（任一链内 ID 均可查）",
      [r["id"] for r in lin_new["reports"]] == [old_id, new_id]
      and lin_new["current_report_id"] == new_id)
check("沿革含每次接替原因",
      len(lin_old["supersessions"]) == 1
      and lin_old["supersessions"][0]["reason"] == "初测样品受污染，复测"
      and lin_old["supersessions"][0]["old_report_id"] == old_id
      and lin_old["supersessions"][0]["new_report_id"] == new_id,
      str(lin_old["supersessions"]))
check("chain_tip_id 与沿革一致", tip(old_id) == new_id and tip(new_id) == new_id)

with db.pool.connection() as c:
    old_row = c.execute("SELECT * FROM reports WHERE id = %s", (old_id,)).fetchone()
check("旧报告及发布快照永久保留（released 与快照原样）",
      old_row["released"] is True
      and old_row["snapshot"]["tested_at"].startswith("2026-03-15")
      and old_row["snapshot"]["reason_code"] == "PASSED_CALIBRATION_COVERAGE",
      str(old_row["snapshot"]))

# ---------- 2. 接替前置校验 ----------
status, detail = locked_supersede(999999, SupersedeSubmit(
    instrument="S1", tested_at=T(2026, 4, 2), reason="x"))
check("接替不存在的报告 -> 404 NOT_FOUND",
      status == 404 and detail["code"] == "NOT_FOUND", str(detail))

status, detail = locked_supersede(old_id, SupersedeSubmit(
    instrument="S1", tested_at=T(2026, 4, 3),
    reference="S1-R3", reason="再次复测"))
check("旧报告已被接替 -> 409 REPORT_ALREADY_SUPERSEDED 且返回现行报告标识",
      status == 409 and detail["code"] == "REPORT_ALREADY_SUPERSEDED"
      and detail["current_report_id"] == new_id
      and detail["superseded_by"] == new_id, str(detail))

try:
    SupersedeSubmit(instrument="S1", tested_at=T(2026, 4, 3), reason="")
    check("空接替原因 -> 参数校验拒绝", False)
except ValidationError:
    check("空接替原因 -> 参数校验拒绝", True)
try:
    SupersedeSubmit(instrument="S1", tested_at=T(2026, 4, 3), reason="   ")
    check("纯空白接替原因 -> 参数校验拒绝", False)
except ValidationError:
    check("纯空白接替原因 -> 参数校验拒绝", True)

# ---------- 3. 链增长与多代沿革 ----------
status, rep3 = locked_supersede(new_id, SupersedeSubmit(
    instrument="S1", tested_at=T(2026, 5, 1),
    reference="S1-R3", reason="客户异议，二次复测"))
check("链尾可继续接替（第二代）", status == 201, str(rep3))
third_id = rep3["report_id"]
lin = lineage(old_id)
check("三代链：从首版查全链，当前有效为第三代",
      [r["id"] for r in lin["reports"]] == [old_id, new_id, third_id]
      and lin["current_report_id"] == third_id, str(lin["current_report_id"]))
check("三代链：两次接替原因按序保留",
      [s["reason"] for s in lin["supersessions"]] == ["初测样品受污染，复测", "客户异议，二次复测"],
      str([s["reason"] for s in lin["supersessions"]]))
status, detail = locked_supersede(new_id, SupersedeSubmit(
    instrument="S1", tested_at=T(2026, 5, 2), reason="中间版本再复测"))
check("中间版本（非链尾）不可再接替 -> 409 且现行为第三代",
      status == 409 and detail["code"] == "REPORT_ALREADY_SUPERSEDED"
      and detail["current_report_id"] == third_id, str(detail))
with db.pool.connection() as c:
    n_sup = c.execute("SELECT count(*) n FROM report_supersessions").fetchone()["n"]
    n_fork = c.execute(
        "SELECT count(*) n FROM (SELECT old_report_id FROM report_supersessions "
        "GROUP BY old_report_id HAVING count(*) > 1) t").fetchone()["n"]
check("链不分叉：每个旧报告至多一条接替边", n_sup == 2 and n_fork == 0, f"sup={n_sup}")

# ---------- 4. 阻断与编号冲突：旧报告仍有效，不留接替记录 ----------
reset()
passed_calibration("S2")
status, rep = locked_publish(ReportSubmit(
    instrument="S2", tested_at=T(2026, 3, 15), reference="S2-R1"))
rid = rep["report_id"]

# 4a. 复测时点命中禁用区间 -> 422 INSTRUMENT_DISABLED
locked_register(DisableCreate(
    event_no="S2-DIS", instrument="S2",
    disabled_from=T(2026, 4, 1), disabled_to=T(2026, 5, 1), reason="泵故障"))
status, detail = locked_supersede(rid, SupersedeSubmit(
    instrument="S2", tested_at=T(2026, 4, 15),
    reference="S2-R2", reason="故障期复测"))
check("复测命中禁用 -> 422 INSTRUMENT_DISABLED（禁用优先，含禁用依据）",
      status == 422 and detail["code"] == "INSTRUMENT_DISABLED"
      and detail["disable"]["event_no"] == "S2-DIS", str(detail))

# 4b. 编号冲突：沿用旧报告编号 -> 409（旧编号不得转移或复用）
status, detail = locked_supersede(rid, SupersedeSubmit(
    instrument="S2", tested_at=T(2026, 3, 20),
    reference="S2-R1", reason="沿用旧编号"))
check("新报告沿用旧编号 -> 409 REPORT_REFERENCE_EXISTS",
      status == 409 and detail["code"] == "REPORT_REFERENCE_EXISTS", str(detail))

# 4c. 编号冲突：占用其他报告编号 -> 409
status, rep_other = locked_publish(ReportSubmit(
    instrument="S2", tested_at=T(2026, 3, 16), reference="S2-OTHER"))
status, detail = locked_supersede(rid, SupersedeSubmit(
    instrument="S2", tested_at=T(2026, 3, 20),
    reference="S2-OTHER", reason="占用他人编号"))
check("新报告占用他人编号 -> 409 REPORT_REFERENCE_EXISTS",
      status == 409 and detail["code"] == "REPORT_REFERENCE_EXISTS", str(detail))

with db.pool.connection() as c:
    n_sup = c.execute(
        "SELECT count(*) n FROM report_supersessions WHERE old_report_id = %s",
        (rid,)).fetchone()["n"]
    n_rep = c.execute("SELECT count(*) n FROM reports").fetchone()["n"]
check("阻断/编号冲突后：无接替记录、无新报告落库",
      n_sup == 0 and n_rep == 2, f"sup={n_sup} reports={n_rep}")
check("阻断/编号冲突后：旧报告仍为当前有效报告", tip(rid) == rid)

# 4d. 禁用期外（未命中区间）复测可正常接替 —— 旧报告此前未被阻断影响
status, rep2 = locked_supersede(rid, SupersedeSubmit(
    instrument="S2", tested_at=T(2026, 5, 2),
    reference="S2-R2", reason="修复后复测"))
check("阻断失败后可再次接替并最终成功", status == 201 and tip(rid) == rep2["report_id"],
      str(status))

# 4e. 校准未通过 / 无校准 -> 422，同样不留痕迹
reset()
status, cal = http_call(create_calibration, CalibrationCreate(
    instrument="S3", valid_from=T(2025, 1, 1), valid_to=T(2030, 1, 1), passed=False))
status, rep = locked_publish(ReportSubmit(
    instrument="S3", tested_at=T(2026, 3, 15)))
check("校准未通过时首次提交被阻断（不落库）", status == 422
      and rep["code"] == "CALIBRATION_NOT_PASSED", str(status))
# 先造一份可放行的报告，再尝试在未通过校准的仪器上复测接替
passed_calibration("S4")
status, rep_ok = locked_publish(ReportSubmit(
    instrument="S4", tested_at=T(2026, 3, 15), reference="S4-R1"))
rid4 = rep_ok["report_id"]
status, detail = locked_supersede(rid4, SupersedeSubmit(
    instrument="S3", tested_at=T(2026, 3, 20), reason="换到未通过校准的仪器复测"))
check("复测仪器校准未通过 -> 422 CALIBRATION_NOT_PASSED",
      status == 422 and detail["code"] == "CALIBRATION_NOT_PASSED", str(detail))
status, detail = locked_supersede(rid4, SupersedeSubmit(
    instrument="S4-GHOST", tested_at=T(2026, 3, 20), reason="换到无校准的仪器复测"))
check("复测仪器无校准 -> 422 NO_CALIBRATION",
      status == 422 and detail["code"] == "NO_CALIBRATION", str(detail))
check("跨仪器阻断后旧报告仍有效", tip(rid4) == rid4)

# 4f. 换仪器复测成功：新报告仪器为复测仪器
passed_calibration("S5")
status, rep5 = locked_supersede(rid4, SupersedeSubmit(
    instrument="S5", tested_at=T(2026, 3, 25),
    reference="S4-R2", reason="原仪器停用，换机复测"))
check("换仪器复测接替成功，新报告仪器为复测仪器",
      status == 201 and rep5["snapshot"]["instrument"] == "S5", str(rep5.get("snapshot")))
lin = lineage(rid4)
check("换仪器复测后沿革完整（两代，仪器不同）",
      [r["instrument"] for r in lin["reports"]] == ["S4", "S5"]
      and lin["current_report_id"] == rep5["report_id"], str(lin["current_report_id"]))

# ---------- 5. 并发接替：同一旧报告至多成功一次 ----------
reset()
passed_calibration("RACE1")
status, rep = locked_publish(ReportSubmit(
    instrument="RACE1", tested_at=T(2026, 3, 15), reference="RACE1-R1"))
rid = rep["report_id"]
N = 8
barrier = threading.Barrier(N)


def race_supersede(i):
    barrier.wait()
    code, payload = locked_supersede(rid, SupersedeSubmit(
        instrument="RACE1", tested_at=T(2026, 4, 1),
        reference=f"RACE1-NEW-{i}", reason=f"并发复测 {i}"))
    if code == 201:
        return ("ok", payload["report_id"])
    return (payload["code"], payload.get("current_report_id"))


with ThreadPoolExecutor(N) as ex:
    results = list(ex.map(race_supersede, range(N)))
winners = [r[1] for r in results if r[0] == "ok"]
losers = [r for r in results if r[0] != "ok"]
check("同仪器并发接替：恰好 1 个成功",
      len(winners) == 1, f"winners={winners} results={results}")
check("同仪器并发接替：败方全部 409 REPORT_ALREADY_SUPERSEDED 且拿到现行报告标识",
      all(r[0] == "REPORT_ALREADY_SUPERSEDED" and r[1] == winners[0] for r in losers),
      str(losers))
with db.pool.connection() as c:
    n_sup = c.execute(
        "SELECT count(*) n FROM report_supersessions WHERE old_report_id = %s",
        (rid,)).fetchone()["n"]
    n_rep = c.execute(
        "SELECT count(*) n FROM reports WHERE instrument = 'RACE1'").fetchone()["n"]
check("并发后链不分叉：接替记录恰 1 条，报告恰 2 份（无残留新报告）",
      n_sup == 1 and n_rep == 2, f"sup={n_sup} reports={n_rep}")
check("并发后链尾即胜者新报告", tip(rid) == winners[0])

# 跨仪器并发接替（不同咨询锁，真实竞速唯一索引兜底路径）
reset()
passed_calibration("RACE2A")
passed_calibration("RACE2B")
status, rep = locked_publish(ReportSubmit(
    instrument="RACE2A", tested_at=T(2026, 3, 15), reference="RACE2-R1"))
rid = rep["report_id"]
barrier = threading.Barrier(2)


def race_supersede_cross(instrument, ref):
    barrier.wait()
    code, payload = locked_supersede(rid, SupersedeSubmit(
        instrument=instrument, tested_at=T(2026, 4, 1),
        reference=ref, reason=f"跨仪器复测 {instrument}"))
    if code == 201:
        return ("ok", payload["report_id"])
    return (payload["code"], payload.get("current_report_id"))


with ThreadPoolExecutor(2) as ex:
    f1 = ex.submit(race_supersede_cross, "RACE2A", "RACE2-NEW-A")
    f2 = ex.submit(race_supersede_cross, "RACE2B", "RACE2-NEW-B")
    cross = [f1.result(), f2.result()]
winners = [r[1] for r in cross if r[0] == "ok"]
losers = [r for r in cross if r[0] != "ok"]
check("跨仪器并发接替：恰好 1 个成功",
      len(winners) == 1, f"results={cross}")
check("跨仪器并发接替：败方 409 且拿到现行报告标识",
      len(losers) == 1 and losers[0][0] == "REPORT_ALREADY_SUPERSEDED"
      and losers[0][1] == winners[0], str(losers))
with db.pool.connection() as c:
    n_sup = c.execute(
        "SELECT count(*) n FROM report_supersessions WHERE old_report_id = %s",
        (rid,)).fetchone()["n"]
    n_rep = c.execute("SELECT count(*) n FROM reports").fetchone()["n"]
check("跨仪器并发后：接替记录恰 1 条，报告恰 2 份（败方整体回滚）",
      n_sup == 1 and n_rep == 2, f"sup={n_sup} reports={n_rep}")

# ---------- 6. 快照不可变与兼容性 ----------
reset()
passed_calibration("S6")
status, rep1 = locked_publish(ReportSubmit(
    instrument="S6", tested_at=T(2026, 3, 15), reference="S6-R1"))
snap_before = rep1["snapshot"]
status, rep2 = locked_supersede(rep1["report_id"], SupersedeSubmit(
    instrument="S6", tested_at=T(2026, 4, 1),
    reference="S6-R2", reason="例行复测"))
check("接替成功", status == 201, str(status))
with db.pool.connection() as c:
    row = c.execute("SELECT snapshot, released FROM reports WHERE id = %s",
                    (rep1["report_id"],)).fetchone()
check("接替后旧报告快照逐字节不变",
      row["snapshot"] == snap_before and row["released"] is True, str(row["snapshot"]))
with db.pool.connection() as c:
    rows = c.execute("SELECT id, reference FROM reports ORDER BY id").fetchall()
check("报告列表含全部版本（旧版保留可查）",
      [r["reference"] for r in rows] == ["S6-R1", "S6-R2"], str(rows))
lin = lineage(rep2["report_id"])
check("单报告查询兼容：沿革内报告含原有字段",
      all(set(r.keys()) == {"id", "reference", "instrument", "tested_at", "released",
                            "basis_calibration_id", "snapshot", "created_at"}
          for r in lin["reports"]), str(lin["reports"][0].keys()))
check("无接替关系的报告：沿革仅自身，当前有效即自身",
      lineage(rep2["report_id"])["current_report_id"] == rep2["report_id"])
check("沿革查询不存在的报告 -> None（路由映射 404）", lineage(999999) is None)

print()
if failures:
    print(f"{len(failures)} 项失败:", failures)
    sys.exit(1)
print("全部报告接替验证通过")
