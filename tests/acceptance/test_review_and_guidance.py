"""集中确认、授权范围、错误提示、不重复提问与新人验收报告计算的独立验收。

覆盖 AC-133、AC-134、AC-136、AC-142；AC-135、AC-138、AC-143、AC-144 需要真人参与，记为 blocked
（真人任务脚本见 tests/usability_protocol.md）。其中不依赖真人的报告计算规则另有自动检查，
这些自动检查不代表对应真人场景通过。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from support import flow
from support import frontend_helpers as fh
from support.cli import QA, blocked

DAILY = ("start", "resume", "review", "status", "export", "demo")


def _snapshot_sha(qa: QA, ref: str) -> str:
    return qa.query("SELECT sha256 FROM snapshots WHERE id=?", (ref,))[0]["sha256"]


def _answered_events(qa: QA) -> int:
    return qa.query("SELECT COUNT(*) n FROM events WHERE event_type='review_item_answered'")[0]["n"]


# ---------------------------------------------------------------------------
# AC-133 同范围问题集中确认并复用有效答复
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-133")
@pytest.mark.layer("lab")
def test_review_groups_by_owner_and_records_answers_per_item(qa: QA):
    qa.ok("demo")
    r = qa.run("review")
    assert r.rc == 3 and r.error_code == "REVIEW_REQUIRED", r.describe()
    d = r.errors[0]["details"]
    mine, others = d["items"], d["others"]
    # 按责任人集中：本人一组，另一责任人（产品）的两个同版本问题一组
    assert len(mine) == 1 and {c["responsible_role"] for c in mine} == {"tester"}
    assert len(others) == 2 and {c["responsible_role"] for c in others} == {"product"}
    assert len({c["basis"][0]["source_ref"] for c in others}) == 1, "两个产品问题应作用于同一来源版本"
    source = (qa.workspace() / "lab" / "materials" / "refund-requirement.md").read_text(encoding="utf-8")
    for c in mine + others:
        assert c["business_question"] and c["affected_scope"] and c["role_label"]
        assert len(c["options"]) >= 2
        assert c["basis"] and all(b["quote"] in source and b["location"] for b in c["basis"]), "依据应逐字出自来源"
    q1, q2, q3 = mine[0], others[0], others[1]

    # 分项答复：只答复自己负责的一项
    a = qa.ok("review", "--answer", f"{q1['handle']}=A", "--note", f"{q1['handle']}=技术方案已写明以 event_id 去重")
    assert [x["handle"] for x in a.result["answers"]] == [q1["handle"]]
    decs = fh.decisions(qa)
    assert len(decs) == 1
    dec = decs[0]
    assert (dec["role"], dec["verification"], dec["result"]) == ("tester", "local_attested", "answer")
    items = fh.review_items(qa)
    assert items[q1["handle"]]["status"] == "answered" and items[q1["handle"]]["decision_ref"] == dec["id"]
    subjects = json.loads(dec["subjects"])
    assert [s["ref"] for s in subjects] == [s["ref"] for s in items[q1["handle"]]["subject_refs"]]
    assert all(_snapshot_sha(qa, s["ref"]) == s["sha256"] for s in subjects), "决定应绑定所答复问题的内容摘要"
    stored = json.loads(qa.query("SELECT canonical FROM snapshots WHERE id=?", (dec["id"],))[0]["canonical"])["payload"]
    assert stored["answer"]["option"] == "A" and "event_id" in stored["answer"]["text"]
    # 另一角色的问题保持待处理
    assert items[q2["handle"]]["status"] == "pending" and items[q3["handle"]]["status"] == "pending"
    assert items[q2["handle"]]["decision_ref"] is None and items[q3["handle"]]["decision_ref"] is None

    # 继续：已生效答复不重复问
    qa.ok("resume")
    v = flow.view(qa)
    asked = [c["business_question"] for c in fh.cards(v)]
    assert q1["business_question"] not in asked
    assert asked.count(q2["business_question"]) == 1 and asked.count(q3["business_question"]) == 1
    all_items = fh.review_items(qa)
    assert sum(1 for i in all_items.values() if i["business_question"] == q1["business_question"]) == 1
    assert fh.stage_of(v, "req_analysis")["status"] == "completed"
    assert {c["handle"] for c in v["reviews"]["others"]} >= {q2["handle"], q3["handle"]}
    assert [d["id"] for d in fh.decisions(qa)] == [dec["id"]], "继续不应产生新的决定"

    # 再次继续与查看仍不重复提问
    qa.ok("resume")
    again = [c["business_question"] for c in fh.cards(flow.view(qa))]
    assert q1["business_question"] not in again and len(again) == len(set(again))


@pytest.mark.ac("AC-133")
@pytest.mark.layer("lab")
def test_interactive_review_shows_basis_and_impact_and_keeps_others_pending(qa: QA):
    """在终端中打开 review：阅读依据和影响，分项答复；另一角色的问题只展示、不能代签。"""
    qa.ok("demo")
    rc, out, timed_out = fh.tty_run(qa, ["review"], [("你的选择", "A")], timeout=60)
    assert not timed_out and rc == 0, out
    assert "[Q1]" in out and "同一回调只记账一次，以什么字段判定“同一回调”？" in out
    assert "依据：R5：支付渠道的退款成功回调可能重复送达，同一回调只记账一次。" in out
    assert "影响范围：回调去重相关测试点与用例" in out and "需要：测试人员" in out
    assert "A. 以回调 event_id 判定" in out and "B. 以 refund_id 判定" in out
    # 另一责任人的问题集中列出，并说明不能代签
    tail = out.split("你不能代签", 1)
    assert len(tail) == 2 and "[Q2]" in tail[1] and "[Q3]" in tail[1] and "产品负责人" in tail[1]
    assert "已记录 Q1 的答复" in out and "不是已认证的产品角色签字" in out
    decs = fh.decisions(qa)
    assert len(decs) == 1 and decs[0]["verification"] == "local_attested"
    items = fh.review_items(qa)
    assert items["Q1"]["status"] == "answered"
    assert items["Q2"]["status"] == "pending" and items["Q3"]["status"] == "pending"
    # 已答复后再次打开，不再询问 Q1
    rc, out2, timed_out = fh.tty_run(qa, ["review"], [], timeout=30)
    assert not timed_out and rc == 0, out2
    assert "你的选择" not in out2 and "[Q1]" not in out2
    assert len(fh.decisions(qa)) == 1


@pytest.mark.ac("AC-133")
@pytest.mark.layer("lab")
def test_non_interactive_review_text_shows_basis_impact_and_other_owner(qa: QA):
    """非终端（如输出被重定向）查看待确认时，人读文本同样要给出依据、影响范围和需他人决定的事项。"""
    qa.ok("demo")
    r = qa.text("review")
    assert r.rc == 3, r.describe()
    text = r.out + r.err
    assert "[Q1]" in text and "A. 以回调 event_id 判定" in text
    assert "R5：支付渠道的退款成功回调可能重复送达" in text, "缺少原文依据\n" + text
    assert "回调去重相关测试点与用例" in text, "缺少受影响范围\n" + text
    assert "部分退款后订单状态应显示什么？" in text and "产品负责人" in text, "缺少需他人决定的事项\n" + text


@pytest.mark.ac("AC-133")
@pytest.mark.layer("lab")
def test_repeating_same_answer_is_reported_as_existing_not_newly_recorded(qa: QA):
    """Q4 已答复后再次执行同一条 review --answer：不新增决定，并应说明“已有有效答复”。"""
    qa.ok("demo")
    qa.ok("review", "--answer", "Q1=A")
    qa.ok("resume")
    mine = flow.view(qa)["reviews"]["mine"]
    assert [c["handle"] for c in mine] == ["Q4"] and mine[0]["kind"] == "测试计划批准"
    first = qa.text("review", "--answer", "Q4=A")
    assert first.rc == 0 and "已记录 Q4 的答复" in first.out
    n_dec, n_ev = len(fh.decisions(qa)), _answered_events(qa)

    again_json = qa.run("review", "--answer", "Q4=A")
    again_text = qa.text("review", "--answer", "Q4=A")
    assert again_json.rc == 0 and again_text.rc == 0
    # 数据层面：没有重复决定
    assert len(fh.decisions(qa)) == n_dec and _answered_events(qa) == n_ev
    # 展示层面：不应再次声称“已记录”
    assert "已有有效答复" in again_text.out and "已记录 Q4 的答复" not in again_text.out, (
        "重复提交同一答复时输出仍为“已记录”，与实际（未新增记录）不符\n" + again_text.out[:400])
    assert again_json.result["answers"][0].get("reused") is True, again_json.result["answers"]


@pytest.mark.ac("AC-133")
@pytest.mark.layer("lab")
def test_answering_with_other_option_after_valid_answer_does_not_create_second_decision(qa: QA):
    qa.ok("demo")
    qa.ok("review", "--answer", "Q1=A")
    r = qa.run("review", "--answer", "Q1=B")
    assert len(fh.decisions(qa)) == 1, "同一有效范围只保留一个决定"
    stored = json.loads(qa.query("SELECT canonical FROM snapshots WHERE kind='decision'")[0]["canonical"])["payload"]
    assert stored["answer"]["option"] == "A"
    if r.rc == 0:
        assert r.result["answers"][0].get("reused") is True
        assert "已有有效答复" in qa.text("review", "--answer", "Q1=B").out


# ---------------------------------------------------------------------------
# AC-134 泛化确认不能授权新目标
# ---------------------------------------------------------------------------

GENERIC = ["全部同意", "都同意", "同意全部", "全部批准", "all", "approve all", "yes to all", " 全部同意 "]


@pytest.mark.ac("AC-134")
@pytest.mark.layer("offline")
def test_generic_approval_is_rejected_and_approves_nothing(qa: QA):
    qa.ok("demo")
    before = fh.ledger(qa)
    items_before = fh.review_items(qa)
    for text in GENERIC:
        r = qa.run("review", "--answer", f"Q1={text}")
        assert r.rc != 0 and r.json["committed"] is False, f"“{text}”不应被接受\n" + r.describe()
        assert r.error_code == "INVALID_ARGUMENT", r.describe()
    r = qa.run("review", "--answer", "Q1=全部同意")
    assert "逐项" in r.errors[0]["message"] or "具体问题" in r.errors[0]["message"], "应要求明确对象范围"
    assert r.errors[0]["related_refs"] == ["Q1"] and r.errors[0]["path"] == "answer"
    # 作为说明文字夹带也不行
    r = qa.run("review", "--answer", "Q1=A", "--note", "Q1=全部同意")
    assert r.rc != 0 and r.error_code == "INVALID_ARGUMENT", r.describe()
    # 不存在“对全部事项作答”的写法
    for kv in ("all=A", "*=A", "全部=A", "Q1,Q2,Q3=A"):
        r = qa.run("review", "--answer", kv)
        assert r.rc != 0 and r.json["committed"] is False, r.describe()
    # 一次提交中夹带他人事项：整体不得让他人事项获批
    r = qa.run("review", "--answer", "Q2=全部同意", "--answer", "Q3=A")
    assert r.rc != 0, r.describe()
    after = fh.ledger(qa)
    assert after["decisions"] == 0 and after["actions"] == before["actions"]
    assert {h: i["status"] for h, i in fh.review_items(qa).items()} == {h: i["status"] for h, i in items_before.items()}
    assert _answered_events(qa) == 0
    rejected = qa.query("SELECT payload FROM events WHERE event_type='command_rejected'")
    assert rejected, "拒绝应留有审计"
    assert all("全部同意" not in e["payload"] for e in rejected), "审计不保存答复原文"
    # 修正为具体选项后可以继续
    ok = qa.run("review", "--answer", "Q1=A")
    assert ok.rc == 0 and len(fh.decisions(qa)) == 1, ok.describe()


@pytest.mark.ac("AC-134")
@pytest.mark.layer("offline")
def test_filling_someone_elses_name_does_not_sign_for_them(qa: QA):
    qa.ok("demo")
    # 日常入口：对产品负责的问题只填他人姓名
    r = qa.run("review", "--answer", "Q2=张三")
    assert r.rc != 0 and r.json["committed"] is False, r.describe()
    r = qa.run("review", "--answer", "Q2=A", "--note", "Q2=产品张三已口头同意")
    assert r.rc == 3 and r.error_code == "AUTHORIZATION_REQUIRED", r.describe()
    assert r.errors[0]["details"]["responsible_role"] == "product"
    assert "转交" in r.errors[0]["message"], "应说明可以转交给责任人"
    # 机器入口：只填确认人姓名，不带任何认证
    r = qa.api("clarification", "answer", "--story", fh.DEMO_TITLE, "--handle", "Q2", "--actor", "张三", "--option", "A")
    assert r.rc == 3 and r.error_code == "AUTHORIZATION_REQUIRED", r.describe()
    # 本地命令行不能登记“外部已核验”的决定
    f = qa.cwd / "decision.json"
    f.write_text(json.dumps({"verification": "external_verified", "role": "product", "actor": "张三",
                             "result": "approve"}, ensure_ascii=False), encoding="utf-8")
    r = qa.api("decision", "record", "--story", fh.DEMO_TITLE, "--file", str(f))
    assert r.rc == 3 and r.error_code == "AUTHORIZATION_INVALID", r.describe()
    assert "原生审批" in r.errors[0]["message"], "应指向原生认证入口"
    assert fh.decisions(qa) == []
    items = fh.review_items(qa)
    assert items["Q2"]["status"] == "pending" and items["Q3"]["status"] == "pending"
    # 转交后仍待处理，不因转交而获批
    t = qa.ok("review", "--transfer", "Q2=product")
    assert "待处理" in t.result["answers"][0]["note"]
    assert fh.review_items(qa)["Q2"]["status"] == "transferred" and fh.decisions(qa) == []
    v = flow.view(qa)
    assert "Q2" in [c["handle"] for c in v["reviews"]["others"]]


@pytest.mark.ac("AC-134")
@pytest.mark.layer("offline")
def test_expired_question_cannot_be_approved(qa: QA):
    """来源变化使旧问题过期后：泛化确认与对旧编号的新答复都不能批准它。"""
    qa.ok("demo")
    qa.ok("review", "--answer", "Q1=A")
    qa.ok("resume")
    assert [c["handle"] for c in flow.view(qa)["reviews"]["mine"]] == ["Q4"]
    fh.edit_source(qa.workspace(), "refund-requirement.md", "金额单位为分。", "金额单位为分。单笔退款上限另行约定。")
    r = qa.run("resume")
    assert r.rc in (0, 3) and r.result["source_changes"], r.describe()
    n = len(fh.decisions(qa))
    actions = fh.ledger(qa)["actions"]
    for answer in ("Q4=全部同意", "Q4=A", "Q4=reject"):
        rr = qa.run("review", "--answer", answer)
        assert rr.rc != 0 and rr.json["committed"] is False, f"过期事项不应接受 {answer}\n" + rr.describe()
    rr = qa.run("review", "--answer", "Q4=A")
    assert rr.error_code == "PRECONDITION_FAILED", rr.describe()
    assert "已变化" in rr.errors[0]["message"]
    assert len(fh.decisions(qa)) == n and fh.ledger(qa)["actions"] == actions
    v = flow.view(qa)
    assert fh.stage_of(v, "test_plan")["status"] != "completed"
    # 对新版本问题逐项作答后可以继续（不出现永久误锁）
    new_mine = v["reviews"]["mine"]
    assert new_mine and all(c["handle"] not in ("Q1", "Q4") for c in new_mine)
    ok = qa.run("review", "--answer", f"{new_mine[0]['handle']}=A")
    assert ok.rc == 0, ok.describe()
    assert len(fh.decisions(qa)) == n + 1


@pytest.mark.ac("AC-134")
@pytest.mark.layer("offline")
def test_existing_approval_does_not_extend_to_other_items(qa: QA):
    """已批准测试计划不等于批准其他事项：产品的关键预期与准出仍各自待处理。"""
    fh.to_confirmed(qa)
    decs = fh.decisions(qa)
    items = fh.review_items(qa)
    # 每个决定只绑定一个事项的对象
    by_ref = {i["decision_ref"]: h for h, i in items.items() if i.get("decision_ref")}
    assert len(by_ref) == len(decs)
    for d in decs:
        subj = [s["ref"] for s in json.loads(d["subjects"])]
        assert subj == [s["ref"] for s in items[by_ref[d["id"]]]["subject_refs"]]
        assert json.loads(d["scope"])["environment"] == "lab"
    # 实验审批桩的模拟答复不升级为真人或外部核验
    sim = [d for d in decs if d["role"] == "product"]
    assert sim and all(d["verification"] == "simulated" for d in sim)
    assert all(d["verification"] != "external_verified" for d in decs)
    # 产品的业务问题没有因为其他批准而被视为已答复
    assert items["Q2"]["status"] in ("pending", "transferred") and items["Q3"]["status"] in ("pending", "transferred")


# ---------------------------------------------------------------------------
# AC-136 错误包含可执行且安全的下一步
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-136")
@pytest.mark.layer("lab")
def test_entry_blocked_message_names_gap_and_safe_next_step(qa: QA):
    v0 = fh.to_entry_gap(qa)
    assert fh.stage_of(v0, "env_ready")["status"] == "completed"
    assert fh.stage_of(v0, "case_design")["status"] == "completed"
    smoke_before = [json.loads(r["canonical"])["payload"] for r in
                    qa.query("SELECT canonical FROM snapshots WHERE kind='case_attempt'")]
    assert [(a["tc_ref"], a["outcome"]) for a in smoke_before] == [("SMOKE-1", "pass")]
    attempt_ids = [r["id"] for r in qa.query("SELECT id FROM snapshots WHERE kind='case_attempt'")]

    # 请求提测接收
    e = qa.api("stage", "evaluate", "--story", fh.DEMO_TITLE, "--stage", "test_entry")
    assert e.rc == 0, e.describe()
    res = e.result
    assert res["status"] in ("blocked", "failed") and res["verdict"] != "pass", res
    reasons = "；".join(res["reasons"])
    assert "SMOKE-2" in reasons and "SMOKE-1" not in reasons, f"应只指出缺少的那一项：{reasons}"
    assert qa.query("SELECT COUNT(*) n FROM aggregates WHERE kind='stage' AND object_id LIKE '%test_entry%' "
                    "AND json_extract(state,'$.status')='completed'")[0]["n"] == 0

    # 人读提示：缺哪项、已有何结果、会执行什么
    s = qa.text("status")
    lines = s.out.splitlines()
    assert any(ln.startswith("当前：提测准入：") and "通过" not in ln.replace("未通过", "") for ln in lines), s.out
    assert any(ln.startswith("原因：") and "SMOKE-2" in ln and "未执行" in ln for ln in lines), s.out
    have = [ln for ln in lines if ln.startswith("已有：")][0]
    assert "环境与数据准备" in have and "用例设计" in have and "提测准入" not in have
    nexts = [ln for ln in lines if ln.startswith("下一步：")]
    run_next = [ln for ln in nexts if "冒烟" in ln]
    assert len(run_next) == 1 and "“resume”" in run_next[0] and "范围：" in run_next[0], nexts
    # 建议安全：只指向日常命令，不建议手改状态
    view = qa.ok("status").result
    for n in view["summary"]["next_actions"]:
        assert n["command"].split()[0] in DAILY, n
    for bad in fh.UNSAFE_HINTS:
        assert bad not in s.out and bad not in json.dumps(view["summary"], ensure_ascii=False), f"提示含不安全建议：{bad}"

    # 按提示继续：给出的命令在当前上下文可用
    r = qa.run("resume")
    assert r.rc in (0, 3), r.describe()
    after = flow.view(qa)
    assert fh.stage_of(after, "test_entry")["status"] == "completed", fh.stage_of(after, "test_entry")
    attempts = [json.loads(x["canonical"])["payload"] for x in
                qa.query("SELECT canonical FROM snapshots WHERE kind='case_attempt'")]
    assert any(a["tc_ref"] == "SMOKE-2" and a["purpose"] == "smoke" and a["outcome"] == "pass" for a in attempts)
    # 已有结果保留
    kept = {r["id"] for r in qa.query("SELECT id FROM snapshots WHERE kind='case_attempt'")}
    assert set(attempt_ids) <= kept


@pytest.mark.ac("AC-136")
@pytest.mark.layer("lab")
def test_daily_errors_carry_code_reason_and_safe_recovery(qa: QA):
    # 尚无任何工作区
    r0 = qa.text("status")
    assert r0.rc == 6 and "WORKSPACE_INVALID" in r0.err and "demo" in r0.err, r0.describe()
    qa.ok("demo")
    cases = [
        (("review",), 3, "REVIEW_REQUIRED"),
        (("review", "--answer", "Q2=A"), 3, "AUTHORIZATION_REQUIRED"),
        (("review", "--answer", "Q1=Z"), 2, "INVALID_ARGUMENT"),
        (("review", "--answer", "Q99=A"), 3, "NOT_FOUND"),
        (("resume", "--task", "不存在的需求"), 3, "SELECTION_REQUIRED"),
        (("export",), 2, "INVALID_ARGUMENT"),
    ]
    before = fh.ledger(qa)
    for args, rc, code in cases:
        t = qa.text(*args)
        assert t.rc == rc, t.describe()
        assert t.err.startswith("未完成：") and f"代码 {code}" in t.err, t.err
        advice = [ln for ln in t.err.splitlines() if ln.startswith("建议：")]
        assert len(advice) == 1 and len(advice[0]) > 5, t.err
        for bad in fh.UNSAFE_HINTS:
            assert bad not in t.err, f"{code} 的提示含不安全建议：{bad}"
        assert "Traceback" not in t.err
        j = qa.run(*args)
        assert j.rc == rc and j.error_code == code and j.json["committed"] is False
        assert j.errors[0]["recovery"] == advice[0][len("建议："):]
    after = fh.ledger(qa)
    assert after["decisions"] == before["decisions"] and after["actions"] == before["actions"]
    # REVIEW_REQUIRED 给出的答复示例可直接执行
    t = qa.text("review")
    example = [ln for ln in t.err.splitlines() if "答复示例：" in ln][0].split("答复示例：", 1)[1].split()
    assert example[:2] == ["review", "--answer"]
    ok = qa.run(*example)
    assert ok.rc == 0, ok.describe()
    # 代签被拒后按提示转交可用
    tr = qa.run("review", "--transfer", "Q2=product")
    assert tr.rc == 0, tr.describe()


# ---------------------------------------------------------------------------
# AC-142 新人验收报告计算可追溯
# ---------------------------------------------------------------------------

def _eval(qa: QA, tmp_path: Path, obs, name: str = "obs.json") -> dict:
    r = fh.evaluate_usability(qa, tmp_path, obs, name)
    assert r.rc == 0 and isinstance(r.json, dict), r.describe()
    return r.json


def _trial_with(index: int, **changes) -> list[dict]:
    obs = fh.full_trial(seconds=1)  # 每项仅 1 秒：平均耗时极低
    o = obs[index]
    obs[index] = fh.observation(o["participant_pseudonym"], o["task_id"], seconds=1, **changes)
    return obs


@pytest.mark.ac("AC-142")
@pytest.mark.layer("offline")
@pytest.mark.parametrize("label,changes", [
    ("一项求助", {"help_events": 1, "notes": "需要实现者告知内部命令"}),
    ("一项未完成", {"completed": False, "notes": "为安全中止"}),
])
def test_report_keeps_all_25_records_and_fails_on_help_or_incomplete(qa: QA, tmp_path: Path, label: str, changes: dict):
    obs = _trial_with(13, **changes)
    assert len(obs) == 25
    ref = fh.reference_report(obs)
    assert ref["verdict"] == "fail"
    rep = _eval(qa, tmp_path, obs)
    # 保留全部 25 项记录和失败
    assert rep["observations"] == obs, "报告必须原样保留全部观察记录"
    assert rep["participants"] == ref["participants"] and len(rep["participants"]) == 5
    assert rep["task_definitions"] == fh.TASK_IDS
    cc = rep["completion_counts"]
    assert cc["total_records"] == 25 and cc["required"] == 25
    assert cc["independent"] == ref["counts"]["independent"]
    assert rep["help_events"] == ref["counts"]["help_events"]
    # 不因平均耗时低而通过
    assert rep["timing"]["raw_seconds"] == pytest.approx([1.0] * 25)
    assert rep["timing"]["median_seconds"] == pytest.approx(1.0) and rep["timing"]["max_seconds"] == pytest.approx(1.0)
    assert rep["verdict"] == "fail", f"{label}：不应判通过"
    # 恢复单独计数
    rr = rep["recovery_results"]
    assert (rr["resume_success"], rr["resume_total"]) == (ref["counts"]["resume_success"], 5)


@pytest.mark.ac("AC-142")
@pytest.mark.layer("offline")
def test_manual_edits_and_unsafe_effects_are_counted_separately(qa: QA, tmp_path: Path):
    obs = fh.full_trial(seconds=1)
    obs[3] = fh.observation("P1", "N4", seconds=1, manual=2, notes="手改 JSON 两次")
    obs[9] = fh.observation("P2", "N5", seconds=1, unsafe=3, notes="重复建单")
    obs[12] = fh.observation("P3", "N3", seconds=1, repeated=1)
    ref = fh.reference_report(obs)
    rep = _eval(qa, tmp_path, obs)
    c = rep["counts"]
    assert c["manual_json_or_source_edits"] == ref["counts"]["manual"] == 2
    assert c["unsafe_or_duplicate_effects"] == ref["counts"]["unsafe"] == 3
    assert c["repeated_question_same_valid_scope"] == ref["counts"]["repeated"] == 1
    assert rep["help_events"] == 0
    assert rep["completion_counts"]["independent"] == 25
    assert rep["verdict"] == "fail", "25 项均完成且耗时很低，但存在手改与不安全动作，不能通过"
    # 单独一类非零也不通过
    for kw in ({"manual": 1}, {"unsafe": 1}, {"repeated": 1}):
        one = _trial_with(7, **kw)
        assert _eval(qa, tmp_path, one, "one.json")["verdict"] == "fail", kw


@pytest.mark.ac("AC-142")
@pytest.mark.layer("offline")
def test_report_timing_and_reference_values(qa: QA, tmp_path: Path):
    obs = []
    secs = iter(range(30, 30 + 25 * 7, 7))
    for p in ["P1", "P2", "P3", "P4", "P5"]:
        for t in fh.TASK_IDS:
            obs.append(fh.observation(p, t, seconds=next(secs)))
    ref = fh.reference_report(obs)
    rep = _eval(qa, tmp_path, {"observations": obs})
    assert rep["timing"]["raw_seconds"] == pytest.approx(ref["durations"])
    assert rep["timing"]["median_seconds"] == pytest.approx(ref["median"])
    assert rep["timing"]["max_seconds"] == pytest.approx(ref["max"])
    assert rep["verdict"] == ref["verdict"] == "pass", "25 项全部独立完成且各计数为 0 时应通过（对照组）"
    # 契约字段齐全；样本范围的限制随报告给出；不虚构改善比例
    for key in ("participants", "task_definitions", "observations", "completion_counts", "help_events",
                "recovery_results", "timing", "limitations", "verdict"):
        assert key in rep, f"usability_report 缺少字段 {key}"
    assert "5人样本" in rep["limitations"]
    dumped = json.dumps(rep["timing"], ensure_ascii=False)
    assert "improvement" not in dumped and "%" not in dumped


@pytest.mark.ac("AC-142")
@pytest.mark.layer("offline")
def test_incomplete_sample_from_fixture_does_not_pass(qa: QA, tmp_path: Path, spec: dict):
    sample = spec["fixtures"]["usability"]["sample_incomplete_trial"]
    assert sample == {"participants": 5, "tasks_per_participant": 5, "independent_completions": 24,
                      "implementation_help_events": 1, "expected_pass": False}
    obs = fh.full_trial()
    obs[17] = fh.observation("P4", "N3", completed=False, help_events=1, notes="实现者代操作")
    rep = _eval(qa, tmp_path, obs)
    assert rep["completion_counts"]["independent"] == sample["independent_completions"]
    assert rep["help_events"] == sample["implementation_help_events"]
    assert (rep["verdict"] == "pass") is sample["expected_pass"]


@pytest.mark.ac("AC-142")
@pytest.mark.layer("offline")
def test_no_observation_or_missing_cells_never_pass(qa: QA, tmp_path: Path):
    empty = _eval(qa, tmp_path, [], "empty.json")
    assert empty["verdict"] != "pass" and empty["completion_counts"]["total_records"] == 0
    assert empty["timing"]["median_seconds"] is None, "没有实测时不得给出耗时"
    # 四人，或缺少 N5 的记录：都不能通过
    four = fh.full_trial(["P1", "P2", "P3", "P4"])
    assert _eval(qa, tmp_path, four, "four.json")["verdict"] == "fail"
    no_n5 = [o for o in fh.full_trial() if o["task_id"] != "N5"]
    rep = _eval(qa, tmp_path, no_n5, "no_n5.json")
    assert rep["verdict"] == "fail"
    rr = rep["recovery_results"]
    assert rr["resume_total"] == 0 and rr["resume_success"] == 0
    # 分母为 0 时不得显示 100%
    flat = json.dumps(rr, ensure_ascii=False)
    assert "100" not in flat and not any(v == 1.0 and isinstance(v, float) for v in rr.values())


@pytest.mark.ac("AC-142")
@pytest.mark.layer("offline")
def test_malformed_observation_returns_structured_error(qa: QA, tmp_path: Path):
    obs = fh.full_trial()
    del obs[4]["task_id"]
    r = fh.evaluate_usability(qa, tmp_path, obs)
    assert "Traceback" not in r.err, "缺少字段的观察记录应返回稳定错误，而不是未处理异常\n" + r.err[-600:]
    assert r.rc != 0 and r.error_code in ("SCHEMA_INVALID", "VALIDATION_FAILED", "INVALID_ARGUMENT"), r.describe()


# ---------------------------------------------------------------------------
# 真人场景中可由程序核对的计算规则（不代表真人场景通过）
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-143")
@pytest.mark.layer("offline")
def test_calc_every_participant_must_complete_all_five(qa: QA, tmp_path: Path):
    """计算规则：每人 5 项均须独立完成；第六名参与者的失败不能被前 25 项完成数掩盖。"""
    obs = fh.full_trial(["P1", "P2", "P3", "P4", "P5", "P6"])
    obs[-2] = fh.observation("P6", "N4", completed=False, notes="未能按提示恢复")
    assert fh.reference_report(obs)["verdict"] == "fail"
    rep = _eval(qa, tmp_path, obs)
    assert rep["completion_counts"]["independent"] == 29 and rep["completion_counts"]["total_records"] == 30
    assert rep["verdict"] != "pass", "有参与者未独立完成全部 5 项，本轮不应判通过"


@pytest.mark.ac("AC-144")
@pytest.mark.layer("offline")
def test_calc_original_failure_kept_when_same_participant_retries(qa: QA, tmp_path: Path):
    """计算规则：原失败保留；同一参与者重做成功不能把本轮改判为通过。"""
    obs = fh.full_trial()
    failed = fh.observation("P3", "N4", completed=False, notes="首次尝试失败")
    obs.insert(13, failed)
    assert len(obs) == 26 and fh.reference_report(obs)["verdict"] == "fail"
    rep = _eval(qa, tmp_path, obs)
    assert failed in rep["observations"], "原失败记录必须保留"
    assert rep["completion_counts"]["independent"] == 25
    assert rep["verdict"] != "pass", "存在未独立完成的记录，本轮不应判通过"


@pytest.mark.ac("AC-144")
@pytest.mark.layer("offline")
@pytest.mark.parametrize("understanding", ["misread_lab_as_production", "incorrect", False])
def test_calc_key_misunderstanding_fails_the_round(qa: QA, tmp_path: Path, understanding):
    """计算规则：把 lab 通过误读为生产通过属于关键误解，本轮不通过（方案 §10.3）。"""
    obs = fh.full_trial()
    obs[0] = fh.observation("P1", "N1", understanding=understanding, notes="把实验通过理解为生产通过")
    assert fh.reference_report(obs)["verdict"] == "fail"
    rep = _eval(qa, tmp_path, obs)
    assert rep["observations"][0]["decision_understanding"] == understanding
    assert rep["verdict"] != "pass", "关键误解应使本轮不通过"


# ---------------------------------------------------------------------------
# 需要真人参与的场景：记为 blocked
# ---------------------------------------------------------------------------

_HUMAN = "需要未参与开发的真人参与者按 tests/usability_protocol.md 实测；尚未开展，程序不得生成“真人通过”"


@pytest.mark.ac("AC-135")
@pytest.mark.layer("usability")
def test_newcomer_distinguishes_local_answer_from_certified_approval():
    blocked(_HUMAN + "（N3：区分业务确认与已认证批准）")


@pytest.mark.ac("AC-138")
@pytest.mark.layer("usability")
def test_newcomer_recovers_from_fault_by_following_hints():
    blocked(_HUMAN + "（N4：按提示完成一次故障恢复）")


@pytest.mark.ac("AC-143")
@pytest.mark.layer("usability")
def test_five_newcomers_complete_five_tasks_independently():
    blocked(_HUMAN + "（5 人 × N1～N5）")


@pytest.mark.ac("AC-144")
@pytest.mark.layer("usability")
def test_misunderstandings_and_failures_stay_in_newcomer_results():
    blocked(_HUMAN + "（冻结观察记录、评审并用新参与者复验）")
