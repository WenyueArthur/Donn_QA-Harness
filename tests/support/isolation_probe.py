"""独立探测本机是否具备操作系统级网络隔离（不调用产品内部函数）。

做法：在环回地址打开监听端口，用 macOS 的 sandbox-exec 以“禁止网络”规则运行一个子进程去连接它。
子进程连接被拒且监听端没有收到连接，才认为本机实际具备该隔离机制。其他平台或没有 sandbox-exec 时为 False。
只连接本测试打开的环回端口，不产生外部流量。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys

SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def os_network_sandbox_works() -> bool:
    if sys.platform != "darwin" or not os.path.exists(SANDBOX_EXEC):
        return False
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    srv.settimeout(0.5)
    port = srv.getsockname()[1]
    code = ("import socket\n"
            "try:\n"
            f"    socket.create_connection(('127.0.0.1', {port}), timeout=2).close(); print('connected')\n"
            "except OSError:\n"
            "    print('blocked')\n")
    try:
        r = subprocess.run([SANDBOX_EXEC, "-p", "(version 1)(allow default)(deny network*)", sys.executable, "-I",
                            "-c", code], capture_output=True, text=True, timeout=30)
        try:
            conn, _ = srv.accept()
            conn.close()
            accepted = True
        except OSError:
            accepted = False
        return r.returncode == 0 and r.stdout.strip() == "blocked" and not accepted
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        srv.close()
