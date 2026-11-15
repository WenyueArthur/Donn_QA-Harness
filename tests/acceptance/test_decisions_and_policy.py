"""独立验收：决定与授权绑定、身份核验、执行回执、原文保护、目标与凭证、配置与错误提示。

场景：AC-061、062、087、088、091、092（external，记 blocked）、093、094、128、129、137。
只通过子进程调用公共 CLI；期望值来自规格与本文件内的简短参考实现。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from support import evidence_helpers as H
from support import flow
from support.cli import blocked, sha256_file, write_json

ac = pytest.mark.ac
layer = pytest.mark.layer

T0 = "2026-11-16T01:00:00Z"       # lab-fixtures.json frozen_time
EXPIRES = "2026-11-16T03:00:00Z"
LATE = "2026-11-16T04:00:00Z"
REFUSED = ("AUTHORIZATION_REQUIRED", "AUTHORIZATION_INVALID")


def _archive_lab(qa, spaces=("lab-qa-archive", "lab-qa-archive-b")):
    """实验工作区 + 一个需求；归档动作不在既有权限内，必须逐个计划授权。"""
    def mutate(cfg):
        cfg["workspace"]["archive_spaces"] = list(spaces)

    ws = H.init_lab(qa, mutate=mutate)
    s1 = H.write_source(qa)
    st = H.start_story(qa, s1)
    story = st["story"]
    trp = H.snapshots(qa, "test_report", story)[-1]["id"]
    ext = H.snapshots(qa, "exit_report", story)[-1]["id"]
    return ws, story, trp, ext


def _plan(qa, story, report, space="lab-qa-archive") -> dict:
    r = H.archive_plan(qa, story, report, space)
    assert r.rc == 0, r.describe()
    a = r.result["action"]
    assert a["state"] == "awaiting_authorization" and a["authorization_ref"] is None
    return a


def _action(qa, aid) -> dict:
    return qa.query("SELECT * FROM actions WHERE id=?", (aid,))[0]


# ---------------------------------------------------------------------------
# AC-061 决定绑定对象而非一句确认
# ---------------------------------------------------------------------------

@ac("AC-061")
@layer("offline")
def test_ac061_决定只对所绑定的计划与动作生效(qa):
    ws, story, trp, ext = _archive_lab(qa)
    a1, a2 = _plan(qa, story, trp), _plan(qa, story, ext)
    assert a1["plan_sha256"] != a2["plan_sha256"]
    # 计划摘要可由登记的计划内容独立重算
    assert a1["plan_sha256"] == H.sha_obj(H.envelope(qa, a1["plan_ref"])["payload"])

    items = {i["key"]: i for i in H.review_items(qa, story)}
    q1, q2 = items[f"auth-{a1['id']}"], items[f"auth-{a2['id']}"]
    assert q1["handle"] != q2["handle"], "两个动作应分别确认"
    # 一句“全部同意”不能成为决定
    g = qa.run("review", "--answer", f"{q1['handle']}=全部同意")
    assert g.rc == 2 and g.error_code == "INVALID_ARGUMENT", g.describe()
    assert H.decisions(qa) == []

    r = qa.run("review", "--answer", f"{q1['handle']}=A")
    assert r.rc == 0, r.describe()
    rows = H.decisions(qa)
    assert len(rows) == 1
    d = rows[0]
    plan_sha = qa.query("SELECT sha256 FROM snapshots WHERE id=?", (a1["plan_ref"],))[0]["sha256"]
    assert d["plan_sha256"] == a1["plan_sha256"]
    assert json.loads(d["subjects"]) == [{"ref": a1["plan_ref"], "sha256": plan_sha}]
    scope = json.loads(d["scope"])
    assert (scope["environment"], scope["target_ids"], scope["action_kinds"]) == ("lab", ["lab-platform"], ["doc.archive"])
    # 审核人、提出者、确认方式可追溯
    env = H.envelope(qa, d["id"])
    assert d["role"] == "tester" and d["actor"].startswith("os:") and d["result"] == "approve"
    assert d["verification"] == "local_attested"
    assert env["payload"]["proposer"] == "broker" and env["producer"]["name"] == d["actor"]
    v = qa.api_ok("decision", "verify", "--id", d["id"]).json
    assert v["valid"] is True and v["reasons"] == []
    assert v["decision"]["role"] == "tester" and v["decision"]["plan_sha256"] == a1["plan_sha256"]
    assert "expires_at" in v["decision"]

    # 只对 D1/A1 生效：A2 仍待授权，dispatch 被拒且无外部调用
    r2 = qa.api("action", "dispatch", "--id", a2["id"])
    assert r2.rc == 3 and r2.error_code in REFUSED, r2.describe()
    assert H.platform_writes(ws) == 0
    assert _action(qa, a2["id"])["state"] == "awaiting_authorization"
    assert _action(qa, a2["id"])["authorization_ref"] is None

    ok = qa.api("action", "dispatch", "--id", a1["id"])
    assert ok.rc == 0 and ok.json["state"] == "succeeded", ok.describe()
    assert _action(qa, a1["id"])["authorization_ref"] == d["id"]
    docs = H.platform_rows(ws, "docs")
    assert len(docs) == 1 and trp in docs[0]["title"] and H.platform_writes(ws) == 1
    # 同一动作重复 dispatch 不产生第二次写
    again = qa.api("action", "dispatch", "--id", a1["id"])
    assert again.rc == 0 and H.platform_writes(ws) == 1
    assert _action(qa, a2["id"])["state"] == "awaiting_authorization"


@ac("AC-061")
@layer("offline")
def test_ac061_摘要或角色不符的决定不能授权(qa):
    ws, story, trp, ext = _archive_lab(qa)
    a1, a2 = _plan(qa, story, trp), _plan(qa, story, ext)
    # 摘要不符：绑定对象摘要与登记对象不同
    bad = H.plan_decision(qa, story, a1["id"])
    bad["subject_refs"][0]["sha256"] = "0" * 64
    r = H.record_decision(qa, story, bad)
    assert r.rc == 4 and r.error_code == "REFERENCE_INVALID" and r.json["committed"] is False, r.describe()
    assert H.decisions(qa) == []
    # 用 A1 的批准去执行 A2：不生效
    r = H.record_decision(qa, story, H.plan_decision(qa, story, a1["id"]))
    assert r.rc == 0, r.describe()
    d2 = qa.api("action", "dispatch", "--id", a2["id"])
    assert d2.rc == 3 and d2.error_code in REFUSED, d2.describe()
    assert H.platform_writes(ws) == 0
    # 角色核验：策略规定的授权角色是 tester，其他角色的本地记录不能授权该动作
    cfg = json.loads((ws / "config" / "qa-flow.json").read_text(encoding="utf-8"))
    assert cfg["rules"]["policy"]["authorizer_role"] == "tester"
    wrong = H.plan_decision(qa, story, a2["id"], role="developer", actor="研发-王五")
    rec = H.record_decision(qa, story, wrong)
    d3 = qa.api("action", "dispatch", "--id", a2["id"])
    assert rec.rc != 0 or d3.rc != 0, \
        "角色不是策略授权角色（tester）的决定被用来授权了动作：" + d3.out[:400]
    assert H.platform_writes(ws) == 0


# ---------------------------------------------------------------------------
# AC-062 同一 OS 用户的自述不冒充认证
# ---------------------------------------------------------------------------

def _real_project(qa, env: str):
    """真实项目配置：缺陷/文档连接器 real-docs 指向 env 环境的同名目标。"""
    tg = [{"id": "real-docs", "environment": env, "credential_ref": "vault:qa/real-docs",
           "allowed_operations": ["doc.archive", "release.probe"]}]
    ad = [{"id": "real-docs", "kind": "issue", "impl": "fake_platform", "capabilities": ["doc_create", "doc_update_etag"],
           "scope": "write", "credential_ref": "vault:qa/real-docs"}]
    r = H.setup_project(qa, H.project_profile(qa, targets=tg, adapters=ad))
    assert r.rc == 0 and r.json["applied"] is True, r.describe()
    s1 = H.write_source(qa)
    st = H.start_story(qa, s1)
    story, src = st["story"], st["sources"]["s1.md"]
    qr = H.submit(qa, story, "quality_review", H.quality_review(src))
    assert qr.rc == 0, qr.describe()
    return H.ws_of(qa, story), story, qr


@ac("AC-062")
@layer("offline")
def test_ac062_本地自述不能用于生产写操作(qa):
    ws, story, qr = _real_project(qa, "production")
    local = {"subject_refs": [{"ref": qr.result["artifact_id"], "sha256": qr.result["sha256"]}],
             "role": "product", "actor": "产品负责人", "verification": "local_attested",
             "scope": {"environment": "production", "target_ids": ["real-docs"], "action_kinds": ["doc.archive"],
                       "story_id": story},
             "result": "approve", "reason": "我是产品负责人，同意写入生产"}
    rec = H.record_decision(qa, story, local)
    notes = [rec]
    for d in H.decisions(qa):
        assert d["verification"] == "local_attested", "本地自述不得被登记为更高的核验方式"
        notes.append(qa.api_ok("decision", "verify", "--id", d["id"]))

    r = H.archive_plan(qa, story, qr.result["artifact_id"], "qa-archive", "--dispatch")
    assert r.rc == 3 and r.error_code in ("AUTHORIZATION_INVALID", "AUTHORIZATION_REQUIRED"), r.describe()
    assert r.json["committed"] is False
    assert H.actions(qa) == [], "不得为生产写入登记任何可执行动作"
    assert qa.query("SELECT COUNT(*) AS n FROM action_attempts")[0]["n"] == 0
    assert not (ws / "lab" / "platform.db").exists(), "不得触达任何平台（包括实验桩）"
    # 报告明确：本地自述不能证明角色身份
    said = H.all_output(r, *notes) + qa.text("admin", "doctor", "--workspace", str(ws)).out
    assert "不能证明" in said or "不是已认证" in said, said[:1500]

    # lab 模拟验证不能跨到真实目标；本地命令行也不能自称外部核验
    for verification in ("simulated", "external_verified"):
        x = H.record_decision(qa, story, dict(local, verification=verification))
        assert x.rc == 3 and x.error_code == "AUTHORIZATION_INVALID" and x.json["committed"] is False, x.describe()
    assert all(d["verification"] == "local_attested" for d in H.decisions(qa))


@ac("AC-062")
@layer("offline")
def test_ac062_本地自述不能授权真实测试目标的写操作(qa):
    ws, story, qr = _real_project(qa, "test")
    plan = H.archive_plan(qa, story, qr.result["artifact_id"], "qa-archive")
    assert plan.rc == 0, plan.describe()
    a = plan.result["action"]
    assert a["environment"] == "test" and a["state"] == "awaiting_authorization"
    rec = H.record_decision(qa, story, H.plan_decision(qa, story, a["id"], role="product", actor="产品负责人"))
    d = qa.api("action", "dispatch", "--id", a["id"])
    assert d.rc == 3 and d.error_code in REFUSED, d.describe()
    row = _action(qa, a["id"])
    assert row["state"] == "awaiting_authorization" and row["authorization_ref"] is None
    assert qa.query("SELECT COUNT(*) AS n FROM action_attempts WHERE outcome='succeeded'")[0]["n"] == 0
    assert not (ws / "lab" / "platform.db").exists()
    # 日常入口同样不能用本地答复批准
    item = [i for i in H.review_items(qa, story) if i["kind"] == "action_authorization"][0]
    rv = qa.run("review", "--answer", f"{item['handle']}=A")
    assert rv.rc == 3 and rv.error_code == "AUTHORIZATION_REQUIRED", rv.describe()
    assert "本地" in rv.errors[0]["message"]
    assert _action(qa, a["id"])["state"] == "awaiting_authorization"
    assert rec.rc in (0, 3)


# ---------------------------------------------------------------------------
# AC-087 / AC-088 共用一次完整实验
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def exec_demo(tmp_path_factory):
    qa = H.make_qa(tmp_path_factory.mktemp("ac087"))
    flow.drive(qa)
    ws, story = qa.workspace(), H.demo_story(qa)
    o: dict = {"qa": qa, "ws": ws, "story": story}
    o["final_status"] = qa.ok("status").result
    o["runs"] = H.snapshots(qa, "execution_run", story)
    o["attempts"] = {a["id"]: a for a in H.snapshots(qa, "case_attempt", story)}
    o["receipts"] = {r["id"]: r for r in qa.query("SELECT * FROM worker_receipts")}
    o["actions"] = {a["id"]: a for a in H.actions(qa)}
    o["action_attempts"] = qa.query("SELECT * FROM action_attempts")
    o["evidence"] = {e["id"]: e for e in qa.query("SELECT * FROM evidence")}
    o["scripts"] = {s["id"]: s for s in H.snapshots(qa, "script_revision", story)}
    o["show_run"] = qa.api("artifact", "show", "--id", o["runs"][-1]["id"])
    o["exec_status"] = qa.api("execution", "status", "--story", story)
    o["unknown_target"] = qa.api("execution", "plan", "--story", story, "--cases", "C1", "--target",
                                 "refund.unknown.example", "--dispatch")
    o["unknown_probe"] = qa.api("environment", "check", "--story", story, "--target",
                                "https://refund.prod.example.invalid")
    o["actions_after_unknown"] = H.actions(qa)

    # ---- AC-088：自述为生产的记录 ----
    last = o["runs"][-1]["envelope"]["payload"]
    latest_att = o["attempts"][last["attempt_refs"][-1]]
    lab_ev = latest_att["envelope"]["payload"]["evidence_refs"][0]
    o["forged_run"] = H.submit(qa, story, "execution_run", dict(last, origin="production"))
    o["forged_attempt"] = H.submit(qa, story, "case_attempt", dict(latest_att["envelope"]["payload"], outcome="pass"))
    o["forged_release"] = H.submit(qa, story, "release_check", {
        "deployment": "prod-deploy-1", "actual_build": "fixed", "environment": "production",
        "window": {"start": "2026-11-16T00:00:00Z", "end": "2026-11-16T02:00:00Z"},
        "probes": [{"id": "P1", "result": "pass", "evidence_ref": lab_ev}], "metric_samples": [], "thresholds": [],
        "verdict": "pass"})
    o["release_stage"] = qa.api("stage", "evaluate", "--story", story, "--stage", "release_verify")
    o["origins_after_forgery"] = qa.query("SELECT DISTINCT origin FROM evidence") + \
        qa.query("SELECT DISTINCT origin FROM snapshots")

    # ---- AC-088：改写证据文件一字节 ----
    row = o["evidence"][lab_ev]
    path = ws / row["rel_path"]
    original = path.read_bytes()
    o["tampered_ev"], o["tampered_attempt"] = lab_ev, latest_att["id"]
    o["seq"] = H.last_seq(qa)
    o["snap_count"] = H.count(qa, "snapshots")
    os.chmod(path, 0o600)
    path.write_bytes(original[:-2] + bytes([original[-2] ^ 0x01]) + original[-1:])
    try:
        o["t_exec"] = qa.api("stage", "evaluate", "--story", story, "--stage", "test_execution")
        o["t_exit"] = qa.api("stage", "evaluate", "--story", story, "--stage", "test_exit")
        o["t_report"] = qa.api("report", "build", "--story", story)
        o["t_status"] = qa.ok("status")
        o["t_status_text"] = qa.text("status", "--details")
        o["t_export"] = qa.run("export", "--output", str(qa.cwd / "tampered-export"))
        o["t_events"] = H.events_since(qa, o["seq"])
        o["t_exit_report"] = H.snapshots(qa, "exit_report", story)[-1]["envelope"]["payload"]
        o["t_evidence_row"] = qa.query("SELECT * FROM evidence WHERE id=?", (lab_ev,))[0]
        o["t_snap_count"] = H.count(qa, "snapshots")
    finally:
        path.write_bytes(original)
    # 恢复真实字节后可以继续
    o["r_exec"] = qa.api("stage", "evaluate", "--story", story, "--stage", "test_execution")
    o["r_exit"] = qa.api("stage", "evaluate", "--story", story, "--stage", "test_exit")
    return o


@ac("AC-087")
@layer("offline")
def test_ac087_执行记录与工作进程回执和证据一致(exec_demo):
    o = exec_demo
    ws = o["ws"]
    assert o["runs"], "前置：完整实验应产生执行记录"
    seen_receipts = set()
    for run in o["runs"]:
        p, env = run["envelope"]["payload"], run["envelope"]
        assert env["producer"]["verification"] == "worker_receipt" and run["origin"] == p["origin"] == "local_lab"
        action = [a for a in o["actions"].values() if a["plan_ref"] == p["plan_ref"]]
        assert len(action) == 1 and action[0]["kind"] == "execute.cases" and action[0]["state"] == "succeeded"
        action = action[0]
        plan = H.envelope(o["qa"], p["plan_ref"])["payload"]
        assert action["plan_sha256"] == H.sha_obj(plan) and plan["target_id"] == p["target"] == action["target_id"]
        leases = [x for x in o["action_attempts"] if x["action_id"] == action["id"] and x["outcome"] == "succeeded"]
        assert len(leases) == 1
        lease = leases[0]
        rids = p["worker_receipt"].split(",")
        assert len(rids) == len(p["attempt_refs"]) == len(plan["request"]["cases"])
        for rid, att_id in zip(rids, p["attempt_refs"]):
            assert rid not in seen_receipts, "回执不可复用"
            seen_receipts.add(rid)
            rec = o["receipts"][rid]
            data = json.loads(rec["data"])
            att = o["attempts"][att_id]["envelope"]
            ap = att["payload"]
            # 回执绑定动作、租约代次与工作进程
            assert rec["action_id"] == action["id"] and rec["lease_epoch"] == lease["lease_epoch"] == data["lease_epoch"]
            assert rec["worker_id"] == lease["worker_id"] == data["worker"] == att["producer"]["name"]
            assert rec["sha256"] == H.sha_obj(data), "回执摘要应与回执内容一致"
            # 用例、构建、数据集
            assert data["tc"] == ap["tc_ref"] and ap["tc_ref"] in plan["request"]["cases"]
            assert ap["build"] == p["build"] == att["context"]["build"] == plan["request"]["expected_build"]
            assert att["context"]["dataset"] == p["dataset"] == plan["request"]["dataset"]
            assert ap["run_ref"] == action["id"] and ap["purpose"] == p["purpose"]
            # 时间窗：脚本运行落在该次租约之内
            assert lease["started_at"] <= data["started_at"] <= data["finished_at"] <= lease["finished_at"]
            # 证据：登记摘要 = 回执摘要 = 磁盘字节摘要，来源等级来自 worker 而非自述
            ev = o["evidence"][data["stdout_ref"]]
            assert data["stdout_ref"] == ap["observations_ref"] and data["stdout_ref"] in ap["evidence_refs"]
            assert ev["sha256"] == data["stdout_sha256"] == sha256_file(ws / ev["rel_path"])
            assert ev["origin"] == "local_lab" and ev["source"] == f"worker:{action['id']}:{ap['tc_ref']}"
            assert json.loads((ws / ev["rel_path"]).read_text(encoding="utf-8"))["case"] == ap["tc_ref"]
            script = o["scripts"][ap["script_ref"]]["envelope"]["payload"]
            assert data["script_sha256"] == script["file_sha256"]
            assert data["exit_code"] == 0 and data["timed_out"] is False
    # 公共读取接口给出的核验结果
    assert o["show_run"].rc == 0 and o["show_run"].json["current"] is True
    assert o["show_run"].json["sha256"] == o["runs"][-1]["sha256"]
    latest = o["exec_status"].json["latest"]
    assert set(latest) == {"SMOKE-1", "SMOKE-2", "C1", "C2", "C3", "C4", "C5"}
    assert all(v["outcome"] == "pass" for v in latest.values())


@ac("AC-087")
@layer("offline")
def test_ac087_原始证据保留并有单独脱敏展示件(exec_demo):
    o = exec_demo
    ws = o["ws"]
    run_evs = [e for e in o["evidence"].values() if (e["source"] or "").startswith("worker:")]
    assert run_evs
    for e in run_evs:
        assert e["state"] == "active" and "/raw/" in e["rel_path"] and (ws / e["rel_path"]).is_file(), "原始证据应保留"
    redacted = [e for e in o["evidence"].values() if e["redacted_of"]]
    files = [p for p in (ws / "evidence" / "redacted").rglob("*") if p.is_file()]
    missing = [e["id"] for e in run_evs if not any(r["redacted_of"] == e["id"] for r in redacted)]
    assert redacted and files and not missing, \
        f"执行证据没有单独的脱敏展示件：脱敏登记 {len(redacted)} 条，脱敏目录文件 {len(files)} 个，缺少展示件的原始证据 {len(missing)} 条"


@ac("AC-088")
@layer("offline")
def test_ac088_改写证据一字节后不能准出(exec_demo):
    o = exec_demo
    assert o["final_status"]["verdict"] == "pass", "前置：篡改前实验已准出"
    for key in ("t_exec", "t_exit"):
        res = o[key]
        assert res.rc != 0 or (res.result["status"] != "completed" and res.result["verdict"] != "pass"), res.describe()
    assert o["t_status"].result["verdict"] != "pass"
    stages = {s["stage"]: s for s in o["t_status"].result["stages"]}
    assert stages["test_exit"]["status"] != "completed" and stages["test_execution"]["status"] != "completed"
    assert o["t_exit_report"]["verdict"] != "pass"
    rep = o["t_report"]
    assert rep.rc != 0 or rep.json["exit_report"]["verdict"] != "pass"
    # 被篡改的尝试不再计入有效结果
    att_tc = o["attempts"][o["tampered_attempt"]]["envelope"]["payload"]["tc_ref"]
    assert o["tampered_attempt"] not in [v.get("attempt") for v in
                                         (o["t_report"].json or {}).get("test_report", {}).get("latest", {}).values()
                                         if isinstance(v, dict)]
    assert att_tc
    # 保留审计：原登记记录与快照未被删除或改写，登记摘要仍是原值
    assert o["t_evidence_row"]["sha256"] == o["evidence"][o["tampered_ev"]]["sha256"]
    assert o["t_snap_count"] >= o["snap_count"]
    assert o["qa"].query("SELECT 1 FROM events WHERE object_id=? AND event_type='evidence_registered'",
                         (o["tampered_ev"],))
    assert any(e["event_type"].startswith("stage_") for e in o["t_events"])
    # 恢复真实字节后不出现永久误锁
    assert o["r_exec"].rc == 0 and o["r_exec"].result["verdict"] == "pass", o["r_exec"].describe()
    assert o["r_exit"].rc == 0 and "准出规则计算通过" in " ".join(o["r_exit"].result["reasons"]), o["r_exit"].describe()


@ac("AC-088")
@layer("offline")
def test_ac088_摘要冲突被明确指出并留下审计(exec_demo):
    """拒绝应指明是证据摘要冲突，而不是泛化为“未执行/已过期”。"""
    o = exec_demo
    crit = {c["id"]: c for c in o["t_exit_report"]["criterion_results"]}
    reasons = " ".join(o["t_exec"].result.get("reasons", []) + o["t_exit"].result.get("reasons", []))
    shown = reasons + o["t_status_text"].out + json.dumps(o["t_status"].result["summary"], ensure_ascii=False)
    codes = [e.get("code") for r in (o["t_exec"], o["t_exit"], o["t_report"], o["t_export"]) for e in r.errors]
    named = "EVIDENCE_INVALID" in codes or any(k in shown for k in ("摘要不符", "证据摘要", "证据无效", "证据已失效",
                                                                    "证据缺失或无效", "存在缺失或无效证据"))
    audit = [e for e in o["t_events"] if o["tampered_ev"] in (e["object_id"] + e["payload"])
             or "摘要" in e["payload"] or "EVIDENCE_INVALID" in e["payload"]]
    problems = []
    if crit["registered_evidence"]["result"] == "pass":
        problems.append("准出报告的“结果证据已登记且可读取”仍为 pass")
    if not named:
        problems.append("阶段原因与状态输出没有指出证据摘要冲突：" + reasons)
    if not audit:
        problems.append("没有记录摘要冲突的审计事件：" + str(sorted({e['event_type'] for e in o['t_events']})))
    assert not problems, "；".join(problems)


@ac("AC-088")
@layer("offline")
def test_ac088_来源等级不由提交者自述决定(exec_demo):
    o = exec_demo
    for key in ("forged_run", "forged_attempt"):
        r = o[key]
        assert r.rc == 2 and r.error_code == "UNSUPPORTED_CONTRACT" and r.json["committed"] is False, r.describe()
    r = o["forged_release"]
    assert r.rc == 4 and r.error_code == "ORIGIN_MISMATCH" and r.json["committed"] is False, r.describe()
    assert o["release_stage"].result["verdict"] != "pass" and o["release_stage"].result["status"] != "completed"
    assert H.snapshots(o["qa"], "release_check", o["story"]) == []
    assert "production" not in {r["origin"] for r in o["origins_after_forgery"]}
    assert len(H.snapshots(o["qa"], "execution_run", o["story"])) == len(o["runs"])
    # 拒绝有审计
    rej = o["qa"].query("SELECT payload FROM events WHERE event_type='command_rejected'")
    assert any(json.loads(e["payload"]).get("code") == "ORIGIN_MISMATCH" for e in rej)


# ---------------------------------------------------------------------------
# AC-091 过期或变更的批准失效
# ---------------------------------------------------------------------------

@ac("AC-091")
@layer("offline")
def test_ac091_批准过期后dispatch被拒且外部调用为零(qa):
    ws, story, trp, ext = _archive_lab(qa)
    t0, late = qa.test_mode(QA_FLOW_TEST_CLOCK=T0), qa.test_mode(QA_FLOW_TEST_CLOCK=LATE)
    a1 = _plan(t0, story, trp)
    rec = H.record_decision(t0, story, H.plan_decision(qa, story, a1["id"], expires_at=EXPIRES))
    assert rec.rc == 0, rec.describe()
    dec = rec.result["decision_ref"]
    assert t0.api_ok("decision", "verify", "--id", dec).json["valid"] is True

    seq = H.last_seq(qa)
    r = late.api("action", "dispatch", "--id", a1["id"])
    assert r.rc == 3 and r.error_code in REFUSED and r.json["committed"] is False, r.describe()
    assert H.platform_rows(ws) == [] and H.platform_rows(ws, "docs") == [], "外部调用数应为 0"
    row = _action(qa, a1["id"])
    assert row["state"] == "awaiting_authorization" and row["authorization_ref"] is None
    assert qa.query("SELECT COUNT(*) AS n FROM action_attempts WHERE outcome='succeeded'")[0]["n"] == 0
    v = late.api_ok("decision", "verify", "--id", dec).json
    assert v["valid"] is False and any("过期" in x for x in v["reasons"])
    # 需要重新确认：该计划的待确认事项仍未答复，没有把旧批准挂到动作上
    item = [i for i in H.review_items(qa, story) if i["key"] == f"auth-{a1['id']}"][0]
    assert item["status"] == "pending" and item["decision_ref"] is None
    assert not [e for e in H.events_since(qa, seq) if e["event_type"] in ("action_succeeded", "action_running")]
    # 对照：未过期时同一批准有效，说明拒绝确由过期引起
    ok = t0.api("action", "dispatch", "--id", a1["id"])
    assert ok.rc == 0 and ok.json["state"] == "succeeded", ok.describe()
    assert H.platform_writes(ws) == 1


@ac("AC-091")
@layer("offline")
def test_ac091_正文目标或环境变化后旧批准不被复用(qa):
    ws, story, trp, ext = _archive_lab(qa)
    t0 = qa.test_mode(QA_FLOW_TEST_CLOCK=T0)
    a1 = _plan(t0, story, trp)                       # H1：报告 TRP → 空间 A
    d1 = H.plan_decision(qa, story, a1["id"], expires_at=EXPIRES)
    rec = H.record_decision(t0, story, d1)
    assert rec.rc == 0, rec.describe()
    dec = rec.result["decision_ref"]

    body = _plan(t0, story, ext)                      # 正文变化：归档内容换成另一份报告
    target = _plan(t0, story, trp, "lab-qa-archive-b")  # 目标变化：同一报告改投另一空间
    assert len({a1["plan_sha256"], body["plan_sha256"], target["plan_sha256"]}) == 3
    for changed in (body, target):
        r = t0.api("action", "dispatch", "--id", changed["id"])
        assert r.rc == 3 and r.error_code in REFUSED, r.describe()
        row = _action(qa, changed["id"])
        assert row["state"] == "awaiting_authorization" and row["authorization_ref"] is None
        # 生成的是新的待确认计划，绑定新摘要
        item = [i for i in H.review_items(qa, story) if i["key"] == f"auth-{changed['id']}"][0]
        assert item["status"] == "pending" and item["plan_sha256"] == changed["plan_sha256"] != a1["plan_sha256"]
    assert H.platform_writes(ws) == 0

    # 环境变化：批准范围写的是 test，当前计划在 lab
    env_dec = H.plan_decision(qa, story, body["id"])
    env_dec["scope"]["environment"] = "test"
    H.record_decision(t0, story, env_dec)
    r = t0.api("action", "dispatch", "--id", body["id"])
    assert r.rc == 3 and r.error_code in REFUSED, r.describe()
    # 把 H1 的批准摘要套到另一计划上：不生效
    forged = H.plan_decision(qa, story, target["id"], plan_sha256=a1["plan_sha256"])
    H.record_decision(t0, story, forged)
    r = t0.api("action", "dispatch", "--id", target["id"])
    assert r.rc == 3 and r.error_code in REFUSED, r.describe()
    assert H.platform_writes(ws) == 0 and H.platform_rows(ws, "docs") == []
    assert [a["authorization_ref"] for a in H.actions(qa)] == [None, None, None]
    assert t0.api_ok("decision", "verify", "--id", dec).json["valid"] is True, "旧批准仍只对 H1 有效"


# ---------------------------------------------------------------------------
# AC-092 审批撤销和远程角色变化（external）
# ---------------------------------------------------------------------------

@ac("AC-092")
@layer("external")
def test_ac092_审批撤销和远程角色变化(qa, tmp_path):
    """先自动化不依赖外部条件的部分（本地撤销、发送前重新核验）；场景本身需要真实审批系统，记 blocked。"""
    ws, story, trp, ext = _archive_lab(qa)
    # 1) 撤销后 dispatch
    a1 = _plan(qa, story, trp)
    dec = H.record_decision(qa, story, H.plan_decision(qa, story, a1["id"])).result["decision_ref"]
    rv = qa.api("decision", "revoke", "--id", dec, "--reason", "审批已撤销")
    assert rv.rc == 0 and rv.result["revoked"] is True, rv.describe()
    r = qa.api("action", "dispatch", "--id", a1["id"])
    assert r.rc == 3 and r.error_code in REFUSED, r.describe()
    assert H.platform_writes(ws) == 0
    assert qa.query("SELECT 1 FROM events WHERE object_id=? AND event_type='decision_revoked'", (dec,)), "应记录变化"
    # 2) 已领取租约、发送前被撤销：屏障钩子保证先后
    a2 = _plan(qa, story, ext)
    dec2 = H.record_decision(qa, story, H.plan_decision(qa, story, a2["id"])).result["decision_ref"]
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    p = qa.test_mode(QA_FLOW_PAUSE_AT="before_send_check", QA_FLOW_PAUSE_DIR=str(barrier)).popen(
        "api", "action", "dispatch", "--id", a2["id"])
    try:
        deadline = time.monotonic() + 30
        while not (barrier / "before_send_check.waiting").exists():
            assert p.poll() is None and time.monotonic() < deadline, "dispatch 未到达发送前屏障"
            time.sleep(0.05)
        assert qa.api("decision", "revoke", "--id", dec2, "--reason", "发送前撤销").rc == 0
        (barrier / "before_send_check.go").write_text("go")
        out, err = p.communicate(timeout=60)
    finally:
        if p.poll() is None:
            p.kill()
    res = json.loads(out)
    assert p.returncode == 3 and res["errors"][0]["code"] == "AUTHORIZATION_INVALID", out + err
    assert res["errors"][0]["details"]["external_calls"] == 0
    assert H.platform_writes(ws) == 0 and H.platform_rows(ws, "docs") == []
    row = _action(qa, a2["id"])
    assert row["state"] == "awaiting_authorization" and row["authorization_ref"] is None
    blocked("需要真实原生审批系统的撤销事件与远程角色目录（外部凭证与专用测试空间）；"
            "产品未接入原生审批适配器，只验证了本地撤销与发送前重新核验")


# ---------------------------------------------------------------------------
# AC-093 受保护原文拒绝所有写通道
# ---------------------------------------------------------------------------

REMOTE_A = "https://Docs.Example.invalid/wiki/REQ77?from=share#h2"
REMOTE_B = "https://docs.example.invalid/wiki/REQ77/"
DOC_A = "doc://lab-qa-archive/DOC-ORIG?view=1"
DOC_B = "DOC://lab-qa-archive/doc-orig/"


def _protected_lab(qa):
    ws = H.init_lab(qa)
    s1 = H.write_source(qa)
    link = qa.cwd / "sources" / "链接 s1.md"
    os.symlink(s1, link)
    copy = H.write_source(qa, "s1 副本.md")
    st = H.start_story(qa, s1, copy)
    return ws, st["story"], s1, link, copy, st["sources"]


@ac("AC-093")
@layer("offline")
def test_ac093_不同写法解析到同一受保护身份(qa):
    ws, story, s1, link, copy, srcs = _protected_lab(qa)
    for uri in (str(link), "file://" + str(s1), str(s1.parent / ".." / "sources" / "s1.md")):
        r = qa.api("source", "import", "--story", story, "--uri", uri)
        assert r.rc == 0 and r.result["source_ref"] == srcs["s1.md"] and r.result["changed"] is False, r.describe()
        assert r.result["identity"] == "file:" + os.path.realpath(s1)
    ids = {}
    for uri in (REMOTE_A, REMOTE_B, DOC_A, DOC_B):
        r = qa.api("source", "import", "--story", story, "--uri", uri)
        assert r.rc == 0 and r.result["read_status"] == "unreadable", r.describe()
        ids[uri] = r.result["identity"]
    assert ids[REMOTE_A] == ids[REMOTE_B] and ids[DOC_A] == ids[DOC_B] and ids[REMOTE_A] != ids[DOC_A]
    protected = {a["identity"] for a in H.aggs(qa, "protected_resource")}
    assert protected == {"file:" + os.path.realpath(s1), "file:" + os.path.realpath(copy), ids[REMOTE_A], ids[DOC_A]}


@ac("AC-093")
@layer("offline")
def test_ac093_受保护远端文档拒绝归档覆盖与越界写入(qa):
    ws, story, s1, link, copy, srcs = _protected_lab(qa)
    assert qa.api("source", "import", "--story", story, "--uri", DOC_A).rc == 0
    rep = H.snapshots(qa, "test_report", story)[-1]["id"]
    n_actions = len(H.actions(qa))
    seq = H.last_seq(qa)
    for doc in ("DOC-ORIG", "doc-orig", "DOC-ORIG/"):
        r = H.archive_plan(qa, story, rep, "lab-qa-archive", "--existing-doc", doc, "--if-match", "etag-1", "--dispatch")
        assert r.rc == 3 and r.error_code == "PROTECTED_RESOURCE" and r.json["committed"] is False, r.describe()
    other = H.archive_plan(qa, story, rep, "原文所在空间", "--dispatch")
    assert other.rc == 3 and other.error_code == "PROTECTED_RESOURCE", other.describe()
    assert len(H.actions(qa)) == n_actions, "被拒的写入不得登记动作"
    assert H.platform_writes(ws) == 0 and H.platform_rows(ws, "docs") == [], "远端写次数不变"
    rej = [json.loads(e["payload"]) for e in H.events_since(qa, seq) if e["event_type"] == "command_rejected"]
    assert len(rej) == 4 and {x["code"] for x in rej} == {"PROTECTED_RESOURCE"}
    # 获准的归档空间内新建文档仍可登记（需要授权），未被误锁
    ok = H.archive_plan(qa, story, rep, "lab-qa-archive")
    assert ok.rc == 0 and ok.result["action"]["state"] == "awaiting_authorization"


@ac("AC-093")
@layer("offline")
def test_ac093_本地原文及其符号链接与副本拒绝文件写入(qa):
    """所有接受输出路径的公共命令，都不得把受保护原文（含符号链接、副本）当作写入目标。"""
    ws, story, s1, link, copy, srcs = _protected_lab(qa)
    original = {s1: s1.read_bytes(), copy: copy.read_bytes()}
    legacy = qa.cwd / "legacy-ok"
    (legacy / "qa-mainflow").mkdir(parents=True)
    (legacy / "qa-mainflow" / "rule.txt").write_text("合成\n", encoding="utf-8")
    commands = {
        "export --output": lambda t: qa.run("export", "--output", str(t)),
        "api coverage export --output": lambda t: qa.run("api", "coverage", "export", "--output", str(t)),
        "api backup create --output": lambda t: qa.run("api", "backup", "create", "--output", str(t)),
        "admin legacy-freeze --output": lambda t: qa.run("admin", "legacy-freeze", "--legacy-root", str(legacy),
                                                         "--output", str(t)),
    }
    violations, unhandled = [], []
    for target_name, target in (("原文", s1), ("符号链接", link), ("副本", copy)):
        for name, run in commands.items():
            r = run(target)
            changed = [p.name for p, data in original.items() if not p.exists() or p.read_bytes() != data]
            if changed:
                violations.append(f"{name} → {target_name}：改写了 {changed}（退出码 {r.rc}）")
                for p, data in original.items():  # 恢复测试自己的夹具文件，继续检查其余通道
                    p.write_bytes(data)
            elif r.rc == 0:
                violations.append(f"{name} → {target_name}：返回成功")
            elif "Traceback" in r.err:
                unhandled.append(f"{name} → {target_name}：未处理异常（退出码 {r.rc}）")
            elif r.error_code not in ("PROTECTED_RESOURCE", None):
                pass  # 以其他稳定错误码拒绝且文件未变，可接受
    assert link.is_symlink() and os.path.realpath(link) == os.path.realpath(s1)
    assert not violations, "受保护原文被写入：\n" + "\n".join(violations)
    assert not unhandled, "拒绝时没有稳定错误码：\n" + "\n".join(unhandled)


# ---------------------------------------------------------------------------
# AC-094 生产目标和凭证不能回退测试
# ---------------------------------------------------------------------------

def _two_env_project(qa, *, prod_credential: bool = True):
    prod = {"id": "prod-docs", "environment": "production", "allowed_operations": ["doc.archive", "release.probe"]}
    if prod_credential:
        prod["credential_ref"] = "env:QA_PROD_DOCS_TOKEN"
    tg = [prod, {"id": "test-docs", "environment": "test", "credential_ref": "env:QA_TEST_DOCS_TOKEN",
                 "allowed_operations": ["doc.archive"]}]
    ad = [{"id": "prod-docs", "kind": "issue", "impl": "fake_platform", "capabilities": ["doc_create"],
           "scope": "write", "credential_ref": "env:QA_PROD_DOCS_TOKEN"}]
    return H.project_profile(qa, targets=tg, adapters=ad)


@ac("AC-094")
@layer("offline")
def test_ac094_缺生产凭证时明确阻断且不回退测试凭证(qa):
    # 测试凭证可用（哨兵值），生产凭证缺失
    env = qa.with_env(QA_TEST_DOCS_TOKEN=H.TOKEN)
    # 1) 管理员配置阶段：生产目标没有凭证引用，明确指出并拒绝应用
    r = H.setup_project(env, _two_env_project(env, prod_credential=False))
    assert r.rc == 6 and r.json["applied"] is False, r.describe()
    assert any("credential_ref" in p["path"] and "凭证" in p["message"] for p in r.json["problems"]), r.out
    assert not (qa.root / "prj-m2").exists()
    # 2) 运行阶段：有效配置应用后，生产凭证引用丢失（在本测试的临时工作区内改配置文件模拟）
    r = H.setup_project(env, _two_env_project(env))
    assert r.rc == 0 and r.json["applied"] is True, r.describe()
    s1 = H.write_source(env)
    st = H.start_story(env, s1)
    story, src = st["story"], st["sources"]["s1.md"]
    qr = H.submit(env, story, "quality_review", H.quality_review(src))
    assert qr.rc == 0, qr.describe()
    ws = H.ws_of(qa, story)
    cfg_path = ws / "config" / "qa-flow.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    for t in cfg["targets"]:
        if t["id"] == "prod-docs":
            t.pop("credential_ref")
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    outs = []
    for extra in ((), ("--dispatch",)):
        r = H.archive_plan(env, story, qr.result["artifact_id"], "qa-archive", *extra)
        outs.append(r)
        assert r.rc == 3 and r.error_code == "CREDENTIAL_MISSING" and r.json["committed"] is False, r.describe()
        assert r.errors[0]["related_refs"] == ["prod-docs"]
    t = env.text("api", "archive", "plan", "--story", story, "--report", qr.result["artifact_id"], "--space",
                 "qa-archive", "--dispatch")
    outs.append(t)
    assert t.rc == 3 and "CREDENTIAL_MISSING" in t.err and "凭证" in t.err
    # 不连接 test、不改用测试目标、不输出凭证值
    assert H.actions(qa) == [], "不得登记任何动作（包括改投测试目标）"
    assert qa.query("SELECT COUNT(*) AS n FROM action_attempts")[0]["n"] == 0
    assert not (ws / "lab").exists() or not (ws / "lab" / "platform.db").exists()
    text = H.all_output(*outs)
    assert H.TOKEN not in text and "test-docs" not in text
    assert "测试凭证" not in text and "改用" not in text.replace("不会改用", "")


@ac("AC-094")
@layer("offline")
def test_ac094_未登记的目标被拒且不猜测地址(qa, exec_demo):
    # 连接器指向的目标没有在已核验配置中登记
    ad = [{"id": "docs.unknown.example", "kind": "issue", "impl": "fake_platform", "capabilities": ["doc_create"],
           "scope": "write", "credential_ref": "env:QA_TEST_DOCS_TOKEN"}]
    tg = [{"id": "test-docs", "environment": "test", "credential_ref": "env:QA_TEST_DOCS_TOKEN",
           "allowed_operations": ["doc.archive"]}]
    env = qa.with_env(QA_TEST_DOCS_TOKEN=H.TOKEN)
    r = H.setup_project(env, H.project_profile(env, targets=tg, adapters=ad))
    assert r.rc == 0, r.describe()
    s1 = H.write_source(env)
    st = H.start_story(env, s1)
    qr = H.submit(env, st["story"], "quality_review", H.quality_review(st["sources"]["s1.md"]))
    r = H.archive_plan(env, st["story"], qr.result["artifact_id"], "qa-archive", "--dispatch")
    assert r.rc == 3 and r.error_code == "UNKNOWN_TARGET" and r.json["committed"] is False, r.describe()
    assert r.errors[0]["related_refs"] == ["docs.unknown.example"]
    assert H.actions(qa) == [] and H.TOKEN not in r.out + r.err and "test-docs" not in r.out
    # 实验工作区中，执行与环境探测入口对未知目标同样拒绝，且不登记动作
    o = exec_demo
    for key in ("unknown_target", "unknown_probe"):
        res = o[key]
        assert res.rc == 3 and res.json["committed"] is False, res.describe()
        assert res.error_code in ("UNKNOWN_TARGET", "PRECONDITION_FAILED"), res.describe()
    assert [a["id"] for a in o["actions_after_unknown"]] == list(o["actions"]), "未知目标不得产生动作"
    assert all(a["target_id"] in ("lab-refund", "lab-platform") for a in o["actions_after_unknown"])


# ---------------------------------------------------------------------------
# AC-128 普通用户缺能力时转交管理员
# ---------------------------------------------------------------------------

REMOTE_REQ = "https://docs.example.invalid/wiki/REQ-77"
SECRET_PROMPTS = ("请输入密钥", "请输入 token", "请粘贴", "输入密码", "填写密钥", "api key", "API Key")


def _project_without_capabilities(qa):
    """真实项目没有审批适配器和文档/缺陷连接器；同一根目录下 lab 模板（demo）可用。"""
    qa.ok("demo")
    lab_ws = qa.root / "lab-demo"
    r = H.setup_project(qa, H.project_profile(qa))
    assert r.rc == 0 and r.json["applied"] is True, r.describe()
    prj_ws = qa.root / "prj-m2"
    s1 = H.write_source(qa)
    local = qa.run("start", "--source", str(s1), "--title", "积分退款需求", "--project", "积分商城项目")
    assert local.rc == 0, local.describe()
    return lab_ws, prj_ws


@ac("AC-128")
@layer("lab")
def test_ac128_缺能力时不回退实验环境且只读结果可查看(qa):
    lab_ws, prj_ws = _project_without_capabilities(qa)
    lab_seq, lab_calls = H.last_seq(qa, lab_ws), len(H.model_calls(lab_ws))
    lab_platform = H.platform_writes(lab_ws)

    start = qa.text("start", "--source", REMOTE_REQ, "--title", "远端需求", "--project", "积分商城项目")
    assert start.rc in (0, 3), start.describe()
    resume = qa.text("resume", "--task", "远端需求")
    status = qa.text("status", "--task", "远端需求")
    view = qa.ok("status", "--task", "远端需求").result
    text = start.out + start.err + resume.out + resume.err

    # 明确说明缺少的能力
    assert "连接器" in text and ("没有已配置" in text or "未配置" in text or "尚未配置" in text), text
    # 不静默改成 lab/test 环境或别的账号
    assert view["lab"] is False and view["task"]["project"] == "积分商城项目"
    assert "合成" not in status.out and "实验" not in view["task"]["environment"]
    assert H.last_seq(qa, lab_ws) == lab_seq and len(H.model_calls(lab_ws)) == lab_calls
    assert H.platform_writes(lab_ws) == lab_platform
    assert not (prj_ws / "lab" / "platform.db").exists() and H.model_calls(prj_ws) == []
    src = [s for s in H.snapshots(qa, "source_revision", ws=prj_ws)
           if s["envelope"]["payload"]["uri"] == REMOTE_REQ][-1]["envelope"]["payload"]
    assert src["read_status"] == "unreadable" and "content_ref" not in src
    # 不要求新人填写密钥
    lower = text + status.out
    assert not [p for p in SECRET_PROMPTS if p in lower]
    # 既有只读结果可查看
    assert status.rc == 0 and "需求：远端需求" in status.out
    other = qa.text("status", "--task", "积分退款需求")
    assert other.rc == 0 and "需求：积分退款需求" in other.out
    assert view["verdict"] != "pass"


@ac("AC-128")
@layer("lab")
def test_ac128_缺连接器时给出管理员处理项(qa):
    lab_ws, prj_ws = _project_without_capabilities(qa)
    qa.text("start", "--source", REMOTE_REQ, "--title", "远端需求", "--project", "积分商城项目")
    resume = qa.text("resume", "--task", "远端需求")
    status = qa.text("status", "--task", "远端需求")
    view = qa.ok("status", "--task", "远端需求").result
    shown = resume.out + resume.err + status.out + json.dumps(view["summary"], ensure_ascii=False)
    assert "连接器" in shown, shown
    assert "管理员" in shown, "缺少文档连接器时，没有告诉普通用户需要管理员处理什么：\n" + shown
    # 状态页（不只是一次性的 resume 提示）也应说明受阻原因，而不是只让用户去“生成并提交需求质检”
    assert "连接器" in status.out or "读取受阻" in status.out or "unreadable" in status.out, status.out


@ac("AC-128")
@layer("lab")
def test_ac128_缺审批能力时给出管理员处理项(qa):
    tg = [{"id": "test-docs", "environment": "test", "credential_ref": "vault:qa/test-docs",
           "allowed_operations": ["doc.archive"]}]
    ad = [{"id": "test-docs", "kind": "issue", "impl": "fake_platform", "capabilities": ["doc_create"],
           "scope": "write", "credential_ref": "vault:qa/test-docs"}]
    qa.ok("demo")
    assert H.setup_project(qa, H.project_profile(qa, targets=tg, adapters=ad)).rc == 0
    s1 = H.write_source(qa)
    st = H.start_story(qa, s1, project="积分商城项目")
    story = st["story"]
    prj_ws = qa.root / "prj-m2"
    qr = H.submit(qa, story, "quality_review", H.quality_review(st["sources"]["s1.md"]))
    assert qr.rc == 0, qr.describe()
    plan = H.archive_plan(qa, story, qr.result["artifact_id"], "qa-archive")
    assert plan.rc == 0, plan.describe()
    item = [i for i in H.review_items(qa, story, prj_ws) if i["kind"] == "action_authorization"][0]
    r = qa.text("review", "--task", "积分退款需求", "--answer", f"{item['handle']}=A")
    assert r.rc == 3 and "AUTHORIZATION_REQUIRED" in r.err, r.describe()
    assert "原生审批" in r.err, r.err
    assert qa.query("SELECT COUNT(*) AS n FROM decisions", ws=prj_ws)[0]["n"] == 0
    assert not [p for p in SECRET_PROMPTS if p in r.out + r.err]
    status = qa.text("status", "--task", "积分退款需求")
    assert status.rc == 0
    assert "管理员" in r.err + status.out, \
        "项目未接入原生审批时，错误与状态都没有给出管理员处理项：\n" + r.err + status.out


# ---------------------------------------------------------------------------
# AC-129 配置变更预览失败保留原配置
# ---------------------------------------------------------------------------

SECRET_VALUE = "sk-SENTINELSECRET1234567890"


def _base_project(qa):
    tg = [{"id": "test-docs", "environment": "test", "credential_ref": "vault:qa/test-docs",
           "allowed_operations": ["doc.archive"]}]
    ad = [{"id": "test-docs", "kind": "issue", "impl": "fake_platform", "capabilities": ["doc_create"],
           "scope": "write", "credential_ref": "vault:qa/test-docs"}]
    cfg = H.project_profile(qa, targets=tg, adapters=ad)
    r = H.setup_project(qa, cfg)
    assert r.rc == 0 and r.json["applied"] is True and r.json["problems"] == [], r.describe()
    return cfg, qa.root / "prj-m2"


def _setup(qa, cfg, name, *extra, text=False):
    path = write_json(qa.cwd / "project-config" / f"{name}.json", cfg)
    run = qa.text if text else qa.run
    return run("admin", "setup", "--config", str(path), "--name", "prj-m2", *extra)


@ac("AC-129")
@layer("lab")
def test_ac129_预览与失败的应用都不覆盖原有效配置(qa):
    cfg, ws = _base_project(qa)
    cfg_file = ws / "config" / "qa-flow.json"
    before = (sha256_file(cfg_file), cfg_file.stat().st_mtime_ns, H.last_seq(qa, ws),
              H.count(qa, "events", "event_type='config_applied'", ws=ws), sorted(p.name for p in (ws / "config").iterdir()))

    def state():
        return (sha256_file(cfg_file), cfg_file.stat().st_mtime_ns, H.last_seq(qa, ws),
                H.count(qa, "events", "event_type='config_applied'", ws=ws),
                sorted(p.name for p in (ws / "config").iterdir()))

    # 凭证引用失败：生产目标没有凭证引用，且有人把明文密钥写进了配置
    bad_cred = H.clone(cfg)
    bad_cred["targets"].append({"id": "prod-docs", "environment": "production", "allowed_operations": ["release.probe"]})
    bad_cred["adapters"][0]["api_key"] = SECRET_VALUE
    # 探测失败：新连接器的实现不可用
    bad_probe = H.clone(cfg)
    bad_probe["adapters"].append({"id": "wiki", "kind": "doc", "impl": "wiki_connector_not_installed",
                                  "capabilities": ["doc_read"], "credential_ref": "vault:qa/wiki"})
    outputs = []
    for name, bad, group, needle in (("bad-cred", bad_cred, "targets", "credential_ref"),
                                     ("bad-probe", bad_probe, "adapters", "impl")):
        for extra in ((), ("--apply",)):
            r = _setup(qa, bad, name, *extra)
            outputs.append(r)
            assert r.rc == 6, r.describe()
            rep = r.json
            assert rep["applied"] is False and rep["preview_only"] is (extra == ())
            assert any(needle in p["path"] for p in rep["problems"]), rep["problems"]
            assert all(p["message"] for p in rep["problems"])
            # 报告配置差异与下一步
            assert group in {d["group"] for d in rep["diff"]} and all(d["change"] == "changed" for d in rep["diff"])
            assert "原有效配置未被修改" in rep["next"] and "修正" in rep["next"]
            assert state() == before, f"{name} {extra} 改动了原配置或写入了事件"
        outputs.append(_setup(qa, bad, name, "--apply", text=True))
        assert outputs[-1].rc == 6 and state() == before
    # 不输出凭证明文
    assert SECRET_VALUE not in H.all_output(*outputs) and "SENTINELSECRET" not in H.all_output(*outputs)
    assert SECRET_VALUE not in H.tree_text(ws / "config")
    assert SECRET_VALUE not in json.dumps(qa.query("SELECT payload FROM events", ws=ws), ensure_ascii=False)
    # 原配置仍然有效可用
    doc = qa.ok("admin", "doctor", "--workspace", str(ws)).json
    assert [c for c in doc["checks"] if c["item"] == "config"][0]["status"] == "ok"
    assert json.loads(cfg_file.read_text(encoding="utf-8"))["targets"] == cfg["targets"]


@ac("AC-129")
@layer("lab")
def test_ac129_有效配置的预览不写入_应用后才生效(qa):
    cfg, ws = _base_project(qa)
    cfg_file = ws / "config" / "qa-flow.json"
    sha, seq = sha256_file(cfg_file), H.last_seq(qa, ws)
    good = H.clone(cfg)
    good["timing"]["source_max_age_hours"] = 24
    r = _setup(qa, good, "good")
    assert r.rc == 0 and r.json["preview_only"] is True and r.json["applied"] is False, r.describe()
    assert r.json["diff"] == [{"group": "timing", "change": "changed"}] and "--apply" in r.json["next"]
    assert sha256_file(cfg_file) == sha and H.last_seq(qa, ws) == seq, "预览不得写入"
    # 新工作区的预览同样不落盘
    p = write_json(qa.cwd / "project-config" / "new.json", dict(good, project={"id": "PRJ-NEW", "name": "新项目"}))
    n = qa.run("admin", "setup", "--config", str(p), "--name", "prj-new")
    assert n.rc == 0 and n.json["applied"] is False and not (qa.root / "prj-new").exists()
    # 修正后的配置可以应用（不出现永久误锁）
    a = _setup(qa, good, "good", "--apply")
    assert a.rc == 0 and a.json["applied"] is True, a.describe()
    assert json.loads(cfg_file.read_text(encoding="utf-8"))["timing"]["source_max_age_hours"] == 24
    assert H.count(qa, "events", "event_type='config_applied'", ws=ws) == 2


# ---------------------------------------------------------------------------
# AC-137 错误详情不泄密或建议绕过
# ---------------------------------------------------------------------------

KEY_SENTINEL = "sk-live-SENTINEL1234567890"
BYPASS_HINTS = ("禁用校验", "关闭校验", "跳过校验", "跳过门禁", "绕过", "--force", "--no-verify", "--skip",
                "使用生产凭证", "改用生产", "生产凭证")
VALIDATOR = ('import json, sys\n'
             'print(json.dumps({"ok": False, "errors": [{"code": "E_UPSTREAM", "message": '
             '"upstream 401: Authorization: Bearer %s token=%s"}]}))\n'
             'sys.stderr.write("fatal: token=%s\\n")\nsys.exit(1)\n' % (KEY_SENTINEL, H.TOKEN, H.TOKEN))


def _failing_gate(qa):
    """旧规则适配器的错误体含合成密钥哨兵，且需求质检门禁确实失败。"""
    r = H.setup_project(qa, H.project_profile(qa, validator_body=VALIDATOR))
    assert r.rc == 0, r.describe()
    s1 = H.write_source(qa)
    st = H.start_story(qa, s1)
    story = st["story"]
    payload = H.quality_review(st["sources"]["s1.md"])
    payload["legacy_report"] = {"summary": "合成报告", "score": 90}
    assert H.submit(qa, story, "quality_review", payload).rc == 0
    gate = qa.api("stage", "evaluate", "--story", story, "--stage", "req_gate")
    return story, gate


@ac("AC-137")
@layer("offline")
def test_ac137_默认错误详情与JSON回执均无原密钥(qa):
    story, gate = _failing_gate(qa)
    assert gate.rc != 0 or (gate.result["status"] == "failed" and gate.result["verdict"] == "fail"), gate.describe()
    ws = H.ws_of(qa, story)
    val = H.snapshots(qa, "validation_report", story)
    assert val and val[-1]["envelope"]["payload"]["structure"]["exit_code"] == 1, "前置：适配器确实返回了失败"
    # 一个带稳定错误码的拒绝：门禁失败时请求下游阶段、以及失败后继续
    blocked_stage = qa.api("stage", "evaluate", "--story", story, "--stage", "req_analysis")
    outputs = {
        "默认": [qa.text("status"), qa.text("resume")],
        "详情": [qa.text("status", "--details"), qa.text("resume", "--details")],
        "JSON": [gate, qa.run("status"), qa.run("resume"), blocked_stage, qa.api("stage", "status", "--story", story)],
    }
    for label, results in outputs.items():
        text = H.all_output(*results)
        assert KEY_SENTINEL not in text and H.TOKEN not in text and "SENTINEL1234567890" not in text, \
            f"{label}输出含原密钥"
        assert not [h for h in BYPASS_HINTS if h in text], f"{label}输出含绕过或回退建议：{text[:800]}"
    # 显示门禁失败的原因与安全的下一步
    st = outputs["默认"][0].out
    assert "需求质检：未通过" in st and "下一步" in st
    view = outputs["JSON"][1].result
    assert view["verdict"] != "pass" and view["summary"]["next_actions"]
    exported = qa.run("export", "--output", str(qa.cwd / "out"))
    assert KEY_SENTINEL not in H.all_output(exported) + H.tree_text(qa.cwd / "out")
    assert H.TOKEN not in H.tree_text(qa.cwd / "out")
    events = json.dumps(qa.query("SELECT payload FROM events", ws=ws), ensure_ascii=False)
    assert KEY_SENTINEL not in events and H.TOKEN not in events


@ac("AC-137")
@layer("offline")
def test_ac137_错误回执带稳定错误码与安全恢复建议(qa):
    ws = H.init_lab(qa)
    s1 = H.write_source(qa)
    st = H.start_story(qa, s1)
    story = st["story"]
    # 错误内容里带有密钥样式的输入：未知目标、读不到的文件、伪造引文
    cases = [
        (qa.api("artifact", "show", "--id", KEY_SENTINEL), 4, "REFERENCE_INVALID"),
        (qa.api("artifact", "submit", "--story", story, "--kind", "claim", "--file",
                str(qa.cwd / f"{H.TOKEN}.json")), 4, "SCHEMA_INVALID"),
        (H.submit(qa, story, "claim", {"text": "x", "claim_type": "fact", "scope": "document",
                                       "review_state": "pending",
                                       "evidence_refs": [{"source_ref": st["sources"]["s1.md"],
                                                          "quote": f"Bearer {KEY_SENTINEL}"}]}, name="k"),
         4, "CITATION_NOT_FOUND"),
    ]
    texts = []
    for r, rc, code in cases:
        assert r.rc == rc and r.error_code == code, r.describe()
        err = r.errors[0]
        assert err["recovery"] and set(err) >= {"code", "path", "message", "related_refs", "recovery"}
        assert r.json["status"] == "error" and r.json["committed"] is False
        texts.append(r.out + r.err)
    t = qa.text("api", "artifact", "show", "--id", KEY_SENTINEL)
    d = qa.text("api", "artifact", "show", "--id", KEY_SENTINEL, "--details")
    assert t.rc == 4 and "REFERENCE_INVALID" in t.err and "建议：" in t.err
    texts += [t.out + t.err, d.out + d.err]
    joined = "\n".join(texts)
    assert KEY_SENTINEL not in joined and H.TOKEN not in joined, "错误输出含原密钥"
    assert not [h for h in BYPASS_HINTS if h in joined]


@ac("AC-137")
@layer("offline")
def test_ac137_适配器错误体不经读取接口原样输出(qa):
    """适配器返回的错误体被登记为普通（非私密）产物后，读取接口的 JSON 回执不应带出原密钥。"""
    story, gate = _failing_gate(qa)
    val = H.snapshots(qa, "validation_report", story)[-1]
    shown = qa.api("artifact", "show", "--id", val["id"])
    assert shown.rc == 0, shown.describe()
    leaked = [s for s in (KEY_SENTINEL, H.TOKEN) if s in shown.out]
    assert val["privacy"] in ("private", "secret") or not leaked, \
        f"校验报告 {val['id']}（隐私级别 {val['privacy']}）经 api artifact show 原样输出了密钥哨兵：{leaked}"


@ac("AC-137")
@layer("offline")
def test_ac137_动作失败回执中的适配器错误不含原密钥(qa):
    """实验部署适配器把收到的构建标识原样写进错误体；失败回执与动作列表不应带出原值。"""
    ws = H.init_lab(qa)
    s1 = H.write_source(qa)
    story = H.start_story(qa, s1)["story"]
    r = qa.api("lab", "deploy", "--story", story, "--build", KEY_SENTINEL, "--dispatch")
    row = [a for a in H.actions(qa) if a["kind"] == "deploy.lab_build"][-1]
    assert row["state"] == "failed", "前置：部署动作确实失败\n" + r.describe()
    listing = qa.api("action", "list", "--story", story)
    outs = {"dispatch 回执": r, "action list": listing, "status": qa.run("status"),
            "status --details": qa.text("status", "--details"), "resume": qa.text("resume")}
    leaks = [name for name, res in outs.items() if KEY_SENTINEL in res.out + res.err]
    assert not leaks, f"以下输出含适配器错误体中的原密钥：{leaks}"
