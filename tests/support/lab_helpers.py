"""批次 M4 的辅助：独立退款参考模型（oracle）、工作区驱动与受控脚本变体。

约束：不导入 qa_flow；预期值来自 lab-fixtures.json 与本文件的纯 Python 参考模型。
产品只通过公共 CLI 调用；数据库与证据文件只读观测。
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import flow
from .cli import QA, REPO, Result, sha256_file, write_json

STORY = "退款实验（合成数据）"
# 产品 `api lab deploy --build` 使用的构建编号 → 夹具中的构建名称
BUILD_IDS = {"build1": "buggy_duplicate", "build2": "fixed", "build3": "buggy_neighbor"}
ALL_CASES = ["SMOKE-1", "SMOKE-2", "C1", "C2", "C3", "C4", "C5"]
# 固定期望表（来自夹具的构建说明，方案 §4.4）：各构建上应当暴露缺陷的用例
EXPECTED_FAILING = {"buggy_duplicate": {"C1", "C3"}, "fixed": set(), "buggy_neighbor": {"C2"}}
PRODUCT_SERVICE = REPO / "src" / "qa_flow" / "resources" / "lab" / "scripts" / "refund_service.py"
PRODUCT_CASES_SCRIPT = REPO / "src" / "qa_flow" / "resources" / "lab" / "scripts" / "refund_cases.py"
LAB_SCRIPT_PATH = "lab/refund_cases.py"


# ---------------------------------------------------------------------------
# 独立参考模型（方案 §4.2、§4.3）
# ---------------------------------------------------------------------------

class RefundOracle:
    """合成退款规则的纯 Python 参考模型。金额为整数分。"""

    def __init__(self, orders: list[dict[str, Any]]):
        self.orders = {o["id"]: {"paid": o["paid"], "refunded": o["refunded"]} for o in orders}
        self.refunds: dict[str, dict[str, Any]] = {}
        self.ledger: dict[str, str] = {}
        self._seq = 0

    def refund(self, order: str, amount: Any, key: str, role: str,
               fail_after_balance: bool = False) -> tuple[int, str | None]:
        if role != "refund_operator":
            return 403, None
        if not isinstance(amount, int) or isinstance(amount, bool):
            return 400, None
        if key in self.refunds:
            r = self.refunds[key]
            return (200, r["id"]) if (r["order"], r["amount"]) == (order, amount) else (409, None)
        o = self.orders.get(order)
        if o is None:
            return 404, None
        if amount <= 0 or amount > o["paid"] - o["refunded"]:
            return 400, None
        if fail_after_balance:
            return 500, None  # 原子性：余额与记录均不变
        self._seq += 1
        rid = f"ORACLE-R{self._seq}"
        o["refunded"] += amount
        self.refunds[key] = {"id": rid, "order": order, "amount": amount}
        return 201, rid

    def callback(self, event_id: str, refund_id: str) -> int:
        if event_id in self.ledger:
            return 200
        self.ledger[event_id] = refund_id
        return 201

    def observe(self, order: str = "O100") -> dict[str, int]:
        return {"refunded": self.orders[order]["refunded"],
                "records": sum(1 for r in self.refunds.values() if r["order"] == order),
                "ledger_entries": len(self.ledger)}


def oracle_observation(case: str, fixtures: dict[str, Any]) -> dict[str, Any]:
    """按参考模型推演各合成用例应观测到的值。键名沿用冻结用例设计中的观测键。"""
    rt = fixtures["refund_target"]
    m = RefundOracle(rt["orders"])
    op = "refund_operator"
    if case == "SMOKE-1":
        st, _ = m.refund("O100", 3000, "K1", op)
        o = m.observe()
        return {"status": st, "refunded_after": o["refunded"], "records_after": o["records"]}
    if case == "SMOKE-2":
        st, _ = m.refund("O100", 0, "K3", op)
        o = m.observe()
        return {"status": st, "refunded_after": o["refunded"], "records_after": o["records"]}
    if case == "C1":
        s1, r1 = m.refund("O100", 3000, "K1", op)
        s2, r2 = m.refund("O100", 3000, "K1", op)
        o = m.observe()
        return {"first_status": s1, "second_status": s2, "same_refund_id": r1 is not None and r1 == r2,
                "refunded_after": o["refunded"], "records_after": o["records"]}
    if case == "C2":
        st, _ = m.refund("O100", 1000, "K9", "read_only")
        o = m.observe()
        return {"status": st, "refunded_after": o["refunded"], "records_after": o["records"]}
    if case == "C3":
        statuses, trace = [], []
        for step in rt["example_sequence"]:
            st, _ = m.refund(step["order"], step["amount"], step["key"], op)
            statuses.append(st)
            trace.append(m.observe()["refunded"])
        return {"statuses": statuses, "refunded_trace": trace, "records_after": m.observe()["records"]}
    if case == "C4":
        cc = rt["concurrency_case"]
        res = [m.refund(cc["initial_order"], r["amount"], r["key"], op)[0] for r in cc["requests"]]
        o = m.observe()
        return {"success_count": sum(1 for s in res if s == 201), "refunded_after": o["refunded"],
                "records_after": o["records"]}
    if case == "C5":
        sf, _ = m.refund("O100", 2000, "K5", op, fail_after_balance=True)
        after_fail = m.observe()
        so, rid = m.refund("O100", 2000, "K6", op)
        c1 = m.callback("EV-1", rid or "")
        c2 = m.callback("EV-1", rid or "")
        return {"failed_status": sf, "refunded_after_failure": after_fail["refunded"],
                "records_after_failure": after_fail["records"], "ok_status": so, "callback_statuses": [c1, c2],
                "ledger_entries": m.observe()["ledger_entries"]}
    raise KeyError(case)


# ---------------------------------------------------------------------------
# 独立观测
# ---------------------------------------------------------------------------

def refund_db_state(target_dir: Path) -> dict[str, Any]:
    """只读读取实验退款库，返回订单、退款记录与回调账本。"""
    conn = sqlite3.connect(f"file:{Path(target_dir) / 'refund.db'}?mode=ro", uri=True)
    try:
        orders = {r[0]: {"paid": r[1], "refunded": r[2], "state": r[3]}
                  for r in conn.execute("SELECT id, paid, refunded, state FROM orders ORDER BY id")}
        refunds = [{"id": r[0], "key": r[1], "order": r[2], "amount": r[3]}
                   for r in conn.execute("SELECT id, idem_key, order_id, amount FROM refunds ORDER BY rowid")]
        ledger = [{"event_id": r[0], "refund_id": r[1]} for r in conn.execute("SELECT event_id, refund_id FROM ledger")]
        return {"orders": orders, "refunds": refunds, "ledger": ledger}
    finally:
        conn.close()


def initial_orders(fixtures: dict[str, Any]) -> dict[str, Any]:
    return {o["id"]: {"paid": o["paid"], "refunded": o["refunded"], "state": o["state"]}
            for o in fixtures["refund_target"]["orders"]}


def envelope(qa: QA, snap_id: str) -> dict[str, Any]:
    rows = qa.query("SELECT CAST(canonical AS TEXT) AS c, origin, sha256 FROM snapshots WHERE id=?", (snap_id,))
    assert rows, f"快照不存在：{snap_id}"
    env = json.loads(rows[0]["c"])
    env["_origin"], env["_sha256"] = rows[0]["origin"], rows[0]["sha256"]
    return env


def snapshots(qa: QA, kind: str) -> list[dict[str, Any]]:
    rows = qa.query("SELECT id FROM snapshots WHERE kind=? ORDER BY rowid", (kind,))
    return [envelope(qa, r["id"]) for r in rows]


def attempts(qa: QA) -> list[dict[str, Any]]:
    """全部用例尝试（按登记顺序）：payload 加上 id、origin、producer。"""
    out = []
    for e in snapshots(qa, "case_attempt"):
        out.append({**e["payload"], "id": e["id"], "origin": e["_origin"], "producer": e["producer"],
                    "depends_on": e["depends_on"]})
    return out


def evidence_row(qa: QA, ev_id: str) -> dict[str, Any]:
    rows = qa.query("SELECT * FROM evidence WHERE id=?", (ev_id,))
    assert rows, f"证据不存在：{ev_id}"
    return rows[0]


def evidence_bytes(qa: QA, ev_id: str) -> bytes:
    row = evidence_row(qa, ev_id)
    return (qa.workspace() / row["rel_path"]).read_bytes()


def receipt(qa: QA, receipt_id: str) -> dict[str, Any]:
    rows = qa.query("SELECT * FROM worker_receipts WHERE id=?", (receipt_id,))
    assert rows, f"worker 回执不存在：{receipt_id}"
    return {**rows[0], "data": json.loads(rows[0]["data"])}


def event_types(qa: QA, since_seq: int = 0) -> list[str]:
    return [r["event_type"] for r in qa.query("SELECT event_type FROM events WHERE seq>? ORDER BY seq", (since_seq,))]


def last_seq(qa: QA) -> int:
    return qa.query("SELECT COALESCE(MAX(seq),0) AS s FROM events")[0]["s"]


def count(qa: QA, kind: str) -> int:
    return qa.query("SELECT COUNT(*) AS n FROM snapshots WHERE kind=?", (kind,))[0]["n"]


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# ---------------------------------------------------------------------------
# 工作区驱动（只用公共 CLI）
# ---------------------------------------------------------------------------

@dataclass
class Lab:
    qa: QA
    ws: Path

    @property
    def target(self) -> Path:
        return self.ws / "lab" / "target"


def set_budgets(qa: QA, **budgets: Any) -> None:
    """由管理员命令修改实验配置的资源限额（在推进流程之前调用）。"""
    ws = qa.workspace()
    cfg = json.loads((ws / "config" / "qa-flow.json").read_text(encoding="utf-8"))
    cfg["budgets"].update(budgets)
    f = write_json(qa.cwd / "lab-config-budgets.json", cfg)
    r = qa.ok("admin", "setup", "--config", str(f), "--name", ws.name, "--apply")
    assert r.result["applied"] is True, r.describe()


def prepare(qa: QA, **budgets: Any) -> Lab:
    """建立合成实验并推进到“计划已批准、用例已质检、尚未实测环境”的状态。

    不调用 resume 进入执行链路：环境实测、冒烟与执行由各测试显式触发。
    """
    qa.ok("demo")
    if budgets:
        set_budgets(qa, **budgets)
    flow.drive(qa, until=flow.until_stage("case_qc"), start_demo=False)
    flow.answer_mine(qa)
    flow.transfer_others(qa)
    for stage in ("test_plan", "case_design", "case_qc"):
        st = evaluate(qa, stage)
        assert st["status"] == "completed", f"前置阶段 {stage} 应已完成：{st}"
    return Lab(qa, qa.workspace())


def evaluate(qa: QA, stage: str) -> dict[str, Any]:
    return qa.api_ok("stage", "evaluate", "--story", STORY, "--stage", stage).result


def stage_statuses(qa: QA) -> dict[str, str]:
    return qa.api_ok("stage", "status", "--story", STORY).result["statuses"]


def deploy(qa: QA, build_id: str) -> Result:
    r = qa.api_ok("lab", "deploy", "--story", STORY, "--build", build_id, "--dispatch")
    assert r.result["state"] == "succeeded", r.describe()
    return r


def register_script(qa: QA, file: Path, tcs: list[str], path: str, repo: str = "lab-repo",
                    commit: str = "lab-commit-sr") -> Result:
    args = ["script", "register", "--story", STORY, "--file", str(file), "--repo", repo, "--commit", commit,
            "--path", path]
    for t in tcs:
        args += ["--tc", t]
    return qa.api(*args)


def review_script(qa: QA, script_ref: str, result: str = "approve", reason: str = "合成实验脚本评审") -> Result:
    return qa.api("script", "review", "--story", STORY, "--script", script_ref, "--result", result, "--reason", reason)


def reviewed_script(qa: QA, file: Path, tcs: list[str], path: str, commit: str = "lab-commit-sr") -> str:
    """登记脚本并由本机评审人批准，返回 script_ref。"""
    r = register_script(qa, file, tcs, path, commit=commit)
    assert r.rc == 0, r.describe()
    ref = r.result["script_ref"]
    rv = review_script(qa, ref)
    assert rv.rc == 0, rv.describe()
    return ref


def install_product_script(qa: QA) -> str:
    """登记产品随包的实验用例脚本（内容取自仓库文件，仅作为字节读取）并评审。"""
    return reviewed_script(qa, PRODUCT_CASES_SCRIPT, ALL_CASES, LAB_SCRIPT_PATH, commit="lab-pkg")


def env_check(qa: QA, dataset: str = "DS1") -> Result:
    return qa.api("environment", "check", "--story", STORY, "--dataset", dataset)


def run_cases(qa: QA, cases: list[str], purpose: str = "execution", script: str | None = None,
              timeout: float = 180) -> Result:
    args = ["execution", "plan", "--story", STORY, "--cases", ",".join(cases), "--purpose", purpose, "--dispatch"]
    if script:
        args += ["--script", script]
    return qa.api(*args, timeout=timeout)


def execution_status(qa: QA) -> dict[str, Any]:
    return qa.api_ok("execution", "status", "--story", STORY).result


def ready_for_cases(qa: QA, build_id: str | None = None, **budgets: Any) -> Lab:
    """prepare + 可选部署 + 登记评审随包脚本。"""
    lab = prepare(qa, **budgets)
    if build_id:
        deploy(qa, build_id)
    install_product_script(qa)
    return lab


def observation_of(qa: QA, attempt: dict[str, Any]) -> dict[str, Any] | None:
    """从登记的证据文件读取该尝试的实际观测（不使用框架的 pass 字段）。"""
    raw = evidence_bytes(qa, attempt["observations_ref"])
    try:
        return json.loads(raw.decode("utf-8")).get("observation")
    except (ValueError, AttributeError):
        return None


def build_matrix(qa: QA) -> dict[str, Any]:
    """在同一工作区依次部署三个构建并经受控 worker 执行全部合成用例。

    返回 {构建名: {"attempts": {tc: 尝试}, "status": execution status, "target_sha": 服务文件摘要}}。
    """
    lab = ready_for_cases(qa)
    out: dict[str, Any] = {"lab": lab, "builds": {}}
    for build_id, name in BUILD_IDS.items():
        deploy(qa, build_id)
        before = {a["id"] for a in attempts(qa)}
        r = run_cases(qa, ALL_CASES, purpose="regression")
        assert r.rc == 0 and r.result["state"] == "succeeded", r.describe()
        new = [a for a in attempts(qa) if a["id"] not in before]
        out["builds"][name] = {"build_id": build_id, "attempts": {a["tc_ref"]: a for a in new},
                               "observations": {a["tc_ref"]: observation_of(qa, a) for a in new},
                               "status": execution_status(qa),
                               "service_sha256": sha256_file(lab.target / "refund_service.py"),
                               "version": json.loads((lab.target / "version.json").read_text(encoding="utf-8")),
                               "db_after": refund_db_state(lab.target)}
    return out


# ---------------------------------------------------------------------------
# 受控脚本变体（由测试登记并评审后交给产品的受控 worker 执行）
# ---------------------------------------------------------------------------

_PRELUDE = r'''
"""合成实验脚本变体（验收用，synthetic）。只输出观测值。"""
import argparse, importlib.util, json, os, sqlite3, subprocess, sys, time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--target-dir", required=True)
ap.add_argument("--case")
ap.add_argument("--dataset", default="DS1")
ap.add_argument("--probe", action="store_true")
a = ap.parse_args()
d = Path(a.target_dir)


def service():
    spec = importlib.util.spec_from_file_location("lab_sut", d / "refund_service.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.RefundService(d)


def observe(order="O100"):
    conn = sqlite3.connect(f"file:{d / 'refund.db'}?mode=ro", uri=True)
    try:
        return {"refunded": conn.execute("SELECT refunded FROM orders WHERE id=?", (order,)).fetchone()[0],
                "records": conn.execute("SELECT COUNT(*) FROM refunds WHERE order_id=?", (order,)).fetchone()[0]}
    finally:
        conn.close()


def emit(obs):
    sys.stdout.write(json.dumps({"case": a.case, "dataset": a.dataset, "observation": obs}))
    sys.stdout.flush()

'''

_BODIES = {
    # SMOKE-1 的请求打到已全额退款的 O200：真实执行、真实失败
    "smoke1_wrong_order": r'''
s = service(); s.reset(a.dataset)
st, _ = s.refund("O200", 3000, "K1", "refund_operator")
o = observe()
s.reset(a.dataset)
emit({"status": st, "refunded_after": o["refunded"], "records_after": o["records"]})
''',
    # 退出码 0，但不产生任何结果
    "no_result": r'''
sys.exit(0)
''',
    # 退出码 0，只自述 pass，没有观测值
    "claims_pass": r'''
sys.stdout.write(json.dumps({"case": a.case, "outcome": "pass", "result": "pass"}))
sys.exit(0)
''',
    # 同条件不稳定：首次运行时脚本自身时序缺陷导致第二次请求带错金额，之后恢复正常
    "flaky_c1": r'''
marker = d / "flaky-first-run.marker"
first = not marker.exists()
marker.write_text("seen")
s = service(); s.reset(a.dataset)
s1, b1 = s.refund("O100", 3000, "K1", "refund_operator")
s2, b2 = s.refund("O100", 4000 if first else 3000, "K1", "refund_operator")
o = observe()
s.reset(a.dataset)
emit({"first_status": s1, "second_status": s2,
      "same_refund_id": b1.get("refund_id") is not None and b1.get("refund_id") == b2.get("refund_id"),
      "refunded_after": o["refunded"], "records_after": o["records"]})
''',
    # 正常的 C1/C2/SMOKE 实现；运行时留下标记文件，用于证明该版本是否被执行
    "marked_cases": r'''
(d / "__MARKER__").write_text(a.case or "")
s = service(); s.reset(a.dataset)
if a.case == "C1":
    s1, b1 = s.refund("O100", 3000, "K1", "refund_operator")
    s2, b2 = s.refund("O100", 3000, "K1", "refund_operator")
    o = observe()
    obs = {"first_status": s1, "second_status": s2,
           "same_refund_id": b1.get("refund_id") is not None and b1.get("refund_id") == b2.get("refund_id"),
           "refunded_after": o["refunded"], "records_after": o["records"]}
else:
    st, _ = s.refund("O100", 1000, "K9", "read_only")
    o = observe()
    obs = {"status": st, "refunded_after": o["refunded"], "records_after": o["records"]}
s.reset(a.dataset)
emit(obs)
''',
    # 恶意脚本：读环境变量与凭证文件、连接未登记地址、创建孙进程后挂起
    "malicious": r'''
import socket
report = {"pid": os.getpid(), "env_keys": sorted(os.environ), "env_secret_visible": "__SENTINEL__" in json.dumps(dict(os.environ))}
try:
    report["credential_file_readable"] = "__SENTINEL__" in Path(r"__CRED_FILE__").read_text()
except OSError as exc:
    report["credential_file_readable"] = False
    report["credential_file_error"] = type(exc).__name__
try:
    c = socket.create_connection(("127.0.0.1", __PORT__), timeout=3)
    c.sendall(b"exfil")
    c.close()
    report["network_connected"] = True
except OSError as exc:
    report["network_connected"] = False
    report["network_error"] = type(exc).__name__
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=__DETACH__)
report["grandchild_pid"] = child.pid
(d / "malicious-report.json").write_text(json.dumps(report))
emit({"status": 400, "refunded_after": 0, "records_after": 0})
time.sleep(300)
''',
    # 先输出一份完整且“看起来通过”的结果，再用空白把输出撑过限额
    "big_output": r'''
s = service(); s.reset(a.dataset)
st, _ = s.refund("O100", 0, "K3", "refund_operator")
o = observe()
s.reset(a.dataset)
(d / "big-output-pid.txt").write_text(str(os.getpid()))
emit({"status": st, "refunded_after": o["refunded"], "records_after": o["records"]})
pad = b" " * 1024
for _ in range(__PAD_KB__):
    os.write(1, pad)
''',
    # 输出完整结果后超出动作时限
    "slow": r'''
s = service(); s.reset(a.dataset)
st, _ = s.refund("O100", 0, "K3", "refund_operator")
o = observe()
s.reset(a.dataset)
(d / "slow-pid.txt").write_text(str(os.getpid()))
emit({"status": st, "refunded_after": o["refunded"], "records_after": o["records"]})
time.sleep(300)
''',
}


def write_variant(directory: Path, name: str, filename: str | None = None, **subst: Any) -> Path:
    """把脚本变体写入测试临时目录。subst 用于替换 __KEY__ 占位符。"""
    body = _BODIES[name]
    for k, v in subst.items():
        body = body.replace(f"__{k}__", str(v))
    p = Path(directory) / (filename or f"{name}.py")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_PRELUDE + body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 环境实测（经日常入口规划，再显式派发）
# ---------------------------------------------------------------------------

def plan_probe_via_resume(qa: QA) -> str:
    """让日常入口 resume 登记随包脚本并规划环境探测，在探测计划提交后用测试钩子停住。

    返回尚未执行的探测操作 ID。之后由测试用 api action dispatch 显式执行，
    以便在探测前后做独立观测或注入环境偏差。
    """
    dep = snapshots(qa, "deployment")[-1]["id"]
    r = qa.test_mode(QA_FLOW_CRASH_AT=f"after_commit:action.plan:probe:{dep}").run("resume")
    assert r.rc == 99, "resume 应在探测计划提交后被测试钩子终止\n" + r.describe()
    acts = qa.api_ok("action", "list", "--story", STORY).result["actions"]
    probes = [a for a in acts if a["kind"] == "environment.probe" and a["state"] == "ready"]
    assert len(probes) == 1, f"应恰有一个待执行的环境探测：{acts}"
    return probes[0]["id"]


def ready_env(qa: QA, build_id: str | None = None, **budgets: Any) -> Lab:
    """prepare + 可选部署 + 实测环境，并确认 env_ready 已完成。"""
    lab = prepare(qa, **budgets)
    if build_id:
        deploy(qa, build_id)
    act = plan_probe_via_resume(qa)
    r = qa.api("action", "dispatch", "--id", act)
    assert r.rc == 0 and r.result["state"] == "succeeded", r.describe()
    st = evaluate(qa, "env_ready")
    assert st["status"] == "completed", f"前置条件：环境应实测就绪：{st}"
    return lab


# ---------------------------------------------------------------------------
# 夹具工厂的加载、直接观测与进程核对
# ---------------------------------------------------------------------------

def make_qa(base: Path) -> QA:
    """在给定临时目录下建立独立的工作区根、HOME 与工作目录（供 module 级夹具使用）。"""
    root, home, cwd = base / "工作区 根", base / "home", base / "cwd"
    for d in (root, home, cwd):
        d.mkdir(parents=True, exist_ok=True)
    return QA(root=root, home=home, cwd=cwd)


def load_factory():
    """按文件路径加载 tests/fixtures/build_lab.py（避免与同名第三方包冲突）。"""
    import importlib.util
    import sys

    name = "lab_fixture_factory"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO / "tests" / "fixtures" / "build_lab.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def direct_observation(ds: Any, case: str, fixtures: dict[str, Any]) -> dict[str, Any]:
    """在工厂的隔离数据集上直接驱动被测服务，并用独立只读连接观测，返回与用例观测键同形的结果。"""
    rt = fixtures["refund_target"]
    ds.reset()
    if case in ("SMOKE-1", "SMOKE-2", "C2"):
        amount, key, role = {"SMOKE-1": (3000, "K1", "refund_operator"), "SMOKE-2": (0, "K3", "refund_operator"),
                             "C2": (1000, "K9", "read_only")}[case]
        st = ds.refund("O100", amount, key, role)["status"]
        o = ds.observe()
        return {"status": st, "refunded_after": o["refunded"], "records_after": o["records"]}
    if case == "C1":
        r1 = ds.refund("O100", 3000, "K1")
        r2 = ds.refund("O100", 3000, "K1")
        o = ds.observe()
        id1, id2 = (r1["body"] or {}).get("refund_id"), (r2["body"] or {}).get("refund_id")
        return {"first_status": r1["status"], "second_status": r2["status"], "same_refund_id": id1 is not None and id1 == id2,
                "refunded_after": o["refunded"], "records_after": o["records"]}
    if case == "C3":
        statuses, trace = [], []
        for step in rt["example_sequence"]:
            statuses.append(ds.refund(step["order"], step["amount"], step["key"])["status"])
            trace.append(ds.observe()["refunded"])
        return {"statuses": statuses, "refunded_trace": trace, "records_after": ds.observe()["records"]}
    if case == "C4":
        cc = rt["concurrency_case"]
        outs = ds.concurrent([{"order": cc["initial_order"], "amount": r["amount"], "key": r["key"],
                               "role": "refund_operator"} for r in cc["requests"]])
        o = ds.observe()
        return {"success_count": sum(1 for x in outs if x["status"] == 201), "refunded_after": o["refunded"],
                "records_after": o["records"]}
    if case == "C5":
        rf = ds.refund("O100", 2000, "K5", fail_after_balance=True)
        after_fail = ds.observe()
        ok = ds.refund("O100", 2000, "K6")
        rid = (ok["body"] or {}).get("refund_id", "")
        c1 = ds.callback("EV-1", rid)["status"]
        c2 = ds.callback("EV-1", rid)["status"]
        return {"failed_status": rf["status"], "refunded_after_failure": after_fail["refunded"],
                "records_after_failure": after_fail["records"], "ok_status": ok["status"],
                "callback_statuses": [c1, c2], "ledger_entries": ds.observe()["ledger_entries"]}
    raise KeyError(case)


def wait_dead(pid: int, timeout_s: float = 10.0) -> bool:
    """有界等待进程消失（被杀进程由系统异步回收）；返回是否已消失。等待的是条件而不是猜先后。"""
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return not pid_alive(pid)


def stage_events(qa: QA, stage: str, event_type: str) -> list[dict[str, Any]]:
    return qa.query("SELECT seq, event_type FROM events WHERE object_id LIKE ? AND event_type=? ORDER BY seq",
                    (f"stage:%:{stage}", event_type))


def build_report(qa: QA) -> dict[str, Any]:
    return qa.api_ok("report", "build", "--story", STORY).result
