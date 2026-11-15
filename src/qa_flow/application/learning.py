"""漏测、改进与知识回流。

知识流程：候选 → 核对 → 确认（human 或 policy，不让策略伪装真人）→ 应用到隔离知识库 → 可检索。
"""

from __future__ import annotations

from typing import Any

from qa_flow.application import review
from qa_flow.application.context import Ctx
from qa_flow.application.core import decision_valid, dep, derived_of, submit_artifact
from qa_flow.domain.learning import improvement_status, search_knowledge
from qa_flow.errors import QAError
from qa_flow.storage.store import Tx, lineage_key


def propose_knowledge(tx: Tx, ctx: Ctx, story_id: str, payload: dict[str, Any], provenance: dict[str, Any] | None = None
                      ) -> dict[str, Any]:
    res = submit_artifact(tx, ctx, "knowledge_candidate", payload, story_id,
                          name=f"kc-{len(tx.aggs('knowledge', story_id)) + 1}", provenance=provenance)
    cid = res["artifact_id"]
    gate = derived_of(tx, cid).get("gate") or {}
    state = {"candidate_ref": cid, "status": "candidate", "gate": gate, "decision_ref": None, "applied_snapshot": None,
             "confirmation_method": None}
    tx.put_agg(f"knowledge:{story_id}:{cid}", "knowledge", story_id, state, expected_revision=0,
               event_type="knowledge_proposed")
    if gate.get("policy_confirm_allowed"):
        state["status"] = "policy_confirmed"
        state["confirmation_method"] = "policy"
        tx.put_agg(f"knowledge:{story_id}:{cid}", "knowledge", story_id, state, event_type="knowledge_policy_confirmed")
    else:
        text = payload["claims"][0]["text"] if payload["claims"] else payload["material_type"]
        review.raise_item(tx, ctx, story_id, kind="knowledge_confirmation", key=f"kc-{cid}",
                          question=f"是否确认将该条目写入知识库：{text}",
                          basis=[{"source_ref": cid, "quote": text}],
                          affected_scope="隔离知识库；下一需求可检索", responsible_role="tester",
                          options=[{"key": "A", "label": "确认入库"}, {"key": "reject", "label": "不入库"}],
                          subject_refs=[cid], raised_by="retrospective",
                          extra={"reasons": gate.get("reasons", [])})
    return {"candidate_ref": cid, "gate": gate, "status": state["status"]}


def apply_confirmed(tx: Tx, ctx: Ctx, story_id: str) -> list[dict[str, Any]]:
    """把已确认的候选应用到隔离知识库（新快照，保留原知识快照）。"""
    applied = []
    for agg in tx.aggs("knowledge", story_id):
        s = dict(agg["state"])
        if s["status"] in ("applied", "rejected"):
            continue
        if s["status"] == "candidate":
            item = None
            for a in tx.aggs("review_item", story_id):
                if a["state"]["key"] == f"kc-{s['candidate_ref']}":
                    item = a["state"]
            if not item or item["status"] != "answered" or not decision_valid(tx, item["decision_ref"])["valid"]:
                continue
            row = tx.conn.execute("SELECT result, verification FROM decisions WHERE id=?",
                                  (item["decision_ref"],)).fetchone()
            if row["result"] == "reject":
                s["status"] = "rejected"
                tx.put_agg(agg["object_id"], "knowledge", story_id, s, expected_revision=agg["revision"],
                           event_type="knowledge_rejected")
                continue
            s["decision_ref"] = item["decision_ref"]
            s["confirmation_method"] = "human"
            s["verification"] = row["verification"]
        cand = tx.require_snapshot(s["candidate_ref"], "knowledge_candidate")
        lineage = lineage_key("knowledge_snapshot", None, ctx.project_id)
        cur = tx.lineage_current(lineage)
        entries = list(tx.get_snapshot(cur)["payload"]["entries"]) if cur else []
        for c in cand["payload"]["claims"]:
            entries.append({"text": c["text"], "claim_type": c["claim_type"], "candidate_ref": cand["id"],
                            "sources": cand["payload"]["sources"], "evidence_refs": c["evidence_refs"],
                            "confirmation_method": s["confirmation_method"], "decision_ref": s.get("decision_ref"),
                            "verification": s.get("verification", "policy"), "story_id": story_id,
                            "tags": [cand["payload"]["material_type"]]})
        snap = tx.put_snapshot("knowledge_snapshot", {"entries": entries, "parent_ref": cur}, story_id=None,
                               project_id=ctx.project_id,
                               producer={"kind": "process", "name": "knowledge", "verification": "process"},
                               depends_on=[dep(tx, cand["id"], "candidate")], lineage=lineage, parent_id=cur)
        s["status"] = "applied"
        s["applied_snapshot"] = snap["id"]
        tx.put_agg(agg["object_id"], "knowledge", story_id, s, expected_revision=agg["revision"],
                   event_type="knowledge_applied", event_payload={"snapshot": snap["id"]})
        applied.append({"candidate_ref": cand["id"], "snapshot": snap["id"]})
    return applied


def search(tx: Tx, ctx: Ctx, query: str) -> dict[str, Any]:
    cur = tx.lineage_current(lineage_key("knowledge_snapshot", None, ctx.project_id))
    if not cur:
        return {"snapshot": None, "results": []}
    entries = tx.get_snapshot(cur)["payload"]["entries"]
    return {"snapshot": cur, "results": search_knowledge(entries, query)}


def _defect_state(tx: Tx, story_id: str, ref: Any) -> dict[str, Any] | None:
    """缺陷引用可以是缺陷 ID（BUG-…）或其提案快照 ID；都必须属于该需求。"""
    if not isinstance(ref, str) or not ref:
        return None
    agg = tx.get_agg(f"defect:{story_id}:{ref}")
    if agg:
        return agg["state"]
    snap = tx.get_snapshot(ref)
    if snap is None or snap["kind"] != "defect" or snap.get("story_id") != story_id:
        return None
    for a in tx.aggs("defect", story_id):
        if a["state"].get("proposal_ref") == ref:
            return a["state"]
    return None


def record_escape(tx: Tx, ctx: Ctx, story_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if _defect_state(tx, story_id, payload.get("defect_ref")) is None:
        raise QAError("REFERENCE_INVALID", f"漏测记录必须绑定该需求下已登记的缺陷：{payload.get('defect_ref')} 不存在",
                      path="defect_ref", related_refs=[str(payload.get("defect_ref"))])
    res = submit_artifact(tx, ctx, "escape", payload, story_id, name=payload["defect_ref"])
    item = review.raise_item(tx, ctx, story_id, kind="disposition", key=f"esc-{res['artifact_id']}",
                             question=f"漏测归因是否确认：{payload['proposed_cause']}",
                             basis=[{"source_ref": res["artifact_id"], "quote": payload["proposed_cause"]}],
                             affected_scope=f"应拦截阶段：{payload['should_have_caught_stage']}",
                             responsible_role="tester",
                             options=[{"key": "A", "label": "确认归因"}, {"key": "reject", "label": "不确认"}],
                             subject_refs=[res["artifact_id"]], raised_by="retrospective",
                             extra={"disposition": "confirm_escape"})
    return {"escape_ref": res["artifact_id"], "confirmation_handle": item["handle"]}


def escape_report(tx: Tx, ctx: Ctx, story_id: str) -> dict[str, Any]:
    from qa_flow.application.execution import attempts

    rows = []
    atts = attempts(tx, story_id)
    for esc in tx.snapshots(story_id, "escape"):
        confirmed = None
        for a in tx.aggs("review_item", story_id):
            s = a["state"]
            if s["key"] == f"esc-{esc['id']}" and s["status"] == "answered" and decision_valid(tx, s["decision_ref"])["valid"]:
                row = tx.conn.execute("SELECT result FROM decisions WHERE id=?", (s["decision_ref"],)).fetchone()
                confirmed = row["result"] != "reject"
        imps = [i for i in tx.snapshots(story_id, "improvement") if i["payload"]["escape_ref"] == esc["id"]]
        bug = _defect_state(tx, story_id, esc["payload"]["defect_ref"])
        fix_build = (bug.get("fix_build") if bug else None) or ""
        imp_rows = []
        for i in imps:
            ver = [a for a in atts if a["id"] in i["payload"]["verification_refs"]]
            # 只有修复构建上的实际通过才能证明措施有效；缺陷尚无修复构建时一律未验证
            imp_rows.append({"improvement_ref": i["id"], "measure": i["payload"]["measure"],
                             "fix_build": fix_build or None,
                             "status": improvement_status(i["payload"], ver, fix_build)})
        rows.append({"escape_ref": esc["id"], "defect_ref": esc["payload"]["defect_ref"],
                     "confirmed": confirmed, "improvements": imp_rows})
    return {"confirmed": [r for r in rows if r["confirmed"]], "unconfirmed": [r for r in rows if not r["confirmed"]],
            "confirmed_count": sum(1 for r in rows if r["confirmed"]),
            "note": "未确认归因单列，不计入确认漏测；新增用例不代表已减少漏测"}


def record_improvement(tx: Tx, ctx: Ctx, story_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    esc = tx.get_snapshot(payload["escape_ref"])
    if esc is None or esc["kind"] != "escape":
        raise QAError("REFERENCE_INVALID", "改进措施必须关联漏测记录", related_refs=[payload["escape_ref"]])
    res = submit_artifact(tx, ctx, "improvement", payload, story_id,
                          name=f"imp-{payload['escape_ref']}-{len(tx.snapshots(story_id, 'improvement'))}",
                          extra_deps=[dep(tx, payload["escape_ref"], "escape")])
    return {"improvement_ref": res["artifact_id"]}
