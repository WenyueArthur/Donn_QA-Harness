"""日常任务入口：开始、继续、待确认、查看/交付，以及离线首次实验。

任务服务把一个用户任务分解为已有应用命令，调用同一套校验、授权与操作调度；
不另写评分、阶段门禁或直接改状态。命令 ID、预期修订、动作键和检查点由系统维护。
活动任务指针只是界面上下文，不是第二套权威状态或授权依据。
"""

from __future__ import annotations

import json
import os
import shutil
from importlib import resources
from pathlib import Path
from typing import Any

from qa_flow import clock
from qa_flow.adapters import feishu_bundle
from qa_flow.application import broker, configuration, defects, execution, learning, review, stages
from qa_flow.application.context import Ctx, execute, open_ctx
from qa_flow.application.core import (current, effective_current, import_source, propagate, source_map, source_state,
                                      stale_reasons, submit_artifact)
from qa_flow.canonical import sha256_obj
from qa_flow.domain import workflow
from qa_flow.errors import QAError
from qa_flow.ids import new_id, stable_id
from qa_flow.reporting import render
from qa_flow.security.resources import resolve_identity
from qa_flow.security.sanitize import build_sanitized_input
from qa_flow.storage.store import Tx, Workspace, atomic_write, lineage_key

ROOT_DIRNAME = ".qa-workspaces"
LAB_WS_NAME = "lab-demo"
ACTIVE_FILE = "active.json"


# ---------------------------------------------------------------------------
# 工作区与任务选择
# ---------------------------------------------------------------------------

def find_root(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("QA_FLOW_HOME")
    if env:
        return Path(env).expanduser().resolve()
    cur = Path.cwd().resolve()
    for p in [cur, *cur.parents]:
        if (p / ROOT_DIRNAME).is_dir():
            return p / ROOT_DIRNAME
    return cur / ROOT_DIRNAME


def workspaces(root: Path) -> list[Workspace]:
    if (root / "workspace.json").is_file():  # 直接指向单个工作区
        return [Workspace(root)]
    if not root.is_dir():
        return []
    return [Workspace(p) for p in sorted(root.iterdir()) if p.is_dir() and Workspace(p).exists()]


def all_tasks(root: Path) -> list[dict[str, Any]]:
    out = []
    for ws in workspaces(root):
        ctx = Ctx(ws)
        with ws.reader() as rtx:
            for a in rtx.aggs("task"):
                out.append({"ws": ws, "ctx": ctx, "task": a["state"]})
    return out


def _active(root: Path) -> dict[str, Any] | None:
    try:
        return json.loads((root / ACTIVE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _set_active(root: Path, ws: Workspace, task_id: str) -> None:
    if (root / "workspace.json").is_file():
        return
    root.mkdir(parents=True, exist_ok=True)
    atomic_write(root / ACTIVE_FILE, json.dumps({"workspace": ws.root.name, "task_id": task_id,
                                                 "note": "界面上下文，不是权威状态或授权依据"},
                                                ensure_ascii=False).encode())


def guard_output(root: Path, target: str | Path, *, is_dir: bool = False) -> None:
    """接受输出路径的命令：目标（及目录内将写入的位置）不得是任何工作区登记的受保护原文，符号链接按真实路径判断。"""
    out = Path(target).expanduser()
    real = os.path.realpath(out)
    protected: set[str] = set()
    for ws in workspaces(root):
        try:
            with ws.reader() as rtx:
                protected |= {a["state"]["identity"] for a in rtx.aggs("protected_resource")}
        except QAError:
            continue
    if f"file:{real}" in protected:
        raise QAError("PROTECTED_RESOURCE", f"输出目标是受保护的原始资料，禁止写入：{out}", path="output",
                      related_refs=[f"file:{real}"])
    if is_dir:
        prefix = real.rstrip(os.sep) + os.sep
        inside = sorted(p for p in protected if p.startswith("file:" + prefix))
        if inside:
            raise QAError("PROTECTED_RESOURCE", f"输出目录包含受保护的原始资料，禁止写入：{out}", path="output",
                          related_refs=inside[:5])


_CONTEXT_SELECTION = {"allowed": False}


def allow_context_selection(allowed: bool) -> None:
    """命令行入口设置：是否允许按本目录的“当前需求”上下文选择（非交互调用一律不允许）。"""
    _CONTEXT_SELECTION["allowed"] = bool(allowed)


def select_task(root: Path, task: str | None, interactive: bool) -> dict[str, Any]:
    tasks = all_tasks(root)
    if not tasks:
        raise QAError("WORKSPACE_INVALID", "当前没有任何任务；先执行 demo 体验离线实验，或用 start --source 开始一个需求")
    if task:
        matches = [t for t in tasks if task in (t["task"]["display_title"], t["task"]["task_id"])] or \
                  [t for t in tasks if task in t["task"]["display_title"]]
        if len(matches) == 1:
            return matches[0]
        raise QAError("SELECTION_REQUIRED", f"按“{task}”没有唯一匹配的需求", path="task",
                      details={"candidates": [_candidate(t) for t in (matches or tasks)]})
    if len(tasks) == 1:
        return tasks[0]
    # 多个需求时：--non-interactive（Agent/脚本调用）从不按“最近使用”猜测；
    # 人在命令行直接使用时，沿用本目录最近一次 start/选择的需求，并在输出中标明当前需求
    if _CONTEXT_SELECTION["allowed"]:
        act = _active(root)
        if act:
            for t in tasks:
                if t["ws"].root.name == act.get("workspace") and t["task"]["task_id"] == act.get("task_id"):
                    return t
    if interactive:
        print("存在多个需求，请选择：")
        for i, t in enumerate(tasks, 1):
            c = _candidate(t)
            print(f"  {i}. {c['title']}（来源：{c['source']}；环境：{c['environment']}）")
        choice = input("输入序号：").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(tasks):
            return tasks[int(choice) - 1]
    raise QAError("SELECTION_REQUIRED", "存在多个需求，不能默认选择；请用 --task <业务名称> 明确选择", path="task",
                  details={"candidates": [_candidate(t) for t in tasks]})


def _candidate(t: dict[str, Any]) -> dict[str, Any]:
    return {"title": t["task"]["display_title"], "source": "、".join(t["task"]["sources"]),
            "environment": t["task"]["environment"], "project": t["ctx"].project_name}


def _choose_workspace(root: Path, project: str | None) -> Workspace:
    wss = workspaces(root)
    if project:
        m = [w for w in wss if project in (w.root.name, Ctx(w).project_name, Ctx(w).project_id)]
        if len(m) == 1:
            return m[0]
        raise QAError("SELECTION_REQUIRED", f"没有唯一匹配“{project}”的已核验项目",
                      details={"candidates": [Ctx(w).project_name for w in wss]})
    if not wss:
        raise QAError("WORKSPACE_INVALID", "没有已核验的项目工作区；先执行 demo，或请管理员执行 admin setup 建立项目")
    if len(wss) == 1:
        return wss[0]
    raise QAError("SELECTION_REQUIRED", "存在多个项目，请用 --project <项目名称> 选择",
                  details={"candidates": [Ctx(w).project_name for w in wss]})


# ---------------------------------------------------------------------------
# 开始
# ---------------------------------------------------------------------------

BUNDLE_GUIDE = ("飞书需求由已配置飞书项目 MCP 的 Agent（Cursor、Codex、Claude Code 等）读取：按 qa-flow Skill "
                "保存工作项、评论和挂载文档的原始输出并写成来源包，再执行 start --bundle <来源包>。")


def start(root: Path, sources: list[str] | None, *, project: str | None = None, title: str | None = None,
          interactive: bool = False, auto_advance: bool = True, bundle: str | None = None) -> dict[str, Any]:
    sources = sources or []
    if bundle and sources:
        raise QAError("INVALID_ARGUMENT", "--source 与 --bundle 只能二选一", path="bundle")
    if not bundle and not sources:
        raise QAError("INVALID_ARGUMENT", "需要 --source <需求文件> 或 --bundle <飞书来源包>", path="source")
    feishu = [s for s in sources if feishu_bundle.is_feishu_url(s)]
    if feishu:
        raise QAError("PRECONDITION_FAILED", "飞书链接不能直接作为来源：qa-flow 不直接连接飞书。" + BUNDLE_GUIDE,
                      path="source", details={"links": feishu})
    ws = _choose_workspace(root, project)
    ctx = Ctx(ws)
    if bundle:
        return _start_bundle(root, ws, ctx, bundle, title, auto_advance)
    abs_sources = [str(Path(s).expanduser().resolve()) if "://" not in s else s for s in sources]
    with ws.reader() as rtx:
        existing = next((a["state"] for a in rtx.aggs("task") if a["state"]["source_uris"] == abs_sources), None)
    if existing is not None:
        # 先结束只读事务再续做：在读事务内写入会与自身的读锁冲突
        _set_active(root, ws, existing["task_id"])
        out = advance(ctx, existing) if auto_advance else {}
        return {"task": existing, "existing": True, **out}
    task_id = new_id("TASK")
    story_id = new_id("STORY")
    op = stable_id("CMD", task_id, "start")

    def handler(tx: Tx) -> dict[str, Any]:
        imported = [import_source(tx, ctx, story_id, s) for s in abs_sources]
        main = imported[0]
        display = title or main.get("title")
        if not display:
            raise QAError("SELECTION_REQUIRED", "无法从来源识别需求名称；请用 --title 提供业务名称", path="title")
        req = {"task_kind": "start", "project_ref": ctx.project_id, "source_refs": [i["source_ref"] for i in imported],
               "requested_scope": "full", "operation_id": op}
        snap = tx.put_snapshot("task_request", req, story_id=story_id, project_id=ctx.project_id,
                               producer={"kind": "operator", "name": ctx.actor, "verification": "local_os_user"})
        state = {"task_id": task_id, "story_id": story_id, "display_title": display,
                 "sources": [Path(s).name if "://" not in s else s for s in abs_sources], "source_uris": abs_sources,
                 "source_refs": req["source_refs"], "environment": "lab（合成实验）" if ctx.is_lab else "test",
                 "project": ctx.project_name, "lab": ctx.is_lab, "created_at": tx.now, "request_ref": snap["id"],
                 "checkpoint": {"completed_steps": [], "pending_steps": [], "last_operation_ref": op, "counter": 0}}
        tx.put_agg(f"task:{task_id}", "task", story_id, state, expected_revision=0, event_type="task_started")
        return {"task": state, "sources": imported}

    res = execute(ctx, "task.start", {"sources": abs_sources, "title": title, "task": task_id}, handler, command_id=op)
    task = res["result"]["task"]
    _set_active(root, ws, task_id)
    out = advance(ctx, task) if auto_advance else {}
    return {"task": task, "sources": res["result"]["sources"], **out}


def _bundle_max_bytes(ctx: Ctx) -> int:
    return int(ctx.budgets.get("max_request_bytes", 1 << 20)) * 8


def _start_bundle(root: Path, ws: Workspace, ctx: Ctx, bundle_path: str, title: str | None,
                  auto_advance: bool) -> dict[str, Any]:
    """按飞书来源包开始需求；同一工作项再次登记时更新原任务的来源，不新建任务。"""
    b = feishu_bundle.load(Path(bundle_path), max_bytes=_bundle_max_bytes(ctx))
    root_identity = resolve_identity(b["root"]["uri"])
    items = b["items"]
    digest = sha256_obj([{k: (sha256_obj(v.decode("utf-8")) if isinstance(v, bytes) else v) for k, v in i.items()}
                         for i in items])
    with ws.reader() as rtx:
        existing = next((a["state"] for a in rtx.aggs("task") if a["state"].get("root_identity") == root_identity), None)
    notes: list[str] = []
    if existing is not None:
        task_id = existing["task_id"]

        def update(tx: Tx) -> dict[str, Any]:
            agg = tx.get_agg(f"task:{task_id}")
            st = dict(agg["state"])
            imported = [import_source(tx, ctx, st["story_id"], i["uri"], provided=i) for i in items]
            removed = [u for u in st["source_uris"] if u not in [i["uri"] for i in items]]
            added = [i["uri"] for i in items if i["uri"] not in st["source_uris"]]
            if removed or added:
                propagate(tx, ctx, st["story_id"], reason="需求来源列表变化："
                          + "；".join(x for x in ("新增挂载 " + "、".join(added) if added else "",
                                                  "不再挂载 " + "、".join(removed) if removed else "") if x))
            st.update({"sources": [feishu_bundle.display_name(i) for i in items], "source_uris": [i["uri"] for i in items],
                       "source_refs": [r["source_ref"] for r in imported], "bundle_digest": digest})
            if title:
                st["display_title"] = title
            tx.put_agg(agg["object_id"], "task", st["story_id"], st, expected_revision=agg["revision"],
                       event_type="task_sources_updated")
            return {"task": st, "imported": imported, "removed": removed, "added": added}

        res = execute(ctx, "task.bundle_refresh", {"task": task_id, "bundle": digest}, update,
                      command_id=stable_id("CMD", task_id, "bundle", digest))["result"]
        task = res["task"]
        for item, r in zip(items, res["imported"]):
            if r["changed"]:
                notes.append(f"来源已变化：{feishu_bundle.display_name(item)}；依赖旧内容的结论与确认已过期，只重新确认受影响部分")
        for u in res["removed"]:
            notes.append(f"工作项不再挂载：{u}；相关结论已重新核验")
        for u in res["added"]:
            notes.append(f"工作项新增挂载：{u}；相关结论已重新核验")
        if not notes:
            notes.append("飞书来源与已登记内容一致，已更新读取时间")
        _set_active(root, ws, task_id)
        out = advance(ctx, task) if auto_advance else {}
        out["notes"] = notes + out.get("notes", [])
        return {"task": task, "existing": True, "bundle": feishu_bundle.describe(b), **out}

    task_id = new_id("TASK")
    story_id = new_id("STORY")
    op = stable_id("CMD", task_id, "start")

    def handler(tx: Tx) -> dict[str, Any]:
        imported = [import_source(tx, ctx, story_id, i["uri"], provided=i) for i in items]
        req = {"task_kind": "start", "project_ref": ctx.project_id, "source_refs": [i["source_ref"] for i in imported],
               "requested_scope": "full", "operation_id": op}
        snap = tx.put_snapshot("task_request", req, story_id=story_id, project_id=ctx.project_id,
                               producer={"kind": "operator", "name": ctx.actor, "verification": "local_os_user"})
        state = {"task_id": task_id, "story_id": story_id, "display_title": title or b["title"],
                 "sources": [feishu_bundle.display_name(i) for i in items], "source_uris": [i["uri"] for i in items],
                 "source_refs": req["source_refs"], "environment": "lab（合成实验）" if ctx.is_lab else "test",
                 "project": ctx.project_name, "lab": ctx.is_lab, "created_at": tx.now, "request_ref": snap["id"],
                 "root_identity": root_identity, "bundle_digest": digest, "source_kind": "feishu_bundle",
                 "checkpoint": {"completed_steps": [], "pending_steps": [], "last_operation_ref": op, "counter": 0}}
        tx.put_agg(f"task:{task_id}", "task", story_id, state, expected_revision=0, event_type="task_started")
        return {"task": state, "sources": imported}

    res = execute(ctx, "task.start", {"bundle": digest, "title": title, "task": task_id}, handler, command_id=op)
    task = res["result"]["task"]
    _set_active(root, ws, task_id)
    for item in items:
        if item["read_status"] != "available":
            notes.append(f"{feishu_bundle.display_name(item)} 读取受阻：{item['failure_reason']}；"
                         "该文档内容未进入分析，结论会标注未验证范围")
    out = advance(ctx, task) if auto_advance else {}
    out["notes"] = notes + out.get("notes", [])
    return {"task": task, "sources": res["result"]["sources"], "bundle": feishu_bundle.describe(b), **out}


# ---------------------------------------------------------------------------
# 自动编排
# ---------------------------------------------------------------------------

def _checkpoint(ctx: Ctx, task: dict[str, Any], step: str, done: bool) -> None:
    def h(tx: Tx) -> dict[str, Any]:
        agg = tx.get_agg(f"task:{task['task_id']}")
        st = agg["state"]
        cp = dict(st["checkpoint"])
        cp["counter"] = cp.get("counter", 0) + 1
        if done and step not in cp["completed_steps"]:
            cp["completed_steps"] = cp["completed_steps"] + [step]
        cp["pending_steps"] = [s for s in cp.get("pending_steps", []) if s != step] + ([] if done else [step])
        cp["last_operation_ref"] = f"{task['task_id']}:{step}"
        st = {**st, "checkpoint": cp}
        tx.put_agg(agg["object_id"], "task", st["story_id"], st, expected_revision=agg["revision"],
                   event_type="task_checkpoint", event_payload={"step": step, "done": done})
        return {}

    execute(ctx, "task.checkpoint", {"task": task["task_id"], "step": step, "done": done, "n": new_id("N")}, h)


def _op(task: dict[str, Any], step: str, *parts: Any) -> str:
    return stable_id("CMD", task["task_id"], step, *parts)


def _generate(ctx: Ctx, task: dict[str, Any], kind: str, refs: dict[str, str], *, name: str | None = None) -> str | None:
    """实验模型桩生成草稿并提交校验。真实模型未配置时返回 None（由 Agent 经 Skill 提交）。"""
    story = task["story_id"]
    try:
        model = ctx.model()
    except QAError:
        return None
    with ctx.ws.reader() as rtx:
        smap = source_map(rtx, ctx, story)
    body = "\n\n".join(v["text"] for k, v in smap.items() if k in task["source_refs"])
    allowed = (ctx.config.get("model") or {}).get("allowed_fields") or ["title", "body"]
    san = build_sanitized_input({"title": task["display_title"], "body": body}, allowed,
                                review_done=(ctx.config.get("model") or {}).get("review_policy") == "lab_auto_reviewed")
    if san["payload"]["blocked_reasons"]:
        return None
    gen = model.generate(kind, san["payload"], refs)
    # 命令 ID 同时绑定生成内容与所依据的上游引用：上游变化后即使内容相同也要重新登记
    op = _op(task, f"gen:{kind}", sha256_obj(gen["payload"]), sha256_obj(refs))

    def h(tx: Tx) -> dict[str, Any]:
        mapping_ev = tx.register_evidence(json.dumps(san["mapping"], ensure_ascii=False).encode(), origin="local_lab"
                                          if ctx.is_lab else "imported", source="sanitizer-mapping",
                                          media_type="application/json", privacy="secret")
        san_snap = tx.put_snapshot("sanitized_input", san["payload"], story_id=story, project_id=ctx.project_id,
                                   producer={"kind": "process", "name": "sanitizer", "verification": "process"},
                                   provenance={"mapping_ref": mapping_ev["id"], "dropped_fields": san["dropped_fields"],
                                               "injection_hints": san["injection_hints"]}, privacy="internal")
        from qa_flow import fingerprints

        prov = dict(gen["provenance"])
        kb = tx.lineage_current(lineage_key("knowledge_snapshot", None, ctx.project_id))
        kb_snap = tx.get_snapshot(kb) if kb else None
        prov.update({"rules_sha256": ctx.rules_sha, "config_sha256": ctx.config_sha,
                     # 尚无知识快照时记录“空知识集”的摘要，而不是 unknown
                     "knowledge_sha256": kb_snap["sha256"] if kb_snap else sha256_obj({"entries": []}),
                     "knowledge_ref": kb or "none",
                     "code_sha256": fingerprints.code_sha256(), **fingerprints.skill(),
                     "tools": [], "sanitized_input_ref": san_snap["id"]})
        tx.put_snapshot("run_provenance", prov, story_id=story, project_id=ctx.project_id,
                        producer={"kind": "process", "name": "model_adapter", "verification": "process"})
        cur = tx.current_of(kind, story, name)
        res = submit_artifact(tx, ctx, kind, gen["payload"], story, name=name, provenance=prov,
                              producer={"kind": "model", "name": prov["model"]["model"], "verification": "lab_stub"},
                              parent_id=cur["id"] if cur and cur["payload"] != gen["payload"] else None)
        return res

    res = execute(ctx, f"generate.{kind}", {"op": op}, h, command_id=op)
    return res["result"]["artifact_id"]


def _evaluate(ctx: Ctx, task: dict[str, Any], stage_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
    def h(tx: Tx) -> dict[str, Any]:
        out = {}
        for sid in stage_ids or workflow.stage_ids():
            out[sid] = stages.evaluate(tx, ctx, task["story_id"], sid)
        return out

    return execute(ctx, "stage.evaluate_all", {"task": task["task_id"], "n": new_id("N")}, h)["result"]


def _dispatch(ctx: Ctx, action: dict[str, Any], notes: list[str]) -> str:
    if action["state"] == "succeeded":
        return "succeeded"
    if action["state"] in ("unknown", "reconciling"):
        r = broker.reconcile(ctx, action["id"])
        notes.append(f"核对未知结果：{broker.describe_kind(action['kind'])} → {r['state']}")
        return r["state"]
    if action["state"] in ("ready", "planned", "failed"):
        try:
            r = broker.dispatch(ctx, action["id"])
        except QAError as exc:
            notes.append(f"{broker.describe_kind(action['kind'])}未执行：{exc.message}")
            return "blocked"
        return r["state"]
    return action["state"]


def _plan(ctx: Ctx, task: dict[str, Any], key: str, fn) -> dict[str, Any]:
    return execute(ctx, f"action.plan:{key}", {"task": task["task_id"], "key": key}, fn,
                   command_id=_op(task, "plan", key))["result"]


def advance(ctx: Ctx, task: dict[str, Any], max_rounds: int = 40) -> dict[str, Any]:
    """恢复检查点：从数据库重建状态，再决定允许继续的步骤。遇到业务歧义、权限不足或结果未知时停止。"""
    notes: list[str] = []
    story = task["story_id"]
    changed = _refresh_sources(ctx, task, notes)
    with ctx.ws.reader() as rtx:
        unknown = broker.pending_unknown(rtx, story)
    for a in unknown:
        r = broker.reconcile(ctx, a["id"])
        notes.append(f"已核对结果未知的操作“{broker.describe_kind(a['kind'])}”：{r['state']}")
    task = dict(task)
    task["_redo_left"] = 1  # 每次续做最多重做一层已失效的产物，逐层呈现变更影响
    for _ in range(max_rounds):
        progressed = _round(ctx, task, notes)
        if not progressed:
            break
    _evaluate(ctx, task)
    return {"notes": notes, "source_changes": changed}


def _refresh_sources(ctx: Ctx, task: dict[str, Any], notes: list[str]) -> list[dict[str, Any]]:
    changes = []
    for uri in task["source_uris"]:
        try:
            r = execute(ctx, "source.refresh", {"task": task["task_id"], "uri": uri, "n": new_id("N")},
                        lambda tx, uri=uri: import_source(tx, ctx, task["story_id"], uri))["result"]
        except QAError as exc:
            notes.append(f"来源刷新失败：{exc.message}")
            continue
        if r.get("needs_agent_fetch"):
            if r["read_status"] == "stale":
                notes.append(f"飞书来源《{r.get('title') or uri}》已超过新鲜度期限；请让 Agent 重新读取后执行 "
                             "start --bundle 更新，期间不形成新结论")
            continue
        if r["changed"]:
            changes.append(r)
            notes.append(f"需求来源已变化：{Path(uri).name}；依赖旧内容的结论与确认已过期，只重新确认受影响部分")
        if r["read_status"] != "available":
            notes.append(f"来源 {Path(uri).name} 读取状态：{r['read_status']}（{r.get('failure_reason') or ''}）")
    if changes:
        _refresh_source_refs(ctx, task)
    return changes


def _refresh_source_refs(ctx: Ctx, task: dict[str, Any]) -> None:
    def h(tx: Tx) -> dict[str, Any]:
        agg = tx.get_agg(f"task:{task['task_id']}")
        refs = []
        for old in agg["state"]["source_refs"]:
            snap = tx.get_snapshot(old)
            refs.append(tx.lineage_current(snap["lineage"]) or old)
        st = {**agg["state"], "source_refs": refs}
        tx.put_agg(agg["object_id"], "task", st["story_id"], st, expected_revision=agg["revision"],
                   event_type="task_sources_updated")
        task["source_refs"] = refs
        return {}

    execute(ctx, "task.sources", {"task": task["task_id"], "n": new_id("N")}, h)


def _st(ctx: Ctx, task: dict[str, Any]) -> dict[str, str]:
    with ctx.ws.reader() as rtx:
        return stages.statuses(rtx, task["story_id"])


def _round(ctx: Ctx, task: dict[str, Any], notes: list[str]) -> bool:
    story = task["story_id"]
    try:
        return _round_inner(ctx, task, notes)
    except QAError as exc:
        if exc.code in ("CITATION_NOT_FOUND", "REFERENCE_INVALID", "SCHEMA_INVALID", "VALIDATION_FAILED",
                        "EVIDENCE_INVALID"):
            notes.append(f"生成内容未通过校验，保持草稿、未登记，阶段不完成：{exc.message}（{exc.code}）")
            return False
        raise


def _round_inner(ctx: Ctx, task: dict[str, Any], notes: list[str]) -> bool:
    story = task["story_id"]
    progressed = False
    res = _evaluate(ctx, task)
    st = {k: v["status"] for k, v in res.items()}
    with ctx.ws.reader() as rtx:
        srcs = {s["id"]: s for s in rtx.snapshots(story, "source_revision") if rtx.is_current(s["id"])}
        req_src = next((s["id"] for s in srcs.values() if "需求" in (s["payload"].get("title") or "")), None) or \
            (task["source_refs"][0] if task["source_refs"] else None)
        tech_src = next((s["id"] for s in srcs.values() if "技术" in (s["payload"].get("title") or "")), None)
        qr = current(rtx, "quality_review", story)
        tr = current(rtx, "technical_review", story)
        ta = current(rtx, "test_analysis", story)
        plan = current(rtx, "test_plan", story)
        design = current(rtx, "case_design", story)
        cqr = current(rtx, "case_quality_review", story)
        qr_ok = qr is not None and effective_current(rtx, qr["id"])
        tr_ok = tr is not None and effective_current(rtx, tr["id"])
        ta_ok = ta is not None and effective_current(rtx, ta["id"])
        plan_ok = plan is not None and effective_current(rtx, plan["id"])
        design_ok = design is not None and effective_current(rtx, design["id"])
        cqr_ok = cqr is not None and effective_current(rtx, cqr["id"]) and design is not None and \
            cqr["payload"]["design_ref"] == design["id"]
        state_answered = _answered(rtx, story, "order-state-partial")
    refs = {"requirement": req_src or "", "tech": tech_src or "", "rule_ref": ctx.rule_ref(),
            "plan_date": clock.now_iso()[:10], "state_answered": "yes" if state_answered else "no"}
    # 需求质检
    if not qr_ok and req_src:
        aid = _generate(ctx, task, "quality_review", refs)
        if aid:
            _propose_clarifications(ctx, task, aid)
            notes.append("已生成并校验需求质检（实验模型桩）")
            return True
        notes.append("需求质检需要由 Agent 按 qa-flow Skill 生成并提交；本机未配置模型")
        return False
    if st["req_gate"] == "completed" and not tr_ok and tech_src and _may_redo(task, tr):
        if _generate(ctx, task, "technical_review", refs):
            _used_redo(task, tr)
            notes.append("已生成并校验技术方案质检（实验模型桩）")
            return True
    if st["req_analysis"] in ("pending",) and st["tech_gate"] in ("completed", "waived") and not ta_ok \
            and _may_redo(task, ta):
        if _generate(ctx, task, "test_analysis", refs):
            _used_redo(task, ta)
            notes.append("已生成需求测试分析；受阻测试点保留但不进入可执行交接")
            return True
    if st["req_analysis"] == "completed" and ta_ok:
        refs["analysis"] = ta["id"]
        if not plan_ok and _may_redo(task, plan) and _generate(ctx, task, "test_plan", refs):
            _used_redo(task, plan)
            notes.append("已生成测试计划（估算区间，不是实测工时）")
            progressed = True
        if not design_ok and _may_redo(task, design) and _generate(ctx, task, "case_design", refs):
            _used_redo(task, design)
            notes.append("已生成用例设计；设计用例不自动成为可执行脚本")
            return True
    if st["case_design"] == "completed" and design_ok and not cqr_ok and _may_redo(task, cqr):
        refs["design"] = design["id"]
        if _generate(ctx, task, "case_quality_review", refs):
            _used_redo(task, cqr)
            notes.append("已完成用例质检（L1/L2/L3）")
            return True
    if progressed:
        return True
    # 以下为实验执行链路
    if not ctx.is_lab:
        return False
    return _lab_execution(ctx, task, st, notes)


def _may_redo(task: dict[str, Any], existing: dict[str, Any] | None) -> bool:
    """首次生成不受限；重做已失效的旧产物时，每次续做只做一层，其余留到下一次续做。"""
    return existing is None or task.get("_redo_left", 1) > 0


def _used_redo(task: dict[str, Any], existing: dict[str, Any] | None) -> None:
    if existing is not None:
        task["_redo_left"] = task.get("_redo_left", 1) - 1


def _answered(rtx: Tx, story: str, question_ref: str) -> bool:
    for a in rtx.aggs("review_item", story):
        s = a["state"]
        if s["kind"] != "clarification" or s["status"] != "answered":
            continue
        clar = rtx.get_snapshot(s["subject_refs"][0]["ref"])
        if clar and clar["payload"].get("question_ref") == question_ref:
            return True
    return False


def _propose_clarifications(ctx: Ctx, task: dict[str, Any], qr_id: str) -> None:
    def h(tx: Tx) -> dict[str, Any]:
        qr = tx.get_snapshot(qr_id)
        out = []
        for q in qr["payload"].get("clarifications") or []:
            out.append(review.propose_clarification(tx, ctx, task["story_id"], q, raised_by="req_gate"))
        return {"clarifications": out}

    execute(ctx, "clarification.propose_batch", {"qr": qr_id}, h, command_id=_op(task, "clar", qr_id))


def _lab_execution(ctx: Ctx, task: dict[str, Any], st: dict[str, str], notes: list[str]) -> bool:
    story = task["story_id"]
    with ctx.ws.reader() as rtx:
        dep_snap = execution.current_deployment(rtx, story)
        design = current(rtx, "case_design", story)
        script = rtx.current_of("script_revision", story, "lab/refund_cases.py")
        dataset = current(rtx, "dataset", story, "DS1")
        envchk = rtx.current_of("environment_check", story, "lab-refund")
        env_current = envchk is not None and effective_current(rtx, envchk["id"])
        v = stages.execution_view(rtx, ctx, story)
        ds = defects.all_defects(rtx, story)
        atts = v["attempts"]
    if dep_snap is None:
        return _lab_deploy(ctx, task, "build1", notes)
    build = dep_snap["payload"]["build"]
    build_id = next((k for k, b in execution.LAB_BUILDS.items() if b["build"] == build), build)
    if st["test_plan"] != "completed" or st["case_design"] != "completed" or design is None:
        return False
    if script is None:
        def reg(tx: Tx) -> dict[str, Any]:
            return {"script_ref": execution.ensure_lab_script(tx, ctx, story, [c["id"] for c in design["payload"]["cases"]])}
        execute(ctx, "script.register_lab", {"task": task["task_id"]}, reg, command_id=_op(task, "script"))
        notes.append("已登记随包受审的实验脚本（绑定文件摘要）")
        return True
    if not env_current:
        def plan_probe(tx: Tx) -> dict[str, Any]:
            a = broker.plan_action(tx, ctx, story, "environment.probe", "lab-refund",
                                   {"script_ref": script["id"], "dataset": "DS1", "expected_build": build_id,
                                    "dataset_ref": dataset["id"] if dataset else None},
                                   [script["id"], dep_snap["id"]] + ([dataset["id"]] if dataset else []),
                                   logical_key=f"probe:{dep_snap['id']}")
            return {"action": a}
        a = _plan(ctx, task, f"probe:{dep_snap['id']}", plan_probe)["action"]
        state = _dispatch(ctx, a, notes)
        notes.append(f"已实测环境与数据（构建 {build_id}，数据集 DS1）：{state}")
        return state == "succeeded"
    if st["env_ready"] != "completed":
        return False
    required_smoke = list(ctx.policy.get("required_smoke") or [])
    smoke_done = {a["tc_ref"] for a in atts if a["purpose"] == "smoke" and a["build"] == build and a["effective"]}
    if st["test_entry"] != "completed" and set(required_smoke) - smoke_done:
        return _run_cases(ctx, task, design, script, dep_snap, build_id, required_smoke, "smoke", notes)
    if st["test_entry"] != "completed":
        return False
    scope = v["scope"]
    on_build = {a["tc_ref"] for a in atts if a["build"] == build and a["effective"] and a["purpose"] != "smoke"}
    if set(scope) - on_build:
        purpose = "execution" if build_id == "build1" else "regression"
        return _run_cases(ctx, task, design, script, dep_snap, build_id, scope, purpose, notes)
    # 失败 → 缺陷
    latest = v["latest"]
    failed = [a for a in latest.values() if a["outcome"] == "fail"]
    open_defect_tcs = {d["tc_ref"] for d in ds if d["status"] not in ("closed", "rejected", "deferred", "accepted_risk")}
    new_failures = [a for a in failed if a["tc_ref"] not in open_defect_tcs]
    if new_failures:
        def prop(tx: Tx) -> dict[str, Any]:
            return {"defects": [defects.propose(tx, ctx, story, a["id"]) for a in new_failures]}
        execute(ctx, "defect.propose_batch", {"attempts": [a["id"] for a in new_failures]}, prop,
                command_id=_op(task, "propose", *[a["id"] for a in new_failures]))
        notes.append(f"发现 {len(new_failures)} 条用例失败，已形成缺陷提案（根因待验证）："
                     + "、".join(a["tc_ref"] for a in new_failures))
        return True
    for d in ds:
        if d["status"] == "proposed":
            a = _plan(ctx, task, f"create:{d['id']}", lambda tx, d=d: {"action": defects.plan_create(tx, ctx, story, d["id"])})["action"]
            state = _dispatch(ctx, a, notes)
            notes.append(f"在实验平台桩创建缺陷 {d['id']}：{state}")
            return state == "succeeded"
        if d.get("external_id") and not d["attachments_verified"]:
            a = _plan(ctx, task, f"attach:{d['id']}", lambda tx, d=d: {"action": defects.plan_attach(tx, ctx, story, d["id"])})["action"]
            state = _dispatch(ctx, a, notes)
            notes.append(f"上传并回读核对缺陷附件 {d['id']}：{state}")
            return state == "succeeded"
    open_ds = [d for d in ds if d["status"] not in ("closed", "rejected", "deferred", "accepted_risk")]
    if open_ds and build_id == "build1":
        # 实验研发修复：部署修复构建，平台侧置为已解决
        if not _lab_deploy(ctx, task, "build2", notes):
            return False
        pf = ctx.platform()
        for d in open_ds:
            pf.developer_resolve(d["external_id"], execution.LAB_BUILDS["build2"]["commit"], "build2")
        defects.sync(ctx, story)
        notes.append("实验研发已修复：部署构建 build2（fixed），缺陷在平台侧置为已解决")
        return True
    if open_ds:
        defects.sync(ctx, story)
        with ctx.ws.reader() as rtx:
            ds = defects.all_defects(rtx, story)
            atts = execution.attempts(rtx, story)
        for d in [x for x in ds if x["status"] not in ("closed", "rejected", "deferred", "accepted_risk")]:
            retests = [a for a in atts if a["tc_ref"] == d["tc_ref"] and a["build"] == build and a["effective"]
                       and d.get("fix_build") == build and a["purpose"] != "smoke"
                       and a["id"] != (d.get("actual_ref") or {}).get("attempt_ref")]
            if not retests:
                continue
            def link(tx: Tx, d=d, r=retests[-1]) -> dict[str, Any]:
                defects.record_retest(tx, story, d["id"], r["id"])
                return {"linked": r["id"]}
            execute(ctx, "defect.link_retest", {"d": d["id"], "a": retests[-1]["id"]}, link,
                    command_id=_op(task, "retest", d["id"], retests[-1]["id"]))
            with ctx.ws.reader() as rtx:
                chk = defects.close_check(rtx, ctx, story, d["id"], execution.attempts(rtx, story))
            if chk["can_close"]:
                for status in ("RETEST", "CLOSED"):
                    a = _plan(ctx, task, f"tr:{d['id']}:{status}",
                              lambda tx, d=d, status=status: {"action": defects.plan_transition(
                                  tx, ctx, story, d["id"], status, "修复构建复测与相邻回归均通过")})["action"]
                    if _dispatch(ctx, a, notes) != "succeeded":
                        return False
                notes.append(f"缺陷 {d['id']} 复测通过、相邻回归通过，已按授权关闭")
            elif retests[-1]["outcome"] != "pass":
                a = _plan(ctx, task, f"tr:{d['id']}:REOPENED",
                          lambda tx, d=d: {"action": defects.plan_transition(tx, ctx, story, d["id"], "REOPENED",
                                                                             "修复构建复测未通过")})["action"]
                _dispatch(ctx, a, notes)
                notes.append(f"缺陷 {d['id']} 复测未通过，已重新打开：{'；'.join(chk['missing'])}")
            else:
                notes.append(f"缺陷 {d['id']} 暂不能关闭：{'；'.join(chk['missing'])}")
        return False
    # 准出之后：知识回流
    if st["test_exit"] == "completed":
        with ctx.ws.reader() as rtx:
            has_kc = bool(rtx.aggs("knowledge", story))
            closed = [d for d in defects.all_defects(rtx, story) if d["status"] == "closed"]
        if not has_kc and closed:
            d = closed[0]
            refs = {"defect": d["proposal_ref"], "defect_evidence": d["evidence_ref"]}
            gen = ctx.model().generate("knowledge_candidate", {"title": task["display_title"]}, refs)

            def kc(tx: Tx) -> dict[str, Any]:
                return learning.propose_knowledge(tx, ctx, story, gen["payload"], provenance=gen["provenance"])
            execute(ctx, "knowledge.propose", {"task": task["task_id"]}, kc, command_id=_op(task, "kc"))
            notes.append("已形成知识候选；先例类条目需要人工确认后才入库")
            return True

        def apply(tx: Tx) -> dict[str, Any]:
            return {"applied": learning.apply_confirmed(tx, ctx, story)}
        r = execute(ctx, "knowledge.apply", {"task": task["task_id"], "n": new_id("N")}, apply)["result"]
        if r["applied"]:
            notes.append("已确认的知识条目已应用到隔离知识库，下一需求可检索")
            return True
    return False


def _lab_deploy(ctx: Ctx, task: dict[str, Any], build_id: str, notes: list[str]) -> bool:
    story = task["story_id"]

    def plan(tx: Tx) -> dict[str, Any]:
        reqs = [r for r in task["source_refs"]]
        return {"action": broker.plan_action(tx, ctx, story, "deploy.lab_build", "lab-refund", {"build_id": build_id},
                                             reqs[:1], logical_key=f"deploy:{build_id}")}
    a = _plan(ctx, task, f"deploy:{build_id}", plan)["action"]
    state = _dispatch(ctx, a, notes)
    notes.append(f"已在本机实验目标部署 {build_id}（{execution.LAB_BUILDS[build_id]['build']}）：{state}")
    return state == "succeeded"


def _run_cases(ctx: Ctx, task: dict[str, Any], design: dict[str, Any], script: dict[str, Any], dep_snap: dict[str, Any],
               build_id: str, cases: list[str], purpose: str, notes: list[str]) -> bool:
    story = task["story_id"]
    key = f"run:{purpose}:{dep_snap['id']}:{','.join(cases)}"

    def plan(tx: Tx) -> dict[str, Any]:
        return {"action": broker.plan_action(tx, ctx, story, "execute.cases", "lab-refund",
                                             {"cases": cases, "purpose": purpose, "script_ref": script["id"],
                                              "design_ref": design["id"], "dataset": "DS1", "expected_build": build_id},
                                             [design["id"], script["id"], dep_snap["id"]], logical_key=key)}
    a = _plan(ctx, task, key, plan)["action"]
    state = _dispatch(ctx, a, notes)
    label = {"smoke": "冒烟", "execution": "执行", "regression": "回归", "retest": "复测"}[purpose]
    notes.append(f"受控 worker 已{label} {len(cases)} 条用例（构建 {build_id}）：{state}")
    return state == "succeeded"


def resume(root: Path, task_name: str | None, interactive: bool) -> dict[str, Any]:
    sel = select_task(root, task_name, interactive)
    _set_active(root, sel["ws"], sel["task"]["task_id"])
    out = advance(sel["ctx"], sel["task"])
    return {"task": sel["task"], **out, "ctx": sel["ctx"]}


# ---------------------------------------------------------------------------
# 查看（只读：不执行重试、远端写入或归档）
# ---------------------------------------------------------------------------

NEXT_STAGE_HINT = {
    "req_gate": "生成并提交需求质检", "tech_gate": "提交技术方案质检或作出适用性决定", "req_analysis": "答复业务问题后生成测试分析",
    "test_plan": "批准测试计划", "case_design": "完成用例设计", "case_qc": "完成用例质检", "env_ready": "实测环境与数据",
    "test_entry": "执行冒烟并完成提测准入", "test_execution": "执行全部必测用例与回归", "test_exit": "确认准出",
    "release_verify": "实际部署后采集生产只读证据", "retrospective": "确认知识候选",
}


def view(ctx: Ctx, task: dict[str, Any]) -> dict[str, Any]:
    story = task["story_id"]
    with ctx.ws.reader() as rtx:
        st_list = []
        for sid in workflow.stage_ids():
            s = stages.stage_state(rtx, story, sid) or {"stage": sid, "title": workflow.stage(sid)["title"],
                                                        "status": "pending", "verdict": "indeterminate", "reasons": []}
            s = dict(s)
            if s.get("evaluation_ref"):
                rs = stale_reasons(rtx, ctx, s)
                if rs:
                    s["status"] = "stale"
                    s["reasons"] = rs
            st_list.append(s)
        items = review.items(rtx, story)
        groups = {"mine": [i for i in items if i["responsible_role"] == ctx.user_role],
                  "others": [i for i in items if i["responsible_role"] != ctx.user_role]}
        unknown = broker.pending_unknown(rtx, story)
        waiting = broker.actions(rtx, story, ("awaiting_authorization",))
        ds = defects.all_defects(rtx, story)
        tr_snap = current(rtx, "test_report", story)
        last_tick = rtx.meta_get("worker_last_tick_at")
        revision = rtx.last_seq()
        dep_snap = execution.current_deployment(rtx, story)
        san = [s for s in rtx.snapshots(story, "sanitized_input")]
        injection = sorted({h for s in san for h in s["envelope"]["provenance"].get("injection_hints", [])})
        kb = learning.search(rtx, ctx, "幂等")
        cur_sources = [s for s in rtx.snapshots(story, "source_revision") if rtx.is_current(s["id"])]
        unsupported = sorted({Path(s["payload"]["uri"]).name for s in cur_sources
                              if s["payload"].get("extraction_method") == "未支持"})
        agent_sources = [s for s in cur_sources if (s["payload"].get("fetched_by") or {}).get("agent")]
        stale_agent = [s["payload"].get("title") or s["payload"]["uri"] for s in agent_sources
                       if source_state(rtx, ctx, s) == "stale"]
        unreadable_agent = [f"{s['payload'].get('title') or s['payload']['uri']}（{s['payload'].get('failure_reason')}）"
                            for s in agent_sources if s["payload"].get("read_status") != "available"]
        admin_items = []
        for s in cur_sources:
            reason = s["payload"].get("failure_reason") or ""
            if s["payload"].get("read_status") != "available" and "连接器" in reason:
                admin_items.append(f"来源 {s['payload']['uri']} 读取受阻（{reason}）；需要管理员用 admin setup "
                                   "配置并核验文档连接器")
        needs_native = [i for i in review.items(rtx, story) if i["kind"] in ("action_authorization", "exit_confirmation",
                                                                              "plan_approval")
                        and i["environment"] not in ("lab", "offline")]
        if needs_native and (ctx.config.get("identity") or {}).get("adapter") in (None, "", "none"):
            admin_items.append(f"{len(needs_native)} 项真实环境批准需要原生审批身份核验，但项目未接入原生审批；"
                               "需要管理员用 admin setup 接入原生审批适配器")
    first_open = next((s for s in st_list if s["status"] not in ("completed", "waived")
                       and s["stage"] not in ("release_verify",)), None)
    done_all = first_open is None
    exit_s = next(s for s in st_list if s["stage"] == "test_exit")
    if done_all:
        headline = "测试已准出；上线验证待实际部署后核验" if exit_s["status"] == "completed" else "全部阶段完成"
    else:
        headline = f"{first_open['title']}：{render.STATUS_ZH.get(first_open['status'], first_open['status'])}"
    reasons = (first_open or {}).get("reasons") or []
    have = [s["title"] for s in st_list if s["status"] in ("completed", "waived")]
    next_actions = []
    if stale_agent:
        next_actions.append({"command": "start --bundle <来源包>", "effect": "让 Agent 重新读取飞书工作项与挂载文档后更新来源",
                             "scope": "、".join(stale_agent[:3]) + (" 等" if len(stale_agent) > 3 else ""),
                             "risk": "只读取飞书，不写入飞书"})
    if unknown:
        next_actions.append({"command": "resume", "effect": "先核对结果未知的操作，不会重发", "scope": f"{len(unknown)} 项操作",
                             "risk": "无新的外部写入"})
    if groups["mine"]:
        next_actions.append({"command": "review", "effect": "逐项处理你负责的业务确认", "scope": f"{len(groups['mine'])} 项",
                             "risk": "只记录本地答复，不是外部认证签字"})
    if groups["others"]:
        roles = sorted({ctx.role_label(i["responsible_role"]) for i in groups["others"]})
        next_actions.append({"command": "review", "effect": f"查看并转交需要{'、'.join(roles)}决定的问题",
                             "scope": f"{len(groups['others'])} 项", "risk": "你不能代签"})
    if not done_all and not groups["mine"] and not unknown:
        next_actions.append({"command": "resume", "effect": NEXT_STAGE_HINT.get(first_open["stage"], "继续"),
                             "scope": "当前需求中仍有效且获准的步骤", "risk": "仅本机实验资源" if ctx.is_lab else "按已核验配置"})
    if done_all or exit_s["status"] == "completed":
        next_actions.append({"command": "export --output <目录>", "effect": "导出本地交付报告", "scope": "当前需求",
                             "risk": "只写本地文件，不归档、不写平台"})
    unverified = [f"附件 {n} 格式不支持自动读取，内容未进入分析，需人工处理" for n in unsupported]
    if agent_sources:
        unverified.append("飞书来源内容由 Agent 读取后登记（agent_reported），qa-flow 未直接核对飞书原文")
    unverified += [f"飞书文档读取受阻，内容未进入分析：{x}" for x in unreadable_agent]
    if stale_agent:
        unverified.append("飞书来源已超过新鲜度期限，需 Agent 重新读取：" + "、".join(stale_agent))
    unverified.append("新人可用性尚未验证（未进行新人实测）")
    if ctx.is_lab:
        unverified.insert(0, "全部结果来自合成实验，不代表真实环境、真实业务或生产通过")
    unverified.append("上线验证：没有获准的生产只读连接器与实际部署，未验证")
    from qa_flow.adapters import local_executor

    if local_executor.isolation()["network"]:
        unverified.append("执行器有操作系统级网络与凭证隔离，但文件系统只是部分隔离，只运行已审可信脚本")
    else:
        unverified.append("执行器不具备网络/凭证/文件隔离，只运行已审可信脚本")
    return {
        "task": task, "lab": ctx.is_lab, "revision": revision, "stages": st_list,
        "summary": {"headline": headline, "reasons": reasons, "have": have, "next_actions": next_actions},
        "reviews": {"mine": [_card(ctx, i) for i in groups["mine"]], "others": [_card(ctx, i) for i in groups["others"]]},
        "open_reviews": [_card(ctx, i) for i in items],
        "unknown_actions": [{"kind": broker.describe_kind(a["kind"]), "id": a["id"], "error": a["last_error"]} for a in unknown],
        "awaiting_authorization": [{"kind": broker.describe_kind(a["kind"]), "target": a["target_id"]} for a in waiting],
        "defects": ds, "test_report": tr_snap["payload"] if tr_snap else None,
        "deployment": dep_snap["payload"] if dep_snap else None,
        "worker_last_tick_at": last_tick, "injection_hints": injection, "unverified": unverified,
        "admin_items": admin_items,
        "knowledge_hits": len(kb["results"]),
        "verdict": exit_s["verdict"] if exit_s["status"] != "stale" else "indeterminate",
        "last_verified_at": max((s.get("evaluated_at") or "" for s in st_list), default="") or None,
    }


def _card(ctx: Ctx, i: dict[str, Any]) -> dict[str, Any]:
    return {"handle": i["handle"], "kind": review.KIND_LABEL.get(i["kind"], i["kind"]),
            "business_question": i["business_question"], "basis": [{"quote": b.get("quote"), "location": b.get("location"),
                                                                     "source_ref": b.get("source_ref")} for b in i["basis"]],
            "affected_scope": i["affected_scope"], "responsible_role": i["responsible_role"],
            "role_label": ctx.role_label(i["responsible_role"]), "options": i["options"], "status": i["status"],
            "raised_by": i["raised_by"]}


def status(root: Path, task_name: str | None, interactive: bool) -> dict[str, Any]:
    sel = select_task(root, task_name, interactive)
    return {"view": view(sel["ctx"], sel["task"]), "ctx": sel["ctx"]}


# ---------------------------------------------------------------------------
# 待确认
# ---------------------------------------------------------------------------

def review_task(root: Path, task_name: str | None, answers: list[str], transfers: list[str], notes_kv: list[str],
                interactive: bool) -> dict[str, Any]:
    sel = select_task(root, task_name, interactive)
    ctx, task = sel["ctx"], sel["task"]
    story = task["story_id"]
    v = view(ctx, task)
    mine = v["reviews"]["mine"]
    notes = dict(kv.split("=", 1) for kv in notes_kv if "=" in kv)
    results = []
    planned: list[tuple[str, str]] = []
    for kv in answers:
        if "=" not in kv:
            raise QAError("INVALID_ARGUMENT", f"答复格式应为 编号=选项：{kv}", path="answer")
        h, opt = kv.split("=", 1)
        planned.append((h.strip(), opt.strip()))
    if interactive and not answers and not transfers and mine:
        planned, transfers = _prompt(mine, v["reviews"]["others"])
    if not planned and not transfers:
        if mine:
            raise QAError("REVIEW_REQUIRED", f"有 {len(mine)} 项待你确认", details={"items": mine,
                                                                               "others": v["reviews"]["others"]})
        return {"view": v, "results": [], "ctx": ctx}
    for h, opt in planned:
        def h_answer(tx: Tx, h=h, opt=opt) -> dict[str, Any]:
            return review.answer(tx, ctx, story, h, opt, notes.get(h))
        # 命令 ID 绑定事项当前修订：事项变化（已答复、过期或被新版本取代）后，同一条命令不会回放旧回执
        with ctx.ws.reader() as rtx:
            item = review.find_item(rtx, story, h)
        r = execute(ctx, "review.answer", {"task": task["task_id"], "handle": h, "option": opt, "note": notes.get(h),
                                           "item": item["object_id"], "rev": item["revision"]},
                    h_answer, command_id=_op(task, "answer", h, opt, notes.get(h), item["object_id"], item["revision"]))
        results.append(r["result"])
    if planned:
        _evaluate(ctx, task)  # 答复后重新判定，摘要原因反映当前事实
    for kv in transfers:
        h, role = kv.split("=", 1) if "=" in kv else (kv, "")
        results.append(_transfer(ctx, task, h.strip(), role.strip()))
    return {"view": view(ctx, task), "results": results, "ctx": ctx}


def _transfer(ctx: Ctx, task: dict[str, Any], handle: str, role: str) -> dict[str, Any]:
    story = task["story_id"]

    def h(tx: Tx) -> dict[str, Any]:
        item = review.find_item(tx, story, handle)
        to = role or item["responsible_role"]
        out = review.transfer(tx, ctx, story, handle, to)
        stub = (ctx.policy.get("simulated_answers") or {}).get(item["kind"])
        if ctx.is_lab and stub and stub["role"] == item["responsible_role"]:
            # 实验审批桩：只能产生 simulated 决定，不能升级为外部核验
            item2 = review.find_item(tx, story, handle)
            ans = review.answer(tx, ctx, story, handle, stub["option"], "实验审批桩模拟答复（不是真人确认）",
                                role=stub["role"], actor=stub["actor"], verification="simulated",
                                expected_revision=item2["revision"])
            out["simulated_decision"] = ans["decision_ref"]
            out["note"] = "实验审批桩已给出模拟答复；它不是真人或已认证的产品签字"
        else:
            out["note"] = f"已转交{ctx.role_label(to)}；该事项继续保持待处理，相关步骤仍受阻"
        return out

    return execute(ctx, "review.transfer", {"task": task["task_id"], "handle": handle, "role": role}, h,
                   command_id=_op(task, "transfer", handle, role))["result"]


def _prompt(mine: list[dict[str, Any]], others: list[dict[str, Any]]) -> tuple[list[tuple[str, str]], list[str]]:
    planned, transfers = [], []
    for c in mine:
        print(f"\n[{c['handle']}] {c['kind']}：{c['business_question']}")
        for b in c["basis"]:
            print(f"  依据：{b['quote']}" + (f"（{b['location']}）" if b.get("location") else ""))
        print(f"  影响范围：{c['affected_scope']}；需要：{c['role_label']}")
        for o in c["options"]:
            print(f"   {o['key']}. {o['label']}")
        print("   t. 转交责任人    s. 暂不处理")
        while True:
            ans = input("  你的选择：").strip()
            if ans.lower() == "s":
                break
            if ans.lower() == "t":
                transfers.append(f"{c['handle']}=")
                break
            if ans in [o["key"] for o in c["options"]]:
                planned.append((c["handle"], ans))
                break
            print("  请输入列出的选项；“全部同意”不能作为逐项决定。")
    if others:
        print("\n以下事项需要其他角色决定，你不能代签：")
        for c in others:
            print(f"  [{c['handle']}] {c['business_question']}（需要：{c['role_label']}）")
    return planned, transfers


# ---------------------------------------------------------------------------
# 交付（只本地导出）
# ---------------------------------------------------------------------------

def export(root: Path, task_name: str | None, output: str, interactive: bool) -> dict[str, Any]:
    sel = select_task(root, task_name, interactive)
    ctx, task = sel["ctx"], sel["task"]
    v = view(ctx, task)
    out = Path(output).expanduser().resolve()
    with ctx.ws.reader() as rtx:
        protected = {a["state"]["identity"] for a in rtx.aggs("protected_resource")}
    if f"file:{out}" in protected:
        raise QAError("PROTECTED_RESOURCE", "导出目录不能是原始资料")
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    v["exported_at"] = clock.now_iso()
    md = render.export_markdown(v)
    base = f"QA交付-{task['display_title']}".replace("/", "_")[:60]
    files = {"markdown": out / f"{base}.md", "html": out / f"{base}.html", "json": out / f"{base}.json"}
    for f in files.values():
        if f"file:{f}" in protected:
            raise QAError("PROTECTED_RESOURCE", "导出文件不能覆盖原始资料", related_refs=[str(f)])
    atomic_write(files["markdown"], md.encode("utf-8"))
    atomic_write(files["html"], render.export_html(md, base).encode("utf-8"))
    safe = {k: v[k] for k in ("task", "lab", "revision", "stages", "summary", "test_report", "defects", "unverified",
                              "verdict", "exported_at")}
    safe["task"] = {k: safe["task"][k] for k in ("task_id", "display_title", "sources", "environment", "project")}
    safe["watermark"] = "合成实验，不代表真实业务或生产" if ctx.is_lab else ""
    atomic_write(files["json"], json.dumps(safe, ensure_ascii=False, indent=2).encode("utf-8"))
    return {"files": {k: str(p) for k, p in files.items()}, "view": v, "ctx": ctx,
            "note": "仅生成本地文件；未写入飞书、未修改缺陷、未发布系统；远端归档需另行规划与授权"}


# ---------------------------------------------------------------------------
# 离线首次实验
# ---------------------------------------------------------------------------

def demo(root: Path, interactive: bool) -> dict[str, Any]:
    ws = Workspace(root / LAB_WS_NAME)
    created = False
    if not ws.exists():
        from qa_flow.security.resources import check_workspace_location

        problems = check_workspace_location(root)
        if problems:
            raise QAError("WORKSPACE_INVALID", "；".join(problems))
        ws.create(LAB_WS_NAME, "lab")
        configuration.apply(ws, configuration.lab_template())
        materials = ws.lab_dir / "materials"
        materials.mkdir(parents=True, exist_ok=True)
        for name in ("refund-requirement.md", "refund-tech.md", "refund-selftest.md"):
            data = resources.files("qa_flow.resources.lab.demo").joinpath(name).read_bytes()
            p = materials / name
            p.write_bytes(data)
            p.chmod(0o444)
        created = True
    ctx = Ctx(ws)
    materials = ws.lab_dir / "materials"
    srcs = [str(materials / "refund-requirement.md"), str(materials / "refund-tech.md"),
            str(materials / "refund-selftest.md")]
    with ws.reader() as rtx:
        existing = [a["state"] for a in rtx.aggs("task")]
    if existing:
        _set_active(root, ws, existing[0]["task_id"])
        out = advance(ctx, existing[0])
        return {"task": existing[0], "created": False, "workspace": str(ws.root), "ctx": ctx, **out}
    res = start(root, srcs, project=ctx.project_name, title="退款实验（合成数据）", auto_advance=False)
    task = res["task"]
    story = task["story_id"]

    def setup(tx: Tx) -> dict[str, Any]:
        ds = submit_artifact(tx, ctx, "dataset", {"name": "DS1", "version": "1", "kind": "synthetic",
                                                   "preparation": "O100 已支付 10000 分、未退款；O200 已全额退款",
                                                   "isolation": "每个工作区独立的实验数据库",
                                                   "cleanup": "每个用例前后重置为初态"}, story, name="DS1")
        selftest = [r for r in task["source_refs"]][2]
        st = submit_artifact(tx, ctx, "self_test_report", {"summary": "研发自测说明（合成）", "source_refs": [selftest],
                                                           "provided_by": "lab-developer", "build": "build1"}, story)
        return {"dataset": ds["artifact_id"], "self_test": st["artifact_id"]}

    execute(ctx, "demo.setup", {"task": task["task_id"]}, setup, command_id=_op(task, "demo-setup"))
    out = advance(ctx, task)
    return {"task": task, "created": created, "workspace": str(ws.root), "ctx": ctx, **out}
