"""目标白名单、受保护原文与工作区位置检查。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from qa_flow.errors import QAError

# 写通道：正文、标题、权限、移动/删除、归档覆盖
WRITE_CHANNELS = {"body", "title", "permission", "move", "delete", "archive_overwrite"}

_SYNC_MARKERS = ("Mobile Documents", "iCloud", "Dropbox", "OneDrive", "Google Drive", "GoogleDrive", "Box Sync")
_NETWORK_FS = {"nfs", "smbfs", "afpfs", "cifs", "webdav", "smb", "fuse.sshfs"}


def check_workspace_location(path: Path) -> list[str]:
    """拒绝初始化到网络共享或同步网盘位置。检测基于路径与挂载类型，是启发式检查。"""
    problems = []
    resolved = Path(path).resolve()
    text = str(resolved)
    if any(m in text for m in _SYNC_MARKERS):
        problems.append("工作区位于同步网盘目录，SQLite 不能在此并发使用")
    fstype = _fs_type(resolved)
    if fstype and fstype.lower() in _NETWORK_FS:
        problems.append(f"工作区位于网络文件系统（{fstype}）")
    return problems


def _fs_type(path: Path) -> str | None:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        import subprocess

        out = subprocess.run(["/sbin/mount"], capture_output=True, text=True, timeout=5, shell=False).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    best, fstype = "", None
    for line in out.splitlines():
        # 形如：/dev/disk3s1 on /System/Volumes/Data (apfs, local, journaled)
        if " on " not in line or "(" not in line:
            continue
        mount_point = line.split(" on ", 1)[1].split(" (", 1)[0]
        kind = line.rsplit("(", 1)[1].split(",")[0].strip(" )")
        if str(probe).startswith(mount_point) and len(mount_point) > len(best):
            best, fstype = mount_point, kind
    return fstype


def resolve_identity(uri: str) -> str:
    """把资源解析到统一身份：本地文件取真实路径；远端文档取规范化 URI。"""
    if "://" in uri and not uri.startswith("file://"):
        base = uri.split("#", 1)[0].split("?", 1)[0].rstrip("/")
        return base.lower()
    p = uri[len("file://"):] if uri.startswith("file://") else uri
    return "file:" + os.path.realpath(os.path.expanduser(p))


def ensure_writable(identity: str, channel: str, protected: set[str]) -> None:
    if channel not in WRITE_CHANNELS:
        raise QAError("INVALID_ARGUMENT", f"未知写通道: {channel}")
    if identity in protected:
        raise QAError("PROTECTED_RESOURCE", f"受保护原文禁止 {channel} 写入", path="target",
                      related_refs=[identity])


def resolve_target(config: dict[str, Any], target_id: str, operation: str) -> dict[str, Any]:
    """未知目标一律拒绝；不猜环境或地址；生产缺凭证明确阻断，不回退测试凭证。"""
    for t in config.get("targets", []):
        if t["id"] == target_id:
            if operation not in t.get("allowed_operations", []):
                raise QAError("UNSUPPORTED_CAPABILITY", f"目标 {target_id} 不允许操作 {operation}",
                              path="operation", related_refs=[target_id])
            if t["environment"] in ("test", "staging", "production") and not t.get("credential_ref"):
                raise QAError("CREDENTIAL_MISSING", f"目标 {target_id} 缺少凭证引用", related_refs=[target_id])
            if t["environment"] == "production" and operation not in ("release.probe", "release.metrics"):
                raise QAError("AUTHORIZATION_INVALID", "生产目标只允许只读核验，本系统不执行生产写入或回滚",
                              related_refs=[target_id])
            return t
    raise QAError("UNKNOWN_TARGET", f"目标未登记: {target_id}", path="target_id", related_refs=[target_id])
