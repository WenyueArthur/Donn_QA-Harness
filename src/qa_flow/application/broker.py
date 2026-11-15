"""统一操作调度与结果核对。

状态机：planned → awaiting_authorization → ready → running →
succeeded | failed | unknown | cancelled；unknown → reconciling → succeeded | failed | unknown。

执行顺序：固定计划 → 取得决定或既有权限 → 事务内登记 → 领取租约（递增代次）→
发送前重新检查 → 事务外执行 → 校验响应与回读 → 提交结果事件。
"""

from __future__ import annotations

import json
import os
import socket
from typing import Any, Callable

from qa_flow import clock, faults
from qa_flow.adapters.base import OutcomeUnknown, RateLimited, RemoteFailed
from qa_flow.application.context import Ctx
from qa_flow.application.core import decision_valid, dep, effective_current
from qa_flow.canonical import sha256_obj
from qa_flow.domain.review import authorization_applies
from qa_flow.errors import QAError
from qa_flow.ids import new_id
from qa_flow.security.resources import resolve_target
from qa_flow.storage.store import Tx

# 动作类型 → 处理器 {perform(ctx, action, plan) -> outcome, record(tx, ctx, action, plan, outcome), query(...)}
HANDLERS: dict[str, dict[str, Callable[..., Any]]] = {}


def handler(kind: str, *, perform: Callable, record: Callable, query: Callable | None = None) -> None:
    HANDLERS[kind] = {"perform": perform, "record": record, "query": query}


def _load_handlers() -> None:
    if HANDLERS:
        return
    from qa_flow.application import archive, defects, execution  # noqa: F401  注册处理器


def worker_id() -> str:
    return f"worker:{socket.gethostname()}:{os.getpid()}"


def _grant_for(ctx: Ctx, target_id: str, environment: str, kind: str) -> str | None:
    for g in ctx.policy.get("standing_grants") or []:
        if g.get("target_id") == target_id and g.get("environment") == environment and kind in g.get("action_kinds", []):
            if environment != "lab":
                return None  # 真实环境的既有权限必须来自外部核验，不接受本地策略自述
            return f"policy:{sha256_obj(g)[:16]}"
    return None


def get_action(tx: Tx, action_id: str) -> dict[str, Any]:
    row = tx.conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    if row is None:
        raise QAError("NOT_FOUND", f"操作不存在: {action_id}", related_refs=[action_id])
    a = dict(row)
    a["remote_receipt"] = json.loads(a["remote_receipt"]) if a["remote_receipt"] else None
    return a


def actions(tx: Tx, story_id: str | None = None, states: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    q = "SELECT id FROM actions WHERE 1=1"
    args: list[Any] = []
    if story_id:
        q += " AND story_id=?"
        args.append(story_id)
    if states:
        q += f" AND state IN ({','.join('?' * len(states))})"
        args += list(states)
    q += " ORDER BY created_at, id"
    return [get_action(tx, r["id"]) for r in tx.conn.execute(q, args)]


def _set_state(tx: Tx, a: dict[str, Any], state: str, **fields: Any) -> None:
    sets = ["state=?", "revision=revision+1", "updated_at=?"]
    vals: list[Any] = [state, tx.now]
    for k, v in fields.items():
        sets.append(f"{k}=?")
        vals.append(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
    cur = tx.conn.execute(f"UPDATE actions SET {', '.join(sets)} WHERE id=? AND revision=?",
                          (*vals, a["id"], a["revision"]))
    if cur.rowcount != 1:
        raise QAError("REVISION_CONFLICT", "操作记录已被并发更新", related_refs=[a["id"]])
    tx.event(a["id"], f"action_{state}", {k: v for k, v in fields.items() if k != "remote_receipt"},
             a["revision"], a["revision"] + 1)
    a["revision"] += 1
    a["state"] = state


def plan_action(tx: Tx, ctx: Ctx, story_id: str, kind: str, target_id: str, request: dict[str, Any],
                input_refs: list[str], logical_key: str | None = None) -> dict[str, Any]:
    target = resolve_target(ctx.config, target_id, kind)
    environment = target["environment"]
    plan = {"kind": kind, "target_id": target_id, "environment": environment, "request": request,
            "input_refs": [{"ref": d["ref"], "sha256": d["sha256"]} for d in (dep(tx, r) for r in input_refs)]}
    plan_sha = sha256_obj(plan)
    idem = sha256_obj([ctx.project_id, story_id, kind, target_id, logical_key or plan_sha])[:32]
    row = tx.conn.execute("SELECT id FROM actions WHERE idempotency_key=?", (idem,)).fetchone()
    if row:
        a = get_action(tx, row["id"])
        if a["plan_sha256"] == plan_sha:
            return a
        if a["state"] in ("planned", "awaiting_authorization", "ready", "failed", "cancelled"):
            # 内容变化：旧计划取消并保留为历史，新计划需要重新授权
            _set_state(tx, a, "cancelled", last_error="计划内容已变化，原计划取消")
            idem = sha256_obj([idem, plan_sha])[:32]
        else:
            raise QAError("PRECONDITION_FAILED", "同一逻辑操作已在执行或结果未知，先核对再决定",
                          related_refs=[a["id"]])
    snap = tx.put_snapshot("action_plan", plan, story_id=story_id, project_id=ctx.project_id,
                           producer={"kind": "process", "name": "broker", "verification": "process"},
                           depends_on=[{**d, "relation": "input", "required": True} for d in plan["input_refs"]])
    aid = new_id("ACT")
    tx.conn.execute(
        "INSERT INTO actions(id, story_id, kind, target_id, environment, plan_ref, plan_sha256, idempotency_key, "
        "state, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?, 'planned', ?, ?)",
        (aid, story_id, kind, target_id, environment, snap["id"], plan_sha, idem, tx.now, tx.now))
    tx.event(aid, "action_planned", {"kind": kind, "target_id": target_id, "plan_sha256": plan_sha}, 0, 1)
    a = get_action(tx, aid)
    authorize(tx, ctx, a)
    return get_action(tx, aid)


def authorize(tx: Tx, ctx: Ctx, a: dict[str, Any]) -> str | None:
    grant = _grant_for(ctx, a["target_id"], a["environment"], a["kind"])
    if grant:
        _set_state(tx, a, "ready", authorization_ref=grant)
        return grant
    ref = _decision_for(tx, a)
    if ref:
        _set_state(tx, a, "ready", authorization_ref=ref)
        return ref
    if a["state"] != "awaiting_authorization":
        _set_state(tx, a, "awaiting_authorization")
    from qa_flow.application import review

    plan = tx.get_snapshot(a["plan_ref"])["payload"]
    review.raise_item(
        tx, ctx, a["story_id"], kind="action_authorization", key=f"auth-{a['id']}",
        question=f"是否允许在 {a['target_id']}（{a['environment']}）执行 {describe_kind(a['kind'])}？",
        basis=[{"source_ref": a["plan_ref"], "quote": f"计划摘要 {a['plan_sha256'][:12]}"}],
        affected_scope=summarize_request(plan), responsible_role=ctx.policy.get("authorizer_role", "tester"),
        options=[{"key": "A", "label": "批准该计划（仅此内容、目标与环境）"}, {"key": "reject", "label": "不批准"}],
        subject_refs=[a["plan_ref"]], raised_by="broker", plan_sha256=a["plan_sha256"], environment=a["environment"],
        target_ids=[a["target_id"]], action_kinds=[a["kind"]])
    return None


def _decision_for(tx: Tx, a: dict[str, Any]) -> str | None:
    rows = tx.conn.execute("SELECT id FROM decisions WHERE plan_sha256=? AND result='approve' AND revoked_at IS NULL",
                           (a["plan_sha256"],)).fetchall()
    for r in rows:
        row = tx.conn.execute("SELECT * FROM decisions WHERE id=?", (r["id"],)).fetchone()
        d = dict(row)
        d["scope"] = json.loads(d["scope"])
        problems = authorization_applies(d, plan_sha256=a["plan_sha256"], environment=a["environment"],
                                         target_id=a["target_id"], action_kind=a["kind"])
        if not problems and decision_valid(tx, r["id"])["valid"]:
            return r["id"]
    return None


def describe_kind(kind: str) -> str:
    return {"execute.cases": "执行测试用例", "environment.probe": "环境探测", "deploy.lab_build": "部署实验构建",
            "rollback.drill": "回滚演练", "defect.create": "创建缺陷", "defect.attach": "上传并关联缺陷附件",
            "defect.transition": "流转缺陷状态", "doc.archive": "归档文档", "notify.send": "发送通知"}.get(kind, kind)


def summarize_request(plan: dict[str, Any]) -> str:
    r = plan.get("request") or {}
    keys = ", ".join(f"{k}={v}" for k, v in sorted(r.items()) if isinstance(v, (str, int)) and k != "content")
    return f"{describe_kind(plan['kind'])}：{keys}"[:200]


def _authorization_still_valid(tx: Tx, ctx: Ctx, a: dict[str, Any]) -> list[str]:
    ref = a.get("authorization_ref")
    if not ref:
        return ["没有授权"]
    if ref.startswith("policy:"):
        return [] if _grant_for(ctx, a["target_id"], a["environment"], a["kind"]) == ref else ["既有权限策略已变化"]
    row = tx.conn.execute("SELECT * FROM decisions WHERE id=?", (ref,)).fetchone()
    if row is None:
        return ["授权决定不存在"]
    d = dict(row)
    d["scope"] = json.loads(d["scope"])
    problems = authorization_applies(d, plan_sha256=a["plan_sha256"], environment=a["environment"],
                                     target_id=a["target_id"], action_kind=a["kind"])
    st = decision_valid(tx, ref)
    return problems + st["reasons"]


def dispatch(ctx: Ctx, action_id: str, *, worker: str | None = None) -> dict[str, Any]:
    """领取租约并执行。结果未知的操作禁止盲目重发，必须先核对。"""
    _load_handlers()
    worker = worker or worker_id()
    lease_s = float(ctx.budgets.get("exec_timeout_seconds", 60)) * 3
    with ctx.ws.transaction(new_id("CMD")) as tx:
        a = get_action(tx, action_id)
        if a["state"] in ("succeeded",):
            return {"action_id": action_id, "state": a["state"], "replayed": True}
        if a["state"] in ("unknown", "reconciling"):
            raise QAError("OUTCOME_UNKNOWN", "该操作结果未知，禁止重发；请先核对", related_refs=[action_id])
        if a["state"] == "running":
            if a["lease_expires_at"] and clock.parse_ts(a["lease_expires_at"]) > clock.parse_ts(tx.now):
                raise QAError("PRECONDITION_FAILED", "操作正由其他工作进程执行", related_refs=[action_id])
            # 租约过期不代表可以重新执行：结果可能已经发生
            _set_state(tx, a, "unknown", last_error="租约过期，原工作进程结果未知")
            return {"action_id": action_id, "state": "unknown", "reason": "租约过期，需要核对"}
        if a["state"] in ("planned", "awaiting_authorization", "failed"):
            if a["state"] == "failed" and not _retryable(a):
                raise QAError("PRECONDITION_FAILED", "该操作已核验失败；如需重新执行请重新规划", related_refs=[action_id])
            authorize(tx, ctx, a)
            a = get_action(tx, action_id)
            if a["state"] != "ready":
                raise QAError("AUTHORIZATION_REQUIRED", "该操作需要责任人批准后才能执行", related_refs=[action_id])
        if a["state"] == "cancelled":
            raise QAError("PRECONDITION_FAILED", "操作已取消", related_refs=[action_id])
        epoch = a["lease_epoch"] + 1
        _set_state(tx, a, "running", lease_epoch=epoch, lease_owner=worker,
                   lease_expires_at=clock.plus_hours(tx.now, lease_s / 3600),
                   attempts=a["attempts"] + 1)
        tx.conn.execute("INSERT INTO action_attempts(action_id, lease_epoch, worker_id, started_at) VALUES(?,?,?,?)",
                        (action_id, epoch, worker, tx.now))
        plan = tx.get_snapshot(a["plan_ref"])["payload"]
    faults.pause_point("before_send_check")
    # 发送前重新检查：决定、源依赖、目标状态与白名单
    with ctx.ws.reader() as rtx:
        problems = _authorization_still_valid(rtx, ctx, a)
        for inp in plan["input_refs"]:
            if inp["ref"].startswith("EV-"):
                ev = rtx.get_evidence(inp["ref"])
                if ev is None or ev["sha256"] != inp["sha256"] or not rtx.evidence_valid(inp["ref"]):
                    problems.append(f"证据 {inp['ref']} 已失效")
                continue
            s = rtx.get_snapshot(inp["ref"])
            if s is None or s["sha256"] != inp["sha256"] or not effective_current(rtx, inp["ref"]):
                problems.append(f"输入 {inp['ref']} 已变化或不再有效")
        try:
            resolve_target(ctx.config, a["target_id"], a["kind"])
        except QAError as exc:
            problems.append(exc.message)
    if problems:
        with ctx.ws.transaction(new_id("CMD")) as tx:
            a2 = get_action(tx, action_id)
            _finish_attempt(tx, action_id, epoch, "rejected_before_send", {"problems": problems})
            _set_state(tx, a2, "awaiting_authorization", authorization_ref=None, last_error="；".join(problems))
        raise QAError("AUTHORIZATION_INVALID", "发送前重新核验未通过，未发送：" + "；".join(problems),
                      related_refs=[action_id], details={"problems": problems, "external_calls": 0})
    h = HANDLERS[a["kind"]]
    faults.crash_point("before_send")
    try:
        outcome = h["perform"](ctx, a, plan)
        outcome.setdefault("state", "succeeded")
    except OutcomeUnknown as exc:
        outcome = {"state": "unknown", "error": str(exc)}
    except RateLimited as exc:
        outcome = {"state": "failed", "error": str(exc), "retryable": True}
    except RemoteFailed as exc:
        outcome = {"state": "failed", "error": str(exc), "partial": getattr(exc, "partial", None),
                   "retryable": bool(getattr(exc, "retryable", False))}
    faults.crash_point("after_send")
    return _record(ctx, action_id, epoch, outcome)


def _retryable(a: dict[str, Any]) -> bool:
    return bool((a.get("remote_receipt") or {}).get("retryable"))


def _finish_attempt(tx: Tx, action_id: str, epoch: int, outcome: str, receipt: dict[str, Any], late: bool = False) -> None:
    tx.conn.execute("UPDATE action_attempts SET finished_at=?, outcome=?, receipt=?, late=? "
                    "WHERE action_id=? AND lease_epoch=? AND finished_at IS NULL",
                    (tx.now, outcome, json.dumps(_safe(receipt), ensure_ascii=False), 1 if late else 0,
                     action_id, epoch))


def _safe(obj: Any) -> Any:
    if isinstance(obj, bytes):
        return {"bytes": len(obj)}
    if isinstance(obj, dict):
        return {k: _safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_safe(v) for v in obj]
    return obj


def _record(ctx: Ctx, action_id: str, epoch: int, outcome: dict[str, Any]) -> dict[str, Any]:
    with ctx.ws.transaction(new_id("CMD")) as tx:
        a = get_action(tx, action_id)
        if a["lease_epoch"] != epoch or a["state"] != "running":
            # 旧工作进程的晚到回执不能覆盖新修订，必须进入核对
            _finish_attempt(tx, action_id, epoch, f"late_{outcome['state']}", outcome, late=True)
            tx.event(action_id, "late_receipt", {"epoch": epoch, "outcome": outcome["state"]})
            if outcome["state"] == "succeeded" and a["state"] not in ("succeeded",):
                _set_state(tx, a, "reconciling", last_error="收到过期租约的成功回执，需核对")
            return {"action_id": action_id, "state": a["state"], "late_receipt": True}
        _finish_attempt(tx, action_id, epoch, outcome["state"], outcome)
        receipt = {k: v for k, v in _safe(outcome).items() if k not in ("data",)}
        if outcome["state"] == "succeeded":
            HANDLERS[a["kind"]]["record"](tx, ctx, a, tx.get_snapshot(a["plan_ref"])["payload"], outcome)
            _set_state(tx, a, "succeeded", remote_receipt=receipt, remote_object_id=outcome.get("remote_object_id"),
                       last_error=None)
        elif outcome["state"] == "unknown":
            _set_state(tx, a, "unknown", remote_receipt=receipt, last_error=outcome.get("error"))
        else:
            if outcome.get("partial"):
                HANDLERS[a["kind"]]["record"](tx, ctx, a, tx.get_snapshot(a["plan_ref"])["payload"], outcome)
            _set_state(tx, a, "failed", remote_receipt=receipt, last_error=outcome.get("error"),
                       remote_object_id=outcome.get("remote_object_id"))
        return {"action_id": action_id, "state": a["state"], "remote_object_id": outcome.get("remote_object_id"),
                "error": outcome.get("error")}


def reconcile(ctx: Ctx, action_id: str) -> dict[str, Any]:
    """核对未知结果：查询到唯一远端对象才标成功；不能查明则保持 unknown。"""
    _load_handlers()
    with ctx.ws.reader() as rtx:
        a = get_action(rtx, action_id)
        plan = rtx.get_snapshot(a["plan_ref"])["payload"]
    if a["state"] not in ("unknown", "reconciling"):
        return {"action_id": action_id, "state": a["state"], "reconciled": False}
    q = HANDLERS[a["kind"]].get("query")
    if q is None:
        return {"action_id": action_id, "state": a["state"], "reconciled": False,
                "reason": "平台不支持查询；需要人工核对，不会重发"}
    try:
        found = q(ctx, a, plan)
    except (RemoteFailed, OutcomeUnknown) as exc:
        return {"action_id": action_id, "state": a["state"], "reconciled": False, "reason": str(exc)}
    with ctx.ws.transaction(new_id("CMD")) as tx:
        a = get_action(tx, action_id)
        if a["state"] not in ("unknown", "reconciling"):
            return {"action_id": action_id, "state": a["state"], "reconciled": False}
        if found.get("found"):
            outcome = {"state": "succeeded", "remote_object_id": found.get("remote_object_id"), "reconciled": True,
                       **{k: v for k, v in found.items() if k not in ("found",)}}
            HANDLERS[a["kind"]]["record"](tx, ctx, a, plan, outcome)
            _set_state(tx, a, "succeeded", remote_object_id=found.get("remote_object_id"),
                       remote_receipt=_safe(outcome), last_error=None)
        elif found.get("authoritative_absent"):
            _set_state(tx, a, "failed", last_error="核对确认远端没有该对象",
                       remote_receipt={"retryable": True, "reconciled_absent": True})
        else:
            tx.event(action_id, "reconcile_inconclusive", {"reason": found.get("reason", "")})
        return {"action_id": action_id, "state": a["state"], "reconciled": True,
                "remote_object_id": found.get("remote_object_id")}


def pending_unknown(tx: Tx, story_id: str | None = None) -> list[dict[str, Any]]:
    return actions(tx, story_id, ("unknown", "reconciling"))
