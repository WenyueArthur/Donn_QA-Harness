"""新人可用性验收报告计算（纯函数）。保留失败与全部样本，不以平均耗时代替实测。"""

from __future__ import annotations

import statistics
from typing import Any

from qa_flow import clock


def evaluate(observations: list[dict[str, Any]], spec: dict[str, Any]) -> dict[str, Any]:
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
    checks = {
        "participants": len(participants) >= acc["required_participants"],
        "tasks_per_participant": not missing_cells,
        "independent_completions": completions >= acc["required_independent_completions"],
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
                              "missing_cells": [list(x) for x in missing_cells]},
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
