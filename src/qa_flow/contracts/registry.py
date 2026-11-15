"""单一现行契约注册表。

每个契约只有一个现行名称（schema = "qa.<kind>"），不新增带版本号的替代字段。
这里只声明 payload 形状；跨对象关系由 domain 中的确定性校验器检查。
"""

from __future__ import annotations

from typing import Any

from qa_flow.contracts.shape import opt

E = lambda *values: ("enum", list(values))  # noqa: E731

CITATION = {"source_ref": "str", "quote": "str", "location": "str?"}
EST = {"low": "num", "likely": "num", "high": "num"}
SUBJECT = {"ref": "str", "sha256": "str"}

STAGE_STATUS = ("pending", "running", "blocked", "completed", "failed", "stale", "waived")
VERDICTS = ("pass", "fail", "indeterminate", "not_applicable")
ATTEMPT_OUTCOMES = ("pass", "fail", "error", "blocked", "not_run", "cancelled")
VERIFICATION_METHODS = ("simulated", "local_attested", "external_verified")
SOURCE_STATES = ("available", "unreadable", "missing", "stale")
ACTION_STATES = ("planned", "awaiting_authorization", "ready", "running", "succeeded", "failed",
                 "unknown", "reconciling", "cancelled")
ORIGINS = ("simulated", "local_lab", "test", "production", "upload", "imported")

MODEL_KINDS = ("flow", "interface", "data", "state", "rule", "dependency")

CONTRACTS: dict[str, dict[str, Any]] = {
    "source_revision": {
        "uri": "str", "kind": E("local_file", "remote_doc", "inline"),
        "read_status": E(*SOURCE_STATES), "content_ref": "str?", "content_sha256": "str?",
        "fetched_at": "str", "freshness_policy": {"max_age_hours": "num?"},
        "title": "str?", "read_scope": "str?", "omitted": "list?", "failure_reason": "str?",
        "extraction_method": "str?",
    },
    "claim": {
        "text": "str", "claim_type": E("fact", "inference", "assumption"),
        "evidence_refs": [CITATION], "scope": "str",
        "review_state": E("pending", "verified", "accepted", "rejected"),
        "subject": "str?", "value": "any?", "runtime_evidence_refs": "list?",
    },
    "clarification": {
        "question": "str", "basis": [CITATION], "aliases": "list?", "blocks": "list?",
        "responsible_role": "str", "options": "list?", "affected_scope": "str?",
        "question_ref": "str?",
    },
    "decision": {
        "subject_refs": [SUBJECT], "plan_sha256": "str?", "role": "str", "actor": "str",
        "verification": E(*VERIFICATION_METHODS),
        "scope": {"environment": "str", "target_ids": "list?", "action_kinds": "list?", "story_id": "str?"},
        "result": E("approve", "reject", "answer", "waive", "accept_risk", "confirm", "defer", "transfer"),
        "answer": "any?", "reason": "str?", "expires_at": "str?", "evidence_ref": "str?",
        "proposer": "str?",
    },
    "quality_review": {
        "rule_ref": "str", "source_refs": ["str"],
        "findings": [{"id": "str", "text": "str", "severity": E("blocker", "major", "minor", "info"),
                      "citations": [CITATION]}],
        "testability": {"verdict": E("pass", "warn", "fail"), "notes": "str?"},
        "clarifications": "list?", "legacy_report": "dict?", "review_note": "str?",
    },
    "technical_review": {
        "applicability": E("provided", "not_provided", "unreadable"),
        "source_refs": "list",
        "models": "dict",
        "checks": [{"id": "str", "kind": E("model", "contract", "rollback", "verifiability"),
                    "result": E("pass", "fail", "indeterminate"), "detail": "str", "citations": "list?"}],
        "questions": "list?", "test_seeds": "list?", "legacy_report": "dict?",
    },
    "test_analysis": {
        "requirements": [{"id": "str", "text": "str", "citations": [CITATION]}],
        "test_points": [{"id": "str", "req_ids": ["str"], "text": "str", "readiness": E("ready", "blocked"),
                         "risk": E("P0", "P1", "P2"), "block_reason": "str?", "block_refs": "list?"}],
        "regression": [{"id": "str", "scope": "str", "tp_ids": ["str"], "reason": "str"}],
        "strategy": {
            "types": [{"type": "str", "applicable": "bool", "reason": "str"}],
            "layers": "list",
            "exclusions": [{"item": "str", "reason": "str"}],
            "automation_candidates": [{"tp_id": "str", "feasibility": E("high", "medium", "low"),
                                       "reason": "str?"}],
            "repeat_conditions": [{"tp_id": "str", "condition": "str", "reason": "str"}],
        },
        "entry_criteria": "list", "exit_criteria": "list", "legacy_report": "dict?",
    },
    "test_plan": {
        "tasks": [{"id": "str", "title": "str", "owner": "str", "date": "str", "estimate_hours": EST,
                   "depends_on": "list?"}],
        "estimate_method": "str", "estimate_range_hours": EST,
        "calendar": {"timezone": "str", "workdays": "list?", "holidays": "list?"},
        "capacity": [{"person": "str", "date": "str", "hours": "num"}],
        "dependencies": [{"id": "str", "description": "str", "status": E("met", "pending", "blocked")}],
        "milestones": [{"name": "str", "date": "str"}],
        "approval_ref": "str?",
    },
    "effort_entry": {"entry_id": "str", "person": "str", "hours": "num", "start": "str?", "end": "str?",
                     "task_ref": "str?"},
    "change_set": {
        "repo": "str", "base_commit": "str", "head_commit": "str", "diff_ref": "str",
        "search_scope": {"paths": "list", "patterns": "list", "complete": "bool"},
        "unknown_scope": "list", "impact_refs": "list",
        "callers": [{"symbol": "str", "file": "str", "reg_ids": "list"}],
        "claims": "dict?",
    },
    "flow_model": {
        "flows": [{"id": "str", "from": "str", "to": "str", "kind": E("design", "runtime"),
                   "evidence_refs": "list", "verification": E("verified", "unverified", "indeterminate"),
                   "deployment_ref": "str?"}],
    },
    "case_design": {
        "analysis_ref": "str",
        "cases": [{"id": "str", "tp_ids": ["str"], "title": "str",
                   "kind": E("smoke", "functional", "regression", "exception", "permission", "concurrency"),
                   "steps": ["str"], "expected": "dict", "expected_text": "str",
                   "key_expectation": "bool", "automation": E("auto", "manual"),
                   "priority": E("P0", "P1", "P2")}],
        "revision_note": "str?",
    },
    "case_quality_review": {
        "design_ref": "str",
        "levels": {"L1": {"result": E(*VERDICTS), "detail": "str"},
                   "L2": {"result": E(*VERDICTS), "detail": "str"},
                   "L3": {"result": E(*VERDICTS), "detail": "str"}},
        "findings": "list", "legacy_report": "dict?",
    },
    "dataset": {"name": "str", "version": "str", "kind": E("synthetic", "masked"), "preparation": "str",
                "isolation": "str", "cleanup": "str"},
    "environment_check": {
        "target_id": "str", "build": {"expected": "str", "observed": "str?"}, "dataset_refs": "list",
        "checks": [{"id": "str", "kind": E("version", "account", "flag", "data", "observability", "cleanup"),
                    "method_provided": "bool", "observed": E("pass", "fail", "not_run"),
                    "evidence_ref": "str?"}],
        "observations": "list", "cleanup_plan": "str", "cleanup_result": opt(E("pass", "fail", "not_run")),
    },
    "script_revision": {
        "tc_refs": ["str"], "repo": "str", "commit": "str", "path": "str", "file_sha256": "str",
        "dependencies": "list", "content_ref": "str", "review_ref": "str?", "ci_ref": "str?",
        "trusted": "bool",
    },
    "execution_run": {
        "plan_ref": "str", "worker_receipt": "str", "origin": E(*ORIGINS), "target": "str", "build": "str",
        "dataset": "str", "attempt_refs": "list", "started_at": "str", "finished_at": "str?",
        "purpose": E("smoke", "execution", "regression", "retest", "stability", "rollback_drill"),
        "ci_ref": "str?",
    },
    "case_attempt": {
        "tc_ref": "str", "script_ref": "str?", "attempt_no": "int", "context_sha256": "str",
        "assertions": [{"key": "str", "expected": "any", "actual": "any", "result": E("pass", "fail")}],
        "observations_ref": "str", "outcome": E(*ATTEMPT_OUTCOMES), "evidence_refs": "list",
        "run_ref": "str", "operator": "str?", "build": "str", "purpose": "str",
    },
    "entry_report": {
        "scope_ref": "str", "policy_ref": "str", "criterion_results": "list", "missing": "list",
        "decision_refs": "list", "verdict": E(*VERDICTS),
    },
    "exit_report": {
        "scope_ref": "str", "policy_ref": "str", "criterion_results": "list", "missing": "list",
        "defect_snapshot": "dict", "decision_refs": "list", "verdict": E(*VERDICTS),
        "external_release_decision": "str?",
    },
    "test_report": {"scope_ref": "str", "counts": "dict", "first_run": "dict", "latest": "dict",
                    "coverage": "dict", "unexecuted": "list", "defect_refs": "list"},
    "release_check": {
        "deployment": "str", "actual_build": "str", "environment": "str",
        "window": {"start": "str", "end": "str"},
        "probes": [{"id": "str", "result": E("pass", "fail", "not_run"), "evidence_ref": "str"}],
        "metric_samples": [{"name": "str", "ts": "str", "value": "num", "unit": "str", "evidence_ref": "str"}],
        "thresholds": [{"name": "str", "op": E("<=", ">=", "<", ">"), "value": "num", "unit": "str"}],
        "gaps": "list?", "verdict": E(*VERDICTS),
    },
    "rollback_plan": {
        "covers": {k: {"plan": "str", "reversible": "bool"} for k in
                   ("code", "config", "database", "data_restore", "verification")},
        "irreversible_changes": "list",
    },
    "rollback_drill": {
        "plan_ref": "str", "environment": "str", "from_build": "str", "to_build": "str",
        "restore_point": "str",
        "before_after_checks": [{"name": "str", "before": "any", "after": "any", "expected": "any",
                                 "result": E("pass", "fail")}],
        "authorization_ref": "str", "outcome": E("pass", "fail", "indeterminate"),
        "unverified_parts": "list",
    },
    "investigation": {
        "defect_ref": "str?", "build": "str", "subject_ids": "list",
        "facts": [{"text": "str", "evidence_refs": ["str"]}],
        "inferences": [{"text": "str", "basis": "str"}], "pending": "list",
    },
    "escape": {"defect_ref": "str", "should_have_caught_stage": "str", "proposed_cause": "str",
               "missing_refs": "list", "change_refs": "list", "verification_refs": "list",
               "confirmed_decision": "str?"},
    "improvement": {"escape_ref": "str", "measure": "str", "change_refs": "list", "verification_refs": "list"},
    "knowledge_candidate": {
        "material_type": E("rule", "precedent", "root_cause", "conflict", "open_question", "glossary"),
        "claims": [{"text": "str", "claim_type": E("fact", "inference"), "evidence_refs": "list",
                    "environment": "str?"}],
        "sources": "list", "conflicts": "list", "confirmation_method": E("human", "policy"),
        "risk": E("low", "high"),
    },
    "metric_input": {"metric_name": E("ai_adoption", "human_modification", "online_defect_ratio",
                                      "confirmed_escapes", "elapsed_time", "effort"), "data": "dict"},
    "action_plan": {"kind": "str", "target_id": "str", "environment": "str", "request": "dict",
                    "input_refs": [SUBJECT]},
    "task_request": {"task_kind": E("start", "resume", "review", "export", "demo"), "project_ref": "str",
                     "source_refs": "list", "requested_scope": "str", "operation_id": "str"},
    "run_provenance": {
        "model": {"provider": "str", "model": "str", "revision": "str"},
        "prompt": {"template_ref": "str", "template_sha256": "str", "expanded_sha256": "str"},
        "params": "dict", "input_sha256": "str", "rules_sha256": "str", "knowledge_sha256": "str",
        "code_sha256": "str", "tools": "list", "imported": "bool", "replayable": "bool",
        "config_sha256": "str?", "knowledge_ref": "str?", "skill_ref": "str?", "skill_sha256": "str?",
    },
    "sanitized_input": {"allowed_fields": ["str"], "fields": "dict", "placeholder_count": "int",
                        "review": {"state": E("done", "pending"), "by": "str?"}, "blocked_reasons": "list"},
    "usability_report": {"participants": "list", "task_definitions": "list", "observations": "list",
                         "completion_counts": "dict", "help_events": "int", "recovery_results": "dict",
                         "timing": "dict", "limitations": "str", "verdict": E("pass", "fail", "indeterminate")},
    "regression_plan": {"items": [{"reg_id": "str", "tp_ids": "list", "tc_ids": "list", "required": "bool"}],
                        "analysis_ref": "str", "design_ref": "str", "change_ref": "str?"},
    "stability_assessment": {"tc_ref": "str", "attempt_refs": "list", "classification":
                             E("stable_pass", "stable_fail", "flaky", "incomparable", "insufficient"),
                             "first_outcome": "str", "contexts": "list"},
    "archive_record": {"report_ref": "str", "target_id": "str", "action_ref": "str", "state": "str",
                       "remote_object_id": "str?", "report_sha256": "str"},
    "knowledge_snapshot": {"entries": "list", "parent_ref": "str?"},
    "validation_report": {"subject_ref": "str", "validator": "str", "structure": "dict", "relations": "dict",
                          "semantic": "dict", "runtime": "dict"},
    "stage_evaluation": {"stage": "str", "verdict": E(*VERDICTS), "status": E(*STAGE_STATUS),
                         "reasons": "list", "inputs": "list", "decisions": "list",
                         "rules_sha256": "str", "config_sha256": "str"},
    "parity_report": {"validator": "str", "samples": "list", "differences": "list"},
    "capability_report": {"items": "list"},
    "self_test_report": {"summary": "str", "source_refs": ["str"], "provided_by": "str", "build": "str"},
    "deployment": {"target_id": "str", "build": "str", "commit": "str", "deployment_id": "str",
                   "deployed_at": "str", "origin": E(*ORIGINS)},
}


def payload_spec(kind: str) -> dict[str, Any]:
    from qa_flow.errors import QAError

    spec = CONTRACTS.get(kind)
    if spec is None:
        raise QAError("UNSUPPORTED_CONTRACT", f"不支持的契约: {kind}", path="kind")
    return spec


# 只能由系统内部生成、不能通过 artifact submit 提交的契约
SYSTEM_ONLY = {
    "execution_run", "case_attempt", "entry_report", "exit_report", "test_report", "stage_evaluation",
    "action_plan", "task_request", "archive_record", "knowledge_snapshot", "validation_report",
    "stability_assessment", "capability_report", "parity_report", "usability_report", "decision",
    "clarification", "source_revision", "regression_plan", "rollback_drill", "deployment",
    "script_revision", "defect",
}
