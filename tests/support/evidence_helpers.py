"""批次 M2（来源、引文、脱敏、决定、授权、配置）验收的辅助函数。

只经由公共 CLI 建立状态；独立观测直接只读查询工作区数据库与磁盘文件。
不导入 qa_flow 的任何模块；摘要参考实现按规格（UTF-8、排序键、紧凑分隔）自行编写。
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .cli import QA, REPO, Result, write_json

LAB_RESOURCES = REPO / "src" / "qa_flow" / "resources" / "lab"

# 合成隐私哨兵（lab-fixtures.json privacy_sentinels）
PHONE = "13800000001"
EMAIL = "synthetic-person@example.invalid"
TOKEN = "TEST_SECRET_DO_NOT_SEND_7F92"
SENTINELS = (PHONE, EMAIL, TOKEN)

S1_TEXT = """# 积分退款需求（合成数据）

> 合成验收资料，不代表任何真实业务。

## 业务规则

- A1：退款金额必须为整数分，且不得超过已支付金额。
- A2：只有退款操作员可以提交退款。
- A3：同一幂等键重复提交时返回同一笔退款。

## 待确认

- 部分退款后订单状态如何展示，文档未说明。
"""
Q_A1 = "A1：退款金额必须为整数分，且不得超过已支付金额。"
Q_A2 = "A2：只有退款操作员可以提交退款。"
Q_OPEN = "部分退款后订单状态如何展示，文档未说明。"
FORGED = "退款可以超过已支付金额"

# 冻结清单要求存在的旧校验器相对路径（合成占位，用于建立非 lab 项目配置）
LEGACY_VALIDATORS = (
    "requirement-quality-gate/scripts/validate_report.py",
    "tech-design-quality-gate/scripts/validate_tech_gate.py",
    "requirements-analysis/scripts/validate_analysis.py",
    "requirement-test-case-design/scripts/validate_traceability.py",
    "test-case-quality-review/scripts/validate_qc_report.py",
)


# ---------------------------------------------------------------------------
# 参考摘要（独立实现）
# ---------------------------------------------------------------------------

def canonical(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha_obj(obj: Any) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def line_of(text: str, quote: str) -> int:
    """引文在原文中的起始行号（从 1 起）；找不到返回 0。"""
    idx = text.find(quote)
    return text.count("\n", 0, idx) + 1 if idx >= 0 else 0


# ---------------------------------------------------------------------------
# 工作区与任务
# ---------------------------------------------------------------------------

def lab_profile(model: bool = False) -> dict[str, Any]:
    """合成实验配置。model=False 时不配置模型桩，产物由测试经 api 提交。"""
    cfg = json.loads((LAB_RESOURCES / "profile.json").read_text(encoding="utf-8"))
    if not model:
        cfg["model"] = {"provider": "none", "allowed_fields": ["title", "body"]}
    return cfg


def init_lab(qa: QA, *, model: bool = False, name: str = "lab-m2", mutate=None) -> Path:
    cfg = lab_profile(model)
    if mutate:
        mutate(cfg)
    path = write_json(qa.cwd / f"{name}-config.json", cfg)
    r = qa.run("admin", "setup", "--config", str(path), "--name", name, "--apply")
    assert r.rc == 0 and r.json["applied"] is True, "实验工作区应可建立\n" + r.describe()
    return qa.root / name


def project_profile(qa: QA, *, targets: list[dict[str, Any]] | None = None,
                    adapters: list[dict[str, Any]] | None = None, pid: str = "PRJ-M2",
                    name: str = "积分商城项目", validator_body: str | None = None) -> dict[str, Any]:
    """真实项目（非 lab）配置：旧规则桥接指向一个最小的冻结清单。不含任何凭证明文。"""
    legacy = qa.cwd / "legacy-root"
    for rel in LEGACY_VALIDATORS:  # 合成的旧校验器占位：只输出固定 JSON，不代表真实旧规则
        f = legacy / "qa-mainflow" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        if not f.exists():
            f.write_text(validator_body or 'import json\nprint(json.dumps({"ok": True, "synthetic": True}))\n',
                         encoding="utf-8")
    cfg_dir = qa.cwd / "project-config"
    cfg_dir.mkdir(exist_ok=True)
    manifest = cfg_dir / "legacy-manifest.json"
    if not manifest.exists():
        r = qa.run("admin", "legacy-freeze", "--legacy-root", str(legacy), "--output", str(manifest), workspace=False)
        assert r.rc == 0, "应能冻结合成旧规则清单\n" + r.describe()
    (qa.cwd / "sources").mkdir(exist_ok=True)
    return {
        "schema": "qa.project_profile", "profile": "project", "project": {"id": pid, "name": name},
        "workspace": {"storage": "sqlite", "source_read_roots": [str(qa.cwd / "sources")], "protected_roots": [],
                      "archive_spaces": ["qa-archive"]},
        "targets": targets if targets is not None else [],
        "rules": {"kind": "legacy_bridge", "manifest": "legacy-manifest.json",
                  "policy": {"required_smoke": [], "required_regression": [], "authorizer_role": "tester",
                             "standing_grants": []}},
        "identity": {"adapter": "none", "current_role": "tester",
                     "role_map": {"tester": "测试人员", "product": "产品负责人", "admin": "管理员"}},
        "model": {"provider": "none"},
        "adapters": adapters if adapters is not None else [],
        "timing": {"timezone": "Asia/Shanghai", "source_max_age_hours": 72},
        "retention": {"privacy_default": "internal", "retention_days": 90, "backup": "manual"},
        "budgets": {"max_request_bytes": 1048576, "max_output_bytes": 262144, "exec_timeout_seconds": 60,
                    "max_concurrency": 1, "max_private_storage_mb": 512},
    }


def setup_project(qa: QA, cfg: dict[str, Any], *, name: str = "prj-m2", apply: bool = True) -> Result:
    path = write_json(qa.cwd / "project-config" / f"{name}.json", cfg)
    args = ["admin", "setup", "--config", str(path), "--name", name]
    return qa.run(*args, *(["--apply"] if apply else []))


def write_source(qa: QA, name: str = "s1.md", text: str = S1_TEXT, sub: str = "sources") -> Path:
    d = qa.cwd / sub
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


def start_story(qa: QA, *sources: Path, title: str = "积分退款需求", project: str | None = None) -> dict[str, Any]:
    """用 start 开始需求，返回 {story, task, sources: {文件名: 当前来源快照 ID}}。"""
    args = ["start", "--title", title]
    for s in sources:
        args += ["--source", str(s)]
    if project:
        args += ["--project", project]
    r = qa.run(*args)
    assert r.rc == 0, "start 应成功\n" + r.describe()
    tasks = [t for t in qa.api_ok("task", "list").json["tasks"] if t["title"] == title]
    assert len(tasks) == 1, tasks
    story = tasks[0]["story_id"]
    return {"story": story, "task": tasks[0]["task_id"], "sources": current_sources(qa, story)}


def ws_of(qa: QA, story: str) -> Path:
    for ws in qa.workspaces():
        if qa.query("SELECT 1 FROM snapshots WHERE story_id=? LIMIT 1", (story,), ws=ws):
            return ws
    raise AssertionError(f"没有找到需求 {story} 所在工作区")


def current_sources(qa: QA, story: str, ws: Path | None = None) -> dict[str, str]:
    """文件名 → 当前来源快照 ID（直接读谱系聚合）。"""
    ws = ws or ws_of(qa, story)
    out = {}
    for row in qa.query("SELECT state FROM aggregates WHERE kind='lineage' AND story_id=?", (story,), ws=ws):
        st = json.loads(row["state"])
        if st.get("kind") == "source_revision":
            env = envelope(qa, st["current"], ws)
            out[Path(env["payload"]["uri"]).name] = st["current"]
    return out


# ---------------------------------------------------------------------------
# 独立观测
# ---------------------------------------------------------------------------

def envelope(qa: QA, sid: str, ws: Path | None = None) -> dict[str, Any]:
    rows = qa.query("SELECT canonical FROM snapshots WHERE id=?", (sid,), ws=ws)
    assert rows, f"快照不存在：{sid}"
    raw = rows[0]["canonical"]
    return json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, memoryview)) else raw)


def snapshots(qa: QA, kind: str, story: str | None = None, ws: Path | None = None) -> list[dict[str, Any]]:
    sql = "SELECT id, sha256, origin, privacy, parent_id, canonical FROM snapshots WHERE kind=?"
    params: tuple = (kind,)
    if story:
        sql += " AND story_id=?"
        params += (story,)
    out = []
    for r in qa.query(sql + " ORDER BY rowid", params, ws=ws):
        raw = r.pop("canonical")
        r["envelope"] = json.loads(bytes(raw).decode("utf-8"))
        out.append(r)
    return out


def count(qa: QA, table: str, where: str = "1=1", params: tuple = (), ws: Path | None = None) -> int:
    return qa.query(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params, ws=ws)[0]["n"]


def last_seq(qa: QA, ws: Path | None = None) -> int:
    return qa.query("SELECT COALESCE(MAX(seq), 0) AS n FROM events", ws=ws)[0]["n"]


def events_since(qa: QA, seq: int, ws: Path | None = None) -> list[dict[str, Any]]:
    return qa.query("SELECT seq, object_id, event_type, payload FROM events WHERE seq>? ORDER BY seq", (seq,), ws=ws)


def aggs(qa: QA, kind: str, story: str | None = None, ws: Path | None = None) -> list[dict[str, Any]]:
    sql = "SELECT object_id, revision, state FROM aggregates WHERE kind=?"
    params: tuple = (kind,)
    if story:
        sql += " AND story_id=?"
        params += (story,)
    out = []
    for r in qa.query(sql + " ORDER BY rowid", params, ws=ws):
        out.append({"object_id": r["object_id"], "revision": r["revision"], **json.loads(r["state"])})
    return out


def platform_rows(ws: Path, table: str = "calls") -> list[dict[str, Any]]:
    """实验平台桩账本；尚未创建时视为空。"""
    path = ws / "lab" / "platform.db"
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def platform_writes(ws: Path) -> int:
    return sum(1 for c in platform_rows(ws) if c["op"] not in ("query", "get", "events", "doc_get")
               and c["outcome"] != "rejected")


def model_calls(ws: Path) -> list[dict[str, Any]]:
    path = ws / "lab" / "model_calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def stage_of(qa: QA, story: str, stage: str) -> dict[str, Any]:
    r = qa.api_ok("stage", "status", "--story", story)
    for s in r.json["stages"]:
        if s and s.get("stage") == stage:
            return s
    return {"stage": stage, "status": r.json["statuses"].get(stage)}


def all_output(*results: Result) -> str:
    return "\n".join(r.out + "\n" + r.err for r in results)


def tree_text(base: Path) -> str:
    """目录内全部文件的文本（非 UTF-8 字节按 latin-1 解读），用于扫描哨兵。"""
    parts = []
    for p in sorted(base.rglob("*")):
        if p.is_file():
            data = p.read_bytes()
            try:
                parts.append(data.decode("utf-8"))
            except UnicodeDecodeError:
                parts.append(data.decode("latin-1"))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 常用产物
# ---------------------------------------------------------------------------

def quality_review(src: str, *, rule_ref: str = "builtin_lab:P1", quote: str = Q_A1, location: str | None = None,
                   extra_findings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    cit: dict[str, Any] = {"source_ref": src, "quote": quote}
    if location:
        cit["location"] = location
    return {"rule_ref": rule_ref, "source_refs": [src],
            "findings": [{"id": "F1", "text": "金额上限的边界值未举例", "severity": "minor", "citations": [cit]},
                         *(extra_findings or [])],
            "testability": {"verdict": "pass", "notes": "规则可测"}, "review_note": "人工审阅：已逐条对照原文"}


def submit(qa: QA, story: str, kind: str, payload: dict[str, Any], *, name: str | None = None,
           parent: str | None = None, provenance: dict[str, Any] | None = None, tag: str = "") -> Result:
    f = write_json(qa.cwd / "payloads" / f"{kind}-{tag or sha_obj(payload)[:8]}.json", payload)
    args = ["artifact", "submit", "--story", story, "--kind", kind, "--file", str(f)]
    if name:
        args += ["--name", name]
    if parent:
        args += ["--parent", parent]
    if provenance is not None:
        args += ["--provenance-file", str(write_json(qa.cwd / "payloads" / f"prov-{sha_obj(provenance)[:8]}.json",
                                                     provenance))]
    return qa.api(*args)


def clone(obj: Any) -> Any:
    return copy.deepcopy(obj)


# ---------------------------------------------------------------------------
# 模块级夹具用：独立目录的调用器与常用状态搭建
# ---------------------------------------------------------------------------

def make_qa(base: Path) -> QA:
    """在给定临时目录下建立独立的工作区根、HOME 与当前目录（供 module 级夹具使用）。"""
    root, home, cwd = base / "工作区 根", base / "home", base / "cwd"
    for d in (root, home, cwd):
        d.mkdir(parents=True, exist_ok=True)
    return QA(root=root, home=home, cwd=cwd)


def demo_story(qa: QA) -> str:
    tasks = qa.api_ok("task", "list").json["tasks"]
    assert len(tasks) == 1, tasks
    return tasks[0]["story_id"]


def review_items(qa: QA, story: str, ws: Path | None = None) -> list[dict[str, Any]]:
    return aggs(qa, "review_item", story, ws)


def decisions(qa: QA, ws: Path | None = None) -> list[dict[str, Any]]:
    return qa.query("SELECT * FROM decisions ORDER BY rowid", ws=ws)


def actions(qa: QA, ws: Path | None = None) -> list[dict[str, Any]]:
    return qa.query("SELECT * FROM actions ORDER BY rowid", ws=ws)


def waive_tech_gate(qa: QA, story: str) -> None:
    """登记“未提供技术材料”并由研发角色作出不适用决定，使需求测试分析可以被评估。"""
    r = submit(qa, story, "technical_review", {"applicability": "not_provided", "source_refs": [], "models": {},
                                               "checks": []})
    assert r.rc == 0, r.describe()
    qa.api_ok("stage", "evaluate", "--story", story, "--stage", "tech_gate")
    item = [i for i in review_items(qa, story) if i["kind"] == "applicability" and i["status"] == "pending"]
    assert len(item) == 1, item
    qa.api_ok("clarification", "answer", "--story", story, "--handle", item[0]["handle"], "--option", "A",
              "--role", "developer", "--actor", "研发-王五", "--text", "本需求无技术方案变更")
    r = qa.api_ok("stage", "evaluate", "--story", story, "--stage", "tech_gate")
    assert r.result["status"] == "waived", r.describe()


def open_question(src: str, *, blocks: list[str] | None = None, question_ref: str = "order-state-partial",
                  question: str = "部分退款后订单状态应显示什么？") -> dict[str, Any]:
    return {"question_ref": question_ref, "question": question,
            "basis": [{"source_ref": src, "quote": Q_OPEN}], "responsible_role": "product",
            "blocks": blocks if blocks is not None else ["req_analysis"], "affected_scope": "订单状态展示",
            "options": [{"key": "A", "label": "显示“部分退款”"}, {"key": "B", "label": "保持“已支付”"}]}


def propose(qa: QA, story: str, q: dict[str, Any], stage: str) -> Result:
    f = write_json(qa.cwd / "payloads" / f"q-{sha_obj([q, stage])[:8]}.json", q)
    return qa.api("clarification", "propose", "--story", story, "--file", str(f), "--stage", stage)


def record_decision(qa: QA, story: str, payload: dict[str, Any]) -> Result:
    f = write_json(qa.cwd / "payloads" / f"decision-{sha_obj(payload)[:8]}.json", payload)
    return qa.api("decision", "record", "--story", story, "--file", str(f))


def plan_decision(qa: QA, story: str, action_id: str, **over: Any) -> dict[str, Any]:
    """为某个待授权操作构造绑定其计划摘要、目标、环境与操作类型的批准决定。"""
    a = qa.query("SELECT * FROM actions WHERE id=?", (action_id,))[0]
    psha = qa.query("SELECT sha256 FROM snapshots WHERE id=?", (a["plan_ref"],))[0]["sha256"]
    d = {"subject_refs": [{"ref": a["plan_ref"], "sha256": psha}], "plan_sha256": a["plan_sha256"], "role": "tester",
         "actor": "测试-赵六", "verification": "local_attested",
         "scope": {"environment": a["environment"], "target_ids": [a["target_id"]], "action_kinds": [a["kind"]],
                   "story_id": story},
         "result": "approve", "reason": "批准该归档计划", "proposer": "broker"}
    d.update(over)
    return d


def archive_plan(qa: QA, story: str, report: str, space: str = "lab-qa-archive", *extra: str) -> Result:
    return qa.api("archive", "plan", "--story", story, "--report", report, "--space", space, *extra)
