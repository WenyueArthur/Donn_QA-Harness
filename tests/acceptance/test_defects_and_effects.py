"""M5 独立验收：缺陷提案与根因、建单、附件、平台事件、复测关单、相邻回归、worker 提醒。

场景：AC-037～AC-046、AC-116、AC-120；AC-111 需要真实测试空间，记 blocked。
同时覆盖方案 §8 故障矩阵中的四行：远端已创建但响应丢失、附件已上传但关联失败、
重复或乱序平台事件、worker 停止与重复 tick。

观测方式：产品只通过子进程 CLI 调用；创建、上传、关联、流转次数从平台桩库的调用账本与事件流独立统计；
本地状态从工作区主库只读查询。平台侧“研发改状态”等外部行为由辅助函数直接写平台桩库模拟。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from support import defects_helpers as dh
from support import flow
from support.cli import blocked, write_json

PRODUCT_TERMINAL = {"closed", "rejected", "deferred", "accepted_risk"}
STALE_TOKENS = ("stale", "过期", "新鲜度", "过时", "未刷新", "需要刷新")


def plus_hours(iso: str, hours: float) -> str:
    """独立的时间参考计算（UTC）。"""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    return (dt + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def at(qa, clock: str):
    return qa.test_mode(QA_FLOW_TEST_CLOCK=clock)


def _create_one_defect(q, story: str, tc: str = "C1", attach: bool = True) -> dict:
    """在“失败已出现、尚无缺陷”的工作区里，对指定用例提案并建单。"""
    att = dh.latest_attempt(q, tc, "build1", "execution")
    assert att and att["outcome"] == "fail", f"前置：{tc} 在 build1 上应失败"
    bug = dh.propose(q, story, att["id"])
    assert bug.rc == 0, bug.describe()
    bug_id = bug.result["defect_id"]
    r = q.api_ok("defect", "create", "--story", story, "--defect", bug_id, "--dispatch")
    assert r.json["state"] == "succeeded", r.describe()
    if attach:
        r = q.api_ok("defect", "attach", "--story", story, "--defect", bug_id, "--dispatch")
        assert r.json["state"] == "succeeded", r.describe()
    return dh.defect_by_tc(q, tc)


# ---------------------------------------------------------------------------
# AC-038 未知根因不阻止合格缺陷草稿
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-038")
@pytest.mark.layer("offline")
def test_ac038_proposal_allowed_with_unknown_root_cause(qa):
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_PROPOSE)
    story = dh.story_id(q)
    att = dh.latest_attempt(q, "C1", "build1", "execution")
    assert att and att["outcome"] == "fail"
    seq0 = dh.last_seq(q)

    r = dh.propose(q, story, att["id"])  # 不提供根因
    assert r.rc == 0 and r.json["status"] == "ok" and r.json["committed"] is True, r.describe()

    d = dh.defect_by_tc(q, "C1")
    assert d["status"] == "proposed"
    # 根因标为待验证，不能出现编造的结论
    assert d["root_cause"]["status"] not in ("confirmed", "verified", "inferred"), d["root_cause"]
    assert "待验证" in d["root_cause"]["text"], d["root_cause"]
    # 提案要素完整：复现步骤、期望、实际、版本、证据
    assert d["reproduction"], "缺少复现步骤"
    assert d["expected_ref"]["expected_text"], "缺少期望"
    assert d["actual_ref"]["attempt_ref"] == att["id"] and d["actual_ref"]["failed_assertions"], "缺少实际结果"
    assert d["build"] == "build1"
    ev = dh.evidence_row(q, d["evidence_ref"])
    assert ev is not None and ev["state"] == "active", "提案证据未登记"
    data = (q.workspace() / ev["rel_path"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == ev["sha256"], "证据文件摘要与登记不符"
    types = [e["event_type"] for e in dh.events_since(q, seq0)]
    assert "defect_proposed" in types
    assert dh.pf_writes(q) == [], "提案阶段不应写入平台"

    # 根因未知不妨碍建单；平台字段中不出现根因结论
    c = q.api_ok("defect", "create", "--story", story, "--defect", d["id"], "--dispatch")
    assert c.json["state"] == "succeeded", c.describe()
    item = dh.pf_items(q)[dh.defect_by_tc(q, "C1")["external_id"]]
    assert not any("cause" in k.lower() or "根因" in k for k in item["fields"]), item["fields"]


@pytest.mark.ac("AC-038")
@pytest.mark.layer("offline")
def test_ac038_passing_attempt_cannot_become_defect(qa):
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_PROPOSE)
    story = dh.story_id(q)
    att = dh.latest_attempt(q, "C2", "build1", "execution")
    assert att and att["outcome"] == "pass"
    seq0 = dh.last_seq(q)
    r = dh.propose(q, story, att["id"])
    assert r.rc == 3 and r.error_code == "PRECONDITION_FAILED", r.describe()
    assert dh.defects(q) == []
    assert "defect_proposed" not in [e["event_type"] for e in dh.events_since(q, seq0)]
    # 修正输入（改用失败的尝试）后可以继续
    ok = dh.propose(q, story, dh.latest_attempt(q, "C1", "build1", "execution")["id"])
    assert ok.rc == 0, ok.describe()


# ---------------------------------------------------------------------------
# AC-037 有完整证据才确认根因
# ---------------------------------------------------------------------------

def _investigation(defect: dict, build: str, evidence_refs: list[str]) -> dict:
    return {
        "defect_ref": defect["id"], "build": build, "subject_ids": ["O100"],
        "facts": [{"text": "同键第二次请求返回 201 且 O100 已退款由 3000 变为 6000", "evidence_refs": evidence_refs}],
        "inferences": [{"text": "幂等键命中后仍走了扣减分支", "basis": "代码指向重复提交分支"}],
        "pending": [],
    }


@pytest.mark.ac("AC-037")
@pytest.mark.layer("lab")
def test_ac037_facts_inferences_and_confirmation_recorded_separately(qa, tmp_path):
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_FIX_DEPLOY)
    story = dh.story_id(q)
    d = dh.defect_by_tc(q, "C1")

    # 事实引用不存在的证据：拒绝，不登记
    bad = q.api("artifact", "submit", "--story", story, "--kind", "investigation", "--file",
                str(write_json(tmp_path / "inv-bad.json", _investigation(d, "build1", ["EV-DOES-NOT-EXIST"]))))
    assert bad.rc == 4 and bad.error_code == "REFERENCE_INVALID", bad.describe()
    assert dh.snapshots(q, "investigation") == []

    good = q.api("artifact", "submit", "--story", story, "--kind", "investigation", "--file",
                 str(write_json(tmp_path / "inv.json", _investigation(d, "build1", [d["evidence_ref"]]))))
    assert good.rc == 0, good.describe()
    inv_id = good.result["artifact_id"]
    inv = dh.show(q, inv_id)
    p = inv["envelope"]["payload"]
    assert p["build"] == "build1" and p["subject_ids"] == ["O100"]
    assert p["facts"][0]["evidence_refs"] == [d["evidence_ref"]] and p["inferences"], "事实与推断应分层保存"
    assert all("evidence_refs" not in i for i in p["inferences"]), "推断不应被记成已证实事实"

    # 责任人确认：独立的决定记录，绑定该调查的摘要
    dec = q.api("decision", "record", "--story", story, "--file", str(write_json(tmp_path / "dec.json", {
        "subject_refs": [{"ref": inv_id, "sha256": inv["sha256"]}], "role": "developer", "actor": "lab-developer",
        "verification": "local_attested", "scope": {"environment": "lab"}, "result": "confirm",
        "reason": "研发负责人确认根因"})))
    assert dec.rc == 0, dec.describe()
    rows = q.query("SELECT id, role, result, subjects FROM decisions WHERE id=?", (dec.result["decision_ref"],))
    assert rows and rows[0]["role"] == "developer" and rows[0]["result"] == "confirm"
    assert json.loads(rows[0]["subjects"])[0]["ref"] == inv_id
    # 调查快照本身未被确认改写
    assert dh.show(q, inv_id)["sha256"] == inv["sha256"]


@pytest.mark.ac("AC-037")
@pytest.mark.layer("lab")
def test_ac037_confirmed_root_cause_is_reflected_on_defect(qa, tmp_path):
    """证据完整且责任人确认后，缺陷的根因状态应从“待验证”变为已确认，并能追溯到调查与确认。"""
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_FIX_DEPLOY)
    story = dh.story_id(q)
    d = dh.defect_by_tc(q, "C1")
    good = q.api_ok("artifact", "submit", "--story", story, "--kind", "investigation", "--file",
                    str(write_json(tmp_path / "inv.json", _investigation(d, "build1", [d["evidence_ref"]]))))
    inv_id = good.result["artifact_id"]
    q.api_ok("decision", "record", "--story", story, "--file", str(write_json(tmp_path / "dec.json", {
        "subject_refs": [{"ref": inv_id, "sha256": dh.show(q, inv_id)["sha256"]}], "role": "developer",
        "actor": "lab-developer", "verification": "local_attested", "scope": {"environment": "lab"},
        "result": "confirm", "reason": "研发负责人确认根因"})))
    q.ok("resume")
    after = dh.defect_by_tc(q, "C1")
    assert after["root_cause"]["status"] in ("confirmed", "verified"), (
        f"调查与责任人确认已登记，但缺陷根因仍为 {after['root_cause']}；没有把确认结果关联到缺陷的入口")


@pytest.mark.ac("AC-037")
@pytest.mark.layer("lab")
def test_ac037_evidence_from_other_build_is_rejected(qa, tmp_path):
    """调查声明 build1，但事实证据来自 build2 的执行：证据未绑定同一 build，应被拒绝。"""
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_FIX_DEPLOY)
    story = dh.story_id(q)
    d = dh.defect_by_tc(q, "C1")
    dh.deploy(q, story, "build2")
    run = dh.run_cases(q, story, ["C1"], "retest")
    assert run.rc == 0 and run.json["state"] == "succeeded", run.describe()
    other = dh.latest_attempt(q, "C1", "build2")
    assert other and other["build"] == "build2"
    r = q.api("artifact", "submit", "--story", story, "--kind", "investigation", "--file", str(write_json(
        tmp_path / "inv-mixed.json", _investigation(d, "build1", [d["evidence_ref"], other["observations_ref"]]))))
    assert r.rc != 0, "调查声明 build1 却引用 build2 的证据，被接受为事实：\n" + r.describe()
    assert dh.snapshots(q, "investigation") == []


# ---------------------------------------------------------------------------
# AC-041 附件实际关联后才算证据齐全
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-041")
@pytest.mark.layer("lab")
def test_ac041_attachment_verified_only_after_link_readback(qa):
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_PROPOSE)
    story = dh.story_id(q)
    d = _create_one_defect(q, story, "C1", attach=False)
    ext = d["external_id"]
    assert ext and d["attachments_verified"] is False and d["attachment_file_id"] is None
    assert dh.pf_items(q)[ext]["attachments"] == []
    assert "附件回读未核对" in q.text("status", "--details").out

    r = q.api_ok("defect", "attach", "--story", story, "--defect", d["id"], "--dispatch")
    assert r.json["state"] == "succeeded", r.describe()
    d = dh.defect_by_tc(q, "C1")
    item = dh.pf_items(q)[ext]
    # 平台字段实际包含附件 ID，且与本地记录一致
    assert item["attachments"] == [d["attachment_file_id"]] and d["attachments_verified"] is True
    files = dh.pf_query(q, "SELECT * FROM files")
    assert len(files) == 1, "只应上传场景要求的一份证据，不强制全量日志"
    ev = dh.evidence_row(q, d["evidence_ref"])
    assert files[0]["id"] == d["attachment_file_id"] and files[0]["item_id"] == ext
    assert files[0]["sha256"] == ev["sha256"], "平台附件内容与本地证据摘要不一致"
    data = (q.workspace() / ev["rel_path"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == files[0]["sha256"]
    # 严重程度与平台字段一致
    assert item["fields"]["severity"] == d["severity"] and d["severity"] in ("blocker", "major", "minor")
    # 调用账本：创建、上传、关联各一次，且关联之后有回读
    ok = lambda op: [c for c in dh.pf_calls(q, op) if c["outcome"] == "ok"]  # noqa: E731
    assert len(dh.pf_calls(q, "create")) == 1 and len(dh.pf_calls(q, "upload")) == 1
    assert len(dh.pf_calls(q, "link")) == 1 and len(ok("link")) == 1
    link_seq = dh.pf_calls(q, "link")[0]["seq"]
    assert any(c["seq"] > link_seq for c in dh.pf_calls(q, "get", ext)), "关联后没有回读工作项"
    assert "附件回读已核对" in q.text("status", "--details").out


# ---------------------------------------------------------------------------
# AC-042 / §8 附件已上传但关联失败
# ---------------------------------------------------------------------------

LINK_FAULT = "attachment_uploaded_link_failed"


def _clear_link_fault(q) -> None:
    """平台恢复正常：把关联故障的剩余次数置零。

    平台桩的该故障在触发后不会自行消耗（见 test_ac042_link_fault_hook_fires_once_and_is_ledgered），
    因此恢复类断言前显式清除。
    """
    q.api_ok("lab", "fault", "--name", LINK_FAULT, "--times", "0")


@pytest.mark.ac("AC-042")
@pytest.mark.layer("lab")
def test_ac042_link_failure_keeps_target_and_retries_link_only(qa):
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_PROPOSE)
    story = dh.story_id(q)
    d = _create_one_defect(q, story, "C1", attach=False)
    ext = d["external_id"]
    q.api_ok("lab", "fault", "--name", LINK_FAULT)

    r = q.api("defect", "attach", "--story", story, "--defect", d["id"], "--dispatch")
    assert (r.json or {}).get("state") == "failed", r.describe()
    d1 = dh.defect_by_tc(q, "C1")
    # 保留 target_id 与不完整状态
    assert d1["external_id"] == ext and d1["attachments_verified"] is False
    assert d1["attachment_file_id"], "已上传的附件 ID 应保留，供只重试关联"
    assert dh.pf_items(q)[ext]["attachments"] == []
    assert [c for c in dh.pf_calls(q, "link") if c["outcome"] == "ok"] == []
    assert len(dh.pf_calls(q, "upload")) == 1
    assert "附件回读未核对" in q.text("status", "--details").out
    act = dh.actions(q, "defect.attach")
    assert len(act) == 1 and act[0]["state"] == "failed" and "关联失败" in (act[0]["last_error"] or "")

    # 恢复：只重试关联，不重复创建缺陷，也不产生第二份附件
    _clear_link_fault(q)
    r2 = q.api_ok("defect", "attach", "--story", story, "--defect", d["id"], "--dispatch")
    assert r2.json["state"] == "succeeded", r2.describe()
    d2 = dh.defect_by_tc(q, "C1")
    assert d2["external_id"] == ext and d2["attachments_verified"] is True
    assert dh.pf_items(q)[ext]["attachments"] == [d1["attachment_file_id"]]
    assert len(dh.pf_items(q)) == 1 and len(dh.pf_calls(q, "create")) == 1, "不得重复创建缺陷"
    assert len(dh.pf_query(q, "SELECT id FROM files")) == 1, "不得产生第二份附件"
    assert [c["outcome"] for c in dh.pf_calls(q, "upload")] == ["ok", "idempotent_hit"]
    assert [c["outcome"] for c in dh.pf_calls(q, "link") if c["outcome"] == "ok"] == ["ok"]
    assert len(dh.actions(q, "defect.attach")) == 1, "恢复应复用原操作记录"


@pytest.mark.ac("AC-042")
@pytest.mark.layer("lab")
def test_ac042_resume_recovers_link_without_recreating(qa):
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_PROPOSE)
    q.api_ok("lab", "fault", "--name", LINK_FAULT)
    q.ok("resume")
    ds = dh.defects(q)
    broken = [d for d in ds if d.get("external_id") and not d["attachments_verified"]]
    assert len(broken) == 1, f"应有一条缺陷停在“已建单、附件未关联”，实际 {[(d['tc_ref'], d['status']) for d in ds]}"
    ext = broken[0]["external_id"]
    assert dh.pf_items(q)[ext]["attachments"] == []
    creates_before = len(dh.pf_calls(q, "create"))
    # 故障持续期间再次恢复：仍不重建单
    q.ok("resume")
    assert len(dh.pf_calls(q, "create")) == creates_before == 1 and len(dh.pf_items(q)) == 1

    _clear_link_fault(q)
    flow.drive(q, start_demo=False)
    ds = dh.defects(q)
    assert len(ds) == 2 and all(d["attachments_verified"] for d in ds), [(d["tc_ref"], d["status"]) for d in ds]
    assert next(d for d in ds if d["id"] == broken[0]["id"])["external_id"] == ext
    items = dh.pf_items(q)
    assert len(items) == 2, f"两条失败用例只应有两个工作项，实际 {list(items)}"
    creates = dh.pf_calls(q, "create")
    assert len({c["target"] for c in creates}) == len(creates) == 2
    assert all(len(i["attachments"]) == 1 for i in items.values())
    assert len(dh.pf_query(q, "SELECT id FROM files")) == 2


@pytest.mark.ac("AC-042")
@pytest.mark.layer("lab")
def test_ac042_link_fault_hook_fires_once_and_is_ledgered(qa):
    """故障钩子按 --times 1 注入一次关联失败：失败调用应记入账本，下一次重试应成功。"""
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_PROPOSE)
    story = dh.story_id(q)
    d = _create_one_defect(q, story, "C1", attach=False)
    q.api_ok("lab", "fault", "--name", LINK_FAULT, "--times", "1")
    r = q.api("defect", "attach", "--story", story, "--defect", d["id"], "--dispatch")
    assert (r.json or {}).get("state") == "failed", r.describe()
    problems = []
    if [c["outcome"] for c in dh.pf_calls(q, "link")] != ["failed"]:
        problems.append(f"失败的关联调用未记入平台调用账本（link 记录：{dh.pf_calls(q, 'link')}）")
    r2 = q.api("defect", "attach", "--story", story, "--defect", d["id"], "--dispatch")
    if (r2.json or {}).get("state") != "succeeded":
        left = dh.pf_query(q, "SELECT remaining FROM faults WHERE name=?", (LINK_FAULT,))
        problems.append(f"故障只注入 1 次，但重试仍失败（state={(r2.json or {}).get('state')}，剩余次数 {left}）")
    assert not problems, "；".join(problems)


# ---------------------------------------------------------------------------
# AC-120 / §8 远端已创建但响应丢失；受理不等于执行成功
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-120")
@pytest.mark.layer("lab")
def test_ac120_create_response_lost_is_unknown_then_reconciled(qa):
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_PROPOSE)
    story = dh.story_id(q)
    att = dh.latest_attempt(q, "C1", "build1", "execution")
    bug_id = dh.propose(q, story, att["id"]).result["defect_id"]
    q.api_ok("lab", "fault", "--name", "create_succeeded_response_lost")

    r = q.api("defect", "create", "--story", story, "--defect", bug_id, "--dispatch")
    assert (r.json or {}).get("state") == "unknown", r.describe()
    # 远端实际已创建，但本地只能记为待核对，不能记业务成功
    assert len(dh.pf_items(q)) == 1
    d = dh.defect_by_tc(q, "C1")
    assert d["status"] == "proposed" and d["external_id"] is None
    act = dh.actions(q, "defect.create")
    assert len(act) == 1 and act[0]["state"] == "unknown" and act[0]["remote_object_id"] is None
    view = flow.view(q)
    assert [u["id"] for u in view["unknown_actions"]] == [act[0]["id"]]
    rep = dh.build_report(q, story)["exit_report"]
    assert dh.criteria(rep)["actions"]["result"] != "pass" and rep["verdict"] != "pass"

    # 结果未知时禁止盲目重发
    again = q.api("defect", "create", "--story", story, "--defect", bug_id, "--dispatch")
    assert again.rc == 5 and again.error_code == "OUTCOME_UNKNOWN", again.describe()
    again2 = q.api("action", "dispatch", "--id", act[0]["id"])
    assert again2.rc == 5 and again2.error_code == "OUTCOME_UNKNOWN", again2.describe()
    assert len(dh.pf_calls(q, "create")) == 1, "未知结果被重发"

    # 核对引用真实远端对象后才记成功
    rec = q.api_ok("action", "reconcile", "--id", act[0]["id"])
    assert rec.json["state"] == "succeeded" and rec.json["remote_object_id"] == "LAB-0001", rec.describe()
    d = dh.defect_by_tc(q, "C1")
    assert d["external_id"] == "LAB-0001" and d["status"] == "created"
    assert len(dh.pf_calls(q, "query")) >= 1 and len(dh.pf_calls(q, "create")) == 1 and len(dh.pf_items(q)) == 1
    assert dh.pf_items(q)["LAB-0001"]["fields"]["qa_ref"] == bug_id


@pytest.mark.ac("AC-120")
@pytest.mark.layer("lab")
def test_ac120_resume_reconciles_unknown_before_any_new_write(qa):
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_PROPOSE)
    q.api_ok("lab", "fault", "--name", "create_succeeded_response_lost")
    r = q.run("resume")
    assert r.rc in (0, 5), r.describe()
    unknown = [a for a in dh.actions(q, "defect.create") if a["state"] == "unknown"]
    assert len(unknown) == 1, [(a["id"], a["state"]) for a in dh.actions(q, "defect.create")]
    assert len(dh.pf_calls(q, "create")) == 1

    flow.drive(q, start_demo=False)
    creates = dh.pf_calls(q, "create")
    assert len(creates) == 2 and len({c["target"] for c in creates}) == 2, creates
    assert all(c["outcome"] == "ok" for c in creates), "同一缺陷被再次发送创建"
    assert len(dh.pf_items(q)) == 2
    assert all(a["state"] == "succeeded" for a in dh.actions(q, "defect.create"))
    assert sorted(d["external_id"] for d in dh.defects(q)) == ["LAB-0001", "LAB-0002"]


@pytest.mark.ac("AC-120")
@pytest.mark.layer("lab")
def test_ac120_accepted_receipt_without_execution_is_not_pass(qa, tmp_path):
    """脚本以退出码 0 返回 accepted=true、executed=false：不能记为业务通过。"""
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_SCRIPT)
    story = dh.story_id(q)
    script = tmp_path / "accept_only.py"
    script.write_text(
        "import json, sys\n"
        "case = sys.argv[sys.argv.index('--case') + 1] if '--case' in sys.argv else None\n"
        "print(json.dumps({'http_status': 200, 'accepted': True, 'executed': False, 'case': case}))\n",
        encoding="utf-8")
    reg = q.api_ok("script", "register", "--story", story, "--file", str(script), "--tc", "SMOKE-1",
                   "--repo", "acceptance", "--commit", "c1", "--path", "acceptance/accept_only.py")
    sr = reg.result["script_ref"]
    q.api_ok("script", "review", "--story", story, "--script", sr, "--result", "approve", "--reason", "验收用桩")

    r = dh.run_cases(q, story, ["SMOKE-1"], "smoke", script=sr)
    assert r.rc == 0, r.describe()
    att = dh.latest_attempt(q, "SMOKE-1", "build1")
    if att is not None:
        assert att["outcome"] != "pass", f"仅有受理回执却记为通过：{att}"
    st = q.api_ok("execution", "status", "--story", story).json
    assert st["latest"].get("SMOKE-1", {}).get("outcome") != "pass"
    rep = dh.build_report(q, story)
    assert rep["test_report"]["counts"]["pass"] == 0
    assert rep["exit_report"]["verdict"] != "pass"
    # 独立业务观测：被测退款库没有任何退款流水
    db = q.workspace() / "lab" / "target" / "refund.db"
    if db.exists():
        import sqlite3

        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "refunds" in tables:
                assert conn.execute("SELECT COUNT(*) FROM refunds").fetchone()[0] == 0
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# AC-039 修复后复测通过并按授权关单
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-039")
@pytest.mark.layer("lab")
def test_ac039_closed_once_and_traceable(qa):
    flow.drive(qa)
    ds = dh.defects(qa)
    assert sorted(d["tc_ref"] for d in ds) == ["C1", "C3"]
    atts = {a["id"]: a for a in dh.attempts(qa)}
    for d in ds:
        ext = d["external_id"]
        assert d["status"] == "closed" and ext
        # 缺陷 → 修复提交/构建 → 复测 → 关闭决定 可追溯
        assert d["fix_build"] == "build2" and d["fix_commit"] == dh.LAB_COMMITS["build2"]
        assert d["actual_ref"]["attempt_ref"] in atts and atts[d["actual_ref"]["attempt_ref"]]["build"] == "build1"
        retests = [atts[r] for r in d["retests"]]
        assert retests and all(r["tc_ref"] == d["tc_ref"] and r["build"] == "build2" for r in retests)
        assert retests[-1]["outcome"] == "pass"
        statuses = [e["status"] for e in dh.pf_events(qa, ext)]
        assert statuses.count("CLOSED") == 1, f"{ext} 的关闭次数应为 1：{statuses}"
        assert statuses.index("RESOLVED") < statuses.index("CLOSED")
        closing = [a for a in dh.actions(qa, "defect.transition")
                   if (a["remote_object_id"] or "").startswith(ext + "@") and a["state"] == "succeeded"]
        assert closing and all(a["authorization_ref"] for a in closing), "关闭操作缺少授权引用"
        assert dh.pf_items(qa)[ext]["status"] == "CLOSED"
    # 复测发生在修复部署之后
    dep = [s for s in dh.snapshots(qa, "deployment") if s["payload"]["build"] == "build2"]
    assert dep and dep[-1]["payload"]["commit"] == dh.LAB_COMMITS["build2"]

    # 重复恢复与再次请求关闭都不会产生第二次关闭
    writes_before = len(dh.pf_writes(qa))
    qa.ok("resume")
    story = dh.story_id(qa)
    for d in ds:
        r = qa.api("defect", "close", "--story", story, "--defect", d["id"], "--dispatch")
        assert r.rc in (0, 3), r.describe()
    for d in ds:
        assert [e["status"] for e in dh.pf_events(qa, d["external_id"])].count("CLOSED") == 1
    assert len(dh.pf_writes(qa)) == writes_before, "重复操作产生了新的平台写入"


# ---------------------------------------------------------------------------
# AC-040 修复状态不等于复测通过
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-040")
@pytest.mark.layer("lab")
def test_ac040_resolved_status_without_fix_build_retest_cannot_close(qa):
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_FIX_DEPLOY)
    story = dh.story_id(q)
    for d in dh.defects(q):
        dh.external_resolve(q, d["external_id"], dh.LAB_COMMITS["build2"], "build2")
    q.api_ok("defect", "sync", "--story", story)
    d = dh.defect_by_tc(q, "C1")
    assert d["status"] == "resolved" and d["external_status"] == "RESOLVED" and d["retests"] == []

    def refuse_close(expect_text: str) -> None:
        before = dh.defect_by_tc(q, "C1")
        seq0, writes = dh.last_seq(q), len(dh.pf_writes(q))
        r = q.api("defect", "close", "--story", story, "--defect", d["id"], "--dispatch")
        assert r.rc == 3 and r.error_code == "PRECONDITION_FAILED", r.describe()
        assert expect_text in r.errors[0]["message"], r.errors[0]["message"]
        assert len(dh.pf_writes(q)) == writes, "拒绝关闭时仍写入了平台"
        assert "CLOSED" not in [e["status"] for e in dh.pf_events(q, d["external_id"])]
        after = dh.defect_by_tc(q, "C1")
        assert after["status"] == "resolved" and after["_revision"] == before["_revision"]
        assert not [e for e in dh.events_since(q, seq0) if e["event_type"] in ("defect_transitioned", "action_planned")]

    refuse_close("复测")
    # 不能用 build1 的记录顶替：关联 build1 上本用例的旧尝试和别的用例的通过记录
    old_fail = dh.latest_attempt(q, "C1", "build1", "execution")
    other_pass = dh.latest_attempt(q, "C2", "build1", "execution")
    assert other_pass["outcome"] == "pass"
    for a in (old_fail, other_pass):
        q.api("defect", "retest", "--story", story, "--defect", d["id"], "--attempt", a["id"])
    refuse_close("复测")
    rep = dh.build_report(q, story)["exit_report"]
    assert dh.criteria(rep)["defects"]["result"] != "pass", "外部显示已修复即被当作缺陷已处理"

    # 补上 build2 的真实复测与相邻回归后可以关闭，且只关闭一次
    dh.deploy(q, story, "build2")
    run = dh.run_cases(q, story, ["C1", "C2"], "retest")
    assert run.rc == 0 and run.json["state"] == "succeeded", run.describe()
    new = dh.latest_attempt(q, "C1", "build2", "retest")
    assert new["outcome"] == "pass"
    q.api_ok("defect", "retest", "--story", story, "--defect", d["id"], "--attempt", new["id"])
    ok = q.api_ok("defect", "close", "--story", story, "--defect", d["id"], "--dispatch")
    assert ok.json["state"] == "succeeded", ok.describe()
    assert [e["status"] for e in dh.pf_events(q, d["external_id"])].count("CLOSED") == 1
    assert dh.defect_by_tc(q, "C1")["status"] == "closed"
    assert dh.defect_by_tc(q, "C3")["status"] == "resolved", "未复测的另一条缺陷不应被连带关闭"


@pytest.mark.ac("AC-040")
@pytest.mark.layer("lab")
def test_ac040_failed_retest_goes_through_authorized_reopen(qa):
    """研发把缺陷标为已修复（声称修复构建 build3），但 build3 上原用例仍失败。"""
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_FIX_DEPLOY)
    story = dh.story_id(q)
    dh.deploy(q, story, "build3")
    assert dh.run_cases(q, story, ["C2"], "execution").json["state"] == "succeeded"
    first = dh.latest_attempt(q, "C2", "build3", "execution")
    assert first["outcome"] == "fail", "前置：build3 上只读角色用例应失败"
    bug_id = dh.propose(q, story, first["id"]).result["defect_id"]
    q.api_ok("defect", "create", "--story", story, "--defect", bug_id, "--dispatch")
    q.api_ok("defect", "attach", "--story", story, "--defect", bug_id, "--dispatch")
    ext = dh.defect_by_tc(q, "C2")["external_id"]
    dh.external_resolve(q, ext, dh.LAB_COMMITS["build3"], "build3")
    q.api_ok("defect", "sync", "--story", story)
    assert dh.defect_by_tc(q, "C2")["status"] == "resolved"

    assert dh.run_cases(q, story, ["C2"], "retest").json["state"] == "succeeded"
    retest = dh.latest_attempt(q, "C2", "build3", "retest")
    assert retest["outcome"] == "fail"
    q.api_ok("defect", "retest", "--story", story, "--defect", bug_id, "--attempt", retest["id"])
    r = q.api("defect", "close", "--story", story, "--defect", bug_id, "--dispatch")
    assert r.rc == 3 and r.error_code == "PRECONDITION_FAILED" and "复测未通过" in r.errors[0]["message"], r.describe()
    assert "CLOSED" not in [e["status"] for e in dh.pf_events(q, ext)]

    # 复测失败后走已授权的 Reopen
    q.ok("resume")
    statuses = [e["status"] for e in dh.pf_events(q, ext)]
    assert statuses.count("REOPENED") == 1 and "CLOSED" not in statuses, statuses
    assert statuses.index("RESOLVED") < statuses.index("REOPENED")
    d = dh.defect_by_tc(q, "C2")
    assert d["status"] == "reopened" and d["external_status"] == "REOPENED"
    reopen = [a for a in dh.actions(q, "defect.transition") if (a["remote_object_id"] or "").startswith(ext + "@")]
    assert len(reopen) == 1 and reopen[0]["state"] == "succeeded" and reopen[0]["authorization_ref"]
    q.ok("resume")
    assert [e["status"] for e in dh.pf_events(q, ext)].count("REOPENED") == 1, "重复恢复产生了第二次 Reopen"


@pytest.mark.ac("AC-040")
@pytest.mark.layer("lab")
def test_ac040_new_defect_is_not_reopened_by_its_discovery_failure(qa):
    """新发现的缺陷尚未被修复，发现它的那次失败不是“复测失败”，不应触发 Reopen。"""
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_FIX_DEPLOY)
    story = dh.story_id(q)
    dh.deploy(q, story, "build3")
    q.ok("resume")
    d = dh.defect_by_tc(q, "C2")  # build3 放开了只读权限，回归中新发现
    statuses = [e["status"] for e in dh.pf_events(q, d["external_id"])]
    assert "RESOLVED" not in statuses, "前置：该缺陷从未被标为已修复"
    assert d["fix_build"] is None
    assert "REOPENED" not in statuses and d["status"] != "reopened", (
        f"缺陷 {d['id']} 从未修复，却被流转为 Reopen：平台事件 {statuses}，本地状态 {d['status']}，复测记录 {d['retests']}")
    assert d["actual_ref"]["attempt_ref"] not in d["retests"], "发现缺陷的那次失败被记成了复测"


# ---------------------------------------------------------------------------
# AC-043 / §8 重复或乱序平台事件；截止点提醒只生成一次
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-043")
@pytest.mark.layer("lab")
def test_ac043_duplicate_r3_and_late_r2_do_not_downgrade(qa):
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_FIX_DEPLOY)
    story = dh.story_id(q)
    d = dh.defect_by_tc(q, "C1")
    ext = d["external_id"]
    assert dh.pf_items(q)[ext]["revision"] == 2, "前置：创建为 r1，关联附件后为 r2"
    r3 = dh.external_set_status(q, ext, "IN_PROGRESS")
    assert r3["revision"] == 3
    q.api_ok("defect", "sync", "--story", story)
    d = dh.defect_by_tc(q, "C1")
    assert d["status"] == "in_progress" and d["platform_revision"] == 3

    # 重复的 r3 与迟到的 r2 到达
    dh.external_emit_event(q, ext, "IN_PROGRESS", 3, r3["event_id"])
    dh.external_emit_event(q, ext, "OPEN", 2, "PEV-LATE-R2")
    seq0 = dh.last_seq(q)
    s = q.api_ok("defect", "sync", "--story", story).json
    assert s["applied"] == [], s
    assert sorted(x["reason"] for x in s["skipped"] if x["defect"] == d["id"]) == ["duplicate", "out_of_order"], s
    after = dh.defect_by_tc(q, "C1")
    assert after["status"] == "in_progress" and after["platform_revision"] == 3
    assert after["_revision"] == d["_revision"], "重复或旧事件不应改动缺陷"
    assert not [e for e in dh.events_since(q, seq0) if e["object_id"].endswith(d["id"])]
    rows = q.query("SELECT event_id, applied FROM platform_events WHERE object_ref=?", (d["id"],))
    assert len([r for r in rows if r["event_id"] == r3["event_id"]]) == 1
    assert {r["event_id"]: r["applied"] for r in rows}.get("PEV-LATE-R2") == 0
    s2 = q.api_ok("defect", "sync", "--story", story).json
    assert s2["applied"] == [] and s2["skipped"] == []


@pytest.mark.ac("AC-043")
@pytest.mark.layer("lab")
def test_ac043_deadline_creates_single_pending_notification(qa):
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_FIX_DEPLOY)
    story = dh.story_id(q)
    d = dh.defect_by_tc(q, "C1")
    ext = d["external_id"]
    r3 = dh.external_set_status(q, ext, "IN_PROGRESS")
    q.api_ok("defect", "sync", "--story", story)
    deadline = plus_hours(d["created_at"], 24)  # 实验配置：缺陷处理时限 24 小时

    early = at(q, plus_hours(d["created_at"], 1)).api_ok("worker", "tick", "--once")
    assert early.json["reminders"] == [] and dh.actions(q, "notify.send") == [], "未到截止点不应生成提醒"

    late = at(q, plus_hours(d["created_at"], 25))
    late.api_ok("worker", "tick", "--once")
    dh.external_emit_event(q, ext, "IN_PROGRESS", 3, r3["event_id"])  # 重复事件再次到达
    dh.external_emit_event(q, ext, "OPEN", 2, "PEV-LATE-R2")
    late.api_ok("worker", "tick", "--once")
    at(q, plus_hours(d["created_at"], 26)).api_ok("worker", "tick", "--once")

    acts = dh.actions(q, "notify.send")
    plans = {a["id"]: dh.show(q, a["plan_ref"])["envelope"]["payload"]["request"] for a in acts}
    mine = [a for a in acts if plans[a["id"]]["defect"] == d["id"]]
    assert len(mine) == 1, f"同一缺陷同一截止点应只有一个提醒动作，实际 {len(mine)}"
    assert plans[mine[0]["id"]]["deadline"] == deadline
    assert mine[0]["state"] == "awaiting_authorization" and mine[0]["attempts"] == 0
    assert len(acts) == len({plans[a["id"]]["defect"] for a in acts}), "存在重复提醒"
    assert dh.pf_query(q, "SELECT * FROM notifications") == [] and dh.pf_calls(q, "notify") == []
    assert dh.defect_by_tc(q, "C1")["status"] == "in_progress"


@pytest.mark.ac("AC-043")
@pytest.mark.layer("lab")
def test_ac043_platform_fault_modes_duplicate_and_reversed_events(qa):
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_FIX_DEPLOY)
    q.api_ok("lab", "fault", "--name", "duplicate_event")
    q.api_ok("lab", "fault", "--name", "out_of_order_event")
    flow.drive(q, start_demo=False)

    ev = dh.pf_events(q)
    ids = [e["event_id"] for e in ev]
    assert len(ids) > len(set(ids)), "前置：平台桩应已产生重复事件"
    order = ["proposed", "created", "in_progress", "resolved", "retesting", "closed"]
    for d in dh.defects(q):
        assert d["status"] == "closed", (d["tc_ref"], d["status"])
        assert d["fix_build"] == "build2" and d["fix_commit"] == dh.LAB_COMMITS["build2"]
        hist = q.query("SELECT event_type, payload FROM events WHERE object_id LIKE ? ORDER BY seq", (f"%{d['id']}",))
        seen = [json.loads(h["payload"]).get("status") for h in hist
                if h["event_type"] in ("defect_synced", "defect_readback", "defect_transitioned")]
        idx = [order.index(s) for s in seen if s in order]
        assert idx == sorted(idx), f"缺陷 {d['id']} 状态出现回退：{seen}"
        revs = [json.loads(h["payload"])["revision"] for h in hist if h["event_type"] == "defect_synced"]
        assert revs == sorted(revs)
        assert [e["status"] for e in dh.pf_events(q, d["external_id"])].count("CLOSED") == 1
    rows = q.query("SELECT event_id, COUNT(*) AS n FROM platform_events GROUP BY event_id")
    assert all(r["n"] == 1 for r in rows)


# ---------------------------------------------------------------------------
# AC-044 拒绝或延期需要有效处置决定；未知平台状态保持 unknown
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-044")
@pytest.mark.layer("lab")
def test_ac044_developer_rejection_is_not_acceptance_without_product_decision(qa):
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_FIX_DEPLOY)
    story = dh.story_id(q)
    for d in dh.defects(q):
        dh.external_set_status(q, d["external_id"], "REJECTED")  # 研发在平台上标“不修”
    q.api_ok("defect", "sync", "--story", story)

    assert q.query("SELECT id FROM decisions WHERE role='product' AND result IN "
                   "('reject','defer','accept_risk','approve')") == [], "前置：产品没有任何处置决定"
    ds = dh.defects(q)
    rep = dh.build_report(q, story)["exit_report"]
    problems = []
    for d in ds:
        if d["status"] in PRODUCT_TERMINAL:
            problems.append(f"{d['id']} 本地状态为 {d['status']}（终态），处置决定 {d['disposition_refs']}")
    if dh.criteria(rep)["defects"]["result"] == "pass":
        problems.append("准出标准“阻断缺陷已关闭或有有效风险接受”被判为 pass")
    others = flow.view(q)["reviews"]["others"]
    if not [c for c in others if c["kind"] == "缺陷处置" and c["responsible_role"] == "product"]:
        problems.append("没有显示待产品处置的事项")
    assert not problems, "研发标不修后，未经产品确认即被视为已接受：" + "；".join(problems)


@pytest.mark.ac("AC-044")
@pytest.mark.layer("lab")
def test_ac044_tester_cannot_close_risk_locally(qa):
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_FIX_DEPLOY)
    story = dh.story_id(q)
    q.api_ok("defect", "sync", "--story", story)
    d = dh.defect_by_tc(q, "C1")

    bad = q.api("defect", "disposition", "--story", story, "--defect", d["id"], "--disposition", "wontfix",
                "--reason", "x")
    assert bad.rc == 2 and bad.error_code == "INVALID_ARGUMENT", bad.describe()

    r = q.api_ok("defect", "disposition", "--story", story, "--defect", d["id"], "--disposition", "rejected",
                 "--reason", "研发认为不修")
    handle = r.result["handle"]
    assert r.result["status"] == "disposition_pending"
    view = flow.view(q)
    card = [c for c in view["reviews"]["others"] if c["handle"] == handle]
    assert card and card[0]["responsible_role"] == "product" and card[0]["kind"] == "缺陷处置"
    assert handle not in [c["handle"] for c in view["reviews"]["mine"]]

    # 测试人员不能代签
    seq0 = dh.last_seq(q)
    sign = q.run("review", "--answer", f"{handle}=A")
    assert sign.rc == 3 and sign.error_code == "AUTHORIZATION_REQUIRED", sign.describe()
    assert not [e for e in dh.events_since(q, seq0) if e["event_type"] == "review_item_answered"]
    after = dh.defect_by_tc(q, "C1")
    assert after["status"] == "created" and after["disposition_refs"] == []
    rep = dh.build_report(q, story)["exit_report"]
    assert dh.criteria(rep)["defects"]["result"] == "fail"
    assert rep["verdict"] == "fail"
    assert d["id"] in [x["id"] for x in rep["defect_snapshot"]["defects"] if x["status"] not in PRODUCT_TERMINAL]


@pytest.mark.ac("AC-044")
@pytest.mark.layer("lab")
def test_ac044_valid_product_disposition_takes_effect(qa):
    """产品负责人作出有效处置决定后，处置应当生效并可追溯（拒绝路径修正后可继续）。"""
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_FIX_DEPLOY)
    story = dh.story_id(q)
    q.api_ok("defect", "sync", "--story", story)
    d = dh.defect_by_tc(q, "C1")
    handle = q.api_ok("defect", "disposition", "--story", story, "--defect", d["id"], "--disposition", "deferred",
                      "--reason", "排期到下个版本").result["handle"]
    ans = q.api("clarification", "answer", "--story", story, "--handle", handle, "--option", "A",
                "--role", "product", "--actor", "lab-product-owner")
    assert ans.rc == 0, "产品负责人的处置决定无法登记：\n" + ans.describe()
    q.ok("resume")
    after = dh.defect_by_tc(q, "C1")
    assert after["disposition_refs"] or after["status"] == "deferred", (
        f"处置决定已登记但未作用到缺陷：status={after['status']} disposition_refs={after['disposition_refs']}")
    # 另一条未处置的缺陷不受影响
    assert dh.defect_by_tc(q, "C3")["status"] == "created"


@pytest.mark.ac("AC-044")
@pytest.mark.layer("lab")
def test_ac044_unmapped_platform_status_stays_unknown(qa):
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_FIX_DEPLOY)
    story = dh.story_id(q)
    d = dh.defect_by_tc(q, "C1")
    dh.external_set_status(q, d["external_id"], "QA_HOLD")  # 配置的状态映射中没有该状态
    q.api_ok("defect", "sync", "--story", story)
    after = dh.defect_by_tc(q, "C1")
    assert after["status"] == "unknown" and after["external_status"] == "QA_HOLD"
    view = flow.view(q)
    assert next(x for x in view["defects"] if x["id"] == d["id"])["status"] == "unknown"
    rep = dh.build_report(q, story)["exit_report"]
    assert dh.criteria(rep)["defects"]["result"] != "pass", "未知平台状态被当作已处理"
    # 未知状态下不能关闭
    r = q.api("defect", "close", "--story", story, "--defect", d["id"], "--dispatch")
    assert r.rc == 3 and r.error_code == "PRECONDITION_FAILED", r.describe()
    # 平台回到已映射状态后恢复正常
    dh.external_set_status(q, d["external_id"], "IN_PROGRESS")
    q.api_ok("defect", "sync", "--story", story)
    assert dh.defect_by_tc(q, "C1")["status"] == "in_progress"


# ---------------------------------------------------------------------------
# AC-045 / AC-046 修复影响与相邻回归（buggy_neighbor 构建）
# ---------------------------------------------------------------------------

def _neighbor_setup(qa):
    """缺陷在 build1 建单；研发声明在 build3（buggy_neighbor）修复；在 build3 上分别执行原用例与相邻用例。"""
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_FIX_DEPLOY)
    story = dh.story_id(q)
    dh.deploy(q, story, "build3")
    for d in dh.defects(q):
        dh.external_resolve(q, d["external_id"], dh.LAB_COMMITS["build3"], "build3")
    q.api_ok("defect", "sync", "--story", story)
    r1 = dh.run_cases(q, story, ["C1"], "retest")        # 原问题
    r2 = dh.run_cases(q, story, ["C2"], "regression")    # 共用退款入口的相邻用例
    assert r1.rc == 0 and r1.json["state"] == "succeeded", r1.describe()
    assert r2.rc == 0 and r2.json["state"] == "succeeded", r2.describe()
    d = dh.defect_by_tc(q, "C1")
    orig = dh.latest_attempt(q, "C1", "build3", "retest")
    neigh = dh.latest_attempt(q, "C2", "build3", "regression")
    q.api_ok("defect", "retest", "--story", story, "--defect", d["id"], "--attempt", orig["id"])
    return q, story, dh.defect_by_tc(q, "C1"), orig, neigh


@pytest.mark.ac("AC-045")
@pytest.mark.layer("lab")
def test_ac045_fix_diff_scope_and_separate_results(qa, tmp_path):
    q, story, d, orig, neigh = _neighbor_setup(qa)

    # 导入 fix diff 并登记改动影响：原退款用例所在回归项与共用入口的相邻回归项
    diff = tmp_path / "fix-0003.diff"
    diff.write_text("--- a/refund_service.py\n+++ b/refund_service.py\n@@ refund @@\n"
                    "-if row and build == 'buggy_duplicate': ...\n+return existing refund for same key\n"
                    "-if role != 'refund_operator': return 403\n", encoding="utf-8")
    src = q.api_ok("source", "import", "--story", story, "--uri", str(diff))
    diff_ev = dh.show(q, src.result["source_ref"])["envelope"]["payload"]["content_ref"]
    change = {"repo": "qa-flow-lab", "base_commit": dh.LAB_COMMITS["build1"], "head_commit": d["fix_commit"],
              "diff_ref": diff_ev, "search_scope": {"paths": ["refund_service.py"], "patterns": ["refund("],
                                                    "complete": True},
              "unknown_scope": [], "impact_refs": [d["id"]],
              "callers": [{"symbol": "RefundService.refund", "file": "refund_service.py", "reg_ids": ["REG-1"]},
                          {"symbol": "RefundService.refund(role)", "file": "refund_service.py", "reg_ids": ["REG-2"]}]}
    bad = dict(change, callers=[{"symbol": "x", "file": "y", "reg_ids": ["REG-404"]}])
    rb = q.api("artifact", "submit", "--story", story, "--kind", "change_set", "--file",
               str(write_json(tmp_path / "chg-bad.json", bad)))
    assert rb.rc == 4 and rb.error_code == "REFERENCE_INVALID", rb.describe()
    rc = q.api("artifact", "submit", "--story", story, "--kind", "change_set", "--file",
               str(write_json(tmp_path / "chg.json", change)))
    assert rc.rc == 0, rc.describe()
    chg = dh.show(q, rc.result["artifact_id"])
    assert diff_ev in [x["ref"] for x in chg["envelope"]["depends_on"]], "改动集未绑定 diff 证据"

    # 范围与 fix commit 一致：缺陷的修复提交 = 改动集 head = 复测所在部署的提交
    dep = [s for s in dh.snapshots(q, "deployment") if s["payload"]["build"] == "build3"][-1]["payload"]
    assert d["fix_commit"] == change["head_commit"] == dep["commit"] == dh.LAB_COMMITS["build3"]
    assert d["fix_build"] == orig["build"] == neigh["build"] == "build3"

    # 原问题与相邻回归分别有结果，互不覆盖
    assert orig["id"] != neigh["id"] and orig["run_ref"] != neigh["run_ref"]
    assert orig["outcome"] == "pass" and neigh["outcome"] == "fail"
    assert d["retests"][-1] == orig["id"]
    reg = q.api_ok("execution", "status", "--story", story).json["regression"]
    rows = {r["tc"]: r for r in reg["rows"]}
    assert rows["C1"]["reg_id"] == "REG-1" and rows["C2"]["reg_id"] == "REG-2"
    assert rows["C1"]["status"] == "pass" and rows["C1"]["attempt_ref"] == orig["id"]
    assert rows["C2"]["status"] == "fail" and rows["C2"]["attempt_ref"] == neigh["id"]
    assert rows["C2"]["required"] is True


@pytest.mark.ac("AC-045", "AC-046")
@pytest.mark.layer("lab")
def test_ac046_neighbor_failure_blocks_close_and_full_pass(qa):
    q, story, d, orig, neigh = _neighbor_setup(qa)
    ext = d["external_id"]
    assert orig["outcome"] == "pass" and neigh["outcome"] == "fail"

    seq0, writes = dh.last_seq(q), len(dh.pf_writes(q))
    r = q.api("defect", "close", "--story", story, "--defect", d["id"], "--dispatch")
    assert r.rc == 3 and r.error_code == "PRECONDITION_FAILED", r.describe()
    det = r.errors[0]["details"]
    assert det["can_close"] is False and any("C2" in m for m in det["missing"]), det
    assert not any("原问题" in m for m in det["missing"]), "原问题复测已通过，不应被说成未通过"
    assert len(dh.pf_writes(q)) == writes
    assert not [e for e in dh.events_since(q, seq0) if e["event_type"] in ("defect_transitioned", "action_planned")]

    # 两个结果同时保留；回归门禁与整体结论都不是通过
    after = dh.defect_by_tc(q, "C1")
    assert after["status"] == "resolved" and after["retests"][-1] == orig["id"]
    reg = q.api_ok("execution", "status", "--story", story).json["regression"]
    assert reg["ok"] is False and reg["required_passed"] < reg["required_total"]
    rep = dh.build_report(q, story)
    rows = {x["tc"]: x["status"] for x in rep["test_report"]["latest"]["rows"]}
    assert rows["C1"] == "pass" and rows["C2"] == "fail"
    assert rep["exit_report"]["verdict"] != "pass"
    assert dh.criteria(rep["exit_report"])["regression"]["result"] == "fail"

    # 继续推进也不能关闭原缺陷，不输出整包通过
    res = q.ok("resume")
    notes = "".join(res.result.get("notes") or [])
    assert "C2" in notes and "暂不能关闭" in notes, notes
    statuses = [e["status"] for e in dh.pf_events(q, ext)]
    assert "CLOSED" not in statuses and "RETEST" not in statuses, statuses
    assert dh.defect_by_tc(q, "C1")["status"] == "resolved"
    view = flow.view(q)
    sm = flow.stage_map(view)
    assert view["verdict"] != "pass"
    assert sm["test_execution"]["verdict"] == "fail" and sm["test_exit"]["status"] != "completed"
    text = q.text("status").out
    assert "已准出" not in text and "全部阶段完成" not in text


# ---------------------------------------------------------------------------
# AC-116 / §8 worker 停止与重复 tick
# ---------------------------------------------------------------------------

def _stopped_worker_setup(qa):
    """一条已建单缺陷（附件关联失败留作旧失败）；worker 从未运行。返回 (q, story, 缺陷, 过期后的时钟)。"""
    q = dh.drive_until_crash(qa, dh.STOP_BEFORE_PROPOSE)
    story = dh.story_id(q)
    d = _create_one_defect(q, story, "C1", attach=False)
    q.api_ok("lab", "fault", "--name", "attachment_uploaded_link_failed")
    r = q.api("defect", "attach", "--story", story, "--defect", d["id"], "--dispatch")
    assert (r.json or {}).get("state") == "failed", r.describe()
    q.api_ok("defect", "sync", "--story", story)
    # 来源有效期 72 小时、缺陷状态有效期 24 小时、处理时限 24 小时：推进到 80 小时后
    return q, story, dh.defect_by_tc(q, "C1"), plus_hours(d["created_at"], 80)


@pytest.mark.ac("AC-116")
@pytest.mark.layer("lab")
def test_ac116_status_marks_stale_while_worker_stopped(qa):
    q, story, d, clock = _stopped_worker_setup(qa)
    late = at(q, clock)
    res = late.ok("status")
    assert res.result["worker_last_tick_at"] is None
    text = late.text("status", "--details").out
    assert "worker 未运行" in text
    blob = json.dumps(res.result, ensure_ascii=False) + "\n" + text
    assert any(t in blob for t in STALE_TOKENS), (
        "来源已超过 72 小时、缺陷状态已超过 24 小时未同步，status 没有任何新鲜度不足的标识。"
        f"阶段状态：{[(s['stage'], s['status']) for s in res.result['stages']]}")


@pytest.mark.ac("AC-116")
@pytest.mark.layer("lab")
def test_ac116_two_ticks_single_unsent_reminder_and_old_failure_kept(qa):
    q, story, d, clock = _stopped_worker_setup(qa)
    late = at(q, clock)
    synced_before = q.query("SELECT value FROM meta WHERE key=?", (f"defect_synced_at:{story}",))[0]["value"]
    failed_before = [a for a in dh.actions(q, "defect.attach") if a["state"] == "failed"]
    assert len(failed_before) == 1

    t1 = late.api_ok("worker", "tick", "--once").json
    t2 = late.api_ok("worker", "tick", "--once").json
    assert len(t1["refreshed"]) == 3 and all(x["read_status"] == "available" for x in t1["refreshed"]), t1
    assert t2["refreshed"] == [], "第二次 tick 不应重复刷新刚核对过的来源"
    assert t1["errors"] == [] and t2["errors"] == []

    # 同一截止点只创建一个提醒动作，且未授权不发送
    acts = dh.actions(q, "notify.send")
    assert len(acts) == 1, f"应只有一个提醒动作，实际 {[(a['id'], a['state']) for a in acts]}"
    req = dh.show(q, acts[0]["plan_ref"])["envelope"]["payload"]["request"]
    assert req["defect"] == d["id"] and req["deadline"] == plus_hours(d["created_at"], 24)
    assert acts[0]["state"] == "awaiting_authorization" and acts[0]["attempts"] == 0
    assert [x["action_id"] for x in t1["reminders"]] == [x["action_id"] for x in t2["reminders"]] == [acts[0]["id"]]
    assert dh.pf_query(q, "SELECT * FROM notifications") == [] and dh.pf_calls(q, "notify") == []
    forced = late.api("action", "dispatch", "--id", acts[0]["id"])
    assert forced.rc == 3 and forced.error_code == "AUTHORIZATION_REQUIRED", forced.describe()
    assert dh.pf_calls(q, "notify") == []

    # 最后同步时刻更新
    meta = {r["key"]: r["value"] for r in q.query("SELECT key, value FROM meta")}
    assert meta["worker_last_tick_at"] == clock
    assert meta[f"defect_synced_at:{story}"] == clock and synced_before != clock
    view = late.ok("status").result
    assert view["worker_last_tick_at"] == clock

    # 不掩盖旧失败：附件关联失败与用例失败仍然可见
    failed_after = [a for a in dh.actions(q, "defect.attach") if a["state"] == "failed"]
    assert [a["id"] for a in failed_after] == [a["id"] for a in failed_before]
    assert "关联失败" in failed_after[0]["last_error"]
    assert dh.defect_by_tc(q, "C1")["attachments_verified"] is False
    listed = late.api_ok("action", "list", "--state", "failed").json["actions"]
    assert failed_after[0]["id"] in [a["id"] for a in listed]
    sm = flow.stage_map(view)
    assert sm["test_execution"]["verdict"] == "fail" and view["verdict"] != "pass"
    assert "附件回读未核对" in late.text("status", "--details").out


# ---------------------------------------------------------------------------
# AC-111 真实测试空间（external）
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-111")
@pytest.mark.layer("external")
def test_ac111_real_test_space_defect_lifecycle():
    blocked("没有获准的专用真实测试空间、测试账号与原生审批；实验平台桩的结果不能代替真实平台接入")
