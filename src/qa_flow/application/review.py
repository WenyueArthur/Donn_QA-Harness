"""待确认事项、澄清与人工决定。

- 同版本、同角色、同范围的问题可以集中展示，但逐项保存答案。
- 同一有效决定不反复要求确认；不同目标、角色或已过期的决定不得混为“一次全部同意”。
- 本地答复不会变成外部认证；业务确认也不会自动授予生产权限。
"""

from __future__ import annotations

import json
from typing import Any

from qa_flow.application.context import Ctx
from qa_flow.application.core import dep, effective_current, decision_valid, source_map
from qa_flow.canonical import sha256_obj
from qa_flow.domain.evidence import check_citations
from qa_flow.domain.review import clarification_key, is_generic_answer
from qa_flow.errors import QAError, ValidationIssues, issue
from qa_flow.storage.store import Tx

KIND_LABEL = {
    "clarification": "业务问题",
    "plan_approval": "测试计划批准",
    "expectation_confirmation": "关键业务预期确认",
    "exit_confirmation": "准出确认",
    "knowledge_confirmation": "知识入库确认",
    "action_authorization": "操作授权",
    "applicability": "适用性决定",
    "disposition": "缺陷处置",
}


def _next_handle(tx: Tx, story_id: str) -> str:
    n = len(tx.aggs("review_item", story_id)) + 1
    return f"Q{n}"


def raise_item(tx: Tx, ctx: Ctx, story_id: str, *, kind: str, key: str, question: str, basis: list[dict[str, Any]],
               affected_scope: str, responsible_role: str, options: list[dict[str, Any]],
               subject_refs: list[str], raised_by: str, blocks: list[str] | None = None,
               plan_sha256: str | None = None, environment: str | None = None,
               target_ids: list[str] | None = None, action_kinds: list[str] | None = None,
               extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """登记待确认事项。同一主问题再次提出时只追加别名，不重复提问。"""
    oid = f"review:{story_id}:{key}"
    existing = tx.get_agg(oid)
    subjects = [dep(tx, r) for r in subject_refs]
    if existing:
        st = dict(existing["state"])
        same_subjects = [s["ref"] for s in st["subject_refs"]] == [s["ref"] for s in subjects] and \
            [s["sha256"] for s in st["subject_refs"]] == [s["sha256"] for s in subjects]
        if same_subjects and st["status"] != "stale":
            if raised_by not in st["raised_by"]:
                st["raised_by"] = st["raised_by"] + [raised_by]
                tx.put_agg(oid, "review_item", story_id, st, expected_revision=existing["revision"],
                           event_type="review_item_alias", event_payload={"alias": raised_by})
            return st
        # 内容已变化：旧事项保留为历史，新版本重新提问并展示差异
        old = dict(st)
        old["status"] = "superseded"
        tx.put_agg(oid, "review_item", story_id, old, expected_revision=existing["revision"],
                   event_type="review_item_superseded", event_payload={})
        oid = f"review:{story_id}:{key}:{sha256_obj([s['sha256'] for s in subjects])[:8]}"
        if tx.get_agg(oid):
            return tx.get_agg(oid)["state"]
    state = {
        "handle": _next_handle(tx, story_id), "kind": kind, "key": key, "story_id": story_id,
        "business_question": question, "basis": basis, "affected_scope": affected_scope,
        "responsible_role": responsible_role, "options": options, "subject_refs": subjects,
        "raised_by": [raised_by], "blocks": blocks or [], "status": "pending", "decision_ref": None,
        "plan_sha256": plan_sha256, "environment": environment or ("lab" if ctx.is_lab else "test"),
        "target_ids": target_ids or [], "action_kinds": action_kinds or [], "extra": extra or {},
        "replaces": existing["object_id"] if existing else None,
    }
    tx.put_agg(oid, "review_item", story_id, state, expected_revision=0, event_type="review_item_raised",
               event_payload={"kind": kind, "handle": state["handle"]})
    return state


def propose_clarification(tx: Tx, ctx: Ctx, story_id: str, q: dict[str, Any], raised_by: str) -> dict[str, Any]:
    sources = source_map(tx, ctx, story_id)
    problems, located = check_citations(q.get("basis") or [], sources, "basis")
    if not q.get("basis"):
        problems.append(issue("basis", "问题必须提供原文依据"))
    if problems:
        raise ValidationIssues("CITATION_NOT_FOUND" if any(p.get("code") == "CITATION_NOT_FOUND" for p in problems)
                               else "VALIDATION_FAILED", problems)
    src_ref = located[0]["source_ref"]
    key = "ask-" + clarification_key(src_ref.split("-")[0] + ":" + sources[src_ref]["uri"],
                                     q.get("question_ref") or q["question"])
    payload = {"question": q["question"], "basis": located, "aliases": [], "blocks": q.get("blocks") or [],
               "responsible_role": q.get("responsible_role", "product"), "options": q.get("options") or [],
               "affected_scope": q.get("affected_scope", ""), "question_ref": q.get("question_ref", "")}
    # 澄清快照不可变：同一问题与同一来源快照只登记一次
    snap_id = None
    for s in tx.snapshots(story_id, "clarification"):
        if s["payload"]["question"] == payload["question"] and s["payload"]["basis"] == located:
            snap_id = s["id"]
            break
    if snap_id is None:
        snap = tx.put_snapshot("clarification", payload, story_id=story_id, project_id=ctx.project_id,
                               producer={"kind": "process", "name": raised_by, "verification": "process"},
                               depends_on=[dep(tx, c["source_ref"]) for c in located])
        snap_id = snap["id"]
    item = raise_item(tx, ctx, story_id, kind="clarification", key=key, question=q["question"], basis=located,
                      affected_scope=payload["affected_scope"], responsible_role=payload["responsible_role"],
                      options=payload["options"], subject_refs=[snap_id], raised_by=raised_by,
                      blocks=payload["blocks"])
    return {"clarification_ref": snap_id, "handle": item["handle"], "aliases": item["raised_by"][1:]}


def items(tx: Tx, story_id: str, include_closed: bool = False) -> list[dict[str, Any]]:
    out = []
    for a in tx.aggs("review_item", story_id):
        s = dict(a["state"])
        s["object_id"] = a["object_id"]
        s["revision"] = a["revision"]
        if s["status"] == "answered" and s.get("decision_ref") and not decision_valid(tx, s["decision_ref"])["valid"]:
            s["status"] = "stale"
        if include_closed or s["status"] in ("pending", "transferred"):
            out.append(s)
    return out


def find_item(tx: Tx, story_id: str, handle: str) -> dict[str, Any]:
    for s in items(tx, story_id, include_closed=True):
        if s["handle"].lower() == handle.lower() and s["status"] not in ("superseded",):
            return s
    raise QAError("NOT_FOUND", f"没有找到待确认事项 {handle}", path="handle")


def answer(tx: Tx, ctx: Ctx, story_id: str, handle: str, option: str | None, text: str | None, *,
           role: str | None = None, actor: str | None = None, verification: str | None = None,
           expires_at: str | None = None, expected_revision: int | None = None) -> dict[str, Any]:
    item = find_item(tx, story_id, handle)
    if item["status"] == "answered":
        return {"handle": handle, "decision_ref": item["decision_ref"], "reused": True}
    if item["status"] in ("stale", "superseded"):
        raise QAError("PRECONDITION_FAILED", "该事项所依据的内容已变化，请查看新版本问题", related_refs=[handle])
    role = role or ctx.user_role
    actor = actor or ctx.actor
    raw = (text or option or "").strip()
    if not raw:
        raise QAError("INVALID_ARGUMENT", "答复不能为空", path="answer")
    if is_generic_answer(raw):
        raise QAError("INVALID_ARGUMENT", "“全部同意”不能作为逐项决定；请针对具体问题选择选项或填写答复",
                      path="answer", related_refs=[handle])
    if role != item["responsible_role"]:
        raise QAError("AUTHORIZATION_REQUIRED",
                      f"该问题需要{ctx.role_label(item['responsible_role'])}决定；可以转交，不能代签",
                      related_refs=[handle], details={"responsible_role": item["responsible_role"]})
    opts = {o["key"]: o for o in item["options"]}
    if option is not None and opts and option not in opts:
        raise QAError("INVALID_ARGUMENT", f"选项 {option} 不在可选范围 {sorted(opts)} 内", path="option")
    verification = verification or ("local_attested" if role != "simulated_stub" else "simulated")
    if verification == "external_verified":
        raise QAError("AUTHORIZATION_INVALID", "本地命令行不能产生外部核验的决定；请使用原生审批入口")
    if item["kind"] in ("action_authorization", "exit_confirmation", "plan_approval") and \
            item["environment"] not in ("lab", "offline") and verification != "external_verified":
        raise QAError("AUTHORIZATION_REQUIRED", "真实环境的批准需要原生审批身份核验；本地答复不能代替",
                      related_refs=[handle])
    result = _result_for(item, option)
    for s in item["subject_refs"]:
        if not s["ref"].startswith("EV-") and not effective_current(tx, s["ref"]):
            raise QAError("PRECONDITION_FAILED", "问题依据的内容已不是当前版本", related_refs=[s["ref"]])
    payload = {
        "subject_refs": [{"ref": s["ref"], "sha256": s["sha256"]} for s in item["subject_refs"]],
        "plan_sha256": item.get("plan_sha256"),
        "role": role, "actor": actor, "verification": verification,
        "scope": {"environment": item["environment"], "target_ids": item.get("target_ids") or [],
                  "action_kinds": item.get("action_kinds") or [], "story_id": story_id},
        "result": result, "answer": {"option": option, "label": opts.get(option, {}).get("label") if option else None,
                                     "text": text},
        "reason": text or opts.get(option, {}).get("label") or None, "expires_at": expires_at, "proposer": ",".join(item["raised_by"]),
    }
    decision = record_decision(tx, ctx, story_id, payload)
    st = {k: v for k, v in item.items() if k not in ("object_id", "revision")}
    st["status"] = "answered"
    st["decision_ref"] = decision["decision_ref"]
    tx.put_agg(item["object_id"], "review_item", story_id, st,
               expected_revision=expected_revision if expected_revision is not None else item["revision"],
               event_type="review_item_answered", event_payload={"decision_ref": decision["decision_ref"]})
    return {"handle": handle, "decision_ref": decision["decision_ref"], "verification": verification,
            "note": "本地答复只代表本机操作者的记录，不是已认证的产品角色签字" if verification == "local_attested" else ""}


def _result_for(item: dict[str, Any], option: str | None) -> str:
    if item["kind"] == "clarification":
        return "answer"
    if option in ("reject", "R", "N"):
        return "reject"
    return {"plan_approval": "approve", "expectation_confirmation": "confirm", "exit_confirmation": "approve",
            "knowledge_confirmation": "confirm", "action_authorization": "approve", "applicability": "waive",
            "disposition": (item.get("extra") or {}).get("disposition", "defer")}.get(item["kind"], "confirm")


def transfer(tx: Tx, ctx: Ctx, story_id: str, handle: str, to_role: str) -> dict[str, Any]:
    item = find_item(tx, story_id, handle)
    st = {k: v for k, v in item.items() if k not in ("object_id", "revision")}
    st["status"] = "transferred"
    st["transferred_to"] = to_role
    tx.put_agg(item["object_id"], "review_item", story_id, st, expected_revision=item["revision"],
               event_type="review_item_transferred", event_payload={"to": to_role})
    return {"handle": handle, "transferred_to": to_role}


def record_decision(tx: Tx, ctx: Ctx, story_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    from qa_flow.contracts import shape
    from qa_flow.contracts.registry import payload_spec

    problems = shape.check(payload, payload_spec("decision"))
    if not payload.get("actor") or not payload.get("role"):
        problems.append(issue("payload.actor", "决定必须有确认人与角色"))
    if problems:
        raise ValidationIssues("SCHEMA_INVALID", problems)
    if payload["verification"] == "simulated" and not ctx.is_lab:
        raise QAError("AUTHORIZATION_INVALID", "模拟决定只能用于实验配置")
    deps = [dep(tx, s["ref"], "subject") for s in payload["subject_refs"]]
    for d, s in zip(deps, payload["subject_refs"]):
        if d["sha256"] != s["sha256"]:
            raise QAError("REFERENCE_INVALID", "决定绑定的摘要与对象不符", related_refs=[s["ref"]])
    snap = tx.put_snapshot("decision", payload, story_id=story_id, project_id=ctx.project_id,
                           producer={"kind": "operator", "name": payload["actor"],
                                     "verification": payload["verification"]},
                           depends_on=deps)
    tx.conn.execute(
        "INSERT INTO decisions(id, story_id, role, actor, verification, result, plan_sha256, subjects, scope, "
        "expires_at, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (snap["id"], story_id, payload["role"], payload["actor"], payload["verification"], payload["result"],
         payload.get("plan_sha256"), json.dumps(payload["subject_refs"]), json.dumps(payload["scope"]),
         payload.get("expires_at"), tx.now))
    return {"decision_ref": snap["id"], "verification": payload["verification"]}


def revoke(tx: Tx, ctx: Ctx, decision_id: str, reason: str) -> dict[str, Any]:
    row = tx.conn.execute("SELECT story_id, revoked_at FROM decisions WHERE id=?", (decision_id,)).fetchone()
    if row is None:
        raise QAError("NOT_FOUND", f"决定不存在: {decision_id}")
    if row["revoked_at"]:
        return {"decision_ref": decision_id, "already_revoked": True}
    tx.conn.execute("UPDATE decisions SET revoked_at=?, revoke_reason=? WHERE id=?", (tx.now, reason, decision_id))
    tx.event(decision_id, "decision_revoked", {"reason": reason})
    from qa_flow.application.core import propagate

    if row["story_id"]:
        propagate(tx, ctx, row["story_id"], reason=f"决定 {decision_id} 已撤销")
    return {"decision_ref": decision_id, "revoked": True}


def valid_decision_for(tx: Tx, story_id: str, kind: str, subject_ref: str) -> str | None:
    """查找覆盖该对象的有效决定（用于复用已生效答复，不重复提问）。"""
    for a in tx.aggs("review_item", story_id):
        s = a["state"]
        if s["kind"] != kind or s["status"] != "answered" or not s.get("decision_ref"):
            continue
        if subject_ref in [r["ref"] for r in s["subject_refs"]] and decision_valid(tx, s["decision_ref"])["valid"]:
            row = tx.conn.execute("SELECT result FROM decisions WHERE id=?", (s["decision_ref"],)).fetchone()
            if row and row["result"] != "reject":
                return s["decision_ref"]
    return None
