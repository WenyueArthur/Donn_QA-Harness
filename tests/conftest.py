"""独立验收的公共夹具与结果记录。

约束（方案 §2、§6）：
- 只通过子进程调用公共 CLI（python -m qa_flow），不导入产品内部模块计算预期。
- 每个测试使用独立临时工作区与独立 HOME，不读取开发者 HOME 中的账号或凭证。
- blocked 单独记录，不并入 pass 或 skip。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))

from support.cli import BLOCKED_PREFIX, QA, REPO  # noqa: E402

ARTIFACTS = REPO / "artifacts"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "ac(*ids): 本测试对应的验收场景编号，如 AC-001")
    config.addinivalue_line("markers", "layer(name): 场景所在层 offline/lab/parity/package/model/external/usability")
    config._ac_records = []  # type: ignore[attr-defined]


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    """工作区根目录。目录名含中文与空格，用于同时覆盖路径处理。"""
    r = tmp_path / "工作区 根"
    r.mkdir()
    return r


@pytest.fixture()
def qa(root: Path, home: Path, tmp_path: Path) -> QA:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    return QA(root=root, home=home, cwd=cwd)


@pytest.fixture()
def spec() -> dict:
    """交接包中的规格文件（只读）。"""
    d = REPO / "docs" / "qa-flow-rebuild"
    return {
        "fixtures": json.loads((d / "lab-fixtures.json").read_text(encoding="utf-8")),
        "cases": json.loads((d / "acceptance-cases.json").read_text(encoding="utf-8"))["cases"],
        "requirements": json.loads((d / "requirements.json").read_text(encoding="utf-8")),
    }


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    rep = outcome.get_result()
    ids = [a for m in item.iter_markers("ac") for a in m.args]
    if not ids:
        return
    if rep.when == "call" or (rep.when == "setup" and rep.outcome != "passed"):
        status = {"passed": "pass", "failed": "fail", "skipped": "skipped"}[rep.outcome]
        reason = ""
        if rep.outcome == "skipped":
            reason = str(rep.longrepr[2]) if isinstance(rep.longrepr, tuple) else str(rep.longrepr)
            reason = reason.removeprefix("Skipped: ")
            if reason.startswith(BLOCKED_PREFIX):
                status, reason = "blocked", reason[len(BLOCKED_PREFIX):].strip()
        elif rep.outcome == "failed":
            reason = str(getattr(rep.longrepr, "reprcrash", None) and rep.longrepr.reprcrash.message or rep.longrepr)[:1500]
        layer = next((m.args[0] for m in item.iter_markers("layer")), None)
        item.config._ac_records.append({  # type: ignore[attr-defined]
            "nodeid": item.nodeid, "ac": ids, "layer": layer, "status": status, "phase": rep.when,
            "reason": reason, "duration_s": round(rep.duration, 3)})


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    recs = getattr(session.config, "_ac_records", [])
    if not recs or os.environ.get("QA_ACCEPTANCE_NO_RECORD") == "1":
        return
    out = ARTIFACTS / "runs"
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    (out / f"run-{stamp}-{os.getpid()}.json").write_text(json.dumps({
        "started_args": list(session.config.invocation_params.args), "exit_status": int(exitstatus),
        "python": sys.version.split()[0], "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "records": recs}, ensure_ascii=False, indent=2), encoding="utf-8")
