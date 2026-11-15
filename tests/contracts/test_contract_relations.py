"""AC-071、AC-072：结构合格后的跨对象关系校验；资料状态与运行状态不混淆。

全部通过子进程调用公共 CLI；状态由只读数据库连接独立核对。
"""

from __future__ import annotations

import json

import pytest

from support import storage_helpers as sh
from support.cli import write_json

RELATION_CODES = {"REFERENCE_INVALID", "VALIDATION_FAILED", "PRECONDITION_FAILED", "NOT_FOUND"}


@pytest.fixture()
def retest_ws(qa):
    """修复构建 build2 已部署并回归、两条缺陷已在平台置为已解决、尚未关联任何复测。"""
    sh.drive_until_crash(qa, "before_commit:defect.link_retest")
    sh.recover(qa)
    story = sh.story_id(qa)
    ds = {d["tc_ref"]: d for d in sh.defects(qa)}
    assert set(ds) == {"C1", "C3"} and all(d["status"] == "resolved" and d["fix_build"] == "build2"
                                           and d["retests"] == [] for d in ds.values()), ds
    ex = qa.api_ok("execution", "status", "--story", story).json
    assert ex["build"] == "build2"
    return {"qa": qa, "story": story, "defects": ds, "attempts": ex["attempts"]}


def _attempt(attempts, tc, build):
    rows = [a for a in attempts if a["tc"] == tc and a["build"] == build and a["purpose"] != "smoke"]
    assert rows, (tc, build)
    return rows[-1]["id"]


@pytest.mark.ac("AC-071")
@pytest.mark.layer("offline")
@pytest.mark.parametrize("wrong", ["other_tc", "old_build", "nonexistent", "not_an_attempt"])
def test_ac071_retest_bound_to_wrong_attempt_is_rejected(retest_ws, wrong):
    """复测尝试绑定到错误的用例、错误的构建或不存在的对象时，跨对象校验必须拒绝。"""
    qa, story, ds, atts = (retest_ws[k] for k in ("qa", "story", "defects", "attempts"))
    bug = ds["C1"]
    attempt = {
        "other_tc": _attempt(atts, "C2", "build2"),       # 用例不符：C2 的尝试绑定到 C1 的缺陷
        "old_build": _attempt(atts, "C1", "build1"),      # 构建不符：缺陷构建上的失败尝试充当修复构建复测
        "nonexistent": "ATT-000000000000",
        "not_an_attempt": bug["proposal_ref"],            # 类型不符：缺陷提案快照
    }[wrong]
    seq0 = sh.last_seq(qa)
    r = qa.api("defect", "retest", "--story", story, "--defect", bug["id"], "--attempt", attempt)
    problems = sh.rejection_problems(r, RELATION_CODES)
    after = {d["tc_ref"]: d for d in sh.defects(qa)}["C1"]
    if after["retests"] != []:
        problems.append(f"缺陷的复测列表被写入了错误对象：{after['retests']}")
    if sh.events_after(qa, seq0, "defect_retest_linked"):
        problems.append("产生了 defect_retest_linked 事件")
    assert not problems, "；".join(problems) + "\n" + r.describe()
    # 修正为正确的尝试后可以继续
    good = _attempt(atts, "C1", "build2")
    ok = qa.api_ok("defect", "retest", "--story", story, "--defect", bug["id"], "--attempt", good)
    assert ok.result["linked"] == good


@pytest.mark.ac("AC-071")
@pytest.mark.layer("offline")
def test_ac071_structurally_valid_wrong_binding_never_completes(retest_ws):
    """即使错误绑定在结构上被接受，也不能据此关闭缺陷或完成阶段。"""
    qa, story, ds, atts = (retest_ws[k] for k in ("qa", "story", "defects", "attempts"))
    bug = ds["C1"]
    before = sh.stage_statuses(qa, story)
    assert before["test_exit"] != "completed"
    for attempt in (_attempt(atts, "C2", "build2"), _attempt(atts, "C1", "build1")):
        qa.api("defect", "retest", "--story", story, "--defect", bug["id"], "--attempt", attempt)
    actions0 = sh.count(qa, "actions")
    transitions0 = len(sh.platform_calls(qa, op="transition"))
    seq0 = sh.last_seq(qa)
    r = qa.api("defect", "close", "--story", story, "--defect", bug["id"], "--dispatch")
    sh.assert_rejected(r, {"PRECONDITION_FAILED", "REFERENCE_INVALID", "VALIDATION_FAILED"})
    assert sh.count(qa, "actions") == actions0, "不得登记关闭动作"
    assert len(sh.platform_calls(qa, op="transition")) == transitions0, "不得向平台发送状态流转"
    assert not [c for c in sh.platform_rows(qa, "SELECT status FROM items") if c["status"] == "CLOSED"]
    assert {d["tc_ref"]: d for d in sh.defects(qa)}["C1"]["status"] == "resolved"
    ev = qa.api_ok("stage", "evaluate", "--story", story, "--stage", "test_exit")
    assert ev.result["status"] != "completed" and ev.result["verdict"] != "pass", ev.describe()
    assert not [e for e in sh.events_after(qa, seq0) if e["event_type"] == "stage_completed"
                and e["object_id"].endswith(":test_exit")]


@pytest.mark.ac("AC-071")
@pytest.mark.layer("offline")
def test_ac071_artifact_relation_checked_after_shape(retest_ws, tmp_path):
    """JSON 结构合法但引用了错误类型的对象：关系校验拒绝，不登记快照，阶段不变；修正后可提交。"""
    qa, story, atts = retest_ws["qa"], retest_ws["story"], retest_ws["attempts"]
    cqr = sh.latest_snapshot(qa, "case_quality_review")
    design_id = cqr["payload"]["design_ref"]
    bad = dict(cqr["payload"], design_ref=_attempt(atts, "C1", "build2"))  # 结构合法的字符串，指向尝试而非用例设计
    bad_file = write_json(tmp_path / "cqr-bad.json", bad)
    snaps0, seq0, stages0 = sh.count(qa, "snapshots"), sh.last_seq(qa), sh.stage_statuses(qa, story)

    v = qa.api_ok("artifact", "validate", "--story", story, "--kind", "case_quality_review", "--file", str(bad_file))
    assert v.json["valid"] is False
    assert [p["path"] for p in v.json["problems"]] == ["payload.design_ref"], v.out
    assert v.json["problems"][0]["code"] == "REFERENCE_INVALID"

    r = qa.api("artifact", "submit", "--story", story, "--kind", "case_quality_review", "--name", "alt",
               "--file", str(bad_file))
    sh.assert_rejected(r, {"REFERENCE_INVALID"})
    assert r.rc == 4 and r.errors[0]["path"] == "payload.design_ref"
    assert sh.count(qa, "snapshots") == snaps0, "被拒绝的产物不得登记"
    assert sh.stage_statuses(qa, story) == stages0
    new = sh.events_after(qa, seq0)
    assert [e["event_type"] for e in new] == ["command_rejected"], new
    assert "REFERENCE_INVALID" in new[0]["payload"]

    # 仅结构校验通过的说明：validate 明确声明不代表阶段完成
    good_file = write_json(tmp_path / "cqr-good.json", dict(cqr["payload"], design_ref=design_id))
    v2 = qa.api_ok("artifact", "validate", "--story", story, "--kind", "case_quality_review", "--file", str(good_file))
    assert v2.json["valid"] is True
    assert sh.count(qa, "snapshots") == snaps0 and sh.stage_statuses(qa, story) == stages0, "validate 不得改变状态"
    ok = qa.api_ok("artifact", "submit", "--story", story, "--kind", "case_quality_review", "--name", "alt",
                   "--file", str(good_file))
    assert ok.json["committed"] is True


@pytest.mark.ac("AC-071")
@pytest.mark.layer("offline")
def test_ac071_attempt_cannot_be_submitted_directly(retest_ws, tmp_path):
    """尝试记录只能由真实执行产生；直接提交结构合法的尝试被拒绝。"""
    qa, story, atts = retest_ws["qa"], retest_ws["story"], retest_ws["attempts"]
    real = sh.snapshot_payload(qa, _attempt(atts, "C1", "build2"))
    forged = dict(real, tc_ref="C3", build="build1")
    f = write_json(tmp_path / "attempt.json", forged)
    snaps0 = sh.count(qa, "snapshots")
    r = qa.api("artifact", "submit", "--story", story, "--kind", "case_attempt", "--file", str(f))
    sh.assert_rejected(r, {"UNSUPPORTED_CONTRACT"})
    assert r.rc == 2
    assert sh.count(qa, "snapshots") == snaps0


# ---------------------------------------------------------------------------
# AC-072
# ---------------------------------------------------------------------------

@pytest.fixture()
def pre_probe_ws(qa):
    """技术文档已读取并质检；脚本已登记；尚未对实验服务做任何真实调用（除部署外没有动作）。"""
    sh.drive_until_crash(qa, "after_commit:script.register_lab")
    sh.recover(qa)
    story = sh.story_id(qa)
    assert [a["kind"] for a in qa.query("SELECT kind FROM actions")] == ["deploy.lab_build"]
    assert sh.count(qa, "snapshots", "kind IN ('environment_check','case_attempt','execution_run')") == 0
    return qa, story


def _tech_source(qa):
    rows = qa.query("SELECT id, canonical FROM snapshots WHERE kind='source_revision'")
    hit = [r["id"] for r in rows if json.loads(r["canonical"])["payload"]["uri"].endswith("refund-tech.md")]
    assert len(hit) == 1
    return hit[0]


@pytest.mark.ac("AC-072")
@pytest.mark.layer("offline")
def test_ac072_material_provided_runtime_not_claimed(pre_probe_ws):
    """已读技术文档 → 资料状态为已提供；未实测 → 运行状态未验证，不报资料缺失也不报接口失败。"""
    qa, story = pre_probe_ws
    tr = sh.latest_snapshot(qa, "technical_review")["payload"]
    assert tr["applicability"] == "provided"
    assert _tech_source(qa) in tr["source_refs"]
    stages = {s["stage"]: s for s in qa.api_ok("stage", "status", "--story", story).json["stages"] if s}
    tech = stages["tech_gate"]
    assert tech["status"] == "completed" and not any("未提供" in x or "受阻" in x for x in tech["reasons"])
    env = stages["env_ready"]
    assert env["status"] == "pending" and env["verdict"] == "indeterminate", env
    assert env["status"] != "completed" and env["verdict"] not in ("pass", "fail")
    assert not any(k in x for x in env["reasons"] for k in ("缺失", "未提供", "失败", "不符")), env["reasons"]
    v = qa.ok("status").result
    assert "环境与数据准备" not in v["summary"]["have"]
    # 实验业务库与平台桩没有被调用过
    assert sh.platform_calls(qa) == []


@pytest.mark.ac("AC-072")
@pytest.mark.layer("offline")
def test_ac072_readiness_check_not_run_is_unverified_not_failed(pre_probe_ws, tmp_path):
    """准备检查只给出方法、未实际执行：资料已提供、运行未验证；阶段不得完成，也不得虚报失败。"""
    qa, story = pre_probe_ws
    chk = {"target_id": "lab-refund", "build": {"expected": "build1"}, "dataset_refs": [],
           "checks": [{"id": "api", "kind": "version", "method_provided": True, "observed": "not_run"},
                      {"id": "tables", "kind": "data", "method_provided": True, "observed": "not_run"},
                      {"id": "logs", "kind": "observability", "method_provided": True, "observed": "not_run"}],
           "observations": [], "cleanup_plan": "重置数据集 DS1"}
    f = write_json(tmp_path / "check.json", chk)
    r = qa.api_ok("artifact", "submit", "--story", story, "--kind", "environment_check", "--name", "lab-refund",
                  "--file", str(f))
    env = r.result["derived"]["environment"]
    assert env["material_status"] == "provided"
    assert env["runtime_status"] == "not_verified"
    assert env["ready"] is False and all(i["measured"] == "not_run" and i["ready"] is False for i in env["items"])

    seq0 = sh.last_seq(qa)
    ev = qa.api_ok("stage", "evaluate", "--story", story, "--stage", "env_ready").result
    assert ev["status"] != "completed" and ev["verdict"] != "pass"
    assert not [e for e in sh.events_after(qa, seq0) if e["event_type"] == "stage_completed"]
    problems = []
    if ev["verdict"] == "fail" or ev["status"] == "failed":
        problems.append(f"未实测却判定为失败：status={ev['status']} verdict={ev['verdict']}")
    bad = [x for x in ev["reasons"] if any(k in x for k in ("不符", "未通过", "失败", "缺失"))]
    if bad:
        problems.append(f"原因中虚报失败或缺失：{bad}")
    assert not problems, "；".join(problems) + f"\n阶段回执：{json.dumps(ev, ensure_ascii=False)[:800]}"


@pytest.mark.ac("AC-072")
@pytest.mark.layer("offline")
def test_ac072_self_reported_pass_without_runtime_evidence_not_ready(pre_probe_ws, tmp_path):
    """提交者自述“已通过”但没有工作进程证据：不得视为已实测就绪。"""
    qa, story = pre_probe_ws
    chk = {"target_id": "lab-refund", "build": {"expected": "build1", "observed": "build1"}, "dataset_refs": [],
           "checks": [{"id": "api", "kind": "version", "method_provided": True, "observed": "pass"}],
           "observations": [], "cleanup_plan": "重置数据集 DS1", "cleanup_result": "pass"}
    f = write_json(tmp_path / "check.json", chk)
    r = qa.api_ok("artifact", "submit", "--story", story, "--kind", "environment_check", "--name", "lab-refund",
                  "--file", str(f))
    env = r.result["derived"]["environment"]
    assert env["ready"] is False and env["runtime_status"] == "not_verified", env
    assert env["items"][0]["measured"] == "not_run"
    ev = qa.api_ok("stage", "evaluate", "--story", story, "--stage", "env_ready").result
    assert ev["status"] != "completed" and ev["verdict"] != "pass", ev


@pytest.mark.ac("AC-072")
@pytest.mark.layer("offline")
def test_ac072_document_citation_is_not_a_runtime_fact(pre_probe_ws, tmp_path):
    """技术文档写明了接口，但引文只能证明资料已提供；声称“接口实际可用”的运行事实降为推断。"""
    qa, story = pre_probe_ws
    tech = _tech_source(qa)
    quote = "POST /refunds：请求字段 order_id、amount、idempotency_key、role；响应 refund_id。"
    claim = {"text": "POST /refunds 在实验环境可用并返回 refund_id", "claim_type": "fact", "scope": "runtime",
             "review_state": "pending", "evidence_refs": [{"source_ref": tech, "quote": quote}]}
    f = write_json(tmp_path / "claim.json", claim)
    r = qa.api_ok("artifact", "submit", "--story", story, "--kind", "claim", "--name", "api-available", "--file", str(f))
    cls = r.result["derived"]["classification"]
    assert cls["accepted_as"] != "fact" and cls["accepted_as"] != "rejected", cls
    assert cls["accepted_as"] == "inference"
    # 同一引文作为文档范围的事实是成立的：资料没有被误报为缺失
    doc_claim = dict(claim, scope="document", text="技术方案写明 POST /refunds 的请求字段")
    f2 = write_json(tmp_path / "claim-doc.json", doc_claim)
    r2 = qa.api_ok("artifact", "submit", "--story", story, "--kind", "claim", "--name", "api-documented", "--file", str(f2))
    assert r2.result["derived"]["classification"]["accepted_as"] == "fact"
