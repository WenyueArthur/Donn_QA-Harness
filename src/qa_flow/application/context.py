"""应用上下文与命令执行（事务边界、幂等与审计）。"""

from __future__ import annotations

import getpass
import json
from importlib import resources
from pathlib import Path
from typing import Any, Callable

from qa_flow import clock, faults
from qa_flow.application import configuration
from qa_flow.canonical import sha256_bytes, sha256_obj
from qa_flow.errors import QAError
from qa_flow.ids import new_id
from qa_flow.storage.store import Tx, Workspace


class Ctx:
    def __init__(self, ws: Workspace):
        self.ws = ws
        self.config = configuration.load(ws)
        self.config_sha = configuration.config_sha(self.config)
        self.profile = self.config.get("profile")
        self.project_id = self.config["project"]["id"]
        self.project_name = self.config["project"]["name"]
        self.policy = configuration.policy(self.config)
        self.budgets = self.config.get("budgets") or {}
        self.config_problem = configuration.applied_rules_problem(ws, self.config)

    @property
    def is_lab(self) -> bool:
        return self.profile == "lab"

    @property
    def rules_sha(self) -> str:
        rules = self.config.get("rules") or {}
        if rules.get("kind") == "builtin_lab":
            data = resources.files("qa_flow.resources.lab").joinpath("rules.json").read_bytes()
            return sha256_bytes(data)
        if rules.get("kind") == "legacy_bridge":
            mp = (self.ws.config_dir / rules.get("manifest", "")).resolve()
            try:
                return sha256_bytes(mp.read_bytes())
            except OSError:
                return "unavailable"
        return "unavailable"

    def rules(self) -> dict[str, Any]:
        rules = self.config.get("rules") or {}
        if rules.get("kind") == "builtin_lab":
            return json.loads(resources.files("qa_flow.resources.lab").joinpath("rules.json").read_text("utf-8"))
        return {"id": "legacy-bridge", "stages": {}}

    def rule_ref(self) -> str:
        rules = self.config.get("rules") or {}
        return f"{rules.get('kind')}:{self.rules_sha[:16]}"

    @property
    def user_role(self) -> str:
        return (self.config.get("identity") or {}).get("current_role", "tester")

    @property
    def actor(self) -> str:
        try:
            return f"os:{getpass.getuser()}"
        except Exception:  # noqa: BLE001
            return "os:unknown"

    def role_label(self, role: str) -> str:
        return ((self.config.get("identity") or {}).get("role_map") or {}).get(role, role)

    def environment_of(self, target_id: str) -> str:
        t = configuration.target(self.config, target_id)
        if t is None:
            raise QAError("UNKNOWN_TARGET", f"目标未登记: {target_id}", related_refs=[target_id])
        return t["environment"]

    def platform(self):
        a = configuration.adapter_by_kind(self.config, "issue")
        if a is None:
            raise QAError("CAPABILITY_UNAVAILABLE", "未配置缺陷平台连接器")
        if a.get("impl") == "fake_platform":
            if not self.is_lab:
                raise QAError("CONFIG_INVALID", "实验平台桩只能用于 lab 配置")
            from qa_flow.adapters.fake_platform import FakePlatform

            return FakePlatform(self.ws.lab_dir)
        raise QAError("CAPABILITY_UNAVAILABLE", f"缺陷平台适配器 {a.get('impl')} 尚未接入")

    def platform_manifest(self) -> dict[str, Any]:
        a = configuration.adapter_by_kind(self.config, "issue")
        if a is None:
            raise QAError("CAPABILITY_UNAVAILABLE", "未配置缺陷平台连接器")
        return a

    def model(self):
        m = self.config.get("model") or {}
        if m.get("provider") == "lab_stub" and self.is_lab:
            from qa_flow.adapters.model_stub import ModelStub

            return ModelStub(self.ws.lab_dir)
        raise QAError("CAPABILITY_UNAVAILABLE", "未配置可用的模型提供方；不发送模型请求。"
                      "可由 Agent 通过 qa-flow Skill 生成内容并经 api artifact submit 提交")


def open_ctx(root: Path) -> Ctx:
    ws = Workspace(root)
    if not ws.exists():
        raise QAError("WORKSPACE_INVALID", f"工作区不存在或不完整: {root}")
    return Ctx(ws)


def execute(ctx: Ctx, name: str, payload: dict[str, Any], handler: Callable[[Tx], dict[str, Any]],
            command_id: str | None = None) -> dict[str, Any]:
    """在一个短事务内执行写命令。

    同命令 ID、同载荷返回已提交结果；同 ID 不同载荷直接拒绝。
    失败时整体回滚（committed=false），并在独立短事务中记录不含原文的拒绝审计。
    """
    cid = command_id or new_id("CMD")
    req_sha = sha256_obj({"name": name, "payload": payload})
    if ctx.config_problem and name != "admin.setup":
        err = QAError("CONFIG_INVALID", ctx.config_problem, path="rules")
        _audit_rejection(ctx, cid, name, req_sha, err)
        raise err
    try:
        with ctx.ws.transaction(cid) as tx:
            faults.crash_point("after_begin")
            faults.crash_point(f"after_begin:{name}")
            row = tx.command_lookup(cid)
            if row is not None:
                if row["request_sha256"] != req_sha:
                    raise QAError("COMMAND_ID_REUSED", "同一命令 ID 已用于不同请求内容", path="command_id",
                                  related_refs=[cid])
                resp = json.loads(row["response"])
                resp["replayed"] = True
                return resp
            result = handler(tx)
            warnings = result.pop("_warnings", []) if isinstance(result, dict) else []
            faults.crash_point(f"before_commit:{name}")
            resp = {"command_id": cid, "status": "ok", "committed": True, "revision": tx.last_seq(),
                    "result": result, "errors": [], "warnings": warnings}
            tx.command_record(cid, name, req_sha, resp)
        faults.crash_point(f"after_commit:{name}")
        return resp
    except QAError as err:
        if err.code != "COMMAND_ID_REUSED":
            _audit_rejection(ctx, cid, name, req_sha, err)
        raise


def _audit_rejection(ctx: Ctx, cid: str, name: str, req_sha: str, err: QAError) -> None:
    try:
        with ctx.ws.transaction(None) as tx:
            tx.event("audit", "command_rejected", {"command_id": cid, "name": name, "request_sha256": req_sha,
                                                   "code": err.code, "path": err.path,
                                                   "related_refs": err.related_refs})
    except Exception:  # noqa: BLE001 — 审计失败不能掩盖原错误
        pass


def now() -> str:
    return clock.now_iso()
