"""批次 M1：存储、幂等、结果未知与恢复（AC-081~086、089、090、095~098、107、113）。

同时覆盖方案 §8 故障矩阵中的相关行：提交各断点崩溃、相同预期修订并发、响应丢失后核对、
旧 worker 晚到、备份恢复、磁盘写失败/证据缺失/数据库损坏。

约束：只通过子进程调用公共 CLI；业务结果由只读数据库连接、证据文件与平台桩账本独立核对。
并发与先后顺序使用产品提供的屏障钩子或进程 join，不用固定 sleep。
"""

from __future__ import annotations

import json
import os
import shutil
import stat

import pytest

from support import flow
from support import storage_helpers as sh
from support.cli import QA, sha256_file, tree_digest, write_json

STALE_CODES = {"SOURCE_STALE", "SOURCE_UNREADABLE", "EVIDENCE_INVALID"}
UNAVAILABLE_CODES = {"RULES_UNAVAILABLE", "DEPENDENCY_UNAVAILABLE", "CAPABILITY_UNAVAILABLE", "CONFIG_INVALID"}
RESTORE_FAIL_CODES = {"EVIDENCE_INVALID", "IO_ERROR", "WORKSPACE_INVALID", "INTERNAL_INCONSISTENCY",
                      "DEPENDENCY_UNAVAILABLE"}

needs_chmod = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root 用户下 chmod 不能阻止读写")


# ===========================================================================
# AC-081 来源字节快照和读取状态
# ===========================================================================

@pytest.mark.ac("AC-081")
@pytest.mark.layer("offline")
def test_ac081_same_name_sources_kept_apart_with_read_status(qa, tmp_path):
    qa.ok("demo")
    story = sh.story_id(qa)
    a = tmp_path / "目录 a" / "spec.md"
    b = tmp_path / "目录 b" / "spec.md"
    for f, text in ((a, "# 规格\n甲方内容\n"), (b, "# 规格\n乙方内容\n")):
        f.parent.mkdir()
        f.write_text(text, encoding="utf-8")
    remote = "https://docs.example.invalid/wiki/spec.md"
    absent = tmp_path / "不存在" / "spec.md"
    before = {str(a): sha256_file(a), str(b): sha256_file(b)}
    snaps0 = sh.count(qa, "snapshots", "kind='source_revision'")

    res = {}
    for uri in (str(a), str(b), remote, str(absent)):
        r = qa.api_ok("source", "import", "--story", story, "--uri", uri)
        assert r.json["committed"] is True
        res[uri] = r.result
    refs = [v["source_ref"] for v in res.values()]
    assert len(set(refs)) == 4, "同名来源不得合并"
    assert sh.count(qa, "snapshots", "kind='source_revision'") == snaps0 + 4

    rows = {r["id"]: r for r in qa.query("SELECT id, lineage, privacy, canonical FROM snapshots WHERE kind='source_revision'")}
    assert len({rows[i]["lineage"] for i in refs}) == 4
    for uri, f in ((str(a), a), (str(b), b)):
        p = json.loads(rows[res[uri]["source_ref"]]["canonical"])["payload"]
        assert p["uri"] == uri and p["read_status"] == "available"
        assert p["content_sha256"] == before[uri], "摘要必须是来源字节的摘要"
        ev = qa.query("SELECT * FROM evidence WHERE id=?", (p["content_ref"],))[0]
        stored = qa.workspace() / ev["rel_path"]
        assert stored.read_bytes() == f.read_bytes(), "必须保存字节快照"
        assert ev["sha256"] == before[uri]
    pa = json.loads(rows[res[str(a)]["source_ref"]]["canonical"])["payload"]
    pb = json.loads(rows[res[str(b)]["source_ref"]]["canonical"])["payload"]
    assert pa["content_sha256"] != pb["content_sha256"] and pa["content_ref"] != pb["content_ref"]

    pr = json.loads(rows[res[remote]["source_ref"]]["canonical"])["payload"]
    assert pr["uri"] == remote
    assert pr["read_status"] == "unreadable", "读取受阻不得记为 missing"
    assert pr["read_status"] != "missing" and pr.get("failure_reason")
    assert not pr.get("content_sha256") and not pr.get("content_ref"), "没有字节就没有摘要"
    pm = json.loads(rows[res[str(absent)]["source_ref"]]["canonical"])["payload"]
    assert pm["read_status"] == "missing", "不存在与读取受阻必须区分"

    # 重复登记同一 URI 不产生新的身份；原文件未被修改
    again = qa.api_ok("source", "import", "--story", story, "--uri", str(a)).result
    assert again["source_ref"] == res[str(a)]["source_ref"] and again["changed"] is False
    assert {str(a): sha256_file(a), str(b): sha256_file(b)} == before
    # 读取受阻的来源不能作为事实依据
    qr = {"rule_ref": "x", "source_refs": [res[remote]["source_ref"]],
          "findings": [{"id": "F1", "text": "规格", "severity": "info",
                        "citations": [{"source_ref": res[remote]["source_ref"], "quote": "甲方内容"}]}],
          "testability": {"verdict": "pass"}}
    r = qa.api("artifact", "submit", "--story", story, "--kind", "quality_review", "--name", "remote",
               "--file", str(write_json(tmp_path / "qr.json", qr)))
    sh.assert_rejected(r, STALE_CODES)


# ===========================================================================
# AC-082 旧快照到期不能充当最新
# ===========================================================================

def _review_citing(src: str, quote: str) -> dict:
    return {"rule_ref": "x", "source_refs": [src],
            "findings": [{"id": "F1", "text": "金额规则明确", "severity": "info",
                          "citations": [{"source_ref": src, "quote": quote}]}],
            "testability": {"verdict": "pass"}}


@needs_chmod
@pytest.mark.ac("AC-082")
@pytest.mark.layer("offline")
def test_ac082_expired_unreadable_snapshot_cannot_back_new_conclusion(qa, tmp_path):
    qa.ok("demo")
    story = sh.story_id(qa)
    quote = "退款金额必须为正整数分。"
    f = tmp_path / "s1.md"
    f.write_text(f"# 需求 S1\n{quote}\n", encoding="utf-8")
    t0 = qa.test_mode(QA_FLOW_TEST_CLOCK=sh.utc_after(0))
    t1 = qa.test_mode(QA_FLOW_TEST_CLOCK=sh.utc_after(100))  # 策略新鲜度为 72 小时
    assert sh.read_config(qa)["timing"]["source_max_age_hours"] == 72
    s1 = t0.api_ok("source", "import", "--story", story, "--uri", str(f)).result["source_ref"]
    s1_payload = sh.snapshot_payload(qa, s1)
    qr_file = write_json(tmp_path / "qr.json", _review_citing(s1, quote))
    fresh = t0.api_ok("artifact", "validate", "--story", story, "--kind", "quality_review", "--file", str(qr_file))
    assert fresh.json["valid"] is True, "对照：新鲜期内该结论可成立"

    os.chmod(f, 0)
    try:
        snaps0, seq0 = sh.count(qa, "snapshots"), sh.last_seq(qa)
        v = t1.api_ok("artifact", "validate", "--story", story, "--kind", "quality_review", "--file", str(qr_file))
        assert v.json["valid"] is False and s1 in v.json["problems"][0]["related_refs"]
        r = t1.api("artifact", "submit", "--story", story, "--kind", "quality_review", "--name", "s1",
                   "--file", str(qr_file))
        sh.assert_rejected(r, STALE_CODES)
        assert s1 in r.errors[0]["related_refs"]
        assert sh.count(qa, "snapshots") == snaps0, "不得登记基于过期快照的肯定结论"
        audit = sh.events_after(qa, seq0)
        assert [e["event_type"] for e in audit] == ["command_rejected"]
        assert quote not in audit[0]["payload"], "审计不得包含原文"

        # 刷新：远端（此处为无读取权限的文件）不可读，如实登记失败原因，S1 保留为历史
        ref = t1.api_ok("source", "refresh", "--story", story).json["refreshed"]
        mine = [x for x in ref if x["identity"].endswith("/s1.md")]
        assert len(mine) == 1 and mine[0]["read_status"] == "unreadable" and mine[0]["failure_reason"], mine
        assert mine[0]["source_ref"] != s1
        s2 = sh.snapshot_payload(qa, mine[0]["source_ref"])
        assert s2["read_status"] == "unreadable" and s2["failure_reason"] and not s2.get("content_sha256")
        shown = t1.api_ok("artifact", "show", "--id", s1).json
        assert shown["current"] is False, "S1 不再是当前版本"
        assert shown["envelope"]["payload"] == s1_payload, "S1 历史内容不变"
        ev = qa.query("SELECT * FROM evidence WHERE id=?", (s1_payload["content_ref"],))[0]
        assert ev["state"] == "active"
        assert (qa.workspace() / ev["rel_path"]).read_text(encoding="utf-8").endswith(quote + "\n"), "S1 字节快照仍可读"
        r2 = t1.api("artifact", "submit", "--story", story, "--kind", "quality_review", "--name", "s1",
                    "--file", str(qr_file))
        sh.assert_rejected(r2, STALE_CODES)
        assert sh.count(qa, "snapshots", "kind='quality_review' AND lineage LIKE '%:s1'") == 0
    finally:
        os.chmod(f, 0o600)
    # 修正后可继续：来源恢复可读并刷新后，基于最新快照的结论可以登记
    ref = t1.api_ok("source", "refresh", "--story", story).json["refreshed"]
    s3 = [x for x in ref if x["identity"].endswith("/s1.md")][0]
    assert s3["read_status"] == "available" and s3["source_ref"] not in (s1,)
    ok = t1.api_ok("artifact", "submit", "--story", story, "--kind", "quality_review", "--name", "s1",
                   "--file", str(write_json(tmp_path / "qr3.json", _review_citing(s3["source_ref"], quote))))
    assert ok.json["committed"] is True


@needs_chmod
@pytest.mark.ac("AC-082")
@pytest.mark.layer("offline")
def test_ac082_stage_not_completed_from_expired_source(qa):
    """需求来源超过新鲜度且不可读时，依赖它的阶段不能给出完成结论。"""
    qa.ok("demo")
    story = sh.story_id(qa)
    assert sh.stage_statuses(qa, story)["req_gate"] == "completed"
    req = qa.workspace() / "lab" / "materials" / "refund-requirement.md"
    t1 = qa.test_mode(QA_FLOW_TEST_CLOCK=sh.utc_after(100))
    src_before = qa.query("SELECT id, sha256 FROM snapshots WHERE kind='source_revision' ORDER BY id")
    os.chmod(req, 0)
    try:
        seq0 = sh.last_seq(qa)
        ev = t1.api_ok("stage", "evaluate", "--story", story, "--stage", "req_gate").result
        assert ev["status"] != "completed" and ev["verdict"] == "indeterminate", ev
        assert any("stale" in x or "过期" in x or "新鲜度" in x for x in ev["reasons"]), ev["reasons"]
        r = t1.run("resume")
        assert r.rc in (0, 3), r.describe()
        st = t1.api_ok("stage", "status", "--story", story).json["statuses"]
        assert st["req_gate"] != "completed", st
        assert not [e for e in sh.events_after(qa, seq0, "stage_completed")]
        assert any("unreadable" in n or "读取状态" in n for n in (r.result or {}).get("notes", [])), r.out[:800]
    finally:
        os.chmod(req, 0o600)
    after = {x["id"]: x["sha256"] for x in qa.query("SELECT id, sha256 FROM snapshots WHERE kind='source_revision'")}
    assert all(after.get(x["id"]) == x["sha256"] for x in src_before), "旧来源快照保留为历史"
    # 修正后可继续
    r = t1.run("resume")
    assert r.rc in (0, 3), r.describe()
    assert t1.api_ok("stage", "status", "--story", story).json["statuses"]["req_gate"] == "completed"


# ===========================================================================
# AC-083 输入变化使传递下游过期
# ===========================================================================

@pytest.mark.ac("AC-083")
@pytest.mark.layer("offline")
def test_ac083_design_revision_stales_execution_report_exit(qa, tmp_path):
    sh.drive_full(qa)
    story = sh.story_id(qa)
    before = sh.stage_statuses(qa, story)
    assert before["test_execution"] == before["test_exit"] == "completed"
    d1 = sh.latest_snapshot(qa, "case_design")
    snaps_before = {r["id"]: r["sha256"] for r in qa.query("SELECT id, sha256 FROM snapshots")}
    events_before = sh.last_seq(qa)
    decisions = qa.query("SELECT id, subjects, result FROM decisions")
    on_d1 = [d["id"] for d in decisions if d1["id"] in d["subjects"]]
    assert on_d1, "前置条件：存在绑定 D1 的批准"
    assert all(qa.api_ok("decision", "verify", "--id", i).json["valid"] for i in on_d1)
    exit_dec = [d["id"] for d in decisions if '"EXT-' in d["subjects"] and d["result"] == "approve"]
    assert len(exit_dec) == 1

    d2_payload = json.loads(json.dumps(d1["payload"]))
    d2_payload["cases"][0]["title"] += "（修订）"
    f = write_json(tmp_path / "d2.json", d2_payload)
    no_parent = qa.api("artifact", "submit", "--story", story, "--kind", "case_design", "--file", str(f))
    sh.assert_rejected(no_parent, {"REVISION_CONFLICT"})
    d2 = qa.api_ok("artifact", "submit", "--story", story, "--kind", "case_design", "--parent", d1["id"],
                   "--file", str(f)).result["artifact_id"]

    st = sh.stage_statuses(qa, story)
    assert st["test_execution"] == "stale", st   # E1
    assert st["test_exit"] == "stale", st        # R1/X1
    assert st["case_design"] != "completed" or st["case_qc"] != "completed"
    view = qa.ok("status").result
    assert view["verdict"] != "pass"
    vst = {s["stage"]: s["status"] for s in view["stages"]}
    assert vst["test_execution"] == "stale" and vst["test_exit"] == "stale"
    assert "测试准出" not in view["summary"]["have"] and "测试执行" not in view["summary"]["have"]

    # 历史不删除
    after = {r["id"]: r["sha256"] for r in qa.query("SELECT id, sha256 FROM snapshots")}
    assert all(after.get(k) == v for k, v in snaps_before.items()), "历史快照被删除或改写"
    assert sh.count(qa, "events", "seq<=?", (events_before,)) == events_before
    assert qa.api_ok("artifact", "show", "--id", d1["id"]).json["current"] is False
    for kind in ("test_report", "exit_report", "case_attempt"):
        assert sh.count(qa, "snapshots", "kind=?", (kind,)) >= 1

    # 审批不自动扩展到 D2
    for i in on_d1:
        v = qa.api_ok("decision", "verify", "--id", i).json
        assert v["valid"] is False and v["reasons"], v
    assert qa.api_ok("decision", "verify", "--id", exit_dec[0]).json["valid"] is False
    assert [d for d in qa.query("SELECT id, subjects FROM decisions") if d2 in d["subjects"]] == []
    assert sh.consistency_problems(qa) == []


@pytest.mark.ac("AC-083")
@pytest.mark.layer("offline")
def test_ac083_strong_rule_change_stales_conclusions(qa, tmp_path):
    """更新强依赖规则（执行策略）后，既有结论的当前适用性过期，历史保留。"""
    sh.drive_full(qa)
    story = sh.story_id(qa)
    snaps_before = {r["id"]: r["sha256"] for r in qa.query("SELECT id, sha256 FROM snapshots")}
    cfg = sh.read_config(qa)
    cfg["rules"]["policy"]["required_regression"] = ["C1", "C2", "C4"]
    r = sh.apply_config(qa, cfg, tmp_path, "lab-demo")
    assert r.rc == 0 and r.json["applied"] is True, r.describe()
    st = sh.stage_statuses(qa, story)
    for stage in ("test_execution", "test_exit"):
        assert st[stage] == "stale", st
    assert qa.ok("status").result["verdict"] != "pass"
    after = {r["id"]: r["sha256"] for r in qa.query("SELECT id, sha256 FROM snapshots")}
    assert all(after.get(k) == v for k, v in snaps_before.items())


# ===========================================================================
# AC-084 依赖组件缺失或停用不算成功
# ===========================================================================

@pytest.mark.ac("AC-084")
@pytest.mark.layer("offline")
def test_ac084_disabled_rules_component_blocks_stage(qa):
    """登记的规则组件被关闭（配置文件被改为未知规则来源）：阶段明确受阻，没有完成事件。"""
    qa.ok("demo")
    story = sh.story_id(qa)
    assert sh.stage_statuses(qa, story)["req_gate"] == "completed"
    cfg_path = qa.workspace() / "config" / "qa-flow.json"
    original = cfg_path.read_text(encoding="utf-8")
    cfg = json.loads(original)
    cfg["rules"]["kind"] = "disabled"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    seq0 = sh.last_seq(qa)
    r = qa.api("stage", "evaluate", "--story", story, "--stage", "req_gate")
    if r.rc == 0:
        # 退出码 0 不是门禁通过依据：必须看回执里的阶段状态
        assert r.result["status"] == "blocked" and r.result["verdict"] == "indeterminate", r.describe()
        assert r.result["reasons"], "受阻必须给出原因"
    else:
        sh.assert_rejected(r, UNAVAILABLE_CODES)
    assert not sh.events_after(qa, seq0, "stage_completed")
    st = qa.run("status")
    if st.rc == 0:
        assert {s["stage"]: s["status"] for s in st.result["stages"]}["req_gate"] != "completed"
        assert st.result["verdict"] != "pass"
    else:
        sh.assert_rejected(st, UNAVAILABLE_CODES)
    doctor = qa.ok("admin", "doctor").json
    assert {c["item"]: c["status"] for c in doctor["checks"]}["config"] == "invalid"
    # 修正后可继续
    cfg_path.write_text(original, encoding="utf-8")
    ok = qa.api_ok("stage", "evaluate", "--story", story, "--stage", "req_gate")
    assert ok.result["status"] == "completed"


@pytest.mark.ac("AC-084")
@pytest.mark.layer("offline")
def test_ac084_unavailable_validator_reported_explicitly(qa, tmp_path):
    """规则校验器不可用（冻结源码目录不可访问）：请求完成阶段时返回明确的 unavailable/blocked。"""
    p = sh.legacy_project(qa, tmp_path)
    story = p["story"]
    moved = tmp_path / "legacy-moved"
    shutil.move(str(p["legacy_root"]), str(moved))
    seq0 = sh.last_seq(qa)
    r = qa.api("stage", "evaluate", "--story", story, "--stage", "req_gate")
    problems = []
    if r.rc == 0:
        res = r.result
        if res["status"] == "completed" or res["verdict"] == "pass":
            problems.append("校验器不可用却得到完成/通过")
        explicit = res["status"] in ("blocked", "unavailable") or any(
            k in x for x in res["reasons"] for k in ("不可用", "unavailable", "不可访问"))
        if not explicit:
            problems.append(f"没有明确的 unavailable/blocked：status={res['status']} reasons={res['reasons']}")
    else:
        problems += sh.rejection_problems(r, UNAVAILABLE_CODES)
    if sh.events_after(qa, seq0, "stage_completed"):
        problems.append("产生了 stage_completed 事件")
    if sh.stage_statuses(qa, story)["req_gate"] == "completed":
        problems.append("阶段仍为 completed")
    if p["frozen_marker"].read_text(encoding="utf-8").strip().splitlines() != ["frozen"]:
        problems.append("校验器不可用期间仍有脚本被执行")
    assert not problems, "；".join(problems) + "\n" + r.describe()
    # 修正后可继续
    shutil.move(str(moved), str(p["legacy_root"]))
    ok = qa.api_ok("stage", "evaluate", "--story", story, "--stage", "req_gate")
    assert ok.result["status"] == "completed"


@pytest.mark.ac("AC-084")
@pytest.mark.layer("offline")
def test_ac084_removed_platform_adapter_blocks_progress(qa, tmp_path):
    """缺陷平台连接器被停用：继续推进时明确报能力不可用，不产生完成事件，也不写平台。"""
    story, ds = sh.proposed_defects_workspace(qa)
    cfg = sh.read_config(qa)
    cfg["adapters"] = [a for a in cfg["adapters"] if a["kind"] != "issue"]
    assert sh.apply_config(qa, cfg, tmp_path, "lab-demo").json["applied"] is True
    seq0, actions0 = sh.last_seq(qa), sh.count(qa, "actions")
    r = qa.api("defect", "create", "--story", story, "--defect", ds[0]["id"], "--dispatch")
    sh.assert_rejected(r, {"CAPABILITY_UNAVAILABLE", "DEPENDENCY_UNAVAILABLE", "UNSUPPORTED_CAPABILITY"})
    resumed = qa.run("resume")
    if resumed.rc != 0:
        sh.assert_rejected(resumed, {"CAPABILITY_UNAVAILABLE", "DEPENDENCY_UNAVAILABLE", "UNSUPPORTED_CAPABILITY"})
    # 配置变更会让上游阶段重新判定；这里只要求依赖该组件的阶段没有完成事件
    assert not [e for e in sh.events_after(qa, seq0, "stage_completed")
                if e["object_id"].endswith((":test_execution", ":test_exit"))]
    assert sh.count(qa, "actions", "kind LIKE 'defect.%'") == 0 and sh.count(qa, "actions") == actions0
    assert sh.platform_calls(qa) == []
    assert all(d["status"] == "proposed" for d in sh.defects(qa))
    st = sh.stage_statuses(qa, story)
    assert st["test_execution"] != "completed" and st["test_exit"] != "completed"


# ===========================================================================
# AC-085 并发提交只成功一次
# ===========================================================================

@pytest.mark.ac("AC-085")
@pytest.mark.layer("offline")
def test_ac085_same_revision_stage_evaluation_commits_once(qa):
    qa.ok("demo")
    story = sh.story_id(qa)
    oid = f"stage:{story}:req_gate"
    rev = qa.query("SELECT revision FROM aggregates WHERE object_id=?", (oid,))[0]["revision"]
    seq0 = sh.last_seq(qa)
    cmds = [("api", "stage", "evaluate", "--story", story, "--stage", "req_gate", "--expected-revision", str(rev),
             "--command-id", f"CMD-RACE-{i}") for i in (1, 2)]
    results = sh.race(qa, *cmds)
    winners = [r for r in results if r.rc == 0]
    losers = [r for r in results if r.rc != 0]
    assert len(winners) == 1 and len(losers) == 1, "\n".join(r.describe() for r in results)
    assert winners[0].json["committed"] is True
    sh.assert_rejected(losers[0], {"REVISION_CONFLICT"})
    assert losers[0].rc == 3 and oid in losers[0].errors[0]["related_refs"]
    # 没有丢失更新：对象只前进一个修订，且只有胜者的事件
    assert qa.query("SELECT revision FROM aggregates WHERE object_id=?", (oid,))[0]["revision"] == rev + 1
    obj_events = [e for e in sh.events_after(qa, seq0) if e["object_id"] == oid]
    assert [(e["prev_rev"], e["new_rev"]) for e in obj_events] == [(rev, rev + 1)]
    win_id, lose_id = winners[0].json["command_id"], [c for c in ("CMD-RACE-1", "CMD-RACE-2")
                                                      if c != winners[0].json["command_id"]][0]
    assert obj_events[0]["command_id"] == win_id
    assert sh.count(qa, "commands", "command_id=?", (win_id,)) == 1
    assert sh.count(qa, "commands", "command_id=?", (lose_id,)) == 0
    assert sh.count(qa, "events", "command_id=?", (lose_id,)) == 0, "失败方不得留下业务事件"
    rejected = [e for e in sh.events_after(qa, seq0, "command_rejected") if lose_id in e["payload"]]
    assert len(rejected) == 1 and "REVISION_CONFLICT" in rejected[0]["payload"]
    assert sh.consistency_problems(qa) == []
    # 刷新修订后可继续
    again = qa.api_ok("stage", "evaluate", "--story", story, "--stage", "req_gate", "--expected-revision", str(rev + 1))
    assert again.json["committed"] is True


@pytest.mark.ac("AC-085")
@pytest.mark.layer("offline")
def test_ac085_same_revision_answers_commit_once(qa):
    """两个进程基于同一修订给出不同答复：一个成功，另一个 REVISION_CONFLICT，不能静默丢弃。"""
    qa.ok("demo")
    story = sh.story_id(qa)
    mine = flow.view(qa)["reviews"]["mine"]
    assert mine and {o["key"] for o in mine[0]["options"]} >= {"A", "B"}
    handle = mine[0]["handle"]
    item = [r for r in qa.query("SELECT object_id, revision, state FROM aggregates WHERE kind='review_item'")
            if json.loads(r["state"])["handle"] == handle][0]
    seq0, dec0 = sh.last_seq(qa), sh.count(qa, "decisions")
    cmds = [("api", "clarification", "answer", "--story", story, "--handle", handle, "--option", opt, "--role", "tester",
             "--expected-revision", str(item["revision"]), "--command-id", f"CMD-ANS-{opt}") for opt in ("A", "B")]
    results = sh.race(qa, *cmds)
    desc = "\n".join(r.describe() for r in results)
    # 独立核对：只有一条决定、对象只前进一个修订、事件连续
    assert sh.count(qa, "decisions") == dec0 + 1, desc
    assert qa.query("SELECT revision FROM aggregates WHERE object_id=?", (item["object_id"],))[0]["revision"] == \
        item["revision"] + 1
    assert sh.consistency_problems(qa) == []
    problems = []
    winners = [r for r in results if r.rc == 0]
    losers = [r for r in results if r.rc != 0]
    if len(winners) != 1:
        problems.append(f"应恰有一个进程成功，实际 {len(winners)} 个以退出码 0 返回 committed")
    for r in losers:
        problems += sh.rejection_problems(r, {"REVISION_CONFLICT"})
    recorded = json.loads(qa.query("SELECT canonical FROM snapshots WHERE kind='decision' ORDER BY rowid DESC LIMIT 1")[0][
        "canonical"])["payload"]["answer"]["option"]
    for r, opt in zip(results, ("A", "B")):
        if r.rc == 0 and opt != recorded:
            problems.append(f"选项 {opt} 的提交返回成功，但登记的决定是 {recorded}（更新被静默丢弃）")
    assert not problems, "；".join(problems) + "\n" + desc
    assert not [e for e in sh.events_after(qa, seq0) if e["command_id"] and e["command_id"] not in (
        winners[0].json["command_id"],)]


@pytest.mark.ac("AC-085")
@pytest.mark.layer("offline")
def test_ac085_concurrent_independent_writes_keep_event_sequence(qa, tmp_path):
    """多个进程同时提交互不冲突的写命令：全部提交，事件序号连续，没有丢失。"""
    qa.ok("demo")
    story = sh.story_id(qa)
    files = []
    for i in range(4):
        f = tmp_path / f"src-{i}.md"
        f.write_text(f"# 来源 {i}\n内容 {i}\n", encoding="utf-8")
        files.append(f)
    snaps0 = sh.count(qa, "snapshots", "kind='source_revision'")
    results = sh.race(qa, *[("api", "source", "import", "--story", story, "--uri", str(f), "--command-id", f"CMD-IMP-{i}")
                            for i, f in enumerate(files)])
    assert all(r.rc == 0 and r.json["committed"] for r in results), "\n".join(r.describe() for r in results)
    assert len({r.result["source_ref"] for r in results}) == 4
    assert sh.count(qa, "snapshots", "kind='source_revision'") == snaps0 + 4
    assert sh.count(qa, "commands", "command_id LIKE 'CMD-IMP-%'") == 4
    assert sorted(r.json["revision"] for r in results) == sorted({r.json["revision"] for r in results})
    assert sh.consistency_problems(qa) == []


# ===========================================================================
# AC-086 提交各断点崩溃后恢复
# ===========================================================================

def _op_source_import(qa, story, tmp, cid):
    f = tmp / "crash-src.md"
    f.write_text("# 崩溃点来源\n内容\n", encoding="utf-8")
    return ("api", "source", "import", "--story", story, "--uri", str(f), "--command-id", cid)


def _op_answer(qa, story, tmp, cid):
    handle = flow.view(qa)["reviews"]["mine"][0]["handle"]
    return ("api", "clarification", "answer", "--story", story, "--handle", handle, "--option", "A", "--role", "tester",
            "--command-id", cid)


def _op_stage_evaluate(qa, story, tmp, cid):
    rev = qa.query("SELECT revision FROM aggregates WHERE object_id=?", (f"stage:{story}:req_gate",))[0]["revision"]
    return ("api", "stage", "evaluate", "--story", story, "--stage", "req_gate", "--expected-revision", str(rev),
            "--command-id", cid)


def _op_artifact_submit(qa, story, tmp, cid):
    rows = qa.query("SELECT id, canonical FROM snapshots WHERE kind='source_revision'")
    tech = [r["id"] for r in rows if json.loads(r["canonical"])["payload"]["uri"].endswith("refund-tech.md")][0]
    claim = {"text": "技术方案写明 refunds 表的幂等键唯一", "claim_type": "fact", "scope": "document",
             "review_state": "pending",
             "evidence_refs": [{"source_ref": tech, "quote": "refunds 表：idempotency_key 唯一约束。"}]}
    f = write_json(tmp / "claim.json", claim)
    return ("api", "artifact", "submit", "--story", story, "--kind", "claim", "--name", "crash", "--file", str(f),
            "--command-id", cid)


CRASH_OPS = {"source.import": _op_source_import, "clarification.answer": _op_answer,
             "stage.evaluate": _op_stage_evaluate, "artifact.submit": _op_artifact_submit}


@pytest.mark.ac("AC-086")
@pytest.mark.layer("offline")
@pytest.mark.parametrize("name", sorted(CRASH_OPS))
def test_ac086_crash_before_commit_leaves_no_partial_state(qa, tmp_path, name):
    """事件已写入、提交前进程终止（§8：事务写事件后、提交前退出；证据落盘后未提交）。"""
    qa.ok("demo")
    story = sh.story_id(qa)
    ws = qa.workspace()
    cid = "CMD-CRASH-BEFORE"
    args = CRASH_OPS[name](qa, story, tmp_path, cid)
    before, files0, tmp0 = sh.dump(qa), sh.evidence_disk_files(ws), sh.tmp_files(ws)
    r = qa.test_mode(QA_FLOW_CRASH_AT=f"before_commit:{name}").run(*args)
    assert r.rc == sh.CRASH_EXIT and r.out == "", r.describe()

    info = sh.recover(qa)  # 重启并运行恢复检查
    assert info["integrity"] == "ok" and str(info["journal_mode"]).lower() == "delete"
    assert sh.dump(qa) == before, "提交前崩溃后数据库出现了半完成状态"
    assert sh.count(qa, "commands", "command_id=?", (cid,)) == 0
    assert sh.consistency_problems(qa) == []
    # 外部遗留文件可识别：只可能是未被数据库引用的内容寻址证据或 tmp/ 下的临时文件
    leftovers = sh.evidence_disk_files(ws) - files0
    assert leftovers <= sh.unreferenced_evidence_files(qa)
    for rel in leftovers:
        assert (ws / rel).name == sha256_file(ws / rel), "遗留证据文件应可按内容摘要识别"
    if name == "source.import":
        assert len(leftovers) == 1, "证据落盘后崩溃应留下一份孤立证据"
    assert all(p.startswith("tmp/") for p in sh.tmp_files(ws) - tmp0)
    status = qa.ok("status")
    assert status.json["revision"] == before["events"][-1]["seq"]

    # 同一命令重新提交：正常提交一次，不是重放
    again = qa.run(*args)
    assert again.rc == 0 and again.json["committed"] is True and not again.json.get("replayed"), again.describe()
    assert sh.count(qa, "commands", "command_id=?", (cid,)) == 1
    assert sh.consistency_problems(qa) == []
    assert sh.unreferenced_evidence_files(qa) == set()
    assert not (ws / "qa.db-journal").exists(), "下一次写事务提交后不应残留回滚日志"


@pytest.mark.ac("AC-086")
@pytest.mark.layer("offline")
@pytest.mark.parametrize("name", sorted(CRASH_OPS))
def test_ac086_crash_after_commit_replays_original_result(qa, tmp_path, name):
    """提交成功后、回执输出前进程终止（§8：同 command_id 重放返回原提交结果）。"""
    qa.ok("demo")
    story = sh.story_id(qa)
    cid = "CMD-CRASH-AFTER"
    args = CRASH_OPS[name](qa, story, tmp_path, cid)
    before = sh.dump(qa)
    r = qa.test_mode(QA_FLOW_CRASH_AT=f"after_commit:{name}").run(*args)
    assert r.rc == sh.CRASH_EXIT and r.out == "", r.describe()

    info = sh.recover(qa)
    assert info["integrity"] == "ok"
    committed = sh.dump(qa)
    rows = [c for c in committed["commands"] if c["command_id"] == cid]
    assert len(rows) == 1, "提交后应恰有一条有效决定（命令记录）"
    stored = json.loads(rows[0]["response"])
    assert stored["committed"] is True and stored["command_id"] == cid
    assert len(committed["events"]) > len(before["events"])
    assert {e["command_id"] for e in committed["events"][len(before["events"]):]} == {cid}
    assert sh.consistency_problems(qa) == []
    assert sh.unreferenced_evidence_files(qa) == set()

    replay = qa.run(*args)
    assert replay.rc == 0, replay.describe()
    assert replay.json["replayed"] is True
    assert replay.json["result"] == stored["result"] and replay.json["revision"] == stored["revision"]
    assert sh.dump(qa) == committed, "重放不得产生新的事件、快照或决定"

    # 同一命令 ID 换了载荷必须被拒绝
    other = list(args)
    if name == "source.import":
        g = tmp_path / "other.md"
        g.write_text("# 另一份\n", encoding="utf-8")
        other[other.index("--uri") + 1] = str(g)
    elif name == "clarification.answer":
        other[other.index("--option") + 1] = "B"
    elif name == "stage.evaluate":
        other[other.index("--stage") + 1] = "tech_gate"
    else:
        other[other.index("--name") + 1] = "other-name"
    reused = qa.run(*other)
    sh.assert_rejected(reused, {"COMMAND_ID_REUSED"})
    assert reused.rc == 2
    assert sh.dump(qa) == committed


@pytest.mark.ac("AC-086")
@pytest.mark.layer("offline")
@pytest.mark.parametrize("point", ["before_commit:defect.propose_batch", "after_commit:defect.propose_batch",
                                   "before_commit:script.register_lab", "after_commit:stage.evaluate_all"])
def test_ac086_flow_continues_after_crash_without_duplicates(qa, point):
    """流程推进中途崩溃后，重启续做到结束：不重复登记对象，引用一致。"""
    sh.drive_until_crash(qa, point)
    info = sh.recover(qa)
    assert info["integrity"] == "ok"
    assert sh.consistency_problems(qa) == []
    sh.drive_full(qa)
    ds = sh.defects(qa)
    assert sorted(d["tc_ref"] for d in ds) == ["C1", "C3"], "每个失败用例恰有一条缺陷"
    assert all(d["status"] == "closed" for d in ds)
    items = sh.platform_rows(qa, "SELECT id, status FROM items ORDER BY id")
    assert [i["id"] for i in items] == sorted(d["external_id"] for d in ds) and len(items) == 2
    assert len(sh.platform_calls(qa, op="create", outcome="ok")) == 2
    assert sh.count(qa, "snapshots", "kind='script_revision'") == 1
    assert sh.consistency_problems(qa) == []
    assert sh.unreferenced_evidence_files(qa) == set()


@pytest.mark.ac("AC-086")
@pytest.mark.layer("offline")
@pytest.mark.parametrize("stage", ["file_prepare", "transaction_begin"])
def test_ac086_crash_injection_available_for_early_breakpoints(qa, tmp_path, stage):
    """场景要求在“文件准备”“事务开始”两个断点注入进程终止；产品需要提供对应的受控故障点。"""
    qa.ok("demo")
    story = sh.story_id(qa)
    candidates = {
        "file_prepare": ["evidence_prepare", "before_evidence", "during_evidence", "evidence_tmp", "file_prepare",
                         "after_evidence", "before_evidence:source.import", "after_evidence:source.import"],
        "transaction_begin": ["after_begin", "tx_begin", "transaction_begin", "after_begin:source.import",
                              "tx_begin:source.import", "before_handler:source.import"],
    }[stage]
    hit = None
    for i, point in enumerate(candidates):
        f = tmp_path / f"probe-{i}.md"
        f.write_text(f"# 探测 {i}\n", encoding="utf-8")
        r = qa.test_mode(QA_FLOW_CRASH_AT=point).api("source", "import", "--story", story, "--uri", str(f))
        if r.rc == sh.CRASH_EXIT:
            hit = point
            break
    assert hit, (f"产品没有为“{stage}”断点提供可注入的进程终止点（tests/README.md 与 faults.py 只登记了 "
                 f"before_commit/after_commit/before_send/after_send）；已尝试 {candidates}")


@needs_chmod
@pytest.mark.ac("AC-086")
@pytest.mark.layer("offline")
@pytest.mark.parametrize("where", ["evidence/raw", "tmp"])
def test_ac086_evidence_write_failure_is_explicit_and_recoverable(qa, tmp_path, where):
    """§8：磁盘写失败（证据文件准备阶段失败）→ 明确错误、停止推进；修复后可继续。"""
    qa.ok("demo")
    story = sh.story_id(qa)
    ws = qa.workspace()
    f = tmp_path / "write-fail.md"
    f.write_text("# 写失败来源\n新的内容\n", encoding="utf-8")
    before = sh.dump(qa)
    target = ws / where
    mode = stat.S_IMODE(target.stat().st_mode)
    os.chmod(target, 0o500)
    try:
        r = qa.api("source", "import", "--story", story, "--uri", str(f))
    finally:
        os.chmod(target, mode)
    problems = sh.rejection_problems(r, {"IO_ERROR", "DEPENDENCY_UNAVAILABLE", "WORKSPACE_INVALID"})
    after = sh.dump(qa)
    for t in ("snapshots", "aggregates", "evidence", "commands"):
        if after[t] != before[t]:
            problems.append(f"写失败后 {t} 表发生变化")
    problems += sh.consistency_problems(qa)
    assert not problems, "；".join(problems) + "\n" + r.describe()
    ok = qa.api_ok("source", "import", "--story", story, "--uri", str(f))
    assert ok.result["read_status"] == "available"


# ===========================================================================
# AC-089 同命令/同请求重复提交无重复副作用
# ===========================================================================

@pytest.mark.ac("AC-089")
@pytest.mark.layer("offline")
def test_ac089_same_command_id_same_payload_is_read_from_record(qa):
    story, ds = sh.proposed_defects_workspace(qa)
    first = qa.api_ok("defect", "create", "--story", story, "--defect", ds[0]["id"], "--command-id", "CMD-DUP-1")
    action = first.json["action"]
    state = sh.dump(qa)
    again = qa.api_ok("defect", "create", "--story", story, "--defect", ds[0]["id"], "--command-id", "CMD-DUP-1")
    assert again.json["action"] == action, "重复命令的响应必须来自已登记的命令结果"
    assert sh.dump(qa) == state, "重复命令不得产生新的事件或对象"
    stored = json.loads(qa.query("SELECT response FROM commands WHERE command_id='CMD-DUP-1'")[0]["response"])
    assert stored["result"]["action"] == action

    # 不同载荷使用同一命令 ID 被拒
    reused = qa.api("defect", "create", "--story", story, "--defect", ds[1]["id"], "--command-id", "CMD-DUP-1")
    sh.assert_rejected(reused, {"COMMAND_ID_REUSED"})
    assert reused.rc == 2 and reused.errors[0]["path"] == "command_id"
    assert sh.dump(qa) == state
    # 不同命令 ID 的同一逻辑操作：仍是同一动作，不产生第二个
    other = qa.api_ok("defect", "create", "--story", story, "--defect", ds[0]["id"], "--command-id", "CMD-DUP-2")
    assert other.json["action"]["id"] == action["id"]
    assert sh.count(qa, "actions", "kind='defect.create'") == 1
    assert sh.count(qa, "snapshots", "kind='action_plan' AND id=?", (action["plan_ref"],)) == 1
    assert sh.platform_calls(qa) == [], "只登记计划时不得有外部调用"

    # 带回执的命令：重放标记 replayed，结果相同
    f = qa.root.parent / "replay.md"
    f.write_text("# 重放来源\n", encoding="utf-8")
    a = qa.api_ok("source", "import", "--story", story, "--uri", str(f), "--command-id", "CMD-DUP-3")
    b = qa.api_ok("source", "import", "--story", story, "--uri", str(f), "--command-id", "CMD-DUP-3")
    assert not a.json.get("replayed") and b.json["replayed"] is True
    assert b.json["result"] == a.json["result"] and b.json["revision"] == a.json["revision"]


@pytest.mark.ac("AC-089")
@pytest.mark.layer("offline")
def test_ac089_replay_after_success_sends_nothing(qa):
    story, ds = sh.proposed_defects_workspace(qa)
    aid = sh.plan_defect_create(qa, story, ds[0]["id"])["id"]
    first = qa.api_ok("action", "dispatch", "--id", aid).json
    assert first["state"] == "succeeded" and first["remote_object_id"]
    ledger = sh.platform_calls(qa)
    assert len([c for c in ledger if c["op"] == "create"]) == 1
    state = sh.dump(qa)
    for _ in range(2):
        again = qa.api_ok("action", "dispatch", "--id", aid).json
        assert again["state"] == "succeeded" and again.get("replayed") is True
    assert sh.platform_calls(qa) == ledger, "成功后的重放不得产生任何外部调用"
    assert sh.dump(qa) == state
    items = sh.platform_rows(qa, "SELECT id, idem_key FROM items")
    row = sh.action_row(qa, aid)
    assert items == [{"id": first["remote_object_id"], "idem_key": row["idempotency_key"]}]
    assert row["attempts"] == 1 and row["remote_object_id"] == first["remote_object_id"]
    assert sh.defects(qa)[0]["external_id"] == first["remote_object_id"]
    # 重新规划同一逻辑操作也不会再建一单
    replanned = sh.plan_defect_create(qa, story, ds[0]["id"])
    assert replanned["id"] == aid and replanned["state"] == "succeeded"
    assert len(sh.platform_calls(qa, op="create")) == 1


@pytest.mark.ac("AC-089")
@pytest.mark.layer("offline")
def test_ac089_replay_before_send_results_in_single_remote_operation(qa):
    """发送前进程终止后重放：先核对再发送，远端逻辑操作恰好一次。"""
    story, ds = sh.proposed_defects_workspace(qa)
    aid = sh.plan_defect_create(qa, story, ds[0]["id"])["id"]
    crashed = qa.test_mode(QA_FLOW_CRASH_AT="before_send").api("action", "dispatch", "--id", aid)
    assert crashed.rc == sh.CRASH_EXIT
    sh.recover(qa)
    assert sh.action_row(qa, aid)["state"] == "running"
    assert sh.platform_calls(qa) == [], "发送前崩溃时远端不应有调用"

    busy = qa.api("action", "dispatch", "--id", aid)   # 租约未到期：不能并行重发
    sh.assert_rejected(busy, {"PRECONDITION_FAILED", "OUTCOME_UNKNOWN"})
    assert sh.platform_calls(qa) == []

    later = qa.test_mode(QA_FLOW_TEST_CLOCK=sh.utc_after(1))  # 租约 3 分钟，已过期
    took = later.api("action", "dispatch", "--id", aid)
    assert took.rc in (0, 5), took.describe()
    assert sh.action_row(qa, aid)["state"] == "unknown", "租约过期后不能直接重发"
    assert sh.platform_calls(qa, op="create") == []
    blind = later.api("action", "dispatch", "--id", aid)
    sh.assert_rejected(blind, {"OUTCOME_UNKNOWN"})
    assert blind.rc == 5 and sh.platform_calls(qa, op="create") == []

    rec = later.api_ok("action", "reconcile", "--id", aid).json
    assert len(sh.platform_calls(qa, op="query")) == 1, "核对必须先查询远端"
    assert rec["state"] != "succeeded" and sh.action_row(qa, aid)["remote_object_id"] is None
    done = later.api_ok("action", "dispatch", "--id", aid).json
    assert done["state"] == "succeeded"
    assert len(sh.platform_calls(qa, op="create", outcome="ok")) == 1
    assert len(sh.platform_rows(qa, "SELECT id FROM items")) == 1
    # 续做到结束：每条缺陷恰有一个平台对象
    sh.drive_full(qa)
    assert len(sh.platform_rows(qa, "SELECT id FROM items")) == 2
    assert len(sh.platform_calls(qa, op="create", outcome="ok")) == 2
    assert sorted(d["external_id"] for d in sh.defects(qa)) == ["LAB-0001", "LAB-0002"]


# ===========================================================================
# AC-090 远端成功但响应丢失先核对
# ===========================================================================

def _lost_response_action(qa: QA) -> tuple[str, str, list[dict]]:
    story, ds = sh.proposed_defects_workspace(qa)
    aid = sh.plan_defect_create(qa, story, ds[0]["id"])["id"]
    qa.test_mode().api_ok("lab", "fault", "--name", "create_succeeded_response_lost")
    r = qa.api("action", "dispatch", "--id", aid)
    assert r.rc in (0, 5), r.describe()
    row = sh.action_row(qa, aid)
    assert row["state"] == "unknown" and row["remote_object_id"] is None, row
    # 前置条件：远端确已创建，本地尚未关联
    items = sh.platform_rows(qa, "SELECT id, idem_key FROM items")
    assert len(items) == 1 and items[0]["idem_key"] == row["idempotency_key"]
    assert sh.defects(qa)[0]["external_id"] is None and sh.defects(qa)[0]["status"] == "proposed"
    return story, aid, items


@pytest.mark.ac("AC-090")
@pytest.mark.layer("lab")
def test_ac090_fault_injection_requires_test_mode(qa):
    """故障注入接口在正式运行中不可用。"""
    sh.proposed_defects_workspace(qa)
    r = qa.api("lab", "fault", "--name", "create_succeeded_response_lost")
    assert r.rc != 0 and r.json["committed"] is False
    assert sh.platform_rows(qa, "SELECT * FROM faults") == []


@pytest.mark.ac("AC-090")
@pytest.mark.layer("lab")
def test_ac090_unknown_outcome_forbids_resend_then_reconciles(qa):
    story, aid, items = _lost_response_action(qa)
    view = qa.ok("status").result
    assert [u for u in view["unknown_actions"]], "状态视图必须显示结果未知的操作"
    assert view["defects"][0]["external_id"] is None

    # 重启 worker 后尝试重发：禁止
    for _ in range(2):
        resend = qa.api("action", "dispatch", "--id", aid)
        sh.assert_rejected(resend, {"OUTCOME_UNKNOWN"})
        assert resend.rc == 5 and resend.json["status"] == "unknown"
    assert len(sh.platform_calls(qa, op="create")) == 1, "未知状态下不得再次创建"
    assert sh.action_row(qa, aid)["state"] == "unknown"

    rec = qa.api_ok("action", "reconcile", "--id", aid).json
    assert rec["state"] == "succeeded" and rec["remote_object_id"] == items[0]["id"]
    assert len(sh.platform_calls(qa, op="query")) == 1 and len(sh.platform_calls(qa, op="create")) == 1
    row = sh.action_row(qa, aid)
    assert row["state"] == "succeeded" and row["remote_object_id"] == items[0]["id"] and row["attempts"] == 1
    d = sh.defects(qa)[0]
    assert d["external_id"] == items[0]["id"] and d["status"] == "created"
    assert sh.platform_rows(qa, "SELECT id, idem_key FROM items") == items, "只关联一个远端对象"
    types = [e["event_type"] for e in qa.query("SELECT event_type FROM events WHERE object_id=? ORDER BY seq", (aid,))]
    assert types == ["action_planned", "action_ready", "action_running", "action_unknown", "action_succeeded"]
    assert qa.api_ok("action", "dispatch", "--id", aid).json.get("replayed") is True
    assert len(sh.platform_calls(qa, op="create")) == 1
    assert sh.consistency_problems(qa) == []


@pytest.mark.ac("AC-090")
@pytest.mark.layer("lab")
@pytest.mark.parametrize("entry", ["worker_tick", "resume"])
def test_ac090_worker_restart_reconciles_before_any_send(qa, entry):
    story, aid, items = _lost_response_action(qa)
    if entry == "worker_tick":
        rep = qa.api_ok("worker", "tick", "--once").json
        assert [x for x in rep["reconciled"] if x["action_id"] == aid and x["state"] == "succeeded"], rep
        assert aid not in [x.get("action_id") for x in rep["dispatched"]]
    else:
        r = qa.run("resume")
        assert r.rc in (0, 3), r.describe()
    row = sh.action_row(qa, aid)
    assert row["state"] == "succeeded" and row["remote_object_id"] == items[0]["id"]
    creates = [c for c in sh.platform_calls(qa, op="create") if c["target"] == row["idempotency_key"]]
    assert len(creates) == 1, "核对后不得对同一逻辑操作再次创建"
    assert len([i for i in sh.platform_rows(qa, "SELECT idem_key FROM items") if i["idem_key"] == row["idempotency_key"]]) == 1
    sh.drive_full(qa)
    assert len(sh.platform_rows(qa, "SELECT id FROM items")) == 2
    assert sorted(d["external_id"] for d in sh.defects(qa)) == ["LAB-0001", "LAB-0002"]


@pytest.mark.ac("AC-090")
@pytest.mark.layer("lab")
def test_ac090_unresolvable_outcome_stays_unknown(qa):
    """远端不支持按幂等键回查（文档归档）：核对不能查明时保持 unknown，始终禁止重发。"""
    story, _ = sh.proposed_defects_workspace(qa)
    report = sh.latest_snapshot(qa, "entry_report")["id"]
    aid = sh.planned_archive(qa, story, report)
    assert sh.action_row(qa, aid)["state"] == "awaiting_authorization"
    sh.approve_broker_item(qa)
    crashed = qa.test_mode(QA_FLOW_CRASH_AT="after_send").api("action", "dispatch", "--id", aid)
    assert crashed.rc == sh.CRASH_EXIT, crashed.describe()
    sh.recover(qa)
    docs = sh.platform_rows(qa, "SELECT id, etag FROM docs")
    assert len(docs) == 1, "前置条件：远端已写入，本地没有回执"
    assert sh.action_row(qa, aid)["state"] == "running"

    later = qa.test_mode(QA_FLOW_TEST_CLOCK=sh.utc_after(1))
    took = later.api("action", "dispatch", "--id", aid)
    assert took.rc in (0, 5), took.describe()
    assert sh.action_row(qa, aid)["state"] == "unknown"
    for step in (("action", "reconcile", "--id", aid), ("worker", "tick", "--once"), ("action", "reconcile", "--id", aid)):
        r = later.api(*step)
        assert r.rc in (0, 5), r.describe()
        row = sh.action_row(qa, aid)
        assert row["state"] in ("unknown", "reconciling") and row["remote_object_id"] is None, (step, row)
        resend = later.api("action", "dispatch", "--id", aid)
        sh.assert_rejected(resend, {"OUTCOME_UNKNOWN"})
    assert sh.platform_rows(qa, "SELECT id, etag FROM docs") == docs, "不得重复创建文档"
    assert len(sh.platform_calls(qa, op="doc_create")) == 1
    assert sh.count(qa, "snapshots", "kind='archive_record'") == 0, "未查明前不得登记归档成功"
    assert qa.ok("status").result["unknown_actions"], "状态视图继续显示结果未知"


# ===========================================================================
# AC-107 租约过期与旧 worker 晚到
# ===========================================================================

@pytest.mark.ac("AC-107")
@pytest.mark.layer("lab")
def test_ac107_late_receipt_from_expired_lease_goes_to_reconcile(qa, tmp_path):
    story, ds = sh.proposed_defects_workspace(qa)
    aid = sh.plan_defect_create(qa, story, ds[0]["id"])["id"]
    w1 = sh.PausedDispatch(qa, aid, tmp_path / "barrier")       # W1 领取租约（代次 1），停在发送前屏障
    held = sh.action_row(qa, aid)
    assert held["state"] == "running" and held["lease_epoch"] == 1

    busy = qa.api("action", "dispatch", "--id", aid)             # 租约有效期内 W2 不能并行执行
    sh.assert_rejected(busy, {"PRECONDITION_FAILED"})
    w2 = qa.test_mode(QA_FLOW_TEST_CLOCK=sh.utc_after(1))        # 租约到期后 W2 接管
    took = w2.api("action", "dispatch", "--id", aid)
    assert took.rc in (0, 5), took.describe()
    assert sh.action_row(qa, aid)["state"] == "unknown"
    blind = w2.api("action", "dispatch", "--id", aid)
    sh.assert_rejected(blind, {"OUTCOME_UNKNOWN"})
    assert sh.platform_calls(qa, op="create") == [], "W2 不得盲目重发"
    before_late = sh.action_row(qa, aid)

    late = w1.release()                                          # W1 此时才发送并晚到
    assert late.rc == 0 and late.json.get("late_receipt") is True, late.describe()
    row = sh.action_row(qa, aid)
    assert row["state"] in ("reconciling", "unknown"), "过期租约的成功回执不能直接改写为成功"
    assert row["remote_object_id"] is None and row["lease_epoch"] == before_late["lease_epoch"]
    assert sh.defects(qa)[0]["external_id"] is None, "晚到回执不得直接关联缺陷"
    attempts = qa.query("SELECT lease_epoch, outcome, late FROM action_attempts WHERE action_id=?", (aid,))
    assert attempts == [{"lease_epoch": 1, "outcome": "late_succeeded", "late": 1}]
    assert len(sh.events_after(qa, 0, "late_receipt")) == 1
    resend = w2.api("action", "dispatch", "--id", aid)
    sh.assert_rejected(resend, {"OUTCOME_UNKNOWN"})

    rec = w2.api_ok("action", "reconcile", "--id", aid).json
    items = sh.platform_rows(qa, "SELECT id, idem_key FROM items")
    assert len(items) == 1 and items[0]["idem_key"] == row["idempotency_key"], "只存在一个远端对象"
    assert rec["state"] == "succeeded" and rec["remote_object_id"] == items[0]["id"]
    assert sh.defects(qa)[0]["external_id"] == items[0]["id"]
    assert len(sh.platform_calls(qa, op="create", outcome="ok")) == 1
    revs = [(e["prev_rev"], e["new_rev"]) for e in qa.query(
        "SELECT prev_rev, new_rev FROM events WHERE object_id=? AND new_rev IS NOT NULL ORDER BY seq", (aid,))]
    assert revs == [(i, i + 1) for i in range(len(revs))], "操作修订必须逐一递增，没有被覆盖"
    assert sh.consistency_problems(qa) == []


@pytest.mark.ac("AC-107")
@pytest.mark.layer("lab")
def test_ac107_late_w1_cannot_overwrite_w2_result(qa, tmp_path):
    """W2 核对（远端尚无对象）并重新执行成功后，W1 晚到：不得改写新修订，远端仍只有一个对象。"""
    story, ds = sh.proposed_defects_workspace(qa)
    aid = sh.plan_defect_create(qa, story, ds[0]["id"])["id"]
    w1 = sh.PausedDispatch(qa, aid, tmp_path / "barrier")
    w2 = qa.test_mode(QA_FLOW_TEST_CLOCK=sh.utc_after(1))
    assert w2.api("action", "dispatch", "--id", aid).rc in (0, 5)
    assert sh.action_row(qa, aid)["state"] == "unknown"
    rec = w2.api_ok("action", "reconcile", "--id", aid).json      # 先查询：远端确无对象
    assert len(sh.platform_calls(qa, op="query")) == 1 and sh.platform_calls(qa, op="create") == []
    assert rec["state"] != "succeeded"
    done = w2.api_ok("action", "dispatch", "--id", aid).json
    assert done["state"] == "succeeded"
    settled = sh.action_row(qa, aid)
    assert settled["lease_epoch"] == 2 and settled["remote_object_id"] == "LAB-0001"
    defect_before = sh.defects(qa)[0]

    late = w1.release()
    assert late.rc == 0 and late.json.get("late_receipt") is True, late.describe()
    assert sh.action_row(qa, aid) == settled, "旧租约的晚到结果改写了新修订"
    assert sh.defects(qa)[0] == defect_before
    assert sh.platform_rows(qa, "SELECT id FROM items") == [{"id": "LAB-0001"}]
    assert len(sh.platform_calls(qa, op="create", outcome="ok")) == 1, "远端逻辑创建只有一次"
    attempts = qa.query("SELECT lease_epoch, outcome, late FROM action_attempts WHERE action_id=? ORDER BY lease_epoch",
                        (aid,))
    assert attempts[0]["lease_epoch"] == 1 and attempts[0]["late"] == 1
    assert attempts[1] == {"lease_epoch": 2, "outcome": "succeeded", "late": 0}
    assert sh.consistency_problems(qa) == []


@pytest.mark.ac("AC-107")
@pytest.mark.layer("lab")
def test_ac107_w1_sent_and_never_returned(qa):
    """W1 已发送但未返回（发送后进程终止）；租约到期后 W2 不盲重发，核对后只关联一个远端对象。"""
    story, ds = sh.proposed_defects_workspace(qa)
    aid = sh.plan_defect_create(qa, story, ds[0]["id"])["id"]
    w1 = qa.test_mode(QA_FLOW_CRASH_AT="after_send").api("action", "dispatch", "--id", aid)
    assert w1.rc == sh.CRASH_EXIT
    sh.recover(qa)
    assert len(sh.platform_rows(qa, "SELECT id FROM items")) == 1, "前置条件：W1 已在远端创建"
    assert sh.action_row(qa, aid)["state"] == "running" and sh.defects(qa)[0]["external_id"] is None

    w2 = qa.test_mode(QA_FLOW_TEST_CLOCK=sh.utc_after(1))
    took = w2.api("action", "dispatch", "--id", aid)
    assert took.rc in (0, 5), took.describe()
    assert sh.action_row(qa, aid)["state"] == "unknown"
    sh.assert_rejected(w2.api("action", "dispatch", "--id", aid), {"OUTCOME_UNKNOWN"})
    assert len(sh.platform_calls(qa, op="create")) == 1, "W2 不得盲目重发"
    rec = w2.api_ok("action", "reconcile", "--id", aid).json
    assert rec["state"] == "succeeded" and rec["remote_object_id"] == "LAB-0001"
    assert sh.platform_rows(qa, "SELECT id FROM items") == [{"id": "LAB-0001"}]
    assert len(sh.platform_calls(qa, op="create")) == 1
    assert sh.defects(qa)[0]["external_id"] == "LAB-0001"
    sh.drive_full(qa)
    assert len(sh.platform_rows(qa, "SELECT id FROM items")) == 2


# ===========================================================================
# AC-095 适配器能力先探测
# ===========================================================================

@pytest.mark.ac("AC-095")
@pytest.mark.layer("offline")
def test_ac095_unsupported_document_update_rejected_before_send(qa, tmp_path):
    sh.drive_full(qa)
    story = sh.story_id(qa)
    report = sh.latest_snapshot(qa, "exit_report")["id"]
    aid = sh.planned_archive(qa, story, report)
    sh.approve_broker_item(qa)
    assert qa.api_ok("action", "dispatch", "--id", aid).json["state"] == "succeeded"
    doc = sh.platform_rows(qa, "SELECT id, etag, content, complete FROM docs")
    assert len(doc) == 1
    original_cfg = sh.read_config(qa)

    # 平台只宣告支持查询/创建，不支持按 ETag 的安全改文档
    cfg = json.loads(json.dumps(original_cfg))
    [issue] = [a for a in cfg["adapters"] if a["kind"] == "issue"]
    issue["capabilities"] = ["query", "create"]
    assert sh.apply_config(qa, cfg, tmp_path, "lab-demo").json["applied"] is True
    caps = {c["item"]: c for c in qa.ok("admin", "doctor").json["checks"]}["adapter:lab-platform"]["detail"]["capabilities"]
    assert caps == ["query", "create"], "能力检查应如实报告已宣告的能力"

    ledger, actions0, seq0 = sh.platform_calls(qa), sh.count(qa, "actions"), sh.last_seq(qa)
    r = qa.api("archive", "plan", "--story", story, "--report", report, "--space", "lab-qa-archive",
               "--existing-doc", doc[0]["id"], "--if-match", doc[0]["etag"], "--dispatch")
    sh.assert_rejected(r, {"UNSUPPORTED_CAPABILITY"})
    assert r.rc == 3 and "lab-platform" in r.errors[0]["related_refs"]
    assert sh.platform_calls(qa) == ledger, "不支持的操作必须在发送前被拒，外部调用为 0"
    assert sh.platform_rows(qa, "SELECT id, etag, content, complete FROM docs") == doc, "不得改用整篇覆盖或新建"
    assert sh.count(qa, "actions") == actions0, "不得登记降级后的动作"
    assert sh.count(qa, "snapshots", "kind='archive_record'") == 1
    new_events = sh.events_after(qa, seq0)
    assert [e["event_type"] for e in new_events] == ["command_rejected"]
    assert "UNSUPPORTED_CAPABILITY" in new_events[0]["payload"]
    # 新建文档的能力同样未宣告：也不能当作替代写法
    r2 = qa.api("archive", "plan", "--story", story, "--report", sh.latest_snapshot(qa, "test_report")["id"],
                "--space", "lab-qa-archive", "--dispatch")
    sh.assert_rejected(r2, {"UNSUPPORTED_CAPABILITY"})
    assert sh.platform_calls(qa) == ledger

    # 修正后可继续：恢复能力宣告后，同一请求可以进入计划（仍需授权）
    assert sh.apply_config(qa, original_cfg, tmp_path, "lab-demo").json["applied"] is True
    ok = qa.api_ok("archive", "plan", "--story", story, "--report", report, "--space", "lab-qa-archive",
                   "--existing-doc", doc[0]["id"], "--if-match", doc[0]["etag"])
    assert ok.json["action"]["state"] == "awaiting_authorization"
    assert sh.platform_calls(qa) == ledger


@pytest.mark.ac("AC-095")
@pytest.mark.layer("offline")
def test_ac095_unsupported_defect_operations_rejected_before_send(qa, tmp_path):
    story, ds = sh.proposed_defects_workspace(qa)
    cfg = sh.read_config(qa)
    [issue] = [a for a in cfg["adapters"] if a["kind"] == "issue"]
    issue["capabilities"] = ["query"]
    assert sh.apply_config(qa, cfg, tmp_path, "lab-demo").json["applied"] is True
    r = qa.api("defect", "create", "--story", story, "--defect", ds[0]["id"], "--dispatch")
    sh.assert_rejected(r, {"UNSUPPORTED_CAPABILITY"})
    assert sh.platform_calls(qa) == [] and sh.count(qa, "actions", "kind LIKE 'defect.%'") == 0
    assert all(d["status"] == "proposed" and not d["external_id"] for d in sh.defects(qa))
    # 目标白名单不允许的操作同样在发送前拒绝
    r2 = qa.api("execution", "plan", "--story", story, "--cases", "C1", "--target", "lab-platform", "--dispatch")
    sh.assert_rejected(r2, {"UNSUPPORTED_CAPABILITY", "PRECONDITION_FAILED"})
    assert sh.platform_calls(qa) == []


# ===========================================================================
# AC-096 陈旧路径与隐式镜像回退被拒
# ===========================================================================

def _doctor_checks(qa: QA) -> dict:
    return {c["item"]: c for c in qa.ok("admin", "doctor").json["checks"]}


@pytest.mark.ac("AC-096")
@pytest.mark.layer("offline")
def test_ac096_setup_rejects_missing_rule_source(qa, tmp_path):
    """配置的规则清单路径不存在，个人目录恰有同名文件：预览与应用都报告配置无效，不建立工作区。"""
    home_marker = tmp_path / "home-ran.txt"
    sh.plant_home_scripts(qa, home_marker)
    (qa.home / "legacy-manifest.json").write_text("{}", encoding="utf-8")
    (qa.cwd / "legacy-manifest.json").write_text("{}", encoding="utf-8")
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    cfg = {"schema": "qa.project_profile", "profile": "project", "project": {"id": "PRJ-X", "name": "陈旧路径项目"},
           "workspace": {"storage": "sqlite", "source_read_roots": [], "protected_roots": []}, "targets": [],
           "rules": {"kind": "legacy_bridge", "manifest": "legacy-manifest.json", "policy": {}},
           "identity": {"adapter": "none"}, "model": {}, "adapters": [], "timing": {}, "retention": {},
           "budgets": {"max_request_bytes": 1048576, "max_output_bytes": 262144, "exec_timeout_seconds": 60,
                       "max_concurrency": 1, "max_private_storage_mb": 64}}
    cfg_file = write_json(cfg_dir / "qa-flow.json", cfg)
    for extra in ((), ("--apply",)):
        r = qa.run("admin", "setup", "--config", str(cfg_file), *extra, cwd=qa.cwd)
        assert r.rc == 6, r.describe()
        assert r.json["applied"] is False
        assert [p["path"] for p in r.json["problems"]] == ["rules.manifest"], r.out
        assert str(cfg_dir) in r.json["problems"][0]["message"], "应指出所配置的路径，而不是其他位置"
    assert qa.workspaces() == [], "配置无效时不得建立工作区"
    assert not home_marker.exists()


@pytest.mark.ac("AC-096")
@pytest.mark.layer("offline")
def test_ac096_stale_rule_source_not_replaced_by_home_copy(qa, tmp_path):
    """冻结规则的源路径不存在，个人安装目录有同名旧脚本：doctor 报配置无效，规则不加载，能力不可用。"""
    p = sh.legacy_project(qa, tmp_path)
    story = p["story"]
    shutil.move(str(p["legacy_root"]), str(tmp_path / "legacy-gone"))
    home_marker = tmp_path / "home-ran.txt"
    sh.plant_home_scripts(qa, home_marker)

    checks = _doctor_checks(qa)
    assert checks["rules"]["status"] == "unavailable" and str(p["legacy_root"]) in checks["rules"]["detail"]
    assert checks["config"]["status"] == "invalid"
    assert [d["path"] for d in checks["config"]["detail"]] == ["rules.manifest"]

    seq0 = sh.last_seq(qa)
    ev = qa.api("stage", "evaluate", "--story", story, "--stage", "req_gate")      # 规则加载入口一
    if ev.rc == 0:
        assert ev.result["status"] != "completed" and ev.result["verdict"] != "pass", ev.describe()
    else:
        sh.assert_rejected(ev, UNAVAILABLE_CODES)
    rec = qa.run("admin", "parity-record", "--samples", str(p["samples"]), "--output", str(tmp_path / "base.json"))
    sh.assert_rejected(rec, {"RULES_UNAVAILABLE"})                                 # 规则加载入口二
    assert rec.rc == 6 and not (tmp_path / "base.json").exists()
    resumed = qa.run("resume", cwd=qa.cwd)
    assert resumed.rc in (0, 3, 6), resumed.describe()

    assert not home_marker.exists(), "不得加载个人目录或当前目录下的同名旧脚本"
    assert p["frozen_marker"].read_text(encoding="utf-8").strip().splitlines() == ["frozen"]
    assert not sh.events_after(qa, seq0, "stage_completed")
    assert sh.stage_statuses(qa, story)["req_gate"] != "completed", "能力必须保持不可用"
    assert qa.ok("status").result["verdict"] != "pass"


@pytest.mark.ac("AC-096")
@pytest.mark.layer("offline")
def test_ac096_missing_manifest_reports_invalid_config_on_rule_load(qa, tmp_path):
    """工作区配置指向的冻结清单文件已不存在：规则加载应报告配置无效（稳定回执），不回退、不完成。"""
    p = sh.legacy_project(qa, tmp_path)
    story = p["story"]
    manifest = qa.workspace() / "config" / "legacy-manifest.json"
    home_marker = tmp_path / "home-ran.txt"
    sh.plant_home_scripts(qa, home_marker)
    for d in (qa.home, qa.cwd, qa.home / ".claude" / "skills"):
        shutil.copy2(manifest, d / "legacy-manifest.json")     # 个人目录恰有同名清单
    manifest.unlink()

    checks = _doctor_checks(qa)
    assert checks["rules"]["status"] == "unavailable" and str(manifest) in checks["rules"]["detail"]
    assert checks["config"]["status"] == "invalid"

    problems = []
    seq0 = sh.last_seq(qa)
    ev = qa.api("stage", "evaluate", "--story", story, "--stage", "req_gate", cwd=qa.cwd)
    if ev.rc == 0:
        if ev.result["status"] == "completed" or ev.result["verdict"] == "pass":
            problems.append("清单缺失却得到完成/通过")
    else:
        problems += [f"stage evaluate：{x}" for x in sh.rejection_problems(ev, UNAVAILABLE_CODES)]
    rec = qa.run("admin", "parity-record", "--samples", str(p["samples"]), "--output", str(tmp_path / "base.json"),
                 cwd=qa.cwd)
    problems += [f"parity-record：{x}" for x in sh.rejection_problems(rec, UNAVAILABLE_CODES)]
    if home_marker.exists():
        problems.append("加载并执行了个人目录下的同名旧脚本")
    if p["frozen_marker"].read_text(encoding="utf-8").strip().splitlines() != ["frozen"]:
        problems.append("清单缺失后仍执行了校验器")
    if sh.events_after(qa, seq0, "stage_completed"):
        problems.append("产生了 stage_completed 事件")
    st = qa.run("status")
    if st.rc == 0:
        cur = {s["stage"]: s["status"] for s in st.result["stages"]}["req_gate"]
        if cur == "completed":
            problems.append("规则不可用后，状态视图仍把需求质检显示为 completed")
    else:
        problems += [f"status：{x}" for x in sh.rejection_problems(st, UNAVAILABLE_CODES)]
    assert not problems, "；".join(problems) + "\n" + ev.describe() + "\n" + rec.describe()


# ===========================================================================
# AC-097 备份恢复保留全部引用
# ===========================================================================

@pytest.fixture()
def backed_up(qa, tmp_path):
    """含内部、私密与受限证据的工作区，及其备份与恢复副本。"""
    sh.proposed_defects_workspace(qa)
    ws = qa.workspace()
    ev = qa.query("SELECT * FROM evidence ORDER BY id")
    assert len({e["rel_path"] for e in ev}) >= 3 and any(e["privacy"] == "private" for e in ev), "前置条件"
    digest0 = tree_digest(ws)
    bak = tmp_path / "备份 目录"
    created = qa.api_ok("backup", "create", "--output", str(bak)).json
    new_root = tmp_path / "恢复 根"
    new_root.mkdir()
    restored = qa.api_ok("backup", "restore", "--from", str(bak), "--to", str(new_root / "lab-demo")).json
    q2 = QA(new_root, qa.home, qa.cwd)
    return {"qa": qa, "ws": ws, "bak": bak, "created": created, "restored": restored, "q2": q2,
            "rws": new_root / "lab-demo", "digest0": digest0, "evidence": ev}


@pytest.mark.ac("AC-097")
@pytest.mark.layer("offline")
def test_ac097_backup_and_restore_keep_database_and_references(backed_up):
    b = backed_up
    qa, q2, ws, rws, bak = b["qa"], b["q2"], b["ws"], b["rws"], b["bak"]
    assert b["created"]["verification"]["ok"] is True and b["restored"]["verification"]["ok"] is True
    assert tree_digest(ws) == b["digest0"], "备份与恢复不得修改原工作区"

    manifest = json.loads((bak / "backup-manifest.json").read_text(encoding="utf-8"))
    assert manifest["database_sha256"] == sha256_file(bak / "qa.db")
    assert manifest["last_event_seq"] == sh.last_seq(qa)
    listed = {e["id"]: e for e in manifest["evidence"]}
    assert set(listed) == {e["id"] for e in b["evidence"]}, "引用清单必须覆盖数据库中的全部证据"
    for e in b["evidence"]:
        assert (listed[e["id"]]["sha256"], listed[e["id"]]["privacy"], listed[e["id"]]["state"]) == (
            e["sha256"], e["privacy"], e["state"])
        for base in (bak, rws):
            f = base / e["rel_path"]
            assert f.is_file() and sha256_file(f) == e["sha256"], f"{base.name} 中证据 {e['id']} 缺失或摘要不符"
    assert q2.query("SELECT * FROM evidence ORDER BY id", ws=rws) == b["evidence"]

    src, dst = sh.dump(qa), sh.dump(q2, ws=rws)
    for t in ("snapshots", "aggregates", "dependencies", "decisions", "actions", "action_attempts", "commands"):
        assert dst[t] == src[t], f"恢复后 {t} 表与原库不一致"
    assert dst["events"][:len(src["events"])] == src["events"]
    extra = dst["events"][len(src["events"]):]
    assert [e["event_type"] for e in extra] == ["restored"], "恢复只追加一条恢复记录"
    assert sh.consistency_problems(q2, rws) == []
    assert {m["key"]: m["value"] for m in dst["meta"]}["restored_from"] == manifest["id"]
    # 恢复副本可直接使用，业务视图一致
    a, c = qa.ok("status").result, q2.ok("status").result
    assert a == c


@pytest.mark.ac("AC-097")
@pytest.mark.layer("offline")
def test_ac097_private_objects_keep_access_restrictions(backed_up):
    b = backed_up
    ws, rws, bak = b["ws"], b["rws"], b["bak"]
    problems = []
    assert stat.S_IMODE((ws / "qa.db").stat().st_mode) & 0o077 == 0, "前置条件：原数据库仅属主可读写"
    for label, base in (("备份", bak), ("恢复副本", rws)):
        mode = stat.S_IMODE((base / "qa.db").stat().st_mode)
        if mode & 0o077:
            problems.append(f"{label}的数据库（含私密快照）权限为 {oct(mode)}，原库为 0o600")
        for e in b["evidence"]:
            if e["privacy"] not in ("private", "secret"):
                continue
            src_mode = stat.S_IMODE((ws / e["rel_path"]).stat().st_mode)
            m = stat.S_IMODE((base / e["rel_path"]).stat().st_mode)
            if m & 0o077 or m != src_mode:
                problems.append(f"{label}中私密证据 {e['id']} 权限为 {oct(m)}（原 {oct(src_mode)}）")
        for d in ("evidence/raw", "config"):
            src_mode = stat.S_IMODE((ws / d).stat().st_mode)
            m = stat.S_IMODE((base / d).stat().st_mode)
            if m & ~src_mode & 0o077:
                problems.append(f"{label}的 {d} 目录权限 {oct(m)} 比原目录 {oct(src_mode)} 更宽")
    restored_rows = {r["id"]: r for r in b["q2"].query("SELECT id, privacy FROM evidence", ws=rws)}
    for e in b["evidence"]:
        if restored_rows[e["id"]]["privacy"] != e["privacy"]:
            problems.append(f"证据 {e['id']} 的隐私等级在恢复后变化")
    snap_priv = b["qa"].query("SELECT id, privacy FROM snapshots ORDER BY id")
    assert b["q2"].query("SELECT id, privacy FROM snapshots ORDER BY id", ws=rws) == snap_priv
    assert not problems, "；".join(sorted(set(problems))[:8])


@pytest.mark.ac("AC-097")
@pytest.mark.layer("offline")
def test_ac097_retention_labels_survive(backed_up):
    b = backed_up
    private = [e for e in b["evidence"] if e["privacy"] in ("private", "secret")]
    restored = {r["id"]: r["retention"] for r in b["q2"].query("SELECT id, retention FROM evidence", ws=b["rws"])}
    manifest = {e["id"]: e for e in json.loads((b["bak"] / "backup-manifest.json").read_text(encoding="utf-8"))["evidence"]}
    assert all(restored[e["id"]] == e["retention"] == manifest[e["id"]]["retention"] for e in b["evidence"]), \
        "保留标签在备份清单或恢复副本中发生变化"
    unlabeled = [e["id"] for e in private if not e["retention"]]
    assert not unlabeled, (f"{len(unlabeled)}/{len(private)} 个私密证据在原库中就没有保留标签（retention 为空），"
                           f"无法满足“保留标签仍在”；配置的 retention 组为 {sh.read_config(b['qa']).get('retention')}")


@pytest.mark.ac("AC-097")
@pytest.mark.layer("offline")
def test_ac097_derived_reports_rebuildable_after_restore(backed_up, tmp_path):
    b = backed_up
    qa, q2 = b["qa"], b["q2"]

    def export(q: QA, out) -> dict:
        r = q.ok("export", "--output", str(out))
        files = r.result["files"]
        data = json.loads(open(files["json"], encoding="utf-8").read())
        data.pop("exported_at"), data.pop("revision")
        md = [ln for ln in open(files["markdown"], encoding="utf-8").read().splitlines()
              if "事件序号" not in ln and "导出时间" not in ln]
        return {"json": data, "md": md, "names": sorted(os.path.basename(p) for p in files.values())}

    src = export(qa, tmp_path / "导出-原")
    first = export(q2, tmp_path / "导出-恢复")
    assert first["json"] == src["json"] and first["names"] == src["names"]
    assert [ln for ln in first["md"] if "导出" not in ln] == [ln for ln in src["md"] if "导出" not in ln]
    shutil.rmtree(tmp_path / "导出-恢复")                       # 派生文件丢失
    assert not (b["rws"] / "exports").exists() or not any((b["rws"] / "exports").iterdir())
    again = export(q2, tmp_path / "导出-恢复")
    assert again == first, "派生报告应可从数据库重建且内容一致"
    assert sh.consistency_problems(q2, b["rws"]) == []


# ===========================================================================
# AC-098 缺证据或复制半份备份不能恢复成功
# ===========================================================================

def _delete_evidence(bak, n):
    files = sorted(p for p in (bak / "evidence").rglob("*") if p.is_file())[:n]
    for f in files:
        f.unlink()
    return {str(f.relative_to(bak)) for f in files}


def _corrupt_page(bak, n=0):
    f = bak / "qa.db"
    data = bytearray(f.read_bytes())
    data[4096 * 3:4096 * 4] = b"\xab" * 4096
    f.write_bytes(bytes(data))
    return set()


def _truncate_db(bak, n=0):
    f = bak / "qa.db"
    data = f.read_bytes()
    f.write_bytes(data[:len(data) // 2])
    return set()


def _destroy_header(bak, n=0):
    f = bak / "qa.db"
    data = bytearray(f.read_bytes())
    data[:100] = b"X" * 100
    f.write_bytes(bytes(data))
    return set()


def _drop_manifest(bak, n=0):
    (bak / "backup-manifest.json").unlink()
    return set()


def _tamper_evidence(bak, n=0):
    f = sorted(p for p in (bak / "evidence").rglob("*") if p.is_file())[0]
    f.write_bytes(b"tampered")
    return {str(f.relative_to(bak))}


def _drop_db(bak, n=0):
    (bak / "qa.db").unlink()
    return set()


BROKEN_BACKUPS = {
    "one_evidence_missing": (_delete_evidence, 1), "three_evidence_missing": (_delete_evidence, 3),
    "evidence_tampered": (_tamper_evidence, 0), "db_page_corrupted": (_corrupt_page, 0),
    "db_half_copied": (_truncate_db, 0), "db_header_destroyed": (_destroy_header, 0),
    "manifest_missing": (_drop_manifest, 0), "db_missing": (_drop_db, 0),
}


@pytest.fixture()
def good_backup(qa, tmp_path):
    sh.proposed_defects_workspace(qa)
    bak = tmp_path / "bak-good"
    assert qa.api_ok("backup", "create", "--output", str(bak)).json["verification"]["ok"] is True
    return bak


@pytest.mark.ac("AC-098")
@pytest.mark.layer("offline")
@pytest.mark.parametrize("damage", sorted(BROKEN_BACKUPS))
def test_ac098_damaged_backup_is_not_restored(qa, tmp_path, good_backup, damage):
    ws = qa.workspace()
    bak = sh.copy_backup(good_backup, tmp_path / "bak-broken")
    ev_rows = qa.query("SELECT id, rel_path FROM evidence WHERE state='active'")
    fn, n = BROKEN_BACKUPS[damage]
    damaged = fn(bak, n)
    expect_refs = {e["id"] for e in ev_rows if e["rel_path"] in damaged}
    ws_digest = tree_digest(ws)
    target_root = tmp_path / "restore-target"
    target_root.mkdir()
    target = target_root / "lab-demo"

    r = qa.api("backup", "restore", "--from", str(bak), "--to", str(target))
    problems = sh.rejection_problems(r, RESTORE_FAIL_CODES)
    if target.exists() and any(target.iterdir()):
        problems.append(f"恢复失败却在目标目录留下内容：{sorted(p.name for p in target.iterdir())}")
    leftovers = [p.name for p in target_root.iterdir() if p.name != "lab-demo"]
    if leftovers:
        problems.append(f"恢复失败后残留暂存目录：{leftovers}")
    if QA(target_root, qa.home, qa.cwd).workspaces():
        problems.append("失败的恢复产生了可被识别为就绪的工作区")
    if tree_digest(ws) != ws_digest:
        problems.append("现有工作区被修改")
    if expect_refs and isinstance(r.json, dict) and r.errors:
        reported = {p.get("ref") for p in (r.errors[0].get("details") or {}).get("problems", [])}
        missing = expect_refs - reported
        if missing:
            problems.append(f"缺失项没有逐条报告：漏报 {sorted(missing)}")
    v = qa.api("backup", "verify", "--path", str(bak))
    if v.rc == 0 and (not isinstance(v.json, dict) or v.json.get("ok") is not False):
        problems.append("backup verify 没有报告该备份不可用")
    if v.rc not in (0, *sh.DOCUMENTED_FAILURE_CODES):
        problems.append(f"backup verify 以未登记的退出码 {v.rc} 结束（未处理异常）：{v.err[-200:]}")
    assert not problems, "；".join(problems) + "\n" + r.describe()[:2500]
    # 修正后可继续：完好的备份可以恢复到同一目标
    ok = qa.api_ok("backup", "restore", "--from", str(good_backup), "--to", str(target))
    assert ok.json["verification"]["ok"] is True and (target / "qa.db").is_file()


@pytest.mark.ac("AC-098")
@pytest.mark.layer("offline")
def test_ac098_restore_never_overwrites_existing_workspace(qa, tmp_path, good_backup):
    ws = qa.workspace()
    digest = tree_digest(ws)
    for src in (good_backup, sh.copy_backup(good_backup, tmp_path / "bak-broken")):
        if src != good_backup:
            _delete_evidence(src, 1)
        r = qa.api("backup", "restore", "--from", str(src), "--to", str(ws))
        sh.assert_rejected(r, RESTORE_FAIL_CODES)
        assert tree_digest(ws) == digest, "现有工作区不得被覆盖"
        assert not [p for p in ws.parent.iterdir() if ".restore-" in p.name]
    other = tmp_path / "occupied"
    other.mkdir()
    (other / "keep.txt").write_text("用户文件", encoding="utf-8")
    r = qa.api("backup", "restore", "--from", str(good_backup), "--to", str(other))
    sh.assert_rejected(r, RESTORE_FAIL_CODES)
    assert [p.name for p in other.iterdir()] == ["keep.txt"]
    assert qa.ok("status").rc == 0


@pytest.mark.ac("AC-098")
@pytest.mark.layer("offline")
def test_ac098_backup_of_workspace_with_missing_evidence_fails(qa, tmp_path):
    """§8 证据缺失：原工作区缺少有效证据时，备份明确失败，不产生可用的备份。"""
    sh.proposed_defects_workspace(qa)
    ws = qa.workspace()
    row = qa.query("SELECT id, rel_path FROM evidence WHERE state='active' ORDER BY id LIMIT 1")[0]
    affected = {e["id"] for e in qa.query("SELECT id FROM evidence WHERE rel_path=?", (row["rel_path"],))}
    (ws / row["rel_path"]).unlink()
    bak = tmp_path / "bak"
    r = qa.api("backup", "create", "--output", str(bak))
    sh.assert_rejected(r, {"EVIDENCE_INVALID"})
    assert set(r.errors[0]["related_refs"]) & affected
    assert not (bak / "backup-manifest.json").exists(), "失败的备份不得留下清单（假就绪）"
    v = qa.api("backup", "verify", "--path", str(bak))
    assert v.rc != 0 or v.json["ok"] is False
    target = tmp_path / "t" / "lab-demo"
    target.parent.mkdir()
    rr = qa.api("backup", "restore", "--from", str(bak), "--to", str(target))
    sh.assert_rejected(rr, RESTORE_FAIL_CODES)
    assert not target.exists()


@pytest.mark.ac("AC-098")
@pytest.mark.layer("offline")
def test_ac098_live_database_corruption_stops_with_explicit_error(qa, tmp_path):
    """§8 数据库损坏：明确错误、停止推进；不能产出“成功”的备份。"""
    sh.proposed_defects_workspace(qa)
    ws = qa.workspace()
    _corrupt_page(ws)
    problems = []
    doctor = qa.run("admin", "doctor")
    if doctor.rc == 0:
        if doctor.json["workspace"]["integrity"] == "ok":
            problems.append("doctor 报告完整性 ok")
    else:
        problems += [f"doctor：{x}" for x in sh.rejection_problems(doctor, {"WORKSPACE_INVALID", "INTERNAL_INCONSISTENCY",
                                                                           "IO_ERROR"})]
    resumed = qa.run("resume")
    if resumed.rc == 0:
        problems.append("数据库损坏后 resume 仍以退出码 0 继续推进")
    else:
        problems += [f"resume：{x}" for x in sh.rejection_problems(
            resumed, {"WORKSPACE_INVALID", "INTERNAL_INCONSISTENCY", "IO_ERROR"})]
    bak = tmp_path / "bak"
    created = qa.api("backup", "create", "--output", str(bak))
    if created.rc == 0:
        problems.append(f"损坏数据库的备份以退出码 0 返回（verification.ok="
                        f"{(created.json or {}).get('verification', {}).get('ok')}），并留下清单："
                        f"{(bak / 'backup-manifest.json').exists()}")
    else:
        problems += [f"backup create：{x}" for x in sh.rejection_problems(
            created, {"WORKSPACE_INVALID", "INTERNAL_INCONSISTENCY", "IO_ERROR", "EVIDENCE_INVALID"})]
    if bak.exists():
        target = tmp_path / "t" / "lab-demo"
        target.parent.mkdir()
        rr = qa.api("backup", "restore", "--from", str(bak), "--to", str(target))
        if rr.rc == 0 or target.exists():
            problems.append("损坏数据库的备份可以被恢复")
    assert not problems, "；".join(problems)


# ===========================================================================
# AC-113 恢复后事件重建与只读导出
# ===========================================================================

@pytest.fixture()
def restored_full(qa, tmp_path):
    """多阶段、缺陷、决定俱全且含一条已撤销批准的工作区，及其恢复副本。"""
    sh.drive_full(qa)
    story = sh.story_id(qa)
    exit_dec = [d["id"] for d in qa.query("SELECT id, subjects, result FROM decisions")
                if '"EXT-' in d["subjects"] and d["result"] == "approve"]
    assert len(exit_dec) == 1
    qa.api_ok("decision", "revoke", "--id", exit_dec[0], "--reason", "验收：撤销准出确认")
    assert qa.api_ok("decision", "verify", "--id", exit_dec[0]).json["valid"] is False
    assert sh.stage_statuses(qa, story)["test_exit"] != "completed"
    bak = tmp_path / "bak"
    qa.api_ok("backup", "create", "--output", str(bak))
    new_root = tmp_path / "新目录"
    new_root.mkdir()
    qa.api_ok("backup", "restore", "--from", str(bak), "--to", str(new_root / "lab-demo"))
    return {"qa": qa, "q2": QA(new_root, qa.home, qa.cwd), "rws": new_root / "lab-demo", "story": story,
            "revoked": exit_dec[0]}


@pytest.mark.ac("AC-113")
@pytest.mark.layer("lab")
def test_ac113_business_state_identical_after_restore(restored_full):
    r = restored_full
    qa, q2, rws, story = r["qa"], r["q2"], r["rws"], r["story"]
    assert q2.ok("status").result == qa.ok("status").result
    assert q2.api_ok("stage", "status", "--story", story).json == qa.api_ok("stage", "status", "--story", story).json
    assert q2.api_ok("execution", "status", "--story", story).json == qa.api_ok("execution", "status", "--story", story).json
    assert q2.api_ok("action", "list").json == qa.api_ok("action", "list").json
    assert sh.defects(q2) == sh.defects(qa) and len(sh.defects(qa)) == 2
    for d in qa.query("SELECT id FROM decisions"):
        a = qa.api_ok("decision", "verify", "--id", d["id"]).json
        b = q2.api_ok("decision", "verify", "--id", d["id"]).json
        assert (a["valid"], a["reasons"]) == (b["valid"], b["reasons"]), d["id"]
    for kind in ("exit_report", "test_report"):
        assert sh.latest_snapshot(q2, kind) == sh.latest_snapshot(qa, kind), "报告与指标快照应一致"
    src, dst = sh.dump(qa), sh.dump(q2, ws=rws)
    for t in ("snapshots", "aggregates", "dependencies", "decisions", "actions", "action_attempts", "evidence",
              "platform_events"):
        assert dst[t] == src[t], t
    assert dst["events"][:len(src["events"])] == src["events"]
    # 派生状态（聚合）与事件、不可变快照一致
    assert sh.consistency_problems(q2, rws) == []


@pytest.mark.ac("AC-113")
@pytest.mark.layer("lab")
def test_ac113_derived_files_rebuilt_from_restored_state(restored_full, tmp_path):
    r = restored_full
    qa, q2 = r["qa"], r["q2"]

    def export(q: QA, out) -> dict:
        res = q.ok("export", "--output", str(out)).result["files"]
        data = json.loads(open(res["json"], encoding="utf-8").read())
        data.pop("exported_at"), data.pop("revision")
        html = os.path.getsize(res["html"])
        return {"json": data, "names": sorted(os.path.basename(p) for p in res.values()), "html_nonempty": html > 0}

    src = export(qa, tmp_path / "src-export")
    out = tmp_path / "restored-export"
    first = export(q2, out)
    assert first == src
    assert first["json"]["verdict"] != "pass" and first["json"]["lab"] is True
    assert {s["stage"]: s["status"] for s in first["json"]["stages"]}["test_exit"] != "completed"
    tables = sh.dump(q2, ws=r["rws"])
    shutil.rmtree(out)
    assert export(q2, out) == first, "派生文件丢失后应可重建"
    assert sh.dump(q2, ws=r["rws"]) == tables, "导出是只读操作"


@pytest.mark.ac("AC-113")
@pytest.mark.layer("lab")
def test_ac113_revoked_approval_not_reactivated(restored_full):
    r = restored_full
    q2, story, dec = r["q2"], r["story"], r["revoked"]
    v = q2.api_ok("decision", "verify", "--id", dec).json
    assert v["valid"] is False and v["decision"]["revoked_at"]
    resumed = q2.run("resume")
    assert resumed.rc in (0, 3), resumed.describe()
    v2 = q2.api_ok("decision", "verify", "--id", dec).json
    assert v2["valid"] is False and v2["decision"]["revoked_at"] == v["decision"]["revoked_at"]
    assert sh.stage_statuses(q2, story)["test_exit"] != "completed", "失效批准不得重新生效"
    approvals = [d for d in q2.query("SELECT id, subjects, result, revoked_at FROM decisions")
                 if '"EXT-' in d["subjects"] and d["result"] == "approve" and not d["revoked_at"]]
    for d in approvals:
        assert q2.api_ok("decision", "verify", "--id", d["id"]).json["valid"] is False or d["id"] != dec
    assert not [d for d in approvals if q2.query("SELECT actor FROM decisions WHERE id=?", (d["id"],))[0]["actor"]
                not in (q2.query("SELECT actor FROM decisions WHERE id=?", (dec,))[0]["actor"],)]
    view = q2.ok("status").result
    assert view["verdict"] != "pass"
