"""稳定错误码与退出码。

退出码：0 命令成功；2 参数/不支持契约；3 前置、选择或授权阻断；
4 证据或产物无效；5 外部结果未知；6 依赖/I/O 不可用；7 内部一致性错误。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

EXIT_OK = 0
EXIT_ARGUMENT = 2
EXIT_BLOCKED = 3
EXIT_INVALID = 4
EXIT_UNKNOWN = 5
EXIT_UNAVAILABLE = 6
EXIT_INTERNAL = 7

# 错误码 → (退出码, 面向人的安全恢复建议)
ERROR_CODES: dict[str, tuple[int, str]] = {
    "INVALID_ARGUMENT": (EXIT_ARGUMENT, "检查命令参数后重新执行。"),
    "UNSUPPORTED_CONTRACT": (EXIT_ARGUMENT, "使用当前支持的契约名称；可执行 api --help 查看。"),
    "COMMAND_ID_REUSED": (EXIT_ARGUMENT, "同一命令 ID 只能对应同一请求内容；改用新的命令 ID。"),
    "SELECTION_REQUIRED": (EXIT_BLOCKED, "按业务名称明确选择要操作的需求或项目。"),
    "REVIEW_REQUIRED": (EXIT_BLOCKED, "执行 review 处理待确认事项后再继续。"),
    "AUTHORIZATION_REQUIRED": (EXIT_BLOCKED, "由有权限的责任人确认该操作范围后再继续。"),
    "AUTHORIZATION_INVALID": (EXIT_BLOCKED, "原授权不覆盖当前内容、目标或环境；需要针对新计划重新确认。"),
    "PROTECTED_RESOURCE": (EXIT_BLOCKED, "原始资料受保护，只能读取；修正应走设计修订或另行沟通原文作者。"),
    "UNKNOWN_TARGET": (EXIT_BLOCKED, "目标未在已核验配置中登记；请管理员通过 admin setup 补充。"),
    "CREDENTIAL_MISSING": (EXIT_BLOCKED, "缺少该目标的凭证引用；请管理员配置，不要在终端粘贴密钥。"),
    "CAPABILITY_UNAVAILABLE": (EXIT_BLOCKED, "该能力尚未配置或核验；请管理员处理，已有只读结果仍可查看。"),
    "UNSUPPORTED_CAPABILITY": (EXIT_BLOCKED, "适配器不支持该操作；不会降级为其他写法。"),
    "REVISION_CONFLICT": (EXIT_BLOCKED, "对象已被其他操作更新；刷新状态后再决定是否继续。"),
    "PRECONDITION_FAILED": (EXIT_BLOCKED, "先完成所列前置条件。"),
    "SOURCE_STALE": (EXIT_BLOCKED, "来源快照已过期；刷新来源后重新核验。"),
    "SOURCE_UNREADABLE": (EXIT_BLOCKED, "来源读取受阻；检查访问权限或连接器后刷新来源。"),
    "SCHEMA_INVALID": (EXIT_INVALID, "按错误路径修正产物内容后重新提交。"),
    "REFERENCE_INVALID": (EXIT_INVALID, "修正引用的对象 ID 后重新提交。"),
    "CITATION_NOT_FOUND": (EXIT_INVALID, "引文必须逐字出现在所引用的来源快照中。"),
    "EVIDENCE_INVALID": (EXIT_INVALID, "证据缺失、摘要不符或来源未核验；重新采集真实证据。"),
    "ORIGIN_MISMATCH": (EXIT_INVALID, "证据来源等级与目标不符；模拟、实验与测试证据不能冒充生产。"),
    "VALIDATION_FAILED": (EXIT_INVALID, "按问题列表修正业务输入后重新提交。"),
    "OUTCOME_UNKNOWN": (EXIT_UNKNOWN, "外部结果未知；执行 resume 或 api action reconcile 先核对，不要重发。"),
    "DEPENDENCY_UNAVAILABLE": (EXIT_UNAVAILABLE, "所需组件不可用；请管理员执行 admin doctor 检查。"),
    "RULES_UNAVAILABLE": (EXIT_UNAVAILABLE, "冻结规则不可用或摘要不符；相关阶段不可判定，请管理员核对规则配置。"),
    "CONFIG_INVALID": (EXIT_UNAVAILABLE, "配置无效；请管理员通过 admin setup 预览并修正。"),
    "WORKSPACE_INVALID": (EXIT_UNAVAILABLE, "工作区不可用；执行 demo 或请管理员建立项目工作区。"),
    "IO_ERROR": (EXIT_UNAVAILABLE, "检查文件路径与权限。"),
    "LIMIT_EXCEEDED": (EXIT_UNAVAILABLE, "已达到配置的资源限额，结果不完整；请管理员调整限额或缩小范围。"),
    "NOT_FOUND": (EXIT_BLOCKED, "对象不存在；执行 status 查看当前可用内容。"),
    "INTERNAL_INCONSISTENCY": (EXIT_INTERNAL, "内部一致性检查失败；保留现场并请开发人员排查。"),
}


@dataclass
class QAError(Exception):
    code: str
    message: str
    path: str = ""
    related_refs: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.code not in ERROR_CODES:
            raise ValueError(f"未登记的错误码: {self.code}")
        super().__init__(self.message)

    @property
    def exit_code(self) -> int:
        return ERROR_CODES[self.code][0]

    @property
    def recovery(self) -> str:
        return ERROR_CODES[self.code][1]

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "related_refs": list(self.related_refs),
            "recovery": self.recovery,
        }
        if self.details:
            out["details"] = self.details
        return out


class ValidationIssues(QAError):
    """携带多条问题的校验失败。"""

    def __init__(self, code: str, issues: list[dict[str, Any]], message: str | None = None):
        first = issues[0] if issues else {}
        super().__init__(
            code=code,
            message=message or first.get("message", "校验失败"),
            path=first.get("path", ""),
            related_refs=sorted({r for i in issues for r in i.get("related_refs", [])}),
            details={"issues": issues},
        )


def issue(path: str, message: str, *refs: str, code: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"path": path, "message": message, "related_refs": list(refs)}
    if code:
        out["code"] = code
    return out
