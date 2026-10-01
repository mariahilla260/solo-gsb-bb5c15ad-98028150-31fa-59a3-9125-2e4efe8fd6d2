#!/usr/bin/env bash
# 报告撤回（样本身份错误登记）端到端示例：
#   登记校准 -> 发布报告 -> 撤回 -> 重复撤回/撤回后复测被拒 -> 沿革/详情/列表
#   -> 已撤回报告仍在禁用影响追溯中 -> 撤回 vs 接替并发至多一方成功
# 依赖：curl、python3（仅用于格式化 JSON、提取字段与并发赛跑）
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool; }
jqid() { python3 -c 'import sys,json;print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

H_JSON="Content-Type: application/json"

echo "== 1. 登记校准并发布报告 R-WD-0001 =="
curl -sS -X POST "$BASE/calibrations" -H "$H_JSON" -d '{
  "instrument": "HPLC-09",
  "valid_from": "2026-01-01T00:00:00Z",
  "valid_to":   "2027-01-01T00:00:00Z",
  "passed": true
}' >/dev/null
RESP=$(curl -sS -X POST "$BASE/reports" -H "$H_JSON" -d '{
  "instrument": "HPLC-09",
  "tested_at": "2026-03-15T08:00:00Z",
  "reference": "R-WD-0001"
}')
echo "$RESP" | pp
RID=$(echo "$RESP" | jqid report_id)

echo "== 2. 按报告 ID + 唯一撤回单号 + 非空原因登记撤回 =="
curl -sS -X POST "$BASE/reports/$RID/withdraw" -H "$H_JSON" -d '{
  "withdrawal_no": "WD-2026-0001",
  "reason": "样本身份错误：检品编号与委托单不一致"
}' | pp
# 201 响应要点：
#   withdrawal.withdrawal_no/reason/created_at —— 撤回单、原因与时间
#   tip_report_id 仍为该报告（它仍是链尾），usable_report_id 为 null（无现行可用报告）

echo "== 3. 重复撤回 -> 409 REPORT_ALREADY_WITHDRAWN（失败不写撤回，附当前链状态） =="
curl -sS -w '\nHTTP %{http_code}\n' -X POST "$BASE/reports/$RID/withdraw" \
  -H "$H_JSON" -d '{
    "withdrawal_no": "WD-2026-0002",
    "reason": "再次撤回"
  }'

echo "== 4. 撤回后复测接替必须拒绝，且不生成新报告 -> 409 REPORT_WITHDRAWN =="
curl -sS -w '\nHTTP %{http_code}\n' -X POST "$BASE/reports/$RID/supersede" \
  -H "$H_JSON" -d '{
    "instrument": "HPLC-09",
    "tested_at": "2026-04-10T08:00:00Z",
    "reference": "R-WD-0001-RE",
    "reason": "撤回后复测"
  }'

echo "== 5. 同号撤回单不得转给其他报告 -> 409 WITHDRAWAL_NO_EXISTS =="
RESP=$(curl -sS -X POST "$BASE/reports" -H "$H_JSON" -d '{
  "instrument": "HPLC-09",
  "tested_at": "2026-03-16T08:00:00Z",
  "reference": "R-WD-0002"
}')
RID2=$(echo "$RESP" | jqid report_id)
curl -sS -w '\nHTTP %{http_code}\n' -X POST "$BASE/reports/$RID2/withdraw" \
  -H "$H_JSON" -d '{
    "withdrawal_no": "WD-2026-0001",
    "reason": "想把已用过的撤回单转给这份报告"
  }'

echo "== 6. 不存在的报告 -> 404 NOT_FOUND；空原因 -> 422 =="
curl -sS -w '\nHTTP %{http_code}\n' -X POST "$BASE/reports/999999/withdraw" \
  -H "$H_JSON" -d '{"withdrawal_no": "WD-X", "reason": "x"}'
curl -sS -w '\nHTTP %{http_code}\n' -X POST "$BASE/reports/$RID/withdraw" \
  -H "$H_JSON" -d '{"withdrawal_no": "WD-Y", "reason": "   "}'

echo "== 7. 详情/列表继续返回历史内容，并区分链尾标识与当前可使用标识 =="
echo "-- 详情：is_chain_tip=true、is_usable=false、withdrawal 为撤回单 --"
curl -sS "$BASE/reports/$RID" | pp
echo "-- 列表（含已撤回报告，历史快照原样） --"
curl -sS "$BASE/reports?instrument=HPLC-09" | pp

echo "== 8. 沿革：reports/supersessions 之外新增 withdrawals[]，双标识并列 =="
curl -sS "$BASE/reports/$RID/lineage" | pp
# tip_report_id/current_report_id = 链尾（被撤回的报告本身）
# usable_report_id = null；withdrawals[] 含撤回单号、原因与时间

echo "== 9. 已撤回报告仍出现在禁用影响追溯中（追溯与快照不改写） =="
curl -sS -X POST "$BASE/disables" -H "$H_JSON" -d '{
  "event_no": "DIS-WD-001",
  "instrument": "HPLC-09",
  "disabled_from": "2026-03-01T00:00:00Z",
  "disabled_to": "2026-04-01T00:00:00Z",
  "reason": "柱温箱故障"
}' >/dev/null
curl -sS "$BASE/disables/affected-reports?instrument=HPLC-09" | pp

echo "== 10. 并发：同一链尾的撤回与接替至多一方成功，败方返回当前链状态 =="
RESP=$(curl -sS -X POST "$BASE/reports" -H "$H_JSON" -d '{
  "instrument": "HPLC-09",
  "tested_at": "2026-07-01T08:00:00Z",
  "reference": "R-RACE-WD"
}')
BASE_ID=$(echo "$RESP" | jqid report_id)
python3 - "$BASE" "$BASE_ID" <<'PY'
import json, sys, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
base, rid = sys.argv[1], sys.argv[2]

def call(path, body):
    req = urllib.request.Request(
        f"{base}{path}", data=json.dumps(body).encode(),
        method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

def withdraw(i):
    return call(f"/reports/{rid}/withdraw",
                {"withdrawal_no": f"WD-RACE-{i}", "reason": f"并发撤回 {i}"})

def supersede(i):
    return call(f"/reports/{rid}/supersede",
                {"instrument": "HPLC-09", "tested_at": "2026-07-02T08:00:00Z",
                 "reference": f"R-RACE-WD-NEW-{i}", "reason": f"并发复测 {i}"})

with ThreadPoolExecutor(8) as ex:
    fs = [ex.submit(withdraw, i) for i in range(4)]
    fs += [ex.submit(supersede, i) for i in range(4)]
    rs = [f.result() for f in fs]
wd = rs[:4]
sup = rs[4:]
ok = [r for r in rs if r[0] == 201]
assert len(ok) == 1, rs
if [r for r in wd if r[0] == 201]:
    assert all(r[0] == 409 and r[1]["detail"]["code"] == "REPORT_ALREADY_WITHDRAWN"
               for r in wd if r[0] != 201), wd
    assert all(r[0] == 409 and r[1]["detail"]["code"] == "REPORT_WITHDRAWN"
               and r[1]["detail"]["chain_state"]["usable_report_id"] is None
               for r in sup), sup
    print("撤回方胜出：1 成功，其余撤回 409 REPORT_ALREADY_WITHDRAWN，"
          "接替 409 REPORT_WITHDRAWN（usable_report_id=null，未生成新报告）")
else:
    winner = ok[0][1]["report_id"]
    assert all(r[0] == 409 for r in rs if r[0] != 201)
    assert all(r[1]["detail"]["chain_state"]["tip_report_id"] == winner
               for r in rs if r[0] == 409), rs
    print("接替方胜出：新报告 id=%s，撤回与其余接替均 409 并返回当前链状态" % winner)
PY
