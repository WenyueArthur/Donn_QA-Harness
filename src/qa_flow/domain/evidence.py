"""来源、引文、事实分层与运行来源（纯函数，不访问数据库）。"""

from __future__ import annotations

from typing import Any

from qa_flow.errors import issue

# 能证明运行事实的证据来源
RUNTIME_ORIGINS = {"local_lab", "test", "production"}
# 证据等级：模拟、实验、测试、生产互不冒充
ORIGIN_RANK = {"upload": 0, "imported": 0, "simulated": 1, "local_lab": 2, "test": 3, "production": 4}


def locate_quote(text: str, quote: str) -> str | None:
    """返回引文所在位置（L起始行-结束行），找不到返回 None。只接受逐字匹配。"""
    q = quote.strip()
    if not q:
        return None
    idx = text.find(q)
    if idx < 0:
        return None
    start = text.count("\n", 0, idx) + 1
    end = start + q.count("\n")
    return f"L{start}" if start == end else f"L{start}-L{end}"


def check_citations(citations: list[dict[str, Any]], sources: dict[str, dict[str, Any]],
                    base_path: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """核对引文。sources: source_ref → {"read_status", "text"}。返回 (问题, 带定位的引文)。"""
    problems: list[dict[str, Any]] = []
    located: list[dict[str, Any]] = []
    for i, c in enumerate(citations):
        p = f"{base_path}[{i}]"
        src = sources.get(c.get("source_ref", ""))
        if src is None:
            problems.append(issue(f"{p}.source_ref", "引用的来源快照不存在", c.get("source_ref", ""),
                                  code="REFERENCE_INVALID"))
            continue
        if src["read_status"] != "available":
            problems.append(issue(f"{p}.source_ref", f"来源状态为 {src['read_status']}，不能作为事实依据",
                                  c["source_ref"], code="SOURCE_UNREADABLE"))
            continue
        loc = locate_quote(src["text"], c.get("quote", ""))
        if loc is None:
            problems.append(issue(f"{p}.quote", "引文未逐字出现在来源快照中", c["source_ref"],
                                  code="CITATION_NOT_FOUND"))
            continue
        if c.get("location") and c["location"] != loc:
            problems.append(issue(f"{p}.location", f"引文位置不符，实际位于 {loc}", c["source_ref"],
                                  code="CITATION_NOT_FOUND"))
            continue
        located.append({**c, "location": loc})
    return problems, located


def classify_claims(claims: list[dict[str, Any]], sources: dict[str, dict[str, Any]],
                    runtime_evidence: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """为每条陈述给出可接受的事实等级。

    - fact 必须有已核验来源；
    - 同一 subject 有矛盾取值时全部转 pending；
    - scope=runtime 的事实必须有运行证据，文档引文不等于运行事实。
    """
    results: list[dict[str, Any]] = []
    by_subject: dict[str, set[str]] = {}
    for c in claims:
        if c.get("subject") is not None and "value" in c:
            by_subject.setdefault(c["subject"], set()).add(repr(c["value"]))
    for i, c in enumerate(claims):
        problems, _ = check_citations(c.get("evidence_refs", []), sources, f"claims[{i}].evidence_refs")
        status = c.get("claim_type", "assumption")
        reasons: list[str] = []
        if problems:
            status = "rejected"
            reasons += [p["message"] for p in problems]
        elif c.get("claim_type") == "fact" and not c.get("evidence_refs"):
            status = "pending"
            reasons.append("事实缺少来源")
        if c.get("subject") is not None and len(by_subject.get(c["subject"], ())) > 1 and status != "rejected":
            status = "pending"
            reasons.append("同一事项存在矛盾来源，需人工确认")
        if c.get("scope") == "runtime" and status == "fact":
            refs = c.get("runtime_evidence_refs") or []
            ok = [r for r in refs if runtime_evidence.get(r, {}).get("origin") in RUNTIME_ORIGINS]
            if not ok:
                status = "inference"
                reasons.append("运行事实缺少运行证据，仅能作为推断")
        results.append({"index": i, "accepted_as": status, "reasons": reasons})
    return results


def origin_satisfies(evidence_origin: str, required_environment: str) -> bool:
    """证据来源是否满足目标环境要求。生产只接受 production，测试只接受 test 或更高的真实来源。"""
    if required_environment == "production":
        return evidence_origin == "production"
    if required_environment == "test":
        return evidence_origin == "test"
    if required_environment == "lab":
        return evidence_origin == "local_lab"
    return False


REPLAY_REQUIRED = ("model.provider", "model.model", "model.revision", "prompt.template_sha256",
                   "prompt.expanded_sha256", "input_sha256")


def provenance_completeness(prov: dict[str, Any]) -> dict[str, Any]:
    """检查运行来源字段；无法取得的标 unknown，不补造，且不可宣称可精确重放。"""
    unknown = []
    for path, value in _walk(prov):
        if value in ("unknown", "", None):
            unknown.append(path)
    # 模型与提示词字段整体缺失（不是写了 unknown）同样视为未知，不能声称可重放
    present = {p for p, _ in _walk(prov)}
    for req in REPLAY_REQUIRED:
        if req not in present and req not in unknown:
            unknown.append(req)
    imported = bool(prov.get("imported"))
    replayable = bool(prov.get("replayable")) and not unknown and not imported
    return {"unknown_fields": unknown, "degraded": bool(unknown) or imported, "replayable": replayable}


def _walk(obj: Any, prefix: str = "") -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += _walk(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list):
        return out
    else:
        out.append((prefix, obj))
    return out


def flow_verification(flows: list[dict[str, Any]], runtime_evidence: dict[str, dict[str, Any]],
                      current_deployment: Any) -> list[dict[str, Any]]:
    """设计流与实测流分开判定。知识检索或错误部署不能证明当前路径。

    current_deployment：当前部署的标识集合（部署快照 ID 与部署 ID 均可）。证据必须产生于流程声明的部署，
    且该部署是当前部署；把其他部署的证据改标为当前部署不能通过。
    """
    if current_deployment is None:
        current: set[str] = set()
    elif isinstance(current_deployment, str):
        current = {current_deployment}
    else:
        current = set(current_deployment)
    out = []
    for f in flows:
        status = "design_only" if f["kind"] == "design" else "unverified"
        missing: list[str] = []
        if f["kind"] == "runtime":
            ev = [runtime_evidence.get(r) for r in f.get("evidence_refs", [])]
            valid = [e for e in ev if e and e.get("origin") in RUNTIME_ORIGINS]
            if not valid:
                status = "indeterminate"
                missing.append(f"缺少 {f['id']} 的运行观测证据")
            elif not current or f.get("deployment_ref") not in current:
                status = "indeterminate"
                missing.append(f"{f['id']} 的证据部署上下文与当前部署不符")
            elif not any(e.get("deployments") is None or current & set(e.get("deployments") or [])
                         for e in valid):
                status = "indeterminate"
                missing.append(f"{f['id']} 的证据产生于其他部署，与当前部署不符")
            else:
                status = "verified"
        out.append({"id": f["id"], "from": f["from"], "to": f["to"], "kind": f["kind"],
                    "status": status, "missing": missing})
    return out
