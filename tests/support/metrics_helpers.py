"""M7/M8 验收的辅助函数：指标输入、已推进的实验工作区、干净安装与临时源码副本。

不导入 qa_flow；预期值由调用方根据规格与夹具自行给出。
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from . import flow
from .cli import QA, REPO, Result, clean_env, write_json

SRC = REPO / "src"
LEGACY_ROOT = Path("/Users/donnie/PycharmProjects/qa-skills")
LEGACY_MANIFEST = REPO / "docs" / "baseline" / "legacy-rule-manifest.json"


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------

def lab_fixtures() -> dict[str, Any]:
    path = REPO / "docs" / "qa-flow-rebuild" / "lab-fixtures.json"
    return json.loads(path.read_text(encoding="utf-8"))


def fixture_data(name: str) -> dict[str, Any]:
    """取夹具中的指标输入，并去掉 expected（预期不交给产品）。"""
    data = copy.deepcopy(lab_fixtures()[name])
    data.pop("expected", None)
    return data


def compute(qa: QA, tmp: Path, metric: str, data: dict[str, Any], name: str = "m") -> Result:
    """调用 api metric compute。该接口直接返回指标对象。"""
    f = write_json(tmp / "metric-input" / f"{name}-{metric}.json", {"metric_name": metric, "data": data})
    return qa.api("metric", "compute", "--input", str(f))


def ratio(numerator: int, denominator: int) -> Fraction:
    return Fraction(numerator, denominator)


def assert_ratio(report: dict[str, Any], numerator: int, denominator: int) -> None:
    """先比整数分子分母，再比数值（允许产品保留 4 位小数）。"""
    assert report["numerator"] == numerator, report
    assert report["denominator"] == denominator, report
    assert report["value"] is not None, report
    assert abs(Fraction(str(report["value"])) - Fraction(numerator, denominator)) <= Fraction(1, 10000), report


def flat_text(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def all_keys(obj: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(str(k))
            out |= all_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= all_keys(v)
    return out


# ---------------------------------------------------------------------------
# 实验工作区
# ---------------------------------------------------------------------------

def new_qa(base: Path, root_name: str = "工作区 根", python: str | None = None) -> QA:
    base.mkdir(parents=True, exist_ok=True)
    for d in (root_name, "home", "cwd"):
        (base / d).mkdir(parents=True, exist_ok=True)
    if python:
        return PyQA(base / root_name, base / "home", base / "cwd", python=python)
    return QA(base / root_name, base / "home", base / "cwd")


def clone_qa(src: QA, base: Path) -> QA:
    """复制一个已推进的工作区，供会改状态的测试独立使用。"""
    base.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src.root, base / src.root.name)
    (base / "home").mkdir()
    (base / "cwd").mkdir()
    return QA(base / src.root.name, base / "home", base / "cwd", dict(src.env_extra))


def story_id(qa: QA) -> str:
    rows = qa.query("SELECT story_id FROM aggregates WHERE kind='task'")
    assert len(rows) == 1, rows
    return rows[0]["story_id"]


def platform_rows(qa: QA, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """只读查询实验平台桩的独立账本。"""
    path = qa.workspace() / "lab" / "platform.db"
    if not path.is_file():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def payload(qa: QA, snapshot_id: str) -> dict[str, Any]:
    rows = qa.query("SELECT CAST(canonical AS TEXT) AS c FROM snapshots WHERE id=?", (snapshot_id,))
    assert rows, f"快照不存在：{snapshot_id}"
    return json.loads(rows[0]["c"])["payload"]


def snapshots(qa: QA, kind: str) -> list[dict[str, Any]]:
    rows = qa.query("SELECT rowid AS n, id, sha256, created_at, CAST(canonical AS TEXT) AS c FROM snapshots "
                    "WHERE kind=? ORDER BY rowid", (kind,))
    return [{"n": r["n"], "id": r["id"], "sha256": r["sha256"], "created_at": r["created_at"],
             "payload": json.loads(r["c"])["payload"]} for r in rows]


def stage_fingerprint(qa: QA) -> list[tuple]:
    """阶段聚合的修订与状态，用于核对“运行状态指纹不变”。"""
    return [(r["object_id"], r["revision"], r["state"]) for r in qa.query(
        "SELECT object_id, revision, state FROM aggregates WHERE kind IN ('stage','task') ORDER BY object_id")]


def review_handle(qa: QA, needle: str) -> str:
    v = flow.view(qa)
    for c in v["reviews"]["mine"]:
        if needle in c["business_question"] or needle in c.get("affected_scope", "") or needle in c["kind"]:
            return c["handle"]
    raise AssertionError(f"没有找到待确认事项：{needle}；现有 {[c['business_question'] for c in v['reviews']['mine']]}")


def exit_reports_by_build(qa: QA) -> list[dict[str, Any]]:
    return [{"id": s["id"], "n": s["n"], "build": s["payload"].get("build"), "verdict": s["payload"].get("verdict")}
            for s in snapshots(qa, "exit_report")]


# ---------------------------------------------------------------------------
# 指定解释器或源码副本的调用器
# ---------------------------------------------------------------------------

@dataclass
class PyQA(QA):
    """用指定解释器（例如干净虚拟环境）调用 CLI。"""

    python: str = sys.executable

    def with_env(self, **env: str) -> "PyQA":
        return PyQA(self.root, self.home, self.cwd, {**self.env_extra, **env}, python=self.python)

    def _argv(self, args, json_out, workspace=True, non_interactive=True):  # type: ignore[override]
        argv = super()._argv(args, json_out, workspace, non_interactive)
        argv[0] = self.python
        return argv


def tree_digest(base: Path, exclude_dirs: tuple[str, ...] = ("__pycache__",)) -> dict[str, str]:
    import hashlib

    out = {}
    for p in sorted(base.rglob("*")):
        if p.is_symlink() or not p.is_file() or any(part in exclude_dirs for part in p.parts):
            continue
        out[str(p.relative_to(base))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def copy_src(dst: Path) -> Path:
    """把产品源码复制到临时目录（不含字节码缓存与构建产物）。"""
    shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
    return dst


def patch_text(path: Path, old: str, new: str, count: int | None = None) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"待替换文本不在 {path} 中：{old!r}"
    path.write_text(text.replace(old, new) if count is None else text.replace(old, new, count), encoding="utf-8")


def module_origin(qa: QA) -> str:
    """被测进程实际加载的 qa_flow 包位置（只用于确认副本生效，不用于计算预期）。"""
    argv = qa._argv((), False, workspace=False, non_interactive=False)[:1] + [
        "-c", "import importlib.util as u; print(u.find_spec('qa_flow').origin)"]
    p = subprocess.run(argv, cwd=str(qa.cwd), text=True, capture_output=True, env=clean_env(qa.home, qa.env_extra))
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def base_python() -> str:
    """当前虚拟环境所基于的解释器，用于离线构建与创建干净环境。"""
    return getattr(sys, "_base_executable", None) or sys.executable


def run_plain(argv: list[str], cwd: Path, home: Path, timeout: float = 600) -> subprocess.CompletedProcess:
    env = clean_env(home, {"PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1",
                           "PIP_CONFIG_FILE": os.devnull})
    return subprocess.run(argv, cwd=str(cwd), text=True, capture_output=True, env=env, timeout=timeout)
