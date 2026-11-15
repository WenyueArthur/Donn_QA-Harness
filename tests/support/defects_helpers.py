"""M5/M6 验收的辅助函数：推进 demo 到指定检查点、读取平台桩账本、模拟平台侧的外部操作者。

约束：
- 产品状态只通过公共 CLI 推进；中途停点使用产品的崩溃钩子（事务回滚，不留半成品）。
- 平台桩库（lab/platform.db）代表“外部系统”。研发在平台上改状态、事件重复或乱序到达，
  由本文件直接写平台桩库来模拟；工作区主库 qa.db 只读查询，从不手改。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import flow
from .cli import QA, REPO, Result

CRASH_RC = 99
LAB_SCRIPT = REPO / "src" / "qa_flow" / "resources" / "lab" / "scripts" / "refund_cases.py"
# 停点：build1 全量执行完成、失败用例尚未形成缺陷提案
STOP_BEFORE_PROPOSE = "before_commit:defect.propose_batch"
# 停点：两条缺陷已在平台桩创建并挂好附件，尚未部署修复构建
STOP_BEFORE_FIX_DEPLOY = "before_commit:action.plan:deploy:build2"
# 停点：设计与质检完成、build1 已部署，尚未登记脚本（未执行任何用例）
STOP_BEFORE_SCRIPT = "before_commit:script.register_lab"

LAB_COMMITS = {"build1": "lab-commit-0001", "build2": "lab-fix-0002", "build3": "lab-commit-0003"}
TERMINAL = {"closed", "rejected", "deferred", "accepted_risk"}


def drive_until_crash(qa: QA, crash_at: str, max_rounds: int = 12) -> QA:
    """按新人操作推进 demo，直到产品在指定提交点被崩溃钩子终止。返回不带崩溃点的测试模式调用器。"""
    q = qa.test_mode(QA_FLOW_CRASH_AT=crash_at)
    q.ok("demo")
    for _ in range(max_rounds):
        flow.answer_mine(q)
        flow.transfer_others(q)
        r = q.run("resume")
        if r.rc == CRASH_RC:
            return qa.test_mode()
        assert r.rc in (0, 3, 5), "resume 出现非预期退出码\n" + r.describe()
    raise AssertionError(f"未到达停点 {crash_at}")


def story_id(qa: QA) -> str:
    tasks = qa.api_ok("task", "list").json["tasks"]
    assert len(tasks) == 1, tasks
    return tasks[0]["story_id"]


# ---- 工作区主库的只读观测 ----

def defects(qa: QA) -> list[dict[str, Any]]:
    rows = qa.query("SELECT state, revision FROM aggregates WHERE kind='defect' ORDER BY object_id")
    out = []
    for r in rows:
        d = json.loads(r["state"])
        d["_revision"] = r["revision"]
        out.append(d)
    return out


def defect_by_tc(qa: QA, tc: str) -> dict[str, Any]:
    ds = [d for d in defects(qa) if d["tc_ref"] == tc]
    assert len(ds) == 1, f"期望用例 {tc} 恰有一条缺陷，实际 {len(ds)}"
    return ds[0]


def actions(qa: QA, kind: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM actions"
    rows = qa.query(sql + (" WHERE kind=?" if kind else "") + " ORDER BY rowid", (kind,) if kind else ())
    return rows


def last_seq(qa: QA) -> int:
    return qa.query("SELECT COALESCE(MAX(seq),0) AS s FROM events")[0]["s"]


def events_since(qa: QA, seq: int) -> list[dict[str, Any]]:
    return qa.query("SELECT seq, object_id, event_type, payload FROM events WHERE seq>? ORDER BY seq", (seq,))


def snapshots(qa: QA, kind: str) -> list[dict[str, Any]]:
    rows = qa.query("SELECT id, canonical, origin, created_at FROM snapshots WHERE kind=? ORDER BY rowid",
                    (kind,))
    out = []
    for r in rows:
        env = json.loads(r["canonical"])
        out.append({"id": r["id"], "payload": env["payload"], "envelope": env, "origin": r["origin"],
                    "created_at": r["created_at"]})
    return out


def attempts(qa: QA) -> list[dict[str, Any]]:
    """直接从快照表读尝试记录（不经过产品的聚合视图）。"""
    out = []
    for s in snapshots(qa, "case_attempt"):
        p = dict(s["payload"])
        p["id"] = s["id"]
        p["origin"] = s["origin"]
        out.append(p)
    return out


def latest_attempt(qa: QA, tc: str, build: str, purpose: str | None = None) -> dict[str, Any] | None:
    rows = [a for a in attempts(qa) if a["tc_ref"] == tc and a["build"] == build
            and (purpose is None or a["purpose"] == purpose)]
    return rows[-1] if rows else None


def evidence_row(qa: QA, ev_id: str) -> dict[str, Any] | None:
    rows = qa.query("SELECT * FROM evidence WHERE id=?", (ev_id,))
    return rows[0] if rows else None


def show(qa: QA, snap_id: str) -> dict[str, Any]:
    return qa.api_ok("artifact", "show", "--id", snap_id).json


def criteria(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in report["criterion_results"]}


def build_report(qa: QA, story: str) -> dict[str, Any]:
    return qa.api_ok("report", "build", "--story", story).json


# ---- 平台桩（外部系统）----

def platform_path(qa: QA) -> Path:
    return qa.workspace() / "lab" / "platform.db"


def pf_query(qa: QA, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    path = platform_path(qa)
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def pf_calls(qa: QA, op: str | None = None, target: str | None = None) -> list[dict[str, Any]]:
    """平台桩调用账本。op/target 为空表示不过滤。"""
    rows = pf_query(qa, "SELECT seq, op, target, outcome FROM calls ORDER BY seq")
    return [r for r in rows if (op is None or r["op"] == op) and (target is None or r["target"] == target)]


def pf_items(qa: QA) -> dict[str, dict[str, Any]]:
    out = {}
    for r in pf_query(qa, "SELECT * FROM items ORDER BY id"):
        r["fields"] = json.loads(r["fields"])
        r["attachments"] = json.loads(r["attachments"])
        out[r["id"]] = r
    return out


def pf_events(qa: QA, item_id: str | None = None) -> list[dict[str, Any]]:
    rows = pf_query(qa, "SELECT * FROM events ORDER BY seq")
    return [r for r in rows if item_id is None or r["item_id"] == item_id]


def pf_writes(qa: QA) -> list[dict[str, Any]]:
    return [c for c in pf_calls(qa) if c["op"] not in ("query", "get", "events", "doc_get")]


def _pf_rw(qa: QA) -> sqlite3.Connection:
    conn = sqlite3.connect(str(platform_path(qa)), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def external_set_status(qa: QA, item_id: str, status: str, *, fields: dict[str, Any] | None = None,
                        event_id: str | None = None) -> dict[str, Any]:
    """模拟平台侧的其他操作者（如研发）改状态：修订号加一并产生一条平台事件。"""
    conn = _pf_rw(qa)
    try:
        row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        assert row is not None, f"平台上没有工作项 {item_id}"
        f = json.loads(row["fields"])
        f.update(fields or {})
        rev = row["revision"] + 1
        eid = event_id or f"PEV-EXT-{item_id}-{rev}"
        conn.execute("UPDATE items SET fields=?, status=?, revision=? WHERE id=?",
                     (json.dumps(f, ensure_ascii=False), status, rev, item_id))
        conn.execute("INSERT INTO events(event_id, item_id, status, revision, created_at) VALUES(?,?,?,?,?)",
                     (eid, item_id, status, rev, "2026-11-15T00:00:00Z"))
        conn.commit()
        return {"event_id": eid, "revision": rev, "status": status}
    finally:
        conn.close()


def external_resolve(qa: QA, item_id: str, fix_commit: str, fix_build: str) -> dict[str, Any]:
    """模拟研发在平台上处理并标记已修复（写入修复提交与修复构建）。"""
    external_set_status(qa, item_id, "IN_PROGRESS")
    return external_set_status(qa, item_id, "RESOLVED", fields={"fix_commit": fix_commit, "fix_build": fix_build})


def external_emit_event(qa: QA, item_id: str, status: str, revision: int, event_id: str) -> None:
    """只向事件流追加一条事件（不改工作项当前状态），用于模拟重复或迟到的旧事件。"""
    conn = _pf_rw(qa)
    try:
        conn.execute("INSERT INTO events(event_id, item_id, status, revision, created_at) VALUES(?,?,?,?,?)",
                     (event_id, item_id, status, revision, "2026-11-15T00:00:00Z"))
        conn.commit()
    finally:
        conn.close()


# ---- 常用产品操作 ----

def deploy(qa: QA, story: str, build: str) -> Result:
    r = qa.api_ok("lab", "deploy", "--story", story, "--build", build, "--dispatch")
    assert r.json["state"] == "succeeded", r.describe()
    return r


def run_cases(qa: QA, story: str, cases: list[str], purpose: str = "execution", script: str | None = None) -> Result:
    args = ["execution", "plan", "--story", story, "--cases", ",".join(cases), "--purpose", purpose, "--dispatch"]
    if script:
        args += ["--script", script]
    return qa.api(*args)


def propose(qa: QA, story: str, attempt_id: str, root_cause: str | None = None) -> Result:
    args = ["defect", "propose", "--story", story, "--attempt", attempt_id]
    if root_cause:
        args += ["--root-cause", root_cause]
    return qa.api(*args)


def version_file(qa: QA) -> Path:
    return qa.workspace() / "lab" / "target" / "version.json"


def status_blob(qa: QA) -> str:
    """status 的机器回执与人读输出合并成一段文本，用于查找面向使用者的提示。"""
    return qa.ok("status").out + "\n" + qa.text("status", "--details").out
