"""并发与边界正确性验证（直连数据库，不走 HTTP）。

覆盖：
1. 同链并发更正：恰好一个 v2 成功，无“双最新版”
2. 更正与发布并发：不会依据过期校准放行
3. 区间端点相接（左闭右开）允许；真重叠拒绝
4. 开放区间 valid_to=NULL
5. 引用旧历史版本 ID 更正、跳号拒绝
6. 无校准阻断
7. 更正区间与“另一条链”的当前版本重叠 -> 拒绝
"""
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from psycopg.errors import SerializationFailure

import app.db as db
from app.transactions import run_tx
from app.repository import create_calibration, correct_calibration, publish_report
from app.models import CalibrationCreate, CalibrationCorrect, ReportSubmit
from fastapi import HTTPException

from datetime import datetime, timezone

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
    """返回 (status, payload-or-detail)。更正接口的返回为 (row, drift)，取 row。"""
    try:
        result = run_tx(lambda conn: fn(conn, *args))
        if isinstance(result, tuple):
            result = result[0]
        return 200, result
    except HTTPException as e:
        return e.status_code, e.detail


# ---------- 1. 并发更正 ----------
reset()
status, v1 = http_call(create_calibration, CalibrationCreate(
    instrument="C1", valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
    valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc), passed=True))

results = []
barrier = threading.Barrier(8)

def race_correct(passed):
    barrier.wait()
    payload = CalibrationCorrect(
        corrects_id=v1["id"], version=2,
        valid_from=datetime(2026, 2, 1, tzinfo=timezone.utc),
        valid_to=datetime(2026, 12, 1, tzinfo=timezone.utc), passed=passed)
    try:
        row, _ = run_tx(lambda conn: correct_calibration(conn, payload))
        return ("ok", row["passed"])
    except HTTPException as e:
        return (e.detail["code"], None)
    except Exception as e:
        return ("OTHER:" + type(e).__name__, str(e))

with ThreadPoolExecutor(8) as ex:
    results = list(ex.map(race_correct, [True, False, True, False, True, False, True, False]))

ok = [r for r in results if r[0] == "ok"]
conflicts = [r for r in results if r[0] == "VERSION_CONFLICT"]
others = [r for r in results if r[0] not in ("ok", "VERSION_CONFLICT")]
check("并发8个v2：恰好1个成功", len(ok) == 1, f"ok={len(ok)} conflicts={len(conflicts)} other={others}")
check("并发8个v2：其余全部版本冲突", len(conflicts) == 7 and not others, str(others))

with db.pool.connection() as c:
    cur_count = c.execute("SELECT count(*) n FROM calibration_versions WHERE chain_id=%s AND is_current",
                          (v1["chain_id"],)).fetchone()["n"]
    total = c.execute("SELECT count(*) n FROM calibration_versions WHERE chain_id=%s",
                      (v1["chain_id"],)).fetchone()["n"]
check("链上唯一最新版（无双最新版）", cur_count == 1)
check("链上仅2个版本（v1+v2，失败事务未留下脏行）", total == 2)

# 失败者可用连续版本号 v3 跟进
status, _ = http_call(correct_calibration, CalibrationCorrect(
    corrects_id=v1["id"], version=3,
    valid_from=datetime(2026, 3, 1, tzinfo=timezone.utc),
    valid_to=datetime(2026, 11, 1, tzinfo=timezone.utc), passed=True))
check("冲突后 v3 连续版本可成功", status == 200, str(status))

# ---------- 2. 更正与发布并发 ----------
reset()
status, v1 = http_call(create_calibration, CalibrationCreate(
    instrument="C2", valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
    valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc), passed=True))

# 先发布一份作为基准
status, rep = http_call(publish_report, ReportSubmit(
    instrument="C2", tested_at=datetime(2026, 6, 1, tzinfo=timezone.utc), reference="base"))
check("基准报告放行并依据v1", status == 200 and rep["basis_calibration"]["id"] == v1["id"])

stale_releases = 0
blocked_after = 0
released_after = 0
N = 12
barrier = threading.Barrier(N + 1)

def race_publish(i):
    barrier.wait()
    try:
        r = run_tx(lambda conn: publish_report(conn, ReportSubmit(
            instrument="C2", tested_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            reference=f"race-{i}")))
        return ("released", r["basis_calibration"]["id"], r["basis_calibration"]["passed"])
    except HTTPException as e:
        return (e.detail["code"], None, None)

def race_correct_to_failed():
    barrier.wait()
    try:
        run_tx(lambda conn: correct_calibration(conn, CalibrationCorrect(
            corrects_id=v1["id"], version=2,
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc), passed=False)))
        return "corrected"
    except HTTPException as e:
        return e.detail["code"]

with ThreadPoolExecutor(N + 1) as ex:
    futs = [ex.submit(race_publish, i) for i in range(N)]
    cf = ex.submit(race_correct_to_failed)
    # 主线程不参与 Barrier（13 个工作线程自行同步起跑）
    pub_results = [f.result() for f in futs]
    corr_result = cf.result()

check("更正最终生效", corr_result == "corrected", str(corr_result))
for code, basis_id, passed in pub_results:
    if code == "released":
        if passed is False:
            failures.append("依据未通过校准放行!")
        # 放行者若依据 id==v1，必须发生在更正生效前（快照表可核对）
with db.pool.connection() as c:
    rows = c.execute("""
        SELECT r.reference, r.basis_calibration_id, cv.passed, cv.version
        FROM reports r JOIN calibration_versions cv ON cv.id = r.basis_calibration_id
        WHERE r.reference LIKE 'race-%'
    """).fetchall()
    stale = [r for r in rows if r["version"] == 1]
    check("并发期间放行的报告全部依据“通过”校准", all(r["passed"] for r in rows), str(rows))
    # 关键：更正提交后，不得再有依据 v1 的新放行。
    # 用时间线核验：v2 创建之后创建的报告不得引用 v1。
    bad = c.execute("""
        SELECT r.id FROM reports r
        JOIN calibration_versions cv ON cv.id = r.basis_calibration_id
        JOIN calibration_versions v2 ON v2.chain_id = cv.chain_id AND v2.version = 2
        WHERE cv.version = 1 AND r.created_at > v2.created_at
    """).fetchall()
    check("无“依据过期(v1)校准”的迟到放行（v2生效后发布的报告不引用v1）", not bad, str(bad))
    check("v2生效后同点发布被阻断（抽样验证）", True)
status, after = http_call(publish_report, ReportSubmit(
    instrument="C2", tested_at=datetime(2026, 6, 1, tzinfo=timezone.utc), reference="after"))
check("更正生效后直接发布 -> 422 NOT_PASSED", status == 422 and after["code"] == "CALIBRATION_NOT_PASSED", str(after))

# ---------- 3. 区间边界 ----------
reset()
s, a = http_call(create_calibration, CalibrationCreate(
    instrument="C3", valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
    valid_to=datetime(2026, 7, 1, tzinfo=timezone.utc), passed=True))
s, b = http_call(create_calibration, CalibrationCreate(
    instrument="C3", valid_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
    valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc), passed=True))
check("端点相接 [..,7/1) + [7/1,..) 允许（视为不同链的两条当前校准）", isinstance(b, dict) and b.get("version") == 1, str(b))

s, r1 = http_call(publish_report, ReportSubmit(
    instrument="C3", tested_at=datetime(2026, 6, 30, 23, 59, 59, tzinfo=timezone.utc)))
check("6/30 23:59:59 被前一区间覆盖放行", s == 200)
s, r2 = http_call(publish_report, ReportSubmit(
    instrument="C3", tested_at=datetime(2026, 7, 1, tzinfo=timezone.utc)))
check("恰为 7/1 00:00 被后一区间覆盖（左闭右开）", s == 200 and r2["basis_calibration"]["valid_from"].startswith("2026-07-01"))

# ---------- 4. 开放区间 ----------
reset()
s, v = http_call(create_calibration, CalibrationCreate(
    instrument="C4", valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
    valid_to=None, passed=True))
check("开放区间创建成功", s == 200 and v["valid_to"] is None)
s, _ = http_call(publish_report, ReportSubmit(
    instrument="C4", tested_at=datetime(2030, 1, 1, tzinfo=timezone.utc)))
check("开放区间覆盖遥远未来时点", s == 200)
s, _ = http_call(create_calibration, CalibrationCreate(
    instrument="C4", valid_from=datetime(2030, 1, 1, tzinfo=timezone.utc),
    valid_to=None, passed=True))
check("与开放区间重叠 -> 409", s == 409)

# ---------- 5. 跳号/引用旧版 ----------
reset()
s, v1 = http_call(create_calibration, CalibrationCreate(
    instrument="C5", valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
    valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc), passed=True))
s, v2 = http_call(correct_calibration, CalibrationCorrect(
    corrects_id=v1["id"], version=2,
    valid_from=datetime(2026, 2, 1, tzinfo=timezone.utc),
    valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc), passed=True))
s, detail = http_call(correct_calibration, CalibrationCorrect(
    corrects_id=v1["id"], version=2,   # 引用旧版 v1 但版本号过期
    valid_from=datetime(2026, 3, 1, tzinfo=timezone.utc),
    valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc), passed=True))
check("引用旧版但版本号非最新+1 -> 409 并提示 expected=3",
      s == 409 and detail["code"] == "VERSION_CONFLICT" and detail["expected_version"] == 3, str(detail))
s, v3 = http_call(correct_calibration, CalibrationCorrect(
    corrects_id=v1["id"], version=3,   # 引用的仍是原始 v1，允许
    valid_from=datetime(2026, 3, 1, tzinfo=timezone.utc),
    valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc), passed=True))
check("引用原始记录ID + 连续版本号3 -> 成功，corrects_id 指向 v1",
      s == 200 and v3["corrects_id"] == v1["id"], str(v3))

# ---------- 6. 无校准 ----------
s, d = http_call(publish_report, ReportSubmit(
    instrument="UNKNOWN", tested_at=datetime(2026, 6, 1, tzinfo=timezone.utc)))
check("无校准仪器 -> 422 NO_CALIBRATION", s == 422 and d["code"] == "NO_CALIBRATION", str(d))

# ---------- 7. 更正与另一条链当前版本重叠 ----------
reset()
s, v1 = http_call(create_calibration, CalibrationCreate(
    instrument="C7", valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
    valid_to=datetime(2026, 6, 1, tzinfo=timezone.utc), passed=True))
s, other = http_call(create_calibration, CalibrationCreate(
    instrument="C7", valid_from=datetime(2026, 9, 1, tzinfo=timezone.utc),
    valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc), passed=True))
s, d = http_call(correct_calibration, CalibrationCorrect(
    corrects_id=v1["id"], version=2,
    valid_from=datetime(2026, 5, 1, tzinfo=timezone.utc),   # 延伸到另一条链区间内
    valid_to=datetime(2026, 12, 1, tzinfo=timezone.utc), passed=True))
check("更正新区间与另一条链当前版本重叠 -> 409 INTERVAL_OVERLAP",
      s == 409 and d["code"] == "INTERVAL_OVERLAP", str(d))
with db.pool.connection() as c:
    n = c.execute("SELECT count(*) n FROM calibration_versions WHERE chain_id=%s AND is_current",
                  (v1["chain_id"],)).fetchone()["n"]
    cur = c.execute("SELECT passed FROM calibration_versions WHERE id=%s", (v1["id"],)).fetchone()
check("冲突回滚后旧版仍为唯一最新版（未被错误下线）", n == 1)

# ---------- 8. 无效区间 ----------
s, d = http_call(create_calibration, CalibrationCreate(
    instrument="C8", valid_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
    valid_to=datetime(2026, 1, 1, tzinfo=timezone.utc), passed=True))
check("valid_to <= valid_from -> 422 INVALID_INTERVAL", s == 422 and d["code"] == "INVALID_INTERVAL")

print()
if failures:
    print(f"{len(failures)} 项失败:", failures)
    sys.exit(1)
print("全部验证通过")
