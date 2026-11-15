"""需求质检、技术方案质检、需求测试分析与改动影响的跨对象校验（纯函数）。

本模块不重新制定旧评分权重或及格线。旧质量评分由冻结规则（旧校验器桥接或
lab 规则）计算；这里只检查新流程追加的追溯、引文与适用性契约。
"""

from __future__ import annotations

from typing import Any

from qa_flow.contracts.registry import MODEL_KINDS
from qa_flow.domain.evidence import check_citations
from qa_flow.domain.planning import validate_strategy
from qa_flow.errors import issue


def validate_quality_review(p: dict[str, Any], sources: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ref in p["source_refs"]:
        if ref not in sources:
            out.append(issue("payload.source_refs", "引用的来源快照不存在", ref, code="REFERENCE_INVALID"))
    seen = set()
    for i, f in enumerate(p["findings"]):
        if f["id"] in seen:
            out.append(issue(f"payload.findings[{i}].id", "问题 ID 重复"))
        seen.add(f["id"])
        if not f["citations"]:
            out.append(issue(f"payload.findings[{i}].citations", "问题必须可追溯到原文"))
        probs, _ = check_citations(f["citations"], sources, f"payload.findings[{i}].citations")
        out += probs
    return out


def validate_technical_review(p: dict[str, Any], sources: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if p["applicability"] != "provided":
        # 无材料或读取受阻：不允许声明模型完整或检查通过
        if any(c["result"] == "pass" for c in p["checks"]):
            out.append(issue("payload.checks", "技术材料未提供或读取受阻时，不能出现通过的检查"))
        return out
    models = p["models"]
    for kind in MODEL_KINDS:
        m = models.get(kind)
        if not isinstance(m, dict):
            out.append(issue(f"payload.models.{kind}", "六类模型必须逐类给出触发判断"))
            continue
        if "triggered" not in m or not isinstance(m.get("triggered"), bool):
            out.append(issue(f"payload.models.{kind}.triggered", "必须明确是否触发"))
            continue
        entities = m.get("entities") or []
        questions = m.get("questions") or []
        if m["triggered"] and not entities and not questions:
            out.append(issue(f"payload.models.{kind}", "已触发的模型不能是空数组；需要实体或待确认问题"))
        if not m["triggered"] and not (m.get("reason") or "").strip():
            out.append(issue(f"payload.models.{kind}.reason", "未触发的模型必须说明原因"))
        for j, e in enumerate(entities):
            probs, _ = check_citations(e.get("citations") or [], sources, f"payload.models.{kind}.entities[{j}].citations")
            if not e.get("citations"):
                probs.append(issue(f"payload.models.{kind}.entities[{j}].citations", "实体必须有证据"))
            out += probs
    entity_ids = {e["id"] for m in models.values() if isinstance(m, dict) for e in (m.get("entities") or [])}
    for i, e in enumerate((models.get("interface") or {}).get("entities") or []):
        for fref in e.get("db_field_refs") or []:
            if fref not in entity_ids:
                out.append(issue(f"payload.models.interface.entities[{i}].db_field_refs", "引用的数据字段不存在",
                                 fref, code="REFERENCE_INVALID"))
    kinds = {c["kind"] for c in p["checks"]}
    for need in ("model", "contract", "rollback", "verifiability"):
        if need not in kinds:
            out.append(issue("payload.checks", f"缺少 {need} 类检查"))
    for i, s in enumerate(p.get("test_seeds") or []):
        for ref in s.get("model_refs") or []:
            if ref not in entity_ids:
                out.append(issue(f"payload.test_seeds[{i}].model_refs", "种子引用的模型实体不存在", ref,
                                 code="REFERENCE_INVALID"))
    return out


def validate_test_analysis(p: dict[str, Any], sources: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    req_ids = set()
    for i, r in enumerate(p["requirements"]):
        req_ids.add(r["id"])
        probs, _ = check_citations(r["citations"], sources, f"payload.requirements[{i}].citations")
        out += probs
    tp_ids = set()
    covered_reqs = set()
    for i, tp in enumerate(p["test_points"]):
        if tp["id"] in tp_ids:
            out.append(issue(f"payload.test_points[{i}].id", "测试点 ID 重复"))
        tp_ids.add(tp["id"])
        for r in tp["req_ids"]:
            if r not in req_ids:
                out.append(issue(f"payload.test_points[{i}].req_ids", f"引用的需求 {r} 不存在", r,
                                 code="REFERENCE_INVALID"))
            covered_reqs.add(r)
        if tp["readiness"] == "blocked" and not (tp.get("block_reason") or "").strip():
            out.append(issue(f"payload.test_points[{i}].block_reason", "受阻测试点必须写明阻断依据"))
    for r in sorted(req_ids - covered_reqs):
        out.append(issue("payload.requirements", f"需求 {r} 没有任何测试点（反向追溯缺失）", r))
    for i, reg in enumerate(p["regression"]):
        for t in reg["tp_ids"]:
            if t not in tp_ids:
                out.append(issue(f"payload.regression[{i}].tp_ids", f"回归项引用的测试点 {t} 不存在", t,
                                 code="REFERENCE_INVALID"))
    out += validate_strategy(p["strategy"], tp_ids)
    return out


def handoff_seed(analysis: dict[str, Any]) -> dict[str, Any]:
    """正式交接只含已明确且可追溯的测试点；受阻项保留但不进入可执行种子。"""
    ready = [tp for tp in analysis["test_points"] if tp["readiness"] == "ready"]
    blocked = [{"id": tp["id"], "block_reason": tp.get("block_reason"), "block_refs": tp.get("block_refs") or []}
               for tp in analysis["test_points"] if tp["readiness"] == "blocked"]
    return {"ready": [tp["id"] for tp in ready], "blocked": blocked, "is_ready": bool(ready)}


def validate_change_set(p: dict[str, Any], reg_ids: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if p["base_commit"] == p["head_commit"]:
        out.append(issue("payload.head_commit", "base 与 head 相同，不构成改动"))
    for i, c in enumerate(p["callers"]):
        if not c["reg_ids"]:
            out.append(issue(f"payload.callers[{i}].reg_ids", "调用方必须关联回归项"))
        for r in c["reg_ids"]:
            if reg_ids and r not in reg_ids:
                out.append(issue(f"payload.callers[{i}].reg_ids", f"回归项 {r} 不存在", r, code="REFERENCE_INVALID"))
    claims = p.get("claims") or {}
    scope_incomplete = (not p["search_scope"]["complete"]) or bool(p["unknown_scope"])
    if claims.get("no_impact") and scope_incomplete:
        out.append(issue("payload.claims.no_impact", "扫描范围不完整，不能声称无影响"))
    if claims.get("no_impact") and p["callers"]:
        out.append(issue("payload.claims.no_impact", "存在调用方时不能声称无影响"))
    return out
