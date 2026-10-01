#!/usr/bin/env bash
# 报告撤回端到端示例（样本身份错误）：
#   登记校准 -> 发布报告 -> 登记撤回 -> 沿革（链尾/现行分列）->
#   重复撤回被拒 -> 撤回后复测接替被拒 -> 接替后再撤回新链尾 ->
#   同号撤回单不得转移 -> 撤回/接替并发至多一方成功
# 依赖：curl、python3（仅用于格式化 JSON 与提取字段）
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool; }
jqid() { python3 -c 'import sys,json;print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

echo "== 1. 登记校准并发布报告 R-WD-0001 =="
curl -sS -X POST "$BASE/calibrations" -H 'Content-Type: application/json' -d '{
  "instrument": "HPLC-01",
  "valid_from": "2026-01-01T00:00:00Z",
  "valid_to":   "2027-01-01T00:00:00Z",
  "passed": true
}' >/dev/null
RESP=$(curl -sS -X POST "$BASE/reports" -H 'Content-Type: application/json' -d '{
  "instrument": "HPLC-01",
  "tested_at": "2026-03-15T08:00:00Z",
  "reference": "R-WD-0001"
}')
R1=$(echo "$RESP" | jqid report_id)
echo "报告 id=$R1"

echo "== 2. 质控按撤回单号 + 非空原因登记撤回（201） =="
curl -sS -X POST "$BASE/reports/$R1/withdraw" -H 'Content-Type: application/json' -d '{
  "withdrawal_no": "WD-2026-0001",
  "reason": "样本身份错误：与委托单样品编号不一致"
}' | pp

echo "== 3. 沿革：tip_report_id 仍指向链尾，current_report_id 为 null，含撤回单/原因/时间 =="
curl -sS "$BASE/reports/$R1/lineage" | pp

echo "== 4. 重复撤回 -> 409 REPORT_ALREADY_WITHDRAWN（detail 附当前链状态） =="
curl -sS -w '\nHTTP %{http_code}\n' -X POST "$BASE/reports/$R1/withdraw" \
  -H 'Content-Type: application/json' -d '{
    "withdrawal_no": "WD-2026-0002",
    "reason": "再次撤回"
  }'

echo "== 5. 撤回后复测接替必须拒绝 -> 409 REPORT_WITHDRAWN（不生成新报告） =="
curl -sS -w '\nHTTP %{http_code}\n' -X POST "$BASE/reports/$R1/supersede" \
  -H 'Content-Type: application/json' -d '{
    "instrument": "HPLC-01",
    "tested_at": "2026-04-10T08:00:00Z",
    "reference": "R-WD-0001-RE",
    "reason": "撤回后复测"
  }'

echo "== 6. 报告详情/列表仍返回历史内容（报告与快照永久保留） =="
curl -sS "$BASE/reports/$R1" | pp

echo "== 7. 另发一份报告，复测接替后撤回新链尾；撤回非链尾（旧报告）被拒 =="
RESP=$(curl -sS -X POST "$BASE/reports" -H 'Content-Type: application/json' -d '{
  "instrument": "HPLC-01",
  "tested_at": "2026-05-15T08:00:00Z",
  "reference": "R-WD-0002"
}')
B1=$(echo "$RESP" | jqid report_id)
RESP=$(curl -sS -X POST "$BASE/reports/$B1/supersede" -H 'Content-Type: application/json' -d '{
  "instrument": "HPLC-01",
  "tested_at": "2026-05-20T08:00:00Z",
  "reference": "R-WD-0002-RE",
  "reason": "前处理异常，复测"
}')
B2=$(echo "$RESP" | jqid report_id)
echo "-- 撤回非链尾旧报告 $B1 -> 409 REPORT_ALREADY_SUPERSEDED（tip/current 指向 $B2） --"
curl -sS -w '\nHTTP %{http_code}\n' -X POST "$BASE/reports/$B1/withdraw" \
  -H 'Content-Type: application/json' -d '{
    "withdrawal_no": "WD-2026-0010",
    "reason": "撤回非链尾"
  }'
echo "-- 撤回链尾 $B2 成功（201） --"
curl -sS -X POST "$BASE/reports/$B2/withdraw" -H 'Content-Type: application/json' -d '{
  "withdrawal_no": "WD-2026-0010",
  "reason": "新链尾样本身份错误"
}' | pp

echo "== 8. 撤回单号不得转给其他报告/复用 -> 409 WITHDRAWAL_NO_EXISTS =="
RESP=$(curl -sS -X POST "$BASE/reports" -H 'Content-Type: application/json' -d '{
  "instrument": "HPLC-01",
  "tested_at": "2026-06-15T08:00:00Z",
  "reference": "R-WD-0003"
}')
B3=$(echo "$RESP" | jqid report_id)
curl -sS -w '\nHTTP %{http_code}\n' -X POST "$BASE/reports/$B3/withdraw" \
  -H 'Content-Type: application/json' -d '{
    "withdrawal_no": "WD-2026-0010",
    "reason": "把别人的撤回单号转给本报告"
  }'

echo "== 9. 并发：同一链尾撤回与接替赛跑，至多一方成功，败方返回当前链状态 =="
RESP=$(curl -sS -X POST "$BASE/reports" -H 'Content-Type: application/json' -d '{
  "instrument": "HPLC-01",
  "tested_at": "2026-07-15T08:00:00Z",
  "reference": "R-RACE-WD"
}')
BASE_ID=$(echo "$RESP" | jqid report_id)
python3 - "$BASE" "$BASE_ID" <<'PY'
import json, sys, urllib.request, urllib.error
import threading
from concurrent.futures import ThreadPoolExecutor
base, rid = sys.argv[1], sys.argv[2]
barrier = threading.Barrier(8)

def call(path, body):
    barrier.wait()
    req = urllib.request.Request(f"{base}/reports/{rid}{path}",
        data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

def do_withdraw(i):
    return call("/withdraw", {"withdrawal_no": f"WD-RACE-{i}", "reason": f"并发撤回 {i}"})

def do_supersede(i):
    return call("/supersede", {"instrument": "HPLC-01",
                               "tested_at": f"2026-08-0{i+1}T08:00:00Z",
                               "reference": f"R-RACE-WD-RE-{i}", "reason": f"并发接替 {i}"})

with ThreadPoolExecutor(8) as ex:
    futs = [ex.submit(do_withdraw, i) for i in range(4)]
    futs += [ex.submit(do_supersede, i) for i in range(4)]
    rs = [f.result() for f in futs]
wd, sp = rs[:4], rs[4:]
wd_ok = [r for r in wd if r[0] == 201]
sp_ok = [r for r in sp if r[0] == 201]
assert len(wd_ok) + len(sp_ok) == 1, (wd, sp)
if wd_ok:
    assert all(r[0] == 409 and r[1]["detail"]["code"]
               in ("REPORT_ALREADY_WITHDRAWN", "REPORT_WITHDRAWN")
               for r in wd if r[0] != 201), wd
    assert all(r[0] == 409 and r[1]["detail"]["code"] == "REPORT_WITHDRAWN"
               and r[1]["detail"]["current_report_id"] is None
               and r[1]["detail"]["tip_report_id"] == int(rid) for r in sp), sp
    print(f"撤回胜：成功撤回 1 个；其余撤回 {4-len(wd_ok)} 个、接替 4 个全部 409，"
          f"败方 detail.current_report_id=null、tip_report_id={rid}")
else:
    assert all(r[0] == 409 and r[1]["detail"]["code"]
               in ("REPORT_ALREADY_SUPERSEDED",) for r in wd), wd
    assert all(r[0] == 409 and r[1]["detail"]["code"] == "REPORT_ALREADY_SUPERSEDED"
               for r in sp if r[0] != 201), sp
    new_id = sp_ok[0][1]["report_id"]
    assert all(r[1]["detail"]["current_report_id"] == new_id
               and r[1]["detail"]["tip_report_id"] == new_id
               for r in wd + sp if r[0] == 409), (wd, sp)
    print(f"接替胜：成功接替 1 个（新报告 id={new_id}）；撤回 4 个与其余接替全部 409，"
          f"败方 detail.current_report_id=tip_report_id={new_id}")
PY

echo "== 10. 已撤回报告仍出现在漂移与禁用影响追溯中（示例见 affected-reports） =="
curl -sS "$BASE/disables/affected-reports?instrument=HPLC-01" | pp
