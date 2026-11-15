"""能力展示：planned / implemented / offline_verified / external_verified 分列显示。

这些是不同事实列，不互相推导；只填技能名或通过结构检查，不能提高真实接入完成度。
验收结果只能来自导入的独立验收记录；没有记录时验证数量为 0。
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

# 需求 → 实现所在模块（实现事实，指向源码位置）
IMPLEMENTED: dict[str, list[str]] = {
    "FLOW-01": ["domain/quality.py:validate_quality_review", "application/stages.py:_req_gate"],
    "FLOW-02": ["application/review.py:propose_clarification", "application/review.py:answer"],
    "FLOW-03": ["domain/planning.py:evaluate_plan", "application/stages.py:_test_plan"],
    "FLOW-04": ["domain/quality.py:validate_test_analysis", "domain/quality.py:handoff_seed"],
    "FLOW-05": ["domain/quality.py:validate_technical_review"],
    "FLOW-06": ["application/stages.py:_tech_gate"],
    "FLOW-07": ["domain/evidence.py:flow_verification"],
    "FLOW-08": ["domain/quality.py:validate_change_set"],
    "FLOW-09": ["domain/planning.py:validate_strategy"],
    "FLOW-10": ["domain/cases.py:validate_case_design", "storage/store.py:_advance_lineage"],
    "FLOW-11": ["domain/cases.py:coverage", "domain/cases.py:traceability"],
    "FLOW-12": ["application/stages.py:_case_qc", "application/stages.py:expectations_status"],
    "FLOW-13": ["application/execution.py:_probe_record", "domain/execution.py:environment_ready"],
    "FLOW-14": ["application/stages.py:_test_entry", "domain/reports.py:entry_criteria"],
    "FLOW-15": ["application/execution.py:_execute_record", "domain/execution.py:assert_observation"],
    "FLOW-16": ["application/execution.py:register_script", "application/execution.py:script_review_valid"],
    "FLOW-17": ["domain/execution.py:stability"],
    "FLOW-18": ["domain/execution.py:regression_gate"],
    "FLOW-19": ["application/core.py:validate_payload(investigation)"],
    "FLOW-20": ["application/defects.py", "domain/defects.py:close_requirements"],
    "FLOW-21": ["application/defects.py:_attach_perform"],
    "FLOW-22": ["application/defects.py:sync", "domain/defects.py:apply_platform_event", "application/worker.py"],
    "FLOW-23": ["domain/defects.py:extended_retest_verdict"],
    "FLOW-24": ["application/core.py:import_source", "application/core.py:propagate"],
    "FLOW-25": ["domain/reports.py:test_report"],
    "FLOW-26": ["application/stages.py:_test_exit", "domain/reports.py:exit_criteria"],
    "FLOW-27": ["domain/release.py:evaluate_release_check"],
    "FLOW-28": ["domain/release.py:rollback_feasibility", "application/execution.py:_rollback_perform"],
    "FLOW-29": ["application/learning.py:record_escape", "application/learning.py:escape_report"],
    "FLOW-30": ["application/learning.py:propose_knowledge", "application/learning.py:apply_confirmed"],
    "AI-01": ["application/review.py:record_decision", "domain/review.py"],
    "AI-02": ["domain/evidence.py:check_citations", "domain/evidence.py:classify_claims"],
    "AI-03": ["domain/evidence.py:provenance_completeness", "adapters/model_stub.py"],
    "AI-04": ["security/sanitize.py"],
    "AI-05": ["domain/metrics.py:compare"],
    "AI-06": ["application/core.py:validate_payload", "contracts/shape.py"],
    "MET-01": ["domain/metrics.py:ai_adoption"],
    "MET-02": ["domain/metrics.py:human_modification"],
    "MET-03": ["domain/metrics.py:online_defect_ratio", "domain/metrics.py:confirmed_escapes"],
    "MET-04": ["domain/metrics.py:elapsed_time", "domain/metrics.py:effort"],
    "SYS-01": ["application/core.py:import_source", "application/core.py:source_state"],
    "SYS-02": ["application/core.py:effective_current", "application/core.py:propagate"],
    "SYS-03": ["storage/store.py", "application/context.py:execute"],
    "SYS-04": ["application/execution.py:_execute_record", "contracts/registry.py:SYSTEM_ONLY"],
    "SYS-05": ["application/broker.py"],
    "SYS-06": ["domain/review.py:authorization_applies", "application/broker.py:_authorization_still_valid"],
    "SYS-07": ["security/resources.py"],
    "SYS-08": ["adapters/registry.py", "adapters/base.py:require_capability", "adapters/legacy_rules.py:verify_manifest"],
    "SYS-09": ["storage/backup.py", "application/admin.py:expire_evidence"],
    "SYS-10": ["pyproject.toml", "application/admin.py:doctor"],
    "SYS-11": ["reporting/capabilities.py"],
    "SYS-12": ["adapters/legacy_rules.py", "application/admin.py:parity"],
    "UX-01": ["application/task_service.py", "cli.py"],
    "UX-02": ["application/task_service.py:demo"],
    "UX-03": ["application/admin.py:setup", "application/configuration.py"],
    "UX-04": ["application/task_service.py:resume"],
    "UX-05": ["application/review.py", "application/task_service.py:review"],
    "UX-06": ["errors.py", "cli.py:_render_error"],
    "UX-07": ["application/task_service.py:status", "application/task_service.py:export"],
    "UX-08": ["domain/usability.py"],
}

# 已知未实现或仅部分实现的范围（如实展示，不计入 implemented）
PARTIAL: dict[str, str] = {
    "SYS-12": "已实现旧规则冻结清单与只读桥接；逐块提取为新模块需在等价验收通过后进行",
    "FLOW-27": "已实现生产证据校验；尚无获准的生产只读连接器",
    "FLOW-15": "macOS 上以 sandbox-exec 隔离网络与凭证；文件系统只是部分隔离，其他平台无隔离；只运行已审可信脚本",
    "SYS-10": "已提供打包元数据；干净环境安装与多操作系统支持未验证",
}


def requirements() -> list[dict[str, Any]]:
    data = resources.files("qa_flow.resources").joinpath("requirements.json").read_text(encoding="utf-8")
    return json.loads(data)["requirements"]


def report(verification_file: Path | None = None) -> dict[str, Any]:
    """按注册的需求—场景对应关系计入验收结果。

    - 记录自称覆盖、但不在该需求注册场景中的结果不计入；
    - 失败、未知或未执行的结果逐条列出，同层存在失败时该层不算已验证。
    """
    reqs = requirements()
    registered = {r["id"]: set(r["test_ids"]) for r in reqs}
    results: dict[str, dict[str, str]] = {}
    layers: dict[str, dict[str, str]] = {}
    if verification_file and Path(verification_file).is_file():
        data = json.loads(Path(verification_file).read_text(encoding="utf-8"))
        for r in data.get("cases", []):
            for req in r.get("requirements", []):
                if r.get("id") not in registered.get(req, set()):
                    continue
                results.setdefault(req, {})[r["id"]] = r.get("result") or "not_run"
                layers.setdefault(req, {})[r["id"]] = "external" if r.get("layer") in ("external", "model") \
                    else "offline"
    items = []
    for r in reqs:
        res = results.get(r["id"], {})
        lay = layers.get(r["id"], {})
        v = {"offline": [], "external": []}
        bad = {"offline": [], "external": []}
        for cid, outcome in sorted(res.items()):
            (v if outcome == "pass" else bad)[lay[cid]].append(cid)
        items.append({
            "id": r["id"], "title": r["title"], "milestone": r["milestone"],
            "planned": True,
            "implemented": r["id"] in IMPLEMENTED,
            "implementation_refs": IMPLEMENTED.get(r["id"], []),
            "limitation": PARTIAL.get(r["id"]),
            "offline_verified": bool(v["offline"]) and not bad["offline"], "offline_evidence": v["offline"],
            "external_verified": bool(v["external"]) and not bad["external"], "external_evidence": v["external"],
            "not_passed": {cid: res[cid] for cid in bad["offline"] + bad["external"]},
            "test_ids": r["test_ids"],
        })
    totals = {"planned": len(items), "implemented": sum(i["implemented"] for i in items),
              "offline_verified": sum(i["offline_verified"] for i in items),
              "external_verified": sum(i["external_verified"] for i in items)}
    return {"items": items, "totals": totals,
            "note": "四列为不同事实，不是单一完成率；未导入独立验收记录时验证数量为 0"}
