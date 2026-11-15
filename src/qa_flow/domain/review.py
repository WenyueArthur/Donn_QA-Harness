"""人工决定、澄清去重与授权适用性（纯函数）。"""

from __future__ import annotations

from typing import Any

from qa_flow import clock
from qa_flow.canonical import sha256_obj

# 环境 → 可接受的决定核验方式
ACCEPTED_VERIFICATION = {
    "lab": {"simulated", "local_attested", "external_verified"},
    "offline": {"simulated", "local_attested", "external_verified"},
    "test": {"external_verified"},
    "staging": {"external_verified"},
    "production": {"external_verified"},
}

# 批量措辞：不能当作逐项决定
GENERIC_APPROVALS = {"全部同意", "都同意", "同意全部", "全部批准", "all", "approve all", "yes to all", "全部"}


def clarification_key(source_object_ref: str, question_ref: str) -> str:
    """按原文对象和问题引用去重；同一问题跨阶段只保留一条主问题。"""
    return sha256_obj([source_object_ref, " ".join(question_ref.split()).lower()])[:16]


def decision_status(decision: dict[str, Any], *, current_subject_shas: dict[str, str | None],
                    now: str | None = None) -> dict[str, Any]:
    """决定是否仍然有效。决定只覆盖所绑定内容；撤销和过期另记。"""
    now = now or clock.now_iso()
    reasons: list[str] = []
    if decision.get("revoked_at"):
        reasons.append(f"已撤销：{decision.get('revoke_reason') or '无说明'}")
    exp = decision.get("expires_at")
    if exp and clock.parse_ts(exp) <= clock.parse_ts(now):
        reasons.append("已过期")
    for subj in decision.get("subjects", []):
        cur = current_subject_shas.get(subj["ref"])
        if cur is None:
            reasons.append(f"绑定对象 {subj['ref']} 已不是当前版本")
        elif cur != subj["sha256"]:
            reasons.append(f"绑定对象 {subj['ref']} 内容已变化")
    return {"valid": not reasons, "reasons": reasons}


def authorization_applies(decision: dict[str, Any], *, plan_sha256: str, environment: str,
                          target_id: str, action_kind: str) -> list[str]:
    """授权是否覆盖该操作计划。内容、目标、环境或范围变化后必须重新授权。"""
    problems: list[str] = []
    if decision.get("result") != "approve":
        problems.append("决定结果不是批准")
    if decision.get("plan_sha256") != plan_sha256:
        problems.append("批准绑定的计划摘要与当前计划不同")
    scope = decision.get("scope", {})
    if scope.get("environment") != environment:
        problems.append(f"批准环境为 {scope.get('environment')}，当前为 {environment}")
    if target_id not in (scope.get("target_ids") or []):
        problems.append("批准范围不包含该目标")
    if action_kind not in (scope.get("action_kinds") or []):
        problems.append("批准范围不包含该操作类型")
    if decision.get("verification") not in ACCEPTED_VERIFICATION.get(environment, set()):
        problems.append(f"{environment} 环境需要外部核验的批准；本地自述不能证明角色身份")
    return problems


def is_generic_answer(text: str) -> bool:
    return " ".join(str(text).split()).lower() in GENERIC_APPROVALS


def group_review_items(items: list[dict[str, Any]], user_role: str) -> dict[str, list[dict[str, Any]]]:
    """按责任角色集中展示；当前用户只能处理自己角色的问题，其他问题保持待处理。"""
    mine, others = [], []
    for it in items:
        (mine if it.get("responsible_role") == user_role else others).append(it)
    return {"mine": mine, "others": others}
