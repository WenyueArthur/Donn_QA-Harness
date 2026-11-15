"""稳定标识生成。ID 生成后不变，不以标题作为唯一身份。"""

from __future__ import annotations

import secrets

from qa_flow.canonical import sha256_obj


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6).upper()}"


def stable_id(prefix: str, *parts: object) -> str:
    return f"{prefix}-{sha256_obj([str(p) for p in parts])[:12].upper()}"
