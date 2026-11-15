"""受控故障点。仅在测试进程显式设置 QA_FLOW_TEST_MODE=1 时可用，正式运行默认不可用。

QA_FLOW_CRASH_AT=<点名>[,<点名>...] 在对应位置立即终止进程（模拟崩溃）。
"""

from __future__ import annotations

import os

from qa_flow.clock import test_mode

CRASH_EXIT = 99


def pause_point(name: str, timeout_s: float = 60.0) -> None:
    """同步屏障：QA_FLOW_PAUSE_AT=<点名> 且 QA_FLOW_PAUSE_DIR 存在时，写出 <点名>.waiting 并等待 <点名>.go。"""
    if not test_mode() or os.environ.get("QA_FLOW_PAUSE_AT") != name:
        return
    import time
    from pathlib import Path

    d = Path(os.environ.get("QA_FLOW_PAUSE_DIR", ""))
    if not d.is_dir():
        return
    safe = name.replace(":", "_").replace("/", "_")
    (d / f"{safe}.waiting").write_text(str(os.getpid()))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and not (d / f"{safe}.go").exists():
        time.sleep(0.05)


def crash_point(name: str) -> None:
    if not test_mode():
        return
    points = {p.strip() for p in os.environ.get("QA_FLOW_CRASH_AT", "").split(",") if p.strip()}
    if name in points:
        os._exit(CRASH_EXIT)
