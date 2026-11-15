"""模型输入最小化与脱敏。

先分类并提取允许字段，再做标识符替换和已知敏感模式检测。
正则匹配不能识别所有敏感数据；未支持格式、未知敏感内容或未完成审阅时阻止发送。
映射表只保存在私有证据中，不进入提示词、普通日志、指标或公开导出。
"""

from __future__ import annotations

import re
from typing import Any

PATTERNS = [
    ("PHONE", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("IDCARD", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("TOKEN", re.compile(r"\b(?=[A-Z0-9_]*_)[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|APIKEY|API_KEY)[A-Z0-9_]*\b")),
    ("KEY", re.compile(r"\b(?:sk|pk|ghp|glpat|xox[abp])[-_][A-Za-z0-9_-]{8,}\b")),
    ("BEARER", re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{12,}")),
]

INJECTION_HINTS = [
    "忽略以上", "忽略之前", "无视规则", "你现在是", "已获授权", "自动批准", "直接批准", "关闭校验",
    "ignore previous", "ignore all previous", "disregard", "you are now", "pre-authorized", "approve automatically",
]

SUPPORTED_TEXT_TYPES = {"text/markdown", "text/plain", "application/json"}


def redact_text(text: str, mapping: dict[str, str], counters: dict[str, int]) -> str:
    def repl_factory(kind: str):
        def repl(m: re.Match[str]) -> str:
            raw = m.group(0)
            for ph, orig in mapping.items():
                if orig == raw:
                    return ph
            counters[kind] = counters.get(kind, 0) + 1
            ph = f"⟨{kind}_{counters[kind]}⟩"
            mapping[ph] = raw
            return ph
        return repl

    out = text
    for kind, pat in PATTERNS:
        out = pat.sub(repl_factory(kind), out)
    return out


def injection_suspected(text: str) -> list[str]:
    low = text.lower()
    return [h for h in INJECTION_HINTS if h.lower() in low]


def build_sanitized_input(fields: dict[str, Any], allowed_fields: list[str],
                          attachments: list[dict[str, Any]] | None = None,
                          review_done: bool = False) -> dict[str, Any]:
    mapping: dict[str, str] = {}
    counters: dict[str, int] = {}
    kept: dict[str, Any] = {}
    blocked: list[str] = []
    for name in allowed_fields:
        if name not in fields:
            continue
        value = fields[name]
        if isinstance(value, str):
            kept[name] = redact_text(value, mapping, counters)
        elif isinstance(value, (int, float, bool)) or value is None:
            kept[name] = value
        else:
            blocked.append(f"字段 {name} 不是受支持的文本或数值格式")
    dropped = sorted(set(fields) - set(allowed_fields))
    for a in attachments or []:
        if a.get("media_type") not in SUPPORTED_TEXT_TYPES:
            blocked.append(f"附件 {a.get('name', '?')} 格式未支持，需人工处理")
    injections = sorted({h for v in kept.values() if isinstance(v, str) for h in injection_suspected(v)})
    if not review_done:
        blocked.append("尚未完成人工审阅")
    return {
        "payload": {
            "allowed_fields": allowed_fields,
            "fields": kept,
            "placeholder_count": len(mapping),
            "review": {"state": "done" if review_done else "pending"},
            "blocked_reasons": blocked,
        },
        "mapping": mapping,
        "dropped_fields": dropped,
        "injection_hints": injections,
    }


def contains_any(text: str, secrets: list[str]) -> list[str]:
    return [s for s in secrets if s and s in text]


def scrub_for_display(text: str) -> str:
    """用于错误详情与普通日志的脱敏展示：替换已知敏感模式。"""
    return redact_text(text, {}, {})
