"""实验平台桩：缺陷工作项、附件、状态事件与文档，带调用账本和故障模式。

账本独立于主数据库（lab/platform.db），用于核对实际创建、更新、关联和查询次数。
故障模式只能在测试进程（QA_FLOW_TEST_MODE=1）中设置。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from qa_flow import clock
from qa_flow.adapters.base import OutcomeUnknown, RateLimited, RemoteFailed
from qa_flow.canonical import sha256_bytes
from qa_flow.errors import QAError
from qa_flow.ids import new_id

FAULTS = {
    "create_succeeded_response_lost", "attachment_uploaded_link_failed", "duplicate_event", "out_of_order_event",
    "unknown_status", "rate_limited", "approval_revoked", "etag_conflict", "partial_document_write",
}

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY, idem_key TEXT UNIQUE, fields TEXT NOT NULL,"
    " status TEXT NOT NULL, revision INTEGER NOT NULL, attachments TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY, item_id TEXT, sha256 TEXT, name TEXT, idem_key TEXT UNIQUE)",
    "CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, item_id TEXT,"
    " status TEXT, revision INTEGER, created_at TEXT)",
    "CREATE TABLE IF NOT EXISTS docs(id TEXT PRIMARY KEY, space TEXT, title TEXT, content TEXT, etag TEXT,"
    " complete INTEGER, idem_key TEXT UNIQUE)",
    "CREATE TABLE IF NOT EXISTS calls(seq INTEGER PRIMARY KEY AUTOINCREMENT, op TEXT, target TEXT, payload_sha256 TEXT,"
    " outcome TEXT, created_at TEXT)",
    "CREATE TABLE IF NOT EXISTS faults(name TEXT PRIMARY KEY, remaining INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS notifications(id TEXT PRIMARY KEY, idem_key TEXT UNIQUE, body TEXT, created_at TEXT)",
]


class FakePlatform:
    def __init__(self, lab_dir: Path):
        self.path = Path(lab_dir) / "platform.db"

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), autocommit=True, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for s in SCHEMA:
                conn.execute(s)
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # 账本与故障 -----------------------------------------------------------
    def _call(self, conn: sqlite3.Connection, op: str, target: str, payload: Any, outcome: str) -> None:
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        conn.execute("INSERT INTO calls(op, target, payload_sha256, outcome, created_at) VALUES(?,?,?,?,?)",
                     (op, target, sha256_bytes(data), outcome, clock.now_iso()))

    def _take_fault(self, conn: sqlite3.Connection, name: str) -> bool:
        row = conn.execute("SELECT remaining FROM faults WHERE name=?", (name,)).fetchone()
        if not row or row["remaining"] == 0:
            return False
        if row["remaining"] > 0:
            conn.execute("UPDATE faults SET remaining=remaining-1 WHERE name=?", (name,))
        return True

    def set_fault(self, name: str, times: int = 1) -> None:
        if not clock.test_mode():
            raise QAError("AUTHORIZATION_INVALID", "故障注入只能在测试进程中启用")
        if name not in FAULTS:
            raise QAError("INVALID_ARGUMENT", f"未知故障模式: {name}")
        with self._db() as conn:
            conn.execute("INSERT INTO faults(name, remaining) VALUES(?, ?) "
                         "ON CONFLICT(name) DO UPDATE SET remaining=excluded.remaining", (name, times))

    def ledger(self) -> list[dict[str, Any]]:
        with self._db() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM calls ORDER BY seq")]

    def write_count(self) -> int:
        return sum(1 for c in self.ledger() if c["op"] not in ("query", "get", "events", "doc_get") and
                   c["outcome"] != "rejected")

    # 工作项 -----------------------------------------------------------------
    def query(self, idem_key: str) -> dict[str, Any] | None:
        with self._db() as conn:
            self._call(conn, "query", idem_key, {"idem_key": idem_key}, "ok")
            row = conn.execute("SELECT * FROM items WHERE idem_key=?", (idem_key,)).fetchone()
            return _item(row) if row else None

    def get(self, item_id: str) -> dict[str, Any]:
        with self._db() as conn:
            self._call(conn, "get", item_id, {}, "ok")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not row:
                raise RemoteFailed(f"工作项不存在: {item_id}")
            return _item(row)

    def create(self, fields: dict[str, Any], idem_key: str) -> dict[str, Any]:
        with self._db() as conn:
            if self._take_fault(conn, "rate_limited"):
                self._call(conn, "create", idem_key, fields, "rate_limited")
                raise RateLimited("平台限流")
            row = conn.execute("SELECT * FROM items WHERE idem_key=?", (idem_key,)).fetchone()
            if row:  # 平台支持幂等键：同键返回已有对象
                self._call(conn, "create", idem_key, fields, "idempotent_hit")
                return _item(row)
            iid = f"LAB-{conn.execute('SELECT COUNT(*) FROM items').fetchone()[0] + 1:04d}"
            conn.execute("INSERT INTO items(id, idem_key, fields, status, revision, attachments, created_at) "
                         "VALUES(?,?,?,?,?,?,?)", (iid, idem_key, json.dumps(fields, ensure_ascii=False), "OPEN", 1,
                                                   "[]", clock.now_iso()))
            conn.execute("INSERT INTO events(event_id, item_id, status, revision, created_at) VALUES(?,?,?,?,?)",
                         (new_id("PEV"), iid, "OPEN", 1, clock.now_iso()))
            self._call(conn, "create", idem_key, fields, "ok")
            lost = self._take_fault(conn, "create_succeeded_response_lost")
        if lost:
            raise OutcomeUnknown("创建已提交但响应丢失")
        return self.get(iid)

    def upload(self, item_id: str, name: str, data: bytes, idem_key: str) -> str:
        with self._db() as conn:
            row = conn.execute("SELECT id FROM files WHERE idem_key=?", (idem_key,)).fetchone()
            if row:
                self._call(conn, "upload", item_id, {"name": name}, "idempotent_hit")
                return row["id"]
            fid = new_id("FILE")
            conn.execute("INSERT INTO files(id, item_id, sha256, name, idem_key) VALUES(?,?,?,?,?)",
                         (fid, item_id, sha256_bytes(data), name, idem_key))
            self._call(conn, "upload", item_id, {"name": name, "sha256": sha256_bytes(data)}, "ok")
            return fid

    def link(self, item_id: str, file_id: str) -> None:
        with self._db() as conn:
            if self._take_fault(conn, "attachment_uploaded_link_failed"):
                self._call(conn, "link", item_id, {"file_id": file_id}, "failed")
                raise RemoteFailed("附件关联失败")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not row:
                raise RemoteFailed("工作项不存在")
            atts = json.loads(row["attachments"])
            if file_id not in atts:
                atts.append(file_id)
                conn.execute("UPDATE items SET attachments=?, revision=revision+1 WHERE id=?",
                             (json.dumps(atts), item_id))
            self._call(conn, "link", item_id, {"file_id": file_id}, "ok")

    def transition(self, item_id: str, status: str, actor: str = "qa") -> dict[str, Any]:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not row:
                raise RemoteFailed("工作项不存在")
            if self._take_fault(conn, "unknown_status"):
                status = "QA_HOLD"
            rev = row["revision"] + 1
            conn.execute("UPDATE items SET status=?, revision=? WHERE id=?", (status, rev, item_id))
            eid = new_id("PEV")
            conn.execute("INSERT INTO events(event_id, item_id, status, revision, created_at) VALUES(?,?,?,?,?)",
                         (eid, item_id, status, rev, clock.now_iso()))
            if self._take_fault(conn, "duplicate_event"):
                conn.execute("INSERT INTO events(event_id, item_id, status, revision, created_at) VALUES(?,?,?,?,?)",
                             (eid, item_id, status, rev, clock.now_iso()))
            self._call(conn, "transition", item_id, {"status": status, "actor": actor}, "ok")
        return self.get(item_id)

    def developer_resolve(self, item_id: str, fix_commit: str, fix_build: str) -> dict[str, Any]:
        """实验中的“研发修复”：平台侧把工作项置为 RESOLVED 并写修复信息。"""
        with self._db() as conn:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            fields = json.loads(row["fields"])
            fields.update({"fix_commit": fix_commit, "fix_build": fix_build})
            conn.execute("UPDATE items SET fields=? WHERE id=?", (json.dumps(fields, ensure_ascii=False), item_id))
        self.transition(item_id, "IN_PROGRESS", actor="lab-developer")
        return self.transition(item_id, "RESOLVED", actor="lab-developer")

    def events(self, since: int = 0) -> list[dict[str, Any]]:
        with self._db() as conn:
            self._call(conn, "events", "*", {"since": since}, "ok")
            rows = [dict(r) for r in conn.execute("SELECT * FROM events WHERE seq>? ORDER BY seq", (since,))]
            if rows and self._take_fault(conn, "out_of_order_event"):
                rows = list(reversed(rows))
            return rows

    # 文档 -----------------------------------------------------------------
    def doc_create(self, space: str, title: str, content: str, idem_key: str) -> dict[str, Any]:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM docs WHERE idem_key=?", (idem_key,)).fetchone()
            if row:
                self._call(conn, "doc_create", space, {"title": title}, "idempotent_hit")
                return dict(row)
            did = new_id("DOC")
            partial = self._take_fault(conn, "partial_document_write")
            body = content[: len(content) // 2] if partial else content
            etag = sha256_bytes(body.encode())[:16]
            conn.execute("INSERT INTO docs(id, space, title, content, etag, complete, idem_key) VALUES(?,?,?,?,?,?,?)",
                         (did, space, title, body, etag, 0 if partial else 1, idem_key))
            self._call(conn, "doc_create", space, {"title": title}, "partial" if partial else "ok")
        if partial:
            raise RemoteFailed("文档部分写入")
        return self.doc_get(did)

    def doc_get(self, doc_id: str) -> dict[str, Any]:
        with self._db() as conn:
            self._call(conn, "doc_get", doc_id, {}, "ok")
            row = conn.execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone()
            if not row:
                raise RemoteFailed("文档不存在")
            return dict(row)

    def doc_update(self, doc_id: str, content: str, if_match: str) -> dict[str, Any]:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone()
            if not row:
                raise RemoteFailed("文档不存在")
            if self._take_fault(conn, "etag_conflict") or row["etag"] != if_match:
                self._call(conn, "doc_update", doc_id, {"if_match": if_match}, "rejected")
                raise RemoteFailed("ETag 冲突：远端文档已被修改")
            etag = sha256_bytes(content.encode())[:16]
            conn.execute("UPDATE docs SET content=?, etag=?, complete=1 WHERE id=?", (content, etag, doc_id))
            self._call(conn, "doc_update", doc_id, {"if_match": if_match}, "ok")
        return self.doc_get(doc_id)

    def notify(self, body: str, idem_key: str) -> str:
        with self._db() as conn:
            row = conn.execute("SELECT id FROM notifications WHERE idem_key=?", (idem_key,)).fetchone()
            if row:
                self._call(conn, "notify", "*", {"idem": idem_key}, "idempotent_hit")
                return row["id"]
            nid = new_id("NTF")
            conn.execute("INSERT INTO notifications(id, idem_key, body, created_at) VALUES(?,?,?,?)",
                         (nid, idem_key, body, clock.now_iso()))
            self._call(conn, "notify", "*", {"idem": idem_key}, "ok")
            return nid

    def approval_revoked(self) -> bool:
        with self._db() as conn:
            return self._take_fault(conn, "approval_revoked")


def _item(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "idem_key": row["idem_key"], "fields": json.loads(row["fields"]), "status": row["status"],
            "revision": row["revision"], "attachments": json.loads(row["attachments"]), "created_at": row["created_at"]}
