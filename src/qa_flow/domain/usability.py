"""新人可用性验收报告计算（纯函数）。保留失败与全部样本，不以平均耗时代替实测。"""

from __future__ import annotations

import statistics
from typing import Any

from qa_flow import clock
from qa_flow.errors import ValidationIssues, issue

_REQUIRED_FIELDS = ("participant_pseudonym", "task_id")
# 决策理解只接受明确的“正确”；其他取值（误读、错误、False）都属于关键误解
_UNDERSTOOD = ("correct", True, None)


def _validate(observations: Any) -> None:
    if not isinstance(observations, list):
        raise ValidationIssues("SCHEMA_INVALID", [issue("observations", "观察记录必须是列表")])
    problems = []
    for i, o in enumerate(observations):
        if not isinstance(o, dict):
            problems.append(issue(f"observations[{i}]", "观察记录必须是对象"))
            continue
        for f in _REQUIRED_FIELDS:
            if not isinstance(o.get(f), str) or not o.get(f):
                problems.append(issue(f"observations[{i}].{f}", f"缺少必填字段 {f}"))
    if problems:
        raise ValidationIssues("SCHEMA_INVALID", problems)


def evaluate(observations: list[dict[str, Any]], spec: dict[str, Any]) -> dict[str, Any]:
    _validate(observations)
    acc = spec["proposed_acceptance"]
    participants = sorted({o["participant_pseudonym"] for o in observations})
    tasks = [t["id"] for t in spec["task_definitions"]]
    completions = sum(1 for o in observations if o.get("independently_completed") is True)
    help_events = sum(int(o.get("help_events") or 0) for o in observations)
    manual = sum(int(o.get("manual_json_or_source_edits") or 0) for o in observations)
    unsafe = sum(int(o.get("unsafe_or_duplicate_effects") or 0) for o in observations)
    repeated = sum(int(o.get("repeated_question_same_valid_scope") or 0) for o in observations)
    durations = []
    for o in observations:
        if o.get("started_at") and o.get("finished_at"):
            durations.append(clock.hours_between(o["started_at"], o["finished_at"]) * 3600)
    missing_cells = [(p, t) for p in participants for t in tasks
                     if not any(o["participant_pseudonym"] == p and o["task_id"] == t for o in observations)]
    resume = [o for o in observations if o["task_id"] == "N5"]
    # 任一记录未独立完成即不通过：同一人重做成功不能抵消原失败，第六人失败也不能被总数掩盖
    failed_records = [i for i, o in enumerate(observations) if o.get("independently_completed") is not True]
    misunderstood = [i for i, o in enumerate(observations) if o.get("decision_understanding") not in _UNDERSTOOD]
    checks = {
        "participants": len(participants) >= acc["required_participants"],
        "tasks_per_participant": not missing_cells,
        "independent_completions": completions >= acc["required_independent_completions"] and not failed_records,
        "decision_understanding": not misunderstood,
        "help": help_events <= acc["max_implementation_help_events"],
        "manual_edits": manual <= acc["max_manual_json_or_source_edits"],
        "unsafe": unsafe <= acc["max_unsafe_or_duplicate_side_effects"],
        "repeated_questions": repeated <= acc["max_repeated_question_same_valid_scope"],
    }
    if not observations:
        verdict = "indeterminate"
    else:
        verdict = "pass" if all(checks.values()) else "fail"
    return {
        "participants": participants,
        "task_definitions": tasks,
        "observations": observations,
        "completion_counts": {"independent": completions, "total_records": len(observations),
                              "required": acc["required_independent_completions"],
                              "missing_cells": [list(x) for x in missing_cells],
                              "failed_record_indexes": failed_records},
        "misunderstanding_record_indexes": misunderstood,
        "help_events": help_events,
        "recovery_results": {"resume_success": sum(1 for o in resume if o.get("resume_success")),
                             "resume_total": len(resume)},
        "timing": {"raw_seconds": durations, "median_seconds": statistics.median(durations) if durations else None,
                   "max_seconds": max(durations) if durations else None,
                   "note": "尚无基线，不给出改善比例"},
        "counts": {"manual_json_or_source_edits": manual, "unsafe_or_duplicate_effects": unsafe,
                   "repeated_question_same_valid_scope": repeated},
        "checks": checks,
        "limitations": spec.get("limitations", ""),
        "verdict": verdict,
    }
