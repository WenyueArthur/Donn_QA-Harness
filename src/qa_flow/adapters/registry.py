"""适配器实现的唯一注册入口。模块定位使用包导入与显式配置，不扫描个人安装目录。"""

from __future__ import annotations

from typing import Any

IMPLS: dict[str, dict[str, Any]] = {
    "fake_platform": {"module": "qa_flow.adapters.fake_platform", "environments": ["lab"],
                      "capabilities": ["query", "create", "update", "attach", "link", "transition", "events",
                                       "idempotency", "doc_create", "doc_read", "doc_update_etag"]},
    "local_subprocess": {"module": "qa_flow.adapters.local_executor", "environments": ["lab", "test"],
                         "capabilities": ["run_trusted"]},
    "lab_model_stub": {"module": "qa_flow.adapters.model_stub", "environments": ["lab"],
                       "capabilities": ["generate"]},
}


def known_impls() -> set[str]:
    return set(IMPLS)


def declared_capabilities(impl: str) -> list[str]:
    return list(IMPLS.get(impl, {}).get("capabilities", []))
