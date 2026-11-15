"""本地受控执行器。

shell=False、参数数组和超时是基本控制，本身不是安全沙箱。
在 macOS 上若系统提供 sandbox-exec，则以操作系统沙箱运行脚本：禁止全部网络访问，禁止读取用户主目录与
工作区中未授权的内容（凭证、配置、私有证据）。其他平台没有该能力时如实报告“无隔离”。
即使有沙箱，文件系统也只是部分隔离（不是独立挂载），因此仍只运行已审可信脚本。
超时或超出输出限额时终止整个进程树，结果标为不完整，绝不记为通过。
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from qa_flow import clock
from qa_flow.canonical import sha256_bytes

NO_ISOLATION = {"credentials": False, "filesystem": False, "network": False, "process_tree_cleanup": True}
SAFE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT")
SANDBOX_EXEC = "/usr/bin/sandbox-exec"


@lru_cache(maxsize=1)
def sandbox_available() -> bool:
    """本机是否可用操作系统沙箱（macOS sandbox-exec）；实际执行一次最小探测，而不是只看文件是否存在。"""
    if sys.platform != "darwin" or not os.path.exists(SANDBOX_EXEC):
        return False
    try:
        r = subprocess.run([SANDBOX_EXEC, "-p", "(version 1)(allow default)(deny network*)", "/usr/bin/true"],
                           capture_output=True, timeout=10, shell=False)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def isolation() -> dict[str, Any]:
    """如实报告当前执行器的隔离能力。文件系统只是部分隔离，不报告为已隔离。"""
    if sandbox_available():
        return {"credentials": True, "filesystem": False, "network": True, "process_tree_cleanup": True,
                "mechanism": "macos_sandbox_exec",
                "note": "网络全部禁止；用户主目录与工作区内未授权内容不可读；不是独立文件系统挂载"}
    return dict(NO_ISOLATION, mechanism="none", note="仅 subprocess 参数控制，不是沙箱")


# 兼容旧引用：模块加载时的隔离能力快照
ISOLATION = isolation()


def _sb_quote(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandbox_profile(read_write: list[Path], read_only: list[Path], deny: list[Path]) -> str:
    """生成 sandbox-exec 规则：禁止网络；拒绝读取 deny 下的内容，再按需放行。后写的规则优先。"""
    def real(p: Path) -> str:
        return os.path.realpath(str(p))

    lines = ["(version 1)", "(allow default)", "(deny network*)"]
    for d in deny:
        lines.append(f"(deny file-read-data file-write* (subpath {_sb_quote(real(d))}))")
    for r in read_only:
        lines.append(f"(allow file-read-data (subpath {_sb_quote(real(r))}))")
    for rw in read_write:
        lines.append(f"(allow file-read-data file-write* (subpath {_sb_quote(real(rw))}))")
    return "\n".join(lines)


def default_denied(workspace_root: Path | None) -> list[Path]:
    """默认拒绝读取：用户主目录（含 HOME 环境变量指向的目录）与工作区根。"""
    out: list[Path] = []
    for h in {os.environ.get("HOME"), os.path.expanduser("~")}:
        if h and os.path.isdir(h):
            out.append(Path(h))
    if workspace_root is not None:
        out.append(Path(workspace_root))
    return out


def interpreter_paths() -> list[Path]:
    return [Path(p) for p in {sys.prefix, sys.base_prefix, sys.exec_prefix, os.path.dirname(sys.executable)} if p]


def run(argv: list[str], *, cwd: Path, timeout_s: float, max_output_bytes: int,
        extra_env: dict[str, str] | None = None, sandbox: dict[str, list[Path]] | None = None) -> dict[str, Any]:
    """sandbox：{"read_write": [...], "read_only": [...], "deny": [...]}；本机支持时以操作系统沙箱运行。"""
    iso = dict(NO_ISOLATION, mechanism="none")
    script_argv = list(argv)
    if sandbox is not None and sandbox_available():
        profile = sandbox_profile([Path(cwd), *sandbox.get("read_write", [])],
                                  [*interpreter_paths(), *sandbox.get("read_only", [])],
                                  sandbox.get("deny", []))
        argv = [SANDBOX_EXEC, "-p", profile, *argv]
        iso = isolation()
    env = {k: os.environ[k] for k in SAFE_ENV_KEYS if k in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)
    started = clock.now_iso()
    t0 = time.monotonic()
    proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, shell=False, start_new_session=True)
    out = bytearray()
    err = bytearray()
    truncated = False
    timed_out = False
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ, out)
    sel.register(proc.stderr, selectors.EVENT_READ, err)
    open_streams = 2
    while open_streams:
        remaining = timeout_s - (time.monotonic() - t0)
        if remaining <= 0:
            timed_out = True
            break
        for key, _ in sel.select(timeout=min(remaining, 0.5)):
            chunk = os.read(key.fileobj.fileno(), 65536)
            if not chunk:
                sel.unregister(key.fileobj)
                open_streams -= 1
                continue
            buf = key.data
            room = max_output_bytes - len(out) - len(err)
            if len(chunk) > room:
                buf.extend(chunk[:max(room, 0)])
                truncated = True
                break
            buf.extend(chunk)
        if truncated:
            break
    killed = False
    # 终止前先记下整棵进程树：脱离进程组（新会话）的孙进程仍以脚本为父进程，只杀进程组会漏掉它
    tree = _descendants(proc.pid) if (timed_out or truncated) else set()
    if timed_out or truncated:
        killed = _kill_tree(proc, tree)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        killed = _kill_tree(proc, tree) or killed
        proc.wait(timeout=5)
    if tree:
        _wait_gone(tree, 3.0)
    sel.close()
    for s in (proc.stdout, proc.stderr):
        s.close()
    return {
        "argv_sha256": sha256_bytes("\0".join(script_argv).encode()),
        "exit_code": proc.returncode,
        "started_at": started,
        "finished_at": clock.now_iso(),
        "timed_out": timed_out,
        "truncated": truncated,
        "killed_process_group": killed,
        "process_group_alive": _group_alive(proc.pid) or any(_pid_alive(p) for p in tree),
        "process_tree_size": len(tree),
        "stdout": bytes(out),
        "stderr": bytes(err),
        "isolation": iso,
        "python": sys.version.split()[0],
    }


def _descendants(root: int) -> set[int]:
    """按父进程关系收集 root 的全部后代（含已 setsid 脱离进程组的孙进程）。"""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=10,
                             shell=False).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found: set[int] = set()
    stack = [root]
    while stack:
        for c in children.get(stack.pop(), []):
            if c not in found:
                found.add(c)
                stack.append(c)
    return found


def _kill_tree(proc: subprocess.Popen, tree: set[int] | None = None) -> bool:
    killed = False
    try:
        os.killpg(proc.pid, signal.SIGKILL)
        killed = True
    except (ProcessLookupError, PermissionError):
        pass
    for pid in sorted(tree or ()):
        try:
            os.kill(pid, signal.SIGKILL)
            killed = True
        except (ProcessLookupError, PermissionError):
            pass
    return killed


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    # 已退出但尚未被回收的僵尸进程不算存活
    try:
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=5,
                            shell=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return True
    return bool(st) and not st.startswith("Z")


def _wait_gone(pids: set[int], timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and any(_pid_alive(p) for p in pids):
        time.sleep(0.05)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
