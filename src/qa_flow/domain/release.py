"""上线验证与回滚（纯函数）。生产验证只读；生产回滚不由本系统发起。"""

from __future__ import annotations

from typing import Any

from qa_flow import clock
from qa_flow.errors import issue

ROLLBACK_PARTS = ("code", "config", "database", "data_restore", "verification")


def evaluate_release_check(p: dict[str, Any], evidence_origin: dict[str, str], deployment: dict[str, Any] | None
                           ) -> dict[str, Any]:
    problems: list[dict[str, Any]] = []
    if p["environment"] != "production":
        problems.append(issue("payload.environment", "上线验证只针对生产环境"))
    for i, pr in enumerate(p["probes"]):
        if evidence_origin.get(pr["evidence_ref"]) != "production":
            problems.append(issue(f"payload.probes[{i}].evidence_ref", "探测证据不是来自获准的生产连接器",
                                  pr["evidence_ref"], code="ORIGIN_MISMATCH"))
    start, end = clock.parse_ts(p["window"]["start"]), clock.parse_ts(p["window"]["end"])
    if end <= start:
        problems.append(issue("payload.window", "观察窗口非法"))
    for i, s in enumerate(p["metric_samples"]):
        if evidence_origin.get(s["evidence_ref"]) != "production":
            problems.append(issue(f"payload.metric_samples[{i}].evidence_ref", "指标样本不是生产来源",
                                  s["evidence_ref"], code="ORIGIN_MISMATCH"))
        ts = clock.parse_ts(s["ts"])
        if not (start <= ts < end):
            problems.append(issue(f"payload.metric_samples[{i}].ts", "样本时间不在观察窗口内"))
    if deployment is None:
        problems.append(issue("payload.deployment", "未登记实际部署，不能判定", code="PRECONDITION_FAILED"))
    elif deployment.get("build") != p["actual_build"]:
        problems.append(issue("payload.actual_build", "运行版本与登记部署不一致"))
    verdict = "indeterminate"
    if not problems:
        thresholds = {t["name"]: t for t in p["thresholds"]}
        fails = [pr for pr in p["probes"] if pr["result"] != "pass"]
        breaches = []
        for s in p["metric_samples"]:
            t = thresholds.get(s["name"])
            if t and t["unit"] == s["unit"] and not _cmp(s["value"], t["op"], t["value"]):
                breaches.append(s)
        if fails or breaches:
            verdict = "fail"
        elif p.get("gaps"):
            verdict = "indeterminate"  # 部分样本不能宣称整个窗口正常
        else:
            verdict = "pass"
    return {"problems": problems, "verdict": verdict}


def _cmp(v: float, op: str, t: float) -> bool:
    return {"<=": v <= t, ">=": v >= t, "<": v < t, ">": v > t}[op]


def rollback_feasibility(plan: dict[str, Any]) -> dict[str, Any]:
    covers = plan["covers"]
    missing = [k for k in ROLLBACK_PARTS if not covers.get(k, {}).get("plan", "").strip()]
    irreversible = [k for k in ROLLBACK_PARTS if covers.get(k) and not covers[k]["reversible"]]
    irreversible += [str(x) for x in plan["irreversible_changes"]]
    return {"feasible": not missing and not irreversible, "missing_parts": missing,
            "irreversible": irreversible,
            "note": "方案可行性与演练结果分开记录；不能演练的部分列为未验证"}
