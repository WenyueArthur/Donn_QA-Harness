"""飞书来源包：Agent 读取飞书项目工作项与挂载文档后交给 qa-flow 登记。

qa-flow 不直接连接飞书（Cursor、Codex、Claude Code 已配置飞书项目 MCP，避免重复实现同一能力）。
这里只用本地合成的工具原始输出，不访问网络；通过子进程调用公共 CLI，并直接读取工作区数据库核对结果。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from support import evidence_helpers as H

layer = pytest.mark.layer

WI_URL = "https://project.feishu.cn/demo-space/story/detail/9001"
PRD_URL = "https://example.feishu.cn/docx/PRDTOKEN123"
TECH_URL = "https://example.feishu.cn/wiki/TECHTOKEN9"
SHEET_URL = "https://example.feishu.cn/sheets/SHEETTOKEN1"
FETCHED = "2026-11-16T05:00:00Z"
CLOCK = "2026-11-16T06:00:00Z"
PRD_RULE = "R1：单日兑换不超过 3 次，第 4 次返回“今日兑换次数已用完”。"


def brief(*, wid: str = "9001", has_more: bool = False, prd: str = PRD_URL + "?from=copylink") -> dict[str, Any]:
    return {
        "work_item_attribute": {
            "owned_project": {"key": "PK1", "simple_name": "demo-space", "name": "演示空间"},
            "work_item_id": wid, "work_item_name": "积分兑换限额",
            "work_item_type": {"key": "story", "name": "产品需求"},
            "role_members": [{"key": "PM", "name": "产品负责人",
                              "members": [{"key": "1", "name": "张三", "email": "pm@example.invalid"}]}],
            "work_item_status": {"name": "待开发"},
        },
        "work_item_fields": [
            {"key": "description", "name": "产品需求描述", "value": "1. 单日兑换不超过 3 次。\n2. 积分不足时提示余额不足。"},
            {"key": "priority", "name": "优先级", "value": {"value": "1", "label": "P1"}},
            {"key": "wiki", "name": "产品文档", "value": prd},
            {"key": "field_x", "name": "技术方案", "value": TECH_URL},
            {"key": "tags", "name": "标签", "value": None},
        ],
        "work_item_current_node": [{"name": "测试排期", "owners": [{"name": "李四"}]}],
        "pagination": {"has_more": has_more},
    }


def comments(*, link: bool = True, total_pages: int = 1) -> dict[str, Any]:
    items = [{"creator": {"name": "王五"}, "created_at": 1793687515163,
              "content": "测试点文档：" + SHEET_URL if link else "已评审"}]
    return {"pagination": {"page_num": 1, "total_pages": total_pages, "total": 1}, "comments": items}


def write_bundle(base: Path, *, wi: dict[str, Any] | None = None, cm: dict[str, Any] | None = None,
                 prd_text: str | None = None, documents: list[dict[str, Any]] | None = None,
                 url: str = WI_URL, extra: dict[str, Any] | None = None) -> Path:
    """按 Skill 约定写一个来源包：工具原始输出原样保存（MCP 文本输出末尾带 log_id 行）。"""
    base.mkdir(parents=True, exist_ok=True)
    (base / "workitem.json").write_text(json.dumps(wi or brief(), ensure_ascii=False) + "\nlog_id: 20261116000001\n",
                                        encoding="utf-8")
    (base / "comments.json").write_text(json.dumps(cm or comments(), ensure_ascii=False) + "\nlog_id: 2\n",
                                        encoding="utf-8")
    (base / "prd.md").write_text(prd_text or f"# 积分兑换限额 PRD\n\n## 规则\n\n{PRD_RULE}\n", encoding="utf-8")
    lark = {"ok": True, "identity": "user", "data": {"document": {
        "title": "积分兑换技术方案", "content": "# 积分兑换技术方案\n\n使用 Redis 计数，key=exchange:{uid}:{date}。\n"}}}
    (base / "tech.json").write_text(json.dumps(lark, ensure_ascii=False), encoding="utf-8")
    if documents is None:
        documents = [
            {"url": PRD_URL, "file": "prd.md", "fetched_at": FETCHED, "tool": "lark-cli docs +fetch"},
            {"url": TECH_URL, "file": "tech.json", "fetched_at": FETCHED, "tool": "lark-cli docs +fetch"},
            {"url": SHEET_URL, "read_status": "unreadable", "failure_reason": "电子表格不支持按正文读取",
             "fetched_at": FETCHED},
        ]
    manifest = {"schema": "qa.source_bundle", "agent": "claude-code",
                "workitem": {"url": url, "brief_file": "workitem.json", "comments_file": "comments.json",
                             "fetched_at": FETCHED, "tool": "FeishuProjectMcp.get_workitem_brief"},
                "documents": documents, **(extra or {})}
    p = base / "bundle.json"
    p.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


@pytest.fixture()
def prj(qa):
    """真实项目工作区（非 lab），固定时钟，便于检验新鲜度。"""
    q = qa.test_mode(QA_FLOW_TEST_CLOCK=CLOCK)
    r = H.setup_project(q, H.project_profile(q))
    assert r.rc == 0 and r.json["applied"] is True, r.describe()
    return q


def sources(qa) -> list[dict[str, Any]]:
    rows = qa.query("SELECT id, story_id, lineage, canonical FROM snapshots WHERE kind='source_revision' ORDER BY rowid")
    out = []
    for r in rows:
        env = json.loads(r["canonical"])
        out.append({"id": r["id"], "story": r["story_id"], "lineage": r["lineage"], "payload": env["payload"],
                    "producer": env.get("producer") or {}})
    return out


def tasks(qa) -> list[dict[str, Any]]:
    return qa.api_ok("task", "list").json["tasks"]


# ---------------------------------------------------------------------------
# 预检与完整性
# ---------------------------------------------------------------------------

@layer("offline")
def test_预检列出挂载文档并指出漏读(qa, tmp_path):
    b = write_bundle(tmp_path / "b", documents=[
        {"url": PRD_URL, "file": "prd.md", "fetched_at": FETCHED, "tool": "lark-cli docs +fetch"}])
    r = qa.api("source", "inspect", "--bundle", str(b), workspace=False)
    assert r.rc == 0, r.describe()
    d = r.json
    assert d["complete"] is False and d["title"] == "积分兑换限额"
    found = {x["url"]: (x["role"], x["found_in"]) for x in d["discovered_links"]}
    assert found == {PRD_URL: ("requirement", "字段「产品文档」"), TECH_URL: ("tech_design", "字段「技术方案」"),
                     SHEET_URL: ("other", "评论 1")}, "链接应去掉查询参数，并按所在字段推断用途"
    msgs = " ".join(p["message"] for p in d["problems"])
    assert TECH_URL in msgs and SHEET_URL in msgs and PRD_URL not in msgs
    assert qa.workspaces() == [], "预检不得写入工作区"


@layer("offline")
def test_漏读挂载文档时拒绝登记且不写入(prj, tmp_path):
    b = write_bundle(tmp_path / "b", documents=[
        {"url": PRD_URL, "file": "prd.md", "fetched_at": FETCHED, "tool": "lark-cli docs +fetch"}])
    r = prj.run("start", "--bundle", str(b))
    assert r.rc == 4 and r.error_code == "VALIDATION_FAILED", r.describe()
    assert TECH_URL in json.dumps(r.json, ensure_ascii=False)
    assert tasks(prj) == [] and sources(prj) == []


@pytest.mark.parametrize("case", ["字段未翻完", "评论未翻完", "工作项 ID 不符", "失败未写原因", "读取时间在未来",
                                  "文档读取失败输出", "缺少评论文件"])
@layer("offline")
def test_来源包不完整或不可信时拒绝(prj, tmp_path, case):
    kw: dict[str, Any] = {}
    if case == "字段未翻完":
        kw["wi"] = brief(has_more=True)
    elif case == "评论未翻完":
        kw["cm"] = comments(total_pages=2)
    elif case == "工作项 ID 不符":
        kw["url"] = "https://project.feishu.cn/demo-space/story/detail/9002"
    elif case == "失败未写原因":
        kw["documents"] = [
            {"url": PRD_URL, "file": "prd.md", "fetched_at": FETCHED},
            {"url": TECH_URL, "read_status": "unreadable", "fetched_at": FETCHED},
            {"url": SHEET_URL, "read_status": "unreadable", "failure_reason": "表格", "fetched_at": FETCHED}]
    elif case == "读取时间在未来":
        kw["documents"] = [
            {"url": PRD_URL, "file": "prd.md", "fetched_at": "2026-12-01T00:00:00Z"},
            {"url": TECH_URL, "file": "tech.json", "fetched_at": FETCHED},
            {"url": SHEET_URL, "read_status": "unreadable", "failure_reason": "表格", "fetched_at": FETCHED}]
    b = write_bundle(tmp_path / "b", **kw)
    if case == "文档读取失败输出":
        (b.parent / "tech.json").write_text(json.dumps({"ok": False, "error": {"message": "permission denied"}}),
                                            encoding="utf-8")
    if case == "缺少评论文件":
        m = json.loads(b.read_text(encoding="utf-8"))
        del m["workitem"]["comments_file"]
        b.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    r = prj.run("start", "--bundle", str(b))
    assert r.rc == 4 and r.error_code == "VALIDATION_FAILED", r.describe()
    assert tasks(prj) == [] and sources(prj) == []


@layer("offline")
def test_飞书链接不能直接作为来源(prj):
    r = prj.run("start", "--source", WI_URL)
    assert r.rc == 3 and r.error_code == "PRECONDITION_FAILED", r.describe()
    msg = r.errors[0]["message"]
    assert "--bundle" in msg and "Agent" in msg and "不直接连接飞书" in msg
    assert tasks(prj) == []


# ---------------------------------------------------------------------------
# 登记、引文与再次登记
# ---------------------------------------------------------------------------

@layer("offline")
def test_登记工作项与挂载文档并可逐字引用(prj, tmp_path):
    b = write_bundle(tmp_path / "b")
    r = prj.run("start", "--bundle", str(b))
    assert r.rc == 0, r.describe()
    res = r.result
    assert res["task"]["display_title"] == "积分兑换限额" and res["task"]["environment"] == "test"
    assert res["lab"] is False
    src = sources(prj)
    by_role = {s["payload"]["role"]: s for s in src}
    assert set(by_role) == {"workitem", "requirement", "tech_design", "other"}
    wi, prd, tech, sheet = by_role["workitem"], by_role["requirement"], by_role["tech_design"], by_role["other"]
    assert wi["payload"]["uri"] == WI_URL and prd["payload"]["uri"] == PRD_URL, "链接应规范化"
    for s in (wi, prd, tech):
        assert s["payload"]["read_status"] == "available" and s["payload"]["fetched_at"] == FETCHED
        assert s["producer"].get("verification") == "agent_reported" and s["producer"].get("kind") == "agent"
        assert s["payload"]["fetched_by"]["agent"] == "claude-code"
    assert sheet["payload"]["read_status"] == "unreadable" and "content_ref" not in sheet["payload"]
    assert sheet["payload"]["failure_reason"] == "电子表格不支持按正文读取"
    assert tech["payload"]["title"] == "积分兑换技术方案", "lark-cli 原始 JSON 应取正文与标题"

    story = wi["story"]
    payload = {"rule_ref": "legacy_bridge:req_gate", "source_refs": [wi["id"], prd["id"]],
               "findings": [{"id": "F1", "text": "描述与 PRD 一致", "severity": "info",
                             "citations": [{"source_ref": wi["id"], "quote": "积分不足时提示余额不足。"},
                                           {"source_ref": prd["id"], "quote": PRD_RULE}]}],
               "testability": {"verdict": "pass"}}
    f = tmp_path / "qr.json"
    f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    v = prj.api_ok("artifact", "validate", "--story", story, "--kind", "quality_review", "--file", str(f))
    assert v.json["valid"] is True, v.describe()
    payload["findings"][0]["citations"][1]["quote"] = "R1：单日兑换不超过 5 次。"
    f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    v = prj.api_ok("artifact", "validate", "--story", story, "--kind", "quality_review", "--file", str(f))
    assert v.json["valid"] is False and "逐字" in json.dumps(v.json["problems"], ensure_ascii=False)

    text = prj.text("status")
    assert "需求文档《积分兑换限额 PRD》" in text.out and "技术方案《积分兑换技术方案》" in text.out
    exp = prj.ok("export", "--output", str(tmp_path / "交付")).result
    unverified = " ".join(exp["unverified"])
    assert "agent_reported" in unverified and "sheets/SHEETTOKEN1" in unverified


@layer("offline")
def test_工作项渲染不含邮箱且保留评论(prj, tmp_path):
    b = write_bundle(tmp_path / "b")
    prj.ok("start", "--bundle", str(b))
    wi = next(s for s in sources(prj) if s["payload"]["role"] == "workitem")
    ev = prj.query("SELECT rel_path FROM evidence WHERE id=?", (wi["payload"]["content_ref"],))[0]
    text = (prj.workspace() / ev["rel_path"]).read_text(encoding="utf-8")
    assert text.startswith("# 积分兑换限额\n")
    assert "产品负责人：张三" in text and "pm@example.invalid" not in text
    assert "### 产品需求描述" in text and "### 标签" not in text, "空字段不渲染"
    assert "王五" in text and SHEET_URL in text and "log_id" not in text


@layer("offline")
def test_同一工作项再次登记复用任务且只在内容变化时生成新快照(prj, tmp_path):
    b = write_bundle(tmp_path / "b")
    prj.ok("start", "--bundle", str(b))
    before = sources(prj)
    again = prj.ok("start", "--bundle", str(b)).result
    assert len(tasks(prj)) == 1 and again["task"]["task_id"] == tasks(prj)[0]["task_id"]
    assert len(sources(prj)) == len(before), "内容未变时不应生成新快照"

    b2 = write_bundle(tmp_path / "b2", prd_text="# 积分兑换限额 PRD\n\nR1：单日兑换不超过 5 次。\n")
    r = prj.ok("start", "--bundle", str(b2))
    after = sources(prj)
    assert len(after) == len(before) + 1
    new = after[-1]
    assert new["payload"]["role"] == "requirement" and new["lineage"] == next(
        s["lineage"] for s in before if s["payload"]["role"] == "requirement")
    assert any("来源已变化" in n and "PRD" in n for n in r.result["notes"]), r.result["notes"]
    task = prj.ok("status").result["task"]
    assert len(tasks(prj)) == 1 and task["display_title"] == "积分兑换限额"


@layer("offline")
def test_新增与移除挂载文档时更新来源列表(prj, tmp_path):
    prj.ok("start", "--bundle", str(write_bundle(tmp_path / "b")))
    wi = brief()
    wi["work_item_fields"] = [f for f in wi["work_item_fields"] if f["key"] != "field_x"]
    b2 = write_bundle(tmp_path / "b2", wi=wi, documents=[
        {"url": PRD_URL, "file": "prd.md", "fetched_at": FETCHED},
        {"url": SHEET_URL, "read_status": "unreadable", "failure_reason": "电子表格不支持按正文读取",
         "fetched_at": FETCHED}])
    r = prj.ok("start", "--bundle", str(b2))
    assert any("不再挂载" in n and TECH_URL in n for n in r.result["notes"]), r.result["notes"]
    shown = " ".join(prj.ok("status").result["task"]["sources"])
    assert "技术方案" not in shown and "需求文档" in shown


# ---------------------------------------------------------------------------
# 刷新与新鲜度
# ---------------------------------------------------------------------------

@layer("offline")
def test_继续时不把_agent_登记的来源降级为读取受阻(prj, tmp_path):
    prj.ok("start", "--bundle", str(write_bundle(tmp_path / "b")))
    n = len(sources(prj))
    for _ in range(2):
        prj.ok("resume")
    prj.api_ok("source", "refresh", "--story", sources(prj)[0]["story"])
    after = sources(prj)
    assert len(after) == n, "刷新不得为 Agent 登记的来源生成新快照"
    assert sorted(s["payload"]["read_status"] for s in after) == ["available", "available", "available", "unreadable"]


@layer("offline")
def test_超过新鲜度期限时提示_agent_重新读取(prj, tmp_path):
    prj.ok("start", "--bundle", str(write_bundle(tmp_path / "b")))
    n = len(sources(prj))
    late = prj.with_env(QA_FLOW_TEST_CLOCK="2026-11-20T00:00:00Z")
    res = late.ok("resume").result
    assert any("start --bundle" in a["command"] for a in res["summary"]["next_actions"]), res["summary"]
    assert any("新鲜度期限" in u for u in res["unverified"])
    tick = late.api_ok("worker", "tick", "--once").json
    assert {x["uri"] for x in tick["agent_fetch_required"]} == {WI_URL, PRD_URL, TECH_URL}
    assert tick["refreshed"] == [] and len(sources(prj)) == n

    fresh = write_bundle(tmp_path / "b2", extra={})
    m = json.loads(fresh.read_text(encoding="utf-8"))
    m["workitem"]["fetched_at"] = "2026-11-19T23:00:00Z"
    for d in m["documents"]:
        d["fetched_at"] = "2026-11-19T23:00:00Z"
    fresh.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    res = late.ok("start", "--bundle", str(fresh)).result
    assert not any("start --bundle" in a["command"] for a in res["summary"]["next_actions"]), res["summary"]
    assert len(sources(prj)) == n, "内容未变时只更新读取时间"
