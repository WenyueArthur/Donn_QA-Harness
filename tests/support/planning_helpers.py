"""批次 M3（计划、分析、用例、变更）验收的辅助函数。

只通过公共 CLI 与只读 SQL 观测工作区，不导入 qa_flow。
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from . import flow
from .cli import QA, Result, write_json

FULL_ROW = "id, kind, story_id, lineage, parent_id, canonical, sha256, created_at"


def make_qa(base: Path) -> QA:
    """在给定目录下建立独立的工作区根、HOME 与 cwd（用于 module 级共享夹具）。"""
    root, home, cwd = base / "工作区 根", base / "home", base / "cwd"
    for d in (root, home, cwd):
        d.mkdir(parents=True, exist_ok=True)
    return QA(root=root, home=home, cwd=cwd)


def story_id(qa: QA) -> str:
    rows = qa.query("SELECT DISTINCT story_id FROM snapshots WHERE kind='task_request'")
    assert len(rows) == 1, f"期望恰好一个需求，实际 {rows}"
    return rows[0]["story_id"]


def envelope(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(row["canonical"])


def latest(qa: QA, kind: str) -> dict[str, Any] | None:
    """某类产物最后登记的快照信封（独立从数据库读取）。"""
    rows = qa.query("SELECT canonical FROM snapshots WHERE kind=? ORDER BY rowid DESC LIMIT 1", (kind,))
    return envelope(rows[0]) if rows else None


def all_of(qa: QA, kind: str) -> list[dict[str, Any]]:
    return [envelope(r) for r in qa.query("SELECT canonical FROM snapshots WHERE kind=? ORDER BY rowid", (kind,))]


def count(qa: QA, kind: str) -> int:
    return qa.query("SELECT COUNT(*) c FROM snapshots WHERE kind=?", (kind,))[0]["c"]


def row(qa: QA, snap_id: str) -> dict[str, Any]:
    """快照整行，用于核对字节未变。"""
    rows = qa.query(f"SELECT {FULL_ROW} FROM snapshots WHERE id=?", (snap_id,))
    assert len(rows) == 1, f"快照 {snap_id} 不存在"
    return rows[0]


def max_seq(qa: QA) -> int:
    return qa.query("SELECT COALESCE(MAX(seq), 0) s FROM events")[0]["s"]


def events_after(qa: QA, seq: int) -> list[dict[str, Any]]:
    return qa.query("SELECT seq, object_id, event_type FROM events WHERE seq>? ORDER BY seq", (seq,))


def side_effects_after(qa: QA, seq: int) -> list[dict[str, Any]]:
    """拒绝之后除审计记录（command_rejected）以外的事件；应为空。"""
    return [e for e in events_after(qa, seq) if e["event_type"] != "command_rejected"]


def rejections_after(qa: QA, seq: int) -> int:
    return len([e for e in events_after(qa, seq) if e["event_type"] == "command_rejected"])


def submit(qa: QA, story: str, kind: str, payload: dict[str, Any], tmp: Path, *extra: str) -> Result:
    f = write_json(tmp / f"payload-{time.monotonic_ns()}.json", payload)
    return qa.api("artifact", "submit", "--story", story, "--kind", kind, "--file", str(f), *extra)


def validate(qa: QA, story: str, kind: str, payload: dict[str, Any], tmp: Path) -> Result:
    f = write_json(tmp / f"payload-{time.monotonic_ns()}.json", payload)
    return qa.api("artifact", "validate", "--story", story, "--kind", kind, "--file", str(f))


def plan_submit(qa: QA, story: str, payload: dict[str, Any], tmp: Path, parent: str | None = None) -> Result:
    f = write_json(tmp / f"plan-{time.monotonic_ns()}.json", payload)
    args = ["plan", "submit", "--story", story, "--file", str(f)]
    if parent:
        args += ["--parent", parent]
    return qa.api(*args)


def stage_eval(qa: QA, story: str, stage: str) -> dict[str, Any]:
    r = qa.api_ok("stage", "evaluate", "--story", story, "--stage", stage)
    return r.result


def stage_statuses(qa: QA, story: str) -> dict[str, str]:
    return qa.api_ok("stage", "status", "--story", story).json["statuses"]


def stage_state(qa: QA, story: str, stage: str) -> dict[str, Any]:
    rows = qa.query("SELECT state FROM aggregates WHERE object_id=?", (f"stage:{story}:{stage}",))
    assert rows, f"阶段 {stage} 尚无记录"
    return json.loads(rows[0]["state"])


def assert_rejected(r: Result, rc: int, code: str, path: str | None = None, ref: str | None = None) -> None:
    """拒绝路径的通用检查：退出码、稳定错误码、未提交、字段位置与相关对象。"""
    assert r.rc == rc, f"期望退出码 {rc}\n{r.describe()}"
    assert r.error_code == code, f"期望错误码 {code}\n{r.describe()}"
    assert isinstance(r.json, dict) and r.json.get("committed") is False and r.json.get("result") is None, r.describe()
    issues = (r.errors[0].get("details") or {}).get("issues") or []
    if path is not None:
        paths = [r.errors[0].get("path")] + [i.get("path") for i in issues]
        assert path in paths, f"期望错误位置 {path}，实际 {paths}"
    if ref is not None:
        refs = set(r.errors[0].get("related_refs") or [])
        for i in issues:
            refs |= set(i.get("related_refs") or [])
        assert ref in refs, f"期望相关对象 {ref}，实际 {sorted(refs)}"


def reviews(qa: QA) -> dict[str, list[dict[str, Any]]]:
    return flow.view(qa)["reviews"]


def drive_without_transfer(qa: QA, done: Callable[[dict[str, str]], bool], max_rounds: int = 8) -> None:
    """只答复测试人员自己负责的事项，不转交产品事项（关键预期保持待确认）。"""
    if not qa.workspaces():
        qa.ok("demo")
    story = story_id(qa)
    for _ in range(max_rounds):
        flow.answer_mine(qa)
        r = qa.run("resume")
        assert r.rc in (0, 3, 5), r.describe()
        if done(stage_statuses(qa, story)) and not flow.view(qa)["reviews"]["mine"]:
            return
    raise AssertionError(f"未能推进到预期进度：{stage_statuses(qa, story)}")


def material(qa: QA, name: str) -> Path:
    return qa.workspace() / "lab" / "materials" / name


def edit_requirement(qa: QA, marker: str = "- R7：单笔退款金额不得超过 5000 分。") -> Path:
    """模拟原文作者修改需求文件（S2）。产品把实验原文设为只读，这里以作者身份改写。"""
    p = material(qa, "refund-requirement.md")
    text = p.read_text(encoding="utf-8")
    assert "## 3. 待确认" in text
    os.chmod(p, 0o644)
    p.write_text(text.replace("## 3. 待确认", f"{marker}\n\n## 3. 待确认"), encoding="utf-8")
    return p


def platform_calls(qa: QA) -> list[tuple[str, str, str]]:
    """实验平台桩的调用账本（独立读取）。"""
    path = qa.workspace() / "lab" / "platform.db"
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [tuple(r) for r in conn.execute("SELECT op, target, outcome FROM calls ORDER BY seq")]
    finally:
        conn.close()


def platform_docs(qa: QA) -> int:
    path = qa.workspace() / "lab" / "platform.db"
    if not path.exists():
        return 0
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    finally:
        conn.close()


def decision_valid(qa: QA, decision_id: str) -> bool:
    return bool(qa.api_ok("decision", "verify", "--id", decision_id).json.get("valid"))


def decisions_on(qa: QA, subject_ref: str) -> list[dict[str, Any]]:
    out = []
    for d in qa.query("SELECT * FROM decisions ORDER BY created_at, id"):
        if any(s["ref"] == subject_ref for s in json.loads(d["subjects"])):
            out.append(d)
    return out


# ---- 简短的独立参考模型 ----

def reference_capacity_conflicts(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """同人同日的最可能估算合计与可用容量比较（独立计算，不调用产品函数）。"""
    demand: dict[tuple[str, str], float] = {}
    for t in plan["tasks"]:
        key = (t["owner"], t["date"])
        demand[key] = demand.get(key, 0.0) + t["estimate_hours"]["likely"]
    cap: dict[tuple[str, str], float] = {}
    for c in plan["capacity"]:
        key = (c["person"], c["date"])
        cap[key] = cap.get(key, 0.0) + c["hours"]
    return [{"person": p, "date": d, "over_hours": need - cap.get((p, d), 0.0)}
            for (p, d), need in sorted(demand.items()) if need > cap.get((p, d), 0.0)]


def reference_coverage(tp_ready: list[str], cases: dict[str, list[str]], scripted: set[str],
                       executed: set[str]) -> dict[str, tuple[int, int]]:
    """三种覆盖的独立参考值：设计按测试点，脚本映射与实际执行按用例。"""
    covered = {t for tps in cases.values() for t in tps} & set(tp_ready)
    return {"design": (len(covered), len(tp_ready)),
            "script_mapping": (len(set(cases) & scripted), len(cases)),
            "execution": (len(set(cases) & executed), len(cases))}


def base_plan(owner: str = "alice", date: str = "2026-11-16") -> dict[str, Any]:
    """AC-005 前置：每人每天可用 6 小时；一项任务的三点估算为 4/6/10 小时。"""
    return {
        "tasks": [{"id": "T1", "title": "用例设计与执行", "owner": owner, "date": date,
                   "estimate_hours": {"low": 4, "likely": 6, "high": 10}, "depends_on": []}],
        "estimate_method": "按测试点复杂度做三点估算（最乐观/最可能/最悲观）",
        "estimate_range_hours": {"low": 4, "likely": 6, "high": 10},
        "calendar": {"timezone": "Asia/Shanghai", "workdays": [date], "holidays": []},
        "capacity": [{"person": owner, "date": date, "hours": 6}],
        "dependencies": [{"id": "DEP1", "description": "实验环境可用", "status": "met"}],
        "milestones": [{"name": "准出", "date": date}],
    }


def clone(obj: Any) -> Any:
    return copy.deepcopy(obj)
