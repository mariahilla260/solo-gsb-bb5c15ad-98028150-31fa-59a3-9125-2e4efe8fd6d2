#!/usr/bin/env bash
# 排班规划示例：查询 HPLC-01 与 GC-02 在给定时间范围内何时“全组可放行”。
# 演示：校准通过、禁用区间优先、无校准仪器不被掩盖、撤销后重查、入参校验。
# 依赖：curl（jq 可选，没有时用 python -m json.tool）
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { if command -v jq >/dev/null 2>&1; then jq .; else python3 -m json.tool; fi; }

echo "== 1. 两台仪器各登记一条“通过”校准（[2026-01-01, 2026-12-01)） =="
curl -sS -X POST "$BASE/calibrations" -H 'Content-Type: application/json' -d '{
  "instrument": "HPLC-01",
  "valid_from": "2026-01-01T00:00:00Z",
  "valid_to":   "2026-12-01T00:00:00Z",
  "passed": true
}' | pp
curl -sS -X POST "$BASE/calibrations" -H 'Content-Type: application/json' -d '{
  "instrument": "GC-02",
  "valid_from": "2026-01-01T00:00:00Z",
  "valid_to":   "2026-12-01T00:00:00Z",
  "passed": true
}' | pp

echo "== 2. HPLC-01 登记禁用区间 [2026-03-01, 2026-04-01) =="
curl -sS -X POST "$BASE/disables" -H 'Content-Type: application/json' -d '{
  "event_no": "DIS-PLAN-001",
  "instrument": "HPLC-01",
  "disabled_from": "2026-03-01T00:00:00Z",
  "disabled_to":   "2026-04-01T00:00:00Z",
  "reason": "进样阀故障待修"
}' | pp

echo "== 3. 查询两台仪器 2026-02-01~2026-05-01（+08:00）何时全组可放行 =="
echo "    预期：放行 -> [3/1,4/1) 因 HPLC-01 禁用全组不可放行 -> 再放行；"
echo "    首段起点截查询起点，末段终点截查询终点（左闭右开）。"
curl -sS -X POST "$BASE/planning/release-windows" \
  -H 'Content-Type: application/json' \
  -d '{
        "instruments": ["HPLC-01", "GC-02"],
        "start": "2026-02-01T00:00:00+08:00",
        "end":   "2026-05-01T00:00:00+08:00"
      }' | pp

echo "== 4. 纳入一台无任何当前校准的仪器 MS-03：全组恒不可放行，且原因不被掩盖 =="
curl -sS -X POST "$BASE/planning/release-windows" \
  -H 'Content-Type: application/json' \
  -d '{
        "instruments": ["HPLC-01", "GC-02", "MS-03"],
        "start": "2026-04-15T00:00:00Z",
        "end":   "2026-04-20T00:00:00Z"
      }' | pp

echo "== 5. 撤销禁用后重查：3 月窗口恢复全组可放行（记录保留，判定随当前状态） =="
curl -sS -X POST "$BASE/disables/DIS-PLAN-001/revoke" \
  -H 'Content-Type: application/json' -d '{"reason": "进样阀更换完成"}' | pp
curl -sS -X POST "$BASE/planning/release-windows" \
  -H 'Content-Type: application/json' \
  -d '{
        "instruments": ["HPLC-01", "GC-02"],
        "start": "2026-03-10T00:00:00Z",
        "end":   "2026-03-20T00:00:00Z"
      }' | pp

echo "== 6. 入参校验：重复仪器 -> 422 =="
curl -sS -o /dev/stderr -w 'HTTP %{http_code}\n' \
  -X POST "$BASE/planning/release-windows" -H 'Content-Type: application/json' \
  -d '{"instruments": ["HPLC-01", "HPLC-01"],
       "start": "2026-02-01T00:00:00Z", "end": "2026-05-01T00:00:00Z"}'

echo "== 7. 入参校验：起点不早于终点 -> 422 =="
curl -sS -o /dev/stderr -w 'HTTP %{http_code}\n' \
  -X POST "$BASE/planning/release-windows" -H 'Content-Type: application/json' \
  -d '{"instruments": ["HPLC-01"],
       "start": "2026-05-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"}'
