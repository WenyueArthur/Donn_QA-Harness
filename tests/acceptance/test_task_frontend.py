"""日常任务入口、首次实验、多任务选择、自动标识与续做的独立验收。

覆盖 AC-121～AC-127、AC-130～AC-132、AC-139～AC-141，以及方案 §10.4 的额外检查（标记为 EXTRA-10.4）。
全部通过子进程调用公共 CLI；预期来自规格与合成夹具，观测来自工作区数据库与实验桩数据库的只读查询。
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from support import flow
from support import frontend_helpers as fh
from support.cli import QA, tree_digest, write_json

STATUS_ZH = {"completed": "已完成", "blocked": "受阻", "failed": "未通过", "pending": "待处理", "stale": "已过期"}
DAILY = ("start", "resume", "review", "status", "export")


def _new_qa(base: Path) -> QA:
    for d in ("home", "工作区 根", "cwd"):
        (base / d).mkdir(parents=True, exist_ok=True)
    return QA(root=base / "工作区 根", home=base / "home", cwd=base / "cwd")


def _cwd_qa(qa: QA) -> QA:
    """不传 --workspace 时，产品在当前目录下使用 .qa-workspaces；这里只用于独立观测。"""
    return QA(root=qa.cwd / ".qa-workspaces", home=qa.home, cwd=qa.cwd)


def _other_profile(tmp: Path) -> Path:
    """另一个已核验项目的配置（合成），用于核对实验不改动其他项目。"""
    cfg = json.loads(fh.LAB_PROFILE.read_text(encoding="utf-8"))
    cfg["project"] = {"id": "PRJ-OTHER", "name": "其他项目（合成）"}
    return write_json(tmp / "other-profile.json", cfg)


def _task_rows(qa: QA, ws: Path | None = None) -> list[dict]:
    return [json.loads(r["state"]) for r in qa.query("SELECT state FROM aggregates WHERE kind='task'", ws=ws)]


def _story_counts(qa: QA, story: str, ws: Path | None = None) -> dict:
    def n(sql: str) -> int:
        return qa.query(sql, (story,), ws=ws)[0]["n"]

    return {"snapshots": n("SELECT COUNT(*) n FROM snapshots WHERE story_id=?"),
            "decisions": n("SELECT COUNT(*) n FROM decisions WHERE story_id=?"),
            "actions": n("SELECT COUNT(*) n FROM actions WHERE story_id=?"),
            "aggregates": qa.query("SELECT object_id, revision FROM aggregates WHERE story_id=? ORDER BY 1", (story,),
                                   ws=ws)}


# ---------------------------------------------------------------------------
# 共享的只读工作区
# ---------------------------------------------------------------------------

@dataclass
class DoneLab:
    qa: QA
    guard_log: Path
    other_ws: Path
    other_before: dict
    other_after: dict
    home_after: dict
    demo_text: str
    final_view: dict


@pytest.fixture(scope="module")
def done_lab(tmp_path_factory: pytest.TempPathFactory) -> DoneLab:
    """在网络守卫下完成一次完整的合成实验；旁边另有一个已核验项目。只记录事实，断言放在各测试里。"""
    base = tmp_path_factory.mktemp("done-lab")
    plain = _new_qa(base)
    r = plain.run("admin", "setup", "--config", str(_other_profile(base)), "--apply")
    assert r.rc == 0, r.describe()
    other_ws = plain.root / "PRJ-OTHER"
    before = tree_digest(other_ws)
    qa, log = fh.with_network_guard(plain, base)
    first = qa.text("demo", non_interactive=False)
    assert first.rc == 0, first.describe()
    v = flow.drive(qa)
    return DoneLab(qa=qa, guard_log=log, other_ws=other_ws, other_before=before, other_after=tree_digest(other_ws),
                   home_after=tree_digest(plain.home), demo_text=first.out + first.err, final_view=v)


@pytest.fixture(scope="module")
def failed_lab(tmp_path_factory: pytest.TempPathFactory) -> QA:
    """设计阶段已通过、build1 执行出现失败用例、未部署生产。只读使用。"""
    qa = _new_qa(tmp_path_factory.mktemp("failed-lab"))
    fh.to_failed_execution(qa)
    return qa


# ---------------------------------------------------------------------------
# AC-121 仅凭来源即可开始需求
# ---------------------------------------------------------------------------

def _single_project_in_cwd(qa: QA) -> QA:
    r = qa.run("admin", "setup", "--config", str(fh.LAB_PROFILE), "--apply", workspace=False)
    assert r.rc == 0 and r.json["applied"] is True, r.describe()
    return _cwd_qa(qa)


@pytest.mark.ac("AC-121")
@pytest.mark.layer("lab")
def test_start_with_source_only_creates_task_and_shows_next_step(qa: QA):
    obs = _single_project_in_cwd(qa)
    src = qa.cwd / "退款需求.md"
    shutil.copyfile(fh.LAB_REQUIREMENT, src)
    assert _task_rows(obs, obs.workspace()) == []

    # 只给来源：不传工作区、任何 ID 或 JSON
    r = qa.text("start", "--source", "./退款需求.md", workspace=False, non_interactive=False)
    assert r.rc == 0, r.describe()
    out = r.out

    tasks = _task_rows(obs)
    assert len(tasks) == 1, tasks
    t = tasks[0]
    assert t["source_uris"] == [str(src.resolve())]
    assert t["project"] == "退款实验（合成数据）" and obs.workspace().name == "PRJ-LAB-REFUND"
    # 展示需求、来源、环境与下一步
    assert f"需求：{t['display_title']}" in out
    assert "退款实验需求" in t["display_title"], "名称应取自来源文件标题"
    assert "来源：退款需求.md" in out
    assert "环境：lab" in out and "合成" in out
    nexts = [ln for ln in out.splitlines() if ln.startswith("下一步：")]
    assert nexts, out
    assert all(any(f"“{c}" in ln for c in DAILY) for ln in nexts), f"下一步应指向日常命令：{nexts}"
    # 默认输出不要求也不暴露内部标识
    for token in ("TASK-", "STORY-", "CMD-") + fh.INTERNAL_TERMS:
        assert token not in out, f"默认输出不应出现内部参数 {token}\n{out}"
    # 无需手工调用底层阶段命令：需求质检已自动生成并完成判定
    events = [e["event_type"] for e in obs.query("SELECT event_type FROM events ORDER BY seq")]
    assert "task_started" in events
    assert obs.query("SELECT COUNT(*) n FROM snapshots WHERE kind='quality_review'")[0]["n"] == 1
    v = qa.run("status", workspace=False)
    assert v.rc == 0, v.describe()
    assert fh.stage_of(v.result, "req_gate")["status"] == "completed"
    # 开始需求不产生平台写入
    assert fh.platform_rows(obs, "SELECT COUNT(*) FROM items") in ([], [(0,)])


@pytest.mark.ac("AC-121")
@pytest.mark.layer("lab")
def test_start_same_source_again_reuses_existing_task(qa: QA):
    """同一来源再次 start：应沿用原任务并正常返回，不新建任务、不报内部异常。"""
    obs = _single_project_in_cwd(qa)
    shutil.copyfile(fh.LAB_REQUIREMENT, qa.cwd / "退款需求.md")
    first = qa.text("start", "--source", "./退款需求.md", workspace=False)
    assert first.rc == 0, first.describe()
    again = qa.text("start", "--source", "./退款需求.md", workspace=False)
    assert "Traceback" not in again.err, "不应向用户输出未处理异常\n" + again.describe()
    assert again.rc == 0, again.describe()
    assert len(_task_rows(obs)) == 1
    assert "已有任务" in again.out


# ---------------------------------------------------------------------------
# AC-122 多任务与非交互场景不猜选择
# ---------------------------------------------------------------------------

SECOND_TITLE = "退款二期（合成）"


def _two_tasks(qa: QA) -> tuple[dict, dict]:
    qa.ok("demo")
    src = qa.cwd / "退款二期需求.md"
    shutil.copyfile(fh.LAB_REQUIREMENT, src)
    qa.ok("start", "--source", str(src), "--title", SECOND_TITLE)
    tasks = {t["display_title"]: t for t in _task_rows(qa)}
    assert set(tasks) == {fh.DEMO_TITLE, SECOND_TITLE}
    return tasks[fh.DEMO_TITLE], tasks[SECOND_TITLE]


@pytest.mark.ac("AC-122")
@pytest.mark.layer("offline")
def test_resume_without_selection_returns_selection_required(qa: QA):
    first, second = _two_tasks(qa)
    before = fh.ledger(qa)
    r = qa.run("resume")  # --non-interactive --json，未作选择
    assert r.rc == 3, "两个需求均可继续时不应默认选择其中一个\n" + r.describe()
    assert r.error_code == "SELECTION_REQUIRED"
    cands = r.errors[0]["details"]["candidates"]
    assert {c["title"] for c in cands} == {fh.DEMO_TITLE, SECOND_TITLE}
    assert all(c.get("source") and c.get("environment") for c in cands)
    assert r.json["committed"] is False
    assert fh.ledger(qa) == before, "未选择时不得对任何需求执行操作"


@pytest.mark.ac("AC-122")
@pytest.mark.layer("offline")
def test_status_and_review_without_selection_do_not_default_to_last_task(qa: QA):
    _two_tasks(qa)
    for cmd in ("status", "review"):
        r = qa.run(cmd)
        assert r.error_code == "SELECTION_REQUIRED" and r.rc == 3, f"{cmd} 不应默选最后一个需求\n" + r.describe()


@pytest.mark.ac("AC-122")
@pytest.mark.layer("offline")
def test_resume_with_business_name_only_touches_selected_task(qa: QA):
    first, second = _two_tasks(qa)
    untouched = _story_counts(qa, second["story_id"])
    events_before = fh.ledger(qa)["event_seq"]
    r = qa.run("resume", "--task", fh.DEMO_TITLE)
    assert r.rc == 0, r.describe()
    assert r.result["task"]["display_title"] == fh.DEMO_TITLE
    assert _story_counts(qa, second["story_id"]) == untouched, "未被选择的需求不应有任何变化"
    assert fh.ledger(qa)["event_seq"] > events_before
    # 名称不唯一时同样要求明确选择，并列出候选
    amb = qa.run("resume", "--task", "退款")
    assert amb.rc == 3 and amb.error_code == "SELECTION_REQUIRED", amb.describe()
    assert len(amb.errors[0]["details"]["candidates"]) == 2


@pytest.mark.ac("AC-122", "EXTRA-10.4")
@pytest.mark.layer("offline")
def test_non_interactive_never_waits_for_terminal_input(qa: QA):
    """即使连接着终端，--non-interactive 缺少选择或决定时也立即返回阻断。"""
    _two_tasks(qa)
    before = fh.ledger(qa)
    rc, out, timed_out = fh.tty_run(qa, ["resume", "--task", "退款", "--non-interactive"], timeout=30)
    assert not timed_out, "非交互调用不得等待终端输入\n" + out
    assert rc == 3 and "SELECTION_REQUIRED" in out and "输入序号" not in out, out
    # 缺少决定：有待本人确认的事项时 review 立即返回 REVIEW_REQUIRED
    rc, out, timed_out = fh.tty_run(qa, ["review", "--task", fh.DEMO_TITLE, "--non-interactive"], timeout=30)
    assert not timed_out, out
    assert rc == 3 and "REVIEW_REQUIRED" in out and "你的选择" not in out, out
    assert fh.ledger(qa) == before


@pytest.mark.ac("AC-122", "EXTRA-10.4")
@pytest.mark.layer("offline")
def test_non_interactive_without_choice_on_terminal_blocks_immediately(qa: QA):
    """连接终端、未给任何选择：应立即返回 SELECTION_REQUIRED，而不是等待输入或默选。"""
    _two_tasks(qa)
    rc, out, timed_out = fh.tty_run(qa, ["resume", "--non-interactive"], timeout=60)
    assert not timed_out, "非交互调用不得等待终端输入\n" + out
    assert rc == 3 and "SELECTION_REQUIRED" in out, f"rc={rc}\n{out}"


# ---------------------------------------------------------------------------
# AC-123 人读输出与机器回执语义一致
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-123")
@pytest.mark.layer("offline")
def test_status_text_and_json_describe_same_state(qa: QA):
    qa.ok("demo")
    flow.answer_mine(qa)
    qa.ok("resume")
    before = fh.ledger(qa)
    js = qa.ok("status")
    text = qa.text("status")
    detail = qa.text("status", "--details")
    assert text.rc == 0 and detail.rc == 0
    assert fh.ledger(qa) == before, "三次查询之间状态不应变化（指向同一版本）"
    v = js.result
    statuses = {s["status"] for s in v["stages"]}
    assert "completed" in statuses and statuses & {"blocked", "pending", "failed"}, statuses

    # 机器回执字段齐全，修订号与数据库事件序号一致
    assert set(js.json) >= {"command_id", "status", "committed", "revision", "result", "errors", "warnings"}
    assert js.json["revision"] == before["event_seq"]

    out = text.out
    lines = out.splitlines()

    def line(prefix: str) -> str:
        m = [ln for ln in lines if ln.startswith(prefix)]
        assert m, f"缺少“{prefix}”行\n{out}"
        return m[0]

    assert v["task"]["display_title"] in line("需求：")
    for s in v["task"]["sources"]:
        assert s in line("来源：")
    assert v["task"]["environment"] in line("来源：")
    assert v["summary"]["headline"] in line("当前：")
    for reason in v["summary"]["reasons"][:3]:
        assert reason in line("原因：")
    for title in v["summary"]["have"]:
        assert title in line("已有：")
    next_lines = [ln for ln in lines if ln.startswith("下一步：")]
    assert len(next_lines) == len(v["summary"]["next_actions"][:3])
    for n, ln in zip(v["summary"]["next_actions"], next_lines):
        assert n["effect"] in ln and n["command"] in ln
    for c in v["reviews"]["others"]:
        assert c["business_question"] in line("需他人决定：")

    # 默认摘要不展示修订号、租约、命令 ID 等内部参数
    for token in ("CMD-", "TASK-", "STORY-", str(js.json["command_id"])) + fh.INTERNAL_TERMS:
        assert token not in out, f"默认输出不应出现 {token}\n{out}"

    # 详情与 JSON 指向同一阶段状态与同一判定记录（证据）
    for s in v["stages"]:
        row = [ln for ln in detail.out.splitlines() if ln.strip().startswith(f"- {s['title']}：")]
        assert len(row) == 1, f"详情缺少阶段 {s['title']}\n{detail.out}"
        assert STATUS_ZH[s["status"]] in row[0], f"{s['title']} 状态与 JSON 不一致：{row[0]} / {s['status']}"
        if s.get("evaluation_ref"):
            assert s["evaluation_ref"] in detail.out
            assert qa.query("SELECT COUNT(*) n FROM snapshots WHERE id=?", (s["evaluation_ref"],))[0]["n"] == 1


@pytest.mark.ac("AC-123")
@pytest.mark.layer("offline")
def test_status_json_matches_independent_database_facts(failed_lab: QA):
    """JSON 中的未执行/失败与数据库里的尝试记录一致，人读详情给出同样的计数。"""
    qa = failed_lab
    v = qa.ok("status").result
    attempts = [json.loads(r["canonical"])["payload"] for r in
                qa.query("SELECT canonical FROM snapshots WHERE kind='case_attempt'")]
    failed_tc = sorted({a["tc_ref"] for a in attempts if a["outcome"] == "fail"})
    assert failed_tc == ["C1", "C3"], failed_tc
    exe = fh.stage_of(v, "test_execution")
    assert exe["status"] == "failed" and exe["verdict"] == "fail"
    assert all(tc in "；".join(exe["reasons"]) for tc in failed_tc)
    text = qa.text("status").out
    assert "测试执行：未通过" in text and "C1" in text and "C3" in text
    detail = qa.text("status", "--details").out
    assert "fail=2" in detail and "pass=5" in detail


# ---------------------------------------------------------------------------
# AC-124 无账号离线完成首次实验
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-124")
@pytest.mark.layer("lab")
def test_demo_completes_offline_without_accounts(done_lab: DoneLab):
    qa = done_lab.qa
    # 首屏明确标注实验环境
    assert "合成实验" in done_lab.demo_text and "不代表真实环境或生产通过" in done_lab.demo_text
    assert "环境：lab" in done_lab.demo_text
    # 无参数、无终端输入即可完成：没有要求填写配置
    v = done_lab.final_view
    assert v["lab"] is True
    st = flow.stage_map(v)
    assert st["test_exit"]["status"] == "completed", {k: s["status"] for k, s in st.items()}
    assert st["release_verify"]["status"] != "completed"
    # 只访问本机资源：网络守卫已装载且没有任何外部连接或解析
    log = done_lab.guard_log.read_text(encoding="utf-8").splitlines()
    assert any(ln.startswith("LOADED") for ln in log), "网络守卫未装载，结论无效"
    assert [ln for ln in log if not ln.startswith("LOADED")] == [], log
    # 证据来源全部是本机实验或导入资料，没有真实测试/生产来源
    origins = {r["origin"] for r in qa.query("SELECT DISTINCT origin FROM evidence", ws=qa.root / "lab-demo")}
    assert origins and origins <= {"local_lab", "imported", "simulated", "local"}, origins
    envs = {r["environment"] for r in qa.query("SELECT DISTINCT environment FROM actions", ws=qa.root / "lab-demo")}
    assert envs == {"lab"}, envs
    # 不改动其他项目的配置与数据，不写入 HOME
    assert done_lab.other_after == done_lab.other_before
    assert done_lab.home_after == {}, f"不应在 HOME 写入文件：{list(done_lab.home_after)}"


@pytest.mark.ac("AC-124")
@pytest.mark.layer("lab")
def test_demo_needs_no_configuration_input(qa: QA, home: Path):
    """demo 不接受也不需要配置参数；关闭标准输入也能启动。"""
    r = qa.text("demo", non_interactive=False)
    assert r.rc == 0, r.describe()
    assert "合成实验目录" in r.out
    assert [p.name for p in qa.workspaces()] == ["lab-demo"]
    assert tree_digest(home) == {}
    cfg = json.loads(next((qa.workspace() / "config").glob("*.json")).read_text(encoding="utf-8"))
    assert cfg["profile"] == "lab"
    assert all(t["environment"] == "lab" for t in cfg["targets"])
    assert all(a.get("credential_ref") is None for a in cfg["adapters"])


# ---------------------------------------------------------------------------
# AC-125 实验不能冒充真实测试或生产
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-125")
@pytest.mark.layer("lab")
def test_lab_export_keeps_watermark_and_origin(done_lab: DoneLab, tmp_path: Path):
    qa = done_lab.qa
    r = qa.ok("export", "--output", str(tmp_path / "交付"))
    files = r.result["files"]
    md = Path(files["markdown"]).read_text(encoding="utf-8")
    html = Path(files["html"]).read_text(encoding="utf-8")
    js = json.loads(Path(files["json"]).read_text(encoding="utf-8"))
    for name, body in (("markdown", md), ("html", html)):
        assert "合成实验" in body and "不代表真实环境验收、真实业务或生产通过" in body, f"{name} 缺少实验水印"
        assert "refund-requirement.md" in body, f"{name} 缺少来源"
    assert js["lab"] is True and "合成实验" in js["watermark"]
    assert "lab" in js["task"]["environment"]
    assert js["task"]["sources"]
    # 不以“上手完成”宣称真实业务或生产验证通过
    unverified = "；".join(js["unverified"])
    assert "不代表真实环境" in unverified and "上线验证" in unverified and "新人可用性尚未验证" in unverified
    release = [s for s in js["stages"] if s["stage"] == "release_verify"][0]
    assert release["status"] != "completed" and release["verdict"] != "pass"
    text = qa.text("export", "--output", str(tmp_path / "交付2")).out
    assert "未验证范围" in text and "合成实验" in text


@pytest.mark.ac("AC-125")
@pytest.mark.layer("lab")
def test_lab_evidence_rejected_as_production_proof(done_lab: DoneLab, tmp_path: Path):
    qa = done_lab.qa
    ws = qa.root / "lab-demo"
    ev = qa.query("SELECT id FROM evidence WHERE origin='local_lab' ORDER BY id LIMIT 1", ws=ws)[0]["id"]
    dep = json.loads(qa.query("SELECT canonical FROM snapshots WHERE kind='deployment' ORDER BY created_at DESC LIMIT 1",
                              ws=ws)[0]["canonical"])["payload"]
    before = fh.ledger(qa, ws)
    payload = {"deployment": dep["deployment_id"], "actual_build": dep["build"], "environment": "production",
               "window": {"start": "2026-11-16T00:00:00Z", "end": "2026-11-16T01:00:00Z"},
               "probes": [{"id": "P1", "result": "pass", "evidence_ref": ev}],
               "metric_samples": [], "thresholds": [], "verdict": "pass"}
    f = write_json(tmp_path / "release_check.json", payload)
    r = qa.api("artifact", "submit", "--story", fh.DEMO_TITLE, "--kind", "release_check", "--file", str(f))
    assert r.rc == 4 and r.error_code == "ORIGIN_MISMATCH", r.describe()
    assert r.json["committed"] is False
    assert r.errors[0]["path"] == "payload.probes[0].evidence_ref" and ev in r.errors[0]["related_refs"]
    # 把导出的实验报告当作准出报告提交同样被拒绝
    exp = qa.ok("export", "--output", str(tmp_path / "交付")).result["files"]["json"]
    r2 = qa.api("artifact", "submit", "--story", fh.DEMO_TITLE, "--kind", "exit_report", "--file", exp)
    assert r2.rc != 0 and r2.json["committed"] is False, r2.describe()
    after = fh.ledger(qa, ws)
    assert after["snapshots"] == before["snapshots"], "被拒绝的证明不得登记为产物"
    assert qa.query("SELECT COUNT(*) n FROM snapshots WHERE kind='release_check'", ws=ws)[0]["n"] == 0
    v = qa.ok("status").result
    assert fh.stage_of(v, "release_verify")["status"] != "completed"
    assert qa.query("SELECT COUNT(*) n FROM events WHERE event_type='command_rejected' AND seq>?",
                    (before["event_seq"],), ws=ws)[0]["n"] >= 1, "来源不符的拒绝应留有审计"


# ---------------------------------------------------------------------------
# AC-126 首次引导中断后继续
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-126")
@pytest.mark.layer("lab")
def test_resume_after_closing_terminal_keeps_answers_and_position(qa: QA, tmp_path: Path):
    qa.ok("demo")
    q1 = flow.view(qa)["reviews"]["mine"][0]
    qa.ok("review", "--answer", f"{q1['handle']}={q1['options'][0]['key']}")
    src_before = qa.query("SELECT COUNT(*) n FROM snapshots WHERE kind='source_revision'")[0]["n"]
    dec_before = fh.decisions(qa)
    assert len(dec_before) == 1

    # 关闭终端后在另一个目录重新打开：只共享磁盘上的工作区
    other_cwd = tmp_path / "另一个终端"
    other_cwd.mkdir()
    later = QA(root=qa.root, home=qa.home, cwd=other_cwd)
    seen = later.text("status")
    assert seen.rc == 0, seen.describe()
    assert f"需求：{fh.DEMO_TITLE}" in seen.out and "已有：需求质检、技术方案质检" in seen.out

    r = later.run("resume")  # 不重新提供任何资料
    assert r.rc == 0, r.describe()
    v = flow.view(later)
    assert len(_task_rows(later)) == 1
    assert later.query("SELECT COUNT(*) n FROM snapshots WHERE kind='source_revision'")[0]["n"] == src_before
    # 已生效答复不重复询问
    assert [d["id"] for d in fh.decisions(later)] == [d["id"] for d in dec_before]
    assert q1["business_question"] not in [c["business_question"] for c in fh.cards(v)]
    assert fh.stage_of(v, "req_analysis")["status"] == "completed", "应从保存位置继续"
    # 只继续获准步骤：测试计划未批准前不执行用例
    assert fh.stage_of(v, "test_plan")["status"] != "completed"
    kinds = {a["kind"] for a in later.query("SELECT kind FROM actions")}
    assert "execute.cases" not in kinds and "environment.probe" not in kinds, kinds
    assert any(c["kind"] == "测试计划批准" for c in v["reviews"]["mine"])

    # 再次执行 demo 也只是继续原任务
    again = later.run("demo")
    assert again.rc == 0, again.describe()
    assert len(_task_rows(later)) == 1 and len(fh.decisions(later)) == 1


# ---------------------------------------------------------------------------
# AC-127 管理员配置一次后测试人员复用
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-127")
@pytest.mark.layer("lab")
def test_tester_reuses_admin_verified_configuration(qa: QA):
    # 管理员：先预览，再应用
    preview = qa.run("admin", "setup", "--config", str(fh.LAB_PROFILE))
    assert preview.rc == 0 and preview.json["applied"] is False and preview.json["preview_only"] is True
    assert qa.workspaces() == [], "预览不得建立或修改工作区"
    applied = qa.run("admin", "setup", "--config", str(fh.LAB_PROFILE), "--apply")
    assert applied.rc == 0 and applied.json["applied"] is True and applied.json["problems"] == []
    ws = qa.workspace()
    cfg_before = tree_digest(ws / "config")
    assert cfg_before
    cfg_events = qa.query("SELECT COUNT(*) n FROM events WHERE event_type='config_applied'")[0]["n"]

    # 测试人员：只提供来源，不提供账号、路径或策略
    src = qa.cwd / "退款需求.md"
    shutil.copyfile(fh.LAB_REQUIREMENT, src)
    started = qa.text("start", "--source", str(src))
    assert started.rc == 0, started.describe()
    assert "环境：lab" in started.out
    for word in ("密码", "token", "凭证", "账号"):
        assert word not in started.out, f"不应向测试人员索取 {word}"
    mine = flow.view(qa)["reviews"]["mine"]
    assert mine, "应有归测试人员答复的问题"
    qa.ok("review", "--answer", f"{mine[0]['handle']}={mine[0]['options'][0]['key']}")
    cont = qa.run("resume")
    assert cont.rc == 0, cont.describe()

    # 权限边界不变：测试人员不能代产品作决定
    other = flow.view(qa)["reviews"]["others"][0]
    n_dec = len(fh.decisions(qa))
    deny = qa.run("review", "--answer", f"{other['handle']}={other['options'][0]['key']}")
    assert deny.rc == 3 and deny.error_code == "AUTHORIZATION_REQUIRED", deny.describe()
    assert len(fh.decisions(qa)) == n_dec
    assert all(d["role"] == "tester" for d in fh.decisions(qa))
    # 受审配置未被日常命令改动
    assert tree_digest(ws / "config") == cfg_before
    assert qa.query("SELECT COUNT(*) n FROM events WHERE event_type='config_applied'")[0]["n"] == cfg_events


# ---------------------------------------------------------------------------
# AC-130 继续任务自动维护幂等标识
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-130")
@pytest.mark.layer("lab")
def test_repeated_resume_reuses_operation_identity_and_writes_once(qa: QA):
    fh.to_failed_execution(qa)
    # 建单请求已发出，进程在展示回执前中断
    r = qa.test_mode(QA_FLOW_CRASH_AT="after_send").run("resume")
    assert r.rc == fh.CRASH_RC, r.describe()
    creates = fh.platform_rows(qa, "SELECT COUNT(*) FROM calls WHERE op='create'")[0][0]
    assert creates == 1 and len(fh.platform_rows(qa, "SELECT id FROM items")) == 1

    # 用户不知道任何内部标识，连续两次继续同一检查点
    for _ in range(2):
        rr = qa.run("resume")
        assert rr.rc in (0, 3, 5), rr.describe()
        assert fh.platform_rows(qa, "SELECT COUNT(*) FROM calls WHERE op='create'")[0][0] == 1, "不得重复建单"
        assert len(fh.platform_rows(qa, "SELECT id FROM items")) == 1
    act = qa.query("SELECT id, idempotency_key, attempts FROM actions WHERE kind='defect.create'")
    assert len(act) == 1 and act[0]["attempts"] == 1, act

    # 原工作进程租约到期后，继续时先核对而不是重发
    later = qa.test_mode(QA_FLOW_TEST_CLOCK=_hours_later(qa, 2))
    for _ in range(3):
        rr = later.run("resume")
        assert rr.rc in (0, 3, 5), rr.describe()
    items = fh.platform_rows(qa, "SELECT id, idem_key FROM items")
    defects = flow.view(qa)["defects"]
    assert len(defects) == 2 and len(items) == 2, (defects, items)
    assert len({k for _, k in items}) == 2
    assert fh.platform_rows(qa, "SELECT COUNT(*) FROM calls WHERE op='create' AND outcome='ok'")[0][0] == 2
    assert sorted(d["external_id"] for d in defects) == sorted(i for i, _ in items)
    again = later.run("resume")
    assert again.rc in (0, 3), again.describe()
    assert len(fh.platform_rows(qa, "SELECT id FROM items")) == 2
    first = qa.query("SELECT attempts, state FROM actions WHERE id=?", (act[0]["id"],))[0]
    assert first == {"attempts": 1, "state": "succeeded"}, "首个建单动作应经核对确认，而不是重新发送"

    # 操作身份由系统持久化；日常命令不接受也不要求用户提供
    assert qa.query("SELECT COUNT(*) n FROM commands WHERE command_id LIKE 'CMD-%'")[0]["n"] > 0
    for flag in ("--command-id", "--expected-revision"):
        bad = qa.run("resume", flag, "1")
        assert bad.rc == 2 and bad.error_code == "INVALID_ARGUMENT", bad.describe()


def _hours_later(qa: QA, hours: int) -> str:
    from datetime import datetime, timedelta, timezone

    latest = qa.query("SELECT MAX(created_at) t FROM events")[0]["t"]
    base = datetime.fromisoformat(latest.replace("Z", "+00:00")).astimezone(timezone.utc)
    return (base + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# AC-131 自动续做不能跳过变更门禁
# ---------------------------------------------------------------------------

def _confirmed_then_source_changed(qa: QA) -> dict:
    fh.to_confirmed(qa)
    fh.crash_resume(qa, "after_commit:script.register_lab")  # 预期与计划已确认，尚未执行任何用例
    v = flow.view(qa)
    assert fh.stage_of(v, "test_plan")["status"] == "completed"
    assert fh.stage_of(v, "case_design")["status"] == "completed"
    state = {"ledger": fh.ledger(qa), "decisions": fh.decisions(qa), "items": fh.review_items(qa)}
    fh.edit_source(qa.workspace(), "refund-requirement.md", "金额单位为分。", "金额单位为分。单笔退款上限另行约定。")
    return state


@pytest.mark.ac("AC-131")
@pytest.mark.layer("lab")
def test_resume_shows_change_and_stops_stale_work(qa: QA):
    before = _confirmed_then_source_changed(qa)
    r = qa.run("resume")
    assert r.rc in (0, 3), r.describe()
    res = r.result
    # 先显示变更
    assert res["source_changes"], "应报告来源变化"
    assert any("来源已变化" in n and "refund-requirement.md" in n for n in res["notes"]), res["notes"]
    after = fh.ledger(qa)
    # 停止已不适用的动作：没有新的探测、执行或外部写入
    assert after["actions"] == before["ledger"]["actions"]
    assert after["action_attempts"] == before["ledger"]["action_attempts"]
    assert after["platform"] == before["ledger"]["platform"] and after["refund"] == before["ledger"]["refund"]
    # 不自动重签
    assert [d["id"] for d in fh.decisions(qa)] == [d["id"] for d in before["decisions"]]
    # 过期结果不算通过
    st = {s["stage"]: s for s in res["stages"]}
    for sid in ("req_analysis", "test_plan", "case_design", "case_qc", "env_ready", "test_entry", "test_execution",
                "test_exit"):
        assert st[sid]["status"] != "completed" and st[sid]["verdict"] != "pass", (sid, st[sid])
    assert res["verdict"] != "pass"
    # 受影响的确认重新待处理，并展示给用户
    old_q1 = before["items"]["Q1"]["business_question"]
    pending = [c for c in res["reviews"]["mine"] if c["business_question"] == old_q1]
    assert len(pending) == 1 and pending[0]["status"] == "pending" and pending[0]["handle"] != "Q1"
    text = qa.text("status").out
    assert old_q1 in text or "review" in text


@pytest.mark.ac("AC-131")
@pytest.mark.layer("lab")
def test_second_resume_after_change_still_does_not_execute(qa: QA):
    before = _confirmed_then_source_changed(qa)
    for _ in range(2):
        r = qa.run("resume")
        assert r.rc in (0, 3), r.describe()
    after = fh.ledger(qa)
    assert after["actions"] == before["ledger"]["actions"]
    assert after["action_attempts"] == before["ledger"]["action_attempts"]
    assert len(fh.decisions(qa)) == len(before["decisions"])


@pytest.mark.ac("AC-131")
@pytest.mark.layer("lab")
def test_old_answers_are_not_reported_as_recorded_after_change(qa: QA):
    """来源变化后重复提交旧编号的原答复：不得显示“已记录”，也不得产生新决定。"""
    before = _confirmed_then_source_changed(qa)
    qa.run("resume")
    n = len(fh.decisions(qa))
    for handle, opt in (("Q1", "A"), ("Q4", "A")):
        r = qa.text("review", "--answer", f"{handle}={opt}")
        assert len(fh.decisions(qa)) == n
        assert f"已记录 {handle} 的答复" not in r.out, (
            f"{handle} 所依据的内容已变化，旧答复不应被显示为已记录\n" + r.describe())
        assert r.rc != 0, f"{handle} 已过期或被新问题取代，应提示查看新版本\n" + r.describe()


# ---------------------------------------------------------------------------
# AC-132 查看不会偷偷续做或重试
# ---------------------------------------------------------------------------

@pytest.fixture()
def unknown_state(qa: QA) -> QA:
    """存在失败用例、结果未知的建单操作和待他人确认的问题。"""
    fh.to_confirmed(qa)
    f = qa.test_mode().api("lab", "fault", "--name", "create_succeeded_response_lost")
    assert f.rc == 0, f.describe()
    r = qa.run("resume")
    assert r.rc in (0, 5), r.describe()
    v = flow.view(qa)
    assert v["unknown_actions"], "前置：应有结果未知的操作"
    assert fh.stage_of(v, "test_execution")["status"] == "failed"
    assert v["reviews"]["others"], "前置：应有待确认事项"
    return qa


@pytest.mark.ac("AC-132")
@pytest.mark.layer("offline")
def test_viewing_and_exporting_do_not_execute_or_retry(unknown_state: QA, tmp_path: Path):
    qa = unknown_state
    before = fh.ledger(qa)
    assert any(s == "unknown" for _, s, _ in before["action_states"])
    for i in range(3):
        t = qa.text("status")
        assert t.rc == 0
        assert "结果未知：创建缺陷" in t.out and "不会重发" in t.out, t.out
        assert "测试执行：未通过" in t.out
        d = qa.text("status", "--details")
        assert d.rc == 0 and "未通过／不通过" in d.out and "结果未知" in d.out
        j = qa.ok("status").result
        assert len(j["unknown_actions"]) == 1 and fh.stage_of(j, "test_execution")["verdict"] == "fail"
        e = qa.run("export", "--output", str(tmp_path / f"导出{i}"))
        assert e.rc == 0, e.describe()
        assert fh.ledger(qa) == before, "查看、详情与导出不得产生新执行、新事件或外部写入"
    rv = qa.text("review")  # 仅查看待确认
    assert rv.rc in (0, 3)
    assert fh.ledger(qa) == before

    # 只有显式继续才处理允许的恢复任务：先核对，不重发
    r = qa.run("resume")
    assert r.rc in (0, 3), r.describe()
    after = fh.ledger(qa)
    assert after["event_seq"] > before["event_seq"]
    assert not any(s == "unknown" for _, s, _ in after["action_states"])
    assert fh.platform_rows(qa, "SELECT COUNT(*) FROM calls WHERE op='create'")[0][0] == \
        len(fh.platform_rows(qa, "SELECT id FROM items")) == 2


@pytest.mark.ac("AC-132")
@pytest.mark.layer("offline")
def test_exported_report_keeps_unknown_and_failure_visible(unknown_state: QA, tmp_path: Path):
    qa = unknown_state
    files = qa.ok("export", "--output", str(tmp_path / "导出")).result["files"]
    md = Path(files["markdown"]).read_text(encoding="utf-8")
    assert "未通过" in md and "失败" in md, "导出报告应保留失败"
    assert "结果未知" in md or "待核对" in md, "导出报告应保留结果未知的操作，不能只展示已确定的内容\n" + md[-1500:]


# ---------------------------------------------------------------------------
# AC-139 日常摘要与可选详情分层
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-139")
@pytest.mark.layer("offline")
def test_summary_first_screen_and_optional_details(failed_lab: QA):
    qa = failed_lab
    v = qa.ok("status").result
    st = flow.stage_map(v)
    # 前置：设计通过、执行失败、未部署生产
    assert st["case_design"]["status"] == "completed" and st["case_qc"]["status"] == "completed"
    assert st["test_execution"]["status"] == "failed"
    assert st["release_verify"]["status"] == "pending"

    summary = qa.text("status").out
    lines = summary.splitlines()
    assert len(lines) <= 15, "首屏应是简短摘要"
    assert any(ln.startswith("当前：测试执行：未通过") for ln in lines), summary
    assert any(ln.startswith("原因：") and "C1" in ln and "C3" in ln for ln in lines), summary
    assert any(ln.startswith("下一步：") for ln in lines), summary
    assert "阶段详情" not in summary and "判定记录" not in summary
    # 设计通过只列为已有结果，不暗示业务或生产通过
    have = [ln for ln in lines if ln.startswith("已有：")][0]
    assert "用例设计" in have and "测试执行" not in have and "测试准出" not in have and "上线验证" not in have
    assert "不代表真实环境或生产通过" in summary
    assert v["verdict"] != "pass"
    assert not any(ln.startswith("当前：") and "通过" in ln and "未通过" not in ln for ln in lines)

    detail = qa.text("status", "--details").out
    assert detail.startswith(summary.rstrip("\n").rsplit("\n", 1)[0][:20])
    assert "阶段详情" in detail
    row = [ln for ln in detail.splitlines() if ln.strip().startswith("- 上线验证：")][0]
    assert "待处理" in row and "不可判定" in row and "尚无实际生产部署" in row
    # 详情能查证据：判定记录与输入对象确实存在
    import re

    refs = set(re.findall(r"\b(?:SE|ATT|ENV|CD|CQR|PLAN|TA|TR|QR|DEP|ENT|TRP|EXT)-[0-9A-F]{12}\b", detail))
    assert any(r.startswith("ATT-") for r in refs) and any(r.startswith("SE-") for r in refs), refs
    for ref in refs:
        assert qa.query("SELECT COUNT(*) n FROM snapshots WHERE id=?", (ref,))[0]["n"] == 1, f"详情引用的 {ref} 不存在"
    assert "测试结果（构建 build1）" in detail and "fail=2" in detail
    assert detail.count("缺陷 BUG-") == 2


@pytest.mark.ac("AC-139")
@pytest.mark.layer("offline")
def test_summary_reason_reflects_current_facts_after_answer(qa: QA):
    """答复后再查看：首屏原因不应仍称该问题“尚未有效答复”。"""
    qa.ok("demo")
    q1 = flow.view(qa)["reviews"]["mine"][0]
    qa.ok("review", "--answer", f"{q1['handle']}=A")
    assert fh.review_items(qa)[q1["handle"]]["status"] == "answered"
    v = qa.ok("status").result
    stale = [r for r in v["summary"]["reasons"] if "尚未有效答复" in r and q1["handle"] in r]
    assert stale == [], f"{q1['handle']} 已答复，摘要原因仍显示未答复：{stale}"


# ---------------------------------------------------------------------------
# AC-140 交付导出不隐式归档
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-140")
@pytest.mark.layer("lab")
def test_export_is_local_only_and_does_not_archive(done_lab: DoneLab, tmp_path: Path):
    qa = done_lab.qa
    ws = qa.root / "lab-demo"
    out_dir = tmp_path / "私有 交付"
    before = fh.ledger(qa, ws)
    r = qa.text("export", "--output", str(out_dir))
    assert r.rc == 0, r.describe()
    # 输出可打开的绝对路径
    paths = [Path(ln.split(": ", 1)[1]) for ln in r.out.splitlines() if ln.strip().startswith(("markdown:", "html:", "json:"))]
    assert len(paths) == 3 and all(p.is_absolute() and p.is_file() and p.stat().st_size > 0 for p in paths), r.out
    assert sorted(p.name for p in out_dir.iterdir()) == sorted(p.name for p in paths), "只导出所选需求的报告"
    assert stat.S_IMODE(out_dir.stat().st_mode) == 0o700
    html = [p for p in paths if p.suffix == ".html"][0].read_text(encoding="utf-8")
    assert html.lstrip().lower().startswith("<!doctype html") and "</html>" in html
    js = json.loads([p for p in paths if p.suffix == ".json"][0].read_text(encoding="utf-8"))
    assert js["task"]["display_title"] == fh.DEMO_TITLE
    assert set(js["task"]) == {"task_id", "display_title", "sources", "environment", "project"}
    body = json.dumps(js, ensure_ascii=False)
    assert str(qa.root) not in body and "checkpoint" not in body, "导出不应包含内部路径或检查点"
    assert "结论：" in r.out and "未验证范围：" in r.out
    # 归档仍待明确处理
    assert "远端归档需另行规划与授权" in r.out and "未写入飞书" in r.out
    # 外部调用为 0：账本、平台桩与业务库均无变化
    after = fh.ledger(qa, ws)
    assert after == before, "导出不得产生事件、操作或平台调用"
    assert qa.query("SELECT COUNT(*) n FROM actions WHERE kind LIKE 'doc.%' OR kind LIKE 'notify.%'", ws=ws)[0]["n"] == 0
    assert after["platform"].get("docs", 0) == 0 and after["platform"].get("notifications", 0) == 0
    assert qa.query("SELECT COUNT(*) n FROM snapshots WHERE kind='archive_record'", ws=ws)[0]["n"] == 0
    jr = qa.ok("export", "--output", str(tmp_path / "二次")).result
    assert "归档" in jr["note"] and all(Path(p).is_file() for p in jr["files"].values())


# ---------------------------------------------------------------------------
# AC-141 帮助入口不暴露底层学习前提
# ---------------------------------------------------------------------------

API_GROUPS_IN_DESIGN = ["init", "doctor", "source", "artifact", "clarification", "decision", "plan", "stage",
                        "environment", "script", "execution", "defect", "action", "worker", "report", "metric",
                        "knowledge", "archive", "backup", "coverage"]


@pytest.mark.ac("AC-141")
@pytest.mark.layer("offline")
def test_daily_help_shows_tasks_first_and_hides_internals(qa: QA):
    h = qa.text("--help", workspace=False, non_interactive=False)
    assert h.rc == 0, h.describe()
    out = h.out
    for task in ("开始一个需求", "继续未完成工作", "处理业务确认", "查看或交付"):
        assert task in out, f"帮助缺少任务：{task}"
    for cmd in DAILY:
        assert cmd in out
    assert "日常只需四类" in out
    # 内部概念只在 api/排障说明中出现
    for token in fh.INTERNAL_TERMS + ("stage evaluate", "TASK-", "STORY-"):
        assert token not in out, f"日常帮助不应出现 {token}"
    hidden = [g for g in API_GROUPS_IN_DESIGN if g not in ("init", "doctor", "plan", "report", "source", "action")]
    listed = [ln.split()[0] for ln in out.splitlines() if ln.startswith("    ")]
    assert set(listed) <= {"demo", *DAILY, "admin", "api"}, listed
    assert not set(listed) & set(hidden)
    assert listed.index("start") < listed.index("api") and listed.index("export") < listed.index("admin")
    assert "api --help" in out and "admin --help" in out
    # 无参数时同样展示日常帮助
    bare = qa.text(workspace=False, non_interactive=False)
    assert bare.rc == 0 and "日常只需四类" in bare.out
    # 各日常命令的帮助也不要求内部参数
    for cmd in ("demo", *DAILY):
        sub = qa.text(cmd, "--help", workspace=False, non_interactive=False)
        assert sub.rc == 0, sub.describe()
        for token in ("command", "revision", "lease", "story", "租约", "修订"):
            assert token not in sub.out.lower(), f"{cmd} --help 不应出现 {token}\n{sub.out}"


@pytest.mark.ac("AC-141")
@pytest.mark.layer("offline")
def test_api_help_lists_machine_groups_for_developers(qa: QA):
    h = qa.text("api", "--help", workspace=False, non_interactive=False)
    assert h.rc == 0, h.describe()
    listed = [ln.split()[0] for ln in h.out.splitlines() if ln.startswith("    ") and ln.split()]
    for g in API_GROUPS_IN_DESIGN:
        assert g in listed, f"api 帮助缺少接口组 {g}"
    assert "command_id" in h.out and "expected_revision" in h.out
    j = qa.run("api", workspace=False)
    assert j.rc == 0 and set(API_GROUPS_IN_DESIGN) <= set(j.json["groups"])
    assert "不作为新人的学习前提" in j.json["note"]
    sub = qa.text("api", "stage", "evaluate", "--help", workspace=False, non_interactive=False)
    assert sub.rc == 0 and "--expected-revision" in sub.out and "--command-id" in sub.out


@pytest.mark.ac("AC-141")
@pytest.mark.layer("offline")
def test_pending_task_view_for_tester_has_no_internal_identifiers(qa: QA):
    qa.ok("demo")
    for args in (("status",), ("review",), ("resume",)):
        r = qa.text(*args)
        out = r.out + r.err
        for token in ("CMD-", "TASK-", "STORY-", "command_id", "expected_revision", "租约", "修订号"):
            assert token not in out, f"{args[0]} 默认输出不应出现 {token}\n{out}"


# ---------------------------------------------------------------------------
# 方案 §10.4 额外检查
# ---------------------------------------------------------------------------

@pytest.mark.ac("EXTRA-10.4")
@pytest.mark.layer("lab")
def test_every_quickstart_command_runs_as_written(qa: QA):
    """一页指南中的每条命令照原样可执行（在当前目录、无 --workspace、无附加参数）。"""
    cmds = fh.quickstart_commands()
    subs = [shlex.split(c)[3] for c in cmds]
    assert subs == ["demo", "start", "resume", "review", "status", "export"], cmds
    shutil.copyfile(fh.LAB_REQUIREMENT, qa.cwd / "需求.md")
    results = {}
    for c in cmds:
        argv = shlex.split(c)[3:]
        r = qa.text(*argv, workspace=False, non_interactive=False)
        results[argv[0]] = r
        assert "Traceback" not in r.err, r.describe()
        assert r.rc in (0, 3), f"指南命令应可执行：{c}\n" + r.describe()
        assert "INVALID_ARGUMENT" not in r.err, r.describe()
    obs = _cwd_qa(qa)
    assert results["demo"].rc == 0 and (qa.cwd / ".qa-workspaces" / "lab-demo").is_dir()
    assert results["start"].rc == 0 and "来源：需求.md" in results["start"].out
    assert results["status"].rc == 0 and results["export"].rc == 0
    assert results["review"].rc in (0, 3) and ("REVIEW_REQUIRED" in results["review"].err or results["review"].rc == 0)
    exported = list((qa.cwd / "交付").glob("*"))
    assert len(exported) == 3, exported
    assert len(_task_rows(obs, obs.root / "lab-demo")) == 2


@pytest.mark.ac("EXTRA-10.4")
@pytest.mark.layer("lab")
def test_daily_status_and_api_stage_status_agree(failed_lab: QA):
    qa = failed_lab
    daily = {s["stage"]: (s["status"], s["verdict"], s.get("evaluation_ref")) for s in qa.ok("status").result["stages"]}
    api = {s["stage"]: (s["status"], s["verdict"], s.get("evaluation_ref")) for s in
           qa.api_ok("stage", "status", "--story", fh.DEMO_TITLE).json["stages"]}
    assert daily == api
    ex = qa.api_ok("execution", "status", "--story", fh.DEMO_TITLE).json
    fails = sorted(k for k, a in ex["latest"].items() if a["outcome"] == "fail")
    assert fails == ["C1", "C3"]
    assert sorted(d["tc"] for d in qa.ok("status").result["defects"]) == fails


@pytest.mark.ac("EXTRA-10.4")
@pytest.mark.layer("lab")
def test_daily_review_and_api_answer_give_same_business_result(qa: QA, tmp_path: Path):
    other = _new_qa(tmp_path / "api-side")
    qa.ok("demo")
    other.ok("demo")
    qa.ok("review", "--answer", "Q1=A")
    other.api_ok("clarification", "answer", "--story", fh.DEMO_TITLE, "--handle", "Q1", "--option", "A",
                 "--role", "tester")

    def norm(q: QA) -> list[tuple]:
        return [(d["role"], d["verification"], d["result"], json.loads(d["scope"])["environment"]) for d in
                fh.decisions(q)]

    assert norm(qa) == norm(other) == [("tester", "local_attested", "answer", "lab")]
    qa.ok("resume")
    other.ok("resume")
    a = {s["stage"]: (s["status"], s["verdict"]) for s in flow.view(qa)["stages"]}
    b = {s["stage"]: (s["status"], s["verdict"]) for s in flow.view(other)["stages"]}
    assert a == b
    # 两个入口对同一越权与泛化输入给出相同阻断
    for q_args, api_args in (
        (("review", "--answer", "Q2=A"),
         ("clarification", "answer", "--story", fh.DEMO_TITLE, "--handle", "Q2", "--option", "A", "--role", "tester")),
        (("review", "--answer", "Q4=全部同意"),
         ("clarification", "answer", "--story", fh.DEMO_TITLE, "--handle", "Q4", "--text", "全部同意", "--role", "tester")),
    ):
        d, p = qa.run(*q_args), other.api(*api_args)
        assert d.rc == p.rc != 0 and d.error_code == p.error_code, (d.describe(), p.describe())
    assert norm(qa) == norm(other)


@pytest.mark.ac("EXTRA-10.4")
@pytest.mark.layer("lab")
def test_api_environment_check_matches_daily_resume(qa: QA, tmp_path: Path):
    """同一状态下，api environment check 与日常 resume 对环境就绪应给出相同业务结论。"""
    daily = _new_qa(tmp_path / "daily-side")
    for q in (qa, daily):
        fh.to_confirmed(q)
        fh.crash_resume(q, "after_commit:script.register_lab")
    # 日常入口：只走到环境探测为止
    fh.crash_resume(daily, f"after_commit:action.plan:probe:{fh.deployment_id(daily)}")
    probe = [a for a in daily.api_ok("action", "list", "--story", fh.DEMO_TITLE).result["actions"]
             if a["kind"] == "environment.probe"][0]
    daily.api_ok("action", "dispatch", "--id", probe["id"])
    d = daily.api_ok("stage", "evaluate", "--story", fh.DEMO_TITLE, "--stage", "env_ready").result
    # 机器入口
    c = qa.api("environment", "check", "--story", fh.DEMO_TITLE)
    assert c.rc == 0, c.describe()
    a = qa.api_ok("stage", "evaluate", "--story", fh.DEMO_TITLE, "--stage", "env_ready").result
    assert d["status"] == "completed" and d["verdict"] == "pass", d
    assert (a["status"], a["verdict"]) == (d["status"], d["verdict"]), (
        f"api 入口结论 {a['status']}/{a['verdict']}（{a['reasons']}）与日常入口 {d['status']}/{d['verdict']} 不一致")


@pytest.mark.ac("EXTRA-10.4")
@pytest.mark.layer("lab")
def test_resume_recovers_after_api_environment_check(qa: QA):
    """经 api 做过环境核验后，日常 resume 仍应能继续，不出现永久受阻。"""
    fh.to_confirmed(qa)
    fh.crash_resume(qa, "after_commit:script.register_lab")
    assert qa.api("environment", "check", "--story", fh.DEMO_TITLE).rc == 0
    for _ in range(2):
        r = qa.run("resume")
        assert r.rc in (0, 3), r.describe()
    v = flow.view(qa)
    assert fh.stage_of(v, "env_ready")["status"] == "completed", fh.stage_of(v, "env_ready")
