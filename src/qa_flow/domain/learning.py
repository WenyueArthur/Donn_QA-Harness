"""漏测、改进与知识候选（纯函数）。"""

from __future__ import annotations

from typing import Any

HUMAN_REQUIRED_TYPES = {"precedent", "root_cause", "conflict", "open_question"}


def knowledge_gate(candidate: dict[str, Any], evidence_origins: dict[str, str] | None = None) -> dict[str, Any]:
    """候选 → 核对 → 确认 → 应用。冲突、未证实事实与高风险条目要求人工确认。

    evidence_origins：已登记且可读取的证据 → 来源等级。为 None 时不核对证据是否存在（纯规则调用）。
    生产结论只有引用了真实生产来源的有效证据才算已证实；推断或提交者自述“已证实”都要人工确认。
    """
    reasons = []
    if candidate["conflicts"]:
        reasons.append("存在冲突来源")
    for c in candidate["claims"]:
        if c["claim_type"] == "fact" and not c["evidence_refs"]:
            reasons.append(f"事实缺少证据：{c['text'][:30]}")
        if evidence_origins is not None:
            missing = [r for r in c["evidence_refs"] if r not in evidence_origins]
            if missing:
                reasons.append(f"引用的证据不存在或不可读取：{'、'.join(missing)}")
        if c.get("environment") == "production":
            if c["claim_type"] != "fact":
                reasons.append("生产结论仅为推断，未经证实")
            elif evidence_origins is not None:
                if not any(evidence_origins.get(r) == "production" for r in c["evidence_refs"]):
                    reasons.append("生产事实缺少真实生产来源的运行证据（自述“已证实”不算）")
            elif not c.get("verified_runtime"):
                reasons.append("生产事实未经运行证据证实")
    needs_human = candidate["material_type"] in HUMAN_REQUIRED_TYPES or candidate["risk"] == "high" or bool(reasons)
    policy_allowed = (not needs_human) and candidate["confirmation_method"] == "policy"
    return {"needs_human": needs_human, "policy_confirm_allowed": policy_allowed, "reasons": reasons}


def improvement_status(improvement: dict[str, Any], verification_attempts: list[dict[str, Any]],
                       fix_build: str | None) -> str:
    """措施完成与效果已证实分开：只有修复构建上实际通过后才标已验证。"""
    if not improvement["change_refs"]:
        return "proposed"
    ok = [a for a in verification_attempts if a["outcome"] == "pass" and (fix_build is None or a["build"] == fix_build)]
    return "verified" if ok else "implemented_unverified"


def search_knowledge(entries: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    terms = [t for t in query.lower().split() if t]
    out = []
    for e in entries:
        hay = (e.get("text", "") + " " + " ".join(e.get("tags", []))).lower()
        if terms and all(t in hay for t in terms):
            out.append(e)
    return out
