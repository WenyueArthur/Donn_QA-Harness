"""阶段正式判定。

阶段状态与业务判定分开；completed 只用于有有效通过判定的阶段。
任一强依赖变化后重新核验；缺服务或缺规则不返回假完成。
"""

from __future__ import annotations

from typing import Any

from qa_flow import clock
from qa_flow.application import broker, review
from qa_flow.application.context import Ctx
from qa_flow.application.core import current, decision_valid, dep, derived_of, effective_current, source_map
from qa_flow.application.execution import attempts as all_attempts
from qa_flow.application.execution import current_deployment, quarantined
from qa_flow.domain import cases as dcases
from qa_flow.domain import execution as dex
from qa_flow.domain import reports as drep
from qa_flow.domain import workflow
from qa_flow.domain.defects import TERMINAL
from qa_flow.errors import QAError
from qa_flow.storage.store import Tx, lineage_key

WORKER_ORIGIN = {"lab": "local_lab", "test": "test"}


class Eval:
    def __init__(self, stage: str):
        self.stage = stage
        self.verdict = "indeterminate"
        self.reasons: list[str] = []
        self.inputs: list[str] = []
        self.decisions: list[str] = []
        self.blocked = False
        self.waived = False
        self.review_required = False
        self.extra: dict[str, Any] = {}

    def need(self, reason: str, *, blocked: bool = True, review: bool = False) -> "Eval":
        self.reasons.append(reason)
        self.blocked = self.blocked or blocked
        self.review_required = self.review_required or review
        return self


def statuses(tx: Tx, story_id: str) -> dict[str, str]:
    out = {s: "pending" for s in workflow.stage_ids()}
    for a in tx.aggs("stage", story_id):
        out[a["state"]["stage"]] = a["state"]["status"]
    return out


def stage_state(tx: Tx, story_id: str, stage_id: str) -> dict[str, Any] | None:
    a = tx.get_agg(f"stage:{story_id}:{stage_id}")
    return a["state"] if a else None


def _upstream(e: Eval, tx: Tx, story_id: str) -> bool:
    missing = workflow.upstream_satisfied(e.stage, statuses(tx, story_id))
    if missing:
        titles = "、".join(workflow.stage(m)["title"] for m in missing)
        e.need(f"等待上游阶段完成：{titles}")
        return False
    return True


def _src_ok(e: Eval, tx: Tx, ctx: Ctx, story_id: str, refs: list[str]) -> bool:
    smap = source_map(tx, ctx, story_id)
    ok = True
    for r in refs:
        st = smap.get(r, {}).get("read_status", "missing")
        if st != "available":
            e.need(f"来源 {smap.get(r, {}).get('title') or r} 状态为 {st}，新鲜度不足，不能形成肯定结论")
            ok = False
    return ok


def _explain_sources(e: Eval, tx: Tx, refs: list[str]) -> None:
    """来源的最新快照读取受阻时，明确说明“读取受阻”及原因，而不只是“已变化”。"""
    for r in refs:
        snap = tx.get_snapshot(r)
        if snap is None or not snap.get("lineage"):
            continue
        cur_id = tx.lineage_current(snap["lineage"])
        cur = tx.get_snapshot(cur_id) if cur_id else None
        if cur and cur["payload"].get("read_status") not in (None, "available"):
            name = cur["payload"].get("title") or cur["payload"].get("uri")
            e.reasons.append(f"来源 {name} 读取受阻（{cur['payload']['read_status']}："
                             f"{cur['payload'].get('failure_reason') or '原因未知'}）；恢复读取后刷新来源再继续")


def _validation_for(tx: Tx, subject_ref: str) -> dict[str, Any] | None:
    for s in reversed(tx.snapshots(None, "validation_report")):
        if s["payload"]["subject_ref"] == subject_ref and effective_current(tx, s["id"]):
            return s
    return None


# ---------------------------------------------------------------------------

def _req_gate(e: Eval, tx: Tx, ctx: Ctx, story_id: str) -> None:
    qr = current(tx, "quality_review", story_id)
    if qr is None:
        e.need("缺少需求质检报告", blocked=False)
        return
    e.inputs.append(qr["id"])
    if not effective_current(tx, qr["id"]):
        e.need("需求质检报告依赖的来源已变化，需要重新质检")
        _explain_sources(e, tx, qr["payload"]["source_refs"])
        return
    if not _src_ok(e, tx, ctx, story_id, qr["payload"]["source_refs"]):
        return
    _rule_verdict(e, tx, ctx, qr, "req_gate", lambda p, r: (
        "fail" if any(f["severity"] in r.get("fail_if_severity", ["blocker"]) for f in p["findings"])
        or (p["testability"]["verdict"] == "fail" and r.get("testability_fail_blocks", True)) else "pass"))


def _rule_verdict(e: Eval, tx: Tx, ctx: Ctx, snap: dict[str, Any], stage: str, lab_fn) -> None:
    kind = (ctx.config.get("rules") or {}).get("kind")
    if kind == "builtin_lab":
        rules = ctx.rules().get("stages", {}).get(stage, {})
        e.verdict = lab_fn(snap["payload"], rules)
        if e.verdict == "fail":
            e.reasons.append("按冻结规则判定不通过")
        return
    if kind == "legacy_bridge":
        v = _validation_for(tx, snap["id"])
        if v is None:
            e.need("旧规则校验结果缺失；执行 api stage evaluate 时会调用冻结的旧校验器", blocked=False)
            return
        e.inputs.append(v["id"])
        structure = v["payload"]["structure"]
        e.verdict = structure.get("verdict", "indeterminate")
        if structure.get("error") or v["payload"].get("validator") == "unavailable":
            e.verdict = "indeterminate"
            e.need(f"旧规则校验器不可用：{structure.get('error') or '未知原因'}；阶段不可判定，不能视为通过")
            return
        if e.verdict != "pass":
            e.reasons.append(f"旧规则校验结论：{e.verdict}")
        return
    e.need("规则来源未配置，阶段不可判定")


def _tech_gate(e: Eval, tx: Tx, ctx: Ctx, story_id: str) -> None:
    if not _upstream(e, tx, story_id):
        return
    tr = current(tx, "technical_review", story_id)
    waiver = _waiver(tx, story_id, "tech_gate")
    if tr is None or tr["payload"]["applicability"] != "provided":
        if tr is not None:
            e.inputs.append(tr["id"])
            label = {"unreadable": "技术材料读取受阻", "not_provided": "未提供技术材料"}[tr["payload"]["applicability"]]
        else:
            label = "缺少技术方案质检"
        if waiver:
            e.decisions.append(waiver)
            e.waived = True
            e.verdict = "not_applicable"
            e.reasons.append(f"{label}；已有有效的不适用决定")
            return
        e.need(f"{label}；不能视为通过或无方案，需要提交技术质检或由责任人作出适用性决定",
               blocked=tr is not None, review=tr is not None)
        if tr is not None:
            review.raise_item(tx, ctx, story_id, kind="applicability", key="applicability-tech_gate",
                              question=f"{label}。技术方案质检是否对本需求不适用？",
                              basis=[{"source_ref": tr["id"], "quote": label}],
                              affected_scope="技术方案质检阶段；不适用时仍需说明原因与范围",
                              responsible_role="developer",
                              options=[{"key": "A", "label": "不适用（需写明原因）"}, {"key": "reject", "label": "适用，补充材料"}],
                              subject_refs=[tr["id"]], raised_by="tech_gate")
        return
    e.inputs.append(tr["id"])
    if not effective_current(tx, tr["id"]) or not _src_ok(e, tx, ctx, story_id, tr["payload"]["source_refs"]):
        if not e.reasons:
            e.need("技术质检依赖的来源已变化")
        _explain_sources(e, tx, tr["payload"]["source_refs"])
        return
    results = [c["result"] for c in tr["payload"]["checks"]]
    if "fail" in results:
        e.verdict = "fail"
        e.reasons.append("存在未通过的技术检查")
    elif "indeterminate" in results:
        e.need("存在不可判定的技术检查", blocked=False)
    else:
        e.verdict = "pass"


def _waiver(tx: Tx, story_id: str, stage: str) -> str | None:
    for a in tx.aggs("review_item", story_id):
        s = a["state"]
        if s["kind"] == "applicability" and s["key"] == f"applicability-{stage}" and s["status"] == "answered":
            if s.get("decision_ref") and decision_valid(tx, s["decision_ref"])["valid"]:
                row = tx.conn.execute("SELECT result FROM decisions WHERE id=?", (s["decision_ref"],)).fetchone()
                if row and row["result"] == "waive":
                    return s["decision_ref"]
    return None


def _blocking_clarifications(tx: Tx, story_id: str, stage: str) -> list[dict[str, Any]]:
    out = []
    for s in review.items(tx, story_id, include_closed=True):
        if s["kind"] != "clarification" or stage not in s.get("blocks", []):
            continue
        if s["status"] == "answered" and s.get("decision_ref") and decision_valid(tx, s["decision_ref"])["valid"]:
            continue
        if s["status"] == "superseded":
            continue
        out.append(s)
    return out


def _clar_decisions(tx: Tx, story_id: str) -> list[str]:
    out = []
    for s in review.items(tx, story_id, include_closed=True):
        if s["kind"] == "clarification" and s["status"] == "answered" and s.get("decision_ref"):
            if decision_valid(tx, s["decision_ref"])["valid"]:
                out.append(s["decision_ref"])
    return out


def _req_analysis(e: Eval, tx: Tx, ctx: Ctx, story_id: str) -> None:
    if not _upstream(e, tx, story_id):
        return
    pending = _blocking_clarifications(tx, story_id, "req_analysis")
    if pending:
        e.need("业务问题尚未有效答复：" + "；".join(f"{p['handle']} {p['business_question']}" for p in pending),
               review=True)
        return
    e.decisions += _clar_decisions(tx, story_id)
    ta = current(tx, "test_analysis", story_id)
    if ta is None:
        e.need("缺少需求测试分析", blocked=False)
        return
    e.inputs.append(ta["id"])
    if not effective_current(tx, ta["id"]):
        e.need("测试分析依赖的上游已变化，需要重新分析", blocked=False)
        return
    seed = derived_of(tx, ta["id"]).get("handoff_seed") or {}
    e.extra["handoff_seed"] = seed
    if not seed.get("is_ready"):
        e.verdict = "fail"
        e.reasons.append("没有可进入正式交接的测试点")
        return
    _rule_verdict(e, tx, ctx, ta, "req_analysis", lambda p, r: "pass")


def _test_plan(e: Eval, tx: Tx, ctx: Ctx, story_id: str) -> None:
    if not _upstream(e, tx, story_id):
        return
    plan = current(tx, "test_plan", story_id)
    if plan is None:
        e.need("缺少测试计划", blocked=False)
        return
    e.inputs.append(plan["id"])
    if not effective_current(tx, plan["id"]):
        e.need("测试计划依赖的范围已变化，需要重新估算")
        return
    ev = derived_of(tx, plan["id"]).get("plan_evaluation") or {}
    e.extra["plan_evaluation"] = ev
    if ev.get("capacity_conflicts"):
        e.verdict = "fail"
        for c in ev["capacity_conflicts"]:
            e.reasons.append(f"{c['person']} 在 {c['date']} 超出可用容量 {c['over_hours']} 小时，不能批准")
        return
    ref = review.valid_decision_for(tx, story_id, "plan_approval", plan["id"])
    if ref is None:
        review.raise_item(tx, ctx, story_id, kind="plan_approval", key=f"plan-{plan['id']}",
                          question="测试计划的范围、估算区间与排期是否批准？（估算不是实测工时）",
                          basis=[{"source_ref": plan["id"], "quote": plan["payload"]["estimate_method"]}],
                          affected_scope=f"估算 {plan['payload']['estimate_range_hours']['low']}–"
                                         f"{plan['payload']['estimate_range_hours']['high']} 小时；"
                                         f"{len(plan['payload']['tasks'])} 项任务",
                          responsible_role=ctx.policy.get("plan_approver_role", "tester"),
                          options=[{"key": "A", "label": "批准该计划"}, {"key": "reject", "label": "不批准，需要调整"}],
                          subject_refs=[plan["id"]], raised_by="test_plan")
        e.need("测试计划等待责任人批准", review=True)
        return
    e.decisions.append(ref)
    e.verdict = "pass"


def _case_design(e: Eval, tx: Tx, ctx: Ctx, story_id: str) -> None:
    if not _upstream(e, tx, story_id):
        return
    d = current(tx, "case_design", story_id)
    if d is None:
        e.need("缺少用例设计", blocked=False)
        return
    e.inputs.append(d["id"])
    if not effective_current(tx, d["id"]):
        e.need("用例设计依赖的测试分析已变化，需要修订设计")
        return
    tr = derived_of(tx, d["id"]).get("traceability") or {}
    e.extra["traceability"] = tr
    if not tr.get("ok"):
        e.verdict = "fail"
        if tr.get("uncovered_tps"):
            e.reasons.append(f"测试点未被覆盖：{', '.join(tr['uncovered_tps'])}")
        for inv in tr.get("invalid_refs", []):
            e.reasons.append(f"用例 {inv['tc']} 引用了无效测试点 {inv['tp']}")
        return
    e.verdict = "pass"


def _case_qc(e: Eval, tx: Tx, ctx: Ctx, story_id: str) -> None:
    if not _upstream(e, tx, story_id):
        return
    if ctx.policy.get("case_qc_applicable") is False:
        e.waived = True
        e.verdict = "not_applicable"
        e.reasons.append("冻结策略规定本项目不适用用例质检")
        return
    if "case_qc_applicable" not in ctx.policy:
        e.need("用例质检适用性未在策略中冻结，不可判定")
        return
    d = current(tx, "case_design", story_id)
    q = current(tx, "case_quality_review", story_id)
    if q is None or d is None or q["payload"]["design_ref"] != d["id"]:
        e.need("缺少针对当前用例设计的质检", blocked=False)
        return
    e.inputs.append(q["id"])
    if not effective_current(tx, q["id"]):
        e.need("用例质检对应的设计已变化")
        return
    levels = q["payload"]["levels"]
    res = [levels[k]["result"] for k in ("L1", "L2", "L3")]
    e.extra["levels"] = {k: levels[k]["result"] for k in ("L1", "L2", "L3")}
    if "fail" in res:
        e.verdict = "fail"
        e.reasons.append("用例质检存在不通过的层级")
    elif "indeterminate" in res:
        e.need("用例质检存在不可判定的层级", blocked=False)
    else:
        e.verdict = "pass"
        e.reasons.append("用例质检通过不代替产品对关键预期的确认")


def _trusted_role(tx: Tx, ctx: Ctx, decision_ref: str) -> bool:
    row = tx.conn.execute("SELECT role, verification FROM decisions WHERE id=?", (decision_ref,)).fetchone()
    return bool(row) and review.identity_trusted(ctx, dict(row))


def expectation_ever_confirmed(tx: Tx, ctx: Ctx, story_id: str, case: dict[str, Any]) -> bool:
    """该用例相同的关键预期是否曾由可信角色确认（设计修订只改动其他用例时，原确认对未变的预期仍然成立）。"""
    for a in tx.aggs("review_item", story_id):
        s = a["state"]
        if s["kind"] != "expectation_confirmation" or s["status"] != "answered" or not s.get("decision_ref"):
            continue
        if not s["key"].endswith(f"-{case['id']}"):
            continue
        row = tx.conn.execute("SELECT * FROM decisions WHERE id=?", (s["decision_ref"],)).fetchone()
        if row is None or row["revoked_at"] or row["result"] == "reject" or not review.identity_trusted(ctx, dict(row)):
            continue
        old = tx.get_snapshot(s["subject_refs"][0]["ref"]) if s["subject_refs"] else None
        old_case = next((c for c in (old["payload"].get("cases") or []) if c["id"] == case["id"]), None) if old else None
        if old_case and old_case.get("expected_text") == case.get("expected_text"):
            return True
    return False


def expectations_status(tx: Tx, ctx: Ctx, story_id: str, raise_items: bool = True) -> dict[str, Any]:
    d = current(tx, "case_design", story_id)
    if d is None:
        return {"confirmed": False, "pending": [], "decisions": []}
    keys = dcases.key_expectations(d["payload"])
    pending, decisions = [], []
    for c in keys:
        key = f"expect-{d['id']}-{c['id']}"
        found = None
        for a in tx.aggs("review_item", story_id):
            s = a["state"]
            if s["key"] == key and s["status"] == "answered" and s.get("decision_ref") and \
                    decision_valid(tx, s["decision_ref"])["valid"] and _trusted_role(tx, ctx, s["decision_ref"]):
                found = s["decision_ref"]
        if found:
            decisions.append(found)
            continue
        pending.append(c["id"])
        if raise_items:
            review.raise_item(tx, ctx, story_id, kind="expectation_confirmation", key=key,
                              question=f"用例 {c['id']}「{c['title']}」的关键业务预期是否正确：{c['expected_text']}",
                              basis=[{"source_ref": d["id"], "quote": c["expected_text"]}],
                              affected_scope=f"用例 {c['id']} 能否进入实际执行",
                              responsible_role="product",
                              options=[{"key": "A", "label": "预期正确"}, {"key": "reject", "label": "预期需要修改"}],
                              subject_refs=[d["id"]], raised_by="case_design")
    return {"confirmed": not pending and bool(d), "pending": pending, "decisions": decisions}


def _env_ready(e: Eval, tx: Tx, ctx: Ctx, story_id: str, target_id: str = "lab-refund") -> None:
    if not _upstream(e, tx, story_id):
        return
    chk = tx.current_of("environment_check", story_id, target_id)
    if chk is None:
        e.need("尚未执行环境实测", blocked=False)
        return
    e.inputs.append(chk["id"])
    if not effective_current(tx, chk["id"]):
        e.need("环境已变更，旧核验结果不再覆盖当前环境", blocked=False)
        return
    origins = {}
    for c in chk["payload"]["checks"]:
        ev = tx.get_evidence(c.get("evidence_ref") or "")
        if ev and tx.evidence_valid(ev["id"]):
            origins[ev["id"]] = ev["origin"]
    res = dex.environment_ready(chk["payload"], origins)
    e.extra["environment"] = res
    if res["ready"]:
        e.verdict = "pass"
        return
    failed = [i["id"] for i in res["items"] if i["measured"] == "fail"]
    build_mismatch = res.get("build_measured", True) and not res["build_match"]
    if failed or build_mismatch:
        e.verdict = "fail"
        e.reasons.append("环境实测未通过：" + "、".join(failed or ["构建版本不符"]))
    else:
        e.need("准备方法已提供，但实测未就绪", blocked=False)


def _latest_on_build(atts: list[dict[str, Any]], build: str | None, purpose: tuple[str, ...] | None = None
                     ) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for a in sorted(atts, key=lambda x: (x["created_at"], x["attempt_no"])):
        if build and a["build"] != build:
            continue
        if purpose and a["purpose"] not in purpose:
            continue
        if not a["effective"]:
            continue
        out[a["tc_ref"]] = a
    return out


def _self_test(tx: Tx, ctx: Ctx, story_id: str) -> bool | str:
    if ctx.policy.get("self_test_required") is False:
        return "not_required"
    st = current(tx, "self_test_report", story_id)
    return bool(st and effective_current(tx, st["id"]))


def _test_entry(e: Eval, tx: Tx, ctx: Ctx, story_id: str) -> None:
    st = statuses(tx, story_id)
    deployment = current_deployment(tx, story_id)
    build = deployment["payload"]["build"] if deployment else None
    exp = expectations_status(tx, ctx, story_id)
    atts = all_attempts(tx, story_id)
    latest_smoke = _latest_on_build(atts, build, ("smoke",))
    smoke = {t: (latest_smoke[t]["outcome"] if t in latest_smoke else None)
             for t in ctx.policy.get("required_smoke") or []}
    d = current(tx, "case_design", story_id)
    design_ids = {c["id"] for c in d["payload"]["cases"]} if d else set()
    crit = drep.entry_criteria(
        plan_ok=st["test_plan"] == "completed", design_ok=st["case_design"] == "completed",
        qc_ok=st["case_qc"] in ("completed", "waived"), expectations_confirmed=exp["confirmed"],
        env_ready=st["env_ready"] == "completed", self_test_provided=_self_test(tx, ctx, story_id),
        smoke=smoke, scope_consistent=bool(design_ids) and set(smoke) <= design_ids)
    verdict = drep.evaluate_criteria(crit)
    e.decisions += exp["decisions"]
    e.inputs += [latest_smoke[t]["id"] for t in latest_smoke if t in smoke]
    if deployment:
        e.inputs.append(deployment["id"])
    payload = {"scope_ref": d["id"] if d else "", "policy_ref": ctx.rule_ref(), "criterion_results": crit,
               "missing": [c["label"] + ("：" + c["reason"] if c["reason"] else "") for c in crit
                           if c["result"] in ("fail", "indeterminate")],
               "decision_refs": exp["decisions"], "verdict": verdict, "build": build}
    snap = _put_report(tx, ctx, story_id, "entry_report", payload, e.inputs)
    e.inputs.append(snap)
    e.extra["entry_report"] = snap
    e.verdict = verdict
    if verdict != "pass":
        for m in payload["missing"]:
            e.reasons.append(m)
        e.blocked = True
        if exp["pending"]:
            e.review_required = True


def _put_report(tx: Tx, ctx: Ctx, story_id: str, kind: str, payload: dict[str, Any], inputs: list[str],
                required: bool = True) -> str:
    lineage = lineage_key(kind, story_id, None)
    cur = tx.lineage_current(lineage)
    if cur:
        old = tx.get_snapshot(cur)
        if old["payload"] == payload and effective_current(tx, cur):
            return cur
    deps = []
    for r in dict.fromkeys(inputs):
        try:
            deps.append(dep(tx, r, "input" if required else "history", required=required))
        except QAError:
            continue
    snap = tx.put_snapshot(kind, payload, story_id=story_id, project_id=ctx.project_id,
                           producer={"kind": "process", "name": f"{kind}_builder", "verification": "process"},
                           context={"rules_sha256": ctx.rules_sha, "config_sha256": ctx.config_sha},
                           depends_on=deps, lineage=lineage, parent_id=cur)
    return snap["id"]


def execution_view(tx: Tx, ctx: Ctx, story_id: str) -> dict[str, Any]:
    d = current(tx, "case_design", story_id)
    ta = current(tx, "test_analysis", story_id)
    deployment = current_deployment(tx, story_id)
    build = deployment["payload"]["build"] if deployment else None
    atts = all_attempts(tx, story_id)
    q = quarantined(tx, story_id)
    scope = [c["id"] for c in d["payload"]["cases"]] if d else []
    latest = _latest_on_build(atts, build)
    reg = None
    if d and ta:
        items = dex.regression_items(ta["payload"], d["payload"], list(ctx.policy.get("required_regression") or []))
        reg = dex.regression_gate(items, latest, q)
    return {"design": d, "analysis": ta, "build": build, "deployment": deployment, "attempts": atts, "scope": scope,
            "latest": latest, "quarantined": q, "regression": reg}


def _test_execution(e: Eval, tx: Tx, ctx: Ctx, story_id: str) -> None:
    if not _upstream(e, tx, story_id):
        return
    v = execution_view(tx, ctx, story_id)
    if not v["scope"]:
        e.need("没有冻结的执行范围")
        return
    rows = []
    for tc in v["scope"]:
        a = v["latest"].get(tc)
        st = "quarantined" if tc in v["quarantined"] else (a["outcome"] if a else "not_run")
        rows.append((tc, st))
        if a:
            e.inputs.append(a["id"])
    e.extra["rows"] = rows
    e.extra["regression"] = v["regression"]
    failed = [tc for tc, s in rows if s in ("fail", "error")]
    missing = [tc for tc, s in rows if s in ("not_run", "blocked", "cancelled", "quarantined")]
    if failed:
        e.verdict = "fail"
        e.reasons.append(f"当前构建上存在失败或执行错误的用例：{', '.join(failed)}")
    elif missing:
        e.need(f"必测用例尚无有效结果：{', '.join(missing)}", blocked=False)
    elif v["regression"] and not v["regression"]["ok"]:
        e.verdict = "fail"
        e.reasons.append("必回归未全部实际通过")
    else:
        e.verdict = "pass"


def defect_snapshot(tx: Tx, ctx: Ctx, story_id: str, has_failures: bool = False) -> dict[str, Any]:
    """缺陷状态快照。存在失败结果却从未取得缺陷状态时，空列表不代表“没有阻断缺陷”。"""
    from qa_flow.application.defects import all_defects

    ds = all_defects(tx, story_id)
    synced = tx.meta_get(f"defect_synced_at:{story_id}")
    max_age = float((ctx.config.get("timing") or {}).get("defect_sync_max_age_hours") or 24)
    external = [d for d in ds if d.get("external_id")]
    recent = bool(synced and clock.hours_between(synced, tx.now) <= max_age)
    fresh = recent or (not external and not has_failures)
    return {"defects": [{"id": d["id"], "status": d["status"], "severity": d["severity"], "tc": d["tc_ref"],
                         "external_id": d.get("external_id")} for d in ds],
            "synced_at": synced, "fresh": fresh}


def _evidence_problems(tx: Tx, attempts: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """已登记执行证据当前不可用（摘要冲突、缺失或销毁）的清单。被篡改的尝试不再有效，但问题必须明确指出。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in attempts:
        for r in a["evidence_refs"]:
            if r in seen:
                continue
            seen.add(r)
            why = tx.evidence_problem(r)
            if why:
                out.append((r, why))
    return out


def _audit_evidence(tx: Tx, problems: list[tuple[str, str]]) -> None:
    if getattr(tx, "read_only", False):
        return
    for ref, why in problems:
        done = tx.conn.execute("SELECT 1 FROM events WHERE object_id=? AND event_type='evidence_integrity_failed' "
                               "AND payload LIKE ?", (ref, f"%{why[:6]}%")).fetchone()
        if not done:
            tx.event(ref, "evidence_integrity_failed", {"reason": why, "code": "EVIDENCE_INVALID"})


def _test_exit(e: Eval, tx: Tx, ctx: Ctx, story_id: str) -> None:
    if not _upstream(e, tx, story_id):
        # 仍生成准出报告，便于展示失败原因；上游未完成时不能通过
        pass
    v = execution_view(tx, ctx, story_id)
    env = ctx.environment_of("lab-refund") if ctx.is_lab else "test"
    want_origin = WORKER_ORIGIN.get(env, env)
    latest = v["latest"]
    scope = v["scope"]
    required_all_pass: bool | None
    if not scope:
        required_all_pass = None
    elif any(tc in v["quarantined"] for tc in scope):
        required_all_pass = False
    elif any(latest.get(tc) is None for tc in scope):
        required_all_pass = None if not any(latest.get(tc) and latest[tc]["outcome"] != "pass" for tc in scope) else False
    else:
        required_all_pass = all(latest[tc]["outcome"] == "pass" for tc in scope)
    evidence_ok = all(all(tx.evidence_valid(r) for r in a["evidence_refs"]) and a["origin"] == want_origin
                      for a in latest.values())
    bad_evidence = _evidence_problems(tx, [a for a in v["attempts"] if a["tc_ref"] in scope])
    if bad_evidence:
        evidence_ok = False
        for ref, why in bad_evidence:
            e.reasons.append(f"结果证据 {ref} 不可用：{why}")
        _audit_evidence(tx, bad_evidence)
    context_ok = bool(v["build"]) and all(a["build"] == v["build"] and a["effective"] for a in latest.values())
    snap = defect_snapshot(tx, ctx, story_id,
                           has_failures=any(a["outcome"] != "pass" for a in latest.values()))
    blocking = set(ctx.policy.get("blocking_severities") or ["blocker", "major"])
    open_blocking = [d["id"] for d in snap["defects"] if d["severity"] in blocking and d["status"] not in TERMINAL]
    accepted = [d["id"] for d in snap["defects"] if d["status"] == "accepted_risk"]
    unknown = [a["id"] for a in broker.pending_unknown(tx, story_id)]
    rollback_ok: bool | None | str
    if ctx.policy.get("rollback_drill_required"):
        drills = tx.snapshots(story_id, "rollback_drill")
        rollback_ok = (drills[-1]["payload"]["outcome"] == "pass") if drills else None
    else:
        rollback_ok = "not_required"
    crit = drep.exit_criteria(required_all_pass=required_all_pass,
                              regression_ok=bool(v["regression"] and v["regression"]["ok"]),
                              evidence_ok=evidence_ok and bool(latest), context_ok=context_ok,
                              open_blocking_defects=open_blocking, defect_snapshot_fresh=snap["fresh"],
                              unknown_actions=unknown, rollback_ok=rollback_ok, accepted_risks=accepted)
    rule_verdict = drep.evaluate_criteria(crit)
    upstream_missing = workflow.upstream_satisfied("test_exit", statuses(tx, story_id))
    if upstream_missing and rule_verdict == "pass":
        rule_verdict = "indeterminate"
    report = test_report_payload(tx, ctx, story_id, v)
    report_ref = _put_report(tx, ctx, story_id, "test_report", report, [a["id"] for a in v["attempts"]],
                             required=False)
    e.inputs += [report_ref] + [a["id"] for a in latest.values()]
    ext = tx.get_agg(f"external_release:{story_id}")
    payload = {"scope_ref": v["design"]["id"] if v["design"] else "", "policy_ref": ctx.rule_ref(),
               "criterion_results": crit, "missing": [c["label"] + "：" + c["reason"] for c in crit
                                                      if c["result"] in ("fail", "indeterminate")],
               "defect_snapshot": {k: v for k, v in snap.items() if k != "synced_at"}, "decision_refs": [],
               "verdict": rule_verdict, "test_report_ref": report_ref,
               "build": v["build"],
               "external_release_decision": (ext["state"]["text"] if ext else None)}
    exit_ref = _put_report(tx, ctx, story_id, "exit_report", payload, e.inputs)
    e.inputs.append(exit_ref)
    e.extra.update({"exit_report": exit_ref, "test_report": report_ref, "rule_verdict": rule_verdict})
    if rule_verdict == "fail":
        e.verdict = "fail"
        e.reasons += payload["missing"]
        if ext:
            e.reasons.append("外部决定发布，QA 未通过")
        return
    if rule_verdict == "indeterminate":
        e.reasons += payload["missing"] or ["上游阶段未完成"]
        e.blocked = True
        return
    ref = review.valid_decision_for(tx, story_id, "exit_confirmation", exit_ref)
    if ref is None:
        review.raise_item(tx, ctx, story_id, kind="exit_confirmation", key=f"exit-{exit_ref}",
                          question="冻结标准计算结果为通过。是否确认测试准出？（准出不会触发部署）",
                          basis=[{"source_ref": exit_ref, "quote": "规则计算结果：pass"}],
                          affected_scope=f"构建 {v['build']} 的测试准出", responsible_role=ctx.policy.get(
                              "exit_approver_role", "tester"),
                          options=[{"key": "A", "label": "确认准出"}, {"key": "reject", "label": "暂不准出"}],
                          subject_refs=[exit_ref], raised_by="test_exit")
        e.need("准出规则计算通过，等待责任人确认", review=True)
        return
    e.decisions.append(ref)
    e.verdict = "pass"


def _bound_to_design(tx: Tx, atts: list[dict[str, Any]], design: dict[str, Any] | None) -> list[dict[str, Any]]:
    """只保留按当前设计（或与当前设计中该用例内容完全相同的旧设计）预期执行的尝试。

    用例预期修订后，按旧预期得到的结果不能算作新设计下的结果。
    """
    if design is None:
        return atts
    cur_cases = {c["id"]: c for c in design["payload"]["cases"]}
    cache: dict[str, dict[str, Any]] = {}
    out = []
    for a in atts:
        snap = tx.get_snapshot(a["id"])
        exp_ref = next((d["ref"] for d in snap["envelope"]["depends_on"] if d["relation"] == "expected"), None)
        if exp_ref is None or exp_ref == design["id"]:
            out.append(a)
            continue
        if exp_ref not in cache:
            old = tx.get_snapshot(exp_ref)
            cache[exp_ref] = {c["id"]: c for c in (old["payload"].get("cases") or [])} if old else {}
        if cache[exp_ref].get(a["tc_ref"]) == cur_cases.get(a["tc_ref"]):
            out.append(a)
    return out


def _blocked_cases(tx: Tx, story_id: str) -> dict[str, str]:
    """已发起执行但被阻断（动作失败、未产生尝试）的用例 → 动作 ID。与从未发起的用例分开计数。"""
    out: dict[str, str] = {}
    for a in broker.actions(tx, story_id, ("failed",)):
        if a["kind"] != "execute.cases":
            continue
        plan = tx.get_snapshot(a["plan_ref"])
        req = (plan["payload"].get("request") or {}) if plan else {}
        if req.get("purpose") == "smoke":
            continue
        for tc in req.get("cases") or []:
            out[tc] = a["id"]
    return out


def test_report_payload(tx: Tx, ctx: Ctx, story_id: str, v: dict[str, Any]) -> dict[str, Any]:
    from qa_flow.application.defects import all_defects

    scripts = [s["payload"] for s in tx.snapshots(story_id, "script_revision") if tx.is_current(s["id"])]
    bound = _bound_to_design(tx, v["attempts"], v["design"])
    cov = dcases.coverage(v["design"]["payload"], v["analysis"]["payload"], scripts, bound) \
        if v["design"] and v["analysis"] else {}
    rep = drep.test_report(v["scope"], bound, v["quarantined"], cov,
                           [{"id": d["id"]} for d in all_defects(tx, story_id)],
                           blocked=_blocked_cases(tx, story_id))
    return {"scope_ref": v["design"]["id"] if v["design"] else "", "counts": rep["latest"]["counts"],
            "first_run": rep["first_run"], "latest": rep["latest"], "coverage": cov, "unexecuted": rep["unexecuted"],
            "defect_refs": rep["defect_refs"], "build": v["build"], "attempt_total": rep["attempt_total"],
            "origin_note": "合成实验结果，不代表真实环境或生产" if ctx.is_lab else ""}


def _release_verify(e: Eval, tx: Tx, ctx: Ctx, story_id: str) -> None:
    rc = current(tx, "release_check", story_id)
    if rc is None:
        e.need("尚无实际生产部署与获准的生产只读证据；上线验证待定（不是失败或通过）", blocked=False)
        return
    e.inputs.append(rc["id"])
    d = derived_of(tx, rc["id"])
    e.verdict = d.get("release", {}).get("verdict", "indeterminate")
    e.reasons += [p["message"] for p in d.get("release", {}).get("problems", [])]


def _retrospective(e: Eval, tx: Tx, ctx: Ctx, story_id: str) -> None:
    if not _upstream(e, tx, story_id):
        return
    pending = []
    for k in tx.aggs("knowledge", story_id):
        s = k["state"]
        if s["status"] not in ("applied", "rejected"):
            pending.append(s["candidate_ref"])
        else:
            e.inputs.append(s["candidate_ref"])
    if pending:
        e.need(f"知识候选待确认或待应用：{', '.join(pending)}", review=True)
        return
    for esc in tx.snapshots(story_id, "escape"):
        e.inputs.append(esc["id"])
    e.verdict = "pass"


EVALUATORS = {
    "req_gate": _req_gate, "tech_gate": _tech_gate, "req_analysis": _req_analysis, "test_plan": _test_plan,
    "case_design": _case_design, "case_qc": _case_qc, "env_ready": _env_ready, "test_entry": _test_entry,
    "test_execution": _test_execution, "test_exit": _test_exit, "release_verify": _release_verify,
    "retrospective": _retrospective,
}


def evaluate(tx: Tx, ctx: Ctx, story_id: str, stage_id: str, expected_revision: int | None = None) -> dict[str, Any]:
    workflow.stage(stage_id)
    e = Eval(stage_id)
    EVALUATORS[stage_id](e, tx, ctx, story_id)
    if e.waived:
        status = "waived"
    else:
        status = workflow.status_for(e.verdict, blocked=e.blocked)
    oid = f"stage:{story_id}:{stage_id}"
    agg = tx.get_agg(oid)
    inputs = list(dict.fromkeys(e.inputs))
    decisions = list(dict.fromkeys(e.decisions))
    state = {"stage": stage_id, "title": workflow.stage(stage_id)["title"], "status": status, "verdict": e.verdict,
             "reasons": e.reasons, "inputs": inputs, "decisions": decisions, "rules_sha256": ctx.rules_sha,
             "config_sha256": ctx.config_sha, "review_required": e.review_required, "extra": _jsonable(e.extra)}
    if agg and expected_revision is None:
        old = {k: v for k, v in agg["state"].items() if k not in ("evaluation_ref", "evaluated_at")}
        if old == state:
            return {**agg["state"], "revision": agg["revision"], "unchanged": True}
    payload = {"stage": stage_id, "verdict": e.verdict, "status": status, "reasons": e.reasons, "inputs": inputs,
               "decisions": decisions, "rules_sha256": ctx.rules_sha, "config_sha256": ctx.config_sha}
    deps = []
    for r in inputs + decisions:
        try:
            deps.append(dep(tx, r, "input"))
        except QAError:
            continue
    snap = tx.put_snapshot("stage_evaluation", payload, story_id=story_id, project_id=ctx.project_id,
                           producer={"kind": "process", "name": "stage_evaluator", "verification": "process"},
                           context={"rules_sha256": ctx.rules_sha, "config_sha256": ctx.config_sha}, depends_on=deps)
    state["evaluation_ref"] = snap["id"]
    state["evaluated_at"] = tx.now
    rev = tx.put_agg(oid, "stage", story_id, state,
                     expected_revision=expected_revision if expected_revision is not None else (agg["revision"] if agg else 0),
                     event_type=f"stage_{status}", event_payload={"verdict": e.verdict})
    return {**state, "revision": rev}


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    return obj


def evaluate_all(tx: Tx, ctx: Ctx, story_id: str) -> dict[str, dict[str, Any]]:
    out = {}
    for sid in workflow.stage_ids():
        out[sid] = evaluate(tx, ctx, story_id, sid)
    return out
