"""机器接口（api 子命令）。所有入口复用同一套资源、授权、产物和证据校验，不能从 API 绕过。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from qa_flow.canonical import loads_strict
from qa_flow.errors import QAError

GROUPS = {
    "init": "私有工作区初始化（等同 admin setup --apply）",
    "doctor": "能力检查",
    "source": "来源与新鲜度：import / refresh",
    "artifact": "产物与关系校验：submit / validate / show",
    "clarification": "问题与答复：propose / answer",
    "decision": "决定与范围核验：record / verify / revoke",
    "plan": "排期、容量与依赖：submit / evaluate",
    "stage": "正式判定与状态：evaluate / status",
    "environment": "实际环境与数据核验：check",
    "script": "脚本与评审：register / review",
    "execution": "执行与尝试：plan / status / stability / quarantine",
    "defect": "缺陷与复测：propose / create / attach / sync / retest / close / disposition",
    "action": "操作与结果核对：list / dispatch / reconcile",
    "worker": "到期刷新、同步与提醒：tick --once",
    "report": "报告：build",
    "metric": "指标：compute",
    "knowledge": "候选与入库：propose / apply / search",
    "archive": "归档计划：plan",
    "backup": "备份与恢复：create / verify / restore",
    "coverage": "实现与验收对应：export",
    "learning": "漏测与改进：escape / improvement / report",
    "release": "上线与回滚：external-decision / drill",
    "usability": "新人验收报告：evaluate",
    "lab": "实验资源：deploy / fault",
    "task": "任务列表：list",
}


def register(api: argparse.ArgumentParser) -> None:
    from qa_flow.cli import Parser, _common

    sub = api.add_subparsers(dest="group", metavar="<接口组>", parser_class=Parser)

    def grp(name: str) -> argparse._SubParsersAction:
        g = sub.add_parser(name, help=GROUPS[name])
        _common(g)
        return g.add_subparsers(dest="op", metavar="<操作>", parser_class=Parser)

    def op(g, name: str, *opts: tuple[str, dict[str, Any]]):
        p = g.add_parser(name)
        _common(p)
        p.add_argument("--command-id")
        for flag, kw in opts:
            p.add_argument(flag, **kw)
        return p

    req = {"required": True}
    story = ("--story", {"required": True, "help": "需求 story_id 或业务名称"})
    p = sub.add_parser("init", help=GROUPS["init"])
    _common(p)
    p.add_argument("--config", required=True)
    p.add_argument("--name")
    p = sub.add_parser("doctor", help=GROUPS["doctor"])
    _common(p)
    g = grp("source")
    op(g, "import", story, ("--uri", req))
    op(g, "refresh", story)
    g = grp("artifact")
    op(g, "submit", story, ("--kind", req), ("--file", req), ("--name", {}), ("--parent", {}),
       ("--provenance-file", {}))
    op(g, "validate", story, ("--kind", req), ("--file", req))
    op(g, "show", ("--id", req))
    g = grp("clarification")
    op(g, "propose", story, ("--file", req), ("--stage", {"default": "api"}))
    op(g, "answer", story, ("--handle", req), ("--option", {}), ("--text", {}), ("--role", {}), ("--actor", {}),
       ("--expected-revision", {"type": int}))
    g = grp("decision")
    op(g, "record", story, ("--file", req))
    op(g, "verify", ("--id", req))
    op(g, "revoke", ("--id", req), ("--reason", req))
    g = grp("plan")
    op(g, "submit", story, ("--file", req), ("--parent", {}))
    op(g, "evaluate", story)
    g = grp("stage")
    op(g, "evaluate", story, ("--stage", req), ("--expected-revision", {"type": int}))
    op(g, "status", story)
    g = grp("environment")
    op(g, "check", story, ("--target", {"default": "lab-refund"}), ("--dataset", {"default": "DS1"}))
    g = grp("script")
    op(g, "register", story, ("--file", req), ("--tc", {"action": "append", "required": True}), ("--repo", req),
       ("--commit", req), ("--path", {}))
    op(g, "review", story, ("--script", req), ("--result", {"choices": ["approve", "reject"], "required": True}),
       ("--reason", req))
    g = grp("execution")
    op(g, "plan", story, ("--cases", req), ("--purpose", {"default": "execution"}), ("--target", {"default": "lab-refund"}),
       ("--script", {}), ("--dispatch", {"action": "store_true"}))
    op(g, "status", story)
    op(g, "stability", story, ("--tc", req))
    op(g, "quarantine", story, ("--tc", req), ("--off", {"action": "store_true"}), ("--reason", req))
    g = grp("defect")
    op(g, "propose", story, ("--attempt", req), ("--root-cause", {}))
    op(g, "create", story, ("--defect", req), ("--dispatch", {"action": "store_true"}))
    op(g, "attach", story, ("--defect", req), ("--dispatch", {"action": "store_true"}))
    op(g, "sync", story)
    op(g, "retest", story, ("--defect", req), ("--attempt", req))
    op(g, "close", story, ("--defect", req), ("--dispatch", {"action": "store_true"}))
    op(g, "disposition", story, ("--defect", req), ("--disposition", req), ("--reason", req))
    g = grp("action")
    op(g, "list", ("--story", {}), ("--state", {}))
    op(g, "dispatch", ("--id", req))
    op(g, "reconcile", ("--id", req))
    g = grp("worker")
    op(g, "tick", ("--once", {"action": "store_true", "required": True}), ("--no-dispatch", {"action": "store_true"}))
    g = grp("report")
    op(g, "build", story)
    g = grp("metric")
    op(g, "compute", ("--input", req), ("--story", {}))
    g = grp("knowledge")
    op(g, "propose", story, ("--file", req))
    op(g, "apply", story)
    op(g, "search", ("--query", req))
    g = grp("archive")
    op(g, "plan", story, ("--report", req), ("--space", req), ("--existing-doc", {}), ("--if-match", {}),
       ("--dispatch", {"action": "store_true"}))
    g = grp("backup")
    op(g, "create", ("--output", req))
    op(g, "verify", ("--path", req))
    op(g, "restore", ("--from", {"required": True, "dest": "src"}), ("--to", req))
    g = grp("coverage")
    op(g, "export", ("--verification", {}), ("--output", {}))
    g = grp("learning")
    op(g, "escape", story, ("--file", req))
    op(g, "improvement", story, ("--file", req))
    op(g, "report", story)
    g = grp("release")
    op(g, "external-decision", story, ("--text", req))
    op(g, "drill", story, ("--plan", req), ("--from-build", req), ("--to-build", req),
       ("--dispatch", {"action": "store_true"}))
    g = grp("usability")
    op(g, "evaluate", ("--input", req), ("--spec", {}))
    g = grp("lab")
    op(g, "deploy", story, ("--build", req), ("--dispatch", {"action": "store_true"}))
    op(g, "fault", ("--name", req), ("--times", {"type": int, "default": 1}))
    g = grp("task")
    op(g, "list")


def _read_json(path: str) -> Any:
    try:
        return loads_strict(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QAError("SCHEMA_INVALID", f"无法读取 JSON：{path}（{exc}）", path="file") from exc


def run(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    from qa_flow.application import (admin, archive, broker, defects, execution, learning, review, stages,
                                     task_service as ts, worker)
    from qa_flow.application.context import Ctx, execute
    from qa_flow.application.core import decision_valid, import_source, submit_artifact
    from qa_flow.contracts.registry import SYSTEM_ONLY
    from qa_flow.storage import backup as bk

    group, opn = args.group, getattr(args, "op", None)
    if group is None:
        return {"groups": GROUPS, "note": "机器接口不作为新人的学习前提；日常使用 start/resume/review/status/export"}
    if group == "init":
        return admin.setup(root, args.name, Path(args.config), True)
    if group == "coverage":
        from qa_flow.reporting.capabilities import report

        rep = report(Path(args.verification) if args.verification else None)
        if args.output:
            Path(args.output).write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        return rep
    if group == "usability":
        from importlib import resources as res

        from qa_flow.domain.usability import evaluate

        obs = _read_json(args.input)
        spec = _read_json(args.spec) if args.spec else None
        if spec is None:
            spec = json.loads(res.files("qa_flow.resources").joinpath("usability_spec.json").read_text("utf-8"))
        return evaluate(obs if isinstance(obs, list) else obs.get("observations", []), spec)
    if group == "metric":
        from qa_flow.domain.metrics import METRICS

        data = _read_json(args.input)
        name = data.get("metric_name")
        if name not in METRICS:
            raise QAError("UNSUPPORTED_CONTRACT", f"未知指标：{name}")
        try:
            return METRICS[name](data.get("data") or {})
        except ValueError as exc:
            raise QAError("VALIDATION_FAILED", str(exc), path="data") from exc
    if group == "backup" and opn == "verify":
        return bk.verify_backup(Path(args.path))
    if group == "backup" and opn == "restore":
        return bk.restore_backup(Path(args.src), Path(args.to))

    wss = ts.workspaces(root)
    if group == "task":
        return {"tasks": [ts._candidate(t) | {"task_id": t["task"]["task_id"], "story_id": t["task"]["story_id"]}
                          for t in ts.all_tasks(root)]}
    if group == "doctor":
        return admin.doctor(Ctx(wss[0]) if len(wss) == 1 else None)
    story_arg = getattr(args, "story", None)
    if story_arg:
        sel = None
        for t in ts.all_tasks(root):
            if story_arg in (t["task"]["story_id"], t["task"]["task_id"], t["task"]["display_title"]):
                sel = t
        if sel is None:
            raise QAError("SELECTION_REQUIRED", f"没有找到需求：{story_arg}", details={
                "candidates": [ts._candidate(t) for t in ts.all_tasks(root)]})
        ctx, story = sel["ctx"], sel["task"]["story_id"]
    else:
        if len(wss) != 1:
            raise QAError("SELECTION_REQUIRED", "需要用 --workspace 指定单个项目工作区")
        ctx, story = Ctx(wss[0]), None
    cid = getattr(args, "command_id", None)

    def run_cmd(name: str, payload: dict[str, Any], fn) -> dict[str, Any]:
        return execute(ctx, name, payload, fn, command_id=cid)

    if group == "backup" and opn == "create":
        return bk.create_backup(ctx.ws, Path(args.output))
    if group == "source":
        if opn == "import":
            return run_cmd("source.import", {"story": story, "uri": args.uri},
                           lambda tx: import_source(tx, ctx, story, args.uri))
        with ctx.ws.reader() as rtx:
            uris = sorted({s["payload"]["uri"] for s in rtx.snapshots(story, "source_revision")})
        return {"refreshed": [run_cmd("source.refresh", {"story": story, "uri": u},
                                      lambda tx, u=u: import_source(tx, ctx, story, u))["result"] for u in uris]}
    if group == "artifact":
        if opn == "show":
            with ctx.ws.reader() as rtx:
                s = rtx.require_snapshot(args.id)
                return {"id": s["id"], "kind": s["kind"], "sha256": s["sha256"], "envelope": s["envelope"],
                        "current": rtx.is_current(s["id"])}
        payload = _read_json(args.file)
        if args.kind in SYSTEM_ONLY:
            raise QAError("UNSUPPORTED_CONTRACT", f"{args.kind} 只能由系统根据真实执行或决定生成，不能直接提交",
                          path="kind")
        if opn == "validate":
            from qa_flow.application.core import validate_payload

            with ctx.ws.reader() as rtx:
                problems, deps, derived = validate_payload(rtx, ctx, args.kind, payload, story)
            return {"valid": not problems, "problems": problems, "derived": derived,
                    "note": "结构与关系校验通过不代表阶段完成或语义正确"}
        prov = _read_json(args.provenance_file) if args.provenance_file else {"imported": True, "model": {
            "provider": "unknown", "model": "unknown", "revision": "unknown"}}
        return run_cmd("artifact.submit", {"story": story, "kind": args.kind, "payload": payload, "name": args.name,
                                           "parent": args.parent},
                       lambda tx: submit_artifact(tx, ctx, args.kind, payload, story, name=args.name,
                                                  parent_id=args.parent, provenance=prov))
    if group == "plan":
        if opn == "submit":
            payload = _read_json(args.file)
            return run_cmd("plan.submit", {"story": story, "payload": payload, "parent": args.parent},
                           lambda tx: submit_artifact(tx, ctx, "test_plan", payload, story, parent_id=args.parent))
        return run_cmd("stage.evaluate", {"story": story, "stage": "test_plan"},
                       lambda tx: stages.evaluate(tx, ctx, story, "test_plan"))
    if group == "clarification":
        if opn == "propose":
            q = _read_json(args.file)
            return run_cmd("clarification.propose", {"story": story, "q": q, "stage": args.stage},
                           lambda tx: review.propose_clarification(tx, ctx, story, q, args.stage))
        if not args.actor and not args.role:
            raise QAError("INVALID_ARGUMENT", "答复必须写明确认人或角色", path="actor")
        return run_cmd("clarification.answer", {"story": story, "handle": args.handle, "option": args.option,
                                                "text": args.text, "role": args.role, "actor": args.actor},
                       lambda tx: review.answer(tx, ctx, story, args.handle, args.option, args.text, role=args.role,
                                                actor=args.actor, expected_revision=args.expected_revision))
    if group == "decision":
        if opn == "record":
            payload = _read_json(args.file)
            if payload.get("verification") == "external_verified":
                raise QAError("AUTHORIZATION_INVALID", "本地命令行不能登记外部核验的决定；需要原生审批适配器核验")
            return run_cmd("decision.record", {"story": story, "payload": payload},
                           lambda tx: review.record_decision(tx, ctx, story, payload))
        if opn == "verify":
            with ctx.ws.reader() as rtx:
                st = decision_valid(rtx, args.id)
                row = rtx.conn.execute("SELECT * FROM decisions WHERE id=?", (args.id,)).fetchone()
            return {"decision": dict(row) if row else None, **st,
                    "note": "local_attested 只代表本机操作者的记录，不能证明产品角色身份"}
        return run_cmd("decision.revoke", {"id": args.id, "reason": args.reason},
                       lambda tx: review.revoke(tx, ctx, args.id, args.reason))
    if group == "stage":
        if opn == "evaluate":
            if ((ctx.config.get("rules") or {}).get("kind") == "legacy_bridge" and
                    args.stage in ("req_gate", "tech_gate", "req_analysis", "case_design", "case_qc")):
                _legacy_validate(ctx, story, args.stage)
            return run_cmd("stage.evaluate", {"story": story, "stage": args.stage, "rev": args.expected_revision},
                           lambda tx: stages.evaluate(tx, ctx, story, args.stage, args.expected_revision))
        with ctx.ws.reader() as rtx:
            return {"statuses": stages.statuses(rtx, story),
                    "stages": [stages.stage_state(rtx, story, s) for s in stages.workflow.stage_ids()]}
    if group == "environment":
        with ctx.ws.reader() as rtx:
            script = rtx.current_of("script_revision", story, "lab/refund_cases.py")
            dep_snap = execution.current_deployment(rtx, story, args.target)
            dataset = rtx.current_of("dataset", story, args.dataset)
        if script is None or dep_snap is None:
            raise QAError("PRECONDITION_FAILED", "缺少已登记脚本或实际部署，不能实测环境")
        build_id = next((k for k, b in execution.LAB_BUILDS.items() if b["build"] == dep_snap["payload"]["build"]), "")
        a = run_cmd("environment.plan", {"story": story, "target": args.target}, lambda tx: {"action": broker.plan_action(
            tx, ctx, story, "environment.probe", args.target,
            {"script_ref": script["id"], "dataset": args.dataset, "expected_build": build_id,
             "dataset_ref": dataset["id"] if dataset else None},
            [script["id"], dep_snap["id"]], logical_key=f"probe:{dep_snap['id']}")})["result"]["action"]
        return broker.dispatch(ctx, a["id"])
    if group == "script":
        if opn == "register":
            data = Path(args.file).read_bytes()
            return run_cmd("script.register", {"story": story, "sha": __import__("hashlib").sha256(data).hexdigest(),
                                               "tc": args.tc, "repo": args.repo, "commit": args.commit},
                           lambda tx: execution.register_script(tx, ctx, story, data=data, path=args.path or Path(args.file).name,
                                                                tc_refs=args.tc, repo=args.repo, commit=args.commit,
                                                                dependencies=[]))
        return run_cmd("script.review", {"story": story, "script": args.script, "result": args.result},
                       lambda tx: execution.review_script(tx, ctx, story, args.script, args.result, args.reason))
    if group == "execution":
        if opn == "status":
            with ctx.ws.reader() as rtx:
                v = stages.execution_view(rtx, ctx, story)
            return {"build": v["build"], "scope": v["scope"], "latest": {k: {"outcome": a["outcome"], "attempt": a["id"]}
                                                                         for k, a in v["latest"].items()},
                    "attempts": [{"id": a["id"], "tc": a["tc_ref"], "outcome": a["outcome"], "build": a["build"],
                                  "purpose": a["purpose"], "attempt_no": a["attempt_no"]} for a in v["attempts"]],
                    "regression": v["regression"], "quarantined": sorted(v["quarantined"])}
        if opn == "stability":
            return run_cmd("execution.stability", {"story": story, "tc": args.tc},
                           lambda tx: execution.assess_stability(tx, ctx, story, args.tc))
        if opn == "quarantine":
            return run_cmd("execution.quarantine", {"story": story, "tc": args.tc, "off": args.off},
                           lambda tx: execution.set_quarantine(tx, story, args.tc, not args.off, args.reason))
        with ctx.ws.reader() as rtx:
            design = rtx.current_of("case_design", story, None)
            script = rtx.get_snapshot(args.script) if args.script else rtx.current_of("script_revision", story,
                                                                                      "lab/refund_cases.py")
            dep_snap = execution.current_deployment(rtx, story, args.target)
        if not (design and script and dep_snap):
            raise QAError("PRECONDITION_FAILED", "缺少用例设计、已登记脚本或实际部署")
        build_id = next((k for k, b in execution.LAB_BUILDS.items() if b["build"] == dep_snap["payload"]["build"]),
                        dep_snap["payload"]["build"])
        cases = [c.strip() for c in args.cases.split(",") if c.strip()]
        a = run_cmd("execution.plan", {"story": story, "cases": cases, "purpose": args.purpose},
                    lambda tx: {"action": broker.plan_action(
                        tx, ctx, story, "execute.cases", args.target,
                        {"cases": cases, "purpose": args.purpose, "script_ref": script["id"], "design_ref": design["id"],
                         "dataset": "DS1", "expected_build": build_id},
                        [design["id"], script["id"], dep_snap["id"]])})["result"]["action"]
        return broker.dispatch(ctx, a["id"]) if args.dispatch else {"action": a}
    if group == "defect":
        if opn == "propose":
            return run_cmd("defect.propose", {"story": story, "attempt": args.attempt},
                           lambda tx: defects.propose(tx, ctx, story, args.attempt, root_cause=args.root_cause))
        if opn == "sync":
            return defects.sync(ctx, story)
        if opn == "retest":
            return run_cmd("defect.retest", {"story": story, "d": args.defect, "a": args.attempt},
                           lambda tx: (defects.record_retest(tx, story, args.defect, args.attempt) or {"linked": args.attempt}))
        if opn == "disposition":
            return run_cmd("defect.disposition", {"story": story, "d": args.defect, "disp": args.disposition},
                           lambda tx: defects.request_disposition(tx, ctx, story, args.defect, args.disposition, args.reason))
        if opn == "close":
            with ctx.ws.reader() as rtx:
                chk = defects.close_check(rtx, ctx, story, args.defect, execution.attempts(rtx, story))
            if not chk["can_close"]:
                raise QAError("PRECONDITION_FAILED", "缺陷不满足关闭条件：" + "；".join(chk["missing"]),
                              details=chk)
            a = run_cmd("defect.close.plan", {"story": story, "d": args.defect}, lambda tx: {"action": defects.plan_transition(
                tx, ctx, story, args.defect, "CLOSED", "复测与相邻回归通过")})["result"]["action"]
            return broker.dispatch(ctx, a["id"]) if args.dispatch else {"action": a}
        fn = defects.plan_create if opn == "create" else defects.plan_attach
        a = run_cmd(f"defect.{opn}.plan", {"story": story, "d": args.defect},
                    lambda tx: {"action": fn(tx, ctx, story, args.defect)})["result"]["action"]
        return broker.dispatch(ctx, a["id"]) if args.dispatch else {"action": a}
    if group == "action":
        if opn == "list":
            with ctx.ws.reader() as rtx:
                states = tuple(args.state.split(",")) if args.state else None
                return {"actions": [{k: a[k] for k in ("id", "kind", "target_id", "environment", "state", "attempts",
                                                       "lease_epoch", "authorization_ref", "remote_object_id", "last_error")}
                                    for a in broker.actions(rtx, story, states)]}
        if opn == "dispatch":
            return broker.dispatch(ctx, args.id)
        return broker.reconcile(ctx, args.id)
    if group == "worker":
        return worker.tick(ctx, dispatch_ready=not args.no_dispatch)
    if group == "report":
        res = run_cmd("report.build", {"story": story, "n": __import__("qa_flow.ids").ids.new_id("N")},
                      lambda tx: {"test_exit": stages.evaluate(tx, ctx, story, "test_exit")})
        with ctx.ws.reader() as rtx:
            tr = rtx.current_of("test_report", story, None)
            er = rtx.current_of("exit_report", story, None)
        return {"test_report": tr and {"id": tr["id"], **tr["payload"]}, "exit_report": er and {"id": er["id"], **er["payload"]},
                "stage": res["result"]["test_exit"]}
    if group == "knowledge":
        if opn == "search":
            with ctx.ws.reader() as rtx:
                return learning.search(rtx, ctx, args.query)
        if opn == "apply":
            return run_cmd("knowledge.apply", {"story": story}, lambda tx: {"applied": learning.apply_confirmed(tx, ctx, story)})
        payload = _read_json(args.file)
        return run_cmd("knowledge.propose", {"story": story, "payload": payload},
                       lambda tx: learning.propose_knowledge(tx, ctx, story, payload))
    if group == "archive":
        a = run_cmd("archive.plan", {"story": story, "report": args.report, "space": args.space},
                    lambda tx: {"action": archive.plan_archive(tx, ctx, story, args.report, args.space,
                                                               args.existing_doc, args.if_match)})["result"]["action"]
        return broker.dispatch(ctx, a["id"]) if args.dispatch else {"action": a,
                                                                     "note": "归档计划已登记；需要授权后才能执行"}
    if group == "learning":
        if opn == "report":
            with ctx.ws.reader() as rtx:
                return learning.escape_report(rtx, ctx, story)
        payload = _read_json(args.file)
        fn = learning.record_escape if opn == "escape" else learning.record_improvement
        return run_cmd(f"learning.{opn}", {"story": story, "payload": payload}, lambda tx: fn(tx, ctx, story, payload))
    if group == "release" and opn == "drill":
        with ctx.ws.reader() as rtx:
            plan_snap = rtx.require_snapshot(args.plan, "rollback_plan")
            from qa_flow.application.core import derived_of

            feas = derived_of(rtx, plan_snap["id"]).get("feasibility") or {}
            script = rtx.current_of("script_revision", story, "lab/refund_cases.py")
        if not feas.get("feasible"):
            raise QAError("VALIDATION_FAILED", "回滚方案不可行，不能请求演练通过：" + "；".join(
                feas.get("missing_parts", []) + feas.get("irreversible", [])), details=feas)
        if script is None:
            raise QAError("PRECONDITION_FAILED", "缺少已登记的实验脚本")
        a = run_cmd("release.drill.plan", {"story": story, "plan": args.plan},
                    lambda tx: {"action": broker.plan_action(
                        tx, ctx, story, "rollback.drill", "lab-refund",
                        {"plan_ref": args.plan, "from_build": args.from_build, "to_build": args.to_build,
                         "script_ref": script["id"]}, [args.plan, script["id"]])})["result"]["action"]
        return broker.dispatch(ctx, a["id"]) if args.dispatch else {"action": a}
    if group == "release":
        def ext(tx):
            tx.put_agg(f"external_release:{story}", "external_release", story,
                       {"text": args.text, "recorded_by": ctx.actor, "at": tx.now}, event_type="external_release_decision")
            return {"recorded": True, "note": "外部业务决定发布时仍显示“外部决定发布，QA 未通过”，不改写 QA 结论"}
        return run_cmd("release.external", {"story": story, "text": args.text}, ext)
    if group == "lab":
        if opn == "fault":
            ctx.platform().set_fault(args.name, args.times)
            return {"fault": args.name, "times": args.times}
        a = run_cmd("lab.deploy.plan", {"story": story, "build": args.build},
                    lambda tx: {"action": broker.plan_action(tx, ctx, story, "deploy.lab_build", "lab-refund",
                                                             {"build_id": args.build}, [], logical_key=f"deploy:{args.build}:api")}
                    )["result"]["action"]
        return broker.dispatch(ctx, a["id"]) if args.dispatch else {"action": a}
    raise QAError("INVALID_ARGUMENT", f"未知接口：{group} {opn or ''}")


def _legacy_validate(ctx, story: str, stage: str) -> None:
    """在事务外调用冻结旧校验器，并把结果作为绑定产物摘要的验证报告登记。"""
    from qa_flow.adapters.legacy_rules import run_validator
    from qa_flow.application.context import execute
    from qa_flow.application.core import current, dep

    kinds = {"req_gate": "quality_review", "tech_gate": "technical_review", "req_analysis": "test_analysis",
             "case_design": "case_design", "case_qc": "case_quality_review"}
    with ctx.ws.reader() as rtx:
        snap = current(rtx, kinds[stage], story)
    if snap is None or not snap["payload"].get("legacy_report"):
        return
    rules = ctx.config["rules"]
    mp = (ctx.ws.config_dir / rules["manifest"]).resolve()
    try:
        res = run_validator(mp, rules.get("manifest_sha256"), stage, snap["payload"]["legacy_report"],
                            float(ctx.budgets.get("exec_timeout_seconds", 60)),
                            int(ctx.budgets.get("max_output_bytes", 262144)))
    except QAError as exc:
        res = {"verdict": "indeterminate", "error": exc.message, "validator": None}

    def h(tx):
        payload = {"subject_ref": snap["id"], "validator": res.get("validator") or "unavailable",
                   "structure": {"verdict": res["verdict"], "exit_code": res.get("exit_code"),
                                 "output": res.get("output"), "error": res.get("error")},
                   "relations": {"note": "跨对象关系由新系统确定性校验器检查"},
                   "semantic": {"status": "not_reviewed"}, "runtime": {"status": "not_applicable"}}
        s = tx.put_snapshot("validation_report", payload, story_id=story, project_id=ctx.project_id,
                            producer={"kind": "adapter", "name": "legacy_rules", "verification": "frozen_manifest"},
                            depends_on=[dep(tx, snap["id"], "subject")], context={"rules_sha256": ctx.rules_sha})
        return {"validation_report": s["id"]}

    execute(ctx, "legacy.validate", {"subject": snap["id"], "verdict": res["verdict"]}, h)
