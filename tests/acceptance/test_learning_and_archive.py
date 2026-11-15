"""漏测与改进、知识回流、只读归档、证据到期清理、完整生命周期。

场景：AC-057、AC-058、AC-059、AC-060、AC-105、AC-109、AC-115；AC-110 记 blocked。
独立观测：工作区 SQLite（只读）、实验平台桩账本 lab/platform.db、证据文件、模型桩记录。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from support import flow
from support import metrics_helpers as mh
from support.cli import blocked, sha256_file, write_json


@pytest.fixture(scope="module")
def done(tmp_path_factory):
    """推进到结束的合成实验工作区（模块内只读复用；会改状态的测试使用其副本）。"""
    qa = mh.new_qa(tmp_path_factory.mktemp("完整 流程"))
    flow.drive(qa)
    return qa


@pytest.fixture()
def lab(done, tmp_path):
    return mh.clone_qa(done, tmp_path / "副本")


def _attempts(qa, story):
    return qa.api_ok("execution", "status", "--story", story).json["attempts"]


def _defect_for(qa, tc: str) -> str:
    for s in mh.snapshots(qa, "defect"):
        if s["payload"]["tc_ref"] == tc:
            return s["id"]
    raise AssertionError(f"没有 {tc} 的缺陷")


def _escape_payload(defect_ref: str, **kw):
    p = {"defect_ref": defect_ref, "should_have_caught_stage": "case_design",
         "proposed_cause": "遗漏重复请求场景的测试点", "missing_refs": ["TP-IDEM"], "change_refs": [],
         "verification_refs": [], "confirmed_decision": None}
    p.update(kw)
    return p


def _register_escape(qa, story, tmp: Path, payload: dict, name: str = "esc"):
    return qa.api("learning", "escape", "--story", story, "--file", str(write_json(tmp / f"{name}.json", payload)))


def _register_improvement(qa, story, tmp: Path, payload: dict, name: str):
    return qa.api("learning", "improvement", "--story", story, "--file", str(write_json(tmp / f"{name}.json", payload)))


def _report(qa, story) -> dict:
    return qa.api_ok("learning", "report", "--story", story).json


def _improvement_status(report: dict, imp_ref: str) -> str:
    for row in report["confirmed"] + report["unconfirmed"]:
        for i in row["improvements"]:
            if i["improvement_ref"] == imp_ref:
                return i["status"]
    raise AssertionError(f"报告中没有改进措施 {imp_ref}: {report}")


# ---------------------------------------------------------------------------
# AC-105 完整生命周期
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-105")
@pytest.mark.layer("lab")
def test_lifecycle_reaches_exit_with_knowledge(done):
    v = flow.view(done)
    st = flow.stage_map(v)
    for stage in ("req_gate", "tech_gate", "req_analysis", "test_plan", "case_design", "case_qc", "env_ready",
                  "test_entry", "test_execution", "test_exit", "retrospective"):
        assert st[stage]["status"] == "completed" and st[stage]["verdict"] == "pass", (stage, st[stage])
    # 没有实际生产部署：上线验证不能显示通过
    assert st["release_verify"]["status"] != "completed" and st["release_verify"]["verdict"] != "pass"
    assert v["lab"] is True
    # 两个缺陷（C1、C3）在平台桩上各建 1 个工作项并已关闭
    assert sorted((d["tc"], d["status"]) for d in v["defects"]) == [("C1", "closed"), ("C3", "closed")]
    items = mh.platform_rows(done, "SELECT id, status FROM items ORDER BY id")
    assert [i["status"] for i in items] == ["CLOSED", "CLOSED"], items
    # 知识回流：候选经人工确认后应用，可检索
    story = mh.story_id(done)
    res = done.api_ok("knowledge", "search", "--query", "退款幂等").json
    assert res["results"] and res["results"][0]["confirmation_method"] == "human", res
    assert res["results"][0]["sources"], res
    assert done.query("SELECT COUNT(*) AS n FROM events WHERE event_type='knowledge_applied'")[0]["n"] == 1
    assert story


@pytest.mark.ac("AC-105")
@pytest.mark.layer("lab")
def test_lifecycle_no_exit_during_fault_period(done):
    """独立读取事件与快照：缺陷构建期间没有通过的准出；通过的准出只出现在修复构建且缺陷关闭之后。"""
    reports = mh.exit_reports_by_build(done)
    assert reports, "没有准出报告"
    buggy = [r for r in reports if r["build"] != "build2"]
    assert buggy and all(r["verdict"] != "pass" for r in buggy), buggy
    passed = [r for r in reports if r["verdict"] == "pass"]
    assert passed and all(r["build"] == "build2" for r in passed), passed
    ev = done.query("SELECT seq, object_id, event_type, payload FROM events ORDER BY seq")
    exit_done = [e["seq"] for e in ev if e["event_type"] == "stage_completed" and e["object_id"].endswith(":test_exit")]
    closed = [e["seq"] for e in ev if e["event_type"] == "defect_transitioned" and json.loads(e["payload"]).get("status") == "closed"]
    first_fail_exec = [e["seq"] for e in ev if e["event_type"] == "stage_failed" and e["object_id"].endswith(":test_execution")]
    assert len(closed) == 2 and exit_done and first_fail_exec
    assert min(exit_done) > max(closed) > min(first_fail_exec)
    # 首轮：C1、C3 在缺陷构建上实际失败；修复构建上复测通过
    story = mh.story_id(done)
    atts = _attempts(done, story)
    first = {a["tc"]: a for a in atts if a["build"] == "build1" and a["purpose"] == "execution"}
    assert first["C1"]["outcome"] == "fail" and first["C3"]["outcome"] == "fail"
    assert first["C2"]["outcome"] == "pass"
    latest = {a["tc"]: a for a in atts if a["build"] == "build2"}
    assert all(latest[tc]["outcome"] == "pass" for tc in ("SMOKE-1", "SMOKE-2", "C1", "C2", "C3", "C4", "C5"))
    assert (done.workspace() / "lab" / "target" / "refund.db").is_file(), "应存在独立的实验退款库"


@pytest.mark.ac("AC-105")
@pytest.mark.layer("lab")
def test_lifecycle_outputs_have_upstream_and_evidence(done):
    ws = done.workspace()
    # 每次用例尝试都依赖已登记证据，证据文件存在且摘要一致
    atts = done.query("SELECT id FROM snapshots WHERE kind='case_attempt'")
    assert len(atts) >= 14
    for a in atts:
        deps = done.query("SELECT input_id, relation FROM dependencies WHERE result_id=?", (a["id"],))
        evs = [d["input_id"] for d in deps if d["relation"] == "evidence"]
        assert evs, f"尝试 {a['id']} 没有证据依赖"
        assert any(d["relation"] == "expected" for d in deps), f"尝试 {a['id']} 没有绑定用例设计"
        for e in evs:
            row = done.query("SELECT sha256, rel_path, state FROM evidence WHERE id=?", (e,))[0]
            assert row["state"] == "active"
            assert sha256_file(ws / row["rel_path"]) == row["sha256"]
    # 已完成阶段的判定记录都有上游输入，且输入快照存在
    v = flow.view(done)
    for s in v["stages"]:
        if s["status"] != "completed":
            continue
        deps = done.query("SELECT input_id, input_sha256 FROM dependencies WHERE result_id=?", (s["evaluation_ref"],))
        assert deps, f"阶段 {s['stage']} 的判定 {s['evaluation_ref']} 没有上游输入"
        for d in deps:
            if d["input_id"].startswith("EV-"):
                continue
            snap = done.query("SELECT sha256 FROM snapshots WHERE id=?", (d["input_id"],))
            assert snap and snap[0]["sha256"] == d["input_sha256"], (s["stage"], d)
    # 缺陷绑定实际失败的尝试
    for d in mh.snapshots(done, "defect"):
        dep = done.query("SELECT input_id FROM dependencies WHERE result_id=?", (d["id"],))
        assert dep and dep[0]["input_id"].startswith("ATT-"), d


@pytest.mark.ac("AC-105")
@pytest.mark.layer("lab")
def test_lifecycle_no_real_external_writes(done):
    acts = done.query("SELECT kind, target_id, environment, state FROM actions")
    assert acts and all(a["environment"] == "lab" for a in acts), acts
    assert {a["target_id"] for a in acts} <= {"lab-refund", "lab-platform"}
    assert all(a["state"] == "succeeded" for a in acts), acts
    cfg = json.loads((done.workspace() / "config" / "qa-flow.json").read_text(encoding="utf-8"))
    assert cfg["profile"] == "lab" and all(t["environment"] == "lab" for t in cfg["targets"])
    calls = [json.loads(line) for line in (done.workspace() / "lab" / "model_calls.jsonl").read_text("utf-8").splitlines()]
    assert calls, "没有模型桩记录"
    provs = mh.snapshots(done, "run_provenance")
    assert provs and all(p["payload"]["model"]["provider"] == "lab_stub" for p in provs)
    # 被测进程使用独立 HOME，全流程没有在其中留下任何文件
    assert not [p for p in done.home.rglob("*") if p.is_file()]
    # 平台写入只发生在工作区内的平台桩账本
    writes = mh.platform_rows(done, "SELECT op, outcome FROM calls WHERE op IN ('create','upload','link','transition')")
    assert sum(1 for w in writes if w["op"] == "create" and w["outcome"] == "ok") == 2


# ---------------------------------------------------------------------------
# AC-057 / AC-058 漏测与改进
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-057")
@pytest.mark.layer("lab")
def test_escape_confirmation_and_chain(lab, tmp_path):
    """登记 ESC → 人工确认 → 关联修订与修复构建上的实际通过 → 措施已验证。"""
    story = mh.story_id(lab)
    bug = _defect_for(lab, "C1")
    design = mh.snapshots(lab, "case_design")[-1]["id"]
    atts = _attempts(lab, story)
    retest_pass = next(a["id"] for a in atts if a["tc"] == "C1" and a["build"] == "build2" and a["outcome"] == "pass")
    r = _register_escape(lab, story, tmp_path, _escape_payload(bug, change_refs=[design]))
    assert r.rc == 0, r.describe()
    esc, handle = r.result["escape_ref"], r.result["confirmation_handle"]
    rep = _report(lab, story)
    assert rep["confirmed_count"] == 0 and [u["escape_ref"] for u in rep["unconfirmed"]] == [esc]
    # 人工确认归因
    c = lab.run("review", "--answer", f"{handle}=A")
    assert c.rc == 0, "测试人员无法确认漏测归因\n" + c.describe()
    rep = _report(lab, story)
    assert rep["confirmed_count"] == 1 and [u["escape_ref"] for u in rep["confirmed"]] == [esc], rep
    assert rep["confirmed"][0]["defect_ref"] == bug
    # 改进措施：关联设计修订与修复构建上的实际通过
    i = _register_improvement(lab, story, tmp_path, {"escape_ref": esc, "measure": "新增用例覆盖同键重复请求",
                                                     "change_refs": [design], "verification_refs": [retest_pass]}, "imp")
    assert i.rc == 0, i.describe()
    rep = _report(lab, story)
    assert _improvement_status(rep, i.result["improvement_ref"]) == "verified", rep
    # 关联链：改进 → 漏测 → 缺陷
    deps = lab.query("SELECT input_id FROM dependencies WHERE result_id=?", (i.result["improvement_ref"],))
    assert esc in [d["input_id"] for d in deps]
    assert mh.payload(lab, esc)["defect_ref"] == bug


@pytest.mark.ac("AC-057")
@pytest.mark.layer("lab")
def test_improvement_not_verified_without_actual_pass(lab, tmp_path):
    """没有执行、执行失败或引用不存在的尝试时，措施不得标为已验证。"""
    story = mh.story_id(lab)
    bug = _defect_for(lab, "C1")
    design = mh.snapshots(lab, "case_design")[-1]["id"]
    atts = _attempts(lab, story)
    failed = next(a["id"] for a in atts if a["tc"] == "C1" and a["outcome"] == "fail")
    passed = next(a["id"] for a in atts if a["tc"] == "C1" and a["build"] == "build2" and a["outcome"] == "pass")
    esc = _register_escape(lab, story, tmp_path, _escape_payload(bug)).result["escape_ref"]

    def status(name, change_refs, verification_refs):
        r = _register_improvement(lab, story, tmp_path, {"escape_ref": esc, "measure": name, "change_refs": change_refs,
                                                         "verification_refs": verification_refs}, name)
        assert r.rc == 0, r.describe()
        return _improvement_status(_report(lab, story), r.result["improvement_ref"])

    assert status("no-run", [design], []) != "verified"
    assert status("failed-run", [design], [failed]) != "verified"
    assert status("forged-ref", [design], ["ATT-DOES-NOT-EXIST"]) != "verified"
    assert status("no-change", [], [passed]) != "verified"
    assert status("real-pass", [design], [passed]) == "verified"


@pytest.mark.ac("AC-057")
@pytest.mark.layer("lab")
def test_improvement_requires_pass_on_fix_build(lab, tmp_path):
    """验证必须来自修复构建：缺陷构建上其他用例的通过不能证明措施有效。"""
    story = mh.story_id(lab)
    bug = _defect_for(lab, "C1")
    design = mh.snapshots(lab, "case_design")[-1]["id"]
    atts = _attempts(lab, story)
    unrelated_old_pass = next(a["id"] for a in atts if a["tc"] == "C2" and a["build"] == "build1" and a["outcome"] == "pass")
    esc = _register_escape(lab, story, tmp_path, _escape_payload(bug)).result["escape_ref"]
    r = _register_improvement(lab, story, tmp_path, {"escape_ref": esc, "measure": "新增用例", "change_refs": [design],
                                                     "verification_refs": [unrelated_old_pass]}, "old-build")
    assert r.rc == 0, r.describe()
    st = _improvement_status(_report(lab, story), r.result["improvement_ref"])
    assert st != "verified", f"缺陷构建 build1 上 C2 的通过被当作措施已验证：{st}"


@pytest.mark.ac("AC-057")
@pytest.mark.layer("lab")
def test_escape_must_bind_existing_defect(lab, tmp_path):
    """漏测记录必须绑定有效缺陷：引用不存在的缺陷应拒绝且不留下漏测记录。"""
    story = mh.story_id(lab)
    before = lab.query("SELECT COUNT(*) AS n FROM snapshots WHERE kind='escape'")[0]["n"]
    r = _register_escape(lab, story, tmp_path, _escape_payload("BUG-DOES-NOT-EXIST"), "bad")
    after = lab.query("SELECT COUNT(*) AS n FROM snapshots WHERE kind='escape'")[0]["n"]
    assert r.rc == 4 and r.error_code == "REFERENCE_INVALID", "引用不存在的缺陷仍被接受\n" + r.describe()
    assert after == before
    # 改进措施引用不存在的漏测记录：拒绝
    i = _register_improvement(lab, story, tmp_path, {"escape_ref": "ESC-NOPE", "measure": "x", "change_refs": [],
                                                     "verification_refs": []}, "bad-imp")
    assert i.rc != 0, i.describe()


@pytest.mark.ac("AC-058")
@pytest.mark.layer("offline")
def test_unconfirmed_escape_listed_separately(lab, tmp_path):
    story = mh.story_id(lab)
    bug = _defect_for(lab, "C3")
    design = mh.snapshots(lab, "case_design")[-1]["id"]
    # Agent 提议归因，并在载荷里自称已有确认决定
    r = _register_escape(lab, story, tmp_path, _escape_payload(bug, confirmed_decision="DEC-SELF-CLAIMED",
                                                               change_refs=[design]))
    assert r.rc == 0, r.describe()
    esc = r.result["escape_ref"]
    passed = next(a["id"] for a in _attempts(lab, story) if a["tc"] == "C3" and a["build"] == "build2" and a["outcome"] == "pass")
    i = _register_improvement(lab, story, tmp_path, {"escape_ref": esc, "measure": "新增 TC 覆盖幂等冲突序列",
                                                     "change_refs": [design], "verification_refs": [passed]}, "imp")
    assert i.rc == 0, i.describe()
    rep = _report(lab, story)
    assert rep["confirmed_count"] == 0 and rep["confirmed"] == [], f"未经人确认的归因被计入确认漏测：{rep}"
    assert [u["escape_ref"] for u in rep["unconfirmed"]] == [esc]
    # 新增 TC 不能变成“已减少漏测”的结论
    text = json.dumps(rep, ensure_ascii=False).replace("不代表已减少漏测", "")
    for claim in ("已减少漏测", "漏测减少", "漏测率下降", "漏测下降"):
        assert claim not in text, rep
    assert not any(k for k in mh.all_keys(rep) if "reduc" in k.lower() or "rate" in k.lower()), rep
    # 没有任何确认决定被登记
    assert lab.query("SELECT COUNT(*) AS n FROM decisions WHERE subjects LIKE ?", (f"%{esc}%",))[0]["n"] == 0


@pytest.mark.ac("AC-058")
@pytest.mark.layer("offline")
def test_rejected_escape_not_counted(lab, tmp_path):
    story = mh.story_id(lab)
    r = _register_escape(lab, story, tmp_path, _escape_payload(_defect_for(lab, "C3")))
    assert r.rc == 0, r.describe()
    lab.ok("review", "--answer", f"{r.result['confirmation_handle']}=reject")
    rep = _report(lab, story)
    assert rep["confirmed_count"] == 0 and len(rep["unconfirmed"]) == 1, rep


# ---------------------------------------------------------------------------
# AC-059 / AC-060 知识回流
# ---------------------------------------------------------------------------

def _candidate(evidence_ref: str | None, text: str, **kw):
    c = {"material_type": "rule",
         "claims": [{"text": text, "claim_type": "fact", "evidence_refs": [evidence_ref] if evidence_ref else [],
                     "environment": "lab"}],
         "sources": [evidence_ref] if evidence_ref else [], "conflicts": [], "confirmation_method": "human",
         "risk": "low"}
    c.update(kw)
    return c


def _propose(qa, story, tmp: Path, cand: dict, name: str):
    return qa.api("knowledge", "propose", "--story", story, "--file", str(write_json(tmp / f"{name}.json", cand)))


def _head(qa) -> dict | None:
    rows = mh.snapshots(qa, "knowledge_snapshot")
    return rows[-1] if rows else None


def _search(qa, query: str) -> list[dict]:
    return qa.api_ok("knowledge", "search", "--query", query).json["results"]


def _lab_evidence(qa) -> str:
    return qa.query("SELECT id FROM evidence WHERE origin='local_lab' AND state='active' ORDER BY created_at LIMIT 1")[0]["id"]


@pytest.mark.ac("AC-059")
@pytest.mark.layer("lab")
def test_confirmed_knowledge_is_searchable_for_next_requirement(lab, tmp_path):
    story = mh.story_id(lab)
    ev = _lab_evidence(lab)
    old = _head(lab)
    assert old is not None
    text = "撤销退款标记KX59：只读角色提交退款返回 403 且无副作用"
    r = _propose(lab, story, tmp_path, _candidate(ev, text), "kc")
    assert r.rc == 0 and r.result["status"] == "candidate", r.describe()
    cand = r.result["candidate_ref"]
    # 人确认之前：应用不生效，检索不到
    assert lab.api_ok("knowledge", "apply", "--story", story).result["applied"] == []
    assert _search(lab, "KX59") == [] and _head(lab)["id"] == old["id"]
    lab.ok("review", "--answer", f"{mh.review_handle(lab, 'KX59')}=A")
    applied = lab.api_ok("knowledge", "apply", "--story", story).result["applied"]
    assert [a["candidate_ref"] for a in applied] == [cand], applied
    new = _head(lab)
    assert new["id"] != old["id"] and new["payload"]["parent_ref"] == old["id"]
    # 原知识快照保留且字节未变
    kept = [s for s in mh.snapshots(lab, "knowledge_snapshot") if s["id"] == old["id"]]
    assert kept and kept[0]["sha256"] == old["sha256"]
    assert all(e in new["payload"]["entries"] for e in old["payload"]["entries"])
    # 为下一合成需求检索
    nxt = lab.cwd / "下一需求 撤销退款.md"
    nxt.write_text("# 撤销退款（合成数据）\n\n只读角色不得撤销退款。\n", encoding="utf-8")
    s = lab.run("start", "--source", str(nxt), "--title", "撤销退款（合成数据）")
    assert s.rc == 0, s.describe()
    hits = _search(lab, "KX59")
    assert len(hits) == 1, hits
    hit = hits[0]
    assert hit["text"] == text
    assert hit["sources"] == [ev] and hit["evidence_refs"] == [ev], "应能定位来源"
    assert hit["candidate_ref"] == cand and hit["story_id"] == story
    assert hit["confirmation_method"] == "human" and hit["decision_ref"], "应能定位确认方式"
    dec = lab.query("SELECT role, result, verification FROM decisions WHERE id=?", (hit["decision_ref"],))
    assert dec and dec[0]["result"] != "reject"
    assert lab.query("SELECT COUNT(*) AS n FROM aggregates WHERE kind='task'")[0]["n"] == 2


@pytest.mark.ac("AC-059")
@pytest.mark.layer("lab")
def test_rejected_knowledge_not_applied(lab, tmp_path):
    story = mh.story_id(lab)
    old = _head(lab)["id"]
    r = _propose(lab, story, tmp_path, _candidate(_lab_evidence(lab), "被否决条目KX59R"), "kc")
    assert r.rc == 0, r.describe()
    lab.ok("review", "--answer", f"{mh.review_handle(lab, 'KX59R')}=reject")
    assert lab.api_ok("knowledge", "apply", "--story", story).result["applied"] == []
    assert _search(lab, "KX59R") == [] and _head(lab)["id"] == old


def _assert_stays_pending(qa, story, tmp_path, cand: dict, marker: str, name: str):
    old = _head(qa)["id"]
    r = _propose(qa, story, tmp_path, cand, name)
    assert r.rc == 0, r.describe()
    assert r.result["status"] == "candidate", f"按自动策略申请入库后应保持待确认：{r.result}"
    applied = qa.api_ok("knowledge", "apply", "--story", story).result["applied"]
    assert applied == [], applied
    assert _search(qa, marker) == [], "未确认条目不应可检索"
    assert _head(qa)["id"] == old, "知识快照不应变化"
    events = qa.query("SELECT event_type FROM events WHERE object_id LIKE ?", (f"%{r.result['candidate_ref']}",))
    assert "knowledge_applied" not in [e["event_type"] for e in events]
    assert "knowledge_policy_confirmed" not in [e["event_type"] for e in events]


@pytest.mark.ac("AC-060")
@pytest.mark.layer("offline")
def test_conflicting_candidate_not_auto_applied(lab, tmp_path):
    story = mh.story_id(lab)
    cand = _candidate(_lab_evidence(lab), "冲突条目KX60A：退款金额可以为 0", confirmation_method="policy",
                      conflicts=["已确认规则：退款金额必须大于 0"])
    _assert_stays_pending(lab, story, tmp_path, cand, "KX60A", "conflict")
    assert not [p for p in lab.home.rglob("*") if p.is_file()], "不得写入工作区之外的共享知识库"


@pytest.mark.ac("AC-060")
@pytest.mark.layer("offline")
def test_unproven_production_fact_not_auto_applied(lab, tmp_path):
    story = mh.story_id(lab)
    cand = _candidate(None, "线上事实KX60B：生产已出现重复退款", confirmation_method="policy")
    cand["claims"][0]["environment"] = "production"
    _assert_stays_pending(lab, story, tmp_path, cand, "KX60B", "prod-no-evidence")
    cand2 = _candidate(_lab_evidence(lab), "线上事实KX60C：生产已出现重复退款", confirmation_method="policy")
    cand2["claims"][0]["environment"] = "production"  # 证据只来自实验环境
    _assert_stays_pending(lab, story, tmp_path, cand2, "KX60C", "prod-lab-evidence")


@pytest.mark.ac("AC-060")
@pytest.mark.layer("offline")
def test_inferred_production_claim_not_auto_applied(lab, tmp_path):
    """仅由推断得出的线上结论按自动策略申请入库：应保持待确认。"""
    story = mh.story_id(lab)
    cand = _candidate(None, "线上推断KX60D：生产大概也会重复退款", confirmation_method="policy")
    cand["claims"][0].update({"claim_type": "inference", "environment": "production"})
    _assert_stays_pending(lab, story, tmp_path, cand, "KX60D", "prod-inference")


@pytest.mark.ac("AC-060")
@pytest.mark.layer("offline")
def test_self_declared_runtime_proof_not_auto_applied(lab, tmp_path):
    """候选自称“已有运行证据”，但引用的证据并不存在：不得按策略自动入库为已证实事实。"""
    story = mh.story_id(lab)
    cand = _candidate("EV-DOES-NOT-EXIST", "线上事实KX60E：生产已出现重复退款", confirmation_method="policy")
    cand["claims"][0].update({"environment": "production", "verified_runtime": True})
    _assert_stays_pending(lab, story, tmp_path, cand, "KX60E", "prod-self-declared")


# ---------------------------------------------------------------------------
# AC-109 归档
# ---------------------------------------------------------------------------

def _archive(qa, story, report, *extra):
    return qa.api("archive", "plan", "--story", story, "--report", report, "--space", "lab-qa-archive", *extra)


def _stage_state(qa):
    return qa.query("SELECT object_id, revision, state FROM aggregates WHERE kind='stage' ORDER BY object_id")


def _docs(qa):
    return mh.platform_rows(qa, "SELECT id, space, title, content, etag, complete FROM docs ORDER BY id")


def _doc_writes(qa):
    return mh.platform_rows(qa, "SELECT op, outcome FROM calls WHERE op IN ('doc_create','doc_update') ORDER BY seq")


def _approve(qa, needle: str = "归档文档"):
    qa.ok("review", "--answer", f"{mh.review_handle(qa, needle)}=A")


@pytest.mark.ac("AC-109")
@pytest.mark.layer("lab")
def test_archive_success_repeat_and_conflict(lab, tmp_path):
    story = mh.story_id(lab)
    d1 = mh.snapshots(lab, "case_design")[-1]  # 已登记终稿 D1
    report = mh.snapshots(lab, "test_report")[-1]
    stages0 = _stage_state(lab)
    verdict0 = [(s["stage"], s["status"], s["verdict"]) for s in flow.view(lab)["stages"]]
    lineage0 = lab.query("SELECT object_id, revision, state FROM aggregates WHERE kind='lineage' ORDER BY object_id")

    # 未获准：只登记计划，不发送
    p = _archive(lab, story, report["id"])
    assert p.rc == 0 and p.json["action"]["state"] == "awaiting_authorization", p.describe()
    r = _archive(lab, story, report["id"], "--dispatch")
    assert r.rc == 3 and r.error_code == "AUTHORIZATION_REQUIRED", r.describe()
    assert _docs(lab) == [] and _doc_writes(lab) == []

    # 获准后成功归档
    _approve(lab)
    r = _archive(lab, story, report["id"], "--dispatch")
    assert r.rc == 0 and r.json["state"] == "succeeded", r.describe()
    docs = _docs(lab)
    assert len(docs) == 1 and docs[0]["space"] == "lab-qa-archive" and docs[0]["complete"] == 1
    assert docs[0]["id"] == r.json["remote_object_id"]
    recs = mh.snapshots(lab, "archive_record")
    assert len(recs) == 1
    assert recs[0]["payload"]["report_ref"] == report["id"] and recs[0]["payload"]["report_sha256"] == report["sha256"]
    assert recs[0]["payload"]["remote_object_id"] == docs[0]["id"]

    def unchanged():
        assert _stage_state(lab) == stages0, "归档改变了阶段状态或修订"
        assert [(s["stage"], s["status"], s["verdict"]) for s in flow.view(lab)["stages"]] == verdict0
        assert lab.query("SELECT object_id, revision, state FROM aggregates WHERE kind='lineage' ORDER BY object_id") == lineage0
        cur = mh.snapshots(lab, "case_design")
        assert len(cur) == 1 and cur[0]["sha256"] == d1["sha256"], "终稿 D1 被改动"
        assert mh.payload(lab, d1["id"]) == d1["payload"]
        assert [s for s in mh.snapshots(lab, "test_report") if s["id"] == report["id"]][0]["sha256"] == report["sha256"]
        assert "feishu_doc_url" not in json.dumps(mh.payload(lab, d1["id"]))

    unchanged()

    # 重复归档：不重写
    r2 = _archive(lab, story, report["id"], "--dispatch")
    assert r2.rc == 0 and r2.json["state"] == "succeeded" and r2.json.get("replayed") is True, r2.describe()
    assert [w["op"] for w in _doc_writes(lab)] == ["doc_create"]
    assert _docs(lab) == docs and len(mh.snapshots(lab, "archive_record")) == 1
    unchanged()

    # 冲突归档：远端文档已变化（ETag 不匹配）则停止，不保存成功快照
    c = _archive(lab, story, report["id"], "--existing-doc", docs[0]["id"], "--if-match", "0000stale0000etag")
    assert c.rc == 0 and c.json["action"]["state"] == "awaiting_authorization", c.describe()
    _approve(lab, "existing_doc")
    c2 = _archive(lab, story, report["id"], "--existing-doc", docs[0]["id"], "--if-match", "0000stale0000etag", "--dispatch")
    assert c2.json["state"] == "failed" and "ETag" in (c2.json.get("error") or ""), c2.describe()
    assert _docs(lab) == docs, "冲突时远端文档不应被改写"
    assert len(mh.snapshots(lab, "archive_record")) == 1, "冲突归档不得保存成功快照"
    act = lab.query("SELECT state, remote_object_id FROM actions WHERE kind='doc.archive' ORDER BY created_at, id")
    assert sorted(a["state"] for a in act) == ["failed", "succeeded"], act
    unchanged()


@pytest.mark.ac("AC-109")
@pytest.mark.layer("lab")
def test_archive_etag_conflict_fault_with_matching_etag(lab, tmp_path):
    """平台桩注入 ETag 冲突：即使本地持有的 ETag 匹配，也停止且不记成功。"""
    story = mh.story_id(lab)
    report = mh.snapshots(lab, "test_report")[-1]
    _archive(lab, story, report["id"])
    _approve(lab)
    first = _archive(lab, story, report["id"], "--dispatch")
    assert first.json["state"] == "succeeded", first.describe()
    doc = _docs(lab)[0]
    args = ("--existing-doc", doc["id"], "--if-match", doc["etag"])
    _archive(lab, story, report["id"], *args)
    _approve(lab, "existing_doc")
    f = lab.test_mode().api("lab", "fault", "--name", "etag_conflict")
    assert f.rc == 0, f.describe()
    c = _archive(lab, story, report["id"], *args, "--dispatch")
    assert c.json["state"] == "failed", c.describe()
    assert _docs(lab)[0] == doc and len(mh.snapshots(lab, "archive_record")) == 1


@pytest.mark.ac("AC-109")
@pytest.mark.layer("lab")
def test_archive_rejects_unapproved_space(lab):
    story = mh.story_id(lab)
    report = mh.snapshots(lab, "test_report")[-1]
    before = lab.query("SELECT COUNT(*) AS n FROM actions")[0]["n"]
    r = lab.api("archive", "plan", "--story", story, "--report", report["id"], "--space", "prd-space", "--dispatch")
    assert r.rc == 3 and r.error_code == "PROTECTED_RESOURCE", r.describe()
    assert lab.query("SELECT COUNT(*) AS n FROM actions")[0]["n"] == before
    assert _docs(lab) == []


# ---------------------------------------------------------------------------
# AC-115 证据到期清理
# ---------------------------------------------------------------------------

def _expire_setup(lab):
    story = mh.story_id(lab)
    att = lab.api_ok("execution", "status", "--story", story).json["latest"]["C1"]["attempt"]
    e1 = next(d["input_id"] for d in lab.query("SELECT input_id, relation FROM dependencies WHERE result_id=?", (att,))
              if d["relation"] == "evidence")
    row = lab.query("SELECT * FROM evidence WHERE id=?", (e1,))[0]
    return story, att, e1, row


@pytest.mark.ac("AC-115")
@pytest.mark.layer("lab")
def test_evidence_expiry_marks_dependents_and_survives_backup(lab, tmp_path):
    story, att, e1, row = _expire_setup(lab)
    ws = lab.workspace()
    assert row["state"] == "active" and (ws / row["rel_path"]).is_file()
    att_sha = lab.query("SELECT sha256 FROM snapshots WHERE id=?", (att,))[0]["sha256"]
    st0 = flow.stage_map(flow.view(lab))
    assert st0["test_execution"]["status"] == "completed" and st0["test_exit"]["status"] == "completed"
    exit_dec = lab.query("SELECT id FROM decisions WHERE subjects LIKE '%EXT-%' AND result='approve' ORDER BY created_at DESC LIMIT 1")[0]["id"]
    assert lab.api_ok("decision", "verify", "--id", exit_dec).json["valid"] is True
    snap_count = lab.query("SELECT COUNT(*) AS n FROM snapshots")[0]["n"]

    r = lab.run("admin", "expire-evidence", "--id", e1, "--reason", "保留期届满，按策略销毁")
    assert r.rc == 0 and r.result["state"] == "expired", r.describe()

    # 销毁事件与摘要
    now = lab.query("SELECT * FROM evidence WHERE id=?", (e1,))[0]
    assert now["state"] == "expired" and now["destroyed_at"] and now["destroy_reason"] == "保留期届满，按策略销毁"
    assert now["sha256"] == row["sha256"], "历史引用应保留摘要"
    ev = lab.query("SELECT payload FROM events WHERE object_id=? AND event_type='evidence_destroyed'", (e1,))
    assert len(ev) == 1 and json.loads(ev[0]["payload"])["sha256"] == row["sha256"]
    assert not (ws / row["rel_path"]).exists(), "原始对象应已销毁"

    # 依赖的当前验证过期，批准不再有效
    v = flow.view(lab)
    st = flow.stage_map(v)
    assert st["test_execution"]["status"] != "completed" and st["test_exit"]["status"] != "completed", st
    assert v["verdict"] != "pass" or "准出" not in v["summary"]["headline"], v["summary"]
    assert "测试准出" not in v["summary"]["have"] and "测试执行" not in v["summary"]["have"]
    assert lab.api_ok("decision", "verify", "--id", exit_dec).json["valid"] is False

    # 旧历史事实保留
    assert lab.query("SELECT sha256 FROM snapshots WHERE id=?", (att,))[0]["sha256"] == att_sha
    assert lab.query("SELECT COUNT(*) AS n FROM snapshots")[0]["n"] >= snap_count

    # 重复清理不产生第二个销毁事件
    again = lab.run("admin", "expire-evidence", "--id", e1, "--reason", "重复")
    assert again.rc == 0, again.describe()
    assert lab.query("SELECT COUNT(*) AS n FROM events WHERE object_id=? AND event_type='evidence_destroyed'", (e1,))[0]["n"] == 1

    # 创建并恢复备份：销毁记录保留，已销毁证据不会被标为可用
    bk = tmp_path / "备份 目录"
    b = lab.api("backup", "create", "--output", str(bk))
    assert b.rc == 0 and b.json["verification"]["ok"] is True, b.describe()
    manifest = json.loads((bk / "backup-manifest.json").read_text(encoding="utf-8"))
    entry = [e for e in manifest["evidence"] if e["id"] == e1]
    assert entry and entry[0]["state"] == "expired" and entry[0].get("destroyed_at"), entry
    assert not list(bk.rglob(row["sha256"])), "备份不得包含已销毁的原始对象"
    restored = tmp_path / "恢复 目录"
    rs = lab.api("backup", "restore", "--from", str(bk), "--to", str(restored))
    assert rs.rc == 0 and rs.json["verification"]["ok"] is True, rs.describe()
    conn = sqlite3.connect(f"file:{restored / 'qa.db'}?mode=ro", uri=True)
    try:
        state = conn.execute("SELECT state, sha256, destroy_reason FROM evidence WHERE id=?", (e1,)).fetchone()
        destroyed = conn.execute("SELECT COUNT(*) FROM events WHERE object_id=? AND event_type='evidence_destroyed'",
                                 (e1,)).fetchone()[0]
    finally:
        conn.close()
    assert state[0] == "expired" and state[1] == row["sha256"] and state[2], state
    assert destroyed == 1
    assert not list(restored.rglob(row["sha256"]))
    rq = mh.QA(restored, lab.home, lab.cwd)
    rv = rq.ok("status").result
    rst = flow.stage_map(rv)
    assert rst["test_execution"]["status"] != "completed" and rst["test_exit"]["status"] != "completed", rst


@pytest.mark.ac("AC-115")
@pytest.mark.layer("lab")
def test_evidence_expiry_is_visible_to_reader(lab):
    """查询依赖状态时应明确说明证据已销毁、不再可读取（而不只是显示“已过期”）。"""
    story, att, e1, row = _expire_setup(lab)
    lab.ok("admin", "expire-evidence", "--id", e1, "--reason", "保留期届满")
    js = lab.ok("status", "--details").json
    text = lab.text("status", "--details").out
    combined = json.dumps(js, ensure_ascii=False) + text
    assert e1 in combined or "销毁" in combined or "不再可读取" in combined or "不可读取" in combined, (
        "status 与 --details 均未说明证据已销毁：\n" + text)


@pytest.mark.ac("AC-115")
@pytest.mark.layer("lab")
def test_expire_unknown_evidence_rejected(lab):
    before = lab.query("SELECT MAX(seq) AS n FROM events")[0]["n"]
    states = lab.query("SELECT id, state FROM evidence ORDER BY id")
    r = lab.run("admin", "expire-evidence", "--id", "EV-DOES-NOT-EXIST", "--reason", "x")
    assert r.rc == 3 and r.error_code == "NOT_FOUND", r.describe()
    # 允许留下拒绝审计，但不得有销毁或阶段过期事件，证据状态不变
    new = [e["event_type"] for e in lab.query("SELECT event_type FROM events WHERE seq>?", (before,))]
    assert not [t for t in new if t in ("evidence_destroyed", "stage_stale")], new
    assert lab.query("SELECT id, state FROM evidence ORDER BY id") == states


# ---------------------------------------------------------------------------
# AC-110 真实模型（受阻）
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-110")
@pytest.mark.layer("model")
def test_real_model_limited_sample():
    blocked("没有获准的真实模型凭证与发送授权；12 个独立标注样本的人工比对也需要真人参与")
