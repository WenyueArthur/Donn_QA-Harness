"""批次 M1（存储、幂等、结果未知、恢复）验收用的辅助函数。

只通过子进程调用公共 CLI，并以只读连接独立读取磁盘上的数据库与文件；不导入 qa_flow。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import flow
from .cli import QA, Result

# 产品文档登记的失败退出码（errors.py 头部说明）；1 表示未处理异常，不属于稳定回执。
DOCUMENTED_FAILURE_CODES = {2, 3, 4, 5, 6, 7}
CRASH_EXIT = 99
TABLES = ("meta", "commands", "snapshots", "events", "aggregates", "dependencies", "decisions", "actions",
          "action_attempts", "worker_receipts", "evidence", "platform_events")
PAUSE_POINT = "before_send_check"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_after(hours: float) -> str:
    """当前真实时间之后若干小时的 ISO UTC 时刻，用于注入时钟。"""
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def story_id(qa: QA) -> str:
    tasks = qa.api_ok("task", "list").json["tasks"]
    assert len(tasks) == 1, tasks
    return tasks[0]["story_id"]


def drive_until_crash(qa: QA, point: str, max_rounds: int = 25) -> None:
    """按新人操作驱动 demo，并在指定崩溃点终止 resume 进程，得到确定的中间状态。"""
    if not qa.workspaces():
        qa.ok("demo")
    crashing = qa.test_mode(QA_FLOW_CRASH_AT=point)
    for _ in range(max_rounds):
        flow.answer_mine(qa)
        flow.transfer_others(qa)
        r = crashing.run("resume")
        if r.rc == CRASH_EXIT:
            return
        assert r.rc in (0, 3, 5), r.describe()
    raise AssertionError(f"驱动 demo 时没有到达崩溃点 {point}")


def drive_full(qa: QA) -> dict[str, Any]:
    v = flow.drive(qa, until=flow.until_stage("retrospective"))
    st = {s["stage"]: s["status"] for s in v["stages"]}
    assert st["test_exit"] == "completed" and st["retrospective"] == "completed", st
    return v


def recover(qa: QA) -> dict[str, Any]:
    """重启后的恢复检查：由产品以读写连接打开数据库（回滚未提交事务）并报告完整性。"""
    r = qa.ok("admin", "doctor")
    return r.json["workspace"]


def dump(qa: QA, ws: Path | None = None, tables: tuple[str, ...] = TABLES) -> dict[str, list[dict[str, Any]]]:
    return {t: qa.query(f"SELECT * FROM {t} ORDER BY 1, 2", ws=ws) for t in tables}


def count(qa: QA, table: str, where: str = "1=1", params: tuple = (), ws: Path | None = None) -> int:
    return qa.query(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params, ws=ws)[0]["n"]


def last_seq(qa: QA) -> int:
    return qa.query("SELECT COALESCE(MAX(seq), 0) AS s FROM events")[0]["s"]


def events_after(qa: QA, seq: int, event_type: str | None = None) -> list[dict[str, Any]]:
    rows = qa.query("SELECT * FROM events WHERE seq>? ORDER BY seq", (seq,))
    return [r for r in rows if event_type is None or r["event_type"] == event_type]


def stage_statuses(qa: QA, story: str) -> dict[str, str]:
    return qa.api_ok("stage", "status", "--story", story).json["statuses"]


def defects(qa: QA) -> list[dict[str, Any]]:
    return [json.loads(r["state"]) for r in qa.query("SELECT state FROM aggregates WHERE kind='defect' ORDER BY rowid")]


def action_row(qa: QA, action_id: str) -> dict[str, Any]:
    rows = qa.query("SELECT * FROM actions WHERE id=?", (action_id,))
    assert len(rows) == 1, rows
    return rows[0]


def snapshot_payload(qa: QA, sid: str) -> dict[str, Any]:
    rows = qa.query("SELECT canonical FROM snapshots WHERE id=?", (sid,))
    assert rows, f"快照不存在: {sid}"
    return json.loads(rows[0]["canonical"])["payload"]


def latest_snapshot(qa: QA, kind: str) -> dict[str, Any]:
    rows = qa.query("SELECT id, canonical FROM snapshots WHERE kind=? ORDER BY rowid DESC LIMIT 1", (kind,))
    assert rows, f"没有 {kind} 快照"
    return {"id": rows[0]["id"], "payload": json.loads(rows[0]["canonical"])["payload"]}


def platform_rows(qa: QA, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """只读查询实验平台桩账本；账本尚未建立时视为没有任何调用。"""
    path = qa.workspace() / "lab" / "platform.db"
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise
    finally:
        conn.close()


def platform_calls(qa: QA, op: str | None = None, outcome: str | None = None) -> list[dict[str, Any]]:
    rows = platform_rows(qa, "SELECT * FROM calls ORDER BY seq")
    return [r for r in rows if (op is None or r["op"] == op) and (outcome is None or r["outcome"] == outcome)]


def rejection_problems(r: Result, codes: set[str]) -> list[str]:
    """拒绝路径的回执检查：登记过的退出码、JSON 回执、稳定错误码、未提交。返回问题列表。"""
    problems = []
    if r.rc == 0:
        problems.append("命令以退出码 0 结束，没有拒绝")
    elif r.rc not in DOCUMENTED_FAILURE_CODES:
        problems.append(f"退出码 {r.rc} 不在产品登记的退出码内（可能是未处理异常）：{r.err[-300:]}")
    if not isinstance(r.json, dict):
        problems.append("标准输出不是 JSON 回执")
    elif r.rc != 0:
        if r.error_code not in codes:
            problems.append(f"错误码 {r.error_code} 不在预期集合 {sorted(codes)} 内")
        if r.json.get("committed") is not False:
            problems.append("拒绝回执的 committed 不是 false")
    return problems


def assert_rejected(r: Result, codes: set[str]) -> None:
    problems = rejection_problems(r, codes)
    assert not problems, "；".join(problems) + "\n" + r.describe()


def evidence_disk_files(ws: Path) -> set[str]:
    return {str(p.relative_to(ws)) for p in (ws / "evidence").rglob("*") if p.is_file()}


def tmp_files(ws: Path) -> set[str]:
    return {str(p.relative_to(ws)) for p in (ws / "tmp").rglob("*") if p.is_file()}


def consistency_problems(qa: QA, ws: Path | None = None) -> list[str]:
    """独立核对：事件序号连续、聚合修订与事件一致、没有悬空的有效引用。"""
    ws = ws or qa.workspace()
    out: list[str] = []
    conn = qa.db(ws)
    try:
        ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if ic != "ok":
            out.append(f"integrity_check: {ic}")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            out.append("存在外键不一致")
        seqs = [r[0] for r in conn.execute("SELECT seq FROM events ORDER BY seq")]
        if seqs != list(range(1, len(seqs) + 1)):
            out.append("事件序号不连续")
        snap_ids = {r[0] for r in conn.execute("SELECT id FROM snapshots")}
        ev_rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM evidence")}
        for r in conn.execute("SELECT id, canonical, sha256 FROM snapshots"):
            if sha256(r["canonical"]) != r["sha256"]:
                out.append(f"快照摘要不符 {r['id']}")
        for eid, e in ev_rows.items():
            if e["state"] != "active":
                continue
            f = ws / e["rel_path"]
            if not f.is_file():
                out.append(f"有效证据文件缺失 {eid}")
            elif sha256(f.read_bytes()) != e["sha256"]:
                out.append(f"有效证据摘要不符 {eid}")
        for r in conn.execute("SELECT result_id, input_id FROM dependencies"):
            if r["result_id"] not in snap_ids:
                out.append(f"依赖的结果对象不存在 {r['result_id']}")
            if r["input_id"] not in snap_ids and r["input_id"] not in ev_rows:
                out.append(f"依赖的输入对象不存在 {r['input_id']}")
        for r in conn.execute("SELECT object_id, revision, state FROM aggregates"):
            if r["object_id"].startswith("lineage:"):
                cur = json.loads(r["state"]).get("current")
                if cur not in snap_ids:
                    out.append(f"谱系当前版本悬空 {r['object_id']} → {cur}")
            mx = conn.execute("SELECT MAX(new_rev) FROM events WHERE object_id=?", (r["object_id"],)).fetchone()[0]
            if mx != r["revision"]:
                out.append(f"聚合修订与事件不一致 {r['object_id']}：修订 {r['revision']}，事件 {mx}")
        for r in conn.execute("SELECT id, plan_ref FROM actions"):
            if r["plan_ref"] not in snap_ids:
                out.append(f"操作计划悬空 {r['id']}")
        for r in conn.execute("SELECT id FROM decisions"):
            if r["id"] not in snap_ids:
                out.append(f"决定快照悬空 {r['id']}")
    finally:
        conn.close()
    return out


def unreferenced_evidence_files(qa: QA) -> set[str]:
    """磁盘上存在、但数据库没有任何证据记录引用的文件（崩溃遗留）。"""
    ws = qa.workspace()
    referenced = {r["rel_path"] for r in qa.query("SELECT rel_path FROM evidence")}
    return evidence_disk_files(ws) - referenced


def race(qa: QA, *commands: tuple[str, ...]) -> list[Result]:
    """同时启动多个 CLI 进程并等待全部结束（进程 join，不用 sleep 猜先后）。"""
    procs = [qa.popen(*c) for c in commands]
    results = []
    for c, p in zip(commands, procs):
        out, err = p.communicate(timeout=120)
        try:
            parsed = json.loads(out)
        except ValueError:
            parsed = None
        results.append(Result(list(c), p.returncode, out, err, parsed))
    return results


class PausedDispatch:
    """在“发送前重新核验”屏障处暂停的 dispatch 进程（充当旧 worker W1）。"""

    def __init__(self, qa: QA, action_id: str, pause_dir: Path):
        pause_dir.mkdir(parents=True, exist_ok=True)
        self.dir = pause_dir
        self.proc: subprocess.Popen = qa.test_mode(QA_FLOW_PAUSE_AT=PAUSE_POINT, QA_FLOW_PAUSE_DIR=str(pause_dir)).popen(
            "api", "action", "dispatch", "--id", action_id)
        deadline = time.monotonic() + 30
        waiting = pause_dir / f"{PAUSE_POINT}.waiting"
        while not waiting.exists():  # 等待屏障信号文件，不是固定时长等待
            assert self.proc.poll() is None, "W1 在到达屏障前已退出：" + (self.proc.stderr.read() or "")
            assert time.monotonic() < deadline, "W1 没有到达发送前屏障"
            time.sleep(0.02)

    def release(self) -> Result:
        (self.dir / f"{PAUSE_POINT}.go").write_text("go")
        out, err = self.proc.communicate(timeout=90)
        try:
            parsed = json.loads(out)
        except ValueError:
            parsed = None
        return Result(["api", "action", "dispatch"], self.proc.returncode, out, err, parsed)


def proposed_defects_workspace(qa: QA) -> tuple[str, list[dict[str, Any]]]:
    """demo 推进到“缺陷已提案、尚未在平台创建”。返回 (story_id, 缺陷列表)。"""
    drive_until_crash(qa, "after_commit:defect.propose_batch")
    ds = defects(qa)
    assert len(ds) == 2 and all(d["status"] == "proposed" and not d["external_id"] for d in ds), ds
    assert platform_calls(qa) == [], "前置条件：平台桩尚无任何调用"
    return story_id(qa), ds


def plan_defect_create(qa: QA, story: str, bug_id: str, *extra: str) -> dict[str, Any]:
    r = qa.api_ok("defect", "create", "--story", story, "--defect", bug_id, *extra)
    return r.json["action"]


def approve_broker_item(qa: QA) -> str:
    """以测试人员身份批准 broker 提出的操作授权事项。返回事项编号。"""
    mine = [c for c in flow.view(qa)["reviews"]["mine"] if "broker" in (c.get("raised_by") or [])]
    assert len(mine) == 1, mine
    qa.ok("review", "--answer", f"{mine[0]['handle']}=A")
    return mine[0]["handle"]


def planned_archive(qa: QA, story: str, report_ref: str, *extra: str) -> str:
    r = qa.api_ok("archive", "plan", "--story", story, "--report", report_ref, "--space", "lab-qa-archive", *extra)
    return r.json["action"]["id"]


def read_config(qa: QA) -> dict[str, Any]:
    return json.loads((qa.workspace() / "config" / "qa-flow.json").read_text(encoding="utf-8"))


def apply_config(qa: QA, cfg: dict[str, Any], tmp: Path, name: str) -> Result:
    path = tmp / f"cfg-{sha256(json.dumps(cfg, sort_keys=True).encode())[:8]}.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return qa.run("admin", "setup", "--config", str(path), "--name", name, "--apply")


# ---------------------------------------------------------------------------
# 旧规则桥接的合成项目（用于“校验器不可用”“陈旧路径”场景）
# ---------------------------------------------------------------------------

LEGACY_VALIDATORS = {
    "req_gate": "qa-mainflow/requirement-quality-gate/scripts/validate_report.py",
    "tech_gate": "qa-mainflow/tech-design-quality-gate/scripts/validate_tech_gate.py",
    "req_analysis": "qa-mainflow/requirements-analysis/scripts/validate_analysis.py",
    "case_design": "qa-mainflow/requirement-test-case-design/scripts/validate_traceability.py",
    "case_qc": "qa-mainflow/test-case-quality-review/scripts/validate_qc_report.py",
}


def _validator_source(marker: Path, tag: str) -> str:
    # 合成校验器：被执行时在标记文件留痕，输出合法 JSON 并以 0 退出。
    return ("import json\n"
            f"open({str(marker)!r}, 'a', encoding='utf-8').write({tag + chr(10)!r})\n"
            f"print(json.dumps({{'ok': True, 'by': {tag!r}}}))\n")


def write_validators(root: Path, marker: Path, tag: str) -> None:
    for rel in LEGACY_VALIDATORS.values():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(_validator_source(marker, tag), encoding="utf-8")


def legacy_project(qa: QA, base: Path) -> dict[str, Any]:
    """建立使用 legacy_bridge 规则的真实项目工作区，并让需求质检阶段经冻结校验器通过一次。"""
    legacy_root = base / "legacy-src"
    frozen_marker = base / "frozen-ran.txt"
    write_validators(legacy_root, frozen_marker, "frozen")
    cfg_dir = base / "cfg"
    cfg_dir.mkdir()
    fr = qa.ok("admin", "legacy-freeze", "--legacy-root", str(legacy_root), "--output",
               str(cfg_dir / "legacy-manifest.json"), workspace=False)
    src_dir = base / "docs"
    src_dir.mkdir()
    (src_dir / "req.md").write_text("# 积分兑换需求\n兑换数量必须为正整数。\n", encoding="utf-8")
    cfg = {
        "schema": "qa.project_profile", "profile": "project", "project": {"id": "PRJ-POINTS", "name": "积分项目"},
        "workspace": {"storage": "sqlite", "source_read_roots": [str(src_dir)], "protected_roots": []},
        "targets": [],
        "rules": {"kind": "legacy_bridge", "manifest": "legacy-manifest.json",
                  "manifest_sha256": fr.json["manifest_sha256"], "policy": {}},
        "identity": {"adapter": "none", "current_role": "tester"}, "model": {}, "adapters": [],
        "timing": {"source_max_age_hours": 72}, "retention": {},
        "budgets": {"max_request_bytes": 1048576, "max_output_bytes": 262144, "exec_timeout_seconds": 60,
                    "max_concurrency": 1, "max_private_storage_mb": 64},
    }
    cfg_path = cfg_dir / "qa-flow.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    setup = qa.ok("admin", "setup", "--config", str(cfg_path), "--apply")
    assert setup.json["applied"] is True and not setup.json["problems"], setup.describe()
    qa.ok("start", "--source", str(src_dir / "req.md"))
    story = story_id(qa)
    src = qa.query("SELECT id FROM snapshots WHERE kind='source_revision'")[0]["id"]
    qr = {"rule_ref": "legacy", "source_refs": [src],
          "findings": [{"id": "F1", "text": "兑换数量规则明确", "severity": "info",
                        "citations": [{"source_ref": src, "quote": "兑换数量必须为正整数。"}]}],
          "testability": {"verdict": "pass"}, "legacy_report": {"score": 90}}
    qr_path = base / "qr.json"
    qr_path.write_text(json.dumps(qr, ensure_ascii=False), encoding="utf-8")
    qa.api_ok("artifact", "submit", "--story", story, "--kind", "quality_review", "--file", str(qr_path))
    ev = qa.api_ok("stage", "evaluate", "--story", story, "--stage", "req_gate")
    assert ev.result["status"] == "completed", "前置条件：冻结校验器可用时需求质检应通过\n" + ev.describe()
    assert frozen_marker.read_text(encoding="utf-8").strip().splitlines() == ["frozen"]
    return {"story": story, "legacy_root": legacy_root, "frozen_marker": frozen_marker, "cfg": cfg, "cfg_dir": cfg_dir,
            "samples": _samples(base)}


def _samples(base: Path) -> Path:
    d = base / "samples" / "req_gate"
    d.mkdir(parents=True)
    (d / "s1.json").write_text("{}", encoding="utf-8")
    return base / "samples"


def plant_home_scripts(qa: QA, marker: Path) -> list[Path]:
    """在个人目录与当前目录放置同名旧脚本与清单；任何一个被执行都会在标记文件留痕。"""
    roots = [qa.home, qa.home / ".claude" / "skills", qa.home / ".claude" / "skills" / "qa-skills",
             qa.home / ".local" / "share" / "qa-flow", qa.cwd]
    for root in roots:
        write_validators(root, marker, f"home:{root.name}")
    return roots


def copy_backup(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst)
    return dst
