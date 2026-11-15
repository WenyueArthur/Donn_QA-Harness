"""环境就绪、提测准入、断言、尝试、稳定性与回归（纯函数）。"""

from __future__ import annotations

from typing import Any

from qa_flow.canonical import sha256_obj

WORKER_ORIGINS = {"local_lab", "test"}


def assert_observation(expected: dict[str, Any], observation: dict[str, Any]) -> list[dict[str, Any]]:
    """按冻结用例的预期逐项断言。预期来自用例，绝不来自实际响应。"""
    out = []
    for key, exp in sorted(expected.items()):
        actual = observation.get(key, "<缺失>")
        out.append({"key": key, "expected": exp, "actual": actual, "result": "pass" if actual == exp else "fail"})
    return out


def attempt_outcome(assertions: list[dict[str, Any]], *, exit_code: int | None, timed_out: bool,
                    truncated: bool, cancelled: bool, observation_ok: bool) -> str:
    """退出码 0 不足以证明通过；超时、截断与取消不产生假通过。"""
    if cancelled:
        return "cancelled"
    if timed_out or truncated or not observation_ok:
        return "error"
    if exit_code not in (0, None):
        return "error"
    if not assertions:
        return "error"
    return "pass" if all(a["result"] == "pass" for a in assertions) else "fail"


def context_sha(build: str, script_sha: str, dataset: str, config_sha: str, seed: str, clock_ref: str) -> str:
    return sha256_obj({"build": build, "script": script_sha, "dataset": dataset, "config": config_sha,
                       "seed": seed, "clock": clock_ref})


def stability(attempts: list[dict[str, Any]], minimum_attempts: int) -> dict[str, Any]:
    """首轮结果不可被重试覆盖；不同上下文不能当同条件波动。"""
    ordered = sorted(attempts, key=lambda a: (a["created_at"], a["attempt_no"]))
    if not ordered:
        return {"classification": "insufficient", "first_outcome": "not_run", "contexts": []}
    contexts = sorted({a["context_sha256"] for a in ordered})
    first = ordered[0]["outcome"]
    if len(contexts) > 1:
        cls = "incomparable"
    elif len(ordered) < minimum_attempts:
        cls = "insufficient"
    else:
        outcomes = {a["outcome"] for a in ordered}
        if outcomes == {"pass"}:
            cls = "stable_pass"
        elif "pass" in outcomes and ("fail" in outcomes or "error" in outcomes):
            cls = "flaky"
        else:
            cls = "stable_fail"
    by_ctx = [{"context_sha256": c, "builds": sorted({a["build"] for a in ordered if a["context_sha256"] == c}),
               "outcomes": [a["outcome"] for a in ordered if a["context_sha256"] == c]} for c in contexts]
    return {"classification": cls, "first_outcome": first, "contexts": by_ctx}


def environment_ready(check: dict[str, Any], evidence_origins: dict[str, str]) -> dict[str, Any]:
    """准备方法已提供不等于就绪；就绪需每项实测通过并有工作进程证据。"""
    items = []
    for c in check["checks"]:
        origin = evidence_origins.get(c.get("evidence_ref") or "")
        measured = c["observed"] == "pass" and origin in WORKER_ORIGINS
        items.append({"id": c["id"], "kind": c["kind"], "method_provided": c["method_provided"],
                      "measured": c["observed"] if origin in WORKER_ORIGINS else "not_run",
                      "ready": measured})
    build_ok = check["build"].get("observed") == check["build"]["expected"]
    ready = bool(items) and all(i["ready"] for i in items) and build_ok
    return {"ready": ready, "build_match": build_ok, "items": items,
            "material_status": "provided" if any(i["method_provided"] for i in items) else "missing",
            "runtime_status": "verified" if ready else "not_verified"}


def latest_by_tc(attempts: list[dict[str, Any]], purpose: str | None = None,
                 build: str | None = None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for a in sorted(attempts, key=lambda x: (x["created_at"], x["attempt_no"])):
        if purpose and a["purpose"] != purpose:
            continue
        if build and a["build"] != build:
            continue
        out[a["tc_ref"]] = a
    return out


def first_by_tc(attempts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for a in sorted(attempts, key=lambda x: (x["created_at"], x["attempt_no"])):
        out.setdefault(a["tc_ref"], a)
    return out


def regression_items(analysis: dict[str, Any], design: dict[str, Any],
                     required_tc: list[str]) -> list[dict[str, Any]]:
    """从影响项选择用例：REG → TP → TC。策略要求的必回归用例全部标 required。"""
    items = []
    for reg in analysis["regression"]:
        tcs = sorted({c["id"] for c in design["cases"] if set(c["tp_ids"]) & set(reg["tp_ids"])})
        items.append({"reg_id": reg["id"], "tp_ids": reg["tp_ids"], "tc_ids": tcs,
                      "required": any(t in required_tc for t in tcs) or reg.get("required", True)})
    listed = {t for i in items for t in i["tc_ids"]}
    for t in required_tc:
        if t not in listed:
            items.append({"reg_id": f"REG-POLICY-{t}", "tp_ids": [], "tc_ids": [t], "required": True})
    return items


def regression_gate(items: list[dict[str, Any]], latest: dict[str, dict[str, Any]],
                    quarantined: set[str]) -> dict[str, Any]:
    """必回归未执行或被隔离均不能计为通过，且不得从分母删除。"""
    rows = []
    for it in items:
        for tc in it["tc_ids"]:
            a = latest.get(tc)
            if tc in quarantined:
                status = "quarantined"
            elif a is None:
                status = "not_run"
            else:
                status = a["outcome"]
            rows.append({"reg_id": it["reg_id"], "tc": tc, "required": it["required"], "status": status,
                         "attempt_ref": a["id"] if a else None})
    required = [r for r in rows if r["required"]]
    passed = [r for r in required if r["status"] == "pass"]
    return {"rows": rows, "required_total": len(required), "required_passed": len(passed),
            "ok": bool(required) and len(passed) == len(required)}
