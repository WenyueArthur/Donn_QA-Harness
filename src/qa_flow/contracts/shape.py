"""最小形状校验器（标准库实现）。

规格语法：{"字段": "类型"}，类型为 str/int/num/bool/list/dict/any，后缀 "?" 表示条件可选；
列表元素规格写为 ["类型或子规格"]；嵌套对象写为子字典；枚举写为 ("enum", [...])。
形状校验只检查结构；引用、摘要、上下文和业务关系由确定性校验器另行检查。
"""

from __future__ import annotations

from typing import Any

from qa_flow.errors import issue

_TYPES = {
    "str": str,
    "int": int,
    "num": (int, float),
    "bool": bool,
    "list": list,
    "dict": dict,
}


def check(value: Any, spec: Any, path: str = "payload") -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    _check(value, spec, path, problems)
    return problems


def _check(value: Any, spec: Any, path: str, out: list[dict[str, Any]]) -> None:
    if isinstance(spec, str):
        base = spec.rstrip("?")
        if base == "any":
            return
        py = _TYPES[base]
        if base in ("int", "num") and isinstance(value, bool):
            out.append(issue(path, f"类型应为 {base}"))
            return
        if not isinstance(value, py):
            out.append(issue(path, f"类型应为 {base}"))
            return
        if base == "str" and not value.strip():
            out.append(issue(path, "不能为空字符串"))
        return
    if isinstance(spec, tuple) and spec and spec[0] == "enum":
        if value not in spec[1]:
            out.append(issue(path, f"取值必须是 {list(spec[1])} 之一"))
        return
    if isinstance(spec, list):
        if not isinstance(value, list):
            out.append(issue(path, "类型应为 list"))
            return
        for i, item in enumerate(value):
            _check(item, spec[0], f"{path}[{i}]", out)
        return
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            out.append(issue(path, "类型应为 dict"))
            return
        for key, sub in spec.items():
            optional = _optional(sub)
            if key not in value or value[key] is None:
                if not optional:
                    out.append(issue(f"{path}.{key}", "缺少必需字段"))
                continue
            _check(value[key], _strip_optional(sub), f"{path}.{key}", out)
        return
    raise TypeError(f"无效规格: {spec!r}")


def _optional(spec: Any) -> bool:
    if isinstance(spec, str):
        return spec.endswith("?")
    if isinstance(spec, tuple) and len(spec) == 3:
        return bool(spec[2])
    if isinstance(spec, dict):
        return bool(spec.get("__optional__"))
    return False


def _strip_optional(spec: Any) -> Any:
    if isinstance(spec, str):
        return spec.rstrip("?")
    if isinstance(spec, tuple) and len(spec) == 3:
        return spec[:2]
    if isinstance(spec, dict) and "__optional__" in spec:
        return {k: v for k, v in spec.items() if k != "__optional__"}
    return spec


def opt(spec: Any) -> Any:
    """把子规格标记为条件可选。"""
    if isinstance(spec, str):
        return spec if spec.endswith("?") else spec + "?"
    if isinstance(spec, tuple):
        return (spec[0], spec[1], True)
    if isinstance(spec, dict):
        return {**spec, "__optional__": True}
    raise TypeError("列表规格请在外层用字符串 'list?' 表示可选")
