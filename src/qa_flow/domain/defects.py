"""缺陷状态机、平台事件处理与关闭条件（纯函数）。"""

from __future__ import annotations

from typing import Any

TRANSITIONS = {
    "proposed": {"created"},
    "created": {"in_progress", "disposition_pending"},
    "in_progress": {"resolved", "disposition_pending"},
    "resolved": {"retesting", "disposition_pending"},
    "retesting": {"closed", "reopened"},
    "reopened": {"in_progress"},
    "disposition_pending": {"rejected", "deferred", "accepted_risk"},
}
TERMINAL = {"closed", "rejected", "deferred", "accepted_risk"}
OPEN_STATES = {"proposed", "created", "in_progress", "resolved", "retesting", "reopened",
               "disposition_pending", "unknown"}
DISPOSITIONS = {"rejected", "deferred", "accepted_risk"}


def can_transition(src: str, dst: str) -> bool:
    return dst in TRANSITIONS.get(src, set())


def map_external_status(external: str, mapping: dict[str, str]) -> str:
    """先探测真实平台状态再按配置映射；无映射时为 unknown，不假设 RESOLVED 等名称。"""
    return mapping.get(external, "unknown")


def apply_platform_event(defect: dict[str, Any], event: dict[str, Any], seen_event_ids: set[str],
                         mapping: dict[str, str]) -> dict[str, Any]:
    """重复事件去重；旧修订不覆盖新状态。返回 {applied, reason, state}。"""
    if event["event_id"] in seen_event_ids:
        return {"applied": False, "reason": "duplicate", "state": defect}
    rev = event.get("platform_revision")
    cur_rev = defect.get("platform_revision") or 0
    if rev is None:
        return {"applied": False, "reason": "no_revision_needs_readback", "state": defect}
    if rev <= cur_rev:
        return {"applied": False, "reason": "out_of_order", "state": defect}
    new = dict(defect)
    new["external_status"] = event["status"]
    new["status"] = map_external_status(event["status"], mapping)
    new["platform_revision"] = rev
    return {"applied": True, "reason": "applied", "state": new}


def close_requirements(defect: dict[str, Any], retests: list[dict[str, Any]],
                       neighbor_results: list[dict[str, Any]], neighbor_required: list[str],
                       close_decision_valid: bool) -> dict[str, Any]:
    """关闭必须关联修复构建、原问题复测与策略要求的相邻回归；不能仅凭已修复状态关闭。"""
    missing = []
    fix_build = defect.get("fix_build")
    if not defect.get("fix_commit") or not fix_build:
        missing.append("缺少修复提交或修复构建")
    original = [r for r in retests if r["tc_ref"] == defect.get("tc_ref") and r["build"] == fix_build]
    if not original:
        missing.append("缺少修复构建上的原问题复测")
    elif original[-1]["outcome"] != "pass":
        missing.append("原问题复测未通过")
    by_tc = {}
    for r in neighbor_results:
        if r["build"] == fix_build:
            by_tc[r["tc_ref"]] = r
    for tc in neighbor_required:
        r = by_tc.get(tc)
        if r is None:
            missing.append(f"相邻回归 {tc} 未在修复构建执行")
        elif r["outcome"] != "pass":
            missing.append(f"相邻回归 {tc} 未通过")
    if not close_decision_valid:
        missing.append("缺少有效的关闭授权")
    return {"can_close": not missing, "missing": missing}


def extended_retest_verdict(original_pass: bool, neighbor_rows: list[dict[str, Any]]) -> dict[str, Any]:
    neighbor_fail = [r for r in neighbor_rows if r["status"] != "pass"]
    return {"original_verified": original_pass, "neighbor_failures": neighbor_fail,
            "fully_verified": original_pass and not neighbor_fail}


def overdue(defect: dict[str, Any], now: str, deadline_hours: float) -> str | None:
    """返回截止时刻（ISO）若已到期。超时按配置计算，通知走统一 broker。"""
    from qa_flow import clock

    if defect.get("status") in TERMINAL or not defect.get("created_at"):
        return None
    deadline = clock.plus_hours(defect["created_at"], deadline_hours)
    return deadline if clock.parse_ts(now) >= clock.parse_ts(deadline) else None
