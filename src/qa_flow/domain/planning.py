"""测试计划、容量、估算与测试策略（纯函数）。"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from qa_flow.errors import issue


def validate_estimate(est: dict[str, Any], path: str) -> list[dict[str, Any]]:
    out = []
    lo, mid, hi = est.get("low"), est.get("likely"), est.get("high")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (lo, mid, hi)):
        return [issue(path, "估算区间必须是数字")]
    if lo < 0 or not (lo <= mid <= hi):
        out.append(issue(path, "估算区间必须满足 0 ≤ low ≤ likely ≤ high"))
    return out


def evaluate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """校验计划并计算容量冲突。估算不是实测工时。"""
    problems: list[dict[str, Any]] = []
    if not plan["estimate_method"].strip() or "固定分钟" in plan["estimate_method"]:
        problems.append(issue("payload.estimate_method", "估算方法必须说明依据，不能用“用例数×固定分钟”"))
    problems += validate_estimate(plan["estimate_range_hours"], "payload.estimate_range_hours")
    ids = set()
    for i, t in enumerate(plan["tasks"]):
        if t["id"] in ids:
            problems.append(issue(f"payload.tasks[{i}].id", "任务 ID 重复"))
        ids.add(t["id"])
        problems += validate_estimate(t["estimate_hours"], f"payload.tasks[{i}].estimate_hours")
    for i, t in enumerate(plan["tasks"]):
        for d in t.get("depends_on") or []:
            if d not in ids:
                problems.append(issue(f"payload.tasks[{i}].depends_on", f"前置任务 {d} 不存在", d))
    demand: dict[tuple[str, str], float] = defaultdict(float)
    for t in plan["tasks"]:
        demand[(t["owner"], t["date"])] += float(t["estimate_hours"]["likely"])
    capacity: dict[tuple[str, str], float] = defaultdict(float)
    for c in plan["capacity"]:
        capacity[(c["person"], c["date"])] += float(c["hours"])
    conflicts = []
    for (person, date), need in sorted(demand.items()):
        have = capacity.get((person, date), 0.0)
        if need > have:
            conflicts.append({"person": person, "date": date, "demand_hours": need, "capacity_hours": have,
                              "over_hours": round(need - have, 2)})
    blocked_deps = [d["id"] for d in plan["dependencies"] if d["status"] != "met"]
    approvable = not problems and not conflicts
    return {"problems": problems, "capacity_conflicts": conflicts, "pending_dependencies": blocked_deps,
            "approvable": approvable, "estimate_is_measurement": False}


def validate_strategy(strategy: dict[str, Any], tp_ids: set[str]) -> list[dict[str, Any]]:
    """测试类型、分层、排除项、自动化候选与重复执行条件均需依据。"""
    out = []
    for i, t in enumerate(strategy["types"]):
        if not t["reason"].strip():
            out.append(issue(f"payload.strategy.types[{i}].reason", "测试类型适用与否必须给出理由"))
    for i, ex in enumerate(strategy["exclusions"]):
        if not ex["reason"].strip():
            out.append(issue(f"payload.strategy.exclusions[{i}].reason", "排除项必须给出理由"))
    for i, a in enumerate(strategy["automation_candidates"]):
        if a["tp_id"] not in tp_ids:
            out.append(issue(f"payload.strategy.automation_candidates[{i}].tp_id", "引用的测试点不存在",
                             a["tp_id"]))
        if a["feasibility"] in ("low", "medium") and not (a.get("reason") or "").strip():
            out.append(issue(f"payload.strategy.automation_candidates[{i}].reason",
                             "低或中等自动化可行性必须说明原因"))
    for i, r in enumerate(strategy["repeat_conditions"]):
        if r["tp_id"] not in tp_ids:
            out.append(issue(f"payload.strategy.repeat_conditions[{i}].tp_id", "引用的测试点不存在", r["tp_id"]))
    return out


def effort_totals(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """实际投入去重并检查同一人重叠申报。"""
    seen: dict[str, dict[str, Any]] = {}
    duplicates = []
    for e in entries:
        if e["entry_id"] in seen:
            duplicates.append(e["entry_id"])
            continue
        seen[e["entry_id"]] = e
    overlaps = []
    by_person: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in seen.values():
        if e.get("start") and e.get("end"):
            by_person[e["person"]].append(e)
    for person, items in by_person.items():
        items.sort(key=lambda x: x["start"])
        for a, b in zip(items, items[1:]):
            if b["start"] < a["end"]:
                overlaps.append({"person": person, "entries": [a["entry_id"], b["entry_id"]]})
    return {"person_hours": sum(float(e["hours"]) for e in seen.values()), "duplicates": duplicates,
            "overlaps": overlaps}
