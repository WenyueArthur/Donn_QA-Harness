"""合成退款实验服务（被测对象）。仅用于验证框架，不是任何公司的真实退款规则。

构建由部署目录中的 version.json 决定：
- buggy_duplicate：同键重复请求错误地再次增加 refunded；
- fixed：执行全部合成规则；
- buggy_neighbor：修复重复退款，但错误放开 read_only 权限。
金额使用整数分。数据保存在部署目录的 SQLite 文件中。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

ORDERS = [("O100", 10000, 0, "paid"), ("O200", 10000, 10000, "refunded")]


class RefundService:
    def __init__(self, target_dir: Path):
        self.dir = Path(target_dir)
        self.version = json.loads((self.dir / "version.json").read_text(encoding="utf-8"))
        self.build = self.version["build"]
        self.db_path = self.dir / "refund.db"

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def reset(self, dataset: str = "DS1") -> None:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DROP TABLE IF EXISTS orders")
            conn.execute("DROP TABLE IF EXISTS refunds")
            conn.execute("DROP TABLE IF EXISTS ledger")
            conn.execute("DROP TABLE IF EXISTS meta")
            conn.execute("CREATE TABLE orders(id TEXT PRIMARY KEY, paid INTEGER, refunded INTEGER, state TEXT)")
            conn.execute("CREATE TABLE refunds(id TEXT PRIMARY KEY, idem_key TEXT UNIQUE, order_id TEXT,"
                         " amount INTEGER, payload TEXT)")
            conn.execute("CREATE TABLE ledger(event_id TEXT PRIMARY KEY, refund_id TEXT)")
            conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
            conn.executemany("INSERT INTO orders VALUES(?,?,?,?)", ORDERS)
            conn.execute("INSERT INTO meta VALUES('dataset', ?)", (dataset,))
            conn.execute("COMMIT")
        finally:
            conn.close()

    def refund(self, order_id: str, amount, key: str, role: str, fail_after_balance: bool = False) -> tuple[int, dict]:
        if role != "refund_operator" and self.build != "buggy_neighbor":
            return 403, {"error": "forbidden"}
        if not isinstance(amount, int) or isinstance(amount, bool):
            return 400, {"error": "amount_must_be_integer"}
        payload = json.dumps({"order": order_id, "amount": amount}, sort_keys=True)
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT id, payload FROM refunds WHERE idem_key=?", (key,)).fetchone()
            if row and self.build != "buggy_duplicate":
                conn.execute("ROLLBACK")
                if row[1] == payload:
                    return 200, {"refund_id": row[0]}
                return 409, {"error": "idempotency_conflict"}
            if row and self.build == "buggy_duplicate" and row[1] != payload:
                conn.execute("ROLLBACK")
                return 409, {"error": "idempotency_conflict"}
            o = conn.execute("SELECT paid, refunded FROM orders WHERE id=?", (order_id,)).fetchone()
            if o is None:
                conn.execute("ROLLBACK")
                return 404, {"error": "order_not_found"}
            paid, refunded = o
            if amount <= 0 or amount > paid - refunded:
                conn.execute("ROLLBACK")
                return 400, {"error": "invalid_amount"}
            rid = "R-" + uuid.uuid4().hex[:10]
            conn.execute("UPDATE orders SET refunded=refunded+? WHERE id=?", (amount, order_id))
            if fail_after_balance:
                conn.execute("ROLLBACK")
                return 500, {"error": "injected_failure"}
            store_key = key if not row else f"{key}#{rid}"
            conn.execute("INSERT INTO refunds VALUES(?,?,?,?,?)", (rid, store_key, order_id, amount, payload))
            conn.execute("COMMIT")
            return 201, {"refund_id": rid}
        except sqlite3.Error:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return 500, {"error": "storage_error"}
        finally:
            conn.close()

    def callback(self, event_id: str, refund_id: str) -> int:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            exists = conn.execute("SELECT 1 FROM ledger WHERE event_id=?", (event_id,)).fetchone()
            if exists:
                conn.execute("ROLLBACK")
                return 200
            conn.execute("INSERT INTO ledger VALUES(?, ?)", (event_id, refund_id))
            conn.execute("COMMIT")
            return 201
        finally:
            conn.close()
