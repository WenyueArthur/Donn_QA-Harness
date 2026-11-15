"""M6 独立验收：测试报告、准出、硬失败、生产来源校验、回滚方案与演练。

场景：AC-049～AC-052、AC-054～AC-056；AC-053、AC-112 需要生产只读连接器与预生产授权，记 blocked。

预期值来自规格：报告分母与分类按冻结范围和实际尝试由测试自己统计；
不调用产品函数计算预期。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from support import defects_helpers as dh
from support import flow
from support.cli import QA, blocked, sha256_file, write_json


def plus_hours(iso: str, hours: float) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    return (dt + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def at(qa, clock: str):
    return qa.test_mode(QA_FLOW_TEST_CLOCK=clock)


# ---------------------------------------------------------------------------
# AC-049 测试报告聚合真实结果
# ---------------------------------------------------------------------------

FOUR = ["C3", "C5", "C2", "C4"]  # 四条用例合起来覆盖全部可执行测试点


@pytest.fixture(scope="module")
def report49(tmp_path_factory):
    """冻结 4 条必测用例：C5 通过、C3 失败、C4 未执行、C2 因目标构建不符被阻断。只读复用。"""
    base = tmp_path_factory.mktemp("ac049")
    for name in ("工作区 根", "home", "cwd"):
        (base / name).mkdir()
    qa = QA(root=base / "工作区 根", home=base / "home", cwd=base / "cwd")
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_SCRIPT)
    story = dh.story_id(q)

    old = dh.snapshots(q, "case_design")[-1]
    payload = dict(old["payload"])
    payload["cases"] = [c for c in payload["cases"] if c["id"] in FOUR]
    assert sorted(c["id"] for c in payload["cases"]) == sorted(FOUR)
    rev = q.api_ok("artifact", "submit", "--story", story, "--kind", "case_design", "--file",
                   str(write_json(base / "design4.json", payload)), "--parent", old["id"])
    design_id = rev.result["artifact_id"]

    reg = q.api_ok("script", "register", "--story", story, "--file", str(dh.LAB_SCRIPT), "--path",
                   "lab/refund_cases.py", "--tc", "C3", "--tc", "C5", "--tc", "C2", "--tc", "C4",
                   "--repo", "qa-flow-lab", "--commit", "acceptance")
    q.api_ok("script", "review", "--story", story, "--script", reg.result["script_ref"], "--result", "approve",
             "--reason", "随包实验脚本")
    run = dh.run_cases(q, story, ["C3", "C5"])
    assert run.rc == 0 and run.json["state"] == "succeeded", run.describe()

    # C2：已发起执行，但目标实际构建与计划不符，执行被阻断
    vf = dh.version_file(q)
    original = vf.read_text(encoding="utf-8")
    vf.write_text(json.dumps({"build_id": "build-other", "build": "fixed", "commit": "x"}), encoding="utf-8")
    try:
        blocked_run = dh.run_cases(q, story, ["C2"])
    finally:
        vf.write_text(original, encoding="utf-8")
    assert (blocked_run.json or {}).get("state") == "failed" and "未执行" in blocked_run.json["error"], blocked_run.describe()

    fail_att = dh.latest_attempt(q, "C3", "build1")
    bug = dh.propose(q, story, fail_att["id"])
    assert bug.rc == 0, bug.describe()
    rep = dh.build_report(q, story)
    return {"qa": q, "story": story, "report": rep, "design": design_id, "bug": bug.result["defect_id"],
            "fail_attempt": fail_att, "blocked_run": blocked_run.json}


@pytest.mark.ac("AC-049")
@pytest.mark.layer("lab")
def test_ac049_denominator_and_pass_count(report49):
    q, tr = report49["qa"], report49["report"]["test_report"]
    assert tr["scope_ref"] == report49["design"]
    # 参考值：独立统计各用例在当前构建上的最新尝试
    mine = {tc: (dh.latest_attempt(q, tc, "build1") or {}).get("outcome") for tc in FOUR}
    assert mine == {"C3": "fail", "C5": "pass", "C2": None, "C4": None}
    assert tr["latest"]["denominator"] == 4 and tr["first_run"]["denominator"] == 4
    assert tr["counts"]["pass"] == 1 and tr["counts"]["fail"] == 1
    assert sum(tr["counts"].values()) == 4, tr["counts"]
    rows = {r["tc"]: r for r in tr["latest"]["rows"]}
    assert sorted(rows) == sorted(FOUR)
    assert rows["C5"]["status"] == "pass" and rows["C3"]["status"] == "fail"


@pytest.mark.ac("AC-049")
@pytest.mark.layer("lab")
def test_ac049_executed_and_unexecuted_not_mixed(report49):
    q, tr = report49["qa"], report49["report"]["test_report"]
    rows = {r["tc"]: r for r in tr["latest"]["rows"]}
    atts = {a["id"]: a for a in dh.attempts(q)}
    for tc in ("C3", "C5"):
        assert rows[tc]["attempt_ref"] in atts and atts[rows[tc]["attempt_ref"]]["tc_ref"] == tc
    for tc in ("C2", "C4"):
        assert rows[tc]["attempt_ref"] is None and rows[tc]["status"] not in ("pass", "fail")
    assert sorted(tr["unexecuted"]) == ["C2", "C4"]
    cov = tr["coverage"]["execution"]
    assert cov["numerator"] == 2 and cov["denominator"] == 4 and sorted(cov["not_executed"]) == ["C2", "C4"]
    ex = report49["report"]["exit_report"]
    assert ex["verdict"] == "fail" and dh.criteria(ex)["required_cases"]["result"] == "fail"


@pytest.mark.ac("AC-049")
@pytest.mark.layer("lab")
def test_ac049_blocked_is_counted_separately_from_not_run(report49):
    """C2 的执行被环境阻断，C4 从未发起：报告应分别计为 blocked 1、not_run 1。"""
    q, tr = report49["qa"], report49["report"]["test_report"]
    failed_exec = [a for a in dh.actions(q, "execute.cases") if a["state"] == "failed"]
    assert len(failed_exec) == 1 and "未执行" in failed_exec[0]["last_error"], "前置：C2 的执行动作被阻断"
    rows = {r["tc"]: r["status"] for r in tr["latest"]["rows"]}
    assert (tr["counts"]["blocked"], tr["counts"]["not_run"]) == (1, 1) and rows["C2"] == "blocked", (
        f"被阻断的 C2 与从未执行的 C4 没有区分：counts={tr['counts']} rows={rows}")


@pytest.mark.ac("AC-049")
@pytest.mark.layer("lab")
def test_ac049_report_cites_failure_and_defect_evidence(report49):
    q, tr = report49["qa"], report49["report"]["test_report"]
    rows = {r["tc"]: r for r in tr["latest"]["rows"]}
    fail_att = report49["fail_attempt"]
    assert rows["C3"]["attempt_ref"] == fail_att["id"]
    ev = dh.evidence_row(q, fail_att["observations_ref"])
    assert ev is not None and ev["state"] == "active" and ev["origin"] == "local_lab"
    assert sha256_file(q.workspace() / ev["rel_path"]) == ev["sha256"]
    assert tr["defect_refs"] == [report49["bug"]]
    d = dh.defect_by_tc(q, "C3")
    assert d["actual_ref"]["attempt_ref"] == fail_att["id"] and d["evidence_ref"] == fail_att["observations_ref"]
    # 报告快照登记了对失败尝试的依赖
    snap = dh.show(q, tr["id"])
    assert fail_att["id"] in [x["ref"] for x in snap["envelope"]["depends_on"]]


# ---------------------------------------------------------------------------
# AC-050 缺陷快照过期阻止肯定报告
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-050")
@pytest.mark.layer("offline")
def test_ac050_stale_defect_snapshot_makes_exit_indeterminate(qa):
    flow.drive(qa)
    story = dh.story_id(qa)
    v = flow.view(qa)
    assert flow.stage_map(v)["test_exit"]["status"] == "completed", "前置：演示流程已准出"
    synced = qa.query("SELECT value FROM meta WHERE key=?", (f"defect_synced_at:{story}",))[0]["value"]
    late = at(qa, plus_hours(synced, 25))  # 策略：缺陷状态有效期 24 小时

    rep = late.api_ok("report", "build", "--story", story).json
    ex = rep["exit_report"]
    c = dh.criteria(ex)["defects"]
    assert c["result"] == "indeterminate" and "刷新缺陷状态" in c["reason"], c
    assert ex["verdict"] == "indeterminate", ex["verdict"]
    assert ex["defect_snapshot"]["fresh"] is False and len(ex["defect_snapshot"]["defects"]) == 2
    # 执行结果本身仍是最新且通过，问题只在缺陷快照
    assert dh.criteria(ex)["required_cases"]["result"] == "pass"
    assert rep["stage"]["verdict"] != "pass" and rep["stage"]["status"] != "completed"
    after = late.ok("status").result
    assert after["verdict"] != "pass" and flow.stage_map(after)["test_exit"]["status"] != "completed"
    assert any("刷新缺陷状态" in r for r in flow.stage_map(after)["test_exit"]["reasons"])

    # 刷新缺陷状态后规则结果恢复
    late.api_ok("defect", "sync", "--story", story)
    ex2 = late.api_ok("report", "build", "--story", story).json["exit_report"]
    assert dh.criteria(ex2)["defects"]["result"] == "pass" and ex2["verdict"] == "pass"


@pytest.mark.ac("AC-050")
@pytest.mark.layer("offline")
def test_ac050_empty_defect_list_without_snapshot_is_not_no_defects(qa):
    """存在失败用例、本地缺陷列表为空且从未取得缺陷状态快照：不能据此判“无阻断缺陷”。"""
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_PROPOSE)
    story = dh.story_id(q)
    assert dh.defects(q) == []
    assert q.query("SELECT value FROM meta WHERE key=?", (f"defect_synced_at:{story}",)) == []
    late = at(q, plus_hours(dh.snapshots(q, "deployment")[-1]["created_at"], 25))
    ex = late.api_ok("report", "build", "--story", story).json["exit_report"]
    assert ex["defect_snapshot"]["defects"] == []
    c = dh.criteria(ex)["defects"]
    assert c["result"] != "pass", f"缺陷列表为空且从未同步，却判定“阻断缺陷已关闭或有有效风险接受”为 pass：{c}"


# ---------------------------------------------------------------------------
# AC-051 冻结标准和有效决定形成准出
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-051")
@pytest.mark.layer("lab")
def test_ac051_computed_pass_and_decision_kept_separately(qa):
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_SCRIPT)
    story = dh.story_id(q)
    dh.deploy(q, story, "build2")  # 修复构建：全部必测首轮通过

    def waiting_exit(v):
        return any(c["kind"] == "准出确认" for c in v["reviews"]["mine"])

    v = flow.drive(q, until=waiting_exit, start_demo=False)
    assert waiting_exit(v), [(s["stage"], s["status"], s["reasons"]) for s in v["stages"]]
    card = next(c for c in v["reviews"]["mine"] if c["kind"] == "准出确认")

    # 规则计算结果已是 pass，但没有决定时阶段不算完成
    ex = next(s for s in dh.snapshots(q, "exit_report") if s["id"] == card["basis"][0]["source_ref"])
    assert ex["id"] == dh.snapshots(q, "exit_report")[-1]["id"], "确认事项应绑定最新的准出报告"
    assert ex["payload"]["verdict"] == "pass"
    assert all(c["result"] in ("pass", "not_applicable") for c in ex["payload"]["criterion_results"])
    tr = dh.show(q, ex["payload"]["test_report_ref"])["envelope"]["payload"]
    assert tr["first_run"]["denominator"] == 7 and tr["first_run"]["counts"]["pass"] == 7, tr["first_run"]["counts"]
    assert dh.defects(q) == []
    st = flow.stage_map(v)["test_exit"]
    assert st["status"] != "completed" and st["verdict"] != "pass"
    assert q.query("SELECT id FROM decisions WHERE subjects LIKE ?", (f"%{ex['id']}%",)) == []

    version_before = sha256_file(dh.version_file(q))
    deploys_before = len(dh.actions(q, "deploy.lab_build"))
    actions_before = len(dh.actions(q))
    writes_before = len(dh.pf_writes(q))

    q.ok("review", "--answer", f"{card['handle']}=A")
    q.ok("resume")
    v2 = flow.view(q)
    st2 = flow.stage_map(v2)["test_exit"]
    assert st2["status"] == "completed" and st2["verdict"] == "pass"

    # 计算结果与决定分别保留
    dec = q.query("SELECT id, role, actor, verification, result, subjects FROM decisions WHERE subjects LIKE ?",
                  (f"%{ex['id']}%",))
    assert len(dec) == 1 and dec[0]["result"] == "approve" and dec[0]["role"] == "tester"
    bound = json.loads(dec[0]["subjects"])[0]
    assert bound == {"ref": ex["id"], "sha256": dh.show(q, ex["id"])["sha256"]}
    assert dh.snapshots(q, "exit_report")[-1]["id"] == ex["id"], "确认不应改写规则计算出的准出报告"
    assert dh.show(q, ex["id"])["envelope"]["payload"]["verdict"] == "pass"
    ev = dh.show(q, st2["evaluation_ref"])["envelope"]["payload"]
    assert ex["id"] in ev["inputs"] and dec[0]["id"] in ev["decisions"]
    assert dec[0]["id"] not in ev["inputs"]

    # 准出不触发任何发布或部署
    assert len(dh.actions(q, "deploy.lab_build")) == deploys_before
    new_kinds = {a["kind"] for a in dh.actions(q)[actions_before:]}
    assert not new_kinds & {"deploy.lab_build", "rollback.drill", "doc.archive", "notify.send"}, new_kinds
    assert sha256_file(dh.version_file(q)) == version_before
    assert len(dh.pf_writes(q)) == writes_before
    assert flow.stage_map(v2)["release_verify"]["status"] != "completed"


# ---------------------------------------------------------------------------
# AC-052 硬失败不能靠改阈值或本地签字通过
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-052")
@pytest.mark.layer("offline")
def test_ac052_hard_fail_survives_local_approval_and_external_release(qa, tmp_path):
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_FIX_DEPLOY)
    story = dh.story_id(q)
    q.api_ok("defect", "sync", "--story", story)
    rep = dh.build_report(q, story)
    ex = rep["exit_report"]
    assert ex["verdict"] == "fail" and dh.criteria(ex)["required_cases"]["result"] == "fail"
    ex_sha = dh.show(q, ex["id"])["sha256"]

    # 伪造准出报告：系统产物不能直接提交
    forged = dict({k: v for k, v in ex.items() if k != "id"}, verdict="pass", missing=[])
    r = q.api("artifact", "submit", "--story", story, "--kind", "exit_report", "--file",
              str(write_json(tmp_path / "forged.json", forged)))
    assert r.rc == 2 and r.error_code == "UNSUPPORTED_CONTRACT", r.describe()
    # 冒充外部核验的批准
    base = {"subject_refs": [{"ref": ex["id"], "sha256": ex_sha}], "actor": "agent", "scope": {"environment": "lab"},
            "reason": "本地批准放行"}
    r = q.api("decision", "record", "--story", story, "--file", str(write_json(
        tmp_path / "d-ext.json", dict(base, role="product", verification="external_verified", result="approve"))))
    assert r.rc == 3 and r.error_code == "AUTHORIZATION_INVALID", r.describe()
    # 导入本地批准与风险接受：即使被登记，也不能抹去硬失败
    for i, (role, result) in enumerate((("tester", "approve"), ("product", "accept_risk"))):
        q.api("decision", "record", "--story", story, "--file", str(write_json(
            tmp_path / f"d{i}.json", dict(base, role=role, verification="local_attested", result=result))))
    ev = q.api("stage", "evaluate", "--story", story, "--stage", "test_exit")
    assert ev.rc == 0 and ev.result["verdict"] == "fail" and ev.result["status"] == "failed", ev.describe()
    rep2 = dh.build_report(q, story)
    assert rep2["exit_report"]["verdict"] == "fail"
    assert dh.criteria(rep2["exit_report"])["required_cases"]["result"] == "fail"
    v = flow.view(q)
    assert v["verdict"] == "fail" and flow.stage_map(v)["test_exit"]["status"] == "failed"

    # 外部业务决定推进发布：仍记录 QA 未通过
    q.api_ok("release", "external-decision", "--story", story, "--text", "业务决定周五发布")
    rep3 = dh.build_report(q, story)
    assert rep3["exit_report"]["verdict"] == "fail"
    assert rep3["exit_report"]["external_release_decision"] == "业务决定周五发布"
    assert rep3["stage"]["verdict"] == "fail" and any("QA 未通过" in x for x in rep3["stage"]["reasons"])
    v = flow.view(q)
    assert v["verdict"] == "fail" and "已准出" not in q.text("status").out
    assert dh.actions(q, "deploy.lab_build")[-1]["state"] == "succeeded" and len(dh.actions(q, "deploy.lab_build")) == 1


@pytest.mark.ac("AC-052")
@pytest.mark.layer("offline")
def test_ac052_unauthorized_policy_edit_is_rejected(qa):
    """直接改工作区配置里的准出策略（把 major 移出阻断级别）：不能被当作有效策略用于判定。"""
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_FIX_DEPLOY)
    story = dh.story_id(q)
    q.api_ok("defect", "sync", "--story", story)
    before = dh.build_report(q, story)["exit_report"]
    assert dh.criteria(before)["defects"]["result"] == "fail", "前置：两条 major 缺陷未关闭"

    cfg_path = q.workspace() / "config" / "qa-flow.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["rules"]["policy"]["blocking_severities"] = ["blocker"]  # 把 major 从阻断级别中去掉
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    r = q.api("report", "build", "--story", story)
    if r.rc != 0:
        assert r.error_code in ("CONFIG_INVALID", "AUTHORIZATION_REQUIRED", "AUTHORIZATION_INVALID",
                                "PRECONDITION_FAILED", "RULES_UNAVAILABLE"), r.describe()
        return
    after = r.json["exit_report"]
    assert after["verdict"] == "fail", "硬失败被改策略消除"
    assert dh.criteria(after)["defects"]["result"] != "pass", (
        "未经授权改动策略后，“阻断缺陷”标准由 fail 变为 pass；"
        f"policy_ref 改动前 {before['policy_ref']}，改动后 {after['policy_ref']}")


# ---------------------------------------------------------------------------
# AC-054 模拟或测试证据不能满足生产验收
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-054")
@pytest.mark.layer("offline")
def test_ac054_lab_evidence_with_production_title_is_rejected(qa, tmp_path):
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_PROPOSE)
    story = dh.story_id(q)
    smoke = dh.latest_attempt(q, "SMOKE-1", "build1", "smoke")
    assert smoke["outcome"] == "pass" and smoke["origin"] == "local_lab"
    dep = dh.snapshots(q, "deployment")[-1]["payload"]

    # 一份标题写着 production 的冒烟通过文件，经来源导入登记为证据
    doc = tmp_path / "production-smoke-pass.md"
    doc.write_text("# production 冒烟通过\n\nenvironment: production\nresult: pass\n", encoding="utf-8")
    src = q.api_ok("source", "import", "--story", story, "--uri", str(doc))
    titled_ev = dh.show(q, src.result["source_ref"])["envelope"]["payload"]["content_ref"]
    assert dh.evidence_row(q, titled_ev)["origin"] != "production"

    def check(probe_ev: str, sample_ev: str) -> dict:
        return {"deployment": dep["deployment_id"], "actual_build": dep["build"], "environment": "production",
                "window": {"start": "2026-11-15T00:00:00Z", "end": "2026-11-15T01:00:00Z"},
                "probes": [{"id": "smoke", "result": "pass", "evidence_ref": probe_ev}],
                "metric_samples": [{"name": "error_rate", "ts": "2026-11-15T00:30:00Z", "value": 0.0, "unit": "ratio",
                                    "evidence_ref": sample_ev}],
                "thresholds": [{"name": "error_rate", "op": "<=", "value": 0.01, "unit": "ratio"}],
                "verdict": "pass"}

    seq0 = dh.last_seq(q)
    for name, probe_ev in (("lab", smoke["observations_ref"]), ("titled", titled_ev)):
        r = q.api("artifact", "submit", "--story", story, "--kind", "release_check", "--file",
                  str(write_json(tmp_path / f"rc-{name}.json", check(probe_ev, probe_ev))))
        assert r.rc == 4 and r.error_code == "ORIGIN_MISMATCH", r.describe()
        assert probe_ev in r.errors[0]["related_refs"]
        assert r.errors[0]["path"].startswith("payload.probes"), r.errors[0]
    assert dh.snapshots(q, "release_check") == []
    types = [e["event_type"] for e in dh.events_since(q, seq0)]
    assert "command_rejected" in types and not [t for t in types if t.startswith("stage_completed")]
    # 证据等级未被标题或手填环境提升
    assert dh.evidence_row(q, smoke["observations_ref"])["origin"] == "local_lab"
    assert dh.evidence_row(q, titled_ev)["origin"] != "production"
    v = q.ok("resume").result
    rv = flow.stage_map(v)["release_verify"]
    assert rv["status"] != "completed" and rv["verdict"] == "indeterminate"
    # 仅校验接口也不给出通过
    val = q.api("artifact", "validate", "--story", story, "--kind", "release_check", "--file",
                str(tmp_path / "rc-lab.json"))
    assert val.rc == 4 and val.error_code == "ORIGIN_MISMATCH", val.describe()


# ---------------------------------------------------------------------------
# AC-055 受控回滚演练核对服务和数据
# ---------------------------------------------------------------------------

FEASIBLE_PLAN = {
    "covers": {
        "code": {"plan": "把实验目标从 build2 切回 build1 的代码包", "reversible": True},
        "config": {"plan": "恢复 build1 对应的 version.json 配置", "reversible": True},
        "database": {"plan": "本次变更无表结构变化", "reversible": True},
        "data_restore": {"plan": "演练目录内的退款账务回到恢复点", "reversible": True},
        "verification": {"plan": "回滚后核对构建、账务并执行 SMOKE-1 健康检查", "reversible": True},
    },
    "irreversible_changes": [],
}


def _drill(qa, tmp_path):
    flow.drive(qa)
    story = dh.story_id(qa)
    assert json.loads(dh.version_file(qa).read_text(encoding="utf-8"))["build_id"] == "build2", "前置：lab 已部署 build2"
    plan = qa.api_ok("artifact", "submit", "--story", story, "--kind", "rollback_plan", "--file",
                     str(write_json(tmp_path / "rbp.json", FEASIBLE_PLAN)))
    assert plan.result["derived"]["feasibility"]["feasible"] is True
    target_before = sha256_file(dh.version_file(qa))
    main_db = qa.workspace() / "lab" / "target" / "refund.db"
    db_before = sha256_file(main_db)
    r = qa.api("release", "drill", "--story", story, "--plan", plan.result["artifact_id"], "--from-build", "build1",
               "--to-build", "build2", "--dispatch")
    return story, plan.result["artifact_id"], r, target_before, (main_db, db_before)


@pytest.mark.ac("AC-055")
@pytest.mark.layer("lab")
def test_ac055_lab_rollback_drill_reconciles_build_ledger_health(qa, tmp_path):
    story, plan_id, r, target_before, (main_db, db_before) = _drill(qa, tmp_path)
    assert r.rc == 0 and r.json["state"] == "succeeded", r.describe()
    act = dh.actions(qa, "rollback.drill")
    assert len(act) == 1 and act[0]["environment"] == "lab" and act[0]["target_id"] == "lab-refund"
    assert act[0]["authorization_ref"], "演练缺少授权引用"

    drills = dh.snapshots(qa, "rollback_drill")
    assert len(drills) == 1 and drills[0]["origin"] == "local_lab"
    p = drills[0]["payload"]
    assert p["plan_ref"] == plan_id and p["environment"] == "lab"
    assert (p["from_build"], p["to_build"]) == ("build1", "build2") and p["outcome"] == "pass"
    assert p["restore_point"] and p["authorization_ref"] == act[0]["authorization_ref"]
    checks = {c["name"]: c for c in p["before_after_checks"]}
    build = next(c for n, c in checks.items() if "build" in n)
    assert (build["before"], build["after"], build["result"]) == ("build2", "build1", "pass")
    ledger = next(c for n, c in checks.items() if "ledger" in n)
    assert ledger["before"] == ledger["after"] and ledger["result"] == "pass"
    health = next(c for n, c in checks.items() if "health" in n)
    assert health["result"] == "pass"

    # 独立核对演练目录：服务已回到 build1，账务与恢复点一致
    drill_dir = qa.workspace() / "lab" / "drill" / act[0]["id"]
    ver = json.loads((drill_dir / "version.json").read_text(encoding="utf-8"))
    assert ver["build_id"] == "build1" and ver["commit"] == dh.LAB_COMMITS["build1"]
    conn = sqlite3.connect(f"file:{drill_dir / 'refund.db'}?mode=ro", uri=True)
    try:
        refunded = conn.execute("SELECT refunded FROM orders WHERE id='O100'").fetchone()[0]
        records = conn.execute("SELECT COUNT(*) FROM refunds").fetchone()[0]
    finally:
        conn.close()
    assert {"O100_refunded": refunded, "records": records} == ledger["after"]

    # 演练在隔离目录进行：正式实验目标未被改动，更没有任何非 lab 环境的操作
    assert sha256_file(dh.version_file(qa)) == target_before and sha256_file(main_db) == db_before
    assert {a["environment"] for a in dh.actions(qa)} == {"lab"}
    deps = [x["ref"] for x in drills[0]["envelope"]["depends_on"]]
    assert plan_id in deps and any(x.startswith("EV-") for x in deps)


@pytest.mark.ac("AC-055")
@pytest.mark.layer("lab")
def test_ac055_drill_reconciles_configuration(qa, tmp_path):
    """规格要求核对 build1、账务和配置：演练记录中应有配置项的前后核对。"""
    story, plan_id, r, _, _ = _drill(qa, tmp_path)
    assert r.rc == 0 and r.json["state"] == "succeeded", r.describe()
    p = dh.snapshots(qa, "rollback_drill")[-1]["payload"]
    names = [c["name"] for c in p["before_after_checks"]]
    covered = [n for n in names if "config" in n.lower() or "配置" in n]
    assert covered or any("config" in str(u).lower() or "配置" in str(u) for u in p["unverified_parts"]), (
        f"演练只核对了 {names}，没有配置的前后核对，也没有把配置列为未验证部分（unverified_parts={p['unverified_parts']}）")


# ---------------------------------------------------------------------------
# AC-056 不可逆变更不能伪装回滚可行
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-056")
@pytest.mark.layer("offline")
def test_ac056_irreversible_change_cannot_claim_rollback_feasible(qa, tmp_path):
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_PROPOSE)
    story = dh.story_id(q)
    irreversible = "DDL 删除 refunds.payload 列的数据，且没有备份"
    plan = {
        "covers": {
            "code": {"plan": "回退到旧代码包", "reversible": True},
            "config": {"plan": "恢复旧配置", "reversible": True},
            "database": {"plan": "DROP COLUMN 无法撤销", "reversible": False},
            "data_restore": {"plan": "没有备份，也没有恢复方法；只有旧代码包", "reversible": False},
            "verification": {"plan": "回滚后执行冒烟", "reversible": True},
        },
        "irreversible_changes": [irreversible],
    }
    sub = q.api("artifact", "submit", "--story", story, "--kind", "rollback_plan", "--file",
                str(write_json(tmp_path / "rbp.json", plan)))
    plan_id = None
    if sub.rc == 0:
        feas = sub.result["derived"]["feasibility"]
        assert feas["feasible"] is False, feas
        assert irreversible in feas["irreversible"] and "database" in feas["irreversible"]
        assert "data_restore" in feas["irreversible"]
        plan_id = sub.result["artifact_id"]
    else:
        assert sub.rc == 4, sub.describe()

    target_before = sha256_file(dh.version_file(q))
    seq0, n_actions = dh.last_seq(q), len(dh.actions(q))
    if plan_id:
        r = q.api("release", "drill", "--story", story, "--plan", plan_id, "--from-build", "build1",
                  "--to-build", "build2", "--dispatch")
        assert r.rc == 4 and r.error_code == "VALIDATION_FAILED", r.describe()
        msg = r.errors[0]["message"]
        assert irreversible in msg and "data_restore" in msg, msg
        r2 = q.api("release", "drill", "--story", story, "--plan", plan_id, "--from-build", "build1",
                   "--to-build", "build2")
        assert r2.rc == 4, "不带 --dispatch 也不应登记演练计划\n" + r2.describe()
    # 没有演练动作、演练记录，也没有任何回滚操作
    assert len(dh.actions(q)) == n_actions and dh.actions(q, "rollback.drill") == []
    assert dh.snapshots(q, "rollback_drill") == []
    assert not (q.workspace() / "lab" / "drill").exists()
    assert sha256_file(dh.version_file(q)) == target_before
    assert not [e for e in dh.events_since(q, seq0) if e["event_type"].startswith("action_")]
    assert {a["environment"] for a in dh.actions(q)} == {"lab"}

    # 修正为可行方案后可以演练
    ok = q.api_ok("artifact", "submit", "--story", story, "--kind", "rollback_plan", "--file",
                  str(write_json(tmp_path / "rbp-ok.json", FEASIBLE_PLAN)), *(["--parent", plan_id] if plan_id else []))
    assert ok.result["derived"]["feasibility"]["feasible"] is True
    d = q.api_ok("release", "drill", "--story", story, "--plan", ok.result["artifact_id"], "--from-build", "build1",
                 "--to-build", "build2", "--dispatch")
    assert d.json["state"] == "succeeded", d.describe()


# ---------------------------------------------------------------------------
# AC-053 / AC-112 真实生产只读与预生产回滚（external）
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-053")
@pytest.mark.layer("external")
def test_ac053_production_verification_from_approved_connector():
    blocked("没有获准的生产只读连接器、实际部署 DEP1 与指标窗口；实验证据不能充当 origin=production")


@pytest.mark.ac("AC-112")
@pytest.mark.layer("external")
def test_ac112_production_readonly_and_preprod_rollback_separated():
    blocked("没有生产只读授权、部署基线与预生产回滚演练授权")
