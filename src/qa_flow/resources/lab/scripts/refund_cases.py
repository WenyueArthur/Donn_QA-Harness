"""合成退款实验的受审测试脚本。

只输出观测值，不输出 pass/fail：断言由框架用冻结用例中的预期独立判定。
账务状态通过独立 SQLite 读取连接观测，不信任服务返回的状态。

用法：refund_cases.py --target-dir <部署目录> --case <用例ID> --dataset DS1
      refund_cases.py --target-dir <部署目录> --probe --dataset DS1
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
import threading
from pathlib import Path


def load_service(target_dir: Path):
    spec = importlib.util.spec_from_file_location("lab_refund_service", target_dir / "refund_service.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.RefundService(target_dir)


def observe(target_dir: Path, order: str = "O100") -> dict:
    conn = sqlite3.connect(f"file:{target_dir / 'refund.db'}?mode=ro", uri=True)
    try:
        refunded = conn.execute("SELECT refunded FROM orders WHERE id=?", (order,)).fetchone()[0]
        records = conn.execute("SELECT COUNT(*) FROM refunds WHERE order_id=?", (order,)).fetchone()[0]
        ledger = conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
        return {"refunded": refunded, "records": records, "ledger_entries": ledger}
    finally:
        conn.close()


def case_smoke_1(svc, d):
    st, _ = svc.refund("O100", 3000, "K1", "refund_operator")
    o = observe(d)
    return {"status": st, "refunded_after": o["refunded"], "records_after": o["records"]}


def case_smoke_2(svc, d):
    st, _ = svc.refund("O100", 0, "K3", "refund_operator")
    o = observe(d)
    return {"status": st, "refunded_after": o["refunded"], "records_after": o["records"]}


def case_c1(svc, d):
    s1, b1 = svc.refund("O100", 3000, "K1", "refund_operator")
    s2, b2 = svc.refund("O100", 3000, "K1", "refund_operator")
    o = observe(d)
    return {"first_status": s1, "second_status": s2,
            "same_refund_id": b1.get("refund_id") is not None and b1.get("refund_id") == b2.get("refund_id"),
            "refunded_after": o["refunded"], "records_after": o["records"]}


def case_c2(svc, d):
    st, _ = svc.refund("O100", 1000, "K9", "read_only")
    o = observe(d)
    return {"status": st, "refunded_after": o["refunded"], "records_after": o["records"]}


def case_c3(svc, d):
    seq = [("K1", 3000), ("K1", 3000), ("K1", 4000), ("K2", 8000), ("K3", 0)]
    statuses = []
    refunded = []
    for key, amount in seq:
        st, _ = svc.refund("O100", amount, key, "refund_operator")
        statuses.append(st)
        refunded.append(observe(d)["refunded"])
    return {"statuses": statuses, "refunded_trace": refunded, "records_after": observe(d)["records"]}


def case_c4(svc, d):
    barrier = threading.Barrier(2)
    results = []

    def go(key):
        barrier.wait()
        st, _ = svc.refund("O100", 7000, key, "refund_operator")
        results.append(st)

    ts = [threading.Thread(target=go, args=(k,)) for k in ("P1", "P2")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    o = observe(d)
    return {"success_count": sum(1 for s in results if s == 201), "refunded_after": o["refunded"],
            "records_after": o["records"]}


def case_c5(svc, d):
    st_fail, _ = svc.refund("O100", 2000, "K5", "refund_operator", fail_after_balance=True)
    after_fail = observe(d)
    st_ok, body = svc.refund("O100", 2000, "K6", "refund_operator")
    c1 = svc.callback("EV-1", body.get("refund_id", ""))
    c2 = svc.callback("EV-1", body.get("refund_id", ""))
    o = observe(d)
    return {"failed_status": st_fail, "refunded_after_failure": after_fail["refunded"],
            "records_after_failure": after_fail["records"], "ok_status": st_ok,
            "callback_statuses": [c1, c2], "ledger_entries": o["ledger_entries"]}


CASES = {"SMOKE-1": case_smoke_1, "SMOKE-2": case_smoke_2, "C1": case_c1, "C2": case_c2, "C3": case_c3,
         "C4": case_c4, "C5": case_c5}


def probe(svc, d, dataset):
    checks = {}
    checks["version"] = {"observed": svc.build, "commit": svc.version.get("commit")}
    svc.reset(dataset)
    o = observe(d)
    checks["data"] = {"dataset": dataset, "O100_refunded": o["refunded"], "records": o["records"]}
    checks["account"] = {"roles": ["refund_operator", "read_only"]}
    checks["observability"] = {"independent_read": True}
    svc.refund("O100", 100, "PROBE", "refund_operator")
    svc.reset(dataset)
    o2 = observe(d)
    checks["cleanup"] = {"restored": o2 == o}
    return checks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-dir", required=True)
    ap.add_argument("--dataset", default="DS1")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--case")
    g.add_argument("--probe", action="store_true")
    a = ap.parse_args()
    d = Path(a.target_dir)
    svc = load_service(d)
    if a.probe:
        print(json.dumps({"probe": probe(svc, d, a.dataset), "build": svc.build}, ensure_ascii=False))
        return 0
    fn = CASES.get(a.case)
    if fn is None:
        print(json.dumps({"error": f"unknown case {a.case}"}))
        return 2
    svc.reset(a.dataset)
    obs = fn(svc, d)
    svc.reset(a.dataset)  # 清理：恢复初态
    print(json.dumps({"case": a.case, "build": svc.build, "dataset": a.dataset, "observation": obs},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
