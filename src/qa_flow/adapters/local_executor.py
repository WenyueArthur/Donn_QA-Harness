"""本地受控执行器。

shell=False、参数数组和超时是基本控制，但不是安全沙箱。
本执行器不具备凭证、文件挂载和网络隔离能力，因此只运行已审可信脚本，并明确显示限制。
超时或超出输出限额时终止整个进程组，结果标为不完整，绝不记为通过。
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from qa_flow import clock
from qa_flow.canonical import sha256_bytes

ISOLATION = {"credentials": False, "filesystem": False, "network": False, "process_tree_cleanup": True}
SAFE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT")


def run(argv: list[str], *, cwd: Path, timeout_s: float, max_output_bytes: int,
        extra_env: dict[str, str] | None = None) -> dict[str, Any]:
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
    if timed_out or truncated:
        killed = _kill_tree(proc)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        killed = _kill_tree(proc) or killed
        proc.wait(timeout=5)
    sel.close()
    for s in (proc.stdout, proc.stderr):
        s.close()
    return {
        "argv_sha256": sha256_bytes("\0".join(argv).encode()),
        "exit_code": proc.returncode,
        "started_at": started,
        "finished_at": clock.now_iso(),
        "timed_out": timed_out,
        "truncated": truncated,
        "killed_process_group": killed,
        "process_group_alive": _group_alive(proc.pid),
        "stdout": bytes(out),
        "stderr": bytes(err),
        "isolation": ISOLATION,
        "python": sys.version.split()[0],
    }


def _kill_tree(proc: subprocess.Popen) -> bool:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
