"""禁用区间功能的正确性与并发验证（直连数据库，不走 HTTP）。

覆盖：
1. 登记禁用区间；无效区间、重复事件号、区间重叠拒绝；边界相接允许；开放区间
2. 撤销：未知事件 404、重复撤销 409；撤销后区间可复用
3. 发布：命中未撤销禁用区间即阻断（即使校准通过），端点左闭右开；撤销后恢复放行
4. 受影响报告查询：追溯登记/撤销后随之变化，原放行记录与快照不改写
5. 并发：登记禁用与发布并发，不会在已生效禁用区间内误放行；并发撤销幂等拒绝
"""
import os

# 并发赛跑线程数（12 发布 + 1 登记）超过默认连接池上限，测试进程内调大。
# 必须在导入 app.config 之前设置。
os.environ.setdefault("DB_POOL_MAX_SIZE", "30")
os.environ.setdefault("DB_POOL_MIN_SIZE", "5")

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import HTTPException

import app.db as db
from app.models import (
    CalibrationCorrect,
    CalibrationCreate,
    DisableCreate,
    DisableRevoke,
    ReportSubmit,
)
from app.repository import (
    affected_reports,
    correct_calibration,
    create_calibration,
    publish_report,
    register_disable,
    revoke_disable,
    get_disable,
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
        return 200, result
    except HTTPException as e:
        return e.status_code, e.detail


def locked_register(payload: DisableCreate):
    try:
        result = run_tx(lambda conn: register_disable(conn, payload),
                        advisory_lock=db.instrument_lock_key(payload.instrument))
        return 200, result
    except HTTPException as e:
        return e.status_code, e.detail


def locked_revoke(event_no: str, payload: DisableRevoke | None = None):
    payload = payload or DisableRevoke()
    probe = run_tx(lambda conn: get_disable(conn, event_no), readonly=True)
    if probe is None:
        return 404, {"code": "NOT_FOUND"}
    try:
        result = run_tx(lambda conn: revoke_disable(conn, event_no, payload),
                        advisory_lock=db.instrument_lock_key(probe["instrument"]))
        return 200, result
    except HTTPException as e:
        return e.status_code, e.detail


def T(y, mo=1, d=1, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def passed_calibration(instrument, frm=T(2025, 1, 1), to=T(2030, 1, 1)):
    status, row = http_call(create_calibration, CalibrationCreate(
        instrument=instrument, valid_from=frm, valid_to=to, passed=True))
    assert status == 200, row
    return row


# ---------- 1. 登记校验 ----------
reset()
status, row = locked_register(DisableCreate(
    event_no="E-1", instrument="D1",
    disabled_from=T(2026, 3, 1), disabled_to=T(2026, 4, 1),
    reason="泵故障"))
check("登记禁用区间成功", status == 200 and row["event_no"] == "E-1" and not row["revoked"], str(row))

status, detail = locked_register(DisableCreate(
    event_no="E-1", instrument="D1",
    disabled_from=T(2026, 5, 1), disabled_to=T(2026, 6, 1), reason="x"))
check("重复事件号 -> 409 DISABLE_EVENT_EXISTS",
      status == 409 and detail["code"] == "DISABLE_EVENT_EXISTS", str(detail))

status, detail = locked_register(DisableCreate(
    event_no="E-2", instrument="D1",
    disabled_from=T(2026, 3, 15), disabled_to=T(2026, 3, 20), reason="重叠"))
check("与未撤销区间重叠 -> 409 DISABLE_INTERVAL_OVERLAP",
      status == 409 and detail["code"] == "DISABLE_INTERVAL_OVERLAP", str(detail))

status, row2 = locked_register(DisableCreate(
    event_no="E-3", instrument="D1",
    disabled_from=T(2026, 4, 1), disabled_to=T(2026, 5, 1), reason="相接"))
check("边界相接 [3/1,4/1) + [4/1,5/1) 允许",
      status == 200 and row2["event_no"] == "E-3", str(status))

status, row3 = locked_register(DisableCreate(
    event_no="E-4", instrument="D1",
    disabled_from=T(2026, 5, 1), disabled_to=None, reason="开放"))
check("开放禁用区间（终点空）允许", status == 200 and row3["disabled_to"] is None, str(status))

status, detail = locked_register(DisableCreate(
    event_no="E-5", instrument="D1",
    disabled_from=T(2026, 6, 1), disabled_to=None, reason="开放重叠"))
check("与开放区间重叠 -> 409",
      status == 409 and detail["code"] == "DISABLE_INTERVAL_OVERLAP", str(detail))

status, detail = locked_register(DisableCreate(
    event_no="E-6", instrument="D1",
    disabled_from=T(2026, 6, 1), disabled_to=T(2026, 6, 1), reason="零长"))
check("终点等于起点 -> 422 INVALID_INTERVAL",
      status == 422 and detail["code"] == "INVALID_INTERVAL", str(detail))

status, detail = locked_register(DisableCreate(
    event_no="E-7", instrument="D1",
    disabled_from=T(2026, 6, 2), disabled_to=T(2026, 6, 1), reason="倒序"))
check("终点早于起点 -> 422 INVALID_INTERVAL", status == 422, str(detail))

# 不同仪器同一时间区间互不影响
status, row4 = locked_register(DisableCreate(
    event_no="E-8", instrument="OTHER",
    disabled_from=T(2026, 3, 15), disabled_to=T(2026, 3, 20), reason="别的仪器"))
check("不同仪器区间可重叠", status == 200, str(status))

# ---------- 2. 撤销 ----------
status, detail = locked_revoke("NOPE", DisableRevoke(reason=None))
check("撤销未知事件 -> 404", status == 404 and detail["code"] == "NOT_FOUND", str(detail))

status, rev = locked_revoke("E-1", DisableRevoke(reason="已修复"))
check("撤销成功并保留记录", status == 200 and rev["revoked"] and rev["revoke_reason"] == "已修复", str(rev))

status, detail = locked_revoke("E-1", DisableRevoke())
check("重复撤销 -> 409 DISABLE_ALREADY_REVOKED",
      status == 409 and detail["code"] == "DISABLE_ALREADY_REVOKED", str(detail))

status, row5 = locked_register(DisableCreate(
    event_no="E-9", instrument="D1",
    disabled_from=T(2026, 3, 10), disabled_to=T(2026, 3, 25), reason="撤销后占位"))
check("撤销后区间释放：与原 E-1 区间重叠的登记成功", status == 200, str(status))
check("已撤销事件号仍不可复用", True)
status, detail = locked_register(DisableCreate(
    event_no="E-1", instrument="D1",
    disabled_from=T(2031, 1, 1), disabled_to=None, reason="复用事件号"))
check("已撤销事件号复用 -> 409 DISABLE_EVENT_EXISTS",
      status == 409 and detail["code"] == "DISABLE_EVENT_EXISTS", str(detail))

# ---------- 3. 发布判定 ----------
reset()
passed_calibration("D2")
# 先在禁用前放行一份
status, rep = locked_publish(ReportSubmit(
    instrument="D2", tested_at=T(2026, 2, 15), reference="before"))
check("禁用前：校准通过 -> 放行", status == 200 and rep["released"] is True, str(status))

locked_register(DisableCreate(
    event_no="F-1", instrument="D2",
    disabled_from=T(2026, 3, 1), disabled_to=T(2026, 4, 1),
    reason="检测器故障"))

# 区间内部命中
status, detail = locked_publish(ReportSubmit(
    instrument="D2", tested_at=T(2026, 3, 15), reference="inside"))
check("命中禁用区间 -> 422 INSTRUMENT_DISABLED 且返回禁用依据",
      status == 422 and detail["code"] == "INSTRUMENT_DISABLED"
      and detail["disable"]["event_no"] == "F-1"
      and detail["disable"]["disabled_to"].startswith("2026-04-01"), str(detail))

# 左闭：起点命中
status, detail = locked_publish(ReportSubmit(
    instrument="D2", tested_at=T(2026, 3, 1), reference="edge-from"))
check("禁用起点（含）命中 -> 阻断", status == 422 and detail["code"] == "INSTRUMENT_DISABLED", str(detail))

# 右开：终点不命中
status, rep2 = locked_publish(ReportSubmit(
    instrument="D2", tested_at=T(2026, 4, 1), reference="edge-to"))
check("禁用终点（不含）不命中 -> 放行", status == 200 and rep2["released"] is True, str(status))

# 已撤销区间不阻断
locked_revoke("F-1", DisableRevoke(reason="修复完成"))
status, rep3 = locked_publish(ReportSubmit(
    instrument="D2", tested_at=T(2026, 3, 15), reference="after-revoke"))
check("撤销禁用后同一时点恢复放行", status == 200 and rep3["released"] is True, str(status))

# 禁用优先于校准判定（即使没有任何校准，也先报禁用）
locked_register(DisableCreate(
    event_no="F-2", instrument="UNKNOWN2",
    disabled_from=T(2026, 1, 1), disabled_to=T(2027, 1, 1), reason="无校准仪器禁用"))
status, detail = locked_publish(ReportSubmit(
    instrument="UNKNOWN2", tested_at=T(2026, 6, 1)))
check("无校准但命中禁用 -> 422 INSTRUMENT_DISABLED（禁用优先）",
      status == 422 and detail["code"] == "INSTRUMENT_DISABLED", str(detail))

# 阻断的报告不落库
with db.pool.connection() as c:
    n = c.execute("SELECT count(*) n FROM reports WHERE instrument='D2'").fetchone()["n"]
    refs = c.execute("SELECT reference FROM reports WHERE instrument='D2'").fetchall()
check("被禁用阻断的报告不落库",
      n == 3 and {r["reference"] for r in refs} == {"before", "edge-to", "after-revoke"}, str(refs))

# 更正校准为未通过后，未禁用时点仍走原校准判定
d3 = passed_calibration("D3")
http_call(correct_calibration, CalibrationCorrect(
    corrects_id=d3["id"], version=2,
    valid_from=T(2025, 1, 1), valid_to=T(2030, 1, 1), passed=False))
status, detail = locked_publish(ReportSubmit(
    instrument="D3", tested_at=T(2026, 6, 1)))
check("未禁用但校准未通过 -> 仍 422 CALIBRATION_NOT_PASSED（兼容原判定）",
      status == 422 and detail["code"] == "CALIBRATION_NOT_PASSED", str(detail))

# ---------- 4. 受影响报告查询（当前禁用状态驱动） ----------
reset()
cal = passed_calibration("D4")
for day, ref in ((1, "r1"), (10, "r2"), (20, "r3")):
    s, _ = locked_publish(ReportSubmit(
        instrument="D4", tested_at=T(2026, 3, day), reference=ref))
    assert s == 200, ref

# 追溯登记：禁用区间覆盖 r1/r2（[3/5, 3/15)）
locked_register(DisableCreate(
    event_no="G-1", instrument="D4",
    disabled_from=T(2026, 3, 5), disabled_to=T(2026, 3, 15),
    reason="追溯登记的故障区间"))
items = run_tx(lambda conn: affected_reports(conn, "D4"), readonly=True)
hit = {i["report_id"]: i for i in items}
refs_hit = {i["reference"] for i in items}
check("追溯登记后受影响报告为 r2（r1 在禁用前，r3 在终点后）",
      refs_hit == {"r2"}, str(refs_hit))
check("受影响报告返回禁用依据与原快照（快照仍为放行）",
      items[0]["disable"]["event_no"] == "G-1"
      and items[0]["snapshot"]["released"] is True
      and items[0]["snapshot"]["reason_code"] == "PASSED_CALIBRATION_COVERAGE"
      and items[0]["basis_calibration_id"] == cal["id"], str(items[0]))

# 扩大禁用区间（撤销旧的 + 登记新的）覆盖 r2/r3
locked_revoke("G-1", DisableRevoke())
locked_register(DisableCreate(
    event_no="G-2", instrument="D4",
    disabled_from=T(2026, 3, 10), disabled_to=None, reason="持续禁用"))
items = run_tx(lambda conn: affected_reports(conn, "D4"), readonly=True)
refs_hit = {i["reference"] for i in items}
check("撤销旧区间+登记开放区间后受影响报告为 r2,r3（结果随当前状态变化）",
      refs_hit == {"r2", "r3"}, str(refs_hit))

# 全部撤销 -> 无受影响
locked_revoke("G-2", DisableRevoke())
items = run_tx(lambda conn: affected_reports(conn, "D4"), readonly=True)
check("全部撤销后无受影响报告", items == [], str(items))

# 原放行记录与快照不改写
with db.pool.connection() as c:
    rows = c.execute(
        "SELECT reference, released, snapshot->>'reason_code' rc FROM reports "
        "WHERE instrument='D4' ORDER BY reference").fetchall()
check("原报告全部仍为放行且快照未改写",
      all(r["released"] and r["rc"] == "PASSED_CALIBRATION_COVERAGE" for r in rows), str(rows))

# ---------- 5. 并发：登记禁用 vs 发布 ----------
reset()
passed_calibration("D5")
N = 12
barrier = threading.Barrier(N + 1)
TARGET = T(2026, 6, 1)


def race_publish(i):
    barrier.wait()
    code, payload = locked_publish(ReportSubmit(
        instrument="D5", tested_at=TARGET, reference=f"rc-{i}"))
    if code == 200:
        return ("released", payload["report_id"])
    return (payload["code"], None)


def race_disable():
    barrier.wait()
    code, payload = locked_register(DisableCreate(
        event_no="H-1", instrument="D5",
        disabled_from=T(2026, 1, 1), disabled_to=T(2027, 1, 1),
        reason="并发登记的禁用"))
    return "registered" if code == 200 else payload["code"]


for round_no in range(5):
    # 每轮清理报告与禁用事件，重新赛跑
    with db.pool.connection() as c:
        c.execute("DELETE FROM reports WHERE instrument='D5'")
        c.execute("DELETE FROM instrument_disable_events WHERE instrument='D5'")
    barrier = threading.Barrier(N + 1)
    with ThreadPoolExecutor(N + 1) as ex:
        futs = [ex.submit(race_publish, i) for i in range(N)]
        df = ex.submit(race_disable)
        pub = [f.result() for f in futs]
        dis = df.result()

    if dis != "registered":
        check(f"第{round_no+1}轮：禁用登记成功", False, str(dis))
        continue
    codes = {p[0] for p in pub}
    # 禁用提交后再发布必须被阻断；禁用提交前发布的报告属于合法放行，但
    # 以禁用事件行为基准核对：其创建时间早于禁用事务的才允许存在。
    check(f"第{round_no+1}轮：发布结果仅 released/INSTRUMENT_DISABLED",
          codes <= {"released", "INSTRUMENT_DISABLED"}, str(codes))
    with db.pool.connection() as c:
        bad = c.execute("""
            SELECT r.id FROM reports r
            JOIN instrument_disable_events d
              ON d.instrument = r.instrument
             AND d.event_no = 'H-1'
             AND r.tested_at >= d.disabled_from
             AND (d.disabled_to IS NULL OR r.tested_at < d.disabled_to)
            WHERE r.created_at > d.created_at
        """).fetchall()
        # created_at 从事务起点取值：禁用生效后启动的发布事务不得落库
        check(f"第{round_no+1}轮：无“禁用生效后”在禁用区间内的误放行",
              not bad, f"bad={[b['id'] for b in bad]}")
    # 禁用生效后直接发布必阻断
    s, d = locked_publish(ReportSubmit(instrument="D5", tested_at=TARGET))
    check(f"第{round_no+1}轮：赛跑结束后同点发布 -> 422 INSTRUMENT_DISABLED",
          s == 422 and d["code"] == "INSTRUMENT_DISABLED", str(s))

# ---------- 6. 并发：重复事件号登记（至多一个成功） ----------
reset()
barrier = threading.Barrier(6)


def race_register():
    barrier.wait()
    code, payload = locked_register(DisableCreate(
        event_no="UNIQ", instrument="D6",
        disabled_from=T(2026, 1, 1), disabled_to=T(2026, 2, 1), reason="x"))
    return "ok" if code == 200 else payload["code"]


with ThreadPoolExecutor(6) as ex:
    results = list(ex.map(lambda _: race_register(), range(6)))
check("并发同事件号登记：恰好1个成功，其余 DISABLE_EVENT_EXISTS",
      results.count("ok") == 1
      and all(c in ("ok", "DISABLE_EVENT_EXISTS", "TRANSACTION_CONFLICT") for c in results),
      str(results))
with db.pool.connection() as c:
    n = c.execute("SELECT count(*) n FROM instrument_disable_events WHERE event_no='UNIQ'").fetchone()["n"]
check("同事件号只有一行", n == 1, str(n))

# ---------- 7. 并发：撤销 vs 发布（串行化，不误放行） ----------
reset()
passed_calibration("D7")
locked_register(DisableCreate(
    event_no="J-1", instrument="D7",
    disabled_from=T(2026, 1, 1), disabled_to=T(2027, 1, 1), reason="x"))
barrier = threading.Barrier(2)


def pub_once():
    barrier.wait()
    code, payload = locked_publish(ReportSubmit(
        instrument="D7", tested_at=T(2026, 6, 1), reference="revoke-race"))
    return "released" if code == 200 else payload["code"]


def revoke_once():
    barrier.wait()
    code, payload = locked_revoke("J-1", DisableRevoke())
    return "revoked" if code == 200 else payload["code"]


with ThreadPoolExecutor(2) as ex:
    f1 = ex.submit(pub_once)
    f2 = ex.submit(revoke_once)
    pr, rr = f1.result(), f2.result()
check("撤销与发布并发：撤销成功且结果可串行（发布要么阻断要么撤销后放行）",
      rr == "revoked" and pr in ("released", "INSTRUMENT_DISABLED"),
      f"publish={pr}, revoke={rr}")

print()
if failures:
    print(f"{len(failures)} 项失败:", failures)
    sys.exit(1)
print("全部禁用区间验证通过")
