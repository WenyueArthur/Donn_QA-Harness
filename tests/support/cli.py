"""通过子进程调用公共 CLI 的封装。不导入 qa_flow 的任何模块。"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
BLOCKED_PREFIX = "BLOCKED:"

# 不向被测进程传递的环境变量前缀：代理、云凭证、个人令牌等。
_DROP_PREFIXES = ("AWS_", "GITLAB_", "GITHUB_", "FEISHU_", "LARK_", "PG_", "OPENAI_", "ANTHROPIC_", "QA_FLOW_")
_DROP_NAMES = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "PYTHONPATH"}


def blocked(reason: str) -> None:
    """把场景记为 blocked（缺少权限、目标或真人参与），不计入通过，也不等同于普通 skip。"""
    pytest.skip(f"{BLOCKED_PREFIX} {reason}")


def clean_env(home: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in _DROP_NAMES and not k.startswith(_DROP_PREFIXES)}
    env.update({"HOME": str(home), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "NO_PROXY": "*",
                "LC_ALL": "en_US.UTF-8"})
    if extra:
        env.update(extra)
    return env


@dataclass
class Result:
    argv: list[str]
    rc: int
    out: str
    err: str
    json: Any = None

    @property
    def ok(self) -> bool:
        return self.rc == 0

    @property
    def result(self) -> Any:
        """机器回执中的 result 字段；api 子命令直接返回对象时即对象本身。"""
        if isinstance(self.json, dict) and "result" in self.json and "status" in self.json:
            return self.json["result"]
        return self.json

    @property
    def errors(self) -> list[dict[str, Any]]:
        return (self.json or {}).get("errors", []) if isinstance(self.json, dict) else []

    @property
    def error_code(self) -> str | None:
        e = self.errors
        return e[0].get("code") if e else None

    def describe(self) -> str:
        return f"argv={self.argv}\nrc={self.rc}\nstdout={self.out[:3000]}\nstderr={self.err[:3000]}"


@dataclass
class QA:
    """绑定一个工作区根目录的 CLI 调用器。"""

    root: Path
    home: Path
    cwd: Path
    env_extra: dict[str, str] = field(default_factory=dict)

    def with_env(self, **env: str) -> "QA":
        return QA(self.root, self.home, self.cwd, {**self.env_extra, **env})

    def test_mode(self, **env: str) -> "QA":
        """启用产品的受控测试钩子（可注入时钟、崩溃点、屏障）。"""
        return self.with_env(QA_FLOW_TEST_MODE="1", **env)

    def popen(self, *args: str, json_out: bool = True, env: dict[str, str] | None = None) -> subprocess.Popen:
        return subprocess.Popen(self._argv(args, json_out), cwd=str(self.cwd), text=True, encoding="utf-8",
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=clean_env(self.home, {**self.env_extra, **(env or {})}))

    def _argv(self, args: tuple[str, ...], json_out: bool, workspace: bool = True,
              non_interactive: bool = True) -> list[str]:
        argv = [sys.executable, "-m", "qa_flow", *[str(a) for a in args]]
        if workspace and "--workspace" not in args:
            argv += ["--workspace", str(self.root)]
        if non_interactive and "--non-interactive" not in args:
            argv.append("--non-interactive")
        if json_out and "--json" not in args:
            argv.append("--json")
        return argv

    def run(self, *args: str, json_out: bool = True, env: dict[str, str] | None = None, timeout: float = 180,
            stdin: str | None = None, cwd: Path | None = None, workspace: bool = True,
            non_interactive: bool = True) -> Result:
        argv = self._argv(args, json_out, workspace, non_interactive)
        p = subprocess.run(argv, cwd=str(cwd or self.cwd), text=True, encoding="utf-8", input=stdin,
                           stdin=None if stdin is not None else subprocess.DEVNULL, capture_output=True,
                           timeout=timeout, env=clean_env(self.home, {**self.env_extra, **(env or {})}))
        parsed = None
        if json_out:
            try:
                parsed = json.loads(p.stdout)
            except ValueError:
                parsed = None
        return Result(argv, p.returncode, p.stdout, p.stderr, parsed)

    def text(self, *args: str, **kw: Any) -> Result:
        """人读输出（不加 --json）。"""
        return self.run(*args, json_out=False, **kw)

    def ok(self, *args: str, **kw: Any) -> Result:
        r = self.run(*args, **kw)
        assert r.rc == 0, "命令应成功\n" + r.describe()
        return r

    def api(self, *args: str, **kw: Any) -> Result:
        return self.run("api", *args, **kw)

    def api_ok(self, *args: str, **kw: Any) -> Result:
        return self.ok("api", *args, **kw)

    # ---- 独立观测：直接读取磁盘上的数据库与文件，不经过产品的判定字段 ----

    def workspaces(self) -> list[Path]:
        return sorted(p.parent for p in self.root.glob("*/qa.db"))

    def workspace(self) -> Path:
        ws = self.workspaces()
        assert len(ws) == 1, f"期望恰好一个项目工作区，实际 {ws}"
        return ws[0]

    def db(self, ws: Path | None = None) -> sqlite3.Connection:
        """以只读方式打开工作区数据库，用于独立核对事件与关系。"""
        path = (ws or self.workspace()) / "qa.db"
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def query(self, sql: str, params: tuple = (), ws: Path | None = None) -> list[dict[str, Any]]:
        conn = self.db(ws)
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()


def write_json(path: Path, obj: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def sha256_file(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_digest(base: Path) -> dict[str, str]:
    """目录内全部文件的摘要，用于核对运行前后未被修改。"""
    return {str(p.relative_to(base)): sha256_file(p) for p in sorted(base.rglob("*")) if p.is_file()}
