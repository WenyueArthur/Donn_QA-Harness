"""适配器公共约定：能力先声明、先探测；不支持的操作在发送前拒绝。"""

from __future__ import annotations

from typing import Any

from qa_flow.errors import QAError


class OutcomeUnknown(Exception):
    """操作结果可能已经发生（超时、断连、响应丢失）。不能据此判定远端未执行。"""


class RemoteFailed(Exception):
    """远端明确返回失败（已核验未执行或执行失败）。"""


class RateLimited(RemoteFailed):
    pass


def require_capability(manifest: dict[str, Any], capability: str) -> None:
    if capability not in (manifest.get("capabilities") or []):
        raise QAError("UNSUPPORTED_CAPABILITY",
                      f"适配器 {manifest.get('id')} 不支持 {capability}；不会降级为其他写法",
                      related_refs=[manifest.get("id", "")])
