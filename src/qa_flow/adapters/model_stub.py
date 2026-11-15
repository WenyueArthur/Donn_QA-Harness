"""记录型实验模型桩。

只用于合成实验：按固定模板为退款实验生成草稿内容，并把实际收到的（已脱敏）输入写入账本，
以便核对模型只收到允许字段与占位符。真实模型提供方未配置时，系统不发送模型请求。
生成内容只是提案，必须经过结构、引用和语义校验；失败输出保持草稿。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from qa_flow import clock
from qa_flow.canonical import sha256_obj

MODEL = {"provider": "lab_stub", "model": "lab-deterministic-stub", "revision": "unknown"}
TEMPLATE_VERSION = "lab-demo-templates/1"

R1 = "R1：退款金额必须为整数分，且 0 < 金额 ≤ 已支付金额 − 已退款金额；否则返回 400，账务不变。"
R2 = "R2：只有退款操作员（refund_operator）可以提交退款；只读角色返回 403，且不产生任何副作用。"
R3 = "R3：同一幂等键、同一请求内容重复提交时，返回同一笔退款；同一幂等键但内容不同时返回 409。"
R4 = "R4：退款余额更新与退款记录必须原子提交；任一步失败时二者均不改变。"
R5 = "R5：支付渠道的退款成功回调可能重复送达，同一回调只记账一次。"
R6 = "R6：并发提交不同幂等键的退款时，总退款金额不得超过已支付金额。"
Q_STATE = "部分退款后，订单状态显示为“部分退款”还是保持“已支付”，文档未说明。"
Q_NOTIFY = "退款到账通知由哪个系统发送，产品尚未确定。"


class ModelStub:
    def __init__(self, lab_dir: Path):
        self.ledger = Path(lab_dir) / "model_calls.jsonl"

    def generate(self, task: str, sanitized: dict[str, Any], refs: dict[str, str]) -> dict[str, Any]:
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(self.ledger, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": clock.now_iso(), "task": task, "received": sanitized}, ensure_ascii=False) + "\n")
        fn = GENERATORS[task]
        payload = fn(refs)
        prompt_text = json.dumps({"task": task, "input": sanitized}, ensure_ascii=False, sort_keys=True)
        return {
            "payload": payload,
            "provenance": {
                "model": dict(MODEL),
                "prompt": {"template_ref": f"{TEMPLATE_VERSION}#{task}", "template_sha256": sha256_obj([TEMPLATE_VERSION, task]),
                           "expanded_sha256": sha256_obj(prompt_text)},
                "params": {"temperature": 0, "seed": 0},
                "input_sha256": sha256_obj(sanitized),
                "imported": False,
                "replayable": False,
                "note": "相同输入不能保证真实模型输出相同；实验桩输出仅用于合成实验",
            },
        }


def _c(src: str, quote: str) -> dict[str, str]:
    return {"source_ref": src, "quote": quote}


def quality_review(refs: dict[str, str]) -> dict[str, Any]:
    s = refs["requirement"]
    return {
        "rule_ref": refs["rule_ref"],
        "source_refs": [s],
        "findings": [
            {"id": "F1", "text": "部分退款后的订单状态未定义，状态展示无法判定预期。", "severity": "major",
             "citations": [_c(s, Q_STATE)]},
            {"id": "F2", "text": "到账通知的发送方未确定，通知相关验证不可执行。", "severity": "minor",
             "citations": [_c(s, Q_NOTIFY)]},
            {"id": "F3", "text": "回调去重的判定依据未在需求中写明。", "severity": "minor", "citations": [_c(s, R5)]},
        ],
        "testability": {"verdict": "warn", "notes": "核心金额与幂等规则可测；状态展示与通知待确认。"},
        "clarifications": [
            {"question_ref": "R5-dedupe-key", "question": "同一回调只记账一次，以什么字段判定“同一回调”？",
             "basis": [_c(s, R5)], "responsible_role": "tester", "affected_scope": "回调去重相关测试点与用例",
             "blocks": ["req_analysis"],
             "options": [{"key": "A", "label": "以回调 event_id 判定（技术方案已写明）"},
                         {"key": "B", "label": "以 refund_id 判定"}]},
            {"question_ref": "order-state-partial", "question": "部分退款后订单状态应显示什么？",
             "basis": [_c(s, Q_STATE)], "responsible_role": "product", "affected_scope": "订单状态展示测试点",
             "blocks": ["TP-STATE"],
             "options": [{"key": "A", "label": "显示“部分退款”"}, {"key": "B", "label": "保持“已支付”"}]},
            {"question_ref": "notify-owner", "question": "退款到账通知由哪个系统发送？",
             "basis": [_c(s, Q_NOTIFY)], "responsible_role": "product", "affected_scope": "通知测试（当前排除）",
             "blocks": [], "options": [{"key": "A", "label": "退款服务发送"}, {"key": "B", "label": "消息中心发送"}]},
        ],
    }


def technical_review(refs: dict[str, str]) -> dict[str, Any]:
    t = refs["tech"]
    api_q = "POST /refunds：请求字段 order_id、amount、idempotency_key、role；响应 refund_id。"
    return {
        "applicability": "provided",
        "source_refs": [t],
        "models": {
            "flow": {"triggered": True, "entities": [
                {"id": "FL1", "name": "退款事务", "citations": [_c(t, "退款服务在同一数据库事务中更新 orders.refunded 并插入 refunds 记录。")]}]},
            "interface": {"triggered": True, "entities": [
                {"id": "API1", "name": "POST /refunds", "citations": [_c(t, api_q)], "db_field_refs": ["DB1", "DB2"]}]},
            "data": {"triggered": True, "entities": [
                {"id": "DB1", "name": "orders.refunded", "citations": [_c(t, "orders 表：paid、refunded 字段，单位为分。")]},
                {"id": "DB2", "name": "refunds.idempotency_key", "citations": [_c(t, "refunds 表：idempotency_key 唯一约束。")]},
                {"id": "DB3", "name": "ledger.event_id", "citations": [_c(t, "ledger 表：按回调 event_id 去重记账。")]}]},
            "state": {"triggered": True, "entities": [
                {"id": "ST1", "name": "paid→refunded", "citations": [_c(t, "订单状态：paid → refunded（全额退款后）。")]}],
                "questions": ["部分退款状态未定义"]},
            "rule": {"triggered": True, "entities": [
                {"id": "RU1", "name": "剩余可退金额", "citations": [_c(t, "剩余可退金额 = paid − refunded，超额请求返回 400。")]}]},
            "dependency": {"triggered": True, "entities": [
                {"id": "DP1", "name": "支付渠道回调", "citations": [_c(t, "支付渠道回调接口，回调事件携带 event_id。")]}]},
        },
        "checks": [
            {"id": "TC-MODEL", "kind": "model", "result": "pass", "detail": "六类模型均有实体与证据"},
            {"id": "TC-CONTRACT", "kind": "contract", "result": "pass", "detail": "接口字段与数据字段可追溯"},
            {"id": "TC-ROLLBACK", "kind": "rollback", "result": "pass", "detail": "方案说明了代码与约束回滚及核对",
             "citations": [_c(t, "代码可回滚到上一构建；refunds 表新增唯一约束可回滚；回滚后核对订单退款金额与记录数。")]},
            {"id": "TC-VERIFY", "kind": "verifiability", "result": "pass", "detail": "账务可通过独立读取核对"},
        ],
        "questions": [{"id": "TQ1", "text": "部分退款后的状态取值待产品确认", "basis": "state 模型"}],
        "test_seeds": [{"id": "SEED1", "text": "同幂等键重复请求", "model_refs": ["API1", "DB2"]},
                       {"id": "SEED2", "text": "回调重复送达", "model_refs": ["DB3", "DP1"]}],
    }


def test_analysis(refs: dict[str, str]) -> dict[str, Any]:
    s = refs["requirement"]
    state_blocked = refs.get("state_answered") != "yes"
    return {
        "requirements": [
            {"id": "REQ-1", "text": "金额校验", "citations": [_c(s, R1)]},
            {"id": "REQ-2", "text": "角色权限", "citations": [_c(s, R2)]},
            {"id": "REQ-3", "text": "幂等", "citations": [_c(s, R3)]},
            {"id": "REQ-4", "text": "原子提交与回调去重", "citations": [_c(s, R4), _c(s, R5)]},
            {"id": "REQ-5", "text": "并发总额", "citations": [_c(s, R6)]},
            {"id": "REQ-6", "text": "部分退款状态", "citations": [_c(s, Q_STATE)]},
        ],
        "test_points": [
            {"id": "TP-AMOUNT", "req_ids": ["REQ-1"], "text": "金额为 0、超额时返回 400 且账务不变", "readiness": "ready", "risk": "P0"},
            {"id": "TP-ROLE", "req_ids": ["REQ-2"], "text": "只读角色返回 403 且无副作用", "readiness": "ready", "risk": "P0"},
            {"id": "TP-IDEM", "req_ids": ["REQ-3"], "text": "同键同载荷返回同一退款；同键不同载荷 409", "readiness": "ready", "risk": "P0"},
            {"id": "TP-ATOMIC", "req_ids": ["REQ-4"], "text": "失败时余额与记录均不变；回调只记账一次", "readiness": "ready", "risk": "P0"},
            {"id": "TP-CONC", "req_ids": ["REQ-5"], "text": "并发不同键总退款不超过已支付", "readiness": "ready", "risk": "P0"},
            {"id": "TP-STATE", "req_ids": ["REQ-6"], "text": "部分退款后的订单状态展示",
             "readiness": "blocked" if state_blocked else "ready", "risk": "P2",
             **({"block_reason": "部分退款状态待产品确认", "block_refs": ["order-state-partial"]} if state_blocked else {})},
        ],
        "regression": [
            {"id": "REG-1", "scope": "幂等重复请求", "tp_ids": ["TP-IDEM"], "reason": "改动涉及幂等处理"},
            {"id": "REG-2", "scope": "角色权限", "tp_ids": ["TP-ROLE"], "reason": "退款入口相邻权限校验"},
        ],
        "strategy": {
            "types": [{"type": "接口功能", "applicable": True, "reason": "规则均可通过接口与账务观测验证"},
                      {"type": "并发", "applicable": True, "reason": "R6 明确并发约束"},
                      {"type": "兼容性", "applicable": False, "reason": "合成实验只有单一客户端与单一构建线"}],
            "layers": ["接口", "数据"],
            "exclusions": [{"item": "到账通知", "reason": "发送方未确定，待产品确认"}],
            "automation_candidates": [{"tp_id": "TP-IDEM", "feasibility": "high"},
                                      {"tp_id": "TP-CONC", "feasibility": "medium", "reason": "需要并发屏障控制先后"}],
            "repeat_conditions": [{"tp_id": "TP-CONC", "condition": "相同构建、数据初态与随机种子下重复 3 次",
                                   "reason": "并发结果可能受调度影响"}],
        },
        "entry_criteria": ["测试计划已批准", "关键预期已确认", "环境实测就绪", "冒烟 SMOKE-1/SMOKE-2 实际通过"],
        "exit_criteria": ["必测用例全部有效通过", "必回归 C1/C2 实际通过", "阻断缺陷关闭或有有效处置"],
    }


def test_plan(refs: dict[str, str]) -> dict[str, Any]:
    day = refs.get("plan_date", "2026-11-16")
    return {
        "tasks": [
            {"id": "T1", "title": "用例设计与评审", "owner": "lab-tester", "date": day,
             "estimate_hours": {"low": 1, "likely": 2, "high": 3}},
            {"id": "T2", "title": "执行与缺陷跟踪", "owner": "lab-tester", "date": day,
             "estimate_hours": {"low": 2, "likely": 3, "high": 5}, "depends_on": ["T1"]},
            {"id": "T3", "title": "回归与准出", "owner": "lab-tester", "date": day,
             "estimate_hours": {"low": 0.5, "likely": 1, "high": 2}, "depends_on": ["T2"]},
        ],
        "estimate_method": "按测试点复杂度三点估算（合成实验示例，不是实测）",
        "estimate_range_hours": {"low": 3.5, "likely": 6, "high": 10},
        "calendar": {"timezone": "Asia/Shanghai", "workdays": [day]},
        "capacity": [{"person": "lab-tester", "date": day, "hours": 8}],
        "dependencies": [{"id": "D1", "description": "实验环境可用", "status": "met"}],
        "milestones": [{"name": "准出", "date": day}],
    }


def case_design(refs: dict[str, str]) -> dict[str, Any]:
    def case(cid, tps, title, kind, steps, expected, text, key, prio="P0"):
        return {"id": cid, "tp_ids": tps, "title": title, "kind": kind, "steps": steps, "expected": expected,
                "expected_text": text, "key_expectation": key, "automation": "auto", "priority": prio}

    cases = [
        case("SMOKE-1", ["TP-AMOUNT"], "正常部分退款", "smoke", ["对 O100 以 K1 退款 3000"],
             {"status": 201, "refunded_after": 3000, "records_after": 1}, "返回 201，已退款 3000，记录 1 条", False),
        case("SMOKE-2", ["TP-AMOUNT"], "退款 0 元被拒", "smoke", ["对 O100 以 K3 退款 0"],
             {"status": 400, "refunded_after": 0, "records_after": 0}, "返回 400，账务不变", False),
        case("C1", ["TP-IDEM"], "同键同载荷重复请求", "regression", ["K1 退款 3000", "K1 再次退款 3000"],
             {"first_status": 201, "second_status": 200, "same_refund_id": True, "refunded_after": 3000,
              "records_after": 1}, "第二次返回 200 且退款 ID 相同，已退款仍为 3000", True),
        case("C2", ["TP-ROLE"], "只读角色退款被拒", "permission", ["read_only 角色对 O100 退款 1000"],
             {"status": 403, "refunded_after": 0, "records_after": 0}, "返回 403，无副作用", True),
        case("C3", ["TP-IDEM", "TP-AMOUNT"], "幂等冲突与非法金额序列", "exception",
             ["K1 3000", "K1 3000", "K1 4000", "K2 8000", "K3 0"],
             {"statuses": [201, 200, 409, 400, 400], "refunded_trace": [3000, 3000, 3000, 3000, 3000],
              "records_after": 1}, "依次 201/200/409/400/400，已退款始终 3000", True),
        case("C4", ["TP-CONC"], "并发不同键退款", "concurrency", ["P1、P2 同时各退款 7000"],
             {"success_count": 1, "refunded_after": 7000, "records_after": 1}, "仅一个成功，总额 7000，记录 1 条", True),
        case("C5", ["TP-ATOMIC"], "故障事务与重复回调", "exception", ["注入失败退款 2000", "成功退款 2000", "重复回调两次"],
             {"failed_status": 500, "refunded_after_failure": 0, "records_after_failure": 0, "ok_status": 201,
              "callback_statuses": [201, 200], "ledger_entries": 1}, "故障时余额与记录不变；回调只记一次", True),
    ]
    return {"analysis_ref": refs["analysis"], "cases": cases}


def case_quality_review(refs: dict[str, str]) -> dict[str, Any]:
    return {
        "design_ref": refs["design"],
        "levels": {"L1": {"result": "pass", "detail": "结构字段齐全"},
                   "L2": {"result": "pass", "detail": "追溯：全部就绪测试点均被覆盖"},
                   "L3": {"result": "pass", "detail": "预期可判定，步骤可执行"}},
        "findings": [],
    }


def knowledge_candidate(refs: dict[str, str]) -> dict[str, Any]:
    return {
        "material_type": "precedent",
        "claims": [{"text": "退款幂等：同键同载荷重复请求曾导致重复扣减，需以回归 C1 覆盖。", "claim_type": "fact",
                    "evidence_refs": [refs["defect_evidence"]], "environment": "lab"}],
        "sources": [refs["defect"]],
        "conflicts": [],
        "confirmation_method": "human",
        "risk": "low",
    }


GENERATORS = {
    "quality_review": quality_review,
    "technical_review": technical_review,
    "test_analysis": test_analysis,
    "test_plan": test_plan,
    "case_design": case_design,
    "case_quality_review": case_quality_review,
    "knowledge_candidate": knowledge_candidate,
}
