"""测试报告、准入与准出判定（纯函数）。"""

from __future__ import annotations

from typing import Any

from qa_flow.domain.execution import first_by_tc, latest_by_tc

OUTCOME_KEYS = ("pass", "fail", "error", "blocked", "not_run", "cancelled", "quarantined")


def test_report(scope_tcs: list[str], attempts: list[dict[str, Any]], quarantined: set[str],
                coverage: dict[str, Any], defects: list[dict[str, Any]],
                blocked: dict[str, str] | None = None) -> dict[str, Any]:
    """从冻结范围、全部尝试与缺陷快照计算。未执行、隔离、取消不得从分母消失。

    blocked：已发起执行却被阻断、没有产生尝试的用例（→ 动作 ID），计为 blocked，与从未发起的 not_run 分开。
    """
    blocked = blocked or {}
    in_scope = [a for a in attempts if a["tc_ref"] in scope_tcs]
    first = first_by_tc(in_scope)
    latest = latest_by_tc(in_scope)

    def bucket(src: dict[str, dict[str, Any]]) -> dict[str, Any]:
        counts = {k: 0 for k in OUTCOME_KEYS}
        rows = []
        for tc in scope_tcs:
            if tc in quarantined:
                st = "quarantined"
            elif tc in src:
                st = src[tc]["outcome"]
            elif tc in blocked:
                st = "blocked"
            else:
                st = "not_run"
            counts[st] += 1
            row = {"tc": tc, "status": st, "attempt_ref": src[tc]["id"] if tc in src else None}
            if st == "blocked":
                row["action_ref"] = blocked[tc]
            rows.append(row)
        return {"denominator": len(scope_tcs), "counts": counts, "rows": rows}

    lat = bucket(latest)
    return {
        "denominator": len(scope_tcs),
        "first_run": bucket(first),
        "latest": lat,
        "unexecuted": [r["tc"] for r in lat["rows"] if r["status"] in ("not_run", "quarantined", "blocked",
                                                                         "cancelled")],
        "coverage": coverage,
        "defect_refs": [d["id"] for d in defects],
        "attempt_total": len(in_scope),
    }


def evaluate_criteria(criteria: list[dict[str, Any]]) -> str:
    """规则结果：任一 fail → fail；否则任一 indeterminate → indeterminate；全部 pass → pass。"""
    results = [c["result"] for c in criteria]
    if not results:
        return "indeterminate"
    if "fail" in results:
        return "fail"
    if "indeterminate" in results:
        return "indeterminate"
    return "pass"


def entry_criteria(*, plan_ok: bool, design_ok: bool, qc_ok: bool, expectations_confirmed: bool,
                   env_ready: bool, self_test_provided: bool | str, smoke: dict[str, Any],
                   scope_consistent: bool) -> list[dict[str, Any]]:
    def c(cid: str, label: str, ok: bool | None | str, reason: str) -> dict[str, Any]:
        if ok == "not_required":
            return {"id": cid, "label": label, "result": "not_applicable", "reason": "当前冻结策略不要求该项"}
        result = "indeterminate" if ok is None else ("pass" if ok else "fail")
        return {"id": cid, "label": label, "result": result, "reason": "" if ok else reason}

    smoke_missing = [t for t, s in smoke.items() if s is None]
    smoke_failed = [t for t, s in smoke.items() if s is not None and s != "pass"]
    smoke_ok: bool | None
    if smoke_failed:
        smoke_ok = False
    elif smoke_missing or not smoke:
        smoke_ok = None
    else:
        smoke_ok = True
    smoke_reason = "；".join(
        ([f"冒烟用例未执行：{', '.join(smoke_missing)}"] if smoke_missing else [])
        + ([f"冒烟用例未通过：{', '.join(smoke_failed)}"] if smoke_failed else []))
    return [
        c("plan", "测试计划已批准", plan_ok, "测试计划尚未获批准"),
        c("design", "用例设计已完成且追溯通过", design_ok, "用例设计未完成或追溯不通过"),
        c("case_qc", "用例质检已完成", qc_ok, "用例质检未完成"),
        c("expectations", "关键业务预期已确认", expectations_confirmed, "关键业务预期尚未获责任人确认"),
        c("environment", "环境与数据已实测就绪", env_ready, "环境与数据尚未实测就绪"),
        c("self_test", "研发自测材料已提供", self_test_provided, "缺少研发自测材料"),
        c("smoke", "实际冒烟通过", smoke_ok, smoke_reason or "冒烟结果缺失"),
        c("scope", "提测范围与计划一致", scope_consistent, "提测范围与计划不一致"),
    ]


def exit_criteria(*, required_all_pass: bool | None, regression_ok: bool, evidence_ok: bool,
                  context_ok: bool, open_blocking_defects: list[str], defect_snapshot_fresh: bool,
                  unknown_actions: list[str], rollback_ok: bool | None, accepted_risks: list[str]
                  ) -> list[dict[str, Any]]:
    def c(cid: str, label: str, ok: bool | None | str, reason: str, hard: bool = True) -> dict[str, Any]:
        if ok == "not_required":
            return {"id": cid, "label": label, "result": "not_applicable", "hard": hard,
                    "reason": "当前冻结策略不要求该项"}
        result = "indeterminate" if ok is None else ("pass" if ok else "fail")
        return {"id": cid, "label": label, "result": result, "hard": hard, "reason": "" if ok else reason}

    defects_ok: bool | None
    if not defect_snapshot_fresh:
        defects_ok = None
    else:
        unresolved = [d for d in open_blocking_defects if d not in accepted_risks]
        defects_ok = not unresolved
    return [
        c("required_cases", "必测范围全部有有效通过结果", required_all_pass, "必测用例存在失败、未执行或缺少有效结果"),
        c("regression", "必回归全部实际通过", regression_ok, "必回归存在未执行、隔离或失败"),
        c("registered_evidence", "结果证据已登记且可读取", evidence_ok, "存在缺失或无效证据"),
        c("matching_context", "证据上下文与当前构建一致", context_ok, "证据上下文与当前构建不一致"),
        c("defects", "阻断缺陷已关闭或有有效风险接受", defects_ok,
          "缺陷快照已过期，需要刷新缺陷状态" if not defect_snapshot_fresh else "存在未处理的阻断缺陷",
          hard=False),
        c("actions", "无结果未知的外部操作", not unknown_actions, f"存在结果未知的操作：{', '.join(unknown_actions)}"),
        c("rollback", "回滚演练结果有效", rollback_ok, "回滚演练未执行或未通过"),
    ]
