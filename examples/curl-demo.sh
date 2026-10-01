#!/usr/bin/env bash
# 端到端调用示例：登记校准 -> 发布报告（成功/阻断）-> 更正校准 -> 查看快照漂移
# 依赖：curl（jq 可选，没有时用 python -m json.tool）
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { if command -v jq >/dev/null 2>&1; then jq .; else python3 -m json.tool; fi; }

echo "== 1. 登记仪器 HPLC-01 的原始校准（v1，2026-01-01 ~ 2026-07-01，通过） =="
CAL=$(curl -sS -X POST "$BASE/calibrations" -H 'Content-Type: application/json' -d '{
  "instrument": "HPLC-01",
  "valid_from": "2026-01-01T00:00:00Z",
  "valid_to":   "2026-07-01T00:00:00Z",
  "passed": true
}')
echo "$CAL" | pp
CAL_ID=$(echo "$CAL" | python3 -c 'import sys,json;print(json.load(sys.stdin)["calibration"]["id"])')

echo "== 2. 区间重叠的第二条当前校准 -> 409 INTERVAL_OVERLAP =="
curl -sS -o /dev/stderr -w 'HTTP %{http_code}\n' -X POST "$BASE/calibrations" \
  -H 'Content-Type: application/json' -d '{
    "instrument": "HPLC-01",
    "valid_from": "2026-06-01T00:00:00Z",
    "valid_to":   "2026-12-01T00:00:00Z",
    "passed": true
  }'

echo "== 3. 提交检测时间 2026-03-15 的报告 -> 放行，返回所据校准 v1 =="
curl -sS -X POST "$BASE/reports" -H 'Content-Type: application/json' -d '{
  "instrument": "HPLC-01",
  "tested_at": "2026-03-15T08:00:00Z",
  "reference": "R-2026-0001"
}' | pp

echo "== 4. 检测时间落在区间外（端点 valid_to 当天，左闭右开）-> 422 OUTSIDE_VALID_PERIOD =="
curl -sS -o /dev/stderr -w 'HTTP %{http_code}\n' -X POST "$BASE/reports" \
  -H 'Content-Type: application/json' -d '{
    "instrument": "HPLC-01",
    "tested_at": "2026-07-01T00:00:00Z",
    "reference": "R-2026-0002"
  }'

echo "== 5. 更正校准（引用 v1，连续版本号 v2，改为未通过，区间前移） =="
CORR=$(curl -sS -X POST "$BASE/calibrations/corrections" \
  -H 'Content-Type: application/json' -d "{
    \"corrects_id\": $CAL_ID,
    \"version\": 2,
    \"valid_from\": \"2026-02-01T00:00:00Z\",
    \"valid_to\":   \"2026-08-01T00:00:00Z\",
    \"passed\": false
  }")
echo "$CORR" | pp

echo "== 6. 再次提交相同检测时间的报告 -> 422 CALIBRATION_NOT_PASSED =="
curl -sS -o /dev/stderr -w 'HTTP %{http_code}\n' -X POST "$BASE/reports" \
  -H 'Content-Type: application/json' -d '{
    "instrument": "HPLC-01",
    "tested_at": "2026-03-15T08:00:00Z",
    "reference": "R-2026-0003"
  }'

echo "== 7. 错误版本号更正（跳号 v9）-> 409 VERSION_CONFLICT =="
curl -sS -o /dev/stderr -w 'HTTP %{http_code}\n' -X POST "$BASE/calibrations/corrections" \
  -H 'Content-Type: application/json' -d "{
    \"corrects_id\": $CAL_ID,
    \"version\": 9,
    \"valid_from\": \"2026-03-01T00:00:00Z\",
    \"valid_to\":   \"2026-09-01T00:00:00Z\",
    \"passed\": true
  }"

echo "== 8. 重算当前有效性：列出与发布快照结论不同的报告（快照不改写） =="
curl -sS "$BASE/calibrations/drift/recompute" | pp

echo "== 9. 历史版本仍可查（列表含 v1/v2，current_only 仅看最新版） =="
curl -sS "$BASE/calibrations?instrument=HPLC-01" | pp

echo "== 10. 登记仪器禁用区间（事件 DIS-2026-001，[3/1,5/1)，左闭右开） =="
curl -sS -X POST "$BASE/disables" -H 'Content-Type: application/json' -d '{
  "event_no": "DIS-2026-001",
  "instrument": "HPLC-01",
  "disabled_from": "2026-03-01T00:00:00Z",
  "disabled_to":   "2026-05-01T00:00:00Z",
  "reason": "进样阀故障待修"
}' | pp

echo "== 11. 重叠禁用区间 -> 409 DISABLE_INTERVAL_OVERLAP（边界相接允许） =="
curl -sS -o /dev/stderr -w 'HTTP %{http_code}\n' -X POST "$BASE/disables" \
  -H 'Content-Type: application/json' -d '{
    "event_no": "DIS-2026-002",
    "instrument": "HPLC-01",
    "disabled_from": "2026-04-15T00:00:00Z",
    "disabled_to":   "2026-06-01T00:00:00Z",
    "reason": "重复故障登记"
  }'

echo "== 12. 检测时间命中禁用区间 -> 422 INSTRUMENT_DISABLED（禁用判定优先于校准判定） =="
curl -sS -o /dev/stderr -w 'HTTP %{http_code}\n' -X POST "$BASE/reports" \
  -H 'Content-Type: application/json' -d '{
    "instrument": "HPLC-01",
    "tested_at": "2026-03-15T08:00:00Z",
    "reference": "R-2026-0004"
  }'

echo "== 13. 查询当前受禁用影响的已发布报告及禁用依据（原快照不改写） =="
curl -sS "$BASE/disables/affected-reports?instrument=HPLC-01" | pp

echo "== 14. 按事件号撤销禁用（记录保留可追溯） =="
curl -sS -X POST "$BASE/disables/DIS-2026-001/revoke" \
  -H 'Content-Type: application/json' -d '{"reason": "进样阀更换完成"}' | pp

echo "== 15. 撤销后受影响报告查询为空；事件记录仍可查 =="
curl -sS "$BASE/disables/affected-reports?instrument=HPLC-01" | pp
curl -sS "$BASE/disables/DIS-2026-001" | pp

