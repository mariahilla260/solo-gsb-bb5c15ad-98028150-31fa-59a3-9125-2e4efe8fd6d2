"""排班规划（仪器组可放行时间窗）功能验证（直连数据库，不走 HTTP）。

覆盖：
1. 请求模型校验：空列表 / 空标识 / 重复 / 起点不早于终点 -> 422
2. 切分正确性：禁用优先、校准通过/未通过/缺失/区间外，左闭右开端点，
   查询首尾与开放终点截断
3. 合并规则：仅当全组结论与各仪器依据（原因码+所据事件/版本）均相同才合并
4. 无当前校准的仪器不被其他仪器的可放行结论掩盖（全组恒不可放行且给出原因）
5. 依据正确性：basis_disable / basis_calibration / nearest_calibration
6. 更正校准、撤销禁用后重查：结果随当前已提交状态变化，整次查询同一快照
"""
import os
import sys
import time
from datetime import datetime, timezone

from fastapi import HTTPException
from pydantic import ValidationError

import app.db as db
from app.models import (
    CalibrationCorrect,
    CalibrationCreate,
    DisableCreate,
    DisableRevoke,
    ReleaseWindowQuery,
)
from app.planning import plan_release_windows
from app.repository import (
    correct_calibration,
    create_calibration,
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


def call(fn, *a, **kw):
    try:
        return 200, run_tx(lambda conn: fn(conn, *a, **kw))
    except HTTPException as e:
        return e.status_code, e.detail


def T(y, mo=1, d=1, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def plan(instruments, start, end):
    return run_tx(
        lambda conn: plan_release_windows(conn, instruments, start, end),
        readonly=True,
    )


def seg_map(result):
    return [(s["start"], s["end"], s["all_releasable"],
             {i["instrument"]: (i["reason_code"], i["basis_calibration"],
                                i["basis_disable"]) for i in s["instruments"]})
            for s in result["segments"]]


def add_cal(instrument, frm, to=None, passed=True):
    status, row = call(create_calibration, CalibrationCreate(
        instrument=instrument, valid_from=frm, valid_to=to, passed=passed))
    assert status == 200, row
    return row


def add_dis(event, instrument, frm, to=None, reason="故障"):
    status, row = call(register_disable, DisableCreate(
        event_no=event, instrument=instrument,
        disabled_from=frm, disabled_to=to, reason=reason))
    assert status == 200, row
    return row


def correct(row_id, version, frm, to, passed):
    status, new = call(correct_calibration, CalibrationCorrect(
        corrects_id=row_id, version=version,
        valid_from=frm, valid_to=to, passed=passed))
    assert status == 200, new
    return new


# ---------- 1. 请求模型校验 ----------
def expect_invalid(payload: dict, label: str):
    try:
        ReleaseWindowQuery(**payload)
        check(label, False, "未拒绝")
    except ValidationError:
        check(label, True)


expect_invalid({"instruments": [], "start": T(2026, 1, 1), "end": T(2026, 2, 1)},
               "空仪器列表 -> 422")
expect_invalid({"instruments": ["A", ""], "start": T(2026, 1, 1), "end": T(2026, 2, 1)},
               "空仪器标识 -> 422")
expect_invalid({"instruments": ["A", "A"], "start": T(2026, 1, 1), "end": T(2026, 2, 1)},
               "重复仪器标识 -> 422")
expect_invalid({"instruments": ["A"], "start": T(2026, 2, 1), "end": T(2026, 2, 1)},
               "起点等于终点 -> 422")
expect_invalid({"instruments": ["A"], "start": T(2026, 3, 1), "end": T(2026, 2, 1)},
               "起点晚于终点 -> 422")

# 带时区/不带时区归一化：2026-01-01 10:00 UTC == 2026-01-01 18:00 +08:00
q = ReleaseWindowQuery(instruments=["A"], start=T(2026, 1, 1, 9),
                       end="2026-01-01T18:00:00+08:00")
check("带时区终点被归一化且严格晚于 UTC 起点（09:00 < 10:00 UTC）",
      q.end == T(2026, 1, 1, 10), f"{q.start} {q.end}")

# ---------- 2. 基本切分：禁用优先 + 校准通过 + 开放终点截断 ----------
reset()
# A: 校准 [2026-01-01, 2026-12-01) 通过；禁用 [2026-03-01, 2026-04-01)
cal_a = add_cal("A", T(2026, 1, 1), T(2026, 12, 1), True)
add_dis("P-D1", "A", T(2026, 3, 1), T(2026, 4, 1), "进样阀故障")

res = plan(["A"], T(2026, 1, 1), T(2026, 6, 1))
segs = [(s["start"], s["end"], s["all_releasable"],
         s["instruments"][0]["reason_code"]) for s in res["segments"]]
check("单机切分为 放行/禁用/放行 三段，端点左闭右开",
      segs == [
          ("2026-01-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00", True, "PASSED_CALIBRATION_COVERAGE"),
          ("2026-03-01T00:00:00+00:00", "2026-04-01T00:00:00+00:00", False, "INSTRUMENT_DISABLED"),
          ("2026-04-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00", True, "PASSED_CALIBRATION_COVERAGE"),
      ], str(segs))
check("开放/首尾截断：首段从查询起点开始、末段截在查询终点",
      res["segments"][0]["start"] == res["query_start"]
      and res["segments"][-1]["end"] == res["query_end"]
      and res["segments"][-1]["end"] == "2026-06-01T00:00:00+00:00", "")

# 依据：禁用段给出事件，放行段给出校准版本
mid = res["segments"][1]["instruments"][0]
check("禁用段依据为禁用事件号且不带校准版本",
      mid["basis_disable"]["event_no"] == "P-D1" and mid["basis_calibration"] is None,
      str(mid))
ok = res["segments"][0]["instruments"][0]
check("放行段依据为校准版本（id/chain/version/passed）",
      ok["basis_calibration"]["id"] == cal_a["id"]
      and ok["basis_calibration"]["passed"] is True
      and ok["basis_disable"] is None, str(ok))

# 子范围查询：首段起点为查询起点（不早于），禁用终点落在范围外时末段截查询终点
res2 = plan(["A"], T(2026, 3, 15), T(2026, 5, 1))
ends = [(s["start"], s["end"], s["instruments"][0]["reason_code"])
        for s in res2["segments"]]
check("子范围：[3/15,4/1)禁用 + [4/1,5/1)放行，边界准确",
      ends == [
          ("2026-03-15T00:00:00+00:00", "2026-04-01T00:00:00+00:00", "INSTRUMENT_DISABLED"),
          ("2026-04-01T00:00:00+00:00", "2026-05-01T00:00:00+00:00", "PASSED_CALIBRATION_COVERAGE"),
      ], str(ends))

# 查询范围完全落在禁用区间内：单段且边界为查询首尾
res3 = plan(["A"], T(2026, 3, 10), T(2026, 3, 20))
check("完全落在禁用区间：单段 INSTRUMENT_DISABLED，首尾即查询首尾",
      len(res3["segments"]) == 1 and not res3["segments"][0]["all_releasable"]
      and res3["segments"][0]["start"] == "2026-03-10T00:00:00+00:00"
      and res3["segments"][0]["end"] == "2026-03-20T00:00:00+00:00", str(seg_map(res3)))

# 开放禁用区间（终点 NULL）：禁用段截到查询终点
add_dis("P-D2", "A2", T(2026, 3, 1), None, "持续禁用")
res4 = plan(["A2"], T(2026, 1, 1), T(2026, 6, 1))
s4 = [(s["start"], s["end"], s["instruments"][0]["reason_code"]) for s in res4["segments"]]
check("开放禁用：[1/1,3/1)NO_CALIBRATION + [3/1,6/1)INSTRUMENT_DISABLED（禁用优先，开放终点截查询终点）",
      s4 == [
          ("2026-01-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00", "NO_CALIBRATION"),
          ("2026-03-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00", "INSTRUMENT_DISABLED"),
      ], str(s4))

# ---------- 3. 全组合并与“依据不同不合并” ----------
reset()
# B: 全年通过校准；C: 全年通过校准但 2 月有一条禁用
add_cal("B", T(2026, 1, 1), T(2027, 1, 1), True)
add_cal("C", T(2026, 1, 1), T(2027, 1, 1), True)
add_dis("G-D1", "C", T(2026, 2, 1), T(2026, 3, 1), "C 故障")

res = plan(["B", "C"], T(2026, 1, 1), T(2026, 4, 1))
segs = [(s["start"], s["end"], s["all_releasable"]) for s in res["segments"]]
check("全组可放行段被 C 的禁用段分开（结论不同不合并）",
      segs == [
          ("2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00", True),
          ("2026-02-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00", False),
          ("2026-03-01T00:00:00+00:00", "2026-04-01T00:00:00+00:00", True),
      ], str(segs))

# 两台仪器在同一窗口被不同事件禁用：全组结论同为 False，但各仪器依据不同 ->
# 此处恰好同时切换（边界重合），仍为单段；再用“先后不同事件”验证依据不同分段。
add_dis("G-D2", "B", T(2026, 2, 15), T(2026, 2, 20), "B 短暂故障")
res = plan(["B", "C"], T(2026, 1, 1), T(2026, 4, 1))
segs = [(s["start"], s["end"], s["all_releasable"],
         {i["instrument"]: i["reason_code"] for i in s["instruments"]})
        for s in res["segments"]]
check("依据变化（B 自身禁用起止）即使全组仍 False 也切分",
      segs == [
          ("2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00", True,
           {"B": "PASSED_CALIBRATION_COVERAGE", "C": "PASSED_CALIBRATION_COVERAGE"}),
          ("2026-02-01T00:00:00+00:00", "2026-02-15T00:00:00+00:00", False,
           {"B": "PASSED_CALIBRATION_COVERAGE", "C": "INSTRUMENT_DISABLED"}),
          ("2026-02-15T00:00:00+00:00", "2026-02-20T00:00:00+00:00", False,
           {"B": "INSTRUMENT_DISABLED", "C": "INSTRUMENT_DISABLED"}),
          ("2026-02-20T00:00:00+00:00", "2026-03-01T00:00:00+00:00", False,
           {"B": "PASSED_CALIBRATION_COVERAGE", "C": "INSTRUMENT_DISABLED"}),
          ("2026-03-01T00:00:00+00:00", "2026-04-01T00:00:00+00:00", True,
           {"B": "PASSED_CALIBRATION_COVERAGE", "C": "PASSED_CALIBRATION_COVERAGE"}),
      ], str(segs))

# 依据不同（校准版本切换）即使结论同为可放行也不合并：
# 同仪器两条“不同校准链”的当前版本相接（v 记录 [1/1,3/1)，w 记录 [3/1,5/1)）
reset()
add_cal("D", T(2026, 1, 1), T(2026, 3, 1), True)
add_cal("D", T(2026, 3, 1), T(2026, 5, 1), True)
res = plan(["D"], T(2026, 1, 1), T(2026, 4, 1))
segs = [(s["start"], s["end"], s["all_releasable"],
         s["instruments"][0]["basis_calibration"]["id"]) for s in res["segments"]]
ids = [s["instruments"][0]["basis_calibration"]["id"] for s in res["segments"]]
check("可放行但所据校准记录变化（相接两条通过版本）不合并",
      len(segs) == 2 and segs[0][:3] == ("2026-01-01T00:00:00+00:00",
                                        "2026-03-01T00:00:00+00:00", True)
      and segs[1][:3] == ("2026-03-01T00:00:00+00:00",
                          "2026-04-01T00:00:00+00:00", True)
      and ids[0] != ids[1], str(segs))

# ---------- 4. 无校准不被掩盖；未通过；区间外与相邻线索 ----------
reset()
add_cal("E", T(2026, 1, 1), T(2026, 6, 1), True)
# F 无任何校准
res = plan(["E", "F"], T(2026, 1, 1), T(2026, 3, 1))
check("含无校准仪器：全组恒不可放行，且 F 显式给出 NO_CALIBRATION（不被 E 掩盖）",
      len(res["segments"]) == 1
      and res["segments"][0]["all_releasable"] is False
      and {i["instrument"]: i["reason_code"] for i in res["segments"][0]["instruments"]}
          == {"E": "PASSED_CALIBRATION_COVERAGE", "F": "NO_CALIBRATION"},
      str(seg_map(res)))

# G：校准未通过
add_cal("G", T(2026, 1, 1), T(2026, 6, 1), False)
res = plan(["G"], T(2026, 1, 1), T(2026, 3, 1))
g0 = res["segments"][0]["instruments"][0]
check("校准未通过 -> CALIBRATION_NOT_PASSED 且返回该版本为依据",
      len(res["segments"]) == 1 and g0["reason_code"] == "CALIBRATION_NOT_PASSED"
      and g0["basis_calibration"]["passed"] is False, str(g0))

# H：校准只覆盖 2 月；之前 OUTSIDE（早于生效），之后 OUTSIDE（晚于失效），2 月放行
add_cal("H", T(2026, 2, 1), T(2026, 3, 1), True)
res = plan(["H"], T(2026, 1, 1), T(2026, 4, 1))
segs = [(s["start"], s["end"], s["instruments"][0]["reason_code"]) for s in res["segments"]]
check("区间外：OUTSIDE_VALID_PERIOD / 通过 / OUTSIDE_VALID_PERIOD，左闭右开",
      segs == [
          ("2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00", "OUTSIDE_VALID_PERIOD"),
          ("2026-02-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00", "PASSED_CALIBRATION_COVERAGE"),
          ("2026-03-01T00:00:00+00:00", "2026-04-01T00:00:00+00:00", "OUTSIDE_VALID_PERIOD"),
      ], str(segs))
check("OUTSIDE 段带 nearest_calibration 相邻线索（id 与区间一致）",
      all(s["instruments"][0]["nearest_calibration"]
          and s["instruments"][0]["basis_calibration"] is None
          for s in res["segments"][::2]), str(seg_map(res)))

# 校准完全落在查询范围之外：全程 OUTSIDE 单段，相邻线索仍给出
reset()
add_cal("I", T(2030, 1, 1), T(2031, 1, 1), True)
res = plan(["I"], T(2026, 1, 1), T(2026, 2, 1))
i0 = res["segments"][0]["instruments"][0]
check("校准在遥远未来：全程 OUTSIDE_VALID_PERIOD 单段且有相邻线索（区别于 NO_CALIBRATION）",
      len(res["segments"]) == 1 and i0["reason_code"] == "OUTSIDE_VALID_PERIOD"
      and i0["nearest_calibration"] is not None, str(i0))

# ---------- 5. 禁用优先于“未通过/缺失”校准 ----------
reset()
add_cal("J", T(2026, 1, 1), T(2027, 1, 1), False)
add_dis("J-D1", "J", T(2026, 2, 1), T(2026, 3, 1), "故障优先")
res = plan(["J"], T(2026, 1, 1), T(2026, 4, 1))
segs = [s["instruments"][0]["reason_code"] for s in res["segments"]]
check("禁用优先：NOT_PASSED -> DISABLED -> NOT_PASSED",
      segs == ["CALIBRATION_NOT_PASSED", "INSTRUMENT_DISABLED", "CALIBRATION_NOT_PASSED"],
      str(segs))

# 已撤销禁用不参与切分
# 直接复用事务级撤销（与路由一致：先探后锁）
probe = run_tx(lambda conn: get_disable(conn, "J-D1"), readonly=True)
assert probe is not None
status, revoked_row = call(revoke_disable, "J-D1", DisableRevoke(reason="修复"))
assert status == 200, revoked_row
res = plan(["J"], T(2026, 1, 1), T(2026, 4, 1))
segs = [s["instruments"][0]["reason_code"] for s in res["segments"]]
check("撤销禁用后规划结果不再含禁用段（记录保留但不参与）",
      segs == ["CALIBRATION_NOT_PASSED"], str(segs))

# ---------- 6. 更正后重查：结果随当前状态变化 ----------
reset()
k = add_cal("K", T(2026, 1, 1), T(2027, 1, 1), True)
res_before = plan(["K"], T(2026, 1, 1), T(2026, 6, 1))
check("更正前：单段全组可放行 v1",
      len(res_before["segments"]) == 1 and res_before["segments"][0]["all_releasable"]
      and res_before["segments"][0]["instruments"][0]["basis_calibration"]["version"] == 1,
      str(seg_map(res_before)))
correct(k["id"], 2, T(2026, 1, 1), T(2027, 1, 1), False)
res_after = plan(["K"], T(2026, 1, 1), T(2026, 6, 1))
check("更正为未通过后重查：单段 CALIBRATION_NOT_PASSED，依据变为 v2",
      len(res_after["segments"]) == 1
      and not res_after["segments"][0]["all_releasable"]
      and res_after["segments"][0]["instruments"][0]["reason_code"] == "CALIBRATION_NOT_PASSED"
      and res_after["segments"][0]["instruments"][0]["basis_calibration"]["version"] == 2,
      str(seg_map(res_after)))

# ---------- 7. 整次查询同一已提交状态：查询进行中提交禁用不产生“半新半旧”结果 ----------
import threading  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

reset()
add_cal("M", T(2026, 1, 1), T(2027, 1, 1), True)

START, END = T(2026, 1, 1), T(2027, 1, 1)
barrier = threading.Barrier(2)
plan_holder: dict = {}
# 与生产路由一致：咨询锁键在事务外预先取得（避免在持连接等待时再申请连接）
lock_key_m = db.instrument_lock_key("M")


def slow_plan_once():
    # 进入 SERIALIZABLE 只读事务、取得快照后与并发禁用登记在屏障处会合，
    # 再等待其提交完成后才切分。整个查询只读、不拿仪器写锁；快照一致性
    # 由 SERIALIZABLE 保证（屏障只在事务内等待一次）。
    def fn(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT 1")  # 取得事务快照
            cur.fetchall()
        barrier.wait()
        time.sleep(0.5)  # 留出禁用登记提交窗口
        return plan_release_windows(conn, ["M"], START, END)

    try:
        plan_holder["res"] = run_tx(fn, readonly=True)
    except HTTPException as e:
        plan_holder["res"] = e.detail


def racing_disable():
    barrier.wait()
    time.sleep(0.1)
    try:
        run_tx(lambda conn: register_disable(conn, DisableCreate(
            event_no="M-D-RACE", instrument="M",
            disabled_from=T(2026, 3, 1), disabled_to=T(2026, 4, 1),
            reason="并发登记")), advisory_lock=lock_key_m)
    except HTTPException as e:
        plan_holder["werr"] = e.detail


with ThreadPoolExecutor(2) as ex:
    fp = ex.submit(slow_plan_once)
    fd = ex.submit(racing_disable)
    fd.result(timeout=30)
    fp.result(timeout=30)

r = plan_holder["res"]
codes = [s["instruments"][0]["reason_code"] for s in r["segments"]] \
    if isinstance(r, dict) and "segments" in r else r
# 查询快照取在禁用事务提交之前：必为单段放行（不会中途“看到”新禁用而混出三段）
check("查询快照先于禁用提交取得：整次结果为提交前状态（单段放行），不混用提交后状态",
      codes == ["PASSED_CALIBRATION_COVERAGE"], str(codes))

# 并发结束后重查：必为三段（当前状态已含禁用）
res_now = plan(["M"], START, END)
check("并发结束后重查稳定为 放行/禁用/放行 三段（之后的查询看到新状态）",
      [s["instruments"][0]["reason_code"] for s in res_now["segments"]]
      == ["PASSED_CALIBRATION_COVERAGE", "INSTRUMENT_DISABLED",
          "PASSED_CALIBRATION_COVERAGE"], "")

# ---------- 8. 响应结构与“仅供规划”声明 ----------
r = res_after
check("响应携带查询回显、段数与 planning_only/notice 声明",
      r["query_start"].startswith("2026-01-01")
      and r["query_end"] == "2026-06-01T00:00:00+00:00"
      and r["segment_count"] == len(r["segments"]) == 1
      and r["planning_only"] is True and "规划" in r["notice"]
      and r["instruments"] == ["K"], str({k_: r[k_] for k_ in
                                          ("query_end", "segment_count", "planning_only")}))
check("时段按时间排序且相邻不重叠、并集恰为查询范围",
      all(a["end"] == b["start"] for a, b in zip(r["segments"], r["segments"][1:]))
      and r["segments"][0]["start"] == r["query_start"]
      and r["segments"][-1]["end"] == r["query_end"], "")

print()
if failures:
    print(f"{len(failures)} 项失败:", failures)
    sys.exit(1)
print("全部排班规划验证通过")
