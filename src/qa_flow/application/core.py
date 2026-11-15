"""来源、产物登记、依赖有效性与失效传播。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from qa_flow import clock
from qa_flow.application.context import Ctx
from qa_flow.contracts import shape
from qa_flow.contracts.registry import payload_spec
from qa_flow.domain import cases as dcases
from qa_flow.domain import evidence as dev
from qa_flow.domain import planning as dplan
from qa_flow.domain import quality as dq
from qa_flow.errors import QAError, ValidationIssues, issue
from qa_flow.security.resources import resolve_identity
from qa_flow.storage.store import Tx, lineage_key

TEXT_SUFFIXES = {".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain", ".json": "application/json"}


# ---------------------------------------------------------------------------
# 来源
# ---------------------------------------------------------------------------

def _read_roots_ok(ctx: Ctx, path: Path) -> bool:
    roots = (ctx.config.get("workspace") or {}).get("source_read_roots") or []
    if not roots:
        return ctx.is_lab  # 真实项目必须显式配置允许读取的原文根
    for r in roots:
        base = (ctx.ws.config_dir / r).resolve()
        try:
            path.resolve().relative_to(base)
            return True
        except ValueError:
            continue
    return False


def import_source(tx: Tx, ctx: Ctx, story_id: str, uri: str) -> dict[str, Any]:
    """登记来源快照。读取受阻与不存在不同；同名不合并；字节不变时复用当前快照。"""
    identity = resolve_identity(uri)
    lineage = lineage_key("source_revision", story_id, identity)
    current_id = tx.lineage_current(lineage)
    max_age = (ctx.config.get("timing") or {}).get("source_max_age_hours")
    payload: dict[str, Any] = {"uri": uri, "fetched_at": tx.now, "freshness_policy": {"max_age_hours": max_age},
                               "read_scope": "full"}
    data: bytes | None = None
    if identity.startswith("file:"):
        path = Path(identity[len("file:"):])
        payload["kind"] = "local_file"
        if not path.exists():
            payload["read_status"] = "missing"
            payload["failure_reason"] = "文件不存在"
        elif not _read_roots_ok(ctx, path):
            payload["read_status"] = "unreadable"
            payload["failure_reason"] = "文件不在已配置的允许读取根内"
        else:
            try:
                data = path.read_bytes()
                payload["read_status"] = "available"
            except PermissionError:
                payload["read_status"] = "unreadable"
                payload["failure_reason"] = "没有读取权限"
            except OSError as exc:
                payload["read_status"] = "unreadable"
                payload["failure_reason"] = f"读取失败：{exc.__class__.__name__}"
    else:
        payload["kind"] = "remote_doc"
        payload["read_status"] = "unreadable"
        payload["failure_reason"] = "没有已配置并核验的文档连接器；不会改用其他工具或账号"
    max_bytes = int(ctx.budgets.get("max_request_bytes", 1 << 20)) * 8
    if data is not None and len(data) > max_bytes:
        payload["read_status"] = "unreadable"
        payload["failure_reason"] = f"来源超过私有存储限额（{len(data)} 字节）"
        data = None
    if data is not None:
        media = TEXT_SUFFIXES.get(Path(identity).suffix.lower(), "application/octet-stream")
        ev = tx.register_evidence(data, origin="imported", source=uri, media_type=media, privacy="private")
        payload["content_ref"] = ev["id"]
        payload["content_sha256"] = ev["sha256"]
        payload["extraction_method"] = "utf-8 原文" if media.startswith("text/") or media.endswith("json") else "未支持"
        payload["title"] = _title(data) or Path(identity).name
        if current_id:
            cur = tx.get_snapshot(current_id)
            if cur and cur["payload"].get("content_sha256") == ev["sha256"] and cur["payload"]["read_status"] == "available":
                touch_source(tx, current_id)
                return {"source_ref": current_id, "changed": False, "read_status": "available",
                        "title": cur["payload"].get("title"), "identity": identity}
    elif current_id:
        # 读取失败：保留旧快照为历史，新状态如实登记，不把失败当作“未变更”
        payload["title"] = (tx.get_snapshot(current_id) or {}).get("payload", {}).get("title")
    shape_problems = shape.check(payload, payload_spec("source_revision"))
    if shape_problems:
        raise ValidationIssues("SCHEMA_INVALID", shape_problems)
    snap = tx.put_snapshot("source_revision", payload, story_id=story_id, project_id=ctx.project_id,
                           producer={"kind": "adapter", "name": "local_source_reader", "verification": "process"},
                           lineage=lineage, parent_id=current_id, privacy="private", origin="imported",
                           provenance={"identity": identity})
    tx.put_agg(f"protected:{identity}", "protected_resource", story_id,
               {"identity": identity, "uri": uri, "source_ref": snap["id"]}, event_type="resource_protected")
    if current_id:
        propagate(tx, ctx, story_id, reason=f"来源 {uri} 已更新为新快照")
    return {"source_ref": snap["id"], "changed": bool(current_id), "read_status": payload["read_status"],
            "title": payload.get("title"), "identity": identity, "failure_reason": payload.get("failure_reason")}


def touch_source(tx: Tx, source_id: str) -> None:
    tx.put_agg(f"freshness:{source_id}", "source_freshness", tx.get_snapshot(source_id)["story_id"],
               {"source_ref": source_id, "verified_at": tx.now}, event_type="source_verified")


def _title(data: bytes) -> str | None:
    try:
        for line in data.decode("utf-8").splitlines():
            s = line.strip()
            if s.startswith("#"):
                return s.lstrip("#").strip()[:80]
    except UnicodeDecodeError:
        return None
    return None


def source_state(tx: Tx, ctx: Ctx, snap: dict[str, Any]) -> str:
    """可用于形成新结论的来源状态：available/unreadable/missing/stale。"""
    p = snap["payload"]
    if p["read_status"] != "available":
        return p["read_status"]
    if not tx.is_current(snap["id"]):
        return "stale"
    max_age = (p.get("freshness_policy") or {}).get("max_age_hours")
    fr = tx.get_agg(f"freshness:{snap['id']}")
    verified = fr["state"]["verified_at"] if fr else p["fetched_at"]
    if max_age and clock.hours_between(verified, tx.now) > float(max_age):
        return "stale"
    return "available"


def source_map(tx: Tx, ctx: Ctx, story_id: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for s in tx.snapshots(story_id, "source_revision"):
        state = source_state(tx, ctx, s)
        text = ""
        if s["payload"].get("content_ref") and s["payload"]["read_status"] == "available":
            try:
                text = tx.read_evidence(s["payload"]["content_ref"]).decode("utf-8")
            except (QAError, UnicodeDecodeError):
                state = "unreadable"
        out[s["id"]] = {"read_status": state, "text": text, "uri": s["payload"]["uri"],
                        "title": s["payload"].get("title")}
    return out


def protected_identities(tx: Tx) -> set[str]:
    out = {a["state"]["identity"] for a in tx.aggs("protected_resource")}
    return out


# ---------------------------------------------------------------------------
# 有效性：快照是否仍覆盖当前上下文（递归检查强依赖）
# ---------------------------------------------------------------------------

def effective_current(tx: Tx, sid: str, memo: dict[str, bool] | None = None) -> bool:
    memo = {} if memo is None else memo
    if sid in memo:
        return memo[sid]
    memo[sid] = True  # 防环
    snap = tx.get_snapshot(sid)
    if snap is None:
        memo[sid] = False
        return False
    ok = tx.is_current(sid)
    if ok and snap["kind"] == "decision":
        ok = decision_valid(tx, sid)["valid"]
    if ok:
        for dep in snap["envelope"]["depends_on"]:
            if not dep.get("required", True):
                continue
            ref = dep["ref"]
            if ref.startswith("EV-"):
                if not tx.evidence_valid(ref):
                    ok = False
                    break
                continue
            dsnap = tx.get_snapshot(ref)
            if dsnap is None or (dep.get("sha256") and dsnap["sha256"] != dep["sha256"]):
                ok = False
                break
            if not effective_current(tx, ref, memo):
                ok = False
                break
    memo[sid] = ok
    return ok


def decision_valid(tx: Tx, decision_id: str) -> dict[str, Any]:
    from qa_flow.domain.review import decision_status

    row = tx.conn.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
    if row is None:
        return {"valid": False, "reasons": ["决定不存在"]}
    import json as _json

    d = dict(row)
    d["subjects"] = _json.loads(d["subjects"])
    cur: dict[str, str | None] = {}
    for s in d["subjects"]:
        snap = tx.get_snapshot(s["ref"])
        if snap is None:
            cur[s["ref"]] = None
            continue
        memo: dict[str, bool] = {decision_id: True}
        cur[s["ref"]] = snap["sha256"] if effective_current(tx, s["ref"], memo) else None
    return decision_status(d, current_subject_shas=cur, now=tx.now)


def dep(tx: Tx, ref: str, relation: str = "input", required: bool = True) -> dict[str, Any]:
    if ref.startswith("EV-"):
        ev = tx.get_evidence(ref)
        if ev is None:
            raise QAError("REFERENCE_INVALID", f"证据不存在: {ref}", related_refs=[ref])
        return {"ref": ref, "sha256": ev["sha256"], "relation": relation, "required": required}
    snap = tx.require_snapshot(ref)
    return {"ref": ref, "sha256": snap["sha256"], "relation": relation, "required": required}


def propagate(tx: Tx, ctx: Ctx, story_id: str, reason: str) -> list[str]:
    """重新核验当前阶段结论：输入、规则、配置或决定变化后标记 stale。原始记录不删除。"""
    changed = []
    for agg in tx.aggs("stage", story_id):
        st = agg["state"]
        if st.get("status") not in ("completed", "failed", "waived", "blocked", "pending"):
            continue
        if not st.get("evaluation_ref"):
            continue
        reasons = stale_reasons(tx, ctx, st)
        if reasons and st.get("status") != "stale":
            new = dict(st)
            new["status"] = "stale"
            new["stale_reasons"] = reasons + [reason]
            new["previous_status"] = st.get("status")
            tx.put_agg(agg["object_id"], "stage", story_id, new, expected_revision=agg["revision"],
                       event_type="stage_stale", event_payload={"reasons": new["stale_reasons"]})
            changed.append(st["stage"])
    for item in tx.aggs("review_item", story_id):
        s = item["state"]
        if s["status"] in ("pending", "transferred") and not all(
                effective_current(tx, r["ref"]) for r in s.get("subject_refs", []) if not r["ref"].startswith("EV-")):
            s = dict(s)
            s["status"] = "stale"
            tx.put_agg(item["object_id"], "review_item", story_id, s, expected_revision=item["revision"],
                       event_type="review_item_stale", event_payload={"reason": reason})
    return changed


def explain_ineffective(tx: Tx, sid: str, seen: set[str] | None = None) -> str | None:
    """沿依赖查找对象失效的根因：被新版本替代、证据销毁/缺失/摘要不符或所依赖对象内容变化。"""
    seen = set() if seen is None else seen
    if sid in seen:
        return None
    seen.add(sid)
    snap = tx.get_snapshot(sid)
    if snap is None:
        return f"{sid} 不存在"
    if not tx.is_current(sid):
        return f"{sid} 已被新版本替代"
    for d in snap["envelope"]["depends_on"]:
        if not d.get("required", True):
            continue
        ref = d["ref"]
        if ref.startswith("EV-"):
            why = tx.evidence_problem(ref)
            if why:
                return f"所依赖的证据 {ref} {why}，不再可读取"
            continue
        dsnap = tx.get_snapshot(ref)
        if dsnap is None:
            return f"所依赖的 {ref} 不存在"
        if d.get("sha256") and dsnap["sha256"] != d["sha256"]:
            return f"所依赖的 {ref} 内容已变化"
        deeper = explain_ineffective(tx, ref, seen)
        if deeper:
            return deeper
    return None


def stale_reasons(tx: Tx, ctx: Ctx, stage_state: dict[str, Any]) -> list[str]:
    reasons = []
    if stage_state.get("rules_sha256") and stage_state["rules_sha256"] != ctx.rules_sha:
        reasons.append("冻结规则已变化")
    if stage_state.get("config_sha256") and stage_state["config_sha256"] != ctx.config_sha:
        reasons.append("配置已变化")
    memo: dict[str, bool] = {}
    for ref in stage_state.get("inputs", []):
        if ref.startswith("EV-"):
            why = tx.evidence_problem(ref)
            if why:
                reasons.append(f"证据 {ref} 已失效：{why}，不再可读取")
            continue
        if not effective_current(tx, ref, memo):
            cause = explain_ineffective(tx, ref)
            reasons.append(f"输入 {ref} 已不是当前有效版本" + (f"：{cause}" if cause else ""))
    for ref in stage_state.get("decisions", []):
        st = decision_valid(tx, ref)
        if not st["valid"]:
            reasons.append(f"决定 {ref} 已失效：{'；'.join(st['reasons'])}")
    return reasons


# ---------------------------------------------------------------------------
# 产物登记
# ---------------------------------------------------------------------------

def _raise_issues(problems: list[dict[str, Any]]) -> None:
    if not problems:
        return
    codes = {p.get("code") for p in problems}
    for code in ("CITATION_NOT_FOUND", "REFERENCE_INVALID", "SOURCE_UNREADABLE"):
        if code in codes:
            raise ValidationIssues(code if code != "SOURCE_UNREADABLE" else "EVIDENCE_INVALID", problems)
    raise ValidationIssues("VALIDATION_FAILED", problems)


def current(tx: Tx, kind: str, story_id: str, name: str | None = None) -> dict[str, Any] | None:
    return tx.current_of(kind, story_id, name)


def validate_payload(tx: Tx, ctx: Ctx, kind: str, payload: dict[str, Any], story_id: str) -> tuple[
        list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """返回 (问题, 依赖, 派生信息)。结构合格后仍执行关系校验。"""
    problems = shape.check(payload, payload_spec(kind))
    if problems:
        raise ValidationIssues("SCHEMA_INVALID", problems)
    deps: list[dict[str, Any]] = []
    derived: dict[str, Any] = {}
    sources = source_map(tx, ctx, story_id)
    if kind == "quality_review":
        problems += dq.validate_quality_review(payload, sources)
        deps += [dep(tx, r) for r in payload["source_refs"] if r in sources]
    elif kind == "technical_review":
        problems += dq.validate_technical_review(payload, sources)
        deps += [dep(tx, r) for r in payload["source_refs"] if r in sources]
    elif kind == "test_analysis":
        problems += dq.validate_test_analysis(payload, sources)
        refs = sorted({c["source_ref"] for r in payload["requirements"] for c in r["citations"]} & set(sources))
        deps += [dep(tx, r) for r in refs]
        qr = current(tx, "quality_review", story_id)
        if qr:
            deps.append(dep(tx, qr["id"], "upstream"))
        tr = current(tx, "technical_review", story_id)
        if tr:
            deps.append(dep(tx, tr["id"], "upstream"))
        derived["handoff_seed"] = dq.handoff_seed(payload)
    elif kind == "test_plan":
        ev = dplan.evaluate_plan(payload)
        problems += ev["problems"]
        derived["plan_evaluation"] = ev
        ta = current(tx, "test_analysis", story_id)
        if ta:
            deps.append(dep(tx, ta["id"], "upstream"))
    elif kind == "case_design":
        ta = tx.get_snapshot(payload["analysis_ref"])
        if ta is None or ta["kind"] != "test_analysis":
            problems.append(issue("payload.analysis_ref", "引用的测试分析不存在", payload["analysis_ref"],
                                  code="REFERENCE_INVALID"))
        else:
            problems += dcases.validate_case_design(payload, ta["payload"])
            derived["traceability"] = dcases.traceability(payload, ta["payload"])
            deps.append(dep(tx, ta["id"], "upstream"))
    elif kind == "case_quality_review":
        d = tx.get_snapshot(payload["design_ref"])
        if d is None or d["kind"] != "case_design":
            problems.append(issue("payload.design_ref", "引用的用例设计不存在", payload["design_ref"],
                                  code="REFERENCE_INVALID"))
        else:
            deps.append(dep(tx, d["id"], "upstream"))
    elif kind == "change_set":
        ta = current(tx, "test_analysis", story_id)
        reg_ids = {r["id"] for r in ta["payload"]["regression"]} if ta else set()
        problems += dq.validate_change_set(payload, reg_ids)
        if not tx.get_evidence(payload["diff_ref"]):
            problems.append(issue("payload.diff_ref", "diff 证据不存在", payload["diff_ref"], code="REFERENCE_INVALID"))
        else:
            deps.append(dep(tx, payload["diff_ref"], "evidence"))
        derived["scope_complete"] = payload["search_scope"]["complete"] and not payload["unknown_scope"]
    elif kind == "flow_model":
        runtime = _evidence_origins(tx, [r for f in payload["flows"] for r in f["evidence_refs"]])
        for r, info in runtime.items():
            info["deployments"] = sorted(_evidence_deployments(tx, r))
        current_deps: set[str] = set()
        for s in tx.snapshots(story_id, "deployment"):
            if tx.is_current(s["id"]):
                current_deps |= {s["id"], s["payload"].get("deployment_id")}
        derived["flows"] = dev.flow_verification(payload["flows"], runtime, current_deps - {None})
    elif kind == "claim":
        if payload.get("review_state") == "accepted":
            problems.append(issue("payload.review_state", "“已接受”只能由独立的接受决定产生（记录确认人与方式）；"
                                  "提交时请使用 pending，再通过 review 登记接受", code="VALIDATION_FAILED"))
        runtime = _evidence_origins(tx, payload.get("runtime_evidence_refs") or [])
        others = _same_subject_claims(tx, story_id, payload)
        derived["classification"] = dev.classify_claims([s["payload"] for s in others] + [payload], sources,
                                                        runtime)[-1]
        conflicting = [s["id"] for s in others if repr(s["payload"].get("value")) != repr(payload.get("value"))]
        if conflicting:
            derived["contradiction"] = {"subject": payload["subject"], "conflicting_refs": conflicting,
                                        "values": sorted({repr(s["payload"].get("value")) for s in others}
                                                         | {repr(payload.get("value"))})}
        elif any(_contradiction_open(tx, s["id"]) for s in others):
            # 矛盾尚未由人解决：同取值的新结论也不能成为无条件事实
            c = derived["classification"]
            if c["accepted_as"] == "fact":
                c["accepted_as"] = "pending"
                c["reasons"].append("同一事项存在未解决的矛盾来源，需人工确认")
        if derived["classification"]["accepted_as"] == "rejected":
            problems += [issue("payload.evidence_refs", r, code="CITATION_NOT_FOUND")
                         for r in derived["classification"]["reasons"]]
    elif kind == "rollback_plan":
        from qa_flow.domain.release import rollback_feasibility

        derived["feasibility"] = rollback_feasibility(payload)
    elif kind == "knowledge_candidate":
        from qa_flow.domain.learning import knowledge_gate

        refs = [r for c in payload["claims"] for r in c["evidence_refs"]]
        derived["gate"] = knowledge_gate(payload, {r: v["origin"] for r, v in _evidence_origins(tx, refs).items()})
    elif kind == "dataset":
        pass
    elif kind == "effort_entry":
        if payload["hours"] < 0:
            problems.append(issue("payload.hours", "工时不能为负"))
        if payload.get("start") and payload.get("end") and payload["end"] <= payload["start"]:
            problems.append(issue("payload.end", "非法时序：结束早于开始"))
    elif kind == "metric_input":
        pass
    elif kind == "release_check":
        from qa_flow.domain.release import evaluate_release_check

        refs = [p["evidence_ref"] for p in payload["probes"]] + [s["evidence_ref"] for s in payload["metric_samples"]]
        origins = {r: v["origin"] for r, v in _evidence_origins(tx, refs).items()}
        deployment = None
        for s in tx.snapshots(story_id, "deployment"):
            if s["payload"]["deployment_id"] == payload["deployment"] and s["origin"] == "production":
                deployment = s["payload"]
        res = evaluate_release_check(payload, origins, deployment)
        origin_problems = [p for p in res["problems"] if p.get("code") == "ORIGIN_MISMATCH"]
        if origin_problems:
            raise ValidationIssues("ORIGIN_MISMATCH", origin_problems)
        derived["release"] = res
        deps += [dep(tx, r, "evidence") for r in refs if r in origins]
    elif kind == "investigation":
        builds_by_ev: dict[str, set[str]] = {}
        for s in tx.snapshots(story_id, "case_attempt"):
            for r in s["payload"].get("evidence_refs") or []:
                builds_by_ev.setdefault(r, set()).add(s["payload"].get("build"))
        for i, f in enumerate(payload["facts"]):
            for r in f["evidence_refs"]:
                ev = tx.get_evidence(r)
                if ev is None or not tx.evidence_valid(r):
                    problems.append(issue(f"payload.facts[{i}].evidence_refs", "事实缺少有效证据", r,
                                          code="REFERENCE_INVALID"))
                    continue
                other = sorted(b for b in builds_by_ev.get(r, set()) if b and b != payload.get("build"))
                if payload.get("build") and other:
                    problems.append(issue(f"payload.facts[{i}].evidence_refs",
                                          f"证据来自构建 {'、'.join(other)} 的执行，与调查声明的构建 "
                                          f"{payload['build']} 不一致，不能作为该构建的事实", r))
        derived["layers"] = {"facts": len(payload["facts"]), "inferences": len(payload["inferences"]),
                             "pending": len(payload["pending"])}
    elif kind == "environment_check":
        origins = {r: v["origin"] for r, v in _evidence_origins(
            tx, [c.get("evidence_ref") for c in payload["checks"] if c.get("evidence_ref")]).items()}
        from qa_flow.domain.execution import environment_ready

        derived["environment"] = environment_ready(payload, origins)
    elif kind == "self_test_report":
        for r in payload["source_refs"]:
            if r not in sources:
                problems.append(issue("payload.source_refs", "自测材料来源不存在", r, code="REFERENCE_INVALID"))
            else:
                deps.append(dep(tx, r))
    elif kind in ("escape", "improvement"):
        pass
    return problems, deps, derived


def _evidence_deployments(tx: Tx, ev_ref: str) -> set[str]:
    """证据产生时所在的部署：登记该证据的执行或探测结果所绑定的部署上下文（快照 ID 与部署 ID）。"""
    out: set[str] = set()
    rows = tx.conn.execute("SELECT result_id FROM dependencies WHERE input_id=? AND relation='evidence'",
                           (ev_ref,)).fetchall()
    for r in rows:
        for c in tx.conn.execute("SELECT input_id FROM dependencies WHERE result_id=? AND relation='context'",
                                 (r["result_id"],)).fetchall():
            snap = tx.get_snapshot(c["input_id"])
            if snap and snap["kind"] == "deployment":
                out |= {snap["id"], snap["payload"].get("deployment_id")}
    return out - {None}


def _evidence_origins(tx: Tx, refs: list[str]) -> dict[str, dict[str, Any]]:
    out = {}
    for r in refs:
        ev = tx.get_evidence(r)
        if ev and tx.evidence_valid(r):
            out[r] = {"origin": ev["origin"]}
    return out


def _same_subject_claims(tx: Tx, story_id: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """同一需求下、同一事项（subject）且带取值的当前结论。"""
    if payload.get("subject") is None or "value" not in payload:
        return []
    out = []
    for s in tx.snapshots(story_id, "claim"):
        p = s["payload"]
        if p.get("subject") == payload["subject"] and "value" in p and tx.is_current(s["id"]):
            out.append(s)
    return out


def _contradiction_open(tx: Tx, claim_id: str) -> bool:
    d = derived_of(tx, claim_id)
    return bool(d.get("contradiction")) or any("矛盾" in r for r in (d.get("classification") or {}).get("reasons", []))


def _flag_contradiction(tx: Tx, ctx: Ctx, story_id: str, new_id_: str, contradiction: dict[str, Any]) -> None:
    """矛盾来源：已有事实一并转为待确认，并提出由产品负责人确认的事项；两处证据都保留。"""
    for ref in contradiction["conflicting_refs"]:
        agg = tx.get_agg(f"derived:{ref}")
        if not agg:
            continue
        st = dict(agg["state"])
        c = dict(st.get("classification") or {})
        if c.get("accepted_as") == "fact":
            c["accepted_as"] = "pending"
        c["reasons"] = list(c.get("reasons") or []) + [f"与 {new_id_} 的取值矛盾，需人工确认"]
        st["classification"] = c
        st["contradiction"] = {**contradiction, "conflicting_refs": [new_id_]}
        tx.put_agg(agg["object_id"], "derived", story_id, st, expected_revision=agg["revision"],
                   event_type="derived_contradiction")
    from qa_flow.application.review import raise_item

    refs = contradiction["conflicting_refs"] + [new_id_]
    basis = []
    for r in refs:
        snap = tx.get_snapshot(r)
        for e in (snap["payload"].get("evidence_refs") or [])[:1]:
            basis.append({"source_ref": e["source_ref"], "quote": e.get("quote", "")})
    raise_item(tx, ctx, story_id, kind="clarification", key=f"conflict-{contradiction['subject']}",
               question=f"来源对“{contradiction['subject']}”给出矛盾取值（{'、'.join(contradiction['values'])}），以哪个为准？",
               basis=basis, affected_scope="依赖该事项的结论在确认前保持待确认，不形成事实",
               responsible_role="product",
               options=[{"key": chr(65 + i), "label": f"以 {v} 为准"} for i, v in enumerate(contradiction["values"])],
               subject_refs=refs, raised_by="claims", extra={"contradiction": contradiction})


def submit_artifact(tx: Tx, ctx: Ctx, kind: str, payload: dict[str, Any], story_id: str, *,
                    name: str | None = None, parent_id: str | None = None,
                    provenance: dict[str, Any] | None = None, producer: dict[str, Any] | None = None,
                    origin: str = "local", extra_deps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    problems, deps, derived = validate_payload(tx, ctx, kind, payload, story_id)
    _raise_issues(problems)
    lineage = lineage_key(kind, story_id, name)
    current_id = tx.lineage_current(lineage)
    if current_id:
        cur = tx.get_snapshot(current_id)
        if cur and cur["payload"] == payload and parent_id not in (None, current_id) and \
                effective_current(tx, current_id):
            # 基于旧版本提交、但内容与当前有效版本完全相同：不是冲突，返回已有版本
            return {"artifact_id": current_id, "kind": kind, "replaced": False, "unchanged": True,
                    "derived": derived}
    if current_id and (parent_id is None or parent_id == current_id):
        cur = tx.get_snapshot(current_id)
        if cur and cur["payload"] == payload:
            if effective_current(tx, current_id):
                return {"artifact_id": current_id, "kind": kind, "replaced": False, "unchanged": True,
                        "derived": derived}
            # 内容相同但上游已变化：登记一个绑定当前上游的新修订，而不是继续返回已失效的旧版本
            parent_id = current_id
    if current_id and parent_id is None:
        raise QAError("REVISION_CONFLICT", "该产物已有当前版本；修订请基于当前版本提交（--parent）",
                      path="parent_id", related_refs=[current_id], details={"current": current_id})
    prov = dict(provenance or {})
    if prov:
        prov["completeness"] = dev.provenance_completeness(prov)
    snap = tx.put_snapshot(kind, payload, story_id=story_id, project_id=ctx.project_id,
                           producer=producer or {"kind": "operator", "name": ctx.actor, "verification": "local_os_user"},
                           context={"rules_sha256": ctx.rules_sha, "config_sha256": ctx.config_sha},
                           depends_on=deps + (extra_deps or []) + ([dep(tx, parent_id, "revises", required=False)]
                                                                    if parent_id else []),
                           provenance=prov, lineage=lineage, parent_id=parent_id, origin=origin)
    if derived:
        tx.put_agg(f"derived:{snap['id']}", "derived", story_id, derived, event_type="derived_recorded")
    if kind == "claim" and derived.get("contradiction"):
        _flag_contradiction(tx, ctx, story_id, snap["id"], derived["contradiction"])
    if parent_id:
        propagate(tx, ctx, story_id, reason=f"{kind} 已修订为 {snap['id']}")
    return {"artifact_id": snap["id"], "kind": kind, "sha256": snap["sha256"], "parent_id": parent_id,
            "derived": derived, "provenance_degraded": (prov.get("completeness") or {}).get("degraded")}


def derived_of(tx: Tx, sid: str) -> dict[str, Any]:
    agg = tx.get_agg(f"derived:{sid}")
    return agg["state"] if agg else {}


def evidence_from_file(tx: Tx, path: str, origin: str = "upload", privacy: str = "internal") -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise QAError("IO_ERROR", f"文件不存在: {path}")
    data = p.read_bytes()
    media = TEXT_SUFFIXES.get(p.suffix.lower(), "application/octet-stream")
    if origin not in ("upload", "imported"):
        # 上传者自述不能决定来源等级
        origin = "upload"
    return tx.register_evidence(data, origin=origin, source=f"file:{os.path.realpath(p)}", media_type=media,
                                privacy=privacy)
