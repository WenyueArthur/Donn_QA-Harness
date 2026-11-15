"""批次 M3 独立验收：计划、测试点、技术模型、影响、策略、用例、覆盖、质检与变更。

对应场景 AC-005 … AC-024、AC-106。全部通过子进程调用公共 CLI；
预期值来自规格、合成夹具或本文件/辅助文件中的简短参考模型。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

import pytest

from support import flow
from support import planning_helpers as h
from support.cli import QA

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def gated(qa: QA) -> tuple[QA, str]:
    """demo 初始状态：需求质检与技术方案质检已完成，测试分析等待业务答复。"""
    qa.ok("demo")
    story = h.story_id(qa)
    st = h.stage_statuses(qa, story)
    assert st["req_gate"] == "completed" and st["tech_gate"] == "completed", st
    return qa, story


@pytest.fixture()
def designed(qa: QA) -> tuple[QA, str]:
    """推进到用例设计完成：测试计划待批准，脚本尚未登记，尚无任何执行。"""
    flow.drive(qa, until=flow.until_stage("case_design"))
    story = h.story_id(qa)
    st = h.stage_statuses(qa, story)
    assert st["req_analysis"] == "completed" and st["case_design"] == "completed", st
    return qa, story


@pytest.fixture()
def planned(qa: QA) -> tuple[QA, str]:
    """推进到测试计划已批准。"""
    flow.drive(qa, until=flow.until_stage("test_plan"))
    story = h.story_id(qa)
    assert h.stage_statuses(qa, story)["test_plan"] == "completed"
    return qa, story


@pytest.fixture(scope="module")
def full(tmp_path_factory: pytest.TempPathFactory) -> tuple[QA, str]:
    """完整跑完的合成实验（只供只读检查与不影响他人的校验使用）。"""
    qa = h.make_qa(tmp_path_factory.mktemp("m3-full"))
    flow.drive(qa)
    story = h.story_id(qa)
    st = h.stage_statuses(qa, story)
    assert st["test_exit"] == "completed", st
    return qa, story


def _requirement_src(qa: QA) -> str:
    ta = h.latest(qa, "test_analysis")
    return ta["payload"]["requirements"][0]["citations"][0]["source_ref"]


# ---------------------------------------------------------------------------
# AC-005 计划有区间和容量校验
# ---------------------------------------------------------------------------


@pytest.mark.ac("AC-005")
@pytest.mark.layer("offline")
def test_ac005_plan_within_capacity_keeps_estimate_separate_from_actual(designed, tmp_path: Path) -> None:
    qa, story = designed
    old = h.latest(qa, "test_plan")
    analysis = h.latest(qa, "test_analysis")
    plan = h.base_plan()
    assert h.reference_capacity_conflicts(plan) == []  # 参考模型：6 小时需求 ≤ 6 小时容量

    r = h.plan_submit(qa, story, plan, tmp_path, parent=old["id"])
    assert r.rc == 0 and r.json["committed"] is True, r.describe()
    ev = r.result["derived"]["plan_evaluation"]
    assert ev["capacity_conflicts"] == [] and ev["problems"] == [] and ev["approvable"] is True
    assert ev["estimate_is_measurement"] is False  # 不声称已有实测工时

    snap = h.envelope(h.row(qa, r.result["artifact_id"]))
    assert snap["payload"] == plan  # 方法、依赖、责任人与估算区间原样保留
    assert snap["payload"]["estimate_method"].strip() and snap["payload"]["tasks"][0]["owner"] == "alice"
    dep_refs = {d["ref"] for d in snap["depends_on"]}
    assert analysis["id"] in dep_refs, "计划应可追溯到所依据的测试分析"
    assert h.row(qa, r.result["artifact_id"])["parent_id"] == old["id"]
    assert h.count(qa, "effort_entry") == 0, "登记估算不应产生任何实际工时记录"

    # 未经责任人确认不能完成；确认问题明确说明估算不是实测
    st = h.stage_eval(qa, story, "test_plan")
    assert st["status"] == "blocked" and st["verdict"] == "indeterminate" and st["decisions"] == []
    mine = [c for c in h.reviews(qa)["mine"] if c["kind"] == "测试计划批准"
            and c["basis"][0]["source_ref"] == r.result["artifact_id"]]
    assert len(mine) == 1 and mine[0]["responsible_role"] == "tester"
    assert "不是实测" in mine[0]["business_question"]
    qa.ok("review", "--answer", f"{mine[0]['handle']}=A")
    st = h.stage_eval(qa, story, "test_plan")
    assert st["status"] == "completed" and st["verdict"] == "pass" and len(st["decisions"]) == 1
    dec = h.decisions_on(qa, r.result["artifact_id"])
    assert [d["result"] for d in dec] == ["approve"]
    assert json.loads(dec[0]["subjects"])[0]["sha256"] == h.row(qa, r.result["artifact_id"])["sha256"]

    # 实际投入另行登记，不改写估算
    before = h.row(qa, r.result["artifact_id"])
    e = h.submit(qa, story, "effort_entry", {"entry_id": "W1", "person": "alice", "hours": 5, "task_ref": "T1"},
                 tmp_path, "--name", "W1")
    assert e.rc == 0, e.describe()
    assert e.result["kind"] == "effort_entry" and e.result["artifact_id"] != r.result["artifact_id"]
    assert h.row(qa, r.result["artifact_id"]) == before
    assert h.latest(qa, "test_plan")["payload"]["tasks"][0]["estimate_hours"] == {"low": 4, "likely": 6, "high": 10}
    assert h.stage_eval(qa, story, "test_plan")["status"] == "completed"


@pytest.mark.ac("AC-005")
@pytest.mark.layer("offline")
def test_ac005_invalid_estimates_are_rejected_without_side_effects(designed, tmp_path: Path) -> None:
    qa, story = designed
    old = h.latest(qa, "test_plan")
    seq, n = h.max_seq(qa), h.count(qa, "test_plan")

    bad = h.base_plan()
    bad["tasks"][0]["estimate_hours"] = {"low": 10, "likely": 6, "high": 4}
    h.assert_rejected(h.plan_submit(qa, story, bad, tmp_path, parent=old["id"]), 4, "VALIDATION_FAILED",
                      path="payload.tasks[0].estimate_hours")
    bad = h.base_plan()
    bad["estimate_method"] = "用例数×固定分钟"
    h.assert_rejected(h.plan_submit(qa, story, bad, tmp_path, parent=old["id"]), 4, "VALIDATION_FAILED",
                      path="payload.estimate_method")
    bad = h.base_plan()
    bad["tasks"][0]["depends_on"] = ["T404"]
    h.assert_rejected(h.plan_submit(qa, story, bad, tmp_path, parent=old["id"]), 4, "VALIDATION_FAILED",
                      path="payload.tasks[0].depends_on", ref="T404")
    bad = h.base_plan()
    del bad["estimate_range_hours"]
    r = h.plan_submit(qa, story, bad, tmp_path, parent=old["id"])
    h.assert_rejected(r, 4, "SCHEMA_INVALID")

    assert h.count(qa, "test_plan") == n and h.latest(qa, "test_plan")["id"] == old["id"]
    assert h.side_effects_after(qa, seq) == [], "被拒绝的计划不应留下审计以外的事件"
    assert h.rejections_after(qa, seq) == 4, "每次拒绝应有审计记录"
    # 修正后可以继续
    ok = h.plan_submit(qa, story, h.base_plan(), tmp_path, parent=old["id"])
    assert ok.rc == 0, ok.describe()


# ---------------------------------------------------------------------------
# AC-006 排期超容量和变更重估
# ---------------------------------------------------------------------------


@pytest.mark.ac("AC-006")
@pytest.mark.layer("offline")
def test_ac006_over_capacity_shows_two_hour_conflict_and_cannot_be_approved(designed, tmp_path: Path) -> None:
    qa, story = designed
    p1 = h.plan_submit(qa, story, h.base_plan(), tmp_path, parent=h.latest(qa, "test_plan")["id"])
    assert p1.rc == 0, p1.describe()
    p1_id = p1.result["artifact_id"]
    h.stage_eval(qa, story, "test_plan")
    for c in h.reviews(qa)["mine"]:
        if c["kind"] == "测试计划批准":
            qa.ok("review", "--answer", f"{c['handle']}=A")
    assert h.stage_eval(qa, story, "test_plan")["status"] == "completed"
    p1_row = h.row(qa, p1_id)

    over = h.base_plan()
    over["tasks"].append({"id": "T2", "title": "新增验证", "owner": "alice", "date": "2026-11-16",
                          "estimate_hours": {"low": 1, "likely": 2, "high": 3}})
    ref = h.reference_capacity_conflicts(over)
    assert ref == [{"person": "alice", "date": "2026-11-16", "over_hours": 2}]

    p2 = h.plan_submit(qa, story, over, tmp_path, parent=p1_id)
    assert p2.rc == 0, p2.describe()  # 计划可登记为待调整版本，但不可批准
    p2_id = p2.result["artifact_id"]
    ev = p2.result["derived"]["plan_evaluation"]
    assert ev["approvable"] is False
    assert [(c["person"], c["date"], c["over_hours"]) for c in ev["capacity_conflicts"]] == [("alice", "2026-11-16", 2)]
    assert ev["capacity_conflicts"][0]["demand_hours"] == 8 and ev["capacity_conflicts"][0]["capacity_hours"] == 6

    st = h.stage_eval(qa, story, "test_plan")
    assert st["status"] == "failed" and st["verdict"] == "fail" and st["decisions"] == []
    assert any("alice" in x and re.search(r"\b2(\.0)?\s*小时", x) for x in st["reasons"]), st["reasons"]
    # 没有针对超容量计划的批准入口
    pending = [c for c in h.reviews(qa)["mine"] if c["basis"] and c["basis"][0]["source_ref"] == p2_id]
    assert pending == []
    # 直接登记“批准”决定也不能让该计划通过
    dec = {"subject_refs": [{"ref": p2_id, "sha256": h.row(qa, p2_id)["sha256"]}], "role": "tester",
           "actor": "tester-a", "verification": "local_attested",
           "scope": {"environment": "lab", "story_id": story}, "result": "approve", "reason": "强行批准"}
    f = tmp_path / "force.json"
    f.write_text(json.dumps(dec, ensure_ascii=False), encoding="utf-8")
    qa.api("decision", "record", "--story", story, "--file", str(f))
    st = h.stage_eval(qa, story, "test_plan")
    assert st["status"] == "failed" and st["verdict"] == "fail"
    assert h.stage_statuses(qa, story)["test_plan"] != "completed"
    # 原估算保留
    assert h.row(qa, p1_id) == p1_row

    # 修正排期后可以继续：把新增任务排到有容量的次日
    fixed = h.clone(over)
    fixed["tasks"][1]["date"] = "2026-11-17"
    fixed["capacity"].append({"person": "alice", "date": "2026-11-17", "hours": 6})
    p3 = h.plan_submit(qa, story, fixed, tmp_path, parent=p2_id)
    assert p3.rc == 0 and p3.result["derived"]["plan_evaluation"]["capacity_conflicts"] == []
    st = h.stage_eval(qa, story, "test_plan")
    assert st["status"] == "blocked" and "批准" in "".join(st["reasons"])


@pytest.mark.ac("AC-006")
@pytest.mark.layer("offline")
def test_ac006_requirement_change_keeps_old_estimate_and_needs_new_confirmation(planned, tmp_path: Path) -> None:
    qa, story = planned
    p1 = h.latest(qa, "test_plan")
    p1_row = h.row(qa, p1["id"])
    ta1 = h.latest(qa, "test_analysis")
    approvals = [d for d in h.decisions_on(qa, p1["id"]) if d["result"] == "approve"]
    assert len(approvals) == 1 and h.decision_valid(qa, approvals[0]["id"])

    h.edit_requirement(qa)
    r = qa.ok("resume")
    assert [c for c in r.result["source_changes"] if c["changed"]], "应识别到需求来源变化"

    # 原估算保留为历史；旧批准不再覆盖新需求；计划不再是已完成
    assert h.row(qa, p1["id"]) == p1_row
    assert h.decision_valid(qa, approvals[0]["id"]) is False
    assert h.stage_statuses(qa, story)["test_plan"] != "completed"

    # 通过机器接口把上游更新到新需求快照，再重估
    flow.answer_mine(qa)
    qa.ok("resume")
    new_src = _latest_requirement_src(qa)
    old_src = ta1["payload"]["requirements"][0]["citations"][0]["source_ref"]
    assert new_src != old_src
    ta2 = json.loads(json.dumps(ta1["payload"]).replace(old_src, new_src))
    s = h.submit(qa, story, "test_analysis", ta2, tmp_path, "--parent", ta1["id"])
    assert s.rc == 0, s.describe()
    assert h.stage_eval(qa, story, "req_analysis")["status"] == "completed"

    st = h.stage_eval(qa, story, "test_plan")
    assert st["status"] not in ("completed", "waived") and st["verdict"] != "pass"
    assert any("重新估算" in x for x in st["reasons"]), st["reasons"]

    p2_payload = h.clone(p1["payload"])
    p2_payload["estimate_range_hours"] = {"low": 4, "likely": 7, "high": 12}
    p2_payload["estimate_method"] += "；需求新增 R7 后重估"
    p2 = h.plan_submit(qa, story, p2_payload, tmp_path, parent=p1["id"])
    assert p2.rc == 0, p2.describe()
    st = h.stage_eval(qa, story, "test_plan")
    assert st["status"] == "blocked" and st["verdict"] == "indeterminate" and st["decisions"] == []
    pending = [c for c in h.reviews(qa)["mine"] if c["kind"] == "测试计划批准"
               and c["basis"][0]["source_ref"] == p2.result["artifact_id"]]
    assert len(pending) == 1, "重估后的计划应待责任人确认，不能沿用旧批准"

    assert h.row(qa, p1["id"]) == p1_row
    assert h.row(qa, p2.result["artifact_id"])["parent_id"] == p1["id"]
    assert h.envelope(h.row(qa, p1["id"]))["payload"]["estimate_range_hours"] == p1["payload"]["estimate_range_hours"]


def _latest_requirement_src(qa: QA) -> str:
    rows = qa.query("SELECT id, canonical FROM snapshots WHERE kind='source_revision' ORDER BY rowid DESC")
    for r in rows:
        if h.envelope(r)["payload"]["uri"].endswith("refund-requirement.md"):
            return r["id"]
    raise AssertionError("没有需求来源快照")


@pytest.mark.ac("AC-006")
@pytest.mark.layer("offline")
def test_ac006_daily_resume_after_requirement_change_reaches_pending_reestimate(planned) -> None:
    """需求变更后只用日常命令（review/resume）继续，应能走到“重估待确认”，不出现永久受阻。"""
    qa, story = planned
    p1 = h.latest(qa, "test_plan")
    h.edit_requirement(qa)
    reached = False
    for _ in range(6):
        qa.ok("resume")
        flow.transfer_others(qa)
        mine = h.reviews(qa)["mine"]
        if any(c["kind"] == "测试计划批准" for c in mine):
            reached = True
            break
        flow.answer_mine(qa)
    st = h.stage_statuses(qa, story)
    assert reached, ("需求变更后日常流程未能产生待确认的重估计划；阶段停留在 "
                     f"{ {k: st[k] for k in ('req_gate', 'tech_gate', 'req_analysis', 'test_plan')} }；"
                     f"需求测试分析原因：{h.stage_state(qa, story, 'req_analysis').get('reasons')}")
    assert h.envelope(h.row(qa, p1["id"]))["payload"] == p1["payload"]


# ---------------------------------------------------------------------------
# AC-007 可交接测试点保留双向追溯
# ---------------------------------------------------------------------------


def _two_point_analysis(base: dict) -> dict:
    """REQ1→TP1 已明确；REQ2→TP2 有开放 TQ。引文沿用已登记分析中逐字核验过的原文。"""
    reqs = {r["id"]: r for r in base["requirements"]}
    return {
        "requirements": [
            {"id": "REQ1", "text": "金额校验", "citations": [reqs["REQ-1"]["citations"][0]]},
            {"id": "REQ2", "text": "部分退款状态", "citations": [reqs["REQ-6"]["citations"][0]]}],
        "test_points": [
            {"id": "TP1", "req_ids": ["REQ1"], "text": "金额为 0 或超额时返回 400", "readiness": "ready", "risk": "P0"},
            {"id": "TP2", "req_ids": ["REQ2"], "text": "部分退款后的状态展示", "readiness": "blocked", "risk": "P1",
             "block_reason": "TQ1 部分退款状态待产品确认", "block_refs": ["TQ1"]}],
        "regression": [{"id": "REG-1", "scope": "金额校验", "tp_ids": ["TP1"], "reason": "入口校验改动"}],
        "strategy": {"types": [{"type": "接口功能", "applicable": True, "reason": "规则可经接口验证"}],
                     "layers": ["接口"], "exclusions": [],
                     "automation_candidates": [{"tp_id": "TP1", "feasibility": "high"}], "repeat_conditions": []},
        "entry_criteria": ["测试计划已批准"], "exit_criteria": ["必测用例全部通过"],
    }


@pytest.mark.ac("AC-007")
@pytest.mark.layer("offline")
def test_ac007_ready_point_enters_seed_blocked_point_is_kept_out(designed, tmp_path: Path) -> None:
    qa, story = designed
    ta1 = h.latest(qa, "test_analysis")
    design1 = h.latest(qa, "case_design")
    payload = _two_point_analysis(ta1["payload"])

    r = h.submit(qa, story, "test_analysis", payload, tmp_path, "--parent", ta1["id"])
    assert r.rc == 0 and r.json["committed"] is True, r.describe()
    seed = r.result["derived"]["handoff_seed"]
    assert seed["ready"] == ["TP1"]
    assert seed["blocked"] == [{"id": "TP2", "block_reason": "TQ1 部分退款状态待产品确认", "block_refs": ["TQ1"]}]

    stored = h.envelope(h.row(qa, r.result["artifact_id"]))["payload"]
    tps = {t["id"]: t for t in stored["test_points"]}
    assert set(tps) == {"TP1", "TP2"}, "受阻测试点应保留在分析中"
    assert tps["TP2"]["readiness"] == "blocked" and tps["TP2"]["block_refs"] == ["TQ1"]
    # 双向追溯：每个测试点指向存在的需求，每条需求至少有一个测试点
    req_ids = {q["id"] for q in stored["requirements"]}
    assert all(set(t["req_ids"]) <= req_ids for t in tps.values())
    assert req_ids == {q for t in tps.values() for q in t["req_ids"]}
    req_text = h.material(qa, "refund-requirement.md").read_text(encoding="utf-8")
    assert all(c["quote"] in req_text for q in stored["requirements"] for c in q["citations"])

    st = h.stage_eval(qa, story, "req_analysis")
    assert st["status"] == "completed" and st["inputs"] == [r.result["artifact_id"]]
    assert st["extra"]["handoff_seed"]["ready"] == ["TP1"]
    assert [b["id"] for b in st["extra"]["handoff_seed"]["blocked"]] == ["TP2"]

    # 受阻测试点不能进入可执行设计
    c0 = design1["payload"]["cases"][0]
    bad = {"analysis_ref": r.result["artifact_id"],
           "cases": [dict(c0, id="C1", tp_ids=["TP1"]), dict(c0, id="C2", tp_ids=["TP2"])]}
    n = h.count(qa, "case_design")
    h.assert_rejected(h.submit(qa, story, "case_design", bad, tmp_path, "--parent", design1["id"]),
                      4, "VALIDATION_FAILED", path="payload.cases[1].tp_ids", ref="TP2")
    assert h.count(qa, "case_design") == n
    good = {"analysis_ref": r.result["artifact_id"], "cases": [dict(c0, id="C1", tp_ids=["TP1"])]}
    g = h.submit(qa, story, "case_design", good, tmp_path, "--parent", design1["id"])
    assert g.rc == 0, g.describe()
    tr = g.result["derived"]["traceability"]
    assert tr["ready_tps"] == ["TP1"] and tr["covered_tps"] == ["TP1"] and tr["ok"] is True


@pytest.mark.ac("AC-007")
@pytest.mark.layer("offline")
def test_ac007_blocked_point_needs_reason_and_requirements_need_points(designed, tmp_path: Path) -> None:
    qa, story = designed
    ta1 = h.latest(qa, "test_analysis")
    n = h.count(qa, "test_analysis")

    p = _two_point_analysis(ta1["payload"])
    del p["test_points"][1]["block_reason"]
    h.assert_rejected(h.submit(qa, story, "test_analysis", p, tmp_path, "--parent", ta1["id"]),
                      4, "VALIDATION_FAILED", path="payload.test_points[1].block_reason")
    p = _two_point_analysis(ta1["payload"])
    p["test_points"] = p["test_points"][:1]  # REQ2 失去全部测试点
    h.assert_rejected(h.submit(qa, story, "test_analysis", p, tmp_path, "--parent", ta1["id"]),
                      4, "VALIDATION_FAILED", path="payload.requirements", ref="REQ2")
    assert h.count(qa, "test_analysis") == n and h.latest(qa, "test_analysis")["id"] == ta1["id"]


# ---------------------------------------------------------------------------
# AC-008 测试点引用不存在的需求
# ---------------------------------------------------------------------------


@pytest.mark.ac("AC-008")
@pytest.mark.layer("offline")
def test_ac008_unknown_requirement_reference_is_rejected_with_path_and_id(designed, tmp_path: Path) -> None:
    qa, story = designed
    ta1 = h.latest(qa, "test_analysis")
    ta1_row = h.row(qa, ta1["id"])
    stage_before = h.stage_state(qa, story, "req_analysis")
    n_ta, n_derived = h.count(qa, "test_analysis"), qa.query("SELECT COUNT(*) c FROM aggregates WHERE kind='derived'")[0]["c"]
    seq = h.max_seq(qa)

    bad = _two_point_analysis(ta1["payload"])
    bad["test_points"][0]["req_ids"] = ["REQ404"]

    v = h.validate(qa, story, "test_analysis", bad, tmp_path)
    assert v.rc == 0 and v.json["valid"] is False, v.describe()
    hit = [p for p in v.json["problems"] if p["path"] == "payload.test_points[0].req_ids"]
    assert hit and "REQ404" in hit[0]["related_refs"] and hit[0].get("code") == "REFERENCE_INVALID"

    r = h.submit(qa, story, "test_analysis", bad, tmp_path, "--parent", ta1["id"])
    h.assert_rejected(r, 4, "REFERENCE_INVALID", path="payload.test_points[0].req_ids", ref="REQ404")
    assert "REQ404" in r.errors[0]["message"]

    # 只有一个测试点且引用无效时，同样拒绝，不登记任何“可交接”结果
    only = _two_point_analysis(ta1["payload"])
    only["requirements"] = only["requirements"][:1]
    only["test_points"] = [dict(only["test_points"][0], req_ids=["REQ404"])]
    h.assert_rejected(h.submit(qa, story, "test_analysis", only, tmp_path, "--parent", ta1["id"]),
                      4, "REFERENCE_INVALID", ref="REQ404")

    assert h.count(qa, "test_analysis") == n_ta
    assert qa.query("SELECT COUNT(*) c FROM aggregates WHERE kind='derived'")[0]["c"] == n_derived
    assert h.row(qa, ta1["id"]) == ta1_row and h.latest(qa, "test_analysis")["id"] == ta1["id"]
    assert h.side_effects_after(qa, seq) == [], "被拒绝的分析不应产生登记或阶段完成事件"
    after = h.stage_eval(qa, story, "req_analysis")
    assert after["inputs"] == stage_before["inputs"] == [ta1["id"]]

    # 修正引用后可以继续
    fixed = _two_point_analysis(ta1["payload"])
    ok = h.submit(qa, story, "test_analysis", fixed, tmp_path, "--parent", ta1["id"])
    assert ok.rc == 0, ok.describe()


# ---------------------------------------------------------------------------
# AC-009 六类模型按材料抽取
# ---------------------------------------------------------------------------

SIX = ("flow", "interface", "data", "state", "rule", "dependency")


@pytest.mark.ac("AC-009")
@pytest.mark.layer("offline")
def test_ac009_six_models_extracted_with_verbatim_evidence_and_field_trace(gated) -> None:
    qa, story = gated
    tr = h.latest(qa, "technical_review")
    assert tr is not None and tr["payload"]["applicability"] == "provided"
    tech_text = h.material(qa, "refund-tech.md").read_text(encoding="utf-8")
    models = tr["payload"]["models"]
    assert set(SIX) <= set(models), f"六类模型应逐类存在：{sorted(models)}"
    for kind in SIX:
        m = models[kind]
        assert m["triggered"] is True and m["entities"], f"{kind} 模型应有实体"
        for e in m["entities"]:
            assert e["citations"], f"{kind}/{e['id']} 缺少证据"
            for c in e["citations"]:
                assert c["source_ref"] in tr["payload"]["source_refs"]
                assert c["quote"] in tech_text, f"引文未逐字出现在技术方案原文：{c['quote']}"
    data_ids = {e["id"] for e in models["data"]["entities"]}
    api_refs = [ref for e in models["interface"]["entities"] for ref in e.get("db_field_refs") or []]
    assert api_refs and set(api_refs) <= data_ids, "接口字段应能追溯到数据字段"
    assert tr["payload"]["source_refs"][0] in {d["ref"] for d in tr["depends_on"]}
    st = h.stage_state(qa, story, "tech_gate")
    assert st["status"] == "completed" and st["inputs"] == [tr["id"]]


@pytest.mark.ac("AC-009")
@pytest.mark.layer("offline")
def test_ac009_models_without_evidence_or_trace_are_rejected(gated, tmp_path: Path) -> None:
    qa, story = gated
    tr = h.latest(qa, "technical_review")
    base, parent = tr["payload"], tr["id"]
    seq = h.max_seq(qa)

    p = h.clone(base)
    p["models"]["flow"]["entities"][0]["citations"] = []
    h.assert_rejected(h.submit(qa, story, "technical_review", p, tmp_path, "--parent", parent),
                      4, "VALIDATION_FAILED", path="payload.models.flow.entities[0].citations")
    p = h.clone(base)
    p["models"]["rule"]["entities"][0]["citations"][0]["quote"] = "方案中并不存在的规则描述"
    h.assert_rejected(h.submit(qa, story, "technical_review", p, tmp_path, "--parent", parent),
                      4, "CITATION_NOT_FOUND", path="payload.models.rule.entities[0].citations[0].quote")
    p = h.clone(base)
    p["models"]["interface"]["entities"][0]["db_field_refs"] = ["DB404"]
    h.assert_rejected(h.submit(qa, story, "technical_review", p, tmp_path, "--parent", parent),
                      4, "REFERENCE_INVALID", path="payload.models.interface.entities[0].db_field_refs", ref="DB404")
    p = h.clone(base)
    del p["models"]["dependency"]
    p["test_seeds"] = [s for s in p["test_seeds"] if "DP1" not in s["model_refs"]]
    h.assert_rejected(h.submit(qa, story, "technical_review", p, tmp_path, "--parent", parent),
                      4, "VALIDATION_FAILED", path="payload.models.dependency")

    assert h.count(qa, "technical_review") == 1 and h.side_effects_after(qa, seq) == []


# ---------------------------------------------------------------------------
# AC-010 触发模型却无实体或问题
# ---------------------------------------------------------------------------


@pytest.mark.ac("AC-010")
@pytest.mark.layer("offline")
def test_ac010_triggered_model_with_empty_arrays_is_rejected(gated, tmp_path: Path) -> None:
    qa, story = gated
    tr = h.latest(qa, "technical_review")
    tr_row = h.row(qa, tr["id"])
    stage_before = h.stage_state(qa, story, "tech_gate")
    seq = h.max_seq(qa)

    bad = h.clone(tr["payload"])
    bad["models"]["interface"] = {"triggered": True, "entities": [], "questions": []}
    for s in bad["test_seeds"]:  # 去掉对已删除接口实体的引用，只保留“静默空数组”这一个问题
        s["model_refs"] = [m for m in s["model_refs"] if m != "API1"]

    v = h.validate(qa, story, "technical_review", bad, tmp_path)
    assert v.rc == 0 and v.json["valid"] is False
    assert [p["path"] for p in v.json["problems"]] == ["payload.models.interface"]

    r = h.submit(qa, story, "technical_review", bad, tmp_path, "--parent", tr["id"])
    h.assert_rejected(r, 4, "VALIDATION_FAILED", path="payload.models.interface")
    bad2 = h.clone(bad)
    bad2["models"]["interface"] = {"triggered": True}
    h.assert_rejected(h.submit(qa, story, "technical_review", bad2, tmp_path, "--parent", tr["id"]),
                      4, "VALIDATION_FAILED", path="payload.models.interface")

    # 未登记；原技术质检与阶段判定不变，没有新的完成事件
    assert h.count(qa, "technical_review") == 1 and h.row(qa, tr["id"]) == tr_row
    assert h.side_effects_after(qa, seq) == []
    assert h.stage_state(qa, story, "tech_gate") == stage_before

    # 修正：写明待确认问题后可以登记
    fixed = h.clone(bad)
    fixed["models"]["interface"]["questions"] = ["接口字段清单待研发确认"]
    ok = h.submit(qa, story, "technical_review", fixed, tmp_path, "--parent", tr["id"])
    assert ok.rc == 0, ok.describe()


# ---------------------------------------------------------------------------
# AC-011 技术建模与质检只登记一次
# ---------------------------------------------------------------------------


@pytest.mark.ac("AC-011")
@pytest.mark.layer("offline")
def test_ac011_single_tech_gate_instance_and_single_snapshot(gated, tmp_path: Path) -> None:
    qa, story = gated
    listing = qa.api_ok("stage", "status", "--story", story).json
    assert list(listing["statuses"]).count("tech_gate") == 1
    tech_titles = [s["title"] for s in listing["stages"] if s and "技术" in s["title"]]
    assert tech_titles == ["技术方案质检"], f"技术建模与质检应属于同一阶段：{tech_titles}"
    rows = qa.query("SELECT object_id FROM aggregates WHERE kind='stage' AND object_id LIKE ?", ("%:tech_gate",))
    assert [r["object_id"] for r in rows] == [f"stage:{story}:tech_gate"]

    trs = h.all_of(qa, "technical_review")
    assert len(trs) == 1
    p = trs[0]["payload"]
    assert {c["kind"] for c in p["checks"]} >= {"model", "contract", "rollback", "verifiability"}
    assert p["questions"] and p["test_seeds"], "TQ 与技术测试种子应在同一份技术质检快照中"
    entity_ids = {e["id"] for m in p["models"].values() for e in m.get("entities") or []}
    assert all(set(s["model_refs"]) <= entity_ids for s in p["test_seeds"])
    assert h.stage_state(qa, story, "tech_gate")["inputs"] == [trs[0]["id"]]
    passed = [e for e in h.all_of(qa, "stage_evaluation")
              if e["payload"]["stage"] == "tech_gate" and e["payload"]["verdict"] == "pass"]
    assert passed and all(e["payload"]["inputs"] == [trs[0]["id"]] for e in passed)

    # 重复继续不会再登记一份
    qa.ok("resume")
    qa.ok("resume")
    assert h.count(qa, "technical_review") == 1
    assert len(qa.query("SELECT 1 FROM aggregates WHERE kind='stage' AND object_id LIKE ?", ("%:tech_gate",))) == 1

    # 缺少任一类检查、或种子引用不在同一快照内的模型实体时拒绝
    seq = h.max_seq(qa)
    bad = h.clone(p)
    bad["checks"] = [c for c in bad["checks"] if c["kind"] != "rollback"]
    h.assert_rejected(h.submit(qa, story, "technical_review", bad, tmp_path, "--parent", trs[0]["id"]),
                      4, "VALIDATION_FAILED", path="payload.checks")
    bad = h.clone(p)
    bad["test_seeds"][0]["model_refs"] = ["API404"]
    h.assert_rejected(h.submit(qa, story, "technical_review", bad, tmp_path, "--parent", trs[0]["id"]),
                      4, "REFERENCE_INVALID", path="payload.test_seeds[0].model_refs", ref="API404")
    assert h.count(qa, "technical_review") == 1 and h.side_effects_after(qa, seq) == []


# ---------------------------------------------------------------------------
# AC-012 缺技术方案不能当成已通过
# ---------------------------------------------------------------------------


@pytest.mark.ac("AC-012")
@pytest.mark.layer("offline")
def test_ac012_unreadable_material_is_not_pass_and_cannot_be_waived_without_decision(gated, tmp_path: Path) -> None:
    qa, story = gated
    tr = h.latest(qa, "technical_review")
    unreadable = {"applicability": "unreadable", "source_refs": tr["payload"]["source_refs"], "models": {},
                  "checks": [{"id": "TC-MIGRATION", "kind": "model", "result": "pass",
                              "detail": "涉及数据库迁移，但材料读取受阻"}]}
    # 读取受阻时不能带着“通过”的检查提交
    h.assert_rejected(h.submit(qa, story, "technical_review", unreadable, tmp_path, "--parent", tr["id"]),
                      4, "VALIDATION_FAILED", path="payload.checks")
    unreadable["checks"][0]["result"] = "indeterminate"
    r = h.submit(qa, story, "technical_review", unreadable, tmp_path, "--parent", tr["id"])
    assert r.rc == 0, r.describe()

    st = h.stage_eval(qa, story, "tech_gate")
    assert st["status"] == "blocked" and st["verdict"] == "indeterminate" and st["decisions"] == []
    assert any("读取受阻" in x for x in st["reasons"]), st["reasons"]
    assert not any("未提供" in x.split("；")[0] for x in st["reasons"]), "读取受阻不能显示成无方案"
    view = flow.view(qa)
    sm = flow.stage_map(view)
    assert sm["tech_gate"]["status"] == "blocked" and sm["tech_gate"]["verdict"] == "indeterminate"
    assert sm["req_analysis"]["status"] not in ("completed", "waived")
    assert "技术方案质检" not in view["summary"]["have"]

    items = [c for c in view["reviews"]["others"] if c["kind"] == "适用性决定"]
    assert len(items) == 1 and items[0]["responsible_role"] == "developer" and items[0]["status"] == "pending"
    handle = items[0]["handle"]
    # 测试人员不能代签适用性决定
    a = qa.run("review", "--answer", f"{handle}=A")
    h.assert_rejected(a, 3, "AUTHORIZATION_REQUIRED", ref=handle)
    b = qa.api("clarification", "answer", "--story", story, "--handle", handle, "--option", "A",
               "--role", "tester", "--actor", "tester-a")
    h.assert_rejected(b, 3, "AUTHORIZATION_REQUIRED", ref=handle)
    # 未与该事项关联的“不适用”决定也不能豁免
    waive = {"subject_refs": [{"ref": r.result["artifact_id"], "sha256": r.result["sha256"]}], "role": "tester",
             "actor": "tester-a", "verification": "local_attested", "scope": {"environment": "lab", "story_id": story},
             "result": "waive", "reason": "跳过技术质检"}
    f = tmp_path / "waive.json"
    f.write_text(json.dumps(waive, ensure_ascii=False), encoding="utf-8")
    qa.api("decision", "record", "--story", story, "--file", str(f))

    st = h.stage_eval(qa, story, "tech_gate")
    assert st["status"] == "blocked" and st["verdict"] == "indeterminate" and st["decisions"] == []
    assert h.stage_statuses(qa, story)["tech_gate"] not in ("completed", "waived")
    assert [c["status"] for c in flow.view(qa)["reviews"]["others"] if c["handle"] == handle] == ["pending"]


@pytest.mark.ac("AC-012")
@pytest.mark.layer("offline")
def test_ac012_unreadable_source_file_blocks_gate_and_recovers(gated) -> None:
    qa, story = gated
    tech = h.material(qa, "refund-tech.md")
    os.chmod(tech, 0o000)
    try:
        r = qa.ok("resume")
        srcs = [e["payload"] for e in h.all_of(qa, "source_revision") if e["payload"]["uri"].endswith("refund-tech.md")]
        assert srcs[-1]["read_status"] == "unreadable" and srcs[-1].get("failure_reason")
        assert srcs[-1]["read_status"] != "missing"
        assert any("unreadable" in n or "读取受阻" in n for n in r.result["notes"]), r.result["notes"]
        st = h.stage_eval(qa, story, "tech_gate")
        assert st["status"] not in ("completed", "waived") and st["verdict"] not in ("pass", "not_applicable")
        sts = h.stage_statuses(qa, story)
        assert sts["tech_gate"] not in ("completed", "waived") and sts["req_analysis"] != "completed"
        assert h.count(qa, "technical_review") == 1, "读取受阻时不应登记新的技术质检结论"
    finally:
        os.chmod(tech, 0o444)
    # 恢复读取权限后可以继续
    qa.ok("resume")
    assert h.stage_statuses(qa, story)["tech_gate"] == "completed"


@pytest.mark.ac("AC-012")
@pytest.mark.layer("offline")
def test_ac012_stage_judgement_shows_read_blocked_for_unreadable_source_file(gated) -> None:
    """来源文件真实读取受阻时，阶段判定与状态视图应显示“读取受阻”，而不只是“已过期/来源已变化”。"""
    qa, story = gated
    tech = h.material(qa, "refund-tech.md")
    os.chmod(tech, 0o000)
    try:
        qa.ok("resume")
        st = h.stage_eval(qa, story, "tech_gate")
        view = flow.view(qa)
    finally:
        os.chmod(tech, 0o444)
    shown = st["reasons"] + flow.stage_map(view)["tech_gate"]["reasons"] + view["summary"]["reasons"]
    assert any(("读取受阻" in x) or ("unreadable" in x) or ("读取权限" in x) for x in shown), \
        f"阶段判定未显示读取受阻：{shown}；状态标题：{view['summary']['headline']}"


# ---------------------------------------------------------------------------
# AC-013 / AC-014 设计流与实测流
# ---------------------------------------------------------------------------


def _flow_context(qa: QA) -> dict:
    """从数据库独立取得两次部署（D1=build1、D2=build2）及各自的受控探测证据。"""
    deps = h.all_of(qa, "deployment")
    envs = h.all_of(qa, "environment_check")
    assert len(deps) >= 2 and len(envs) >= 2, "完整实验应有两次部署与两次环境探测"
    ev_old = envs[0]["payload"]["checks"][0]["evidence_ref"]
    ev_cur = envs[-1]["payload"]["checks"][0]["evidence_ref"]
    for ev in (ev_old, ev_cur):
        rows = qa.query("SELECT origin, source, state FROM evidence WHERE id=?", (ev,))
        assert rows[0]["origin"] == "local_lab" and ":probe" in rows[0]["source"] and rows[0]["state"] == "active"
    assert envs[0]["payload"]["build"]["observed"] == "build1" and envs[-1]["payload"]["build"]["observed"] == "build2"
    kb = h.latest(qa, "technical_review")["payload"]["source_refs"][0]
    kb_ev = h.envelope(h.row(qa, kb))["payload"]["content_ref"]  # 文档/知识类证据，不是运行观测
    return {"d1": deps[0], "d2": deps[-1], "ev_d1": ev_old, "ev_d2": ev_cur, "ev_kb": kb_ev}


def _flows(result_json: dict) -> dict[str, dict]:
    return {f["id"]: f for f in result_json["derived"]["flows"]}


@pytest.mark.ac("AC-013")
@pytest.mark.layer("offline")
def test_ac013_design_flow_kept_and_unobserved_runtime_flow_not_verified(full, tmp_path: Path) -> None:
    qa, story = full
    ctx = _flow_context(qa)
    d2 = ctx["d2"]["payload"]["deployment_id"]
    model = {"flows": [
        {"id": "F-DESIGN", "from": "A", "to": "B", "kind": "design", "evidence_refs": [ctx["ev_kb"]],
         "verification": "verified"},  # 提交方自称已验证，系统不应照单全收
        {"id": "F-A", "from": "probe", "to": "A", "kind": "runtime", "evidence_refs": [ctx["ev_d2"]],
         "verification": "verified", "deployment_ref": d2},
        {"id": "F-B", "from": "A", "to": "B", "kind": "runtime", "evidence_refs": [],
         "verification": "unverified", "deployment_ref": d2}]}
    r = h.submit(qa, story, "flow_model", model, tmp_path, "--name", "ac013")
    assert r.rc == 0, r.describe()
    flows = _flows(r.result)
    assert (flows["F-DESIGN"]["from"], flows["F-DESIGN"]["to"], flows["F-DESIGN"]["kind"]) == ("A", "B", "design")
    assert flows["F-DESIGN"]["status"] == "design_only", "设计关系不能被标成运行已验证"
    assert flows["F-B"]["status"] in ("unverified", "indeterminate") and flows["F-B"]["missing"]
    assert any("运行观测证据" in m for m in flows["F-B"]["missing"])
    assert not all(f["status"] == "verified" for f in flows.values()), "不能标全部部署正常"
    stored = h.envelope(h.row(qa, r.result["artifact_id"]))["payload"]["flows"]
    assert [(f["from"], f["to"], f["kind"]) for f in stored if f["id"] == "F-DESIGN"] == [("A", "B", "design")]
    agg = qa.query("SELECT state FROM aggregates WHERE object_id=?", (f"derived:{r.result['artifact_id']}",))
    assert {f["id"]: f["status"] for f in json.loads(agg[0]["state"])["flows"]}["F-B"] != "verified"


@pytest.mark.ac("AC-013")
@pytest.mark.layer("offline")
def test_ac013_runtime_flow_with_current_probe_evidence_is_marked_verified(full, tmp_path: Path) -> None:
    """对照：A 有当前部署上的受控探测证据时，A 的实测流应能标为已验证（设计流与实测流分别标记）。"""
    qa, story = full
    ctx = _flow_context(qa)
    statuses = {}
    for label, ref in (("deployment_id", ctx["d2"]["payload"]["deployment_id"]), ("snapshot_id", ctx["d2"]["id"])):
        model = {"flows": [{"id": "F-A", "from": "probe", "to": "A", "kind": "runtime",
                            "evidence_refs": [ctx["ev_d2"]], "verification": "verified", "deployment_ref": ref}]}
        v = h.validate(qa, story, "flow_model", model, tmp_path)
        assert v.rc == 0, v.describe()
        statuses[label] = (_flows(v.json)["F-A"]["status"], _flows(v.json)["F-A"]["missing"])
    assert any(s == "verified" for s, _ in statuses.values()), \
        f"当前部署 {ctx['d2']['payload']['deployment_id']} 上的探测证据未被识别为实测：{statuses}"


@pytest.mark.ac("AC-014")
@pytest.mark.layer("offline")
def test_ac014_knowledge_or_other_deployment_evidence_cannot_confirm_current_flow(full, tmp_path: Path) -> None:
    qa, story = full
    ctx = _flow_context(qa)
    d1, d2 = ctx["d1"]["payload"]["deployment_id"], ctx["d2"]["payload"]["deployment_id"]
    assert d1 != d2 and ctx["d2"]["payload"]["build"] != ctx["d1"]["payload"]["build"]
    model = {"flows": [
        # 证据来自 D1，声明的部署上下文也是 D1；当前部署为 D2
        {"id": "F-D1", "from": "A", "to": "B", "kind": "runtime", "evidence_refs": [ctx["ev_d1"]],
         "verification": "verified", "deployment_ref": d1},
        # 只有知识库/文档证据
        {"id": "F-KB", "from": "A", "to": "B", "kind": "runtime", "evidence_refs": [ctx["ev_kb"]],
         "verification": "verified", "deployment_ref": d2},
        # 证据来自 D1，却声明为 D2
        {"id": "F-RELABEL", "from": "A", "to": "B", "kind": "runtime", "evidence_refs": [ctx["ev_d1"]],
         "verification": "verified", "deployment_ref": d2}]}
    r = h.submit(qa, story, "flow_model", model, tmp_path, "--name", "ac014")
    assert r.rc == 0, r.describe()
    flows = _flows(r.result)
    assert flows["F-D1"]["status"] == "indeterminate", flows["F-D1"]
    assert any("部署" in m and "不符" in m for m in flows["F-D1"]["missing"]), flows["F-D1"]["missing"]
    assert flows["F-KB"]["status"] == "indeterminate"
    assert any("运行观测证据" in m for m in flows["F-KB"]["missing"]), flows["F-KB"]["missing"]
    assert flows["F-RELABEL"]["status"] != "verified", "D1 的证据改标为 D2 后不能被确认为 D2 的实测"
    assert all(f["status"] != "verified" and f["missing"] for f in flows.values())


# ---------------------------------------------------------------------------
# AC-015 / AC-016 改动影响
# ---------------------------------------------------------------------------

DIFF = ("--- a/refund/common.py\n+++ b/refund/common.py\n@@ -1 +1 @@\n"
        "-def remaining(order): return order.paid\n+def remaining(order): return order.paid - order.refunded\n")


def _import_diff(qa: QA, story: str, tmp: Path) -> tuple[str, Path]:
    diff = tmp / "repo" / "change.diff"
    diff.parent.mkdir(parents=True, exist_ok=True)
    diff.write_text(DIFF, encoding="utf-8")
    r = qa.api_ok("source", "import", "--story", story, "--uri", str(diff))
    assert r.result["read_status"] == "available", r.describe()
    return h.envelope(h.row(qa, r.result["source_ref"]))["payload"]["content_ref"], diff


def _change_set(diff_ref: str) -> dict:
    return {"repo": "lab/refund-service", "base_commit": "1111111aaaa", "head_commit": "2222222bbbb",
            "diff_ref": diff_ref,
            "search_scope": {"paths": ["refund/"], "patterns": ["remaining("], "complete": True},
            "unknown_scope": [], "impact_refs": ["REG-1", "REG-2"],
            "callers": [{"symbol": "remaining", "file": "refund/api.py", "reg_ids": ["REG-1"]},
                        {"symbol": "remaining", "file": "refund/callback.py", "reg_ids": ["REG-2"]}]}


@pytest.mark.ac("AC-015")
@pytest.mark.layer("offline")
def test_ac015_change_set_binds_callers_regression_and_search_scope(designed, tmp_path: Path) -> None:
    qa, story = designed
    reg_ids = {r["id"] for r in h.latest(qa, "test_analysis")["payload"]["regression"]}
    assert {"REG-1", "REG-2"} <= reg_ids
    diff_ref, diff = _import_diff(qa, story, tmp_path)
    cs = _change_set(diff_ref)

    r = h.submit(qa, story, "change_set", cs, tmp_path)
    assert r.rc == 0 and r.json["committed"] is True, r.describe()
    assert r.result["derived"]["scope_complete"] is True
    snap = h.envelope(h.row(qa, r.result["artifact_id"]))
    p = snap["payload"]
    assert (p["repo"], p["base_commit"], p["head_commit"]) == ("lab/refund-service", "1111111aaaa", "2222222bbbb")
    assert p["search_scope"] == {"paths": ["refund/"], "patterns": ["remaining("], "complete": True}
    assert [(c["file"], c["reg_ids"]) for c in p["callers"]] == [("refund/api.py", ["REG-1"]),
                                                                  ("refund/callback.py", ["REG-2"])]
    # diff 以字节摘要绑定
    dep = [d for d in snap["depends_on"] if d["ref"] == diff_ref]
    assert dep and dep[0]["sha256"] == hashlib.sha256(diff.read_bytes()).hexdigest()
    # 两个调用方关联的回归项都在必回归建议中
    reg = qa.api_ok("execution", "status", "--story", story).json["regression"]
    required = {row["reg_id"] for row in reg["rows"] if row["required"]}
    assert {"REG-1", "REG-2"} <= required


@pytest.mark.ac("AC-015")
@pytest.mark.layer("offline")
def test_ac015_callers_must_link_existing_regression_items(designed, tmp_path: Path) -> None:
    qa, story = designed
    diff_ref, _ = _import_diff(qa, story, tmp_path)
    seq = h.max_seq(qa)

    cs = _change_set(diff_ref)
    cs["callers"][1]["reg_ids"] = []
    h.assert_rejected(h.submit(qa, story, "change_set", cs, tmp_path), 4, "VALIDATION_FAILED",
                      path="payload.callers[1].reg_ids")
    cs = _change_set(diff_ref)
    cs["callers"][1]["reg_ids"] = ["REG-404"]
    h.assert_rejected(h.submit(qa, story, "change_set", cs, tmp_path), 4, "REFERENCE_INVALID",
                      path="payload.callers[1].reg_ids", ref="REG-404")
    cs = _change_set(diff_ref)
    cs["head_commit"] = cs["base_commit"]
    h.assert_rejected(h.submit(qa, story, "change_set", cs, tmp_path), 4, "VALIDATION_FAILED",
                      path="payload.head_commit")
    cs = _change_set("EV-000000000000")
    h.assert_rejected(h.submit(qa, story, "change_set", cs, tmp_path), 4, "REFERENCE_INVALID",
                      path="payload.diff_ref", ref="EV-000000000000")
    cs = _change_set(diff_ref)
    del cs["search_scope"]
    h.assert_rejected(h.submit(qa, story, "change_set", cs, tmp_path), 4, "SCHEMA_INVALID")
    assert h.count(qa, "change_set") == 0 and h.side_effects_after(qa, seq) == []


@pytest.mark.ac("AC-016")
@pytest.mark.layer("offline")
def test_ac016_incomplete_scan_cannot_claim_no_impact_or_drop_regression(designed, tmp_path: Path) -> None:
    qa, story = designed
    diff_ref, _ = _import_diff(qa, story, tmp_path)
    ta = h.latest(qa, "test_analysis")
    reg_before = qa.api_ok("execution", "status", "--story", story).json["regression"]
    seq = h.max_seq(qa)

    # 只检查了调用方一，调用方二未读取，却声称全部无影响
    claim = _change_set(diff_ref)
    claim.update({"callers": [], "impact_refs": [], "unknown_scope": ["refund/callback.py"],
                  "claims": {"no_impact": True}})
    claim["search_scope"] = {"paths": ["refund/api.py"], "patterns": ["remaining("], "complete": False}
    h.assert_rejected(h.submit(qa, story, "change_set", claim, tmp_path), 4, "VALIDATION_FAILED",
                      path="payload.claims.no_impact")
    # 自称扫描完整但仍列有未读取范围
    claim2 = h.clone(claim)
    claim2["search_scope"]["complete"] = True
    h.assert_rejected(h.submit(qa, story, "change_set", claim2, tmp_path), 4, "VALIDATION_FAILED",
                      path="payload.claims.no_impact")
    # 已发现调用方时也不能声称无影响
    claim3 = _change_set(diff_ref)
    claim3["claims"] = {"no_impact": True}
    h.assert_rejected(h.submit(qa, story, "change_set", claim3, tmp_path), 4, "VALIDATION_FAILED",
                      path="payload.claims.no_impact")
    assert h.count(qa, "change_set") == 0 and h.side_effects_after(qa, seq) == []

    # 如实提交不完整扫描：登记并标记范围不全
    honest = h.clone(claim)
    del honest["claims"]
    honest["callers"] = [{"symbol": "remaining", "file": "refund/api.py", "reg_ids": ["REG-1"]}]
    r = h.submit(qa, story, "change_set", honest, tmp_path)
    assert r.rc == 0, r.describe()
    assert r.result["derived"]["scope_complete"] is False
    stored = h.envelope(h.row(qa, r.result["artifact_id"]))["payload"]
    assert stored["unknown_scope"] == ["refund/callback.py"] and stored["search_scope"]["complete"] is False

    # 相邻回归未被自动删除
    assert h.latest(qa, "test_analysis")["id"] == ta["id"]
    reg_after = qa.api_ok("execution", "status", "--story", story).json["regression"]
    assert reg_after == reg_before
    assert {row["reg_id"] for row in reg_after["rows"] if row["required"]} >= {"REG-1", "REG-2"}


# ---------------------------------------------------------------------------
# AC-017 / AC-018 测试策略
# ---------------------------------------------------------------------------


def _strategy_analysis(base: dict) -> dict:
    """存在接口与状态争用、无前端变化：接口/并发适用，兼容测试不适用且写明理由。"""
    p = h.clone(base)
    p["strategy"] = {
        "types": [{"type": "接口功能", "applicable": True, "reason": "规则均可通过接口与账务观测验证"},
                  {"type": "并发", "applicable": True, "reason": "存在余额状态争用"},
                  {"type": "兼容性", "applicable": False, "reason": "本次无前端变化，只有单一接口客户端"}],
        "layers": ["接口", "数据"],
        "exclusions": [{"item": "前端兼容矩阵", "reason": "无前端变化"}],
        "automation_candidates": [{"tp_id": "TP-IDEM", "feasibility": "high"},
                                  {"tp_id": "TP-CONC", "feasibility": "medium", "reason": "需要并发屏障控制先后"}],
        "repeat_conditions": [{"tp_id": "TP-CONC", "condition": "相同构建与数据初态下重复 3 次",
                               "reason": "并发结果可能受调度影响"}]}
    return p


@pytest.mark.ac("AC-017")
@pytest.mark.layer("offline")
def test_ac017_strategy_with_reasoned_exclusion_passes_without_fake_compat_results(full, tmp_path: Path) -> None:
    qa, story = full
    ta = h.latest(qa, "test_analysis")
    mine = _strategy_analysis(ta["payload"])
    v = h.validate(qa, story, "test_analysis", mine, tmp_path)
    assert v.rc == 0 and v.json["valid"] is True and v.json["problems"] == [], v.describe()

    # 已登记并走完流程的策略：适用与排除明确，自动化候选与重复执行条件明确
    s = ta["payload"]["strategy"]
    types = {t["type"]: t for t in s["types"]}
    assert types["兼容性"]["applicable"] is False and types["兼容性"]["reason"].strip()
    assert any(t["applicable"] for n, t in types.items() if "接口" in n) and types["并发"]["applicable"] is True
    tp_ids = {t["id"] for t in ta["payload"]["test_points"]}
    assert s["automation_candidates"] and all(a["tp_id"] in tp_ids and a["feasibility"] in ("high", "medium", "low")
                                              for a in s["automation_candidates"])
    assert all((a.get("reason") or "").strip() for a in s["automation_candidates"] if a["feasibility"] != "high")
    assert s["repeat_conditions"] and all(c["tp_id"] in tp_ids and c["condition"].strip() and c["reason"].strip()
                                          for c in s["repeat_conditions"])
    assert h.stage_state(qa, story, "req_analysis")["status"] == "completed"

    # 不生成虚构的兼容实测：用例、尝试与报告中都没有兼容性结果
    design = h.latest(qa, "case_design")["payload"]
    case_ids = {c["id"] for c in design["cases"]}
    assert not [c for c in design["cases"] if "兼容" in c["title"] or "compat" in c["kind"]]
    attempts = h.all_of(qa, "case_attempt")
    assert attempts and {a["payload"]["tc_ref"] for a in attempts} <= case_ids
    report = h.latest(qa, "test_report")["payload"]
    assert {r["tc"] for r in report["latest"]["rows"]} == case_ids
    assert "兼容" not in json.dumps(report, ensure_ascii=False)


@pytest.mark.ac("AC-017")
@pytest.mark.layer("offline")
def test_ac017_strategy_without_reasons_or_with_unknown_points_is_invalid(full, tmp_path: Path) -> None:
    qa, story = full
    base = h.latest(qa, "test_analysis")["payload"]

    p = _strategy_analysis(base)
    del p["strategy"]["types"][2]["reason"]  # 兼容测试不适用却没有理由
    r = h.validate(qa, story, "test_analysis", p, tmp_path)
    h.assert_rejected(r, 4, "SCHEMA_INVALID", path="payload.strategy.types[2].reason")
    p = _strategy_analysis(base)
    p["strategy"]["exclusions"][0]["reason"] = ""
    h.assert_rejected(h.validate(qa, story, "test_analysis", p, tmp_path), 4, "SCHEMA_INVALID",
                      path="payload.strategy.exclusions[0].reason")
    p = _strategy_analysis(base)
    p["strategy"]["repeat_conditions"][0]["tp_id"] = "TP-404"
    v = h.validate(qa, story, "test_analysis", p, tmp_path)
    assert v.rc == 0 and v.json["valid"] is False
    assert [x for x in v.json["problems"] if x["path"] == "payload.strategy.repeat_conditions[0].tp_id"
            and "TP-404" in x["related_refs"]]
    p = _strategy_analysis(base)
    p["strategy"]["automation_candidates"][0]["tp_id"] = "TP-404"
    v = h.validate(qa, story, "test_analysis", p, tmp_path)
    assert v.json["valid"] is False
    assert [x for x in v.json["problems"] if x["path"] == "payload.strategy.automation_candidates[0].tp_id"]


@pytest.mark.ac("AC-018")
@pytest.mark.layer("offline")
def test_ac018_low_automation_feasibility_without_reason_is_rejected(designed, tmp_path: Path) -> None:
    qa, story = designed
    ta = h.latest(qa, "test_analysis")
    ta_row = h.row(qa, ta["id"])
    stage_before = h.stage_state(qa, story, "req_analysis")
    repeat_before = ta["payload"]["strategy"]["repeat_conditions"]
    assert any(c["tp_id"] == "TP-CONC" for c in repeat_before)
    seq = h.max_seq(qa)

    bad = h.clone(ta["payload"])
    bad["strategy"]["automation_candidates"] = [{"tp_id": "TP-CONC", "feasibility": "low"}]
    bad["strategy"]["repeat_conditions"] = []  # 同时悄悄去掉重复执行条件
    r = h.submit(qa, story, "test_analysis", bad, tmp_path, "--parent", ta["id"])
    h.assert_rejected(r, 4, "VALIDATION_FAILED", path="payload.strategy.automation_candidates[0].reason")
    bad["strategy"]["automation_candidates"] = [{"tp_id": "TP-CONC", "feasibility": "low", "reason": "   "}]
    r = h.submit(qa, story, "test_analysis", bad, tmp_path, "--parent", ta["id"])
    assert r.rc == 4 and r.error_code in ("VALIDATION_FAILED", "SCHEMA_INVALID"), r.describe()
    assert r.errors[0]["path"] == "payload.strategy.automation_candidates[0].reason"
    bad["strategy"]["automation_candidates"] = [{"tp_id": "TP-CONC", "feasibility": "low", "reason": ""}]
    r = h.submit(qa, story, "test_analysis", bad, tmp_path, "--parent", ta["id"])
    assert r.rc == 4 and r.errors[0]["path"] == "payload.strategy.automation_candidates[0].reason", r.describe()

    # 未登记；重复执行风险保持原状，没有被改成已解决
    assert h.row(qa, ta["id"]) == ta_row and h.latest(qa, "test_analysis")["id"] == ta["id"]
    assert h.latest(qa, "test_analysis")["payload"]["strategy"]["repeat_conditions"] == repeat_before
    assert h.side_effects_after(qa, seq) == []
    assert h.stage_state(qa, story, "req_analysis") == stage_before

    # 写明原因后可以登记，且重复执行条件原样保留
    good = h.clone(ta["payload"])
    good["strategy"]["automation_candidates"] = [
        {"tp_id": "TP-CONC", "feasibility": "low", "reason": "缺少可控的并发屏障，需人工观察"}]
    ok = h.submit(qa, story, "test_analysis", good, tmp_path, "--parent", ta["id"])
    assert ok.rc == 0, ok.describe()
    stored = h.envelope(h.row(qa, ok.result["artifact_id"]))["payload"]["strategy"]
    assert stored["automation_candidates"][0]["feasibility"] == "low"
    assert stored["repeat_conditions"] == repeat_before


# ---------------------------------------------------------------------------
# AC-019 设计与脚本分别登记
# ---------------------------------------------------------------------------


@pytest.mark.ac("AC-019")
@pytest.mark.layer("offline")
def test_ac019_design_completes_without_script_mapping_or_execution(designed) -> None:
    qa, story = designed
    design = h.latest(qa, "case_design")
    case_ids = [c["id"] for c in design["payload"]["cases"]]
    assert "C1" in case_ids
    st = h.stage_state(qa, story, "case_design")
    assert st["status"] == "completed" and st["verdict"] == "pass" and st["inputs"] == [design["id"]]

    # 还没有脚本：脚本映射为空，执行未开始
    assert h.count(qa, "script_revision") == 0
    ex = qa.api_ok("execution", "status", "--story", story).json
    assert ex["scope"] == case_ids and ex["attempts"] == [] and ex["latest"] == {}
    rep = qa.api_ok("report", "build", "--story", story).json["test_report"]
    cov = rep["coverage"]
    assert (cov["design"]["numerator"], cov["design"]["denominator"]) == (5, 5)
    assert cov["script_mapping"]["numerator"] == 0 and cov["script_mapping"]["unmapped"] == case_ids
    assert cov["execution"]["numerator"] == 0 and cov["execution"]["not_executed"] == case_ids
    assert rep["counts"]["pass"] == 0 and rep["counts"]["not_run"] == len(case_ids)
    assert all(r["status"] == "not_run" and r["attempt_ref"] is None for r in rep["latest"]["rows"])

    # 没有脚本时请求执行被拒绝，不生成任何执行记录
    r = qa.api("execution", "plan", "--story", story, "--cases", "C1", "--dispatch")
    h.assert_rejected(r, 3, "PRECONDITION_FAILED")
    assert h.count(qa, "case_attempt") == 0 and h.count(qa, "execution_run") == 0
    sts = h.stage_statuses(qa, story)
    assert sts["case_design"] == "completed" and sts["test_execution"] != "completed"


# ---------------------------------------------------------------------------
# AC-020 修订不覆盖已登记设计
# ---------------------------------------------------------------------------


def _executed_design(qa: QA) -> str:
    """推进到已按 D1 执行：D1 有质检报告、产品确认与执行结果引用。"""
    flow.drive(qa, until=lambda v: flow.stage_map(v)["test_execution"]["status"] in ("completed", "failed"))
    return h.story_id(qa)


def _revise_c1(d1_payload: dict) -> dict:
    d2 = h.clone(d1_payload)
    c1 = next(c for c in d2["cases"] if c["id"] == "C1")
    c1["expected"]["second_status"] = 201
    c1["expected_text"] = "第二次返回 201 且退款 ID 相同，已退款仍为 3000"
    d2["revision_note"] = "依据产品澄清：重复请求改为返回 201"
    return d2


@pytest.mark.ac("AC-020")
@pytest.mark.layer("offline")
def test_ac020_revision_creates_child_snapshot_and_invalidates_dependents(qa: QA, tmp_path: Path) -> None:
    story = _executed_design(qa)
    d1 = h.latest(qa, "case_design")
    d1_row = h.row(qa, d1["id"])
    cqr = h.latest(qa, "case_quality_review")
    assert cqr["payload"]["design_ref"] == d1["id"], "前置：质检报告引用 D1"
    confirms = [d for d in h.decisions_on(qa, d1["id"]) if d["result"] == "confirm"]
    assert confirms and all(h.decision_valid(qa, d["id"]) for d in confirms)
    assert h.stage_statuses(qa, story)["case_qc"] == "completed"
    d2_payload = _revise_c1(d1["payload"])

    # 不指明父版本的覆盖式提交被拒绝
    r = h.submit(qa, story, "case_design", d2_payload, tmp_path)
    h.assert_rejected(r, 3, "REVISION_CONFLICT", ref=d1["id"])
    assert h.row(qa, d1["id"]) == d1_row and h.count(qa, "case_design") == 1

    r = h.submit(qa, story, "case_design", d2_payload, tmp_path, "--parent", d1["id"])
    assert r.rc == 0, r.describe()
    d2_id = r.result["artifact_id"]
    assert d2_id != d1["id"]
    # D1 字节不变
    assert h.row(qa, d1["id"]) == d1_row
    shown = qa.api_ok("artifact", "show", "--id", d1["id"]).json
    assert shown["sha256"] == d1_row["sha256"] and shown["current"] is False
    assert shown["envelope"]["payload"] == d1["payload"]
    # D2 有父快照与修改依据
    d2_row = h.row(qa, d2_id)
    d2 = h.envelope(d2_row)
    assert d2_row["parent_id"] == d1["id"]
    assert d2["payload"]["revision_note"] == "依据产品澄清：重复请求改为返回 201"
    assert qa.api_ok("artifact", "show", "--id", d2_id).json["current"] is True

    # 依赖 D1 的当前结论失效
    sts = h.stage_statuses(qa, story)
    for stage in ("case_qc", "test_entry", "test_execution"):
        assert sts[stage] not in ("completed", "waived"), f"{stage} 仍显示为 {sts[stage]}"
    assert all(h.decision_valid(qa, d["id"]) is False for d in confirms), "绑定 D1 的产品确认应失效"
    qc = h.stage_eval(qa, story, "case_design")
    assert qc["inputs"] == [d2_id]
    st = h.stage_eval(qa, story, "case_qc")
    assert st["status"] != "completed" and st["verdict"] != "pass"
    entry = h.stage_eval(qa, story, "test_entry")
    assert entry["status"] != "completed" and any("关键业务预期" in x for x in entry["reasons"])
    ex = qa.api_ok("execution", "status", "--story", story).json
    assert ex["latest"] == {}, "按 D1 执行的结果不能作为 D2 的当前结果"
    # 历史保留
    assert h.count(qa, "case_design") == 2 and cqr["id"] == h.latest(qa, "case_quality_review")["id"]


@pytest.mark.ac("AC-020")
@pytest.mark.layer("offline")
def test_ac020_report_for_revised_design_does_not_reuse_results_bound_to_old_design(qa: QA, tmp_path: Path) -> None:
    """C1 的预期修订为 D2 后，为 D2 生成的测试报告不应把按 D1 预期得到的通过算作当前结果。"""
    story = _executed_design(qa)
    d1 = h.latest(qa, "case_design")
    r = h.submit(qa, story, "case_design", _revise_c1(d1["payload"]), tmp_path, "--parent", d1["id"])
    assert r.rc == 0, r.describe()
    d2_id = r.result["artifact_id"]
    # 独立核对：现有尝试全部绑定 D1，没有任何一次按 D2 执行
    bound = {x["input_id"] for x in qa.query(
        "SELECT DISTINCT d.input_id FROM dependencies d JOIN snapshots s ON s.id=d.result_id "
        "WHERE s.kind='case_attempt' AND d.relation='expected'")}
    assert bound == {d1["id"]}

    rep = qa.api_ok("report", "build", "--story", story).json["test_report"]
    assert rep["scope_ref"] == d2_id
    c1 = next(x for x in rep["latest"]["rows"] if x["tc"] == "C1")
    assert c1["status"] != "pass", f"D2 的报告把绑定 D1 的尝试 {c1['attempt_ref']} 记为 C1 当前通过"
    assert "C1" in rep["coverage"]["execution"]["not_executed"]


# ---------------------------------------------------------------------------
# AC-021 三种覆盖分别计算
# ---------------------------------------------------------------------------


@pytest.mark.ac("AC-021")
@pytest.mark.layer("offline")
def test_ac021_three_coverages_reported_separately(qa: QA, tmp_path: Path) -> None:
    flow.drive(qa)
    story = h.story_id(qa)
    ta1, d1 = h.latest(qa, "test_analysis"), h.latest(qa, "case_design")
    script = h.latest(qa, "script_revision")

    # 2 个测试点
    a = h.clone(ta1["payload"])
    a["requirements"] = [q for q in a["requirements"] if q["id"] in ("REQ-1", "REQ-2")]
    a["test_points"] = [t for t in a["test_points"] if t["id"] in ("TP-AMOUNT", "TP-ROLE")]
    a["regression"] = [g for g in a["regression"] if g["id"] == "REG-2"]
    a["strategy"]["automation_candidates"] = [{"tp_id": "TP-ROLE", "feasibility": "high"}]
    a["strategy"]["repeat_conditions"] = []
    ra = h.submit(qa, story, "test_analysis", a, tmp_path, "--parent", ta1["id"])
    assert ra.rc == 0, ra.describe()
    assert h.stage_eval(qa, story, "req_analysis")["status"] == "completed"
    # 2 条用例：C2 已有脚本；C-NEW 只有设计
    c2 = next(c for c in d1["payload"]["cases"] if c["id"] == "C2")
    c_new = dict(next(c for c in d1["payload"]["cases"] if c["id"] == "SMOKE-2"), id="C-NEW", kind="functional",
                 title="退款 0 元被拒（尚无脚本）")
    d = {"analysis_ref": ra.result["artifact_id"], "cases": [c2, c_new], "revision_note": "收窄为两个测试点"}
    rd = h.submit(qa, story, "case_design", d, tmp_path, "--parent", d1["id"])
    assert rd.rc == 0, rd.describe()
    d2_id = rd.result["artifact_id"]
    assert h.stage_eval(qa, story, "case_design")["status"] == "completed"
    assert "C2" in script["payload"]["tc_refs"] and "C-NEW" not in script["payload"]["tc_refs"]

    # 只有 C2 按当前设计实际执行并通过
    run = qa.api("execution", "plan", "--story", story, "--cases", "C2", "--dispatch")
    assert run.rc == 0 and run.json["state"] == "succeeded", run.describe()
    on_d2 = [e for e in h.all_of(qa, "case_attempt") if d2_id in {x["ref"] for x in e["depends_on"]}]
    assert [(e["payload"]["tc_ref"], e["payload"]["outcome"]) for e in on_d2] == [("C2", "pass")]

    ref = h.reference_coverage(["TP-AMOUNT", "TP-ROLE"], {"C2": ["TP-ROLE"], "C-NEW": ["TP-AMOUNT"]},
                               scripted=set(script["payload"]["tc_refs"]), executed={"C2"})
    assert ref == {"design": (2, 2), "script_mapping": (1, 2), "execution": (1, 2)}

    rep = qa.api_ok("report", "build", "--story", story).json["test_report"]
    cov = rep["coverage"]
    got = {k: (cov[k]["numerator"], cov[k]["denominator"]) for k in ("design", "script_mapping", "execution")}
    assert got == ref
    assert cov["script_mapping"]["unmapped"] == ["C-NEW"] and cov["execution"]["not_executed"] == ["C-NEW"]
    assert cov["design"]["unit"] != cov["execution"]["unit"]
    assert not ({"overall", "total", "coverage_rate", "percent"} & set(cov)), "不应有合并后的单一覆盖率"
    assert rep["counts"]["pass"] == 1 and rep["counts"]["not_run"] == 1
    stored = h.latest(qa, "test_report")["payload"]["coverage"]
    assert {k: (stored[k]["numerator"], stored[k]["denominator"]) for k in ref} == ref

    # 人读交付件：三项分别展示，不统称 100% 覆盖
    out = tmp_path / "交付"
    qa.ok("export", "--output", str(out))
    md = next(out.glob("*.md")).read_text(encoding="utf-8")
    assert re.search(r"设计覆盖[:：]\s*2/2", md) and re.search(r"脚本映射[:：]\s*1/2", md)
    assert re.search(r"实际执行[:：]\s*1/2", md)
    for line in md.splitlines():
        if "100%" in line or "100 %" in line:
            assert "不能" in line or "不得" in line, f"交付件出现未加限定的 100% 说法：{line}"
    js = json.loads(next(out.glob("*.json")).read_text(encoding="utf-8"))
    assert {k: (js["test_report"]["coverage"][k]["numerator"], js["test_report"]["coverage"][k]["denominator"])
            for k in ref} == ref


# ---------------------------------------------------------------------------
# AC-022 孤立 TP 或无效 TC 引用
# ---------------------------------------------------------------------------


@pytest.mark.ac("AC-022")
@pytest.mark.layer("offline")
def test_ac022_orphan_point_and_invalid_reference_block_design_gate(designed, tmp_path: Path) -> None:
    qa, story = designed
    d1 = h.latest(qa, "case_design")
    d1_row = h.row(qa, d1["id"])
    seq = h.max_seq(qa)

    # TP-ROLE（相当于规格中的 TP2）没有任何用例；C1 指向不存在的 TP9
    bad = h.clone(d1["payload"])
    bad["cases"] = [c for c in bad["cases"] if "TP-ROLE" not in c["tp_ids"]]
    i_c1 = next(i for i, c in enumerate(bad["cases"]) if c["id"] == "C1")
    bad["cases"][i_c1]["tp_ids"] = ["TP9"]
    # 参考模型：TP-IDEM 仍由 C3 覆盖，未覆盖的只有 TP-ROLE
    ready = {"TP-AMOUNT", "TP-ROLE", "TP-IDEM", "TP-ATOMIC", "TP-CONC"}
    assert ready - {t for c in bad["cases"] for t in c["tp_ids"]} == {"TP-ROLE"}

    v = h.validate(qa, story, "case_design", bad, tmp_path)
    assert v.rc == 0 and v.json["valid"] is False, v.describe()
    tr = v.json["derived"]["traceability"]
    assert tr["uncovered_tps"] == ["TP-ROLE"] and tr["invalid_refs"] == [{"tc": "C1", "tp": "TP9"}]
    assert tr["ok"] is False
    assert [p for p in v.json["problems"] if p["path"] == f"payload.cases[{i_c1}].tp_ids" and "TP9" in p["related_refs"]]

    r = h.submit(qa, story, "case_design", bad, tmp_path, "--parent", d1["id"])
    h.assert_rejected(r, 4, "REFERENCE_INVALID", path=f"payload.cases[{i_c1}].tp_ids", ref="TP9")
    assert h.count(qa, "case_design") == 1 and h.row(qa, d1["id"]) == d1_row
    assert h.side_effects_after(qa, seq) == []

    # 只剩孤立测试点时：设计可登记为修订，但设计完成门禁不通过
    orphan = h.clone(d1["payload"])
    orphan["cases"] = [c for c in orphan["cases"] if "TP-ROLE" not in c["tp_ids"]]
    r = h.submit(qa, story, "case_design", orphan, tmp_path, "--parent", d1["id"])
    assert r.rc == 0, r.describe()
    assert r.result["derived"]["traceability"]["uncovered_tps"] == ["TP-ROLE"]
    st = h.stage_eval(qa, story, "case_design")
    assert st["status"] == "failed" and st["verdict"] == "fail"
    assert any("TP-ROLE" in x for x in st["reasons"]), st["reasons"]
    sts = h.stage_statuses(qa, story)
    assert sts["case_design"] != "completed" and sts["case_qc"] != "completed"
    assert not [e for e in h.events_after(qa, seq) if e["event_type"] == "stage_completed"
                and e["object_id"].endswith(":case_design")]

    # 补回用例后门禁恢复
    fixed = h.clone(d1["payload"])
    fixed["revision_note"] = "补回 TP-ROLE 的用例"
    ok = h.submit(qa, story, "case_design", fixed, tmp_path, "--parent", r.result["artifact_id"])
    assert ok.rc == 0, ok.describe()
    assert h.stage_eval(qa, story, "case_design")["status"] == "completed"


# ---------------------------------------------------------------------------
# AC-023 质检和业务预期确认各自存在
# ---------------------------------------------------------------------------


@pytest.mark.ac("AC-023")
@pytest.mark.layer("offline")
def test_ac023_quality_review_and_expectation_decisions_are_separate_records(full) -> None:
    qa, story = full
    d1 = h.latest(qa, "case_design")
    d1_sha = h.row(qa, d1["id"])["sha256"]
    cqr = h.latest(qa, "case_quality_review")
    assert cqr["payload"]["design_ref"] == d1["id"]
    assert {k: v["result"] for k, v in cqr["payload"]["levels"].items()} == {"L1": "pass", "L2": "pass", "L3": "pass"}

    key_cases = [c["id"] for c in d1["payload"]["cases"] if c["key_expectation"]]
    confirms = [d for d in h.decisions_on(qa, d1["id"]) if d["result"] == "confirm"]
    assert len(confirms) == len(key_cases) == 5
    for d in confirms:
        assert d["role"] == "product" and d["id"] != cqr["id"]
        assert json.loads(d["subjects"]) == [{"ref": d1["id"], "sha256": d1_sha}], "签字应绑定 D1 的摘要"
        assert d["verification"] == "simulated", "实验审批桩的答复应如实标为 simulated"
        assert h.decision_valid(qa, d["id"])
    kinds = {r["id"]: r["kind"] for r in qa.query(
        "SELECT id, kind FROM snapshots WHERE id IN (%s)" % ",".join("?" * (len(confirms) + 1)),
        tuple([cqr["id"]] + [d["id"] for d in confirms]))}
    assert kinds[cqr["id"]] == "case_quality_review" and all(kinds[d["id"]] == "decision" for d in confirms)

    # 设计交接（提测准入）分别评估两项
    entries = [e for e in h.all_of(qa, "entry_report") if e["payload"]["verdict"] == "pass"]
    assert entries
    crit = {c["id"]: c["result"] for c in entries[-1]["payload"]["criterion_results"]}
    assert crit["case_qc"] == "pass" and crit["expectations"] == "pass"
    assert set(entries[-1]["payload"]["decision_refs"]) == {d["id"] for d in confirms}
    # 质检阶段本身不引用产品签字
    qc = h.stage_state(qa, story, "case_qc")
    assert qc["status"] == "completed" and qc["inputs"] == [cqr["id"]] and qc["decisions"] == []
    cfg = json.loads((qa.workspace() / "config" / "qa-flow.json").read_text(encoding="utf-8"))
    assert cfg["rules"]["policy"]["case_qc_applicable"] is True


@pytest.mark.ac("AC-023")
@pytest.mark.layer("offline")
def test_ac023_case_qc_applicability_follows_actual_policy(qa: QA, tmp_path: Path) -> None:
    flow.drive(qa, until=flow.until_stage("case_qc"))
    story = h.story_id(qa)
    cfg_path = qa.workspace() / "config" / "qa-flow.json"
    original = json.loads(cfg_path.read_text(encoding="utf-8"))
    cqr = h.latest(qa, "case_quality_review")

    def apply_policy(mutate) -> dict:
        cfg = h.clone(original)
        mutate(cfg["rules"]["policy"])
        f = tmp_path / f"cfg-{time.monotonic_ns()}.json"
        f.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        r = qa.ok("admin", "setup", "--config", str(f), "--name", qa.workspace().name, "--apply")
        assert r.json["applied"] is True and r.json["problems"] == [], r.describe()
        for s in ("req_gate", "tech_gate", "req_analysis", "case_design"):
            assert h.stage_eval(qa, story, s)["status"] == "completed"
        return h.stage_eval(qa, story, "case_qc")

    st = apply_policy(lambda p: p.__setitem__("case_qc_applicable", False))
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["rules"]["policy"]["case_qc_applicable"] is False
    assert st["status"] == "waived" and st["verdict"] == "not_applicable" and st["inputs"] == []
    assert any("策略" in x for x in st["reasons"])

    st = apply_policy(lambda p: p.pop("case_qc_applicable"))
    assert st["status"] == "blocked" and st["verdict"] == "indeterminate", "策略未冻结时不能默认适用或不适用"

    st = apply_policy(lambda p: p.__setitem__("case_qc_applicable", True))
    assert st["status"] == "completed" and st["verdict"] == "pass" and st["inputs"] == [cqr["id"]]


# ---------------------------------------------------------------------------
# AC-024 质检通过不能替产品签字
# ---------------------------------------------------------------------------


def _l3_passed_expectations_pending(qa: QA) -> tuple[str, list[dict]]:
    """用例质检已通过、环境与冒烟就绪，但关键业务预期没有转交产品，保持待确认。"""
    h.drive_without_transfer(qa, lambda s: s["case_qc"] == "completed" and s["env_ready"] == "completed"
                             and s["test_plan"] == "completed")
    story = h.story_id(qa)
    cqr = h.latest(qa, "case_quality_review")
    assert cqr["payload"]["levels"]["L3"]["result"] == "pass"
    pending = [c for c in h.reviews(qa)["others"] if c["kind"] == "关键业务预期确认"]
    assert pending and all(c["status"] == "pending" and c["responsible_role"] == "product" for c in pending)
    return story, pending


def _entry_blocked(qa: QA, story: str) -> dict:
    st = h.stage_eval(qa, story, "test_entry")
    assert st["status"] not in ("completed", "waived") and st["verdict"] != "pass", st
    assert any("关键业务预期" in x for x in st["reasons"]), st["reasons"]
    return st


@pytest.mark.ac("AC-024")
@pytest.mark.layer("offline")
def test_ac024_entry_blocked_while_key_expectations_pending(qa: QA, tmp_path: Path) -> None:
    story, pending = _l3_passed_expectations_pending(qa)
    st = _entry_blocked(qa, story)
    assert st["decisions"] == []
    entry = h.envelope(h.row(qa, st["extra"]["entry_report"]))["payload"]
    crit = {c["id"]: c["result"] for c in entry["criterion_results"]}
    assert crit["case_qc"] == "pass" and crit["expectations"] == "fail" and entry["verdict"] != "pass"
    sts = h.stage_statuses(qa, story)
    assert sts["case_qc"] == "completed" and sts["test_execution"] != "completed"

    design = h.latest(qa, "case_design")
    seq = h.max_seq(qa)
    # 测试人员不能在日常入口代签
    a = qa.run("review", "--answer", f"{pending[0]['handle']}=A")
    h.assert_rejected(a, 3, "AUTHORIZATION_REQUIRED", ref=pending[0]["handle"])
    assert h.side_effects_after(qa, seq) == []
    # 直接登记一条署名“产品负责人”的决定，不能解除阻断
    forged = {"subject_refs": [{"ref": design["id"], "sha256": h.row(qa, design["id"])["sha256"]}],
              "role": "product", "actor": "产品负责人", "verification": "local_attested",
              "scope": {"environment": "lab", "story_id": story}, "result": "confirm", "reason": "预期正确"}
    f = tmp_path / "forged.json"
    f.write_text(json.dumps(forged, ensure_ascii=False), encoding="utf-8")
    qa.api("decision", "record", "--story", story, "--file", str(f))
    forged["verification"] = "external_verified"
    f.write_text(json.dumps(forged, ensure_ascii=False), encoding="utf-8")
    h.assert_rejected(qa.api("decision", "record", "--story", story, "--file", str(f)), 3, "AUTHORIZATION_INVALID")

    _entry_blocked(qa, story)
    still = [c for c in h.reviews(qa)["others"] if c["kind"] == "关键业务预期确认"]
    assert len(still) == len(pending) and all(c["status"] == "pending" for c in still)
    assert h.stage_statuses(qa, story)["test_entry"] != "completed"


@pytest.mark.ac("AC-024")
@pytest.mark.layer("offline")
def test_ac024_self_declared_product_role_cannot_unblock_entry(qa: QA) -> None:
    """Agent 在机器接口的答复中自填角色与产品姓名（--role product --actor 产品负责人），不能解除准入阻断。"""
    story, pending = _l3_passed_expectations_pending(qa)
    _entry_blocked(qa, story)
    outcomes = []
    for c in pending:
        r = qa.api("clarification", "answer", "--story", story, "--handle", c["handle"], "--option", "A",
                   "--role", "product", "--actor", "产品负责人")
        outcomes.append((c["handle"], r.rc, r.error_code, (r.result or {}).get("verification") if r.rc == 0 else None))
    st = h.stage_eval(qa, story, "test_entry")
    assert st["status"] not in ("completed", "waived") and st["verdict"] != "pass", \
        f"自填产品角色的本地答复解除了准入阻断：答复结果 {outcomes}；提测准入 {st['status']}/{st['verdict']}"


@pytest.mark.ac("AC-024")
@pytest.mark.layer("offline")
def test_ac024_execution_cannot_start_while_entry_blocked(qa: QA) -> None:
    """关键预期未确认、准入受阻时，机器接口也不能开始正式执行。"""
    story, _ = _l3_passed_expectations_pending(qa)
    _entry_blocked(qa, story)

    def formal_attempts() -> list[str]:
        return [e["id"] for e in h.all_of(qa, "case_attempt") if e["payload"]["purpose"] == "execution"]

    assert formal_attempts() == []
    r = qa.api("execution", "plan", "--story", story, "--cases", "C1", "--purpose", "execution", "--dispatch")
    after = formal_attempts()
    assert after == [], (f"准入受阻时仍执行了正式用例并生成尝试 {after}；"
                         f"命令退出码 {r.rc}，回执 {r.out[:300]}")
    assert r.rc != 0


# ---------------------------------------------------------------------------
# AC-106 批准后需求变更的竞态
# ---------------------------------------------------------------------------


@pytest.mark.ac("AC-106")
@pytest.mark.layer("lab")
def test_ac106_requirement_change_between_approval_and_send_blocks_dispatch(qa: QA, tmp_path: Path) -> None:
    flow.drive(qa)
    story = h.story_id(qa)
    assert h.stage_statuses(qa, story)["test_exit"] == "completed"
    exit_report = h.latest(qa, "exit_report")

    # 生成 action H1（归档准出报告到实验平台桩），并由责任人批准，但尚未发送
    plan = qa.api_ok("archive", "plan", "--story", story, "--report", exit_report["id"], "--space", "lab-qa-archive")
    action = plan.json["action"]
    assert action["state"] == "awaiting_authorization" and action["kind"] == "doc.archive"
    auth = [c for c in h.reviews(qa)["mine"] if c["kind"] == "操作授权"]
    assert len(auth) == 1
    qa.ok("review", "--answer", f"{auth[0]['handle']}=A")
    approvals = qa.query("SELECT * FROM decisions WHERE plan_sha256=? AND result='approve'", (action["plan_sha256"],))
    assert len(approvals) == 1 and approvals[0]["revoked_at"] is None
    approval_id = approvals[0]["id"]
    approval_row = h.row(qa, approval_id)
    assert h.decision_valid(qa, approval_id) is True
    assert qa.query("SELECT attempts FROM actions WHERE id=?", (action["id"],))[0]["attempts"] == 0
    calls_before, docs_before = h.platform_calls(qa), h.platform_docs(qa)
    assert docs_before == 0
    old_src = _latest_requirement_src(qa)

    # worker 领取后停在“发送前重新核验”屏障；此时插入需求新快照 S2
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    worker = qa.test_mode(QA_FLOW_PAUSE_AT="before_send_check", QA_FLOW_PAUSE_DIR=str(barrier))
    proc = worker.popen("api", "action", "dispatch", "--id", action["id"])
    try:
        waiting = barrier / "before_send_check.waiting"
        deadline = time.monotonic() + 40
        while not waiting.exists():
            assert proc.poll() is None, f"worker 未到达屏障即退出：{proc.communicate()}"
            assert time.monotonic() < deadline, "worker 未在限时内到达发送前屏障"
            time.sleep(0.02)
        assert qa.query("SELECT state FROM actions WHERE id=?", (action["id"],))[0]["state"] == "running"
        assert h.platform_calls(qa) == calls_before

        h.edit_requirement(qa)
        refreshed = qa.api_ok("source", "refresh", "--story", story).json["refreshed"]
        assert [x for x in refreshed if x["changed"]], "S2 应登记为需求来源的新快照"
        new_src = _latest_requirement_src(qa)
        assert new_src != old_src

        (barrier / "before_send_check.go").write_text("go", encoding="utf-8")
        out, err = proc.communicate(timeout=90)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    # 发送前重验失败
    assert proc.returncode == 3, f"rc={proc.returncode}\n{out}\n{err}"
    receipt = json.loads(out)
    e = receipt["errors"][0]
    assert receipt["committed"] is False and e["code"] == "AUTHORIZATION_INVALID"
    assert action["id"] in e["related_refs"] and e["details"]["external_calls"] == 0 and e["details"]["problems"]

    # 平台调用数为 0：平台桩账本与文档表均无新增
    assert h.platform_calls(qa) == calls_before
    assert h.platform_docs(qa) == 0
    assert h.count(qa, "archive_record") == 0
    a = qa.query("SELECT * FROM actions WHERE id=?", (action["id"],))[0]
    assert a["state"] == "awaiting_authorization" and a["authorization_ref"] is None and a["remote_object_id"] is None
    attempts = qa.query("SELECT outcome, late FROM action_attempts WHERE action_id=?", (action["id"],))
    assert [(x["outcome"], x["late"]) for x in attempts] == [("rejected_before_send", 0)]
    assert not qa.query("SELECT 1 FROM events WHERE object_id=? AND event_type='action_succeeded'", (action["id"],))

    # 旧批准保留为历史，但不覆盖新需求
    assert h.row(qa, approval_id) == approval_row
    d = qa.query("SELECT revoked_at FROM decisions WHERE id=?", (approval_id,))
    assert len(d) == 1 and d[0]["revoked_at"] is None
    assert h.decision_valid(qa, approval_id) is False
    assert qa.api_ok("artifact", "show", "--id", new_src).json["current"] is True
    assert qa.api_ok("artifact", "show", "--id", old_src).json["current"] is False
    assert "R7" in h.material(qa, "refund-requirement.md").read_text(encoding="utf-8")

    # 再次发送不会沿用旧批准
    again = qa.api("action", "dispatch", "--id", action["id"])
    h.assert_rejected(again, 3, "AUTHORIZATION_REQUIRED", ref=action["id"])
    assert h.platform_calls(qa) == calls_before and h.platform_docs(qa) == 0
    assert h.stage_statuses(qa, story)["test_exit"] != "completed"
