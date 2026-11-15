"""用例设计、追溯与三种覆盖（纯函数）。"""

from __future__ import annotations

from typing import Any

from qa_flow.errors import issue


def validate_case_design(design: dict[str, Any], analysis: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    tp_ids = {tp["id"] for tp in analysis["test_points"]}
    ready = {tp["id"] for tp in analysis["test_points"] if tp["readiness"] == "ready"}
    seen = set()
    for i, c in enumerate(design["cases"]):
        if c["id"] in seen:
            out.append(issue(f"payload.cases[{i}].id", "用例 ID 重复"))
        seen.add(c["id"])
        for t in c["tp_ids"]:
            if t not in tp_ids:
                out.append(issue(f"payload.cases[{i}].tp_ids", f"用例引用的测试点 {t} 无效", t, code="REFERENCE_INVALID"))
            elif t not in ready:
                out.append(issue(f"payload.cases[{i}].tp_ids", f"测试点 {t} 受阻，不能进入可执行设计", t))
        if not c["expected"]:
            out.append(issue(f"payload.cases[{i}].expected", "预期必须可判定，不能为空"))
    return out


def traceability(design: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    tp_ids = {tp["id"] for tp in analysis["test_points"]}
    ready = {tp["id"] for tp in analysis["test_points"] if tp["readiness"] == "ready"}
    covered = set()
    invalid = []
    for c in design["cases"]:
        for t in c["tp_ids"]:
            if t in tp_ids:
                covered.add(t)
            else:
                invalid.append({"tc": c["id"], "tp": t})
    orphan = sorted(ready - covered)
    return {"ready_tps": sorted(ready), "covered_tps": sorted(covered & ready), "uncovered_tps": orphan,
            "invalid_refs": invalid, "ok": not orphan and not invalid}


def coverage(design: dict[str, Any], analysis: dict[str, Any], scripts: list[dict[str, Any]],
             attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """设计覆盖、脚本映射、执行覆盖分别计算，不互相代替。"""
    ready = sorted(tp["id"] for tp in analysis["test_points"] if tp["readiness"] == "ready")
    tc_ids = [c["id"] for c in design["cases"]]
    tr = traceability(design, analysis)
    mapped = sorted({t for s in scripts for t in s["tc_refs"] if t in tc_ids})
    executed = sorted({a["tc_ref"] for a in attempts if a["outcome"] in ("pass", "fail") and a["tc_ref"] in tc_ids})
    return {
        "design": {"numerator": len(tr["covered_tps"]), "denominator": len(ready), "unit": "测试点"},
        "script_mapping": {"numerator": len(mapped), "denominator": len(tc_ids), "unit": "用例",
                           "unmapped": [t for t in tc_ids if t not in mapped]},
        "execution": {"numerator": len(executed), "denominator": len(tc_ids), "unit": "用例",
                      "not_executed": [t for t in tc_ids if t not in executed]},
        "code": {"status": "not_measured", "value": None},
        "note": "三种覆盖分别计算，不能统称 100% 覆盖",
    }


def key_expectations(design: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in design["cases"] if c["key_expectation"]]
