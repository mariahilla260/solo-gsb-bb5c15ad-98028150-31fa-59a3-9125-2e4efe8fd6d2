"""端到端调用示例（标准库即可运行）：

    BASE_URL=http://localhost:8000 python3 examples/demo.py

演示：校准登记 -> 区间冲突 -> 放行/阻断 -> 并发更正的版本冲突 -> 更正后漂移。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")


def call(method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def show(title: str, status: int, body: dict) -> None:
    print(f"\n== {title} -> HTTP {status} ==")
    print(json.dumps(body, indent=2, ensure_ascii=False))


def main() -> None:
    status, body = call("POST", "/calibrations", {
        "instrument": "GC-02",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": "2026-07-01T00:00:00Z",
        "passed": True,
    })
    show("登记校准 v1（通过）", status, body)
    cal_id = body["calibration"]["id"]

    status, body = call("POST", "/calibrations", {
        "instrument": "GC-02",
        "valid_from": "2026-06-01T00:00:00Z",
        "valid_to": "2026-12-01T00:00:00Z",
        "passed": True,
    })
    show("重叠区间登记（应 409）", status, body)

    status, body = call("POST", "/reports", {
        "instrument": "GC-02",
        "tested_at": "2026-03-15T08:00:00Z",
        "reference": "GC-REP-1",
    })
    show("发布报告（应 201，依据 v1）", status, body)

    # 并发提交两个 v2 更正：至多一个成功，另一个必然 409 VERSION_CONFLICT
    def make_correction(passed: bool):
        return call("POST", "/calibrations/corrections", {
            "corrects_id": cal_id,
            "version": 2,
            "valid_from": "2026-02-01T00:00:00Z",
            "valid_to": "2026-08-01T00:00:00Z",
            "passed": passed,
        })

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda p: make_correction(p), [True, False]))
    for i, (status, body) in enumerate(results, 1):
        show(f"并发更正 #{i}（恰一个成功）", status, body)

    status, body = call("GET", "/calibrations/drift/recompute", None)
    show("更正后重算：与快照结论不同的报告", status, body)

    status, body = call("GET", "/calibrations?instrument=GC-02", None)
    show("该校准全部版本（旧版保留可查）", status, body)

    # ---- 仪器禁用区间 ----
    status, body = call("POST", "/disables", {
        "event_no": "DIS-GC-2026-001",
        "instrument": "GC-02",
        "disabled_from": "2026-03-01T00:00:00Z",
        "disabled_to": "2026-05-01T00:00:00Z",
        "reason": "进样阀故障待修",
    })
    show("登记禁用区间（左闭右开，唯一事件号）", status, body)

    status, body = call("POST", "/disables", {
        "event_no": "DIS-GC-2026-002",
        "instrument": "GC-02",
        "disabled_from": "2026-04-01T00:00:00Z",
        "disabled_to": "2026-04-10T00:00:00Z",
        "reason": "重叠登记",
    })
    show("重叠禁用区间（应 409 DISABLE_INTERVAL_OVERLAP）", status, body)

    status, body = call("POST", "/reports", {
        "instrument": "GC-02",
        "tested_at": "2026-03-15T08:00:00Z",
        "reference": "GC-REP-2",
    })
    show("命中禁用区间发布（应 422 INSTRUMENT_DISABLED，含禁用依据）", status, body)

    status, body = call("GET", "/disables/affected-reports?instrument=GC-02", None)
    show("当前受禁用影响的已发布报告（原快照仍为放行）", status, body)

    status, body = call("POST", "/disables/DIS-GC-2026-001/revoke",
                        {"reason": "进样阀更换完成"})
    show("按事件号撤销禁用（记录保留）", status, body)

    status, body = call("GET", "/disables/affected-reports?instrument=GC-02", None)
    show("撤销后受影响报告为空（原报告与快照不改写）", status, body)


if __name__ == "__main__":
    main()
