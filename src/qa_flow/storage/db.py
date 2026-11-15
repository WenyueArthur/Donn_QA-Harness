"""SQLite 连接与表结构。

采用回滚日志：journal_mode=DELETE、synchronous=EXTRA。
连接使用 autocommit=True，所有写入由显式 BEGIN IMMEDIATE 事务控制，
不依赖解释器的隐式事务默认值。短事务内不调用网络或模型。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commands(
  command_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  response TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  project_id TEXT,
  story_id TEXT,
  lineage TEXT,
  parent_id TEXT REFERENCES snapshots(id),
  canonical BLOB NOT NULL,
  sha256 TEXT NOT NULL,
  privacy TEXT NOT NULL DEFAULT 'internal',
  origin TEXT NOT NULL DEFAULT 'local',
  created_at TEXT NOT NULL,
  command_id TEXT
);
CREATE INDEX IF NOT EXISTS snapshots_story ON snapshots(story_id, kind);
CREATE INDEX IF NOT EXISTS snapshots_lineage ON snapshots(lineage);
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  command_id TEXT,
  object_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  prev_rev INTEGER,
  new_rev INTEGER,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_object ON events(object_id);
CREATE TABLE IF NOT EXISTS aggregates(
  object_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  story_id TEXT,
  revision INTEGER NOT NULL,
  state TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS aggregates_story ON aggregates(story_id, kind);
CREATE TABLE IF NOT EXISTS dependencies(
  result_id TEXT NOT NULL REFERENCES snapshots(id),
  input_id TEXT NOT NULL,
  input_sha256 TEXT NOT NULL,
  relation TEXT NOT NULL,
  required INTEGER NOT NULL,
  PRIMARY KEY(result_id, input_id, relation)
);
CREATE INDEX IF NOT EXISTS dependencies_input ON dependencies(input_id);
CREATE TABLE IF NOT EXISTS decisions(
  id TEXT PRIMARY KEY REFERENCES snapshots(id),
  story_id TEXT,
  role TEXT NOT NULL,
  actor TEXT NOT NULL,
  verification TEXT NOT NULL,
  result TEXT NOT NULL,
  plan_sha256 TEXT,
  subjects TEXT NOT NULL,
  scope TEXT NOT NULL,
  expires_at TEXT,
  revoked_at TEXT,
  revoke_reason TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions(
  id TEXT PRIMARY KEY,
  story_id TEXT,
  kind TEXT NOT NULL,
  target_id TEXT NOT NULL,
  environment TEXT NOT NULL,
  plan_ref TEXT NOT NULL REFERENCES snapshots(id),
  plan_sha256 TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  authorization_ref TEXT,
  lease_epoch INTEGER NOT NULL DEFAULT 0,
  lease_owner TEXT,
  lease_expires_at TEXT,
  state TEXT NOT NULL,
  remote_receipt TEXT,
  remote_object_id TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  revision INTEGER NOT NULL DEFAULT 1,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS actions_remote_object
  ON actions(kind, target_id, remote_object_id) WHERE remote_object_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS action_attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action_id TEXT NOT NULL REFERENCES actions(id),
  lease_epoch INTEGER NOT NULL,
  worker_id TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  outcome TEXT,
  receipt TEXT,
  late INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS worker_receipts(
  id TEXT PRIMARY KEY,
  action_id TEXT REFERENCES actions(id),
  worker_id TEXT NOT NULL,
  lease_epoch INTEGER NOT NULL,
  data TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence(
  id TEXT PRIMARY KEY,
  sha256 TEXT NOT NULL,
  rel_path TEXT,
  size INTEGER NOT NULL,
  media_type TEXT,
  origin TEXT NOT NULL,
  source TEXT,
  privacy TEXT NOT NULL DEFAULT 'internal',
  retention TEXT,
  state TEXT NOT NULL DEFAULT 'active',
  redacted_of TEXT,
  created_at TEXT NOT NULL,
  destroyed_at TEXT,
  destroy_reason TEXT
);
CREATE TABLE IF NOT EXISTS platform_events(
  event_id TEXT PRIMARY KEY,
  object_ref TEXT NOT NULL,
  platform_revision INTEGER,
  received_at TEXT NOT NULL,
  applied INTEGER NOT NULL
);
"""


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), autocommit=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
    if str(mode).lower() != "delete":
        raise RuntimeError(f"journal_mode 未能设置为 DELETE: {mode}")
    conn.execute("PRAGMA synchronous=EXTRA")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def initialize(conn: sqlite3.Connection, created_at: str) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 逐条执行，避免 executescript 的隐式提交
        for stmt in [s.strip() for s in SCHEMA.split(";") if s.strip()]:
            conn.execute(stmt)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)", (SCHEMA_VERSION,)
        )
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('created_at', ?)", (created_at,))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
