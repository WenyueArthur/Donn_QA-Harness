"""运行来源指纹：代码、Skill 与知识快照的摘要。

摘要只证明字节一致，不证明语义正确；取不到时如实标 unknown，不补造。
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent
_SKILL_CANDIDATES = (
    _PKG_ROOT.parents[1] / "skills" / "qa-flow" / "SKILL.md",  # 源码目录运行
    _PKG_ROOT / "resources" / "skill" / "SKILL.md",            # 打包附带（若存在）
)


@lru_cache(maxsize=1)
def code_sha256() -> str:
    """qa_flow 包内全部 .py 与资源文件按相对路径排序后的摘要。"""
    h = hashlib.sha256()
    files = sorted(p for p in _PKG_ROOT.rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    if not files:
        return "unknown"
    for p in files:
        h.update(p.relative_to(_PKG_ROOT).as_posix().encode("utf-8") + b"\0")
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


@lru_cache(maxsize=1)
def skill() -> dict[str, str]:
    for p in _SKILL_CANDIDATES:
        if p.is_file():
            return {"skill_ref": "qa-flow/SKILL.md", "skill_sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
    return {"skill_ref": "unknown", "skill_sha256": "unknown"}
