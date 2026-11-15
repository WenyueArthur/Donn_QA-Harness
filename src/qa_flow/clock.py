"""可注入时钟。

正式运行使用系统 UTC 时间。只有测试进程显式设置 QA_FLOW_TEST_MODE=1 时，
QA_FLOW_TEST_CLOCK（ISO-8601 UTC）才生效，用于检验过期、提醒和窗口。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone


def test_mode() -> bool:
    return os.environ.get("QA_FLOW_TEST_MODE") == "1"


def now() -> datetime:
    if test_mode():
        fixed = os.environ.get("QA_FLOW_TEST_CLOCK")
        if fixed:
            return parse_ts(fixed)
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_iso() -> str:
    return to_iso(now())


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str) -> datetime:
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        raise ValueError(f"时间缺少时区: {value}")
    return dt.astimezone(timezone.utc)


def hours_between(start: str, end: str) -> float:
    return (parse_ts(end) - parse_ts(start)).total_seconds() / 3600.0


def plus_hours(ts: str, hours: float) -> str:
    return to_iso(parse_ts(ts) + timedelta(hours=hours))
