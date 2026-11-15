"""旧规则桥接（过渡期）。

- M0：冻结旧仓库工作区源码摘要清单（不复制安装镜像，不改旧仓库）。
- 运行：核对清单中每个文件的摘要，把冻结字节复制到私有临时目录，
  在 `python -I -B`、清空环境变量的子进程中调用旧仓库的纯校验器，输出只写私有临时目录。
- 不调用会修订交付物、写台账或上传的旧收尾脚本。
- 本机不具备网络隔离能力：结果中如实记录该限制。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from qa_flow import clock
from qa_flow.canonical import sha256_bytes, sha256_file
from qa_flow.errors import QAError

# 阶段 → 旧纯校验器（相对旧仓库根）
VALIDATORS: dict[str, dict[str, Any]] = {
    "req_gate": {"script": "qa-mainflow/requirement-quality-gate/scripts/validate_report.py", "args": ["--json"]},
    "tech_gate": {"script": "qa-mainflow/tech-design-quality-gate/scripts/validate_tech_gate.py", "args": ["--json"]},
    "req_analysis": {"script": "qa-mainflow/requirements-analysis/scripts/validate_analysis.py", "args": ["--json"]},
    "case_design": {"script": "qa-mainflow/requirement-test-case-design/scripts/validate_traceability.py",
                    "args": ["--json", "--coverage"]},
    "case_qc": {"script": "qa-mainflow/test-case-quality-review/scripts/validate_qc_report.py", "args": ["--json"]},
}
# 已知会写交付物、台账或上传的旧脚本：永不调用
FORBIDDEN_NAME_PARTS = ("finalize", "upload", "patch_", "ledger", "ingest", "install", "sync", "archive")
EXCLUDE_DIRS = {"__pycache__", ".git", "node_modules"}


def freeze(legacy_root: Path, output: Path) -> dict[str, Any]:
    root = Path(legacy_root).resolve()
    if not (root / "qa-mainflow").is_dir():
        raise QAError("CONFIG_INVALID", f"旧仓库根缺少 qa-mainflow: {root}")
    files = []
    for p in sorted((root / "qa-mainflow").rglob("*")):
        if p.is_symlink():
            continue
        if not p.is_file() or any(part in EXCLUDE_DIRS for part in p.parts) or p.suffix == ".pyc":
            continue
        rel = p.relative_to(root).as_posix()
        files.append({"path": rel, "sha256": sha256_file(p), "size": p.stat().st_size})
    for extra in ("AGENTS.md",):
        if (root / extra).is_file():
            files.append({"path": extra, "sha256": sha256_file(root / extra), "size": (root / extra).stat().st_size})
    commit, dirty = _git_state(root)
    manifest = {
        "schema": "qa.legacy_rule_manifest",
        "captured_at": clock.now_iso(),
        "legacy_root": str(root),
        "git_commit": commit,
        "worktree_dirty": dirty,
        "note": "冻结的是工作区字节摘要；工作区可能与提交不同，不能只凭提交号复现。不是完整源码备份。",
        "validators": VALIDATORS,
        "files": files,
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    output.write_bytes(data)
    return {"manifest": str(output), "manifest_sha256": sha256_bytes(data), "file_count": len(files),
            "git_commit": commit, "worktree_dirty": dirty}


def _git_state(root: Path) -> tuple[str, bool | None]:
    try:
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                                timeout=10, shell=False).stdout.strip() or "unknown"
        status = subprocess.run(["git", "--no-optional-locks", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True,
                                timeout=20, shell=False).stdout
        return commit, bool(status.strip())
    except (OSError, subprocess.SubprocessError):
        return "unknown", None


def verify_manifest(manifest_path: Path, expected_sha: str | None) -> dict[str, Any]:
    problems: list[str] = []
    try:
        data = Path(manifest_path).read_bytes()
    except OSError as exc:
        raise QAError("CONFIG_INVALID", f"配置指向的冻结规则清单不存在或不可读：{manifest_path}；"
                      "不会改用其他目录下的同名文件", path="rules.manifest") from exc
    if expected_sha and sha256_bytes(data) != expected_sha:
        problems.append("冻结清单本身的摘要与配置不符")
    try:
        manifest = json.loads(data)
    except ValueError as exc:
        raise QAError("CONFIG_INVALID", f"冻结规则清单无法解析：{manifest_path}", path="rules.manifest") from exc
    root = Path(manifest["legacy_root"])
    if not root.is_dir():
        problems.append(f"旧仓库根不可访问：{root}")
        return {"ok": False, "problems": problems, "manifest": manifest}
    changed = []
    for f in manifest["files"]:
        p = root / f["path"]
        if not p.is_file():
            changed.append(f"{f['path']}（缺失）")
        elif sha256_file(p) != f["sha256"]:
            changed.append(f"{f['path']}（摘要变化）")
    if changed:
        problems.append(f"{len(changed)} 个冻结文件已变化，旧规则不可用：" + "；".join(changed[:5]))
    for stage, v in manifest["validators"].items():
        if any(part in Path(v["script"]).name for part in FORBIDDEN_NAME_PARTS):
            problems.append(f"{stage} 指向禁止调用的旧收尾脚本")
        if not any(f["path"] == v["script"] for f in manifest["files"]):
            problems.append(f"{stage} 的校验器不在冻结清单中")
    return {"ok": not problems, "problems": problems, "manifest": manifest}


def run_validator(manifest_path: Path, manifest_sha: str | None, stage: str, report: dict[str, Any],
                  timeout_s: float, max_output_bytes: int) -> dict[str, Any]:
    check = verify_manifest(manifest_path, manifest_sha)
    if not check["ok"]:
        raise QAError("RULES_UNAVAILABLE", "冻结规则不可用：" + "；".join(check["problems"]))
    manifest = check["manifest"]
    v = manifest["validators"].get(stage)
    if v is None:
        raise QAError("RULES_UNAVAILABLE", f"阶段 {stage} 没有冻结的旧校验器")
    root = Path(manifest["legacy_root"])
    from qa_flow.adapters.local_executor import run

    with tempfile.TemporaryDirectory(prefix="qa-legacy-") as tmp:
        tmpd = Path(tmp)
        copy_root = tmpd / "frozen"
        for f in manifest["files"]:
            dst = copy_root / f["path"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / f["path"], dst)
            if sha256_file(dst) != f["sha256"]:
                raise QAError("RULES_UNAVAILABLE", f"复制冻结文件时摘要不符：{f['path']}")
            os.chmod(dst, 0o444)
        work = tmpd / "work"
        work.mkdir()
        report_path = work / "report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        argv = [sys.executable, "-I", "-B", str(copy_root / v["script"]), str(report_path), *v["args"]]
        res = run(argv, cwd=work, timeout_s=timeout_s, max_output_bytes=max_output_bytes)
    parsed: Any = None
    try:
        parsed = json.loads(res["stdout"].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        parsed = None
    complete = not res["timed_out"] and not res["truncated"] and parsed is not None
    if res["exit_code"] == 0 and complete:
        verdict = "pass"
    elif res["exit_code"] == 1 and complete:
        verdict = "fail"
    else:
        verdict = "indeterminate"
    return {
        "validator": v["script"],
        "validator_sha256": next(f["sha256"] for f in manifest["files"] if f["path"] == v["script"]),
        "manifest_git_commit": manifest.get("git_commit"),
        "exit_code": res["exit_code"],
        "verdict": verdict,
        "output": parsed,
        "stderr_sha256": sha256_bytes(res["stderr"]),
        "complete": complete,
        "limitations": ["执行器未隔离网络；旧校验器为纯本地校验", "源码以冻结副本只读运行"],
    }
