"""备份、核验与恢复。

- 备份期间持有写锁，暂停新的写命令，等正在提交的短事务结束。
- 使用 SQLite 在线备份接口复制数据库，再复制其引用的有效证据。
- 核验数据库完整性、外键、对象摘要、事件序号和全部证据引用。
- 恢复只写入新目录，全部核验通过后才可使用；不覆盖现有工作区。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from qa_flow import clock
from qa_flow.canonical import loads_strict, sha256_bytes, sha256_file
from qa_flow.errors import QAError
from qa_flow.ids import new_id
from qa_flow.storage import db
from qa_flow.storage.store import DB_NAME, WORKSPACE_MARKER, Workspace, atomic_write

MANIFEST = "backup-manifest.json"


def create_backup(ws: Workspace, output: Path) -> dict[str, Any]:
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise QAError("IO_ERROR", f"备份目录非空，拒绝覆盖: {output}")
    if _is_within(output, ws.root):
        raise QAError("INVALID_ARGUMENT", "备份目录不能位于工作区内部")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with ws.connect() as lock, ws.connect() as conn:
        lock.execute("BEGIN IMMEDIATE")  # 持有写锁：暂停其他写命令，等正在提交的短事务结束
        conn.execute("BEGIN DEFERRED")  # 独立的只读连接作为备份源
        try:
            dst_path = output / DB_NAME
            dst = sqlite3.connect(str(dst_path))
            try:
                conn.backup(dst)
            finally:
                dst.close()
            rows = conn.execute("SELECT * FROM evidence ORDER BY id").fetchall()
            evidence = []
            for r in rows:
                item = {"id": r["id"], "sha256": r["sha256"], "rel_path": r["rel_path"], "state": r["state"],
                        "privacy": r["privacy"], "retention": r["retention"]}
                if r["state"] == "active":
                    src = ws.root / r["rel_path"]
                    if not src.is_file() or sha256_file(src) != r["sha256"]:
                        raise QAError("EVIDENCE_INVALID", f"备份时发现有效证据缺失或摘要不符: {r['id']}",
                                      related_refs=[r["id"]])
                    dst_file = output / r["rel_path"]
                    dst_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    if not dst_file.exists():
                        shutil.copy2(src, dst_file)
                        dst_file.chmod(0o600)
                else:
                    item["destroyed_at"] = r["destroyed_at"]
                    item["destroy_reason"] = r["destroy_reason"]
                evidence.append(item)
            last_seq = conn.execute("SELECT MAX(seq) AS s FROM events").fetchone()["s"] or 0
        finally:
            conn.execute("ROLLBACK")
            lock.execute("ROLLBACK")
    for name in (WORKSPACE_MARKER,):
        shutil.copy2(ws.root / name, output / name)
    if ws.config_dir.is_dir():
        shutil.copytree(ws.config_dir, output / "config", dirs_exist_ok=True)
    manifest = {
        "schema": "qa.backup_manifest",
        "id": new_id("BAK"),
        "created_at": clock.now_iso(),
        "source_workspace": ws.marker().get("name"),
        "database_sha256": sha256_file(output / DB_NAME),
        "last_event_seq": last_seq,
        "evidence": evidence,
        "config_files": sorted(
            {str(p.relative_to(output)): sha256_file(p) for p in (output / "config").rglob("*") if p.is_file()}.items()
        ) if (output / "config").is_dir() else [],
    }
    atomic_write(output / MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2).encode())
    report = verify_backup(output)
    return {"backup_dir": str(output), "manifest_id": manifest["id"], "verification": report}


def verify_backup(path: Path) -> dict[str, Any]:
    path = Path(path).resolve()
    problems: list[dict[str, Any]] = []

    def bad(kind: str, message: str, ref: str = "") -> None:
        problems.append({"kind": kind, "message": message, "ref": ref})

    manifest_path = path / MANIFEST
    if not manifest_path.is_file():
        return {"ok": False, "problems": [{"kind": "manifest", "message": "缺少备份清单", "ref": ""}]}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError:
        return {"ok": False, "problems": [{"kind": "manifest", "message": "备份清单无法解析", "ref": ""}]}
    dbp = path / DB_NAME
    if not dbp.is_file():
        bad("database", "缺少数据库文件")
        return {"ok": False, "problems": problems}
    if sha256_file(dbp) != manifest.get("database_sha256"):
        bad("database", "数据库文件摘要与清单不符")
    if not (path / WORKSPACE_MARKER).is_file():
        bad("marker", "缺少工作区标记")
    conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if ic != "ok":
            bad("integrity", f"integrity_check: {ic}")
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            bad("foreign_key", f"外键不一致 {len(fk)} 处")
        seqs = [r[0] for r in conn.execute("SELECT seq FROM events ORDER BY seq")]
        if seqs and seqs != list(range(1, len(seqs) + 1)):
            bad("events", "事件序号不连续")
        if (seqs[-1] if seqs else 0) != manifest.get("last_event_seq"):
            bad("events", "事件末序号与清单不符")
        for r in conn.execute("SELECT id, canonical, sha256 FROM snapshots"):
            if sha256_bytes(r["canonical"]) != r["sha256"]:
                bad("snapshot", "快照摘要不符", r["id"])
            else:
                try:
                    loads_strict(r["canonical"])
                except ValueError:
                    bad("snapshot", "快照规范字节无法解析", r["id"])
        db_evidence = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM evidence")}
    finally:
        conn.close()
    listed = {e["id"]: e for e in manifest.get("evidence", [])}
    for eid, row in db_evidence.items():
        item = listed.get(eid)
        if item is None:
            bad("evidence", "数据库中的证据未列入清单", eid)
            continue
        if row["state"] != item["state"]:
            bad("evidence", "证据状态与清单不符", eid)
        if row["state"] == "active":
            f = path / row["rel_path"]
            if not f.is_file():
                bad("evidence", "有效证据文件缺失", eid)
            elif sha256_file(f) != row["sha256"]:
                bad("evidence", "有效证据文件摘要不符", eid)
    for eid in listed:
        if eid not in db_evidence:
            bad("evidence", "清单中的证据不在数据库中", eid)
    return {"ok": not problems, "problems": problems, "manifest_id": manifest.get("id"),
            "evidence_count": len(db_evidence)}


def restore_backup(backup: Path, target: Path) -> dict[str, Any]:
    backup = Path(backup).resolve()
    target = Path(target).resolve()
    if target.exists() and any(target.iterdir()):
        raise QAError("IO_ERROR", f"恢复目标非空，拒绝覆盖: {target}")
    report = verify_backup(backup)
    if not report["ok"]:
        raise QAError("EVIDENCE_INVALID", "备份核验失败，未执行恢复", details={"problems": report["problems"]})
    staging = target.with_name(f".{target.name}.restore-{new_id('R')}")
    shutil.copytree(backup, staging)
    (staging / MANIFEST).unlink()
    for d in ("tmp", "exports", "evidence/raw", "evidence/redacted"):
        (staging / d).mkdir(parents=True, exist_ok=True, mode=0o700)
    # 恢复后再次核验（基于原清单）
    shutil.copy2(backup / MANIFEST, staging / MANIFEST)
    again = verify_backup(staging)
    (staging / MANIFEST).unlink()
    if not again["ok"]:
        shutil.rmtree(staging, ignore_errors=True)
        raise QAError("EVIDENCE_INVALID", "恢复副本核验失败", details={"problems": again["problems"]})
    if target.exists():
        target.rmdir()
    staging.rename(target)
    conn = db.connect(target / DB_NAME)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO meta(key, value) VALUES('restored_from', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (report.get("manifest_id") or "",))
        conn.execute("INSERT INTO meta(key, value) VALUES('restored_at', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (clock.now_iso(),))
        conn.execute(
            "INSERT INTO events(command_id, object_id, event_type, prev_rev, new_rev, payload, created_at) "
            "VALUES(NULL, 'workspace', 'restored', NULL, NULL, ?, ?)",
            (json.dumps({"manifest_id": report.get("manifest_id")}), clock.now_iso()))
        conn.execute("COMMIT")
    finally:
        conn.close()
    return {"restored_to": str(target), "verification": again}


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
