#!/usr/bin/env bash
# 报告接替（复测）端到端示例：
#   登记校准 -> 发布报告 -> 复测接替 -> 重复接替被拒 -> 沿革查询 -> 并发接替
# 依赖：curl、python3（仅用于格式化 JSON 与提取字段）
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool; }
jqid() { python3 -c 'import sys,json;print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

echo "== 1. 登记校准（HPLC-01，2026 全年通过） =="
curl -sS -X POST "$BASE/calibrations" -H 'Content-Type: application/json' -d '{
  "instrument": "HPLC-01",
  "valid_from": "2026-01-01T00:00:00Z",
  "valid_to":   "2027-01-01T00:00:00Z",
  "passed": true
}' | pp

echo "== 2. 首次发布报告 R-2026-0001 =="
RESP=$(curl -sS -X POST "$BASE/reports" -H 'Content-Type: application/json' -d '{
  "instrument": "HPLC-01",
  "tested_at": "2026-03-15T08:00:00Z",
  "reference": "R-2026-0001"
}')
echo "$RESP" | pp
R1=$(echo "$RESP" | jqid report_id)

echo "== 3. 复测并接替报告 $R1（新编号 R-2026-0001-RE，接替原因必填） =="
RESP=$(curl -sS -X POST "$BASE/reports/$R1/supersede" -H 'Content-Type: application/json' -d '{
  "instrument": "HPLC-01",
  "tested_at": "2026-04-10T08:00:00Z",
  "reference": "R-2026-0001-RE",
  "reason": "初测样品前处理异常，复测"
}')
echo "$RESP" | pp
R2=$(echo "$RESP" | jqid report_id)

echo "== 4. 旧报告已被接替：再次接替 -> 409 REPORT_ALREADY_SUPERSEDED（含现行报告标识） =="
curl -sS -w '\nHTTP %{http_code}\n' -X POST "$BASE/reports/$R1/supersede" \
  -H 'Content-Type: application/json' -d '{
    "instrument": "HPLC-01",
    "tested_at": "2026-04-11T08:00:00Z",
    "reference": "R-2026-0001-RE2",
    "reason": "重复接替"
  }'

echo "== 5. 新报告沿用旧编号 -> 409 REPORT_REFERENCE_EXISTS（旧编号不得转移或复用） =="
curl -sS -w '\nHTTP %{http_code}\n' -X POST "$BASE/reports/$R2/supersede" \
  -H 'Content-Type: application/json' -d '{
    "instrument": "HPLC-01",
    "tested_at": "2026-05-01T08:00:00Z",
    "reference": "R-2026-0001",
    "reason": "沿用旧编号"
  }'

echo "== 6. 复测时点命中禁用区间 -> 422 INSTRUMENT_DISABLED（旧报告仍有效，不留接替记录） =="
curl -sS -X POST "$BASE/disables" -H 'Content-Type: application/json' -d '{
  "event_no": "DIS-2026-050", "instrument": "HPLC-01",
  "disabled_from": "2026-05-01T00:00:00Z", "disabled_to": "2026-06-01T00:00:00Z",
  "reason": "检测器故障"
}' | pp
curl -sS -w '\nHTTP %{http_code}\n' -X POST "$BASE/reports/$R2/supersede" \
  -H 'Content-Type: application/json' -d '{
    "instrument": "HPLC-01",
    "tested_at": "2026-05-10T08:00:00Z",
    "reference": "R-2026-0002",
    "reason": "故障期复测"
  }'

echo "== 7. 撤销禁用后复测接替成功（链继续增长） =="
curl -sS -X POST "$BASE/disables/DIS-2026-050/revoke" \
  -H 'Content-Type: application/json' -d '{"reason": "检测器更换完成"}' | pp
RESP=$(curl -sS -X POST "$BASE/reports/$R2/supersede" -H 'Content-Type: application/json' -d '{
  "instrument": "HPLC-01",
  "tested_at": "2026-06-10T08:00:00Z",
  "reference": "R-2026-0002",
  "reason": "修复后复测"
}')
echo "$RESP" | pp
R3=$(echo "$RESP" | jqid report_id)

echo "== 8. 按链内任一报告 ID 查完整沿革（含每次接替原因与当前有效报告） =="
curl -sS "$BASE/reports/$R1/lineage" | pp
curl -sS "$BASE/reports/$R3/lineage" | pp

echo "== 9. 并发接替同一旧报告：10 个并发请求至多 1 个成功 =="
RESP=$(curl -sS -X POST "$BASE/reports" -H 'Content-Type: application/json' -d '{
  "instrument": "HPLC-01", "tested_at": "2026-07-01T08:00:00Z", "reference": "R-RACE-BASE"
}')
BASE_ID=$(echo "$RESP" | jqid report_id)
python3 - "$BASE" "$BASE_ID" <<'PY'
import json, sys, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
base, rid = sys.argv[1], sys.argv[2]
def sup(i):
    req = urllib.request.Request(f"{base}/reports/{rid}/supersede",
        data=json.dumps({"instrument": "HPLC-01", "tested_at": "2026-07-02T08:00:00Z",
                         "reference": f"R-RACE-{i}", "reason": f"并发复测 {i}"}).encode(),
        method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())
with ThreadPoolExecutor(10) as ex:
    rs = list(ex.map(sup, range(10)))
ok = [r for r in rs if r[0] == 201]
losers = [r for r in rs if r[0] != 201]
assert len(ok) == 1, rs
assert all(r[0] == 409 and r[1]["detail"]["code"] == "REPORT_ALREADY_SUPERSEDED"
           and r[1]["detail"]["current_report_id"] == ok[0][1]["report_id"]
           for r in losers), rs
print(f"成功 {len(ok)} 个（新报告 id={ok[0][1]['report_id']}），"
      f"失败 {len(losers)} 个（均为 409 REPORT_ALREADY_SUPERSEDED，"
      f"detail.current_report_id 指向胜者新报告）")
PY

echo "== 10. 旧版报告与快照仍可查（永久保留，不改写） =="
curl -sS "$BASE/reports/$R1" | pp
