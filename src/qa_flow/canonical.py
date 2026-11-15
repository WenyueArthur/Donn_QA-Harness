"""规范字节与摘要。

业务 JSON：UTF-8、排序键、紧凑分隔、不允许 NaN/Infinity。
原文与二进制证据按原始字节计算 SHA-256。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_bytes(obj: Any) -> bytes:
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_obj(obj: Any) -> str:
    return sha256_bytes(canonical_bytes(obj))


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def loads_strict(text: str | bytes) -> Any:
    """解析 JSON，拒绝 NaN/Infinity。"""

    def _reject(value: str) -> Any:
        raise ValueError(f"不允许的数值: {value}")

    return json.loads(text, parse_constant=_reject)


def dumps_pretty(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False, allow_nan=False)
