"""pytest 实时进度条插件（只用标准库）。

用法：scripts/run-tests.sh [pytest 参数...]，或 python -m pytest -p pytest_progress（需把 tools/ 加入 PYTHONPATH）。

- 终端模式（标准输出是终端且能打开 /dev/tty）：两行进度区原地刷新，每 0.2 秒更新一次，
  显示进度条、百分比、完成数、通过/失败/跳过计数、已用时间、预计剩余时间和当前用例。
  失败或出错的用例立即打印在进度区上方，不用等到最后。
- 纯文本模式（输出被重定向到文件或管道，例如 CI 或后台任务）：每完成 5% 打印一行进度，失败时立即打印一行。
- QA_PROGRESS=0 关闭本插件；QA_PROGRESS=plain 强制纯文本模式。

运行期间屏蔽 pytest 默认的点状进度输出；结束后照常打印 pytest 自己的失败详情与汇总。
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
import unicodedata
from typing import Any

import pytest

SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
EIGHTHS = "▏▎▍▌▋▊▉"
RESET, BOLD, DIM = "\x1b[0m", "\x1b[1m", "\x1b[2m"
GREEN, RED, YELLOW, CYAN, GREY = "\x1b[32m", "\x1b[31m", "\x1b[33m", "\x1b[36m", "\x1b[90m"
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _fmt(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _width(text: str) -> int:
    """可见宽度：去掉颜色控制符，中文等宽字符按 2 列计。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in _ANSI.sub("", text))


def _truncate(text: str, max_w: int) -> str:
    if _width(text) <= max_w:
        return text
    out, w = [], 0
    for c in reversed(text):  # 保留尾部：用例名比路径更有用
        cw = 2 if unicodedata.east_asian_width(c) in "WF" else 1
        if w + cw > max_w - 1:
            break
        out.append(c)
        w += cw
    return "…" + "".join(reversed(out))


def _bar(frac: float, width: int, color: str) -> str:
    frac = min(max(frac, 0.0), 1.0)
    cells = frac * width
    full = int(cells)
    part = ""
    if full < width:
        idx = int((cells - full) * 8)
        part = EIGHTHS[idx - 1] if idx > 0 else ""
    empty = width - full - (1 if part else 0)
    return f"{color}{'█' * full}{part}{RESET}{GREY}{'░' * empty}{RESET}"


class Progress:
    def __init__(self, config: pytest.Config, mode: str):
        self.config = config
        self.mode = mode  # "tty" | "plain"
        self.total = 0
        self.done = 0
        self.passed = self.failed = self.skipped = self.errors = 0
        self.current = ""
        self.t0 = time.monotonic()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.tty: Any = None
        self.drawn = False
        self.spin_i = 0
        self.next_plain_pct = 5
        self.reporter: Any = None
        self.suppress = False
        self._orig_write = self._orig_line = None

    # 屏蔽 pytest 默认进度输出 -------------------------------------------------
    def attach(self) -> None:
        self.reporter = self.config.pluginmanager.get_plugin("terminalreporter")
        tw = getattr(self.reporter, "_tw", None)
        if tw is None:
            return
        self._orig_write, self._orig_line = tw.write, tw.line

        def write(s: str = "", *a: Any, **k: Any) -> None:
            if not self.suppress:
                self._orig_write(s, *a, **k)

        def line(s: str = "", *a: Any, **k: Any) -> None:
            if not self.suppress:
                self._orig_line(s, *a, **k)

        tw.write, tw.line = write, line
        self.suppress = True

    def detach(self) -> None:
        self.suppress = False

    # 输出 -------------------------------------------------------------------
    def _out(self, text: str) -> None:
        if self.mode == "tty":
            self.tty.write(text)
            self.tty.flush()
        elif self._orig_write is not None:
            self._orig_write(text, flush=True)

    def _clear(self) -> None:
        if self.drawn:
            self._out("\r\x1b[2K\x1b[1A\r\x1b[2K")
            self.drawn = False

    def _counts(self) -> str:
        bad = self.failed + self.errors
        return (f"{GREEN}✓ {self.passed}{RESET}  "
                f"{(RED + BOLD) if bad else GREY}✗ {bad}{RESET}  "
                f"{YELLOW if self.skipped else GREY}○ {self.skipped}{RESET}")

    def _times(self) -> str:
        elapsed = time.monotonic() - self.t0
        if self.done >= 3 and self.done < self.total:
            eta = elapsed / self.done * (self.total - self.done)
            return f"{GREY}用时{RESET} {_fmt(elapsed)}  {GREY}剩余约{RESET} {_fmt(eta)}"
        return f"{GREY}用时{RESET} {_fmt(elapsed)}"

    def _draw(self) -> None:
        cols = max(40, shutil.get_terminal_size((100, 20)).columns)
        frac = self.done / self.total if self.total else 0.0
        color = RED if (self.failed or self.errors) else GREEN
        spin = SPIN[self.spin_i % len(SPIN)]
        tail = f" {BOLD}{int(frac * 100):>3}%{RESET}  {self.done}/{self.total}  {self._counts()}  {self._times()}"
        bar_w = max(10, min(40, cols - _width(tail) - 4))
        line1 = f"{CYAN}{spin}{RESET} {_bar(frac, bar_w, color)}{tail}"
        if _width(line1) > cols - 1:  # 太窄时去掉时间
            tail = f" {BOLD}{int(frac * 100):>3}%{RESET}  {self.done}/{self.total}  {self._counts()}"
            line1 = f"{CYAN}{spin}{RESET} {_bar(frac, bar_w, color)}{tail}"
        line2 = f"  {GREY}↳ {_truncate(self.current, cols - 6)}{RESET}" if self.current else ""
        self._clear()
        self._out(f"{line1}\n{line2}")
        self.drawn = True

    def refresh(self) -> None:
        with self.lock:
            if self.mode == "tty":
                self._draw()

    def _ticker(self) -> None:
        while not self.stop.wait(0.2):
            with self.lock:
                self.spin_i += 1
                self._draw()

    # 生命周期 ---------------------------------------------------------------
    def start(self, total: int) -> None:
        self.total = total
        self.t0 = time.monotonic()
        self.attach()
        if self.mode == "tty":
            self._out("\x1b[?25l")  # 隐藏光标
            self.refresh()
            self.thread = threading.Thread(target=self._ticker, name="pytest-progress", daemon=True)
            self.thread.start()
        else:
            self._out(f"开始执行 {total} 个用例\n")

    def failure(self, nodeid: str, phase: str, duration: float) -> None:
        tag = {"call": "失败", "setup": "准备出错", "teardown": "清理出错"}.get(phase, phase)
        with self.lock:
            self._clear()
            self._out(f"{RED}✗ {tag}{RESET} {nodeid} {GREY}({duration:.1f}s){RESET}\n")
            if self.mode == "tty":
                self._draw()

    def finished_one(self) -> None:
        self.done += 1
        if self.mode == "tty":
            self.refresh()
            return
        pct = self.done * 100 // self.total if self.total else 100
        if pct >= self.next_plain_pct or self.done == self.total:
            self.next_plain_pct = (pct // 5 + 1) * 5
            elapsed = time.monotonic() - self.t0
            eta = elapsed / self.done * (self.total - self.done) if self.done else 0
            self._out(f"[{pct:>3}%] {self.done}/{self.total}  通过 {self.passed}  失败 {self.failed + self.errors}  "
                      f"跳过 {self.skipped}  用时 {_fmt(elapsed)}  剩余约 {_fmt(eta)}\n")

    def finish(self) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)
        with self.lock:
            elapsed = _fmt(time.monotonic() - self.t0)
            if self.mode == "tty":
                self._clear()
                color = RED if (self.failed or self.errors) else GREEN
                cols = max(40, shutil.get_terminal_size((100, 20)).columns)
                frac = self.done / self.total if self.total else 1.0
                tail = f" {BOLD}{int(frac * 100):>3}%{RESET}  {self.done}/{self.total}  {self._counts()}  {GREY}用时{RESET} {elapsed}"
                bar_w = max(10, min(40, cols - _width(tail) - 4))
                mark = f"{RED}✗{RESET}" if (self.failed or self.errors) else f"{GREEN}✔{RESET}"
                self._out(f"{mark} {_bar(frac, bar_w, color)}{tail}\n\x1b[?25h")
                self.tty.close()
            else:
                self._out(f"完成 {self.done}/{self.total}  通过 {self.passed}  失败 {self.failed + self.errors}  "
                          f"跳过 {self.skipped}  用时 {elapsed}\n")
        self.detach()


def _choose_mode(config: pytest.Config) -> tuple[str | None, Any]:
    setting = os.environ.get("QA_PROGRESS", "").strip().lower()
    if setting in ("0", "off", "false", "no"):
        return None, None
    if setting != "plain":
        reporter = config.pluginmanager.get_plugin("terminalreporter")
        f = getattr(getattr(reporter, "_tw", None), "_file", None)
        try:
            is_tty = bool(f is not None and f.isatty())
        except (ValueError, OSError):
            is_tty = False
        if is_tty:
            try:
                return "tty", open("/dev/tty", "w", encoding="utf-8")
            except OSError:
                pass
    return "plain", None


_STATE: dict[str, Progress] = {}


@pytest.hookimpl(trylast=True)
def pytest_collection_finish(session: pytest.Session) -> None:
    if session.config.option.collectonly or getattr(session.config.option, "help", False):
        return
    mode, tty = _choose_mode(session.config)
    if mode is None:
        return
    p = Progress(session.config, mode)
    p.tty = tty
    _STATE["p"] = p
    p.start(len(session.items))


def pytest_runtest_logstart(nodeid: str, location: Any) -> None:
    p = _STATE.get("p")
    if p:
        p.current = nodeid.split("tests/", 1)[-1]
        p.refresh()


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    p = _STATE.get("p")
    if not p:
        return
    if report.when == "call":
        if report.passed:
            p.passed += 1
        elif report.failed:
            p.failed += 1
            p.failure(report.nodeid, "call", report.duration)
        elif report.skipped:
            p.skipped += 1
    elif report.when == "setup":
        if report.failed:
            p.errors += 1
            p.failure(report.nodeid, "setup", report.duration)
        elif report.skipped:
            p.skipped += 1
    elif report.when == "teardown" and report.failed:
        p.errors += 1
        p.failure(report.nodeid, "teardown", report.duration)


def pytest_runtest_logfinish(nodeid: str, location: Any) -> None:
    p = _STATE.get("p")
    if p:
        p.finished_one()


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    p = _STATE.pop("p", None)
    if p:
        p.finish()


def pytest_unconfigure(config: pytest.Config) -> None:
    p = _STATE.pop("p", None)
    if p:  # 被中断等异常路径：恢复光标与输出
        p.finish()
