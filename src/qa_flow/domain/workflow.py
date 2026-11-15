"""阶段注册表、状态与判定的分离。

阶段状态：pending/running/blocked/completed/failed/stale/waived。
业务判定：pass/fail/indeterminate/not_applicable，与状态分开。
completed 只用于有有效通过判定的阶段；不适用需有效决定并显示为 waived。
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any


@lru_cache(maxsize=1)
def registry() -> dict[str, Any]:
    data = resources.files("qa_flow.resources").joinpath("stages.json").read_text(encoding="utf-8")
    return json.loads(data)


def stages() -> list[dict[str, Any]]:
    return registry()["stages"]


def stage(stage_id: str) -> dict[str, Any]:
    for s in stages():
        if s["id"] == stage_id:
            return s
    from qa_flow.errors import QAError

    raise QAError("INVALID_ARGUMENT", f"未知阶段: {stage_id}", path="stage")


def stage_ids() -> list[str]:
    return [s["id"] for s in stages()]


def status_for(verdict: str, *, waived: bool = False, blocked: bool = False) -> str:
    if waived:
        return "waived"
    if verdict == "pass":
        return "completed"
    if verdict == "fail":
        return "failed"
    if blocked:
        return "blocked"
    return "pending" if verdict == "indeterminate" else "blocked"


def upstream_satisfied(stage_id: str, statuses: dict[str, str]) -> list[str]:
    """返回未满足的上游阶段。completed 与 waived 视为满足。"""
    s = stage(stage_id)
    return [d for d in s["depends_on"] if statuses.get(d) not in ("completed", "waived")]
