"""环境核验、脚本登记、执行与尝试、稳定性、回归，以及实验部署与回滚演练。

执行身份来自受控 worker：执行记录和尝试只能由调度器在核对回执后生成，
不能通过 artifact submit 上传 pass。预期来自冻结用例，不读取实际响应生成预期。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from importlib import resources
from pathlib import Path
from typing import Any

from qa_flow.adapters import local_executor
from qa_flow.application import broker
from qa_flow.application.context import Ctx
from qa_flow.application.core import dep, effective_current
from qa_flow.canonical import sha256_bytes, sha256_obj
from qa_flow.domain import execution as dex
from qa_flow.errors import QAError
from qa_flow.ids import new_id
from qa_flow.storage.store import Tx, lineage_key

LAB_BUILDS = {
    "build1": {"build": "buggy_duplicate", "commit": "lab-commit-0001"},
    "build2": {"build": "fixed", "commit": "lab-fix-0002"},
    "build3": {"build": "buggy_neighbor", "commit": "lab-commit-0003"},
}
ORIGIN_BY_ENV = {"lab": "local_lab", "test": "test", "staging": "test", "production": "production"}


def _lab_resource(name: str) -> bytes:
    return resources.files("qa_flow.resources.lab.scripts").joinpath(name).read_bytes()


# ---------------------------------------------------------------------------
# 部署（实验）
# ---------------------------------------------------------------------------

def current_deployment(tx: Tx, story_id: str, target_id: str = "lab-refund") -> dict[str, Any] | None:
    return tx.current_of("deployment", story_id, target_id)


def _deploy_perform(ctx: Ctx, action: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    build_id = plan["request"]["build_id"]
    spec = LAB_BUILDS.get(build_id)
    if spec is None:
        from qa_flow.adapters.base import RemoteFailed

        raise RemoteFailed(f"未知实验构建 {build_id}")
    target = Path(plan["request"].get("target_dir") or (ctx.ws.lab_dir / "target"))
    target.mkdir(parents=True, exist_ok=True)
    (target / "refund_service.py").write_bytes(_lab_resource("refund_service.py"))
    (target / "version.json").write_text(json.dumps({"build_id": build_id, **spec}), encoding="utf-8")
    observed = json.loads((target / "version.json").read_text(encoding="utf-8"))
    return {"state": "succeeded", "remote_object_id": f"{plan['target_id']}:{build_id}:{new_id('DEP')}",
            "observed_build": observed["build_id"], "commit": observed["commit"]}


def _deploy_record(tx: Tx, ctx: Ctx, action: dict[str, Any], plan: dict[str, Any], outcome: dict[str, Any]) -> None:
    story = action["story_id"]
    lineage = lineage_key("deployment", story, plan["target_id"])
    payload = {"target_id": plan["target_id"], "build": outcome["observed_build"], "commit": outcome["commit"],
               "deployment_id": outcome["remote_object_id"], "deployed_at": tx.now,
               "origin": ORIGIN_BY_ENV[plan["environment"]]}
    tx.put_snapshot("deployment", payload, story_id=story, project_id=ctx.project_id,
                    producer={"kind": "worker", "name": "broker", "verification": "worker_receipt"},
                    lineage=lineage, parent_id=tx.lineage_current(lineage), origin=payload["origin"],
                    depends_on=[dep(tx, action["plan_ref"], "plan", required=False)])
    from qa_flow.application.core import propagate

    propagate(tx, ctx, story, reason=f"目标 {plan['target_id']} 已部署 {payload['build']}")


# ---------------------------------------------------------------------------
# 脚本登记与评审
# ---------------------------------------------------------------------------

def register_script(tx: Tx, ctx: Ctx, story_id: str, *, data: bytes, path: str, tc_refs: list[str], repo: str,
                    commit: str, dependencies: list[str], trusted_review: dict[str, Any] | None = None) -> dict[str, Any]:
    ev = tx.register_evidence(data, origin="imported", source=f"{repo}@{commit}:{path}", media_type="text/x-python")
    lineage = lineage_key("script_revision", story_id, path)
    cur = tx.lineage_current(lineage)
    if cur:
        snap = tx.get_snapshot(cur)
        if snap["payload"]["file_sha256"] == ev["sha256"] and snap["payload"]["commit"] == commit:
            return {"script_ref": cur, "unchanged": True}
    payload = {"tc_refs": tc_refs, "repo": repo, "commit": commit, "path": path, "file_sha256": ev["sha256"],
               "dependencies": dependencies, "content_ref": ev["id"], "trusted": False}
    snap = tx.put_snapshot("script_revision", payload, story_id=story_id, project_id=ctx.project_id,
                           producer={"kind": "operator", "name": ctx.actor, "verification": "local_os_user"},
                           depends_on=[dep(tx, ev["id"], "content")], lineage=lineage, parent_id=cur)
    if cur:
        from qa_flow.application.core import propagate

        propagate(tx, ctx, story_id, reason=f"脚本 {path} 内容已变化，需重新评审")
    review_ref = None
    if trusted_review:
        from qa_flow.application.review import record_decision

        review_ref = record_decision(tx, ctx, story_id, {
            "subject_refs": [{"ref": snap["id"], "sha256": snap["sha256"]}], "role": "reviewer",
            "actor": trusted_review["actor"], "verification": trusted_review["verification"],
            "scope": {"environment": "lab" if ctx.is_lab else "test", "action_kinds": ["execute.cases"],
                      "target_ids": []},
            "result": "approve", "reason": trusted_review["reason"]})["decision_ref"]
    return {"script_ref": snap["id"], "file_sha256": ev["sha256"], "review_ref": review_ref}


def review_script(tx: Tx, ctx: Ctx, story_id: str, script_ref: str, result: str, reason: str) -> dict[str, Any]:
    snap = tx.require_snapshot(script_ref, "script_revision")
    from qa_flow.application.review import record_decision

    d = record_decision(tx, ctx, story_id, {
        "subject_refs": [{"ref": snap["id"], "sha256": snap["sha256"]}], "role": "reviewer", "actor": ctx.actor,
        "verification": "local_attested",
        "scope": {"environment": "lab" if ctx.is_lab else "test", "action_kinds": ["execute.cases"], "target_ids": []},
        "result": "approve" if result == "approve" else "reject", "reason": reason})
    return {"script_ref": script_ref, "review_ref": d["decision_ref"],
            "note": "评审只覆盖该文件摘要；文件变化后需重新评审"}


def script_review_valid(tx: Tx, script_ref: str) -> str | None:
    snap = tx.get_snapshot(script_ref)
    if snap is None:
        return None
    rows = tx.conn.execute("SELECT id, subjects FROM decisions WHERE role='reviewer' AND result='approve' "
                           "AND revoked_at IS NULL ORDER BY created_at DESC").fetchall()
    from qa_flow.application.core import decision_valid

    for r in rows:
        subs = json.loads(r["subjects"])
        if any(s["ref"] == script_ref and s["sha256"] == snap["sha256"] for s in subs) and \
                decision_valid(tx, r["id"])["valid"]:
            return r["id"]
    return None


def ensure_lab_script(tx: Tx, ctx: Ctx, story_id: str, tc_refs: list[str]) -> str:
    data = _lab_resource("refund_cases.py")
    res = register_script(tx, ctx, story_id, data=data, path="lab/refund_cases.py", tc_refs=tc_refs,
                          repo="qa-flow-lab", commit=f"pkg-{sha256_bytes(data)[:12]}", dependencies=["python-stdlib"],
                          trusted_review={"actor": "package:qa-flow-lab", "verification": "simulated",
                                          "reason": "随包发布的合成实验受审脚本"})
    return res["script_ref"]


# ---------------------------------------------------------------------------
# 执行（broker 处理器）
# ---------------------------------------------------------------------------

def _materialize_script(ctx: Ctx, script_ref: str) -> tuple[bytes, dict[str, Any]]:
    with ctx.ws.reader() as rtx:
        snap = rtx.require_snapshot(script_ref, "script_revision")
        data = rtx.read_evidence(snap["payload"]["content_ref"])
        if sha256_bytes(data) != snap["payload"]["file_sha256"]:
            raise QAError("EVIDENCE_INVALID", "脚本字节与登记摘要不符")
        if not script_review_valid(rtx, script_ref):
            raise QAError("AUTHORIZATION_INVALID", "脚本未通过评审或评审已不覆盖当前内容；不运行未审脚本",
                          related_refs=[script_ref])
    return data, snap


def _target_dir(ctx: Ctx, plan: dict[str, Any]) -> Path:
    return Path(plan["request"].get("target_dir") or (ctx.ws.lab_dir / "target"))


def _observed_build(target: Path) -> str | None:
    try:
        return json.loads((target / "version.json").read_text(encoding="utf-8"))["build_id"]
    except (OSError, ValueError, KeyError):
        return None


def _run_script(ctx: Ctx, data: bytes, args: list[str]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="qa-exec-", dir=str(ctx.ws.tmp_dir)) as tmp:
        script = Path(tmp) / "script.py"
        script.write_bytes(data)
        return local_executor.run([sys.executable, "-I", "-B", str(script), *args], cwd=Path(tmp),
                                  timeout_s=float(ctx.budgets.get("exec_timeout_seconds", 60)),
                                  max_output_bytes=int(ctx.budgets.get("max_output_bytes", 262144)))


def _execute_perform(ctx: Ctx, action: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    req = plan["request"]
    data, _ = _materialize_script(ctx, req["script_ref"])
    target = _target_dir(ctx, plan)
    observed = _observed_build(target)
    if observed != req["expected_build"]:
        from qa_flow.adapters.base import RemoteFailed

        raise RemoteFailed(f"目标实际构建 {observed} 与计划 {req['expected_build']} 不符，未执行")
    results = []
    for tc in req["cases"]:
        res = _run_script(ctx, data, ["--target-dir", str(target), "--case", tc, "--dataset", req["dataset"]])
        results.append({"tc": tc, **res})
    return {"state": "succeeded", "results": results, "observed_build": observed}


def _execute_record(tx: Tx, ctx: Ctx, action: dict[str, Any], plan: dict[str, Any], outcome: dict[str, Any]) -> None:
    req = plan["request"]
    story = action["story_id"]
    origin = ORIGIN_BY_ENV[plan["environment"]]
    design = tx.require_snapshot(req["design_ref"], "case_design")
    expected = {c["id"]: c["expected"] for c in design["payload"]["cases"]}
    deployment = current_deployment(tx, story, plan["target_id"])
    script = tx.require_snapshot(req["script_ref"], "script_revision")
    ctx_sha = dex.context_sha(outcome["observed_build"], script["payload"]["file_sha256"], req["dataset"],
                              ctx.config_sha, str(req.get("seed", 0)), "system")
    attempt_refs = []
    receipt_rows = []
    for r in outcome["results"]:
        ev = tx.register_evidence(r["stdout"], origin=origin, source=f"worker:{action['id']}:{r['tc']}",
                                  media_type="application/json")
        err_ev = tx.register_evidence(r["stderr"], origin=origin, source=f"worker:{action['id']}:{r['tc']}:stderr",
                                      media_type="text/plain") if r["stderr"] else None
        obs: dict[str, Any] | None = None
        try:
            parsed = json.loads(r["stdout"].decode("utf-8"))
            obs = parsed.get("observation") if parsed.get("case") == r["tc"] else None
        except (ValueError, UnicodeDecodeError, AttributeError):
            obs = None
        assertions = dex.assert_observation(expected.get(r["tc"], {}), obs or {}) if obs is not None else []
        outcome_s = dex.attempt_outcome(assertions, exit_code=r["exit_code"], timed_out=r["timed_out"],
                                        truncated=r["truncated"], cancelled=False, observation_ok=obs is not None)
        prev = [a for a in tx.snapshots(story, "case_attempt") if a["payload"]["tc_ref"] == r["tc"]]
        receipt = {"worker": action.get("lease_owner"), "lease_epoch": action["lease_epoch"], "tc": r["tc"],
                   "argv_sha256": r["argv_sha256"], "exit_code": r["exit_code"], "timed_out": r["timed_out"],
                   "truncated": r["truncated"], "killed_process_group": r["killed_process_group"],
                   "process_group_alive": r["process_group_alive"], "stdout_ref": ev["id"],
                   "stdout_sha256": ev["sha256"], "script_sha256": script["payload"]["file_sha256"],
                   "started_at": r["started_at"], "finished_at": r["finished_at"], "isolation": r["isolation"],
                   "python": r["python"]}
        rid = new_id("WR")
        rsha = sha256_obj(receipt)
        tx.conn.execute("INSERT INTO worker_receipts(id, action_id, worker_id, lease_epoch, data, sha256, created_at) "
                        "VALUES(?,?,?,?,?,?,?)", (rid, action["id"], receipt["worker"] or "unknown",
                                                  action["lease_epoch"], json.dumps(receipt), rsha, tx.now))
        receipt_rows.append(rid)
        payload = {"tc_ref": r["tc"], "script_ref": req["script_ref"], "attempt_no": len(prev) + 1,
                   "context_sha256": ctx_sha, "assertions": assertions, "observations_ref": ev["id"],
                   "outcome": outcome_s, "evidence_refs": [ev["id"]] + ([err_ev["id"]] if err_ev else []),
                   "run_ref": action["id"], "build": outcome["observed_build"], "purpose": req["purpose"],
                   "worker_receipt": rid}
        deps = [dep(tx, ev["id"], "evidence"), dep(tx, req["design_ref"], "expected"),
                dep(tx, req["script_ref"], "script")]
        if deployment:
            deps.append(dep(tx, deployment["id"], "context"))
        snap = tx.put_snapshot("case_attempt", payload, story_id=story, project_id=ctx.project_id,
                               producer={"kind": "worker", "name": receipt["worker"] or "worker",
                                         "verification": "worker_receipt"},
                               context={"build": outcome["observed_build"], "dataset": req["dataset"],
                                        "context_sha256": ctx_sha, "target": plan["target_id"]},
                               depends_on=deps, origin=origin)
        attempt_refs.append(snap["id"])
    run_payload = {"plan_ref": action["plan_ref"], "worker_receipt": ",".join(receipt_rows), "origin": origin,
                   "target": plan["target_id"], "build": outcome["observed_build"], "dataset": req["dataset"],
                   "attempt_refs": attempt_refs, "started_at": outcome["results"][0]["started_at"] if outcome["results"] else tx.now,
                   "finished_at": tx.now, "purpose": req["purpose"]}
    tx.put_snapshot("execution_run", run_payload, story_id=story, project_id=ctx.project_id,
                    producer={"kind": "worker", "name": action.get("lease_owner") or "worker",
                              "verification": "worker_receipt"},
                    depends_on=[dep(tx, a, "attempt") for a in attempt_refs], origin=origin)


def _probe_perform(ctx: Ctx, action: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    req = plan["request"]
    data, _ = _materialize_script(ctx, req["script_ref"])
    target = _target_dir(ctx, plan)
    res = _run_script(ctx, data, ["--target-dir", str(target), "--probe", "--dataset", req["dataset"]])
    res["observed_build"] = _observed_build(target)
    return {"state": "succeeded", "result": res}


def _probe_record(tx: Tx, ctx: Ctx, action: dict[str, Any], plan: dict[str, Any], outcome: dict[str, Any]) -> None:
    req = plan["request"]
    story = action["story_id"]
    origin = ORIGIN_BY_ENV[plan["environment"]]
    r = outcome["result"]
    ev = tx.register_evidence(r["stdout"], origin=origin, source=f"worker:{action['id']}:probe",
                              media_type="application/json")
    try:
        probe = json.loads(r["stdout"].decode("utf-8"))["probe"]
    except (ValueError, KeyError, UnicodeDecodeError):
        probe = {}
    ok = r["exit_code"] == 0 and not r["timed_out"] and not r["truncated"]

    def chk(cid: str, kind: str, passed: bool) -> dict[str, Any]:
        return {"id": cid, "kind": kind, "method_provided": True, "observed": "pass" if (ok and passed) else "fail",
                "evidence_ref": ev["id"]}

    build_obs = (probe.get("version") or {}).get("observed")
    expected_build_name = LAB_BUILDS.get(req["expected_build"], {}).get("build", req["expected_build"])
    data = probe.get("data") or {}
    checks = [
        chk("ENV-VERSION", "version", build_obs == expected_build_name),
        chk("ENV-ACCOUNT", "account", set((probe.get("account") or {}).get("roles", [])) >= {"refund_operator", "read_only"}),
        chk("ENV-DATA", "data", data.get("dataset") == req["dataset"] and data.get("O100_refunded") == 0),
        chk("ENV-OBS", "observability", bool((probe.get("observability") or {}).get("independent_read"))),
        chk("ENV-CLEANUP", "cleanup", bool((probe.get("cleanup") or {}).get("restored"))),
    ]
    deployment = current_deployment(tx, story, plan["target_id"])
    payload = {"target_id": plan["target_id"], "build": {"expected": req["expected_build"],
                                                         "observed": outcome["result"].get("observed_build")},
               "dataset_refs": [req["dataset_ref"]] if req.get("dataset_ref") else [], "checks": checks,
               "observations": [ev["id"]], "cleanup_plan": "每个用例执行前后重置为数据集初态",
               "cleanup_result": "pass" if checks[-1]["observed"] == "pass" else "fail"}
    deps = [dep(tx, ev["id"], "evidence")]
    if deployment:
        deps.append(dep(tx, deployment["id"], "context"))
    if req.get("dataset_ref"):
        deps.append(dep(tx, req["dataset_ref"], "dataset"))
    lineage = lineage_key("environment_check", story, plan["target_id"])
    tx.put_snapshot("environment_check", payload, story_id=story, project_id=ctx.project_id,
                    producer={"kind": "worker", "name": action.get("lease_owner") or "worker",
                              "verification": "worker_receipt"},
                    depends_on=deps, origin=origin, lineage=lineage, parent_id=tx.lineage_current(lineage))


def _rollback_perform(ctx: Ctx, action: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """在隔离的演练目录执行：部署新构建 → 记账 → 回滚到旧构建 → 核对代码、配置与账务。"""
    req = plan["request"]
    data, _ = _materialize_script(ctx, req["script_ref"])
    drill = ctx.ws.lab_dir / "drill" / action["id"]
    if drill.exists():
        shutil.rmtree(drill)
    drill.mkdir(parents=True)
    fake_plan = {"target_id": plan["target_id"], "request": {"build_id": req["to_build"], "target_dir": str(drill)}}
    _deploy_perform(ctx, action, fake_plan)
    _run_script(ctx, data, ["--target-dir", str(drill), "--probe", "--dataset", "DS1"])
    import sqlite3

    def ledger() -> dict[str, Any]:
        c = sqlite3.connect(f"file:{drill / 'refund.db'}?mode=ro", uri=True)
        try:
            return {"O100_refunded": c.execute("SELECT refunded FROM orders WHERE id='O100'").fetchone()[0],
                    "records": c.execute("SELECT COUNT(*) FROM refunds").fetchone()[0]}
        finally:
            c.close()

    before = {"build": _observed_build(drill), "ledger": ledger()}
    fake_plan["request"]["build_id"] = req["from_build"]
    _deploy_perform(ctx, action, fake_plan)
    after = {"build": _observed_build(drill), "ledger": ledger()}
    health = _run_script(ctx, data, ["--target-dir", str(drill), "--case", "SMOKE-1", "--dataset", "DS1"])
    return {"state": "succeeded", "before": before, "after": after, "health_exit": health["exit_code"],
            "health_stdout": health["stdout"], "remote_object_id": f"drill:{action['id']}"}


def _rollback_record(tx: Tx, ctx: Ctx, action: dict[str, Any], plan: dict[str, Any], outcome: dict[str, Any]) -> None:
    req = plan["request"]
    origin = ORIGIN_BY_ENV[plan["environment"]]
    ev = tx.register_evidence(json.dumps({k: v for k, v in outcome.items() if k != "health_stdout"},
                                         ensure_ascii=False).encode(), origin=origin,
                              source=f"worker:{action['id']}:drill", media_type="application/json")
    health_ok = False
    try:
        health_ok = json.loads(outcome["health_stdout"].decode()).get("observation", {}).get("status") == 201
    except (ValueError, AttributeError):
        health_ok = False
    checks = [
        {"name": "code_build", "before": outcome["before"]["build"], "after": outcome["after"]["build"],
         "expected": req["from_build"], "result": "pass" if outcome["after"]["build"] == req["from_build"] else "fail"},
        {"name": "ledger", "before": outcome["before"]["ledger"], "after": outcome["after"]["ledger"],
         "expected": outcome["before"]["ledger"],
         "result": "pass" if outcome["after"]["ledger"] == outcome["before"]["ledger"] else "fail"},
        {"name": "health", "before": None, "after": health_ok, "expected": True, "result": "pass" if health_ok else "fail"},
    ]
    payload = {"plan_ref": req["plan_ref"], "environment": plan["environment"], "from_build": req["from_build"],
               "to_build": req["to_build"], "restore_point": f"drill:{action['id']}", "before_after_checks": checks,
               "authorization_ref": action.get("authorization_ref") or "", "outcome":
                   "pass" if all(c["result"] == "pass" for c in checks) else "fail",
               "unverified_parts": req.get("unverified_parts", [])}
    tx.put_snapshot("rollback_drill", payload, story_id=action["story_id"], project_id=ctx.project_id,
                    producer={"kind": "worker", "name": action.get("lease_owner") or "worker",
                              "verification": "worker_receipt"},
                    depends_on=[dep(tx, ev["id"], "evidence"), dep(tx, req["plan_ref"], "plan")], origin=origin)


def _query_none(ctx: Ctx, action: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    # 本地执行没有远端对象可查询：结果未知时只能重新规划新的尝试，并保留未知记录
    return {"found": False, "reason": "本地执行结果无法回查；请重新规划一次新的执行尝试"}


def _deploy_query(ctx: Ctx, action: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    observed = _observed_build(_target_dir(ctx, plan))
    if observed == plan["request"]["build_id"]:
        spec = LAB_BUILDS[observed]
        return {"found": True, "remote_object_id": f"{plan['target_id']}:{observed}:reconciled",
                "observed_build": observed, "commit": spec["commit"]}
    return {"found": False, "authoritative_absent": True}


broker.handler("execute.cases", perform=_execute_perform, record=_execute_record, query=_query_none)
broker.handler("environment.probe", perform=_probe_perform, record=_probe_record, query=_query_none)
broker.handler("deploy.lab_build", perform=_deploy_perform, record=_deploy_record, query=_deploy_query)
broker.handler("rollback.drill", perform=_rollback_perform, record=_rollback_record, query=_query_none)


# ---------------------------------------------------------------------------
# 查询与评估
# ---------------------------------------------------------------------------

def attempts(tx: Tx, story_id: str) -> list[dict[str, Any]]:
    out = []
    for s in tx.snapshots(story_id, "case_attempt"):
        p = dict(s["payload"])
        p["id"] = s["id"]
        p["created_at"] = s["created_at"]
        p["origin"] = s["origin"]
        p["effective"] = effective_current(tx, s["id"])
        out.append(p)
    return out


def assess_stability(tx: Tx, ctx: Ctx, story_id: str, tc: str) -> dict[str, Any]:
    rule = ctx.policy.get("stability_rule") or {}
    if "minimum_attempts" not in rule:
        raise QAError("RULES_UNAVAILABLE", "稳定性规则未冻结，不能判定")
    rows = [a for a in attempts(tx, story_id) if a["tc_ref"] == tc]
    res = dex.stability(rows, int(rule["minimum_attempts"]))
    payload = {"tc_ref": tc, "attempt_refs": [a["id"] for a in rows], "classification": res["classification"],
               "first_outcome": res["first_outcome"], "contexts": res["contexts"]}
    snap = tx.put_snapshot("stability_assessment", payload, story_id=story_id, project_id=ctx.project_id,
                           producer={"kind": "process", "name": "stability", "verification": "process"},
                           depends_on=[dep(tx, a["id"], "attempt") for a in rows],
                           context={"rules_sha256": ctx.rules_sha})
    return {"assessment_ref": snap["id"], **payload}


def quarantined(tx: Tx, story_id: str) -> set[str]:
    agg = tx.get_agg(f"quarantine:{story_id}")
    return set(agg["state"].get("tcs", [])) if agg else set()


def set_quarantine(tx: Tx, story_id: str, tc: str, on: bool, reason: str) -> dict[str, Any]:
    cur = quarantined(tx, story_id)
    cur = (cur | {tc}) if on else (cur - {tc})
    tx.put_agg(f"quarantine:{story_id}", "quarantine", story_id, {"tcs": sorted(cur), "reason": reason},
               event_type="quarantine_changed", event_payload={"tc": tc, "on": on, "reason": reason})
    return {"quarantined": sorted(cur), "note": "隔离用例仍属于范围，不从必回归分母删除"}


def regression_plan(tx: Tx, ctx: Ctx, story_id: str, analysis: dict[str, Any], design: dict[str, Any]) -> dict[str, Any]:
    required = list(ctx.policy.get("required_regression") or [])
    items = dex.regression_items(analysis["payload"], design["payload"], required)
    payload = {"items": items, "analysis_ref": analysis["id"], "design_ref": design["id"]}
    lineage = lineage_key("regression_plan", story_id, None)
    cur = tx.lineage_current(lineage)
    if cur and tx.get_snapshot(cur)["payload"] == payload:
        return {"regression_ref": cur, **payload}
    snap = tx.put_snapshot("regression_plan", payload, story_id=story_id, project_id=ctx.project_id,
                           producer={"kind": "process", "name": "regression_selector", "verification": "process"},
                           depends_on=[dep(tx, analysis["id"]), dep(tx, design["id"])], lineage=lineage, parent_id=cur)
    return {"regression_ref": snap["id"], **payload}
