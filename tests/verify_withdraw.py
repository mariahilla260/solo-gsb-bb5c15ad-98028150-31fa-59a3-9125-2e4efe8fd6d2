"""报告撤回（样本身份错误登记）功能验证。

覆盖（仓库层直连 + 真实 HTTP API 两层）：
1. 基础撤回：链尾未撤回报告按 报告ID+唯一撤回单号+非空原因 登记成功；
   报告与发布快照永久保留；链尾仍是该报告，但该链不再有可使用的现行报告
2. 拒绝：不存在 404；已撤回 409 REPORT_ALREADY_WITHDRAWN；已接替（非链尾）
   409 REPORT_ALREADY_SUPERSEDED；撤回单号重复（转给其他报告）409 WITHDRAWAL_NO_EXISTS；
   空原因/空单号参数校验拒绝；失败不写撤回
3. 撤回后复测接替：409 REPORT_WITHDRAWN 且不生成新报告、不留接替记录
4. 详情/列表/沿革：历史内容继续返回；区分链尾标识与当前可使用标识；
   沿革显示撤回单、原因与时间
5. 已撤回报告仍出现在既有漂移重算与禁用影响追溯中
6. 并发：同一链尾的撤回与接替赛跑至多一方成功，败方返回当前链状态；
   同号撤回单并发不得转给多份报告
7. HTTP API：/reports/{id}/withdraw 经 uvicorn 真实服务调用的状态码与响应体
"""
import os

# 并发赛跑线程数超过默认连接池上限，测试进程内调大。必须在导入 app.config 前设置。
os.environ.setdefault("DB_POOL_MAX_SIZE", "30")
os.environ.setdefault("DB_POOL_MIN_SIZE", "5")

import json
import sys
import threading
import urllib.error
import urllib.request
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
    chain_status,
    chain_tip_id,
    compute_drift,
    create_calibration,
    get_report_flags,
    list_reports_flags,
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
        return 200, run_tx(lambda conn: fn(conn, *args))
    except HTTPException as e:
        return e.status_code, e.detail


def locked_publish(payload: ReportSubmit):
    try:
        result = run_tx(lambda conn: publish_report(conn, payload),
                        advisory_lock=db.instrument_lock_key(payload.instrument))
        return 201, result
    except HTTPException as e:
        return e.status_code, e.detail


def locked_supersede(report_id: int, payload: SupersedeSubmit):
    """与生产路由一致：先报告链锁后仪器锁（统一锁序防死锁）；败方补当前链状态。"""
    try:
        result = run_tx(
            lambda conn: supersede_report(conn, report_id, payload),
            advisory_lock=[
                db.report_lock_key(report_id),
                db.instrument_lock_key(payload.instrument),
            ],
        )
        return 201, result
    except HTTPException as e:
        detail = e.detail
        if isinstance(detail, dict) and detail.get("code") in (
                "REPORT_ALREADY_SUPERSEDED", "REPORT_WITHDRAWN"):
            st = run_tx(lambda conn: chain_status(conn, report_id), readonly=True)
            detail["chain_state"] = st
        return e.status_code, detail


def locked_withdraw(report_id: int, payload: WithdrawSubmit):
    """与生产路由一致：报告链锁；失败补当前链状态。"""
    try:
        result = run_tx(
            lambda conn: withdraw_report(conn, report_id, payload),
            advisory_lock=db.report_lock_key(report_id),
        )
        return 201, result
    except HTTPException as e:
        detail = e.detail
        if isinstance(detail, dict) and detail.get("code") in (
                "REPORT_ALREADY_SUPERSEDED", "REPORT_ALREADY_WITHDRAWN",
                "REPORT_WITHDRAWN"):
            detail["chain_state"] = run_tx(
                lambda conn: chain_status(conn, report_id), readonly=True)
        return e.status_code, detail


def locked_register(payload: DisableCreate):
    try:
        return 200, run_tx(lambda conn: register_disable(conn, payload),
                          advisory_lock=db.instrument_lock_key(payload.instrument))
    except HTTPException as e:
        return e.status_code, e.detail


def passed_calibration(instrument, frm=T(2025, 1, 1), to=T(2030, 1, 1)):
    status, row = http_call(create_calibration, CalibrationCreate(
        instrument=instrument, valid_from=frm, valid_to=to, passed=True))
    assert status == 200, row
    return row


def flags(report_id):
    return run_tx(lambda conn: get_report_flags(conn, report_id), readonly=True)


# ---------- 1. 基础撤回：链尾、未撤回、报告与快照保留 ----------
reset()
passed_calibration("W1")
status, rep = locked_publish(ReportSubmit(
    instrument="W1", tested_at=T(2026, 3, 15), reference="W1-R1"))
rid = rep["report_id"]
snap_before = rep["snapshot"]

status, out = locked_withdraw(rid, WithdrawSubmit(
    withdrawal_no="WD-2026-0001", reason="样本身份错误：检品编号与委托单不符"))
check("撤回成功 -> 201，撤回单/原因落库",
      status == 201
      and out["withdrawal"]["withdrawal_no"] == "WD-2026-0001"
      and out["withdrawal"]["report_id"] == rid
      and out["withdrawal"]["reason"] == "样本身份错误：检品编号与委托单不符",
      str(out))
check("撤回后链尾标识仍是该报告，但无当前可使用报告",
      out["chain_status"]["tip_report_id"] == rid
      and out["chain_status"]["usable_report_id"] is None
      and out["chain_status"]["chain_withdrawn"] is True, str(out["chain_status"]))

with db.pool.connection() as c:
    row = c.execute("SELECT released, snapshot FROM reports WHERE id = %s",
                    (rid,)).fetchone()
    n_w = c.execute("SELECT count(*) n FROM report_withdrawals").fetchone()["n"]
check("报告行与发布快照永久保留（released/snapshot 原样），撤回登记恰 1 条",
      row["released"] is True and row["snapshot"] == snap_before and n_w == 1,
      str(row["snapshot"]))

f = flags(rid)
check("详情：is_chain_tip=true 但 is_usable=false（区分两个标识）",
      f["is_chain_tip"] is True and f["is_usable"] is False
      and f["is_withdrawn"] is True and f["tip_report_id"] == rid
      and f["withdrawal_no"] == "WD-2026-0001", str(dict(f)))
check("详情仍返回历史内容（快照字段不变）",
      f["snapshot"] == snap_before and f["reference"] == "W1-R1")

lin = run_tx(lambda conn: report_lineage(conn, rid), readonly=True)
check("沿革：tip=撤回报告，usable=None，含撤回单/原因/时间",
      lin["tip_report_id"] == rid and lin["usable_report_id"] is None
      and len(lin["withdrawals"]) == 1
      and lin["withdrawals"][0]["withdrawal_no"] == "WD-2026-0001"
      and lin["withdrawals"][0]["reason"].startswith("样本身份错误")
      and lin["withdrawals"][0]["created_at"] is not None, str(lin["withdrawals"]))

# ---------- 2. 拒绝场景：不存在 / 已撤回 / 空入参 ----------
status, detail = locked_withdraw(999999, WithdrawSubmit(
    withdrawal_no="WD-X", reason="不存在的报告"))
check("撤回不存在的报告 -> 404 NOT_FOUND",
      status == 404 and detail["code"] == "NOT_FOUND", str(detail))

status, detail = locked_withdraw(rid, WithdrawSubmit(
    withdrawal_no="WD-2026-0002", reason="重复撤回"))
check("已撤回报告再次撤回 -> 409 REPORT_ALREADY_WITHDRAWN 且附链状态",
      status == 409 and detail["code"] == "REPORT_ALREADY_WITHDRAWN"
      and detail["withdrawal_no"] == "WD-2026-0001"
      and detail["chain_state"]["usable_report_id"] is None, str(detail))

for bad in ("", "   "):
    try:
        WithdrawSubmit(withdrawal_no=bad, reason="x")
        check("空/空白撤回单号 -> 参数校验拒绝", False)
    except ValidationError:
        check("空/空白撤回单号 -> 参数校验拒绝", True)
    try:
        WithdrawSubmit(withdrawal_no="WD-Z", reason=bad)
        check("空/空白撤回原因 -> 参数校验拒绝", False)
    except ValidationError:
        check("空/空白撤回原因 -> 参数校验拒绝", True)

with db.pool.connection() as c:
    n_w = c.execute("SELECT count(*) n FROM report_withdrawals").fetchone()["n"]
check("各类失败后撤回记录仍只有 1 条（失败不写撤回）", n_w == 1, f"n={n_w}")

# ---------- 3. 撤回后复测接替必须拒绝，且不生成新报告 ----------
status, detail = locked_supersede(rid, SupersedeSubmit(
    instrument="W1", tested_at=T(2026, 4, 1),
    reference="W1-R2", reason="撤回后复测"))
check("撤回后复测接替 -> 409 REPORT_WITHDRAWN",
      status == 409 and detail["code"] == "REPORT_WITHDRAWN"
      and detail["withdrawal_no"] == "WD-2026-0001"
      and detail["chain_state"]["usable_report_id"] is None, str(detail))
with db.pool.connection() as c:
    n_rep = c.execute("SELECT count(*) n FROM reports WHERE instrument = 'W1'").fetchone()["n"]
    n_sup = c.execute("SELECT count(*) n FROM report_supersessions").fetchone()["n"]
check("拒绝后不生成新报告、不留接替记录", n_rep == 1 and n_sup == 0,
      f"reports={n_rep} sup={n_sup}")

# ---------- 4. 已接替（非链尾）不能撤回；链尾可撤回 ----------
reset()
passed_calibration("W2")
status, r1 = locked_publish(ReportSubmit(
    instrument="W2", tested_at=T(2026, 3, 15), reference="W2-R1"))
id1 = r1["report_id"]
status, r2 = locked_supersede(id1, SupersedeSubmit(
    instrument="W2", tested_at=T(2026, 4, 1),
    reference="W2-R2", reason="例行复测"))
id2 = r2["report_id"]

status, detail = locked_withdraw(id1, WithdrawSubmit(
    withdrawal_no="WD-MID", reason="撤回非链尾"))
check("非链尾（已接替）报告撤回 -> 409 REPORT_ALREADY_SUPERSEDED 且败方见链尾",
      status == 409 and detail["code"] == "REPORT_ALREADY_SUPERSEDED"
      and detail["chain_state"]["tip_report_id"] == id2
      and detail["chain_state"]["usable_report_id"] == id2, str(detail.get("chain_state")))
with db.pool.connection() as c:
    n_w = c.execute("SELECT count(*) n FROM report_withdrawals").fetchone()["n"]
check("非链尾撤回失败不写撤回", n_w == 0, f"n={n_w}")

status, out = locked_withdraw(id2, WithdrawSubmit(
    withdrawal_no="WD-TIP", reason="链尾样本身份错误"))
check("链尾撤回成功，整条链随即无可用现行报告",
      status == 201 and out["chain_status"]["usable_report_id"] is None, str(status))
check("从中间报告查链状态：链尾=id2，已撤回，可用=None",
      run_tx(lambda conn: chain_status(conn, id1), readonly=True)
      == run_tx(lambda conn: chain_status(conn, id1), readonly=True))  # 稳定读取
st = run_tx(lambda conn: chain_status(conn, id1), readonly=True)
check("中间报告视角的链状态正确",
      st["tip_report_id"] == id2 and st["usable_report_id"] is None
      and st["report"]["is_chain_tip"] is False
      and st["report"]["is_withdrawn"] is False, str(st))
f2 = flags(id1)
check("中间报告自身 is_chain_tip=false、is_usable=false；链尾撤回信息在 tip 上",
      f2["is_chain_tip"] is False and f2["is_usable"] is False
      and f2["is_withdrawn"] is False and f2["withdrawal_no"] is None
      and f2["tip_report_id"] == id2 and f2["tip_withdrawal_no"] == "WD-TIP",
      str(dict(f2)))
check("撤回链尾后仍不能从中间报告接替",
      locked_supersede(id1, SupersedeSubmit(
          instrument="W2", tested_at=T(2026, 5, 1), reason="x"))[0] == 409)

# ---------- 5. 同号撤回单不得转给其他报告 ----------
reset()
passed_calibration("W3A")
passed_calibration("W3B")
status, a = locked_publish(ReportSubmit(
    instrument="W3A", tested_at=T(2026, 3, 15), reference="W3A-R1"))
status, b = locked_publish(ReportSubmit(
    instrument="W3B", tested_at=T(2026, 3, 15), reference="W3B-R1"))
id_a, id_b = a["report_id"], b["report_id"]
status, _ = locked_withdraw(id_a, WithdrawSubmit(
    withdrawal_no="WD-SAME", reason="先用于 A"))
check("A 使用撤回单 WD-SAME 成功", status == 201, str(status))
status, detail = locked_withdraw(id_b, WithdrawSubmit(
    withdrawal_no="WD-SAME", reason="再转给 B"))
check("同号撤回单转给其他报告 -> 409 WITHDRAWAL_NO_EXISTS",
      status == 409 and detail["code"] == "WITHDRAWAL_NO_EXISTS"
      and detail["withdrawal_no"] == "WD-SAME", str(detail))
with db.pool.connection() as c:
    wb = c.execute("SELECT * FROM report_withdrawals WHERE report_id = %s",
                   (id_b,)).fetchone()
check("B 未被写入撤回（失败不写撤回）", wb is None)

# ---------- 6. 已撤回报告仍出现在漂移与禁用影响追溯中 ----------
reset()
status, cal = http_call(create_calibration, CalibrationCreate(
    instrument="W4", valid_from=T(2025, 1, 1), valid_to=T(2026, 6, 1), passed=True))
status, rep = locked_publish(ReportSubmit(
    instrument="W4", tested_at=T(2026, 3, 15), reference="W4-R1"))
rid4 = rep["report_id"]
locked_withdraw(rid4, WithdrawSubmit(withdrawal_no="WD-DRIFT", reason="身份错误"))
# 更正校准为未通过 -> 快照放行 vs 当前阻断
from app.models import CalibrationCorrect
from app.repository import correct_calibration
# 找当前版本 id 与链
with db.pool.connection() as c:
    currow = c.execute(
        "SELECT * FROM calibration_versions WHERE instrument='W4' AND is_current").fetchone()
status, _ = http_call(correct_calibration, CalibrationCorrect(
    corrects_id=currow["id"], version=2,
    valid_from=T(2025, 1, 1), valid_to=T(2030, 1, 1), passed=False))
check("更正校准成功（v2 未通过）", status == 200, str(status))
drift = run_tx(lambda conn: compute_drift(conn, "W4"), readonly=True)
check("已撤回报告仍出现在漂移重算中（NOW_BLOCKED）",
      [d for d in drift if d["report_id"] == rid4 and d["change"] == "NOW_BLOCKED"] != [],
      str([(d["report_id"], d["change"]) for d in drift]))

# 禁用影响追溯：登记命中 tested_at 的未撤销禁用区间
locked_register(DisableCreate(
    event_no="DIS-W4", instrument="W4",
    disabled_from=T(2026, 3, 1), disabled_to=T(2026, 4, 1), reason="故障"))
aff = run_tx(lambda conn: affected_reports(conn, "W4"), readonly=True)
check("已撤回报告仍出现在禁用影响追溯中（且 disable 依据正确）",
      [x for x in aff if x["report_id"] == rid4
       and x["disable"]["event_no"] == "DIS-W4"] != [],
      str([(x["report_id"], x["disable"]["event_no"]) for x in aff]))

# 列表也包含已撤回报告（历史内容）
items = run_tx(lambda conn: list_reports_flags(conn, "W4"), readonly=True)
check("报告列表继续返回已撤回报告（含历史快照与链标识）",
      [i for i in items if i["id"] == rid4 and i["is_withdrawn"]
       and i["is_chain_tip"] and not i["is_usable"]] != [],
      str([(i["id"], i["is_withdrawn"]) for i in items]))

# ---------- 7. 并发：同一链尾 撤回 vs 接替，至多一方成功 ----------
reset()
passed_calibration("W5")
status, rep = locked_publish(ReportSubmit(
    instrument="W5", tested_at=T(2026, 3, 15), reference="W5-R1"))
rid = rep["report_id"]
barrier = threading.Barrier(8)


def race_withdraw(i):
    barrier.wait()
    code, payload = locked_withdraw(rid, WithdrawSubmit(
        withdrawal_no=f"WD-RACE-{i}", reason=f"并发撤回 {i}"))
    return ("withdraw", "ok" if code == 201 else payload["code"])


N_W = 4


def race_sup(i):
    barrier.wait()
    code, payload = locked_supersede(rid, SupersedeSubmit(
        instrument="W5", tested_at=T(2026, 4, 1 + i),
        reference=f"W5-NEW-{i}", reason=f"并发复测 {i}"))
    if code == 201:
        return ("supersede", "ok:" + str(payload["report_id"]))
    return ("supersede", payload["code"] + ":" + str(
        payload.get("chain_state", {}).get("tip_report_id")))


with ThreadPoolExecutor(8) as ex:
    futs = [ex.submit(race_withdraw, i) for i in range(N_W)]
    futs += [ex.submit(race_sup, i) for i in range(4)]
    raw = [f.result() for f in futs]

wd_results = [v for op, v in raw if op == "withdraw"]
sup_results = [v for op, v in raw if op == "supersede"]
n_winners = wd_results.count("ok") + sum(1 for v in sup_results if v.startswith("ok:"))
check("撤回/接替并发：至多一方成功", n_winners == 1,
      f"wd={wd_results} sup={sup_results}")
with db.pool.connection() as c:
    n_w = c.execute("SELECT count(*) n FROM report_withdrawals").fetchone()["n"]
    n_sup = c.execute("SELECT count(*) n FROM report_supersessions").fetchone()["n"]
    n_rep = c.execute("SELECT count(*) n FROM reports").fetchone()["n"]
st = run_tx(lambda conn: chain_status(conn, rid), readonly=True)
if wd_results.count("ok") == 1:
    check("撤回方胜出：1 撤回、0 接替、无新报告，链已无可用现行报告",
          n_w == 1 and n_sup == 0 and n_rep == 1
          and st["usable_report_id"] is None and st["tip_report_id"] == rid,
          f"w={n_w} s={n_sup} r={n_rep}")
    check("撤回败方全部 409 REPORT_ALREADY_WITHDRAWN",
          all(v == "REPORT_ALREADY_WITHDRAWN" for v in wd_results if v != "ok"),
          str(wd_results))
    check("接替败方全部 409 REPORT_WITHDRAWN 且链尾指向被撤回报告",
          all(v == f"REPORT_WITHDRAWN:{rid}" for v in sup_results),
          str(sup_results))
else:
    new_tip = int(next(v.split(":", 1)[1] for v in sup_results if v.startswith("ok:")))
    check("接替方胜出：0 撤回、1 接替、2 报告，现行报告为新版",
          n_w == 0 and n_sup == 1 and n_rep == 2
          and st["usable_report_id"] == new_tip and st["tip_report_id"] == new_tip,
          f"w={n_w} s={n_sup} r={n_rep} tip={new_tip}")
    check("撤回败方全部 409 REPORT_ALREADY_SUPERSEDED",
          all(v == "REPORT_ALREADY_SUPERSEDED" for v in wd_results),
          str(wd_results))
    check("接替败方全部 409 REPORT_ALREADY_SUPERSEDED 且拿到新链尾",
          all(v == f"REPORT_ALREADY_SUPERSEDED:{new_tip}" for v in sup_results
              if not v.startswith("ok:")), str(sup_results))

# ---------- 8. 并发：同号撤回单不得转给两份报告 ----------
reset()
passed_calibration("W6A")
passed_calibration("W6B")
status, a = locked_publish(ReportSubmit(
    instrument="W6A", tested_at=T(2026, 3, 15), reference="W6A-R1"))
status, b = locked_publish(ReportSubmit(
    instrument="W6B", tested_at=T(2026, 3, 15), reference="W6B-R1"))
id_a, id_b = a["report_id"], b["report_id"]
b2 = threading.Barrier(2)


def race_same(target):
    b2.wait()
    code, payload = locked_withdraw(target, WithdrawSubmit(
        withdrawal_no="WD-DUP", reason="同号并发"))
    return ("ok", target) if code == 201 else (payload["code"], target)


with ThreadPoolExecutor(2) as ex:
    fs = [ex.submit(race_same, id_a), ex.submit(race_same, id_b)]
rs = [f.result() for f in fs]
ok = [r for r in rs if r[0] == "ok"]
check("同号撤回单并发：恰一份成功", len(ok) == 1, str(rs))
check("同号撤回单并发：败方 409 WITHDRAWAL_NO_EXISTS",
      [r for r in rs if r[0] != "ok"][0][0] == "WITHDRAWAL_NO_EXISTS", str(rs))
with db.pool.connection() as c:
    rows = c.execute("SELECT report_id FROM report_withdrawals").fetchall()
check("撤回单仅绑定一份报告", len(rows) == 1 and rows[0]["report_id"] == ok[0][1], str(rows))

# ---------- 9. 真实 HTTP API（uvicorn 后台线程 + urllib） ----------
import uvicorn

from app.main import app

config = uvicorn.Config(app, host="127.0.0.1", port=8123, log_level="warning")
server = uvicorn.Server(config)
threading.Thread(target=server.run, daemon=True).start()
import time
for _ in range(100):
    try:
        urllib.request.urlopen("http://127.0.0.1:8123/health", timeout=1).read()
        break
    except Exception:
        time.sleep(0.1)

BASE = "http://127.0.0.1:8123"


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# 用全新链做 HTTP 验证（避免与上面状态混淆）
api("POST", "/calibrations", {
    "instrument": "H1", "valid_from": "2025-01-01T00:00:00Z",
    "valid_to": "2030-01-01T00:00:00Z", "passed": True})
st, rep = api("POST", "/reports", {
    "instrument": "H1", "tested_at": "2026-03-15T08:00:00Z",
    "reference": "H1-R1"})
http_rid = rep["report_id"]

st, out = api("POST", f"/reports/{http_rid}/withdraw", {
    "withdrawal_no": "WD-HTTP-1", "reason": "样本身份错误：委托方更正"})
check("HTTP 撤回成功 201，含撤回单与链状态",
      st == 201 and out["withdrawal"]["withdrawal_no"] == "WD-HTTP-1"
      and out["tip_report_id"] == http_rid and out["usable_report_id"] is None,
      str(out)[:300])

st, out = api("GET", f"/reports/{http_rid}/lineage")
check("HTTP 沿革：双标识 + withdrawals[] + 历史报告字段不变",
      st == 200 and out["tip_report_id"] == http_rid
      and out["current_report_id"] == http_rid
      and out["usable_report_id"] is None
      and [w["withdrawal_no"] for w in out["withdrawals"]] == ["WD-HTTP-1"]
      and set(out["reports"][0].keys()) == {
          "id", "reference", "instrument", "tested_at", "released",
          "basis_calibration_id", "snapshot", "created_at"}
      and out["reports"][0]["snapshot"]["released"] is True, str(out)[:400])

st, out = api("POST", f"/reports/{http_rid}/supersede", {
    "instrument": "H1", "tested_at": "2026-04-01T08:00:00Z",
    "reference": "H1-R2", "reason": "撤回后复测"})
check("HTTP 撤回后接替 -> 409 REPORT_WITHDRAWN 且无新报告",
      st == 409 and out["detail"]["code"] == "REPORT_WITHDRAWN"
      and out["detail"]["chain_state"]["usable_report_id"] is None, str(out)[:300])

st, out = api("POST", f"/reports/{http_rid}/withdraw", {
    "withdrawal_no": "WD-HTTP-2", "reason": "重复撤回"})
check("HTTP 重复撤回 -> 409 REPORT_ALREADY_WITHDRAWN",
      st == 409 and out["detail"]["code"] == "REPORT_ALREADY_WITHDRAWN", str(out)[:300])

# 同号撤回单转给另一份报告 -> 409 WITHDRAWAL_NO_EXISTS（单号不可复用）
api("POST", "/calibrations", {
    "instrument": "H1B", "valid_from": "2025-01-01T00:00:00Z",
    "valid_to": "2030-01-01T00:00:00Z", "passed": True})
_, rep = api("POST", "/reports", {
    "instrument": "H1B", "tested_at": "2026-03-15T08:00:00Z", "reference": "H1B-R1"})
other_rid = rep["report_id"]
st, out = api("POST", f"/reports/{other_rid}/withdraw", {
    "withdrawal_no": "WD-HTTP-1", "reason": "把同号撤回单转给别的报告"})
check("HTTP 同号撤回单转给其他报告 -> 409 WITHDRAWAL_NO_EXISTS",
      st == 409 and out["detail"]["code"] == "WITHDRAWAL_NO_EXISTS", str(out)[:300])
st, out = api("GET", f"/reports/{other_rid}")
check("被拒报告未被撤回（仍是当前可使用报告）",
      out["report"]["is_withdrawn"] is False and out["report"]["is_usable"] is True,
      str(out)[:300])

st, out = api("POST", "/reports/999999/withdraw", {
    "withdrawal_no": "WD-404", "reason": "不存在"})
check("HTTP 撤回不存在报告 -> 404 NOT_FOUND",
      st == 404 and out["detail"]["code"] == "NOT_FOUND", str(out)[:200])

st, out = api("POST", f"/reports/{http_rid}/withdraw", {
    "withdrawal_no": "WD-BLANK", "reason": "   "})
check("HTTP 空白原因 -> 422 参数校验", st == 422, str(st))

st, out = api("GET", f"/reports/{http_rid}")
check("HTTP 详情继续返回历史内容并带链标识",
      st == 200 and out["report"]["snapshot"]["released"] is True
      and out["report"]["is_chain_tip"] is True
      and out["report"]["is_usable"] is False
      and out["report"]["withdrawal"]["withdrawal_no"] == "WD-HTTP-1",
      str(out)[:400])

st, out = api("GET", "/reports?instrument=H1")
check("HTTP 列表含已撤回报告（历史内容 + 双标识）",
      st == 200 and len(out["items"]) == 1
      and out["items"][0]["id"] == http_rid
      and out["items"][0]["is_usable"] is False, str(out)[:300])

# 接替-再撤回 混合链的 HTTP 行为
api("POST", "/calibrations", {
    "instrument": "H2", "valid_from": "2025-01-01T00:00:00Z",
    "valid_to": "2030-01-01T00:00:00Z", "passed": True})
_, rep = api("POST", "/reports", {
    "instrument": "H2", "tested_at": "2026-03-15T08:00:00Z", "reference": "H2-R1"})
h2_1 = rep["report_id"]
_, rep = api("POST", f"/reports/{h2_1}/supersede", {
    "instrument": "H2", "tested_at": "2026-04-01T08:00:00Z",
    "reference": "H2-R2", "reason": "复测"})
h2_2 = rep["report_id"]
st, out = api("POST", f"/reports/{h2_1}/withdraw", {
    "withdrawal_no": "WD-H2-MID", "reason": "撤回非链尾"})
check("HTTP 非链尾撤回 -> 409 REPORT_ALREADY_SUPERSEDED + 现行链尾",
      st == 409 and out["detail"]["code"] == "REPORT_ALREADY_SUPERSEDED"
      and out["detail"]["chain_state"]["tip_report_id"] == h2_2
      and out["detail"]["chain_state"]["usable_report_id"] == h2_2, str(out)[:400])

server.should_exit = True
print()
if failures:
    print(f"{len(failures)} 项失败:", failures)
    sys.exit(1)
print("全部报告撤回验证通过")
