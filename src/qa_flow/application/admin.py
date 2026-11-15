"""管理员：配置预览与应用、能力检查、旧规则冻结与等价比较、证据到期清理。"""

from __future__ import annotations

import json
import platform
import sqlite3
import sys
from pathlib import Path
from typing import Any

from qa_flow import __version__, clock
from qa_flow.adapters import legacy_rules, local_executor
from qa_flow.application import configuration
from qa_flow.application.context import Ctx, execute
from qa_flow.application.core import propagate
from qa_flow.canonical import sha256_obj
from qa_flow.errors import QAError
from qa_flow.security.resources import check_workspace_location
from qa_flow.storage.store import Tx, Workspace

BOUNDARIES = [
    "本地数据库不能防御同一 OS 用户的恶意篡改；文件摘要只证明字节一致，不证明内容真实或身份可信。",
    "本地答复只是 local_attested，不是已认证的产品角色签字；真实批准需要原生审批身份核验。",
    "外部平台不提供查询或幂等能力时，不能保证结果未知的写操作自动恢复。",
    "执行器在 macOS 上用 sandbox-exec 禁止网络并阻止读取主目录与工作区私有内容；文件系统只是部分隔离，"
    "其他平台没有隔离；只运行已审可信脚本，不宣称支持不可信脚本。",
    "首轮只验收进程崩溃恢复；断电持久性需在目标文件系统另做测试。",
    "支持的操作系统由验收结果决定；当前未完成任何平台的验收。",
]


def setup(root: Path, project_dir_name: str | None, config_file: Path, apply: bool) -> dict[str, Any]:
    try:
        new_cfg = json.loads(Path(config_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QAError("CONFIG_INVALID", f"配置文件不可读：{config_file}") from exc
    name = project_dir_name or (new_cfg.get("project") or {}).get("id") or "project"
    ws = Workspace(root / name)
    config_dir = ws.config_dir if ws.exists() else Path(config_file).resolve().parent
    old = None
    if ws.exists():
        try:
            old = configuration.load(ws)
        except QAError:
            old = None
    problems = configuration.validate(new_cfg, config_dir)
    problems += check_workspace_location_issues(root)
    if not problems:
        problems += configuration.probe(new_cfg, config_dir)
    report = {"workspace": str(ws.root), "diff": configuration.diff(old, new_cfg), "problems": problems,
              "applied": False, "preview_only": not apply}
    if not apply or problems:
        if problems:
            report["next"] = "修正所列配置项后重新预览；原有效配置未被修改"
        else:
            report["next"] = "预览通过；加 --apply 原子应用"
        return report
    if not ws.exists():
        ws.create(name, new_cfg["profile"])
    # 规则清单路径相对显式配置目录：一并复制清单，保持引用可解析
    rules = new_cfg.get("rules") or {}
    if rules.get("kind") == "legacy_bridge" and rules.get("manifest"):
        src = (Path(config_file).resolve().parent / rules["manifest"]).resolve()
        dst = ws.config_dir / Path(rules["manifest"]).name
        if src != dst:
            dst.write_bytes(src.read_bytes())
        new_cfg = {**new_cfg, "rules": {**rules, "manifest": dst.name}}
    sha = configuration.apply(ws, new_cfg)
    ctx = Ctx(ws)

    def h(tx: Tx) -> dict[str, Any]:
        tx.event("config", "config_applied", {"config_sha256": sha, "diff": report["diff"]})
        stories = sorted({a["story_id"] for a in tx.aggs("task")})
        stale = {s: propagate(tx, ctx, s, reason="配置已变化") for s in stories}
        return {"stale": stale}

    res = execute(ctx, "admin.setup", {"sha": sha}, h)
    report.update({"applied": True, "config_sha256": sha, "stale": res["result"]["stale"]})
    return report


def check_workspace_location_issues(root: Path) -> list[dict[str, Any]]:
    return [{"path": "workspace", "message": m} for m in check_workspace_location(root)]


def doctor(ctx: Ctx | None, root: Path | None = None) -> dict[str, Any]:
    conn = sqlite3.connect(":memory:")
    try:
        sqlite_version = conn.execute("select sqlite_version()").fetchone()[0]
    finally:
        conn.close()
    out: dict[str, Any] = {
        "qa_flow": __version__, "python": sys.version.split()[0], "sqlite": sqlite_version,
        "platform": platform.platform(), "executor": {"impl": "local_subprocess", "isolation": local_executor.isolation(),
                                                       "untrusted_scripts": "不支持"},
        "boundaries": BOUNDARIES, "checks": [],
    }
    from qa_flow.security.resources import check_workspace_location

    loc_root = ctx.ws.root if ctx is not None else root
    if loc_root is not None:
        loc = check_workspace_location(Path(loc_root))
        out["checks"].append({"item": "workspace_location", "status": "unsupported" if loc else "ok",
                              "detail": "；".join(loc) + "；该位置只允许只读查看，不调度执行或外部写入" if loc
                              else f"{loc_root} 位于本机文件系统"})
    if ctx is None:
        out["workspace"] = None
        out["checks"].append({"item": "workspace", "status": "unavailable", "detail": "未选择工作区"})
        return out
    with ctx.ws.connect() as c:
        jm = c.execute("PRAGMA journal_mode").fetchone()[0]
        sync = c.execute("PRAGMA synchronous").fetchone()[0]
        ic = c.execute("PRAGMA integrity_check").fetchone()[0]
    out["workspace"] = {"root": str(ctx.ws.root), "profile": ctx.profile, "project": ctx.project_name,
                        "journal_mode": jm, "synchronous": sync, "integrity": ic}
    rules = ctx.config.get("rules") or {}
    if rules.get("kind") == "legacy_bridge":
        mp = (ctx.ws.config_dir / rules.get("manifest", "")).resolve()
        if not mp.is_file():
            out["checks"].append({"item": "rules", "status": "unavailable", "detail": f"指定的冻结清单不存在：{mp}；不会加载其他位置的旧脚本"})
        else:
            v = legacy_rules.verify_manifest(mp, rules.get("manifest_sha256"))
            out["checks"].append({"item": "rules", "status": "ok" if v["ok"] else "unavailable",
                                  "detail": "；".join(v["problems"]) or f"旧规则冻结清单有效（{len(v['manifest']['files'])} 个文件）"})
    else:
        out["checks"].append({"item": "rules", "status": "ok", "detail": f"{rules.get('kind')}（合成实验规则，不是旧项目评分规则）"})
    problems = configuration.validate(ctx.config, ctx.ws.config_dir) + configuration.probe(ctx.config, ctx.ws.config_dir)
    if ctx.config_problem:
        problems.append({"path": "rules", "message": ctx.config_problem})
    out["checks"].append({"item": "config", "status": "ok" if not problems else "invalid", "detail": problems})
    for a in ctx.config.get("adapters") or []:
        out["checks"].append({"item": f"adapter:{a['id']}", "status": "ok" if a.get("impl") else "unavailable",
                              "detail": {"impl": a.get("impl"), "capabilities": a.get("capabilities"),
                                         "environment": "lab" if ctx.is_lab else "configured"}})
    model = ctx.config.get("model") or {}
    out["checks"].append({"item": "model", "status": "lab_stub" if model.get("provider") == "lab_stub" else "unavailable",
                          "detail": "仅合成实验模型桩；不发送真实模型请求" if model.get("provider") == "lab_stub"
                          else "未配置模型提供方；不发送模型请求"})
    ident = ctx.config.get("identity") or {}
    out["checks"].append({"item": "identity", "status": "local_only",
                          "detail": "未接入原生审批适配器：只能验证本地实验，不能认证真实批准"
                          if ident.get("adapter") in (None, "none", "lab_stub") else ident.get("adapter")})
    with ctx.ws.reader() as rtx:
        last = rtx.meta_get("worker_last_tick_at")
    out["checks"].append({"item": "worker", "status": "ok" if last else "never_run",
                          "detail": f"最后一次 tick：{last}" if last else "未运行 worker tick；自动刷新与提醒未启用"})
    reminders = ((ctx.config.get("timing") or {}).get("reminders") or {})
    out["checks"].append({"item": "notifications", "status": "authorized" if reminders.get("notify_authorized") else "not_authorized",
                          "detail": "通知只登记待发送动作；未获授权时不发送"})
    return out


def legacy_freeze(legacy_root: Path, output: Path) -> dict[str, Any]:
    return legacy_rules.freeze(legacy_root, output)


def parity_record(ctx: Ctx, samples_dir: Path, output: Path) -> dict[str, Any]:
    """运行冻结的旧校验器，保存样本输出作为等价比较基线（不由新校验器动态生成预期）。"""
    rules = ctx.config.get("rules") or {}
    if rules.get("kind") != "legacy_bridge":
        raise QAError("RULES_UNAVAILABLE", "当前配置未使用旧规则桥接")
    mp = (ctx.ws.config_dir / rules["manifest"]).resolve()
    samples = []
    for f in sorted(Path(samples_dir).glob("*/*.json")):
        stage = f.parent.name
        if stage not in legacy_rules.VALIDATORS:
            continue
        data = json.loads(f.read_text(encoding="utf-8"))
        res = legacy_rules.run_validator(mp, rules.get("manifest_sha256"), stage, data,
                                         float(ctx.budgets.get("exec_timeout_seconds", 60)),
                                         int(ctx.budgets.get("max_output_bytes", 262144)))
        samples.append({"stage": stage, "sample": f.name, "input_sha256": sha256_obj(data), "exit_code": res["exit_code"],
                        "verdict": res["verdict"], "output": res["output"], "validator_sha256": res["validator_sha256"]})
    base = {"schema": "qa.parity_baseline", "captured_at": clock.now_iso(), "manifest": str(mp), "samples": samples}
    Path(output).write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"baseline": str(output), "samples": len(samples)}


def parity_check(ctx: Ctx, samples_dir: Path, baseline: Path) -> dict[str, Any]:
    base = json.loads(Path(baseline).read_text(encoding="utf-8"))
    rules = ctx.config.get("rules") or {}
    mp = (ctx.ws.config_dir / rules["manifest"]).resolve()
    diffs = []
    for s in base["samples"]:
        f = Path(samples_dir) / s["stage"] / s["sample"]
        data = json.loads(f.read_text(encoding="utf-8"))
        if sha256_obj(data) != s["input_sha256"]:
            diffs.append({"sample": s["sample"], "kind": "input_changed"})
            continue
        res = legacy_rules.run_validator(mp, rules.get("manifest_sha256"), s["stage"], data,
                                         float(ctx.budgets.get("exec_timeout_seconds", 60)),
                                         int(ctx.budgets.get("max_output_bytes", 262144)))
        for key in ("exit_code", "verdict", "output"):
            if res[key] != s[key]:
                diffs.append({"sample": s["sample"], "stage": s["stage"], "field": key})
    return {"samples": len(base["samples"]), "differences": diffs, "equivalent": not diffs,
            "note": "等价只用于防止重构暗改已确认规则；每个允许差异需要单独的审批依据"}


def expire_evidence(ctx: Ctx, evidence_id: str, reason: str) -> dict[str, Any]:
    """按保留策略销毁私有证据：记录销毁事件与摘要，依赖它的当前验证过期，不能再作为有效证据读取。"""

    def h(tx: Tx) -> dict[str, Any]:
        ev = tx.get_evidence(evidence_id)
        if ev is None:
            raise QAError("NOT_FOUND", f"证据不存在：{evidence_id}")
        if ev["state"] != "active":
            return {"evidence_id": evidence_id, "state": ev["state"], "already": True}
        # 原始证据与其脱敏展示件一并销毁：展示件由原始字节派生，原件到期后不能留下可读副本
        targets = [ev] + [dict(r) for r in tx.conn.execute(
            "SELECT * FROM evidence WHERE redacted_of=? AND state='active'", (evidence_id,))]
        ids = [t["id"] for t in targets]
        files = []
        for t in targets:
            tx.conn.execute("UPDATE evidence SET state='expired', destroyed_at=?, destroy_reason=? WHERE id=?",
                            (tx.now, reason, t["id"]))
            tx.event(t["id"], "evidence_destroyed", {"sha256": t["sha256"], "reason": reason})
        for t in targets:
            shared = tx.conn.execute(
                f"SELECT COUNT(*) AS n FROM evidence WHERE rel_path=? AND state='active' "
                f"AND id NOT IN ({','.join('?' * len(ids))})", (t["rel_path"], *ids)).fetchone()["n"]
            if not shared:
                files.append(t["rel_path"])
        stories = {r["story_id"] for r in tx.conn.execute("SELECT DISTINCT story_id FROM snapshots WHERE story_id IS NOT NULL")}
        stale = {s: propagate(tx, ctx, s, reason=f"证据 {evidence_id} 已按保留策略销毁") for s in stories}
        return {"evidence_id": evidence_id, "state": "expired", "sha256": ev["sha256"], "stale": stale,
                "destroyed_display_copies": ids[1:], "_files": sorted(set(files))}

    res = execute(ctx, "evidence.expire", {"id": evidence_id, "reason": reason}, h)
    for rel in res["result"].pop("_files", None) or []:
        p = ctx.ws.root / rel
        if p.is_file():
            p.unlink()
    return res
