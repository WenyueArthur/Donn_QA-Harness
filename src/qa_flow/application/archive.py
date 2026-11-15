"""归档与通知。

归档是独立动作：读取终稿，不写回终稿地址，也不改变阶段状态；
目标地址、平台对象 ID、快照和归档状态放在独立记录中。
目标文档必须明确属于获准的 QA 归档范围；原文及其副本始终受保护。
"""

from __future__ import annotations

from typing import Any

from qa_flow.adapters.base import RemoteFailed, require_capability
from qa_flow.application import broker
from qa_flow.application.context import Ctx
from qa_flow.application.core import protected_identities
from qa_flow.errors import QAError
from qa_flow.security.resources import ensure_writable, resolve_identity
from qa_flow.storage.store import Tx


def plan_archive(tx: Tx, ctx: Ctx, story_id: str, report_ref: str, space: str, existing_doc: str | None = None,
                 if_match: str | None = None) -> dict[str, Any]:
    report = tx.require_snapshot(report_ref)
    allowed = (ctx.config.get("workspace") or {}).get("archive_spaces") or (["lab-qa-archive"] if ctx.is_lab else [])
    if space not in allowed:
        raise QAError("PROTECTED_RESOURCE", f"目标空间 {space} 不在获准的 QA 归档范围内", related_refs=[space])
    channel = "archive_overwrite" if existing_doc else "body"
    ensure_writable(resolve_identity(f"doc://{space}/{existing_doc or 'new'}"), channel, protected_identities(tx))
    require_capability(ctx.platform_manifest(), "doc_update_etag" if existing_doc else "doc_create")
    from qa_flow.reporting.render import report_markdown

    content = report_markdown(report["payload"], report["kind"])
    request = {"space": space, "title": f"QA 归档 {report_ref}", "content": content, "report_ref": report_ref,
               "report_sha256": report["sha256"], "existing_doc": existing_doc, "if_match": if_match}
    return broker.plan_action(tx, ctx, story_id, "doc.archive", ctx.platform_manifest()["id"], request, [report_ref],
                              logical_key=f"archive:{report_ref}:{space}:{existing_doc or ''}")


def _archive_perform(ctx: Ctx, action: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    r = plan["request"]
    pf = ctx.platform()
    if r.get("existing_doc"):
        doc = pf.doc_update(r["existing_doc"], r["content"], r.get("if_match") or "")
    else:
        doc = pf.doc_create(r["space"], r["title"], r["content"], action["idempotency_key"])
    read = pf.doc_get(doc["id"])
    if read["content"] != r["content"] or not read["complete"]:
        raise RemoteFailed("回读内容与归档内容不一致")
    return {"state": "succeeded", "remote_object_id": read["id"], "etag": read["etag"]}


def _archive_query(ctx: Ctx, action: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    return {"found": False, "reason": "文档创建不可按幂等键回查；需人工核对"}


def _archive_record(tx: Tx, ctx: Ctx, action: dict[str, Any], plan: dict[str, Any], outcome: dict[str, Any]) -> None:
    r = plan["request"]
    tx.put_snapshot("archive_record", {"report_ref": r["report_ref"], "target_id": action["target_id"],
                                       "action_ref": action["id"], "state": "archived",
                                       "remote_object_id": outcome["remote_object_id"],
                                       "report_sha256": r["report_sha256"], "etag": outcome.get("etag")},
                    story_id=action["story_id"], project_id=ctx.project_id,
                    producer={"kind": "worker", "name": "broker", "verification": "worker_receipt"})


def _notify_perform(ctx: Ctx, action: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    nid = ctx.platform().notify(plan["request"]["body"], action["idempotency_key"])
    return {"state": "succeeded", "remote_object_id": nid}


def _notify_record(tx: Tx, ctx: Ctx, action: dict[str, Any], plan: dict[str, Any], outcome: dict[str, Any]) -> None:
    tx.event(action["id"], "notification_sent", {"remote_object_id": outcome["remote_object_id"]})


broker.handler("doc.archive", perform=_archive_perform, record=_archive_record, query=_archive_query)
broker.handler("notify.send", perform=_notify_perform, record=_notify_record, query=None)
