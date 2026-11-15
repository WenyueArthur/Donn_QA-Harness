"""指标的精确定义（纯函数）。

数据不完整时指标不可计算：输出 value=null、缺项与范围，不补成 0。
"""

from __future__ import annotations

from typing import Any

from qa_flow import clock


def _report(name: str, cohort: str, numerator: Any, denominator: Any, value: Any, unit: str,
            status: str, missing: list[str], filters: dict[str, Any] | None = None,
            extra: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {"metric_name": name, "cohort": cohort, "filters": filters or {}, "numerator": numerator,
           "denominator": denominator, "value": value, "unit": unit, "data_status": status,
           "missing": missing}
    if extra:
        out.update(extra)
    return out


def ai_adoption(data: dict[str, Any]) -> dict[str, Any]:
    """被明确采纳的原始 AI 用例 ID 数 / 冻结生成集合的原始 ID 数。"""
    origins = list(dict.fromkeys(data.get("origin_ids") or []))
    cohort = data.get("cohort", "")
    if not origins:
        return _report("ai_adoption", cohort, 0, 0, None, "ratio", "not_applicable", [])
    disp = {}
    for d in data.get("dispositions") or []:
        disp.setdefault(d["origin"], d)  # 每个原始 ID 最多计一次
    undecided = [o for o in origins if o not in disp or disp[o].get("decision") not in ("accepted", "rejected")]
    accepted = sum(1 for o in origins if disp.get(o, {}).get("decision") == "accepted")
    if undecided:
        return _report("ai_adoption", cohort, accepted, len(origins), None, "ratio", "incomplete",
                       [f"原始 ID {o} 未决定" for o in undecided])
    return _report("ai_adoption", cohort, accepted, len(origins), round(accepted / len(origins), 4), "ratio",
                   "complete", [], extra={"excluded_human_added": data.get("human_added_ids") or []})


def human_modification(data: dict[str, Any]) -> dict[str, Any]:
    """有已核验人工语义修改的原始 AI 用例 ID 数 / 同一冻结生成集合。"""
    origins = list(dict.fromkeys(data.get("origin_ids") or []))
    cohort = data.get("cohort", "")
    if not origins:
        return _report("human_modification", cohort, 0, 0, None, "ratio", "not_applicable", [])
    missing: list[str] = []
    modified: set[str] = set()
    agent_instructed: set[str] = set()
    disp = {d["origin"]: d for d in data.get("dispositions") or []}
    events = data.get("edit_events")
    if events is None:
        # 夹具形式：每个原始 ID 的 human_semantic_change 已由核验后的事件归纳
        for o in origins:
            d = disp.get(o)
            if d is None or "human_semantic_change" not in d:
                missing.append(f"原始 ID {o} 缺少修改记录")
            elif d["human_semantic_change"]:
                modified.add(o)
    else:
        for e in events:
            origin = e.get("origin")
            if origin not in origins:
                continue
            actor = e.get("actor_kind")
            if e.get("change_kind") == "format":
                continue  # 纯格式不计
            if actor == "human" and e.get("verified"):
                modified.add(origin)
            elif actor == "agent_on_human_instruction":
                agent_instructed.add(origin)
            elif actor in (None, "unknown") or not e.get("verified"):
                missing.append(f"事件 {e.get('id')} 作者不明或未核验")
    status = "incomplete" if missing else "complete"
    value = None if missing else round(len(modified) / len(origins), 4)
    return _report("human_modification", cohort, len(modified), len(origins), value, "ratio", status, missing,
                   extra={"agent_on_human_instruction": len(agent_instructed)})


def online_defect_ratio(data: dict[str, Any]) -> dict[str, Any]:
    window = data.get("discovery_window")
    release = data.get("release")
    missing = []
    if not window or not window.get("start") or not window.get("end_exclusive"):
        missing.append("缺少发现窗口")
    if not release:
        missing.append("缺少发布范围")
    if missing:
        return _report("online_defect_ratio", release or "", None, None, None, "ratio", "not_computable", missing)
    valid = [d for d in data.get("defects") or [] if d.get("valid") and not d.get("duplicate_of")]
    if not valid:
        return _report("online_defect_ratio", release, 0, 0, None, "ratio", "not_applicable", [],
                       filters={"window": window})
    online = [d for d in valid if d.get("found_in") == "production"]
    value = len(online) / len(valid)
    return _report("online_defect_ratio", release, len(online), len(valid), round(value, 4), "ratio", "complete",
                   [], filters={"window": window}, extra={"display_percent": f"{value * 100:.2f}"})


def confirmed_escapes(data: dict[str, Any]) -> dict[str, Any]:
    defects = [d for d in data.get("defects") or [] if d.get("valid") and not d.get("duplicate_of")]
    confirmed = [d for d in defects if d.get("confirmed_escape") is True]
    unconfirmed = [d["id"] for d in defects if d.get("found_in") == "production" and d.get("confirmed_escape")
                   is not True]
    by_stage: dict[str, int] = {}
    for d in confirmed:
        st = d.get("should_have_caught_stage", "unknown")
        by_stage[st] = by_stage.get(st, 0) + 1
    return _report("confirmed_escapes", data.get("release", ""), len(confirmed), None, len(confirmed), "count",
                   "complete", [], extra={"by_stage": by_stage, "unconfirmed": unconfirmed,
                                          "note": "不命名为通用“漏测率”"})


def _union_hours(intervals: list[tuple[Any, Any]]) -> float:
    merged: list[list[Any]] = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return sum((e - s).total_seconds() for s, e in merged) / 3600.0


def elapsed_time(data: dict[str, Any]) -> dict[str, Any]:
    start, end = data.get("entry_accepted_at"), data.get("exit_decided_at")
    if not start or not end:
        return _report("elapsed_time", "", None, None, None, "hours", "not_computable", ["缺少提测或准出时刻"])
    s, e = clock.parse_ts(start), clock.parse_ts(end)
    if e < s:
        raise ValueError("非法时序：准出时刻早于提测接收时刻")
    elapsed = (e - s).total_seconds() / 3600.0
    pauses = []
    limits = []
    for p in data.get("pauses") or []:
        if not p.get("start") or not p.get("end"):
            limits.append("存在缺少起止时刻的暂停")
            continue
        ps, pe = clock.parse_ts(p["start"]), clock.parse_ts(p["end"])
        if pe < ps:
            raise ValueError("非法时序：暂停结束早于开始")
        ps, pe = max(ps, s), min(pe, e)
        if pe > ps:
            pauses.append((ps, pe))
    union = _union_hours(pauses)
    active = elapsed - union
    status = "complete" if not limits else "incomplete"
    return _report("elapsed_time", "", None, None, round(elapsed, 4), "hours", status, limits,
                   extra={"elapsed_hours": round(elapsed, 4), "pause_union_hours": round(union, 4),
                          "active_hours": round(active, 4) if not limits else None})


def effort(data: dict[str, Any]) -> dict[str, Any]:
    seen: dict[str, dict[str, Any]] = {}
    for e in data.get("effort_entries") or []:
        if e["hours"] < 0:
            raise ValueError("工时不能为负")
        seen.setdefault(e["id"], e)  # 同事件只计一次
    hours = sum(float(e["hours"]) for e in seen.values())
    wd = data.get("workday_hours")
    days = round(hours / wd, 4) if wd else None
    missing = [] if wd else ["日工时配置缺失，不计算人天"]
    return _report("effort", "", None, None, round(hours, 4), "person_hours", "complete" if wd else "incomplete",
                   missing, extra={"person_hours": round(hours, 4), "person_days": days})


METRICS = {
    "ai_adoption": ai_adoption,
    "human_modification": human_modification,
    "online_defect_ratio": online_defect_ratio,
    "confirmed_escapes": confirmed_escapes,
    "elapsed_time": elapsed_time,
    "effort": effort,
}


def compare(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    """效果比较：任一侧不完整或范围不一致时输出不可比较，不给出改善百分比。"""
    problems = []
    for label, r in (("before", before), ("after", after)):
        if r is None:
            problems.append(f"{label} 缺失")
        elif r.get("data_status") != "complete" or r.get("value") is None:
            problems.append(f"{label} 数据不完整")
    if not problems and before["metric_name"] != after["metric_name"]:
        problems.append("指标不同，不可比较")
    if problems:
        return {"comparable": False, "problems": problems, "delta": None}
    return {"comparable": True, "problems": [], "delta": after["value"] - before["value"],
            "note": "差值仅描述该样本范围，需同时列出样本数、范围、排除规则和可比性限制"}
