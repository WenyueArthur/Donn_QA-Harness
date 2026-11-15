"""通过公共日常命令驱动合成实验（demo）到指定进度。只使用 CLI，不调用产品内部函数。

驱动步骤与新人操作一致：review 答复自己负责的问题、转交需他人决定的问题、resume 继续。
实验配置中的审批桩会对“关键预期确认”类问题给出 simulated 答复；其他产品问题保持待处理。
"""

from __future__ import annotations

from typing import Any, Callable

from .cli import QA, Result

STAGES = ["req_gate", "tech_gate", "req_analysis", "test_plan", "case_design", "case_qc", "env_ready",
          "test_entry", "test_execution", "test_exit", "release_verify", "retrospective"]


def view(qa: QA, *extra: str) -> dict[str, Any]:
    return qa.ok("status", *extra).result


def stage_map(v: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["stage"]: s for s in v["stages"]}


def answer_mine(qa: QA, option: str | None = None) -> list[Result]:
    """答复当前归自己负责的全部事项；默认选择每项的第一个选项。"""
    out = []
    for c in view(qa)["reviews"]["mine"]:
        opt = option or (c["options"][0]["key"] if c.get("options") else "A")
        out.append(qa.ok("review", "--answer", f"{c['handle']}={opt}"))
    return out


def transfer_others(qa: QA, only_kind: str | None = "关键业务预期确认") -> list[Result]:
    out = []
    for c in view(qa)["reviews"]["others"]:
        if only_kind and c.get("kind") != only_kind:
            continue
        if c.get("status") not in (None, "open", "pending"):
            continue
        out.append(qa.ok("review", "--transfer", f"{c['handle']}={c.get('responsible_role', 'product')}"))
    return out


def drive(qa: QA, until: Callable[[dict[str, Any]], bool] | None = None, max_rounds: int = 25,
          start_demo: bool = True) -> dict[str, Any]:
    """驱动 demo。until(view) 返回 True 时停止；否则直到连续两轮修订号不变。"""
    if start_demo and not qa.workspaces():
        qa.ok("demo")
    last_rev, stable = None, 0
    v = view(qa)
    for _ in range(max_rounds):
        if until and until(v):
            return v
        answer_mine(qa)
        transfer_others(qa)
        r = qa.run("resume")
        assert r.rc in (0, 3, 5), "resume 出现非预期退出码\n" + r.describe()
        v = view(qa)
        rev = qa.run("status").json.get("revision")
        stable = stable + 1 if rev == last_rev else 0
        last_rev = rev
        if stable >= 2:
            break
    return v


def until_stage(stage: str, statuses: tuple[str, ...] = ("completed",)) -> Callable[[dict[str, Any]], bool]:
    return lambda v: stage_map(v)[stage]["status"] in statuses
