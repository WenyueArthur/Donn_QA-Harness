"""worker tick --once：到期来源刷新、平台同步、提醒与待核对操作。

同一目标与截止点使用稳定任务键，重复 tick 不产生重复通知。
未配置或未授权时不宣称自动提醒已启用，也不发送通知。
"""

from __future__ import annotations

from typing import Any

from qa_flow.application import broker
from qa_flow.application.context import Ctx, execute
from qa_flow.application.core import import_source, source_state
from qa_flow.domain.defects import overdue
from qa_flow.errors import QAError
from qa_flow.ids import stable_id


def tick(ctx: Ctx, dispatch_ready: bool = True) -> dict[str, Any]:
    report: dict[str, Any] = {"refreshed": [], "agent_fetch_required": [], "synced": [], "reminders": [],
                              "reconciled": [], "dispatched": [], "errors": []}
    with ctx.ws.reader() as rtx:
        stories = sorted({a["story_id"] for a in rtx.aggs("task")})
        due_sources = []
        for sid in stories:
            for s in rtx.snapshots(sid, "source_revision"):
                if rtx.is_current(s["id"]) and source_state(rtx, ctx, s) == "stale":
                    if (s["payload"].get("fetched_by") or {}).get("agent"):
                        # Agent 读取的飞书来源：worker 不能自行重读，只登记需要 Agent 重新读取
                        report["agent_fetch_required"].append({"story": sid, "uri": s["payload"]["uri"],
                                                               "title": s["payload"].get("title")})
                        continue
                    due_sources.append((sid, s["payload"]["uri"]))
    for sid, uri in due_sources:
        try:
            r = execute(ctx, "source.refresh", {"story": sid, "uri": uri, "tick": stable_id("TICK", sid, uri)},
                        lambda tx: import_source(tx, ctx, sid, uri))
            report["refreshed"].append({"story": sid, "uri": uri, "changed": r["result"]["changed"],
                                        "read_status": r["result"]["read_status"]})
        except QAError as exc:
            report["errors"].append({"step": "refresh", "code": exc.code, "message": exc.message})
    for sid in stories:
        try:
            with ctx.ws.reader() as rtx:
                has_ext = any(a["state"].get("external_id") for a in rtx.aggs("defect", sid))
            if has_ext:
                from qa_flow.application.defects import sync

                report["synced"].append({"story": sid, **sync(ctx, sid)})
        except QAError as exc:
            report["errors"].append({"step": "sync", "code": exc.code, "message": exc.message})
    reminders = ((ctx.config.get("timing") or {}).get("reminders") or {})
    if reminders.get("enabled"):
        hours = float(reminders.get("defect_deadline_hours") or 24)
        for sid in stories:
            with ctx.ws.reader() as rtx:
                due = [(a["state"], overdue(a["state"], rtx.now, hours)) for a in rtx.aggs("defect", sid)]
            for d, deadline in due:
                if not deadline:
                    continue
                key = f"notify:{d['id']}:{deadline}"

                def plan(tx, d=d, deadline=deadline, key=key):
                    a = broker.plan_action(tx, ctx, sid, "notify.send", ctx.platform_manifest()["id"],
                                           {"body": f"缺陷 {d['id']} 已超过处理截止时间 {deadline}", "defect": d["id"],
                                            "deadline": deadline}, [d["proposal_ref"]], logical_key=key)
                    return {"action_id": a["id"], "state": a["state"]}

                try:
                    r = execute(ctx, "notify.plan", {"key": key}, plan, command_id=stable_id("CMD", key))
                    report["reminders"].append({"story": sid, "defect": d["id"], **r["result"],
                                                "note": "通知未获授权时只登记待发送动作，不发送"})
                except QAError as exc:
                    report["errors"].append({"step": "reminder", "code": exc.code, "message": exc.message})
    with ctx.ws.reader() as rtx:
        unknown = [a["id"] for a in broker.pending_unknown(rtx)]
        ready = [a["id"] for a in broker.actions(rtx, None, ("ready",))]
    for aid in unknown:
        report["reconciled"].append(broker.reconcile(ctx, aid))
    if dispatch_ready:
        for aid in ready:
            try:
                report["dispatched"].append(broker.dispatch(ctx, aid))
            except QAError as exc:
                report["errors"].append({"step": "dispatch", "action": aid, "code": exc.code, "message": exc.message})
    with ctx.ws.transaction(None) as tx:
        tx.meta_set("worker_last_tick_at", tx.now)
        tx.event("worker", "tick", {k: len(v) for k, v in report.items()})
    return report
