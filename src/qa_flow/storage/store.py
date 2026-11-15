"""工作区与事务存储。

- 快照不可变：只插入，不更新规范字节。
- 聚合按预期修订号更新，冲突返回 REVISION_CONFLICT。
- 事务内同时提交命令结果、业务事件、对象修订和待执行操作。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from qa_flow import clock
from qa_flow.canonical import canonical_bytes, loads_strict, sha256_bytes
from qa_flow.errors import QAError
from qa_flow.ids import new_id
from qa_flow.storage import db

DB_NAME = "qa.db"
WORKSPACE_MARKER = "workspace.json"


class Workspace:
    """单个项目工作区：数据库、证据、配置、导出与实验资源。"""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    # 目录布局 -------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.root / DB_NAME

    @property
    def evidence_dir(self) -> Path:
        return self.root / "evidence"

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def tmp_dir(self) -> Path:
        return self.root / "tmp"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    @property
    def lab_dir(self) -> Path:
        return self.root / "lab"

    @property
    def marker_path(self) -> Path:
        return self.root / WORKSPACE_MARKER

    def exists(self) -> bool:
        return self.marker_path.is_file() and self.db_path.is_file()

    def marker(self) -> dict[str, Any]:
        try:
            return json.loads(self.marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise QAError("WORKSPACE_INVALID", f"工作区标记不可读: {self.marker_path}") from exc

    def create(self, name: str, profile: str) -> None:
        if self.root.exists() and any(self.root.iterdir()):
            raise QAError("WORKSPACE_INVALID", f"目标目录非空，拒绝覆盖: {self.root}")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for d in (self.evidence_dir / "raw", self.evidence_dir / "redacted", self.config_dir,
                  self.tmp_dir, self.exports_dir):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
        conn = db.connect(self.db_path)
        try:
            db.initialize(conn, clock.now_iso())
        finally:
            conn.close()
        os.chmod(self.db_path, 0o600)
        marker = {"schema": "qa.workspace", "name": name, "profile": profile,
                  "created_at": clock.now_iso()}
        _atomic_write(self.marker_path, json.dumps(marker, ensure_ascii=False, indent=2).encode())

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if not self.exists():
            raise QAError("WORKSPACE_INVALID", f"工作区不存在或不完整: {self.root}")
        conn = db.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self, command_id: str | None = None) -> Iterator["Tx"]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tx = Tx(conn, self, command_id)
            try:
                yield tx
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    @contextmanager
    def reader(self) -> Iterator["Tx"]:
        """只读视图：使用独立连接与延迟事务，保证读取一致。"""
        with self.connect() as conn:
            conn.execute("BEGIN DEFERRED")
            try:
                yield Tx(conn, self, None, read_only=True)
            finally:
                conn.execute("ROLLBACK")


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{new_id('TMP')}")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


atomic_write = _atomic_write


class Tx:
    """一个事务内的存储操作集合。"""

    def __init__(self, conn: sqlite3.Connection, ws: Workspace, command_id: str | None,
                 read_only: bool = False):
        self.conn = conn
        self.ws = ws
        self.command_id = command_id
        self.read_only = read_only
        self.now = clock.now_iso()

    def _guard(self) -> None:
        if self.read_only:
            raise QAError("INTERNAL_INCONSISTENCY", "只读视图中不允许写入")

    # meta -----------------------------------------------------------------
    def meta_get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        self._guard()
        self.conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # 命令幂等 -----------------------------------------------------------------
    def command_lookup(self, command_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM commands WHERE command_id=?", (command_id,)).fetchone()

    def command_record(self, command_id: str, name: str, request_sha: str, response: dict) -> None:
        self._guard()
        self.conn.execute(
            "INSERT INTO commands(command_id, name, request_sha256, response, created_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (command_id, name, request_sha, json.dumps(response, ensure_ascii=False), self.now),
        )

    # 快照 -----------------------------------------------------------------
    def put_snapshot(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        story_id: str | None,
        project_id: str | None,
        producer: dict[str, Any],
        context: dict[str, Any] | None = None,
        depends_on: list[dict[str, Any]] | None = None,
        provenance: dict[str, Any] | None = None,
        lineage: str | None = None,
        parent_id: str | None = None,
        privacy: str = "internal",
        origin: str = "local",
        snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        self._guard()
        sid = snapshot_id or new_id(_prefix(kind))
        envelope = {
            "schema": f"qa.{kind}",
            "id": sid,
            "project_id": project_id,
            "story_id": story_id,
            "created_at": self.now,
            "producer": producer,
            "context": context or {},
            "depends_on": depends_on or [],
            "payload": payload,
            "provenance": provenance or {},
        }
        data = canonical_bytes(envelope)
        digest = sha256_bytes(data)
        self.conn.execute(
            "INSERT INTO snapshots(id, kind, project_id, story_id, lineage, parent_id, canonical, "
            "sha256, privacy, origin, created_at, command_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, kind, project_id, story_id, lineage, parent_id, data, digest, privacy, origin,
             self.now, self.command_id),
        )
        for dep in depends_on or []:
            self.conn.execute(
                "INSERT OR IGNORE INTO dependencies(result_id, input_id, input_sha256, relation, required) "
                "VALUES(?,?,?,?,?)",
                (sid, dep["ref"], dep.get("sha256", ""), dep.get("relation", "input"),
                 1 if dep.get("required", True) else 0),
            )
        self.event(sid, "snapshot_registered", {"kind": kind, "sha256": digest, "lineage": lineage,
                                                "parent_id": parent_id})
        if lineage:
            self._advance_lineage(lineage, kind, story_id, sid, digest, parent_id)
        return {"id": sid, "sha256": digest, "envelope": envelope}

    def _advance_lineage(self, lineage: str, kind: str, story_id: str | None, sid: str,
                         digest: str, parent_id: str | None) -> None:
        obj = f"lineage:{lineage}"
        agg = self.get_agg(obj)
        if agg is None:
            if parent_id:
                raise QAError("REFERENCE_INVALID", "修订的父快照不在该谱系中", path="parent_id",
                              related_refs=[parent_id])
            self.put_agg(obj, "lineage", story_id, {"kind": kind, "current": sid, "current_sha256": digest,
                                                    "history": [sid]}, expected_revision=0)
            return
        state = agg["state"]
        if parent_id is None or parent_id != state["current"]:
            raise QAError(
                "REVISION_CONFLICT",
                "该产物已有当前版本；修订必须基于当前快照提交",
                path="parent_id",
                related_refs=[state["current"]],
                details={"current": state["current"]},
            )
        state["history"].append(sid)
        state["current"] = sid
        state["current_sha256"] = digest
        self.put_agg(obj, "lineage", story_id, state, expected_revision=agg["revision"])

    def get_snapshot(self, sid: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM snapshots WHERE id=?", (sid,)).fetchone()
        if not row:
            return None
        env = loads_strict(row["canonical"])
        return {"id": row["id"], "kind": row["kind"], "sha256": row["sha256"], "story_id": row["story_id"],
                "lineage": row["lineage"], "parent_id": row["parent_id"], "origin": row["origin"],
                "privacy": row["privacy"], "created_at": row["created_at"], "envelope": env,
                "payload": env["payload"]}

    def require_snapshot(self, sid: str, kind: str | None = None, path: str = "") -> dict[str, Any]:
        snap = self.get_snapshot(sid)
        if snap is None or (kind and snap["kind"] != kind):
            raise QAError("REFERENCE_INVALID", f"引用的快照不存在或类型不符: {sid}", path=path,
                          related_refs=[sid])
        return snap

    def snapshots(self, story_id: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT id FROM snapshots WHERE 1=1"
        args: list[Any] = []
        if story_id is not None:
            q += " AND story_id=?"
            args.append(story_id)
        if kind is not None:
            q += " AND kind=?"
            args.append(kind)
        q += " ORDER BY rowid"
        return [self.get_snapshot(r["id"]) for r in self.conn.execute(q, args).fetchall()]

    def lineage_current(self, lineage: str) -> str | None:
        agg = self.get_agg(f"lineage:{lineage}")
        return agg["state"]["current"] if agg else None

    def current_of(self, kind: str, story_id: str, name: str | None = None) -> dict[str, Any] | None:
        sid = self.lineage_current(lineage_key(kind, story_id, name))
        return self.get_snapshot(sid) if sid else None

    def is_current(self, sid: str) -> bool:
        row = self.conn.execute("SELECT lineage FROM snapshots WHERE id=?", (sid,)).fetchone()
        if not row:
            return False
        if not row["lineage"]:
            return True
        return self.lineage_current(row["lineage"]) == sid

    def dependents(self, input_id: str) -> list[str]:
        return [r["result_id"] for r in self.conn.execute(
            "SELECT DISTINCT result_id FROM dependencies WHERE input_id=?", (input_id,)).fetchall()]

    # 事件 -----------------------------------------------------------------
    def event(self, object_id: str, event_type: str, payload: dict[str, Any],
              prev_rev: int | None = None, new_rev: int | None = None) -> int:
        self._guard()
        cur = self.conn.execute(
            "INSERT INTO events(command_id, object_id, event_type, prev_rev, new_rev, payload, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (self.command_id, object_id, event_type, prev_rev, new_rev,
             json.dumps(payload, ensure_ascii=False, sort_keys=True), self.now),
        )
        return int(cur.lastrowid)

    def events(self, object_id: str | None = None, since: int = 0) -> list[dict[str, Any]]:
        q = "SELECT * FROM events WHERE seq>?"
        args: list[Any] = [since]
        if object_id:
            q += " AND object_id=?"
            args.append(object_id)
        q += " ORDER BY seq"
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in self.conn.execute(q, args)]

    def last_seq(self) -> int:
        row = self.conn.execute("SELECT MAX(seq) AS s FROM events").fetchone()
        return int(row["s"] or 0)

    # 聚合 -----------------------------------------------------------------
    def get_agg(self, object_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM aggregates WHERE object_id=?", (object_id,)).fetchone()
        if not row:
            return None
        return {"object_id": row["object_id"], "kind": row["kind"], "story_id": row["story_id"],
                "revision": row["revision"], "state": json.loads(row["state"]),
                "updated_at": row["updated_at"]}

    def aggs(self, kind: str, story_id: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT object_id FROM aggregates WHERE kind=?"
        args: list[Any] = [kind]
        if story_id is not None:
            q += " AND story_id=?"
            args.append(story_id)
        q += " ORDER BY rowid"
        return [self.get_agg(r["object_id"]) for r in self.conn.execute(q, args).fetchall()]

    def put_agg(self, object_id: str, kind: str, story_id: str | None, state: dict[str, Any],
                expected_revision: int | None = None, event_type: str | None = None,
                event_payload: dict[str, Any] | None = None) -> int:
        """写入聚合。expected_revision=None 表示按读取到的当前修订更新（调用方已在同一事务内读取）。"""
        self._guard()
        row = self.conn.execute("SELECT revision FROM aggregates WHERE object_id=?", (object_id,)).fetchone()
        current = int(row["revision"]) if row else 0
        if expected_revision is not None and expected_revision != current:
            raise QAError("REVISION_CONFLICT",
                          f"对象修订冲突：期望 {expected_revision}，当前 {current}",
                          path="expected_revision", related_refs=[object_id],
                          details={"current_revision": current})
        new_rev = current + 1
        data = json.dumps(state, ensure_ascii=False, sort_keys=True)
        if row:
            cur = self.conn.execute(
                "UPDATE aggregates SET state=?, revision=?, updated_at=? WHERE object_id=? AND revision=?",
                (data, new_rev, self.now, object_id, current))
            if cur.rowcount != 1:
                raise QAError("REVISION_CONFLICT", "对象已被并发更新", related_refs=[object_id])
        else:
            self.conn.execute(
                "INSERT INTO aggregates(object_id, kind, story_id, revision, state, updated_at) "
                "VALUES(?,?,?,?,?,?)", (object_id, kind, story_id, new_rev, data, self.now))
        self.event(object_id, event_type or f"{kind}_updated", event_payload or {}, current, new_rev)
        return new_rev

    # 证据 -----------------------------------------------------------------
    def register_evidence(self, data: bytes, *, origin: str, source: str, media_type: str,
                          privacy: str = "internal", retention: str | None = None,
                          redacted_of: str | None = None) -> dict[str, Any]:
        """证据字节先安全落盘、校验摘要，再提交数据库引用。"""
        self._guard()
        digest = sha256_bytes(data)
        sub = "redacted" if redacted_of else "raw"
        rel = f"evidence/{sub}/{digest[:2]}/{digest}"
        target = self.ws.root / rel
        if not target.exists():
            from qa_flow import faults
            from qa_flow.canonical import sha256_file

            tmp = self.ws.tmp_dir / f"ev-{new_id('T')}"
            try:
                self.ws.tmp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                with open(tmp, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp, 0o600)
                faults.crash_point("evidence_tmp")  # 证据临时文件已写出、尚未移入内容寻址目录
                if sha256_file(tmp) != digest:
                    tmp.unlink(missing_ok=True)
                    raise QAError("IO_ERROR", "证据落盘后摘要不符")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.replace(tmp, target)
                _fsync_dir(target.parent)
            except OSError as exc:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise QAError("IO_ERROR", f"证据文件写入失败（{exc.strerror or exc}）；本次未提交任何记录，"
                              "修复目录权限或磁盘空间后重试", path=str(getattr(exc, "filename", "") or "")) from exc
        if not retention:
            retention = self._default_retention()
        eid = new_id("EV")
        self.conn.execute(
            "INSERT INTO evidence(id, sha256, rel_path, size, media_type, origin, source, privacy, retention, "
            "state, redacted_of, created_at) VALUES(?,?,?,?,?,?,?,?,?, 'active', ?, ?)",
            (eid, digest, rel, len(data), media_type, origin, source, privacy, retention, redacted_of, self.now))
        self.event(eid, "evidence_registered", {"sha256": digest, "origin": origin, "source": source,
                                                "privacy": privacy})
        out = {"id": eid, "sha256": digest, "origin": origin}
        if redacted_of is None and source.startswith("worker:"):
            # 执行证据：原始字节保留在 raw/，另存一份脱敏展示件供查看与导出
            from qa_flow.security.sanitize import scrub_for_display

            shown = scrub_for_display(data.decode("utf-8", errors="replace")).encode("utf-8")
            red = self.register_evidence(shown, origin=origin, source=f"redacted:{source}", media_type=media_type,
                                         privacy="internal", retention=retention, redacted_of=eid)
            out["redacted_ref"] = red["id"]
        return out

    def _default_retention(self) -> str:
        """未显式指定时，按配置的保留组生成保留标签，例如 retention_days=90 → "days:90"。"""
        try:
            cfg = json.loads((self.ws.config_dir / "qa-flow.json").read_text(encoding="utf-8"))
            days = (cfg.get("retention") or {}).get("retention_days")
        except (OSError, ValueError, AttributeError):
            days = None
        return f"days:{int(days)}" if isinstance(days, int) and days > 0 else "unspecified"

    def get_evidence(self, eid: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM evidence WHERE id=?", (eid,)).fetchone()
        return dict(row) if row else None

    def read_evidence(self, eid: str) -> bytes:
        ev = self.get_evidence(eid)
        if ev is None:
            raise QAError("EVIDENCE_INVALID", f"证据不存在: {eid}", related_refs=[eid])
        if ev["state"] != "active":
            raise QAError("EVIDENCE_INVALID", f"证据已{ev['state']}，不能作为有效证据读取", related_refs=[eid])
        path = self.ws.root / ev["rel_path"]
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise QAError("EVIDENCE_INVALID", f"证据文件缺失: {eid}", related_refs=[eid]) from exc
        if sha256_bytes(data) != ev["sha256"]:
            raise QAError("EVIDENCE_INVALID", f"证据摘要不符: {eid}", related_refs=[eid])
        return data

    def evidence_problem(self, eid: str) -> str | None:
        """证据不可用的具体原因：不存在、已销毁、文件缺失或摘要不符；可用时返回 None。"""
        ev = self.get_evidence(eid)
        if ev is None:
            return "证据不存在"
        if ev["state"] != "active":
            return f"证据已{ev['state']}" + (f"（{ev['destroy_reason']}）" if ev.get("destroy_reason") else "")
        try:
            data = (self.ws.root / ev["rel_path"]).read_bytes()
        except OSError:
            return "证据文件缺失"
        if sha256_bytes(data) != ev["sha256"]:
            return "证据摘要不符（文件内容与登记摘要冲突）"
        return None

    def evidence_valid(self, eid: str) -> bool:
        try:
            self.read_evidence(eid)
            return True
        except QAError:
            return False


def lineage_key(kind: str, story_id: str | None, name: str | None = None) -> str:
    return f"{kind}:{story_id or '-'}:{name or 'main'}"


_PREFIXES = {
    "source_revision": "SRC", "claim": "CLM", "clarification": "ASK", "decision": "DEC",
    "test_plan": "PLAN", "change_set": "CHG", "environment_check": "ENV", "script_revision": "SR",
    "execution_run": "RUN", "case_attempt": "ATT", "defect": "BUG", "entry_report": "ENT",
    "exit_report": "EXT", "release_check": "REL", "rollback_drill": "RBD", "rollback_plan": "RBP",
    "escape": "ESC", "improvement": "IMP", "knowledge_candidate": "KC", "metric_report": "MET",
    "action_plan": "AP", "task_request": "TRQ", "quality_review": "QR", "technical_review": "TR",
    "test_analysis": "TA", "case_design": "CD", "stage_evaluation": "SE", "test_report": "TRP",
    "run_provenance": "PRV", "sanitized_input": "SAN", "usability_report": "USR",
    "knowledge_snapshot": "KS", "archive_record": "ARC", "regression_plan": "REGP",
    "stability_assessment": "STB", "investigation": "INV", "flow_model": "FLW", "dataset": "DS",
    "validation_report": "VAL", "capability_report": "CAP", "effort_entry": "EFF",
    "metric_input": "MIN", "parity_report": "PAR", "case_quality_review": "CQR", "deployment": "DEP",
    "self_test_report": "STR",
}


def _prefix(kind: str) -> str:
    return _PREFIXES.get(kind, "OBJ")
