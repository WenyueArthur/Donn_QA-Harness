"""缺陷：提案、创建、附件、状态同步、复测、关闭与处置。

创建、附件、修复、复测和关闭是独立操作；附件必须回读验证关联到工作项。
"""

from __future__ import annotations

from typing import Any

from qa_flow.adapters.base import RemoteFailed, require_capability
from qa_flow.application import broker
from qa_flow.application.context import Ctx
from qa_flow.application.core import dep
from qa_flow.domain import defects as ddef
from qa_flow.errors import QAError
from qa_flow.ids import new_id
from qa_flow.storage.store import Tx


class PartialFailure(RemoteFailed):
    def __init__(self, message: str, partial: dict[str, Any], retryable: bool):
        super().__init__(message)
        self.partial = partial
        self.retryable = retryable


def _oid(story_id: str, bug_id: str) -> str:
    return f"defect:{story_id}:{bug_id}"


def get(tx: Tx, story_id: str, bug_id: str) -> dict[str, Any]:
    agg = tx.get_agg(_oid(story_id, bug_id))
    if agg is None:
        raise QAError("NOT_FOUND", f"缺陷不存在: {bug_id}", related_refs=[bug_id])
    return agg


def all_defects(tx: Tx, story_id: str) -> list[dict[str, Any]]:
    return [a["state"] for a in tx.aggs("defect", story_id)]


def _save(tx: Tx, agg: dict[str, Any], state: dict[str, Any], event: str, payload: dict[str, Any] | None = None) -> None:
    tx.put_agg(agg["object_id"], "defect", state["story_id"], state, expected_revision=agg["revision"],
               event_type=event, event_payload=payload or {})


def severity_for(ctx: Ctx, case: dict[str, Any]) -> str:
    rule = ctx.policy.get("severity_by_priority") or {"P0": "major", "P1": "minor", "P2": "minor"}
    return rule.get(case.get("priority", "P1"), "minor")


def propose(tx: Tx, ctx: Ctx, story_id: str, attempt_id: str, *, root_cause: str | None = None) -> dict[str, Any]:
    att = tx.require_snapshot(attempt_id, "case_attempt")
    p = att["payload"]
    if p["outcome"] != "fail":
        raise QAError("PRECONDITION_FAILED", "只有断言失败的尝试才能形成产品缺陷提案；执行错误需先分析",
                      related_refs=[attempt_id])
    for d in all_defects(tx, story_id):
        if d["tc_ref"] == p["tc_ref"] and d["status"] not in ddef.TERMINAL:
            return {"defect_id": d["id"], "existing": True}
    design = None
    for dref in att["envelope"]["depends_on"]:
        if dref["relation"] == "expected":
            design = tx.get_snapshot(dref["ref"])
    case = next((c for c in (design["payload"]["cases"] if design else []) if c["id"] == p["tc_ref"]), {})
    bug_id = new_id("BUG")
    failed = [a for a in p["assertions"] if a["result"] == "fail"]
    payload = {
        "title": f"{case.get('title', p['tc_ref'])}：实际结果与预期不符",
        "tc_ref": p["tc_ref"],
        "reproduction": case.get("steps", []),
        "expected_ref": {"design_ref": design["id"] if design else None, "tc": p["tc_ref"],
                         "expected_text": case.get("expected_text")},
        "actual_ref": {"attempt_ref": attempt_id, "failed_assertions": failed},
        "severity": severity_for(ctx, case),
        "status": "proposed",
        "build": p["build"],
        "root_cause": {"status": "unverified" if not root_cause else "inferred", "text": root_cause or "根因待验证"},
    }
    snap = tx.put_snapshot("defect", payload, story_id=story_id, project_id=ctx.project_id,
                           producer={"kind": "process", "name": "defect_proposer", "verification": "process"},
                           depends_on=[dep(tx, attempt_id, "actual", required=False)])
    state = {"id": bug_id, "story_id": story_id, "proposal_ref": snap["id"], **payload, "external_id": None,
             "external_status": None, "platform_revision": 0, "attachment_file_id": None,
             "attachments_verified": False, "fix_commit": None, "fix_build": None, "retests": [],
             "disposition_refs": [], "created_at": tx.now, "evidence_ref": p["observations_ref"]}
    tx.put_agg(_oid(story_id, bug_id), "defect", story_id, state, expected_revision=0, event_type="defect_proposed",
               event_payload={"tc": p["tc_ref"]})
    return {"defect_id": bug_id, "proposal_ref": snap["id"], "root_cause": payload["root_cause"]["status"]}


def plan_create(tx: Tx, ctx: Ctx, story_id: str, bug_id: str) -> dict[str, Any]:
    agg = get(tx, story_id, bug_id)
    d = agg["state"]
    require_capability(ctx.platform_manifest(), "create")
    fields = {"title": d["title"], "severity": d["severity"], "tc": d["tc_ref"], "reproduction": d["reproduction"],
              "expected": d["expected_ref"]["expected_text"], "build": d["build"], "qa_ref": bug_id}
    return broker.plan_action(tx, ctx, story_id, "defect.create", ctx.platform_manifest()["id"],
                              {"defect_id": bug_id, "fields": fields}, [d["proposal_ref"]], logical_key=f"create:{bug_id}")


def _create_perform(ctx: Ctx, action: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    item = ctx.platform().create(plan["request"]["fields"], action["idempotency_key"])
    read = ctx.platform().get(item["id"])  # 回读
    return {"state": "succeeded", "remote_object_id": read["id"], "external_status": read["status"],
            "platform_revision": read["revision"]}


def _create_query(ctx: Ctx, action: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    item = ctx.platform().query(action["idempotency_key"])
    if item:
        return {"found": True, "remote_object_id": item["id"], "external_status": item["status"],
                "platform_revision": item["revision"]}
    return {"found": False, "authoritative_absent": True}


def _create_record(tx: Tx, ctx: Ctx, action: dict[str, Any], plan: dict[str, Any], outcome: dict[str, Any]) -> None:
    story = action["story_id"]
    agg = get(tx, story, plan["request"]["defect_id"])
    st = dict(agg["state"])
    if st.get("external_id") and st["external_id"] != outcome["remote_object_id"]:
        raise QAError("INTERNAL_INCONSISTENCY", "缺陷已关联其他平台对象", related_refs=[st["external_id"]])
    mapping = ctx.platform_manifest().get("status_mapping") or {}
    st.update({"external_id": outcome["remote_object_id"], "external_status": outcome.get("external_status"),
               "status": ddef.map_external_status(outcome.get("external_status") or "", mapping),
               "platform_revision": outcome.get("platform_revision") or 0})
    _save(tx, agg, st, "defect_created", {"external_id": st["external_id"]})


def plan_attach(tx: Tx, ctx: Ctx, story_id: str, bug_id: str) -> dict[str, Any]:
    agg = get(tx, story_id, bug_id)
    d = agg["state"]
    if not d.get("external_id"):
        raise QAError("PRECONDITION_FAILED", "缺陷尚未在平台创建", related_refs=[bug_id])
    require_capability(ctx.platform_manifest(), "attach")
    return broker.plan_action(tx, ctx, story_id, "defect.attach", ctx.platform_manifest()["id"],
                              {"defect_id": bug_id, "external_id": d["external_id"], "evidence_ref": d["evidence_ref"]},
                              [d["proposal_ref"], d["evidence_ref"]], logical_key=f"attach:{bug_id}:{d['evidence_ref']}")


def _attach_perform(ctx: Ctx, action: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    req = plan["request"]
    with ctx.ws.reader() as rtx:
        data = rtx.read_evidence(req["evidence_ref"])
    pf = ctx.platform()
    file_id = pf.upload(req["external_id"], f"{req['evidence_ref']}.json", data, action["idempotency_key"] + ":upload")
    try:
        pf.link(req["external_id"], file_id)
    except RemoteFailed as exc:
        raise PartialFailure(f"附件已上传但关联失败：{exc}", {"file_id": file_id, "linked": False}, True) from exc
    read = pf.get(req["external_id"])
    if file_id not in read["attachments"]:
        raise PartialFailure("回读未发现附件关联", {"file_id": file_id, "linked": False}, True)
    return {"state": "succeeded", "remote_object_id": f"{req['external_id']}#{file_id}", "file_id": file_id,
            "linked": True}


def _attach_query(ctx: Ctx, action: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    read = ctx.platform().get(plan["request"]["external_id"])
    if read["attachments"]:
        return {"found": True, "remote_object_id": f"{read['id']}#{read['attachments'][-1]}",
                "file_id": read["attachments"][-1], "linked": True}
    return {"found": False, "authoritative_absent": True}


def _attach_record(tx: Tx, ctx: Ctx, action: dict[str, Any], plan: dict[str, Any], outcome: dict[str, Any]) -> None:
    agg = get(tx, action["story_id"], plan["request"]["defect_id"])
    st = dict(agg["state"])
    partial = outcome.get("partial") or {}
    st["attachment_file_id"] = outcome.get("file_id") or partial.get("file_id")
    st["attachments_verified"] = bool(outcome.get("linked"))
    _save(tx, agg, st, "defect_attachment", {"verified": st["attachments_verified"]})


def plan_transition(tx: Tx, ctx: Ctx, story_id: str, bug_id: str, status: str, reason: str) -> dict[str, Any]:
    agg = get(tx, story_id, bug_id)
    d = agg["state"]
    if not d.get("external_id"):
        raise QAError("PRECONDITION_FAILED", "缺陷尚未在平台创建", related_refs=[bug_id])
    require_capability(ctx.platform_manifest(), "transition")
    return broker.plan_action(tx, ctx, story_id, "defect.transition", ctx.platform_manifest()["id"],
                              {"defect_id": bug_id, "external_id": d["external_id"], "status": status, "reason": reason},
                              [d["proposal_ref"]] + d["retests"][-1:],
                              logical_key=f"transition:{bug_id}:{status}:{len(d['retests'])}")


def _transition_perform(ctx: Ctx, action: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    req = plan["request"]
    read = ctx.platform().transition(req["external_id"], req["status"])
    return {"state": "succeeded", "remote_object_id": f"{req['external_id']}@{read['revision']}",
            "external_status": read["status"], "platform_revision": read["revision"]}


def _transition_query(ctx: Ctx, action: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    read = ctx.platform().get(plan["request"]["external_id"])
    if read["status"] == plan["request"]["status"]:
        return {"found": True, "remote_object_id": f"{read['id']}@{read['revision']}",
                "external_status": read["status"], "platform_revision": read["revision"]}
    return {"found": False, "reason": "平台状态与计划不同，需人工核对"}


def _transition_record(tx: Tx, ctx: Ctx, action: dict[str, Any], plan: dict[str, Any], outcome: dict[str, Any]) -> None:
    agg = get(tx, action["story_id"], plan["request"]["defect_id"])
    st = dict(agg["state"])
    if (outcome.get("platform_revision") or 0) > (st.get("platform_revision") or 0):
        mapping = ctx.platform_manifest().get("status_mapping") or {}
        st["external_status"] = outcome["external_status"]
        st["status"] = ddef.map_external_status(outcome["external_status"], mapping)
        st["platform_revision"] = outcome["platform_revision"]
    _save(tx, agg, st, "defect_transitioned", {"status": st["status"]})


broker.handler("defect.create", perform=_create_perform, record=_create_record, query=_create_query)
broker.handler("defect.attach", perform=_attach_perform, record=_attach_record, query=_attach_query)
broker.handler("defect.transition", perform=_transition_perform, record=_transition_record, query=_transition_query)


def sync(ctx: Ctx, story_id: str) -> dict[str, Any]:
    """从平台读取事件：重复事件去重，旧修订不覆盖新状态；未映射状态保持 unknown。"""
    pf = ctx.platform()
    mapping = ctx.platform_manifest().get("status_mapping") or {}
    with ctx.ws.reader() as rtx:
        since = int(rtx.meta_get(f"platform_cursor:{story_id}", "0") or 0)
    events = pf.events(since)
    applied, skipped = [], []
    with ctx.ws.transaction(new_id("CMD")) as tx:
        by_ext = {d["external_id"]: d for d in all_defects(tx, story_id) if d.get("external_id")}
        max_seq = since
        for ev in events:
            max_seq = max(max_seq, ev["seq"])
            d = by_ext.get(ev["item_id"])
            if d is None:
                continue
            seen = {r["event_id"] for r in tx.conn.execute("SELECT event_id FROM platform_events")}
            agg = get(tx, story_id, d["id"])
            res = ddef.apply_platform_event(agg["state"], {"event_id": ev["event_id"], "status": ev["status"],
                                                           "platform_revision": ev["revision"]}, seen, mapping)
            tx.conn.execute("INSERT OR IGNORE INTO platform_events(event_id, object_ref, platform_revision, received_at, "
                            "applied) VALUES(?,?,?,?,?)", (ev["event_id"], d["id"], ev["revision"], tx.now,
                                                          1 if res["applied"] else 0))
            if res["applied"]:
                st = res["state"]
                if st["status"] == "resolved":
                    item = pf.get(d["external_id"])  # 回读修复信息
                    st["fix_commit"] = item["fields"].get("fix_commit")
                    st["fix_build"] = item["fields"].get("fix_build")
                _save(tx, agg, st, "defect_synced", {"status": st["status"], "revision": st["platform_revision"]})
                by_ext[d["external_id"]] = st
                applied.append({"defect": d["id"], "status": st["status"]})
            else:
                skipped.append({"defect": d["id"], "reason": res["reason"]})
        # 乱序事件可能留下较旧状态：按回读的当前对象核对
        for ext, d in by_ext.items():
            item = pf.get(ext)
            agg = get(tx, story_id, d["id"])
            if item["revision"] > (agg["state"].get("platform_revision") or 0):
                st = dict(agg["state"])
                st.update({"external_status": item["status"], "status": ddef.map_external_status(item["status"], mapping),
                           "platform_revision": item["revision"]})
                _save(tx, agg, st, "defect_readback", {"status": st["status"]})
        tx.meta_set(f"platform_cursor:{story_id}", str(max_seq))
        tx.meta_set(f"defect_synced_at:{story_id}", tx.now)
    return {"applied": applied, "skipped": skipped, "events": len(events)}


def record_retest(tx: Tx, story_id: str, bug_id: str, attempt_id: str) -> None:
    agg = get(tx, story_id, bug_id)
    st = dict(agg["state"])
    if attempt_id not in st["retests"]:
        st["retests"] = st["retests"] + [attempt_id]
        _save(tx, agg, st, "defect_retest_linked", {"attempt": attempt_id})


def close_check(tx: Tx, ctx: Ctx, story_id: str, bug_id: str, attempts: list[dict[str, Any]]) -> dict[str, Any]:
    d = get(tx, story_id, bug_id)["state"]
    retests = [a for a in attempts if a["id"] in d["retests"]]
    neighbors_required = [t for t in (ctx.policy.get("required_regression") or []) if t != d["tc_ref"]]
    neighbor_results = [a for a in attempts if a["tc_ref"] in neighbors_required]
    grant_ok = broker._grant_for(ctx, ctx.platform_manifest()["id"], ctx.environment_of(ctx.platform_manifest()["id"]),
                                 "defect.transition") is not None
    res = ddef.close_requirements(d, retests, neighbor_results, neighbors_required, close_decision_valid=True)
    res["authorization"] = "standing_grant" if grant_ok else "decision_required"
    return res


def request_disposition(tx: Tx, ctx: Ctx, story_id: str, bug_id: str, disposition: str, reason: str) -> dict[str, Any]:
    if disposition not in ddef.DISPOSITIONS:
        raise QAError("INVALID_ARGUMENT", f"处置只能是 {sorted(ddef.DISPOSITIONS)}")
    from qa_flow.application.review import raise_item

    d = get(tx, story_id, bug_id)["state"]
    item = raise_item(tx, ctx, story_id, kind="disposition", key=f"disp-{bug_id}-{disposition}",
                      question=f"缺陷“{d['title']}”是否按“{disposition}”处置？理由：{reason}",
                      basis=[{"source_ref": d["proposal_ref"], "quote": d["title"]}],
                      affected_scope=f"用例 {d['tc_ref']} 的失败结果仍保留", responsible_role="product",
                      options=[{"key": "A", "label": f"同意按 {disposition} 处置"}, {"key": "reject", "label": "不同意"}],
                      subject_refs=[d["proposal_ref"]], raised_by="defects", extra={"disposition": disposition,
                                                                                   "defect_id": bug_id})
    return {"handle": item["handle"], "status": "disposition_pending",
            "note": "处置需要产品负责人的有效决定；在决定前不视为已接受，测试失败不会消除"}
