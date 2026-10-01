"""报告撤回（样本身份错误等）功能的正确性与并发验证（直连数据库，不走 HTTP）。

覆盖：
1. 基础撤回：仅链尾且尚未撤回可撤回；成功后链不再有可使用的现行报告
   （tip_report_id 仍为该报告，current_report_id 为 NULL），报告与发布快照永久保留，
   沿革显示撤回单、原因与时间
2. 拒绝场景：不存在 -> 404；已接替 -> 409 REPORT_ALREADY_SUPERSEDED；
   已撤回 -> 409 REPORT_ALREADY_WITHDRAWN（败方均附当前链状态）；
   撤回单号转给其他报告/复用 -> 409 WITHDRAWAL_NO_EXISTS；空原因/空单号 -> 参数校验
3. 撤回后复测接替必须拒绝：409 REPORT_WITHDRAWN，不生成新报告、不留接替记录
4. 历史兼容：报告详情/列表内容不变；已撤回报告仍出现在漂移重算与禁用影响追溯中
5. 并发：同一链尾的撤回与接替（同仪器/跨仪器）、撤回与撤回，至多一方成功，
   败方拿到当前链状态；链上“接替后再撤回新链尾”可完整沿革
6. 失败不写撤回：全部失败路径后撤回表无对应记录
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
    WithdrawSubmit,
)
from app.repository import (
    affected_reports,
    chain_state,
    chain_tip_id,
    compute_drift,
    create_calibration,
    publish_report,
    register_disable,
    report_lineage,
    supersede_report,
    withdraw_report,
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
    """与生产路由一致：按复测仪器咨询锁串行；败方补当前链状态。"""
    try:
        result = run_tx(lambda conn: supersede_report(conn, report_id, payload),
                        advisory_lock=db.instrument_lock_key(payload.instrument))
        return 201, result
    except HTTPException as e:
        detail = e.detail
        state = run_tx(lambda conn: chain_state(conn, report_id), readonly=True)
        if state is not None and e.status_code == 409 and detail.get("code") in (
                "REPORT_ALREADY_SUPERSEDED", "REPORT_ALREADY_WITHDRAWN",
                "REPORT_WITHDRAWN"):
            detail["tip_report_id"] = state["tip_report_id"]
            detail["current_report_id"] = state["current_report_id"]
        return e.status_code, detail


def locked_withdraw(report_id: int, payload: WithdrawSubmit):
    """撤回（无咨询锁，报告行 FOR UPDATE 串行）；败方补当前链状态。"""
    try:
        result = run_tx(lambda conn: withdraw_report(conn, report_id, payload))
        return 201, result
    except HTTPException as e:
        detail = e.detail
        state = run_tx(lambda conn: chain_state(conn, report_id), readonly=True)
        if state is not None and e.status_code == 409 and detail.get("code") in (
                "REPORT_ALREADY_SUPERSEDED", "REPORT_ALREADY_WITHDRAWN",
                "REPORT_WITHDRAWN"):
            detail["tip_report_id"] = state["tip_report_id"]
            detail["current_report_id"] = state["current_report_id"]
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


def state(report_id):
    return run_tx(lambda conn: chain_state(conn, report_id), readonly=True)


def tip(report_id):
    return run_tx(lambda conn: chain_tip_id(conn, report_id), readonly=True)


# ---------- 1. 基础撤回 ----------
reset()
passed_calibration("W1")
status, rep = locked_publish(ReportSubmit(
    instrument="W1", tested_at=T(2026, 3, 15), reference="W1-R1"))
rid = rep["report_id"]
snap_before = rep["snapshot"]

status, result = locked_withdraw(rid, WithdrawSubmit(
    withdrawal_no="WD-2026-0001", reason="样本身份错误：与委托单不一致"))
check("链尾未撤回报告可撤回 -> 201", status == 201, str(result))
w = result["withdrawal"]
check("撤回响应含撤回单/原因/时间且链不再有现行报告",
      result["withdrawn"] is True
      and result["tip_report_id"] == rid
      and result["current_report_id"] is None
      and w["withdrawal_no"] == "WD-2026-0001"
      and w["reason"] == "样本身份错误：与委托单不一致"
      and w["report_id"] == rid
      and w["created_at"], str(result))

st = state(rid)
check("链状态：链尾仍是该报告，但当前可使用标识为 NULL",
      st["tip_report_id"] == rid and st["current_report_id"] is None
      and st["withdrawal"]["withdrawal_no"] == "WD-2026-0001", str(st))
check("chain_tip_id 仍指向被撤回的链尾（区分链尾/现行）", tip(rid) == rid)

lin = lineage(rid)
check("沿革：tip_report_id 指向链尾，current_report_id 为 NULL",
      lin["tip_report_id"] == rid and lin["current_report_id"] is None, str(lin))
check("沿革显示撤回单、原因与时间",
      [(x["withdrawal_no"], x["reason"]) for x in lin["withdrawals"]]
      == [("WD-2026-0001", "样本身份错误：与委托单不一致")]
      and lin["withdrawals"][0]["created_at"], str(lin["withdrawals"]))

with db.pool.connection() as c:
    row = c.execute("SELECT released, snapshot FROM reports WHERE id = %s",
                    (rid,)).fetchone()
check("报告行与发布快照永久保留且原样不改写",
      row["released"] is True and row["snapshot"] == snap_before, str(row["snapshot"]))

# ---------- 2. 撤回前置校验 ----------
status, detail = locked_withdraw(999999, WithdrawSubmit(
    withdrawal_no="WD-MISS", reason="x"))
check("撤回不存在的报告 -> 404 NOT_FOUND",
      status == 404 and detail["code"] == "NOT_FOUND", str(detail))

status, detail = locked_withdraw(rid, WithdrawSubmit(
    withdrawal_no="WD-2026-0002", reason="重复撤回"))
check("已撤回 -> 409 REPORT_ALREADY_WITHDRAWN 且附当前链状态",
      status == 409 and detail["code"] == "REPORT_ALREADY_WITHDRAWN"
      and detail["tip_report_id"] == rid
      and detail["current_report_id"] is None
      and detail["withdrawal_no"] == "WD-2026-0001", str(detail))

# 已接替（非链尾）报告不可撤回
reset()
passed_calibration("W2")
status, r1 = locked_publish(ReportSubmit(
    instrument="W2", tested_at=T(2026, 3, 15), reference="W2-R1"))
old_id = r1["report_id"]
status, r2 = locked_supersede(old_id, SupersedeSubmit(
    instrument="W2", tested_at=T(2026, 4, 1),
    reference="W2-R2", reason="复测"))
new_id = r2["report_id"]
status, detail = locked_withdraw(old_id, WithdrawSubmit(
    withdrawal_no="WD-2026-010", reason="撤回非链尾"))
check("已接替（非链尾）-> 409 REPORT_ALREADY_SUPERSEDED 且附链状态",
      status == 409 and detail["code"] == "REPORT_ALREADY_SUPERSEDED"
      and detail["tip_report_id"] == new_id
      and detail["current_report_id"] == new_id
      and detail["superseded_by"] == new_id, str(detail))

# 撤回单号不得转给其他报告，也不可复用
status, other = locked_publish(ReportSubmit(
    instrument="W2", tested_at=T(2026, 3, 16), reference="W2-OTHER"))
status, detail = locked_withdraw(other["report_id"], WithdrawSubmit(
    withdrawal_no="WD-2026-010", reason="换个报告使用同一单号"))
check("同号撤回单转给其他报告 -> 409 WITHDRAWAL_NO_EXISTS（本次未撤回）",
      status == 409 and detail["code"] == "WITHDRAWAL_NO_EXISTS"
      and detail["withdrawal_no"] == "WD-2026-010"
      and detail["bound_report_id"] is None, str(detail))
# 先成功占用单号的链尾（new_id）仍可撤回；另一份未受影响
status, detail = locked_withdraw(new_id, WithdrawSubmit(
    withdrawal_no="WD-2026-010", reason="撤回当前链尾"))
check("被拒报告未被误撤回；当前链尾用同号可成功",
      status == 201, str(detail))
status, detail = locked_withdraw(other["report_id"], WithdrawSubmit(
    withdrawal_no="WD-2026-010", reason="再次尝试复用单号"))
check("已使用的撤回单号不可复用 -> 409 WITHDRAWAL_NO_EXISTS 且归属正确",
      status == 409 and detail["code"] == "WITHDRAWAL_NO_EXISTS"
      and detail["bound_report_id"] == new_id, str(detail))
check("另一份报告仍为现行（失败不写撤回）",
      state(other["report_id"])["current_report_id"] == other["report_id"])

for kwargs in (dict(withdrawal_no="WD-X", reason=""),
               dict(withdrawal_no="WD-X", reason="   "),
               dict(withdrawal_no="", reason="x"),
               dict(withdrawal_no="   ", reason="x")):
    try:
        WithdrawSubmit(**kwargs)
        check(f"参数校验拒绝 {kwargs}", False)
    except ValidationError:
        check(f"参数校验拒绝 {kwargs}", True)

# ---------- 3. 撤回后复测接替必须拒绝，不生成新报告 ----------
reset()
passed_calibration("W3")
status, rep = locked_publish(ReportSubmit(
    instrument="W3", tested_at=T(2026, 3, 15), reference="W3-R1"))
rid = rep["report_id"]
status, _ = locked_withdraw(rid, WithdrawSubmit(
    withdrawal_no="WD-2026-020", reason="身份错误"))
check("撤回成功", status == 201, str(status))
status, detail = locked_supersede(rid, SupersedeSubmit(
    instrument="W3", tested_at=T(2026, 4, 1),
    reference="W3-R2", reason="撤回后复测"))
check("撤回后复测接替 -> 409 REPORT_WITHDRAWN 且败方附当前链状态",
      status == 409 and detail["code"] == "REPORT_WITHDRAWN"
      and detail["withdrawal_no"] == "WD-2026-020"
      and detail["tip_report_id"] == rid
      and detail["current_report_id"] is None, str(detail))
with db.pool.connection() as c:
    n_rep = c.execute("SELECT count(*) n FROM reports").fetchone()["n"]
    n_sup = c.execute("SELECT count(*) n FROM report_supersessions").fetchone()["n"]
    n_wd = c.execute("SELECT count(*) n FROM report_withdrawals").fetchone()["n"]
check("撤回后接替不生成新报告、不留接替记录",
      n_rep == 1 and n_sup == 0 and n_wd == 1,
      f"reports={n_rep} sup={n_sup} wd={n_wd}")
check("链仍无现行报告", state(rid)["current_report_id"] is None)

# ---------- 4. 历史内容与追溯兼容 ----------
reset()
from app.models import CalibrationCorrect  # noqa: E402
from app.repository import correct_calibration  # noqa: E402

# v1 覆盖检测时间且通过；更正为 v2（同区间改成未通过）制造 NOW_BLOCKED 漂移
status, v1 = http_call(create_calibration, CalibrationCreate(
    instrument="W4", valid_from=T(2025, 1, 1), valid_to=T(2030, 1, 1), passed=True))
status, rep = locked_publish(ReportSubmit(
    instrument="W4", tested_at=T(2026, 1, 15), reference="W4-R1"))
rid = rep["report_id"]
status, v2row = http_call(correct_calibration, CalibrationCorrect(
    corrects_id=v1["id"], version=2,
    valid_from=T(2025, 1, 1), valid_to=T(2030, 1, 1), passed=False))
assert status == 200, v2row
# 追溯登记禁用区间，命中已发布报告检测时间
status, _ = locked_register(DisableCreate(
    event_no="W4-DIS", instrument="W4",
    disabled_from=T(2026, 1, 1), disabled_to=T(2026, 2, 1), reason="追溯登记故障"))
status, _ = locked_withdraw(rid, WithdrawSubmit(
    withdrawal_no="WD-2026-030", reason="身份错误"))
check("撤回成功（漂移/禁用均存在的链尾）", status == 201, str(status))

drift = run_tx(lambda conn: compute_drift(conn, "W4"), readonly=True)
check("已撤回报告仍出现在漂移重算中（NOW_BLOCKED）",
      [d["report_id"] for d in drift] == [rid]
      and drift[0]["change"] == "NOW_BLOCKED", str([d["report_id"] for d in drift]))

affected = run_tx(lambda conn: affected_reports(conn, "W4"), readonly=True)
check("已撤回报告仍出现在禁用影响追溯中（快照仍为发布时放行）",
      [a["report_id"] for a in affected] == [rid]
      and affected[0]["disable"]["event_no"] == "W4-DIS"
      and affected[0]["snapshot"]["released"] is True, str(affected))

with db.pool.connection() as c:
    listed = c.execute(
        "SELECT id, reference, released, snapshot FROM reports ORDER BY id").fetchall()
check("报告列表/详情内容不变：撤回报告仍在列且字段原样",
      [(r["id"], r["reference"], r["released"]) for r in listed] == [(rid, "W4-R1", True)]
      and listed[0]["snapshot"]["released"] is True, str(listed))

# 撤销禁用后，已撤回报告随追溯结果变化（消失），但报告与撤回均保留
# （直接置 revoked 以复用同一事件号场景；走登记一条新撤销路径等价）
with db.pool.connection() as c:
    c.execute(
        "UPDATE instrument_disable_events SET revoked = TRUE, revoked_at = now() "
        "WHERE event_no = 'W4-DIS'")
affected = run_tx(lambda conn: affected_reports(conn, "W4"), readonly=True)
check("禁用撤销后追溯结果变化：已撤回报告随之消失（记录本身保留）",
      affected == [] and state(rid)["current_report_id"] is None, str(affected))

# ---------- 5a. 并发：撤回 vs 撤回（同一链尾） ----------
reset()
passed_calibration("C1")
status, rep = locked_publish(ReportSubmit(
    instrument="C1", tested_at=T(2026, 3, 15), reference="C1-R1"))
rid = rep["report_id"]
N = 8
barrier = threading.Barrier(N)


def race_withdraw(i):
    barrier.wait()
    code, payload = locked_withdraw(rid, WithdrawSubmit(
        withdrawal_no=f"WD-C1-{i}", reason=f"并发撤回 {i}"))
    if code == 201:
        return ("ok", payload["withdrawal"]["withdrawal_no"])
    return (payload["code"], payload.get("tip_report_id"),
            payload.get("current_report_id"))


with ThreadPoolExecutor(N) as ex:
    results = list(ex.map(race_withdraw, range(N)))
winners = [r for r in results if r[0] == "ok"]
losers = [r for r in results if r[0] != "ok"]
check("并发撤回：恰好 1 个成功", len(winners) == 1, str(results))
check("并发撤回：败方 409 REPORT_ALREADY_WITHDRAWN 且拿到链状态（tip 在、现行为 NULL）",
      all(r[0] == "REPORT_ALREADY_WITHDRAWN" and r[1] == rid and r[2] is None
          for r in losers), str(losers))
with db.pool.connection() as c:
    n_wd = c.execute(
        "SELECT count(*) n FROM report_withdrawals WHERE report_id = %s",
        (rid,)).fetchone()["n"]
check("并发撤回后撤回记录恰 1 条（败方单号未落库）", n_wd == 1, f"wd={n_wd}")

# ---------- 5b. 并发：撤回 vs 接替（同仪器，同一咨询锁路径） ----------
reset()
passed_calibration("C2")
status, rep = locked_publish(ReportSubmit(
    instrument="C2", tested_at=T(2026, 3, 15), reference="C2-R1"))
rid = rep["report_id"]
barrier = threading.Barrier(2)
outcome: dict = {}


def do_withdraw():
    barrier.wait()
    code, payload = locked_withdraw(rid, WithdrawSubmit(
        withdrawal_no="WD-C2", reason="并发撤回"))
    outcome["withdraw"] = (code, payload)


def do_supersede():
    barrier.wait()
    code, payload = locked_supersede(rid, SupersedeSubmit(
        instrument="C2", tested_at=T(2026, 4, 1),
        reference="C2-R2", reason="并发接替"))
    outcome["supersede"] = (code, payload)


with ThreadPoolExecutor(2) as ex:
    fw = ex.submit(do_withdraw)
    fs = ex.submit(do_supersede)
    fw.result(); fs.result()
wc, wd_ = outcome["withdraw"]
sc, sd_ = outcome["supersede"]
ok_side = "withdraw" if wc == 201 else "supersede" if sc == 201 else None
check("撤回 vs 接替：恰好一方成功",
      (wc == 201) ^ (sc == 201), f"withdraw={wc} supersede={sc}")
if ok_side == "withdraw":
    check("撤回胜：接替败方 409 REPORT_WITHDRAWN",
          sc == 409 and sd_["code"] == "REPORT_WITHDRAWN", str(sd_))
    st = state(rid)
    check("撤回胜后：链尾不变、无现行报告、无新报告",
          st["tip_report_id"] == rid and st["current_report_id"] is None, str(st))
    with db.pool.connection() as c:
        n_rep = c.execute("SELECT count(*) n FROM reports").fetchone()["n"]
        n_sup = c.execute("SELECT count(*) n FROM report_supersessions").fetchone()["n"]
    check("撤回胜：不生成新报告/接替记录", n_rep == 1 and n_sup == 0,
          f"reports={n_rep} sup={n_sup}")
else:
    new_id = sd_["report_id"]
    check("接替胜：撤回败方 409 REPORT_ALREADY_SUPERSEDED 且现行指向新报告",
          wc == 409 and wd_["code"] == "REPORT_ALREADY_SUPERSEDED"
          and wd_["tip_report_id"] == new_id and wd_["current_report_id"] == new_id,
          str(wd_))
    with db.pool.connection() as c:
        n_wd = c.execute("SELECT count(*) n FROM report_withdrawals").fetchone()["n"]
    check("接替胜：败方撤回不落库", n_wd == 0, f"wd={n_wd}")
    # 接替成功后，新链尾仍可被撤回（链的正常终结）
    status, result = locked_withdraw(new_id, WithdrawSubmit(
        withdrawal_no="WD-C2-AFTER", reason="接替后再撤回新链尾"))
    check("接替后可撤回新链尾，沿革含接替与撤回且无现行报告",
          status == 201 and state(rid)["current_report_id"] is None, str(status))
    lin = lineage(rid)
    check("完整沿革：两报告 + 接替 + 撤回，tip=新链尾",
          [r["id"] for r in lin["reports"]] == [rid, new_id]
          and lin["tip_report_id"] == new_id
          and lin["current_report_id"] is None
          and [x["report_id"] for x in lin["withdrawals"]] == [new_id]
          and lin["supersessions"][0]["new_report_id"] == new_id,
          str((lin["tip_report_id"], lin["withdrawals"])))

# ---------- 5c. 并发：撤回 vs 跨仪器接替（不同咨询锁，行锁兜底） ----------
reset()
passed_calibration("C3A")
passed_calibration("C3B")
status, rep = locked_publish(ReportSubmit(
    instrument="C3A", tested_at=T(2026, 3, 15), reference="C3-R1"))
rid = rep["report_id"]
barrier = threading.Barrier(2)
outcome = {}


def do_withdraw2():
    barrier.wait()
    outcome["withdraw"] = locked_withdraw(rid, WithdrawSubmit(
        withdrawal_no="WD-C3", reason="跨仪器赛跑撤回"))


def do_supersede2():
    barrier.wait()
    outcome["supersede"] = locked_supersede(rid, SupersedeSubmit(
        instrument="C3B", tested_at=T(2026, 4, 1),
        reference="C3-R2", reason="跨仪器赛跑接替"))


with ThreadPoolExecutor(2) as ex:
    fw = ex.submit(do_withdraw2)
    fs = ex.submit(do_supersede2)
    fw.result(); fs.result()
wc, wd_ = outcome["withdraw"]
sc, sd_ = outcome["supersede"]
check("跨仪器：撤回与接替恰好一方成功",
      (wc == 201) ^ (sc == 201), f"withdraw={wc} supersede={sc}")
with db.pool.connection() as c:
    n_wd = c.execute("SELECT count(*) n FROM report_withdrawals").fetchone()["n"]
    n_sup = c.execute("SELECT count(*) n FROM report_supersessions").fetchone()["n"]
    n_rep = c.execute("SELECT count(*) n FROM reports").fetchone()["n"]
if wc == 201:
    check("跨仪器：撤回胜 -> 接替被 409 拒绝，无新报告/接替记录",
          sc == 409 and n_wd == 1 and n_sup == 0 and n_rep == 1,
          f"{sc} wd={n_wd} sup={n_sup} rep={n_rep}")
    check("跨仪器：撤回胜，接替败方原因为 REPORT_WITHDRAWN",
          sd_["code"] == "REPORT_WITHDRAWN", str(sd_))
else:
    check("跨仪器：接替胜 -> 撤回 409 被拒，无撤回记录，恰两份报告一条接替",
          wc == 409 and n_wd == 0 and n_sup == 1 and n_rep == 2
          and wd_["code"] == "REPORT_ALREADY_SUPERSEDED",
          f"{wc} wd={n_wd} sup={n_sup} rep={n_rep}")

# ---------- 6. 多代链撤回后从任一历史报告查沿革 ----------
reset()
passed_calibration("C4")
ids = []
status, rep = locked_publish(ReportSubmit(
    instrument="C4", tested_at=T(2026, 3, 1), reference="C4-R1"))
ids.append(rep["report_id"])
for i, day in enumerate((4, 5, 6), start=2):
    status, rep = locked_supersede(ids[-1], SupersedeSubmit(
        instrument="C4", tested_at=T(2026, day, 1),
        reference=f"C4-R{i}", reason=f"第 {i} 代复测"))
    assert status == 201, rep
    ids.append(rep["report_id"])
status, _ = locked_withdraw(ids[-1], WithdrawSubmit(
    withdrawal_no="WD-C4-TIP", reason="末代身份错误"))
check("多代链：链尾撤回成功", status == 201, str(status))
for hist_id in ids:
    lin = lineage(hist_id)
    ok = ([r["id"] for r in lin["reports"]] == ids
          and lin["tip_report_id"] == ids[-1]
          and lin["current_report_id"] is None
          and [w["report_id"] for w in lin["withdrawals"]] == [ids[-1]])
    check(f"从历史报告 {hist_id} 查沿革一致（含撤回、无现行）", ok, str(lin))

print()
if failures:
    print(f"{len(failures)} 项失败:", failures)
    sys.exit(1)
print("全部报告撤回验证通过")
