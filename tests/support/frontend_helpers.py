"""日常入口与集中确认验收的辅助函数。只通过子进程与只读查询观测，不导入 qa_flow。"""

from __future__ import annotations

import hashlib
import json
import os
import pty
import select
import sqlite3
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .cli import QA, REPO, Result, clean_env
from . import flow

DEMO_TITLE = "退款实验（合成数据）"
# 随包的合成需求文件：只作为数据文件读取，不导入产品模块
LAB_REQUIREMENT = REPO / "src" / "qa_flow" / "resources" / "lab" / "demo" / "refund-requirement.md"
LAB_PROFILE = REPO / "src" / "qa_flow" / "resources" / "lab" / "profile.json"
QUICKSTART = REPO / "docs" / "qa-flow-rebuild" / "QUICKSTART.md"
CRASH_RC = 99

# 日常输出中不应出现的内部参数名（方案 AC-123、AC-141）
INTERNAL_TERMS = ("command_id", "expected_revision", "expected-revision", "command-id", "lease", "租约", "修订号",
                  "idempotency", "revision")
# 不安全的恢复建议（设计 §11.7）
UNSAFE_HINTS = ("手改", "手工修改", "直接修改数据库", "改 JSON", "改JSON", "填 completed", "completed=", "关闭校验",
                "跳过门禁", "sqlite3 ", "UPDATE ")


# ---------------------------------------------------------------------------
# 终端（pty）调用：用于核对交互模式与“不等待输入”
# ---------------------------------------------------------------------------

def tty_run(qa: QA, args: list[str], feeds: list[tuple[str, str]] | None = None, timeout: float = 60,
            workspace: bool = True) -> tuple[int | None, str, bool]:
    """在伪终端中执行命令。feeds 为 (等待出现的文字, 随后输入的一行)。返回 (退出码, 输出, 是否超时)。"""
    argv = [sys.executable, "-m", "qa_flow", *args]
    if workspace:
        argv += ["--workspace", str(qa.root)]
    master, slave = pty.openpty()
    p = subprocess.Popen(argv, cwd=str(qa.cwd), stdin=slave, stdout=slave, stderr=slave,
                         env=clean_env(qa.home, qa.env_extra), close_fds=True)
    os.close(slave)
    buf, pos, feeds = b"", 0, list(feeds or [])
    deadline = time.monotonic() + timeout
    timed_out = True
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                timed_out = False
                break
            buf += chunk
            if feeds and feeds[0][0].encode("utf-8") in buf[pos:]:
                pos = len(buf)
                os.write(master, (feeds.pop(0)[1] + "\n").encode("utf-8"))
        elif p.poll() is not None:
            timed_out = False
            break
    if timed_out:
        p.kill()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
    os.close(master)
    return (None if timed_out else p.returncode), buf.decode("utf-8", "replace").replace("\r\n", "\n"), timed_out


# ---------------------------------------------------------------------------
# 独立观测：事件、操作与外部账本
# ---------------------------------------------------------------------------

def _table_counts(path: Path) -> dict[str, int]:
    if not path.is_file():
        return {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {n: conn.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in sorted(names)}
    finally:
        conn.close()


def platform_rows(qa: QA, sql: str, ws: Path | None = None) -> list[tuple]:
    path = (ws or qa.workspace()) / "lab" / "platform.db"
    if not path.is_file():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def refund_digest(qa: QA, ws: Path | None = None) -> str:
    """实验退款库的业务数据摘要（订单、退款、记账）。"""
    path = (ws or qa.workspace()) / "lab" / "target" / "refund.db"
    if not path.is_file():
        return "absent"
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = [conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in ("orders", "refunds", "ledger")]
    finally:
        conn.close()
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


def ledger(qa: QA, ws: Path | None = None) -> dict[str, Any]:
    """状态与副作用账本快照：用于核对只读命令前后没有任何新增。"""
    ws = ws or qa.workspace()

    def one(sql: str) -> Any:
        return qa.query(sql, ws=ws)[0]["n"]

    return {
        "event_seq": one("SELECT COALESCE(MAX(seq),0) n FROM events"),
        "commands": one("SELECT COUNT(*) n FROM commands"),
        "snapshots": one("SELECT COUNT(*) n FROM snapshots"),
        "decisions": one("SELECT COUNT(*) n FROM decisions"),
        "actions": one("SELECT COUNT(*) n FROM actions"),
        "action_attempts": one("SELECT COUNT(*) n FROM action_attempts"),
        "action_states": sorted((a["id"], a["state"], a["attempts"]) for a in
                                qa.query("SELECT id, state, attempts FROM actions", ws=ws)),
        "platform": _table_counts(ws / "lab" / "platform.db"),
        "refund": refund_digest(qa, ws),
    }


def decisions(qa: QA) -> list[dict[str, Any]]:
    return qa.query("SELECT id, role, actor, verification, result, subjects, scope, revoked_at FROM decisions ORDER BY created_at, id")


def review_items(qa: QA) -> dict[str, dict[str, Any]]:
    """直接读聚合表中的待确认事项原始状态（按编号）。"""
    out = {}
    for r in qa.query("SELECT object_id, state FROM aggregates WHERE kind='review_item'"):
        st = json.loads(r["state"])
        out[st["handle"]] = st
    return out


def stage_of(view: dict[str, Any], stage: str) -> dict[str, Any]:
    return flow.stage_map(view)[stage]


def cards(view: dict[str, Any]) -> list[dict[str, Any]]:
    return view["reviews"]["mine"] + view["reviews"]["others"]


# ---------------------------------------------------------------------------
# 把合成实验推进到指定检查点（只用公共命令与受控测试钩子）
# ---------------------------------------------------------------------------

def to_confirmed(qa: QA) -> dict[str, Any]:
    """demo → 答复 Q1 → 生成计划与用例 → 批准计划 → 转交关键预期（实验审批桩模拟答复）。此时尚未执行任何用例。"""
    qa.ok("demo")
    flow.answer_mine(qa)
    qa.ok("resume")
    flow.answer_mine(qa)
    flow.transfer_others(qa)
    return flow.view(qa)


def crash_resume(qa: QA, point: str) -> Result:
    """在指定崩溃点终止一次 resume，模拟回执未展示就中断。"""
    r = qa.test_mode(QA_FLOW_CRASH_AT=point).run("resume")
    assert r.rc == CRASH_RC, f"期望在 {point} 处崩溃（退出码 {CRASH_RC}）\n" + r.describe()
    return r


def deployment_id(qa: QA) -> str:
    rows = qa.query("SELECT id FROM snapshots WHERE kind='deployment' ORDER BY created_at")
    assert rows, "应已有实验部署记录"
    return rows[-1]["id"]


def to_entry_gap(qa: QA) -> dict[str, Any]:
    """环境与用例有效，必需冒烟 SMOKE-2 尚未执行。

    做法：日常 resume 规划环境探测后中断；经 api 派发同一探测动作、评估环境阶段，再只执行 SMOKE-1。
    """
    to_confirmed(qa)
    crash_resume(qa, f"after_commit:action.plan:probe:{deployment_id(qa)}")
    acts = qa.api_ok("action", "list", "--story", DEMO_TITLE).result["actions"]
    probe = [a for a in acts if a["kind"] == "environment.probe"]
    assert len(probe) == 1 and probe[0]["state"] in ("ready", "planned"), acts
    d = qa.api_ok("action", "dispatch", "--id", probe[0]["id"]).json
    assert d["state"] == "succeeded", d
    env = qa.api_ok("stage", "evaluate", "--story", DEMO_TITLE, "--stage", "env_ready").result
    assert env["status"] == "completed", env
    s1 = qa.api_ok("execution", "plan", "--story", DEMO_TITLE, "--cases", "SMOKE-1", "--purpose", "smoke",
                   "--dispatch").json
    assert s1["state"] == "succeeded", s1
    return flow.view(qa)


def to_failed_execution(qa: QA) -> dict[str, Any]:
    """build1 上已执行并出现失败用例、已形成缺陷提案，尚未建单与修复。"""
    to_confirmed(qa)
    crash_resume(qa, "after_commit:defect.propose_batch")
    return flow.view(qa)


def edit_source(ws: Path, name: str, old: str, new: str) -> Path:
    """修改测试自己工作区内的合成来源文件，用于制造来源变更。"""
    p = ws / "lab" / "materials" / name
    text = p.read_text(encoding="utf-8")
    assert old in text, f"来源中应包含待替换文字：{old}"
    os.chmod(p, 0o644)
    p.write_text(text.replace(old, new), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 网络守卫：被测进程启动时装载，非本机连接一律记录并拒绝
# ---------------------------------------------------------------------------

_GUARD = '''\
import os, socket
_log = os.environ.get("QA_NETGUARD_LOG")
_LOCAL = {"127.0.0.1", "::1", "localhost", ""}
def _w(line):
    if _log:
        with open(_log, "a", encoding="utf-8") as f:
            f.write(line + "\\n")
_w("LOADED %d" % os.getpid())
_orig_connect, _orig_connect_ex, _orig_gai = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo
def _check(sock, addr):
    if sock.family == getattr(socket, "AF_UNIX", None):
        return
    host = addr[0] if isinstance(addr, tuple) else str(addr)
    if host not in _LOCAL:
        _w("CONNECT %r" % (addr,))
        raise OSError("验收网络守卫：禁止非本机连接 %r" % (addr,))
def _connect(self, addr):
    _check(self, addr)
    return _orig_connect(self, addr)
def _connect_ex(self, addr):
    _check(self, addr)
    return _orig_connect_ex(self, addr)
def _gai(host, *a, **k):
    if host not in _LOCAL and host is not None:
        _w("RESOLVE %r" % (host,))
        raise socket.gaierror("验收网络守卫：禁止解析外部主机 %r" % (host,))
    return _orig_gai(host, *a, **k)
socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = _connect, _connect_ex, _gai
'''


def with_network_guard(qa: QA, tmp: Path) -> tuple[QA, Path]:
    d = tmp / "netguard"
    d.mkdir(parents=True, exist_ok=True)
    (d / "sitecustomize.py").write_text(_GUARD, encoding="utf-8")
    log = tmp / "netguard.log"
    return qa.with_env(PYTHONPATH=str(d), QA_NETGUARD_LOG=str(log)), log


# ---------------------------------------------------------------------------
# QUICKSTART 命令提取
# ---------------------------------------------------------------------------

def quickstart_commands() -> list[str]:
    """从一页指南中提取全部 `python -m qa_flow ...` 命令（代码块与行内代码）。"""
    import re

    text = QUICKSTART.read_text(encoding="utf-8")
    found = re.findall(r"python -m qa_flow[^`\n|；]*", text)
    out: list[str] = []
    for c in (x.strip() for x in found):
        if c and c not in out:
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# 新人验收报告：独立参考模型（依据方案 §10.3 的拟定门槛，不调用产品函数）
# ---------------------------------------------------------------------------

TASK_IDS = ["N1", "N2", "N3", "N4", "N5"]


def observation(participant: str, task: str, *, start: str = "2026-11-16T01:00:00Z", seconds: int = 60,
                completed: bool = True, help_events: int = 0, manual: int = 0, unsafe: int = 0, repeated: int = 0,
                resume_success: bool | None = None, understanding: Any = "correct", notes: str = "") -> dict[str, Any]:
    t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
    t1 = datetime.fromtimestamp(t0.timestamp() + seconds, tz=t0.tzinfo)
    return {
        "participant_pseudonym": participant, "task_id": task, "started_at": start,
        "finished_at": t1.strftime("%Y-%m-%dT%H:%M:%SZ"), "independently_completed": completed,
        "help_events": help_events, "resume_success": (completed if task == "N5" else None) if resume_success is None
        else resume_success, "decision_understanding": understanding, "manual_json_or_source_edits": manual,
        "unsafe_or_duplicate_effects": unsafe, "repeated_question_same_valid_scope": repeated, "notes": notes,
    }


def full_trial(participants: list[str] | None = None, seconds: int = 60) -> list[dict[str, Any]]:
    """每人 5 项、全部独立完成的观察记录。"""
    ps = participants or ["P1", "P2", "P3", "P4", "P5"]
    return [observation(p, t, seconds=seconds) for p in ps for t in TASK_IDS]


def reference_report(obs: list[dict[str, Any]], required_participants: int = 5) -> dict[str, Any]:
    """参考计算：每人每项均须独立完成；求助、手改、不安全动作、重复确认与关键误解任一非零即不通过。"""
    participants = sorted({o["participant_pseudonym"] for o in obs})
    cells = {(p, t): [o for o in obs if o["participant_pseudonym"] == p and o["task_id"] == t]
             for p in participants for t in TASK_IDS}
    missing = [k for k, v in cells.items() if not v]
    failed_records = [o for o in obs if o.get("independently_completed") is not True]
    durations = [(datetime.fromisoformat(o["finished_at"].replace("Z", "+00:00"))
                  - datetime.fromisoformat(o["started_at"].replace("Z", "+00:00"))).total_seconds() for o in obs]
    n5 = [o for o in obs if o["task_id"] == "N5"]
    counts = {
        "independent": sum(1 for o in obs if o.get("independently_completed") is True),
        "total_records": len(obs),
        "help_events": sum(int(o.get("help_events") or 0) for o in obs),
        "manual": sum(int(o.get("manual_json_or_source_edits") or 0) for o in obs),
        "unsafe": sum(int(o.get("unsafe_or_duplicate_effects") or 0) for o in obs),
        "repeated": sum(int(o.get("repeated_question_same_valid_scope") or 0) for o in obs),
        "resume_success": sum(1 for o in n5 if o.get("resume_success")),
        "resume_total": len(n5),
    }
    misunderstanding = [o for o in obs if o.get("decision_understanding") not in ("correct", True, None)]
    ok = (bool(obs) and len(participants) >= required_participants and not missing and not failed_records
          and counts["help_events"] == 0 and counts["manual"] == 0 and counts["unsafe"] == 0
          and counts["repeated"] == 0 and not misunderstanding)
    return {"participants": participants, "counts": counts, "durations": durations,
            "median": statistics.median(durations) if durations else None,
            "max": max(durations) if durations else None,
            "verdict": "pass" if ok else ("indeterminate" if not obs else "fail")}


def evaluate_usability(qa: QA, tmp: Path, obs: Any, name: str = "observations.json") -> Result:
    f = tmp / name
    f.write_text(json.dumps(obs, ensure_ascii=False, indent=2), encoding="utf-8")
    return qa.api("usability", "evaluate", "--input", str(f))
