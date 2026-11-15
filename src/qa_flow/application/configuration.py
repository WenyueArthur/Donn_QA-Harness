"""九组配置的加载、校验、预览、探测与原子应用。

配置是受审业务输入，修改后使依赖结论过期；不得包含凭证明文。
配置引用路径相对显式配置目录解析，不相对当前 shell 目录或个人 HOME。
"""

from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path
from typing import Any

from qa_flow.canonical import sha256_obj
from qa_flow.errors import QAError
from qa_flow.storage.store import Workspace, atomic_write

CONFIG_FILE = "qa-flow.json"
GROUPS = ("workspace", "targets", "rules", "identity", "model", "adapters", "timing", "retention", "budgets")
ENVIRONMENTS = ("lab", "test", "staging", "production")
SECRET_KEYS = re.compile(r"(?i)(password|secret|token|api_?key|private_?key|cookie)")
SECRET_VALUES = re.compile(r"(?i)(sk-[A-Za-z0-9]{8,}|glpat-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|bearer\s+\S{12,})")


def lab_template() -> dict[str, Any]:
    text = resources.files("qa_flow.resources.lab").joinpath("profile.json").read_text(encoding="utf-8")
    return json.loads(text)


def load(ws: Workspace) -> dict[str, Any]:
    path = ws.config_dir / CONFIG_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise QAError("CONFIG_INVALID", "工作区缺少配置文件", path=str(path)) from exc
    except ValueError as exc:
        raise QAError("CONFIG_INVALID", "配置文件无法解析", path=str(path)) from exc


def config_sha(cfg: dict[str, Any]) -> str:
    return sha256_obj(cfg)


def validate(cfg: dict[str, Any], config_dir: Path) -> list[dict[str, Any]]:
    """静态校验：九组齐全、无明文凭证、lab 只指向本机资源、目标环境明确。"""
    problems: list[dict[str, Any]] = []

    def bad(path: str, msg: str) -> None:
        problems.append({"path": path, "message": msg})

    if cfg.get("schema") != "qa.project_profile":
        bad("schema", "配置 schema 必须为 qa.project_profile")
    profile = cfg.get("profile")
    if profile not in ("lab", "project"):
        bad("profile", "profile 只能是 lab 或 project")
    if not (cfg.get("project") or {}).get("name"):
        bad("project.name", "缺少项目业务名称")
    for g in GROUPS:
        if g not in cfg:
            bad(g, "缺少配置组")
    _scan_secrets(cfg, "", bad)
    ids = set()
    for i, t in enumerate(cfg.get("targets") or []):
        p = f"targets[{i}]"
        if t.get("id") in ids:
            bad(f"{p}.id", "目标 ID 重复")
        ids.add(t.get("id"))
        if t.get("environment") not in ENVIRONMENTS:
            bad(f"{p}.environment", "环境必须明确为 lab/test/staging/production")
        if not t.get("allowed_operations"):
            bad(f"{p}.allowed_operations", "必须列出允许操作")
        if profile == "lab":
            if t.get("environment") != "lab":
                bad(f"{p}.environment", "合成配置只能指向 lab 资源")
            for a in t.get("allowed_addresses") or []:
                if a not in ("local-process", "local-file", "127.0.0.1", "localhost", "::1"):
                    bad(f"{p}.allowed_addresses", f"合成配置不允许地址 {a}")
        elif t.get("environment") in ("test", "staging", "production") and not t.get("credential_ref"):
            bad(f"{p}.credential_ref", "真实目标需要凭证引用（不是明文）")
    rules = cfg.get("rules") or {}
    if rules.get("kind") not in ("builtin_lab", "legacy_bridge"):
        bad("rules.kind", "规则来源必须是 builtin_lab 或 legacy_bridge")
    if profile == "project" and rules.get("kind") == "builtin_lab":
        bad("rules.kind", "真实项目不能使用合成实验规则")
    if rules.get("kind") == "legacy_bridge":
        manifest = rules.get("manifest")
        if not manifest:
            bad("rules.manifest", "旧规则桥接需要冻结清单路径")
        else:
            mp = (config_dir / manifest).resolve()
            if not mp.is_file():
                bad("rules.manifest", f"冻结清单不存在：{mp}")
    if "policy" not in rules:
        bad("rules.policy", "缺少执行策略（准入准出标准）")
    identity = cfg.get("identity") or {}
    if profile == "lab" and identity.get("adapter") not in ("lab_stub", "none"):
        bad("identity.adapter", "合成配置只能使用实验身份桩")
    budgets = cfg.get("budgets") or {}
    for key in ("max_request_bytes", "max_output_bytes", "exec_timeout_seconds", "max_concurrency",
                "max_private_storage_mb"):
        v = budgets.get(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
            bad(f"budgets.{key}", "限额必须是正数，不能无限运行")
    for i, a in enumerate(cfg.get("adapters") or []):
        if not a.get("capabilities"):
            bad(f"adapters[{i}].capabilities", "适配器必须声明能力")
    return problems


def _scan_secrets(obj: Any, path: str, bad) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if SECRET_KEYS.search(k) and isinstance(v, str) and v and not k.endswith("_ref"):
                bad(p, "配置不得包含凭证明文；请改用 credential_ref")
            _scan_secrets(v, p, bad)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _scan_secrets(v, f"{path}[{i}]", bad)
    elif isinstance(obj, str) and SECRET_VALUES.search(obj):
        bad(path, "配置值疑似凭证明文；请改用 credential_ref")


def probe(cfg: dict[str, Any], config_dir: Path) -> list[dict[str, Any]]:
    """能力探测：规则清单摘要、适配器实现是否存在。探测失败不写入配置。"""
    from qa_flow.adapters.registry import known_impls

    problems = []
    impls = known_impls()
    for i, a in enumerate(cfg.get("adapters") or []):
        if a.get("impl") not in impls:
            problems.append({"path": f"adapters[{i}].impl", "message": f"适配器实现 {a.get('impl')} 不可用"})
    rules = cfg.get("rules") or {}
    if rules.get("kind") == "legacy_bridge" and rules.get("manifest"):
        from qa_flow.adapters.legacy_rules import verify_manifest

        mp = (config_dir / rules["manifest"]).resolve()
        if mp.is_file():
            res = verify_manifest(mp, rules.get("manifest_sha256"))
            for p in res["problems"]:
                problems.append({"path": "rules.manifest", "message": p})
    return problems


def diff(old: dict[str, Any] | None, new: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    old = old or {}
    for g in sorted(set(old) | set(new)):
        if old.get(g) != new.get(g):
            out.append({"group": g, "change": "added" if g not in old else ("removed" if g not in new else "changed")})
    return out


def apply(ws: Workspace, cfg: dict[str, Any]) -> str:
    atomic_write(ws.config_dir / CONFIG_FILE, json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8"))
    return config_sha(cfg)


def target(cfg: dict[str, Any], target_id: str) -> dict[str, Any] | None:
    for t in cfg.get("targets") or []:
        if t["id"] == target_id:
            return t
    return None


def adapter(cfg: dict[str, Any], adapter_id: str) -> dict[str, Any] | None:
    for a in cfg.get("adapters") or []:
        if a["id"] == adapter_id:
            return a
    return None


def adapter_by_kind(cfg: dict[str, Any], kind: str) -> dict[str, Any] | None:
    for a in cfg.get("adapters") or []:
        if a["kind"] == kind:
            return a
    return None


def policy(cfg: dict[str, Any]) -> dict[str, Any]:
    return (cfg.get("rules") or {}).get("policy") or {}
