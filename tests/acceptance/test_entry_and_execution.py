"""独立验收：环境、实际冒烟、脚本评审、断言与尝试、执行器限额、合成退款业务（批次 M4）。

场景：AC-025～AC-032、AC-108、AC-117、AC-118、AC-119。
只通过子进程调用公共 CLI；预期值来自 lab-fixtures.json 与独立参考模型（support.lab_helpers.RefundOracle）。
"""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
import time

import pytest

from support import lab_helpers as L
from support.cli import REPO, sha256_file, tree_digest, write_json

FIXTURES_PATH = REPO / "docs" / "qa-flow-rebuild" / "lab-fixtures.json"
BUILD_PARAMS = [("build1", "buggy_duplicate"), ("build2", "fixed"), ("build3", "buggy_neighbor")]
NOT_COMPLETED = ("failed", "blocked", "pending", "stale")


@pytest.fixture(scope="module")
def fixtures() -> dict:
    return json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def factory(tmp_path_factory):
    """夹具工厂（方案 §5）：来源集合、三个构建的隔离部署、独立数据集与参考模型。"""
    return L.load_factory().build(FIXTURES_PATH, tmp_path_factory.mktemp("lab-factory"))


@pytest.fixture(scope="module")
def matrix(tmp_path_factory):
    """同一工作区依次部署三个构建，经受控 worker 执行全部合成用例（只读复用）。"""
    return L.build_matrix(L.make_qa(tmp_path_factory.mktemp("lab-matrix")))


# ---------------------------------------------------------------------------
# 夹具工厂自检（AC-117、AC-118 的前置：独立数据集、重置与清理、参考模型）
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-117", "AC-118")
@pytest.mark.layer("lab")
def test_factory_receipt_and_isolation(factory, fixtures, tmp_path):
    rc = factory.receipt
    # 来源集合：四类原文，摘要与文件一致，每类 3 条参考事实
    cats = {s["category"]: s for s in rc["sources"]}
    assert set(cats) == {"explicit", "missing", "contradiction", "injection"}
    bench = fixtures["model_benchmark"]
    assert [len(cats[c]["reference_facts"]) for c in ("explicit", "missing", "contradiction", "injection")] == [
        bench["explicit_facts"], bench["missing_information"], bench["contradictions"],
        bench["prompt_injection_or_misleading_claims"]]
    for s in rc["sources"]:
        assert hashlib.sha256(open(s["path"], "rb").read()).hexdigest() == s["sha256"]
    # 三个构建：隔离目录、独立数据库、初态与夹具一致
    assert set(rc["services"]) == set(fixtures["refund_target"]["builds"])
    assert len({s["database"] for s in rc["services"].values()}) == 3
    for s in rc["services"].values():
        assert s["initial_state"]["orders"] == L.initial_orders(fixtures)
        assert s["database"].startswith(rc["root"])
        # 如实记录：产品是本地进程调用，没有环回端口
        assert s["transport"] == "local_process" and s["loopback_port"] is None
    assert "环回" in rc["transport_deviation"]

    def origins(obj):
        if isinstance(obj, dict):
            if "origin" in obj:
                yield obj["origin"]
            for v in obj.values():
                yield from origins(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from origins(v)

    assert set(origins(rc)) <= {"synthetic", "local_lab"}
    # 数据集互相隔离；清理后与初态一致并删除
    a, b = factory.dataset("iso-a", "fixed"), factory.dataset("iso-b", "fixed")
    assert a.refund("O100", 3000, "K1")["status"] == 201
    assert a.observe()["refunded"] == 3000 and b.observe()["refunded"] == 0
    assert all(e["origin"] == "local_lab" for e in a.log)
    for ds in (a, b):
        rec = factory.cleanup(ds)
        assert rec["restored"] is True and rec["removed"] is True
    # 参考模型复现夹具的固定顺序期望（§4.3）
    m = factory.oracle()
    ids = []
    for step in fixtures["refund_target"]["example_sequence"]:
        st, rid = m.refund(step["order"], step["amount"], step["key"], "refund_operator")
        ids.append(rid)
        assert st == step["expected_status"] and m.observe()["refunded"] == step["expected_refunded"]
    assert ids[0] == ids[1]
    # 命令行入口输出同样结构的回执
    p = subprocess.run([sys.executable, str(REPO / "tests" / "fixtures" / "build_lab.py"), "--fixtures",
                        str(FIXTURES_PATH), "--root", str(tmp_path)], capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    assert set(json.loads(p.stdout)) == set(rc)


# ---------------------------------------------------------------------------
# AC-025 环境与数据准备有实际核验
# ---------------------------------------------------------------------------

def _dataset_id(qa) -> str:
    ds = [d for d in L.snapshots(qa, "dataset") if d["payload"]["name"] == "DS1"]
    assert len(ds) == 1
    return ds[0]["id"]


@pytest.mark.ac("AC-025")
@pytest.mark.layer("lab")
def test_ac025_env_probe_binds_build_and_dataset(qa, home, fixtures):
    """日常入口规划的环境探测：实测结果引用 build 与 DS1，创建与清理可追溯，只接触本机实验库。"""
    lab = L.prepare(qa)
    L.deploy(qa, "build2")
    act = L.plan_probe_via_resume(qa)
    home_before = tree_digest(home)
    seq = L.last_seq(qa)
    r = qa.api("action", "dispatch", "--id", act)
    assert r.rc == 0 and r.result["state"] == "succeeded", r.describe()

    checks = L.snapshots(qa, "environment_check")
    assert len(checks) == 1
    env = checks[0]
    p = env["payload"]
    dep = L.snapshots(qa, "deployment")[-1]
    # 独立核对：目标目录实际版本为 fixed，部署记录与环境核验都指向同一构建
    version = json.loads((lab.target / "version.json").read_text(encoding="utf-8"))
    assert version["build"] == "fixed"
    assert dep["payload"]["build"] == "build2"
    assert p["build"] == {"expected": "build2", "observed": "build2"}
    assert p["target_id"] == "lab-refund"
    assert p["dataset_refs"] == [_dataset_id(qa)]
    deps = {d["relation"]: d["ref"] for d in env["depends_on"]}
    assert deps.get("context") == dep["id"] and deps.get("dataset") == _dataset_id(qa)
    assert env["producer"]["kind"] == "worker" and env["_origin"] == "local_lab"
    # 每项检查都有实测结果与工作进程证据
    kinds = {c["kind"]: c for c in p["checks"]}
    assert {"version", "data", "cleanup"} <= set(kinds)
    for c in p["checks"]:
        assert c["observed"] == "pass", c
        row = L.evidence_row(qa, c["evidence_ref"])
        assert row["origin"] == "local_lab" and row["state"] == "active"
        assert hashlib.sha256(L.evidence_bytes(qa, c["evidence_ref"])).hexdigest() == row["sha256"]
    probe = json.loads(L.evidence_bytes(qa, p["observations"][0]))["probe"]
    assert probe["version"]["observed"] == "fixed"
    assert probe["data"] == {"dataset": "DS1", "O100_refunded": 0, "records": 0}
    assert probe["cleanup"] == {"restored": True}
    assert p["cleanup_result"] == "pass"
    # 清理后的业务库与夹具初态一致（独立只读连接）
    state = L.refund_db_state(lab.target)
    assert state == {"orders": L.initial_orders(fixtures), "refunds": [], "ledger": []}
    # 操作账本：恰好一次探测，目标为本机实验资源；隔离 HOME 未被触碰
    acts = qa.api_ok("action", "list", "--story", L.STORY).result["actions"]
    probes = [a for a in acts if a["kind"] == "environment.probe"]
    assert len(probes) == 1 and probes[0]["environment"] == "lab" and probes[0]["state"] == "succeeded"
    assert tree_digest(home) == home_before
    dbs = sorted(str(f.relative_to(qa.root)) for f in qa.root.rglob("*.db"))
    assert all(d.startswith(lab.ws.name + "/") for d in dbs), dbs
    assert "action_succeeded" in L.event_types(qa, seq)
    st = L.evaluate(qa, "env_ready")
    assert (st["status"], st["verdict"]) == ("completed", "pass"), st
    assert env["id"] in st["inputs"]


@pytest.mark.ac("AC-025")
@pytest.mark.layer("lab")
def test_ac025_api_environment_check_on_fixed_build(qa):
    """机器接口 api environment check 在 build=fixed、DS1 上应得到与日常入口一致的就绪结论。"""
    lab = L.ready_for_cases(qa, "build2")
    r = L.env_check(qa, "DS1")
    assert r.rc == 0 and r.result["state"] == "succeeded", r.describe()
    p = L.snapshots(qa, "environment_check")[-1]["payload"]
    assert json.loads((lab.target / "version.json").read_text(encoding="utf-8"))["build"] == "fixed"
    assert p["dataset_refs"] == [_dataset_id(qa)]
    assert p["build"] == {"expected": "build2", "observed": "build2"}, p["build"]
    assert {c["id"]: c["observed"] for c in p["checks"]}["ENV-VERSION"] == "pass", p["checks"]
    st = L.evaluate(qa, "env_ready")
    assert (st["status"], st["verdict"]) == ("completed", "pass"), st


# ---------------------------------------------------------------------------
# AC-026 只有造数说明不能标环境就绪
# ---------------------------------------------------------------------------

def _review_handles(qa) -> list[str]:
    v = qa.ok("status").result["reviews"]
    return sorted(c["handle"] for c in v["mine"] + v["others"])


@pytest.mark.ac("AC-026")
@pytest.mark.layer("lab")
def test_ac026_documented_method_without_measurement_is_not_ready(qa):
    """造数说明与自测材料已登记，但没有任何实测：环境不得就绪。"""
    L.prepare(qa)
    assert L.snapshots(qa, "dataset")[0]["payload"]["preparation"]
    assert L.count(qa, "self_test_report") == 1
    assert L.count(qa, "environment_check") == 0
    st = L.evaluate(qa, "env_ready")
    assert st["status"] in NOT_COMPLETED and st["verdict"] != "pass", st
    assert not L.stage_events(qa, "env_ready", "stage_completed")
    entry = L.evaluate(qa, "test_entry")
    assert entry["status"] != "completed"


def _wrong_version_probe(qa):
    lab = L.prepare(qa)
    L.deploy(qa, "build2")
    act = L.plan_probe_via_resume(qa)
    version_file = lab.target / "version.json"
    good = version_file.read_text(encoding="utf-8")
    # 环境偏差：目标上实际运行的是另一个构建（在本测试自己的工作区内注入）
    version_file.write_text(json.dumps({"build_id": "build1", "build": "buggy_duplicate",
                                        "commit": "lab-commit-0001"}), encoding="utf-8")
    handles = _review_handles(qa)
    r = qa.api("action", "dispatch", "--id", act)
    assert r.rc == 0, r.describe()
    return lab, version_file, good, handles


@pytest.mark.ac("AC-026")
@pytest.mark.layer("lab")
def test_ac026_wrong_version_probe_marks_method_provided_but_not_ready(qa):
    lab, _, _, handles = _wrong_version_probe(qa)
    p = L.snapshots(qa, "environment_check")[-1]["payload"]
    assert p["build"]["expected"] == "build2" and p["build"]["observed"] == "build1", p["build"]
    by_id = {c["id"]: c for c in p["checks"]}
    assert by_id["ENV-VERSION"]["observed"] == "fail"
    assert all(c["method_provided"] is True for c in p["checks"])
    st = L.evaluate(qa, "env_ready")
    assert st["status"] in NOT_COMPLETED and st["verdict"] != "pass", st
    env = st["extra"]["environment"]
    assert env["material_status"] == "provided"  # 方法标已提供
    assert env["runtime_status"] == "not_verified" and env["ready"] is False  # 实测未就绪
    assert not L.stage_events(qa, "env_ready", "stage_completed")
    # 不要求重复提供整份说明：没有新增待确认事项
    assert _review_handles(qa) == handles
    entry = L.evaluate(qa, "test_entry")
    assert entry["status"] != "completed" and entry["verdict"] != "pass"
    assert not [e for e in L.snapshots(qa, "entry_report") if e["payload"]["verdict"] == "pass"]


@pytest.mark.ac("AC-026")
@pytest.mark.layer("lab")
def test_ac026_recheck_after_environment_fixed(qa):
    """环境修正后重新实测应能继续（方案 §6：不出现永久误锁）。"""
    lab, version_file, good, _ = _wrong_version_probe(qa)
    assert L.evaluate(qa, "env_ready")["status"] != "completed"
    version_file.write_text(good, encoding="utf-8")  # 环境恢复为计划中的 build2
    before = L.count(qa, "environment_check")
    r_api = L.env_check(qa, "DS1")
    r_resume = qa.run("resume")
    st = L.evaluate(qa, "env_ready")
    assert L.count(qa, "environment_check") > before, (
        "环境修正后没有任何公共入口产生新的环境实测\n"
        f"api environment check: rc={r_api.rc} {r_api.out[:600]}\nresume: rc={r_resume.rc}")
    assert (st["status"], st["verdict"]) == ("completed", "pass"), st


# ---------------------------------------------------------------------------
# AC-027 实际冒烟决定提测接收
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-027")
@pytest.mark.layer("lab")
def test_ac027_actual_smoke_decides_entry(qa, fixtures):
    L.ready_env(qa)  # demo 初始部署 build1；自测材料声明的构建也是 build1
    dep = L.snapshots(qa, "deployment")[-1]
    self_test = L.snapshots(qa, "self_test_report")[-1]["payload"]
    assert self_test["build"] == dep["payload"]["build"] == "build1"
    before = L.evaluate(qa, "test_entry")
    assert before["status"] != "completed"
    assert set(fixtures["policy"]["required_smoke"]) == {"SMOKE-1", "SMOKE-2"}
    r = L.run_cases(qa, ["SMOKE-1", "SMOKE-2"], "smoke")
    assert r.rc == 0 and r.result["state"] == "succeeded", r.describe()
    st = L.evaluate(qa, "test_entry")
    assert (st["status"], st["verdict"]) == ("completed", "pass"), st
    atts = {a["tc_ref"]: a for a in L.attempts(qa)}
    assert set(atts) == {"SMOKE-1", "SMOKE-2"} and len(L.attempts(qa)) == 2  # 首轮即通过，没有重试
    for tc, a in atts.items():
        assert (a["attempt_no"], a["outcome"], a["build"], a["purpose"]) == (1, "pass", "build1", "smoke")
        assert a["producer"]["kind"] == "worker" and a["origin"] == "local_lab"
        rc = L.receipt(qa, a["worker_receipt"])["data"]
        assert rc["exit_code"] == 0 and not rc["timed_out"] and not rc["truncated"]
        assert L.observation_of(qa, a) == L.oracle_observation(tc, fixtures)
    # 提测接收证据绑定该构建与两个运行记录
    report = L.envelope(qa, st["extra"]["entry_report"])
    assert report["payload"]["verdict"] == "pass" and report["payload"]["build"] == "build1"
    refs = {d["ref"] for d in report["depends_on"]}
    assert {atts["SMOKE-1"]["id"], atts["SMOKE-2"]["id"], dep["id"]} <= refs, refs
    runs = L.snapshots(qa, "execution_run")
    assert len(runs) == 1 and runs[0]["payload"]["build"] == "build1"
    assert set(runs[0]["payload"]["attempt_refs"]) == {a["id"] for a in atts.values()}
    assert {atts["SMOKE-1"]["id"], atts["SMOKE-2"]["id"]} <= set(st["inputs"])
    assert len(L.stage_events(qa, "test_entry", "stage_completed")) == 1


# ---------------------------------------------------------------------------
# AC-028 冒烟失败或没执行不能接收
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-028")
@pytest.mark.layer("lab")
def test_ac028_failed_or_missing_smoke_blocks_entry(qa, tmp_path):
    L.ready_env(qa)
    bad = L.reviewed_script(qa, L.write_variant(tmp_path, "smoke1_wrong_order"), ["SMOKE-1"], "lab/smoke_variant.py")
    r = L.run_cases(qa, ["SMOKE-1"], "smoke", script=bad)
    assert r.rc == 0, r.describe()
    atts = L.attempts(qa)
    assert [(a["tc_ref"], a["outcome"]) for a in atts] == [("SMOKE-1", "fail")]  # SMOKE-2 只有设计用例
    st = L.evaluate(qa, "test_entry")
    assert st["status"] in NOT_COMPLETED and st["verdict"] in ("fail", "indeterminate"), st
    reasons = "；".join(st["reasons"])
    assert "未通过：SMOKE-1" in reasons and "未执行：SMOKE-2" in reasons, reasons  # 两项原因都明确
    assert not L.stage_events(qa, "test_entry", "stage_completed")
    assert not [e for e in L.snapshots(qa, "entry_report") if e["payload"]["verdict"] == "pass"]
    assert L.evaluate(qa, "test_execution")["status"] != "completed"
    # 手写的“已接收”报告不能登记
    n = L.count(qa, "entry_report")
    forged = write_json(tmp_path / "entry.json", {"scope_ref": "x", "policy_ref": "x", "criterion_results": [],
                                                  "missing": [], "decision_refs": [], "verdict": "pass"})
    f = qa.api("artifact", "submit", "--story", L.STORY, "--kind", "entry_report", "--file", str(forged))
    assert f.rc == 2 and f.error_code == "UNSUPPORTED_CONTRACT", f.describe()
    assert L.count(qa, "entry_report") == n
    assert L.evaluate(qa, "test_entry")["status"] != "completed"
    # 修正后可继续：两条冒烟实际通过后才接收，首轮失败记录保留
    ok = L.run_cases(qa, ["SMOKE-1", "SMOKE-2"], "smoke")
    assert ok.rc == 0, ok.describe()
    st2 = L.evaluate(qa, "test_entry")
    assert (st2["status"], st2["verdict"]) == ("completed", "pass"), st2
    assert [(a["tc_ref"], a["outcome"]) for a in L.attempts(qa)][0] == ("SMOKE-1", "fail")


# ---------------------------------------------------------------------------
# AC-029 执行预期负向请求仍可通过
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-029")
@pytest.mark.layer("lab")
@pytest.mark.parametrize("build_id,build", BUILD_PARAMS)
def test_ac029_expected_negative_request_passes(qa, fixtures, build_id, build):
    lab = L.ready_for_cases(qa, build_id)
    r = L.run_cases(qa, ["SMOKE-2"], "execution")
    assert r.rc == 0 and r.result["state"] == "succeeded", r.describe()
    (a,) = L.attempts(qa)
    assert a["outcome"] == "pass" and a["build"] == build_id
    expected = {"status": 400, "refunded_after": 0, "records_after": 0}  # 夹具规则：金额 0 → 400 且账务不变
    assert L.oracle_observation("SMOKE-2", fixtures) == expected
    assert {x["key"]: (x["expected"], x["actual"], x["result"]) for x in a["assertions"]} == {
        k: (v, v, "pass") for k, v in expected.items()}
    # 观测证据与请求摘要已保存，且与磁盘字节一致
    assert L.observation_of(qa, a) == expected
    row = L.evidence_row(qa, a["observations_ref"])
    rc = L.receipt(qa, a["worker_receipt"])["data"]
    assert row["origin"] == "local_lab" and rc["stdout_sha256"] == row["sha256"]
    assert hashlib.sha256(L.evidence_bytes(qa, a["observations_ref"])).hexdigest() == row["sha256"]
    assert len(rc["argv_sha256"]) == 64 and rc["exit_code"] == 0
    # 独立读取业务库：无账务变化
    assert L.refund_db_state(lab.target) == {"orders": L.initial_orders(fixtures), "refunds": [], "ledger": []}


@pytest.mark.ac("AC-029")
@pytest.mark.layer("lab")
def test_ac029_negative_expectation_not_met_is_fail(qa, fixtures):
    """对照：预期被拒绝的请求实际成功（buggy_neighbor 的 read_only 退款）时不能通过。"""
    L.ready_for_cases(qa, "build3")
    assert L.run_cases(qa, ["C2"], "execution").rc == 0
    (a,) = L.attempts(qa)
    assert a["outcome"] == "fail"
    actual = {x["key"]: x["actual"] for x in a["assertions"]}
    assert actual == {"status": 201, "refunded_after": 1000, "records_after": 1}
    assert {x["key"]: x["expected"] for x in a["assertions"]} == L.oracle_observation("C2", fixtures)


# ---------------------------------------------------------------------------
# AC-030 退出码 0 和手填 pass 不够
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-030")
@pytest.mark.layer("lab")
@pytest.mark.parametrize("variant,stdout_is_empty", [("no_result", True), ("claims_pass", False)])
def test_ac030_exit_zero_without_result_is_not_pass(qa, tmp_path, variant, stdout_is_empty):
    L.ready_for_cases(qa, "build2")
    ref = L.reviewed_script(qa, L.write_variant(tmp_path, variant), ["C2"], f"lab/{variant}.py")
    r = L.run_cases(qa, ["C2"], "execution", script=ref)
    assert r.rc == 0, r.describe()
    (a,) = L.attempts(qa)
    rc = L.receipt(qa, a["worker_receipt"])["data"]
    assert rc["exit_code"] == 0  # 脚本确实返回 0
    assert a["outcome"] != "pass" and a["outcome"] in ("error", "blocked"), a["outcome"]
    assert a["assertions"] == []  # 没有观测就没有断言，不伪造
    raw = L.evidence_bytes(qa, a["observations_ref"])
    assert (raw == b"") is stdout_is_empty  # 证据是脚本的真实输出
    status = L.execution_status(qa)
    assert status["latest"]["C2"]["outcome"] != "pass"
    report = L.build_report(qa)["test_report"]
    assert {x["tc"]: x["status"] for x in report["latest"]["rows"]}["C2"] != "pass"
    assert report["counts"]["pass"] == 0


@pytest.mark.ac("AC-030")
@pytest.mark.layer("lab")
def test_ac030_handwritten_pass_is_rejected(qa, tmp_path):
    L.ready_for_cases(qa, "build2")
    design = L.snapshots(qa, "case_design")[-1]["id"]
    forged = {
        "case_attempt": {"tc_ref": "C1", "attempt_no": 1, "context_sha256": "0" * 64, "assertions": [],
                         "observations_ref": "EV-FORGED", "outcome": "pass", "evidence_refs": [], "run_ref": "manual",
                         "build": "build2", "purpose": "execution"},
        "execution_run": {"plan_ref": design, "worker_receipt": "WR-FORGED", "origin": "local_lab",
                          "target": "lab-refund", "build": "build2", "dataset": "DS1", "attempt_refs": [],
                          "started_at": "2026-11-16T01:00:00Z", "purpose": "execution"},
        "test_report": {"scope_ref": design, "counts": {"pass": 7}},
    }
    seq = L.last_seq(qa)
    snapshots_before = qa.query("SELECT COUNT(*) AS n FROM snapshots")[0]["n"]
    for kind, payload in forged.items():
        f = write_json(tmp_path / f"{kind}.json", payload)
        r = qa.api("artifact", "submit", "--story", L.STORY, "--kind", kind, "--file", str(f))
        assert r.rc == 2 and r.error_code == "UNSUPPORTED_CONTRACT", r.describe()
        assert r.json["committed"] is False
    assert qa.query("SELECT COUNT(*) AS n FROM snapshots")[0]["n"] == snapshots_before
    assert qa.query("SELECT COUNT(*) AS n FROM worker_receipts")[0]["n"] == 0
    assert L.attempts(qa) == [] and L.execution_status(qa)["latest"] == {}
    assert "snapshot_registered" not in L.event_types(qa, seq)
    # 修正后可继续：真实执行仍然可用
    assert L.run_cases(qa, ["C1"], "execution").rc == 0
    assert [(a["tc_ref"], a["outcome"]) for a in L.attempts(qa)] == [("C1", "pass")]


# ---------------------------------------------------------------------------
# AC-031 评审脚本与实际 CI 提交相同
# ---------------------------------------------------------------------------

def _register_sr1(qa, tmp_path):
    f = L.write_variant(tmp_path, "marked_cases", "sr1.py", MARKER="sr1-ran")
    r = L.register_script(qa, f, ["C1"], "lab/sr1.py", repo="lab-repo", commit="abc1234")
    assert r.rc == 0, r.describe()
    return f, r.result


@pytest.mark.ac("AC-031")
@pytest.mark.layer("lab")
def test_ac031_reviewed_script_runs_with_same_commit_and_hash(qa, tmp_path):
    lab = L.ready_for_cases(qa, "build2")
    f, reg = _register_sr1(qa, tmp_path)
    sr1 = reg["script_ref"]
    assert reg["file_sha256"] == sha256_file(f)
    rv = L.review_script(qa, sr1)
    assert rv.rc == 0 and rv.result["review_ref"], rv.describe()
    r = L.run_cases(qa, ["C1"], "execution", script=sr1)
    assert r.rc == 0 and r.result["state"] == "succeeded", r.describe()
    (a,) = L.attempts(qa)
    rev = L.envelope(qa, sr1)
    assert rev["payload"]["tc_refs"] == ["C1"] and rev["payload"]["commit"] == "abc1234"
    assert rev["payload"]["repo"] == "lab-repo" and rev["payload"]["file_sha256"] == sha256_file(f)
    assert a["script_ref"] == sr1 and a["outcome"] == "pass"
    assert L.receipt(qa, a["worker_receipt"])["data"]["script_sha256"] == sha256_file(f)
    assert L.evidence_bytes(qa, rev["payload"]["content_ref"]) == f.read_bytes()
    assert (lab.target / "sr1-ran").read_text() == "C1"  # 实际运行的就是这份内容
    # 人工评审绑定 SR1 的快照摘要
    dec = qa.query("SELECT * FROM decisions WHERE id=?", (rv.result["review_ref"],))[0]
    assert dec["role"] == "reviewer" and dec["result"] == "approve"
    assert json.loads(dec["subjects"]) == [{"ref": sr1, "sha256": rev["_sha256"]}]


@pytest.mark.ac("AC-031")
@pytest.mark.layer("lab")
def test_ac031_ci_result_reference_is_recorded(qa, tmp_path):
    """规格要求记录绑定 SR1 的 CI 结果引用，且不能引用技能仓库自身的测试。"""
    L.ready_for_cases(qa, "build2")
    _, reg = _register_sr1(qa, tmp_path)
    assert L.review_script(qa, reg["script_ref"]).rc == 0
    assert L.run_cases(qa, ["C1"], "execution", script=reg["script_ref"]).rc == 0
    help_text = qa.text("api", "script", "register", "--help", workspace=False, non_interactive=False).out
    rev = L.envelope(qa, reg["script_ref"])["payload"]
    (a,) = L.attempts(qa)
    run = L.snapshots(qa, "execution_run")[-1]["payload"]
    recorded = rev.get("ci_ref") or a.get("ci_ref") or run.get("ci_ref")
    assert recorded, ("产品没有记录 CI 结果引用：script_revision.ci_ref 为空，执行记录也没有 CI 引用；"
                      f"script register 的参数为：{help_text.strip()[:400]}")


# ---------------------------------------------------------------------------
# AC-032 评审后脚本变化必须重审
# ---------------------------------------------------------------------------

def _sr1_approved_then_sr2(qa, tmp_path):
    lab = L.ready_for_cases(qa, "build2")
    f1 = L.write_variant(tmp_path / "v1", "marked_cases", "sr.py", MARKER="sr1-ran")
    sr1 = L.reviewed_script(qa, f1, ["C2"], "lab/sr.py", commit="c1")
    f2 = L.write_variant(tmp_path / "v2", "marked_cases", "sr.py", MARKER="sr2-ran")
    r = L.register_script(qa, f2, ["C2"], "lab/sr.py", commit="c2")
    assert r.rc == 0 and r.result["script_ref"] != sr1, r.describe()
    assert r.result["file_sha256"] == sha256_file(f2) != sha256_file(f1)
    return lab, sr1, r.result["script_ref"]


@pytest.mark.ac("AC-032")
@pytest.mark.layer("lab")
def test_ac032_changed_script_is_not_run_under_old_approval(qa, tmp_path):
    lab, sr1, sr2 = _sr1_approved_then_sr2(qa, tmp_path)
    decisions = qa.query("SELECT id, subjects, result, revoked_at FROM decisions WHERE role='reviewer' ORDER BY id")
    seq = L.last_seq(qa)
    r = L.run_cases(qa, ["C2"], "execution", script=sr2)
    assert r.rc == 3 and r.error_code == "AUTHORIZATION_INVALID", r.describe()
    assert r.json["committed"] is False
    assert not (lab.target / "sr2-ran").exists()  # 未审内容没有被执行
    assert L.attempts(qa) == [] and L.count(qa, "execution_run") == 0
    assert "action_succeeded" not in L.event_types(qa, seq)
    # 以旧引用 SR1 再试：无论拒绝还是只运行已审的 SR1 内容，都不得运行 SR2
    L.run_cases(qa, ["C2"], "retest", script=sr1)
    assert not (lab.target / "sr2-ran").exists()
    assert all(a["script_ref"] == sr1 for a in L.attempts(qa))
    # 原批准没有被扩展到新内容
    assert qa.query("SELECT id, subjects, result, revoked_at FROM decisions WHERE role='reviewer' ORDER BY id") == decisions
    assert all(sr2 not in d["subjects"] for d in decisions)
    assert any(sr1 in d["subjects"] for d in decisions)


@pytest.mark.ac("AC-032")
@pytest.mark.layer("lab")
def test_ac032_rerun_after_rereview_can_continue(qa, tmp_path):
    """SR2 重新评审后，同一条执行命令应能继续（方案 §6：修正后不出现永久误锁）。"""
    lab, _, sr2 = _sr1_approved_then_sr2(qa, tmp_path)
    cmd = ["execution", "plan", "--story", L.STORY, "--cases", "C2", "--purpose", "execution", "--dispatch",
           "--script", sr2]
    assert qa.api(*cmd).error_code == "AUTHORIZATION_INVALID"
    assert L.review_script(qa, sr2).rc == 0
    r = qa.api(*cmd)
    stuck = qa.api_ok("action", "list", "--story", L.STORY, "--state", "running").result["actions"]
    assert r.rc == 0, f"重新评审后仍不能执行；滞留在 running 的操作：{stuck}\n" + r.describe()
    (a,) = L.attempts(qa)
    assert a["script_ref"] == sr2 and a["outcome"] == "pass"
    assert (lab.target / "sr2-ran").exists()


@pytest.mark.ac("AC-032")
@pytest.mark.layer("lab")
def test_ac032_tampered_script_bytes_are_not_run(qa, tmp_path):
    """工作区内已登记的 SR1 脚本字节被改成 SR2 内容：摘要不符，拒绝执行。"""
    lab = L.ready_for_cases(qa, "build2")
    f1 = L.write_variant(tmp_path / "v1", "marked_cases", "sr.py", MARKER="sr1-ran")
    sr1 = L.reviewed_script(qa, f1, ["C2"], "lab/sr.py", commit="c1")
    f2 = L.write_variant(tmp_path / "v2", "marked_cases", "sr.py", MARKER="sr2-ran")
    stored = lab.ws / L.evidence_row(qa, L.envelope(qa, sr1)["payload"]["content_ref"])["rel_path"]
    stored.chmod(0o644)
    stored.write_bytes(f2.read_bytes())
    r = L.run_cases(qa, ["C2"], "execution", script=sr1)
    assert r.rc != 0 and r.error_code in ("EVIDENCE_INVALID", "AUTHORIZATION_INVALID"), r.describe()
    assert not (lab.target / "sr2-ran").exists() and not (lab.target / "sr1-ran").exists()
    assert L.attempts(qa) == []


# ---------------------------------------------------------------------------
# AC-108 脚本越界与超时子进程清理
# ---------------------------------------------------------------------------

SENTINEL = "TEST_SECRET_DO_NOT_SEND_7F92"  # 夹具 privacy_sentinels.token（合成哨兵，不是真实凭证）
TIMEOUT_S = 4


def _run_malicious(qa, tmp_path, detach: bool) -> dict:
    """登记并评审一个恶意实验脚本，经受控 worker 执行并触发超时。返回独立观测。

    网络探测只连接本测试在环回地址上打开的监听端口，不产生外部流量。
    """
    lab = L.ready_for_cases(qa, "build2", exec_timeout_seconds=TIMEOUT_S)
    cred = qa.home / ".lab-credentials"
    cred.write_text(f"token={SENTINEL}\n", encoding="utf-8")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    srv.settimeout(0.2)
    out: dict = {"lab": lab}
    try:
        script = L.write_variant(tmp_path, "malicious", SENTINEL=SENTINEL, CRED_FILE=str(cred),
                                 PORT=srv.getsockname()[1], DETACH="True" if detach else "False")
        ref = L.reviewed_script(qa, script, ["SMOKE-2"], "lab/malicious.py")
        t0 = time.monotonic()
        r = L.run_cases(qa.with_env(LAB_FAKE_CREDENTIAL=SENTINEL), ["SMOKE-2"], "execution", script=ref)
        out["elapsed"] = time.monotonic() - t0
        out["result"] = r
        out["report"] = json.loads((lab.target / "malicious-report.json").read_text(encoding="utf-8"))
        try:
            conn, _ = srv.accept()
            out["received"] = conn.recv(16)
            conn.close()
        except OSError:
            out["received"] = None
        out["script_dead"] = L.wait_dead(out["report"]["pid"])
        out["grandchild_dead"] = L.wait_dead(out["report"]["grandchild_pid"], 5.0)
        atts = L.attempts(qa)
        out["attempt"] = atts[-1] if atts else None
        out["receipt"] = L.receipt(qa, atts[-1]["worker_receipt"])["data"] if atts else None
        out["tmp_left"] = sorted(p.name for p in (lab.ws / "tmp").iterdir())
        out["doctor"] = qa.api_ok("doctor").result
    finally:
        srv.close()
        rep = out.get("report") or {}
        for key in ("pid", "grandchild_pid"):
            if rep.get(key):
                L.kill_quietly(rep[key])
    return out


@pytest.fixture(scope="module")
def malicious_run(tmp_path_factory):
    base = tmp_path_factory.mktemp("lab-malicious")
    return _run_malicious(L.make_qa(base), base, detach=False)


@pytest.mark.ac("AC-108")
@pytest.mark.layer("lab")
def test_ac108_timeout_kills_process_tree_and_is_not_pass(malicious_run):
    m = malicious_run
    assert m["report"]["grandchild_pid"] != m["report"]["pid"]
    a, rc = m["attempt"], m["receipt"]
    assert a is not None and a["outcome"] != "pass", a  # 脚本挂起前已输出一份“看起来通过”的结果
    assert rc["timed_out"] is True and rc["killed_process_group"] is True
    assert TIMEOUT_S <= m["elapsed"] < TIMEOUT_S + 20
    # 清理记录与独立观测一致：脚本进程与孙进程都已终止，临时目录已清理
    assert m["script_dead"] and m["grandchild_dead"]
    assert rc["process_group_alive"] is False
    assert m["tmp_left"] == []


@pytest.mark.ac("AC-108")
@pytest.mark.layer("lab")
def test_ac108_detached_grandchild_is_terminated(qa, tmp_path):
    """孙进程脱离进程组（新会话）后挂起：超时后进程树仍应全部终止，清理记录应与事实一致。"""
    m = _run_malicious(qa, tmp_path, detach=True)
    assert m["attempt"] is not None and m["attempt"]["outcome"] != "pass"
    assert m["receipt"]["timed_out"] is True
    assert m["script_dead"]
    assert m["grandchild_dead"], (
        f"超时后孙进程 {m['report']['grandchild_pid']} 仍在运行，而回执记录 process_group_alive="
        f"{m['receipt']['process_group_alive']}")


@pytest.mark.ac("AC-108")
@pytest.mark.layer("lab")
def test_ac108_no_credentials_readable(malicious_run):
    rep = malicious_run["report"]
    assert rep["env_secret_visible"] is False, rep["env_keys"]  # 环境变量中的凭证未被继承
    assert rep["credential_file_readable"] is False, "脚本读到了凭证文件内容（执行器没有文件或凭证隔离）"


@pytest.mark.ac("AC-108")
@pytest.mark.layer("lab")
def test_ac108_network_is_isolated(malicious_run):
    m = malicious_run
    assert m["report"]["network_connected"] is False and m["received"] is None, (
        f"脚本连通了未登记的地址并发出数据 {m['received']!r}（执行器没有 OS/容器级网络隔离）")


@pytest.mark.ac("AC-108")
@pytest.mark.layer("lab")
def test_ac108_subprocess_arguments_are_not_presented_as_sandbox(malicious_run):
    """产品不得把 subprocess 参数当作沙箱：能力检查与回执声明的隔离能力必须与脚本实际观测一致。

    以恶意脚本自己的观测为准：连上了环回端口就是没有网络隔离，读到了凭证文件就是没有凭证隔离。
    报告“有隔离”而脚本实际越界，或报告“无隔离”而脚本实际被拦，都算不一致。
    """
    m = malicious_run
    rep = m["report"]
    actual = {"network": rep["network_connected"] is False and m["received"] is None,
              "credentials": rep["credential_file_readable"] is False and rep["env_secret_visible"] is False}
    ex = m["doctor"]["executor"]
    for label, iso in (("doctor", ex["isolation"]), ("回执", m["receipt"]["isolation"])):
        for cap in ("network", "credentials"):
            assert iso[cap] is actual[cap], (
                f"{label} 声明 {cap}={iso[cap]}，但脚本实际观测为{'已隔离' if actual[cap] else '未隔离'}：{rep}")
    # 文件系统不是独立挂载：不得宣称已隔离，也不得宣称支持不可信脚本
    assert ex["isolation"]["filesystem"] is False and m["receipt"]["isolation"]["filesystem"] is False
    assert ex["untrusted_scripts"] == "不支持"


@pytest.mark.ac("AC-108")
@pytest.mark.layer("lab")
def test_ac108_isolating_executor_is_available(malicious_run):
    """场景前提是“在支持隔离的执行器运行”：产品应提供具备凭证与网络隔离的执行器。"""
    adapters = [c for c in malicious_run["doctor"]["checks"] if c["item"].startswith("adapter:") and
                "run_trusted" in (c["detail"].get("capabilities") or [])]
    iso = malicious_run["doctor"]["executor"]["isolation"]
    assert iso["credentials"] and iso["network"], (
        f"产品只有无隔离的本地执行器：isolation={iso}，执行器适配器={adapters}")


# ---------------------------------------------------------------------------
# AC-117 合成金额并发与幂等冲突
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-117")
@pytest.mark.layer("lab")
@pytest.mark.parametrize("build", ["buggy_duplicate", "fixed", "buggy_neighbor"])
def test_ac117_direct_concurrency_and_sequence_against_oracle(factory, fixtures, build):
    """在工厂的隔离部署上直接驱动被测服务：进程屏障并发，再重置执行夹具五步顺序。"""
    rt = fixtures["refund_target"]
    cc = rt["concurrency_case"]
    # 并发：两个进程在屏障后同时发出不同键的 7000 退款
    ds = factory.dataset(f"ac117-concurrency-{build}", build)
    assert ds.observe()["refunded"] == cc["reset_refunded"]
    outs = ds.concurrent([{"order": cc["initial_order"], "amount": q["amount"], "key": q["key"],
                           "role": "refund_operator"} for q in cc["requests"]])
    o = ds.observe()
    assert sum(1 for x in outs if x["status"] == 201) == cc["expected_success_count"], outs
    assert sorted(x["status"] for x in outs) == [201, 400]
    assert o["refunded"] == cc["expected_refunded"] and o["records"] == cc["expected_refund_records"]
    assert factory.cleanup(ds)["restored"] is True
    # 顺序：K1/K1/K1 改额/K2 超额/K3 零金额
    ds = factory.dataset(f"ac117-sequence-{build}", build)
    oracle = factory.oracle()
    trace = []
    for step in rt["example_sequence"]:
        res = ds.refund(step["order"], step["amount"], step["key"])
        exp_status, _ = oracle.refund(step["order"], step["amount"], step["key"], "refund_operator")
        assert exp_status == step["expected_status"] and oracle.observe()["refunded"] == step["expected_refunded"]
        trace.append({"status": res["status"], "refund_id": (res["body"] or {}).get("refund_id"), **ds.observe(),
                      "expected_status": exp_status, "expected": oracle.observe()})
    deviations = [i for i, t in enumerate(trace) if (t["status"], t["refunded"], t["records"]) != (
        t["expected_status"], t["expected"]["refunded"], t["expected"]["records"])]
    if build == "buggy_duplicate":
        # 该构建的已知缺陷必须能被独立观测发现：第二次同键请求重复入账
        assert deviations and deviations[0] == 1, trace
        assert trace[1]["status"] == 201 and trace[1]["refunded"] == 6000
    else:
        assert deviations == [], trace
        assert trace[0]["refund_id"] and trace[0]["refund_id"] == trace[1]["refund_id"]  # 同键同载荷不重复
        assert [t["status"] for t in trace[2:]] == [409, 400, 400]
        assert [t["records"] for t in trace] == [1, 1, 1, 1, 1]  # 冲突、超额、零金额都没有额外记录
    assert factory.cleanup(ds)["restored"] is True


@pytest.mark.ac("AC-117")
@pytest.mark.layer("lab")
@pytest.mark.parametrize("build", ["buggy_duplicate", "fixed", "buggy_neighbor"])
def test_ac117_framework_results_match_independent_observation(matrix, factory, fixtures, build):
    """框架经受控 worker 得到的 C3（顺序）、C4（并发）结果，必须与独立观测和参考模型一致。"""
    m = matrix["builds"][build]
    assert m["version"]["build"] == build
    assert m["service_sha256"] == factory.receipt["services"][build]["service_sha256"]  # 同一份被测字节
    for case in ("C3", "C4"):
        a = m["attempts"][case]
        oracle = L.oracle_observation(case, fixtures)
        ds = factory.dataset(f"ac117-fw-{case}-{build}", build)
        direct = L.direct_observation(ds, case, fixtures)
        factory.cleanup(ds)
        assert {x["key"]: x["expected"] for x in a["assertions"]} == oracle  # 冻结预期与参考模型一致
        assert m["observations"][case] == direct  # 框架保存的观测与独立观测一致
        assert {x["key"]: x["actual"] for x in a["assertions"]} == direct
        want = "fail" if case in L.EXPECTED_FAILING[build] else "pass"
        assert (direct == oracle) is (want == "pass")
        assert a["outcome"] == want, (build, case, a["assertions"])
        assert a["producer"]["kind"] == "worker" and a["origin"] == "local_lab"
    # 执行后业务库回到初态（每个用例前后重置）
    assert m["db_after"] == {"orders": L.initial_orders(fixtures), "refunds": [], "ledger": []}


# ---------------------------------------------------------------------------
# AC-118 业务事务失败与重复回调
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-118")
@pytest.mark.layer("lab")
@pytest.mark.parametrize("build", ["buggy_duplicate", "fixed", "buggy_neighbor"])
def test_ac118_direct_atomic_failure_and_duplicate_callback(factory, fixtures, build):
    ds = factory.dataset(f"ac118-direct-{build}", build)
    oracle = factory.oracle()
    initial = ds.state()
    # 在余额更新与退款记录之间注入已知故障
    failed = ds.refund("O100", 2000, "K5", fail_after_balance=True)
    assert oracle.refund("O100", 2000, "K5", "refund_operator", fail_after_balance=True)[0] == 500
    assert failed["status"] == 500
    assert ds.state() == initial  # 余额与记录均不变
    # 另一笔成功退款，再重复发送同一回调两次
    ok = ds.refund("O100", 2000, "K6")
    assert ok["status"] == oracle.refund("O100", 2000, "K6", "refund_operator")[0] == 201
    rid = ok["body"]["refund_id"]
    statuses = [ds.callback("EV-1", rid)["status"], ds.callback("EV-1", rid)["status"]]
    assert statuses == [oracle.callback("EV-1", rid), oracle.callback("EV-1", rid)] == [201, 200]
    state = ds.state()
    assert state["ledger"] == [{"event_id": "EV-1", "refund_id": rid}]  # 只记一次账
    assert ds.observe() == oracle.observe() == {"refunded": 2000, "records": 1, "ledger_entries": 1}
    assert len(ds.log) >= 5 and all("state_after" in e for e in ds.log)
    assert factory.cleanup(ds)["restored"] is True


@pytest.mark.ac("AC-118")
@pytest.mark.layer("lab")
@pytest.mark.parametrize("build", ["buggy_duplicate", "fixed", "buggy_neighbor"])
def test_ac118_framework_keeps_actual_observations(matrix, factory, fixtures, build):
    m = matrix["builds"][build]
    a = m["attempts"]["C5"]
    oracle = L.oracle_observation("C5", fixtures)
    ds = factory.dataset(f"ac118-fw-{build}", build)
    direct = L.direct_observation(ds, "C5", fixtures)
    factory.cleanup(ds)
    assert direct == oracle
    assert a["outcome"] == "pass"
    by_key = {x["key"]: x for x in a["assertions"]}
    # 不只记录 HTTP 状态：故障后的余额、记录数与回调账本条数都作为实际观测保存并断言
    assert {"refunded_after_failure", "records_after_failure", "ledger_entries", "failed_status",
            "callback_statuses"} <= set(by_key)
    assert {k: v["actual"] for k, v in by_key.items()} == direct
    assert {k: v["expected"] for k, v in by_key.items()} == oracle
    assert m["observations"]["C5"] == direct


MUTANTS = {
    # 故障注入后本应回滚，改为提交余额：HTTP 仍返回 500，但余额已变
    "non_atomic_failure": (
        '            if fail_after_balance:\n                conn.execute("ROLLBACK")\n',
        '            if fail_after_balance:\n                conn.execute("COMMIT")\n',
        {"failed_status": "pass", "refunded_after_failure": "fail"}),
    # 重复回调仍返回 200，但又记了一次账
    "double_booking_callback": (
        '            if exists:\n                conn.execute("ROLLBACK")\n                return 200\n',
        '            if exists:\n                conn.execute("INSERT INTO ledger VALUES(?, ?)", (event_id + "#dup", refund_id))\n'
        '                conn.execute("COMMIT")\n                return 200\n',
        {"callback_statuses": "pass", "ledger_entries": "fail"}),
}


@pytest.mark.ac("AC-118")
@pytest.mark.layer("lab")
@pytest.mark.parametrize("mutant", sorted(MUTANTS))
def test_ac118_wrong_data_with_correct_http_status_is_caught(qa, mutant):
    """对照：在本测试工作区内给被测服务注入“状态码正确、账务错误”的缺陷，框架不得只信状态码。"""
    lab = L.ready_for_cases(qa, "build2")
    old, new, expected_results = MUTANTS[mutant]
    svc = lab.target / "refund_service.py"
    src = svc.read_text(encoding="utf-8")
    assert src.count(old) == 1, "被测服务源码结构已变化，无法注入对照缺陷"
    svc.write_text(src.replace(old, new), encoding="utf-8")
    r = L.run_cases(qa, ["C5"], "execution")
    assert r.rc == 0, r.describe()
    (a,) = L.attempts(qa)
    results = {x["key"]: x["result"] for x in a["assertions"]}
    for key, want in expected_results.items():
        assert results[key] == want, (key, a["assertions"])
    assert a["outcome"] == "fail"
    assert L.execution_status(qa)["latest"]["C5"]["outcome"] == "fail"


# ---------------------------------------------------------------------------
# AC-119 输出与资源限额不会截断成成功
# ---------------------------------------------------------------------------

LIMIT = 4096


def _limited_lab(qa):
    lab = L.ready_for_cases(qa, "build2", max_output_bytes=LIMIT, exec_timeout_seconds=TIMEOUT_S)
    cfg = json.loads((lab.ws / "config" / "qa-flow.json").read_text(encoding="utf-8"))
    assert cfg["budgets"]["max_output_bytes"] == LIMIT and cfg["budgets"]["exec_timeout_seconds"] == TIMEOUT_S
    assert all(a.get("credential_ref") is None for a in cfg["adapters"])  # 没有生产凭证
    assert all(t["environment"] == "lab" for t in cfg["targets"])
    return lab


@pytest.mark.ac("AC-119")
@pytest.mark.layer("lab")
def test_ac119_output_over_limit_is_not_truncated_into_pass(qa, tmp_path):
    lab = _limited_lab(qa)
    ref = L.reviewed_script(qa, L.write_variant(tmp_path, "big_output", PAD_KB=256), ["SMOKE-2"], "lab/big.py")
    r = L.run_cases(qa, ["SMOKE-2"], "execution", script=ref)
    assert r.rc in (0, 6), r.describe()
    (a,) = L.attempts(qa)
    rc = L.receipt(qa, a["worker_receipt"])["data"]
    # 被截断的输出本身是一份完整且断言全过的 JSON：仍然不能记为通过
    kept = L.evidence_bytes(qa, a["observations_ref"])
    assert json.loads(kept)["observation"] == {"status": 400, "refunded_after": 0, "records_after": 0}
    assert a["outcome"] != "pass" and a["outcome"] == "error", a
    assert rc["truncated"] is True  # 截断标识
    sizes = [L.evidence_row(qa, e)["size"] for e in a["evidence_refs"]]
    assert sum(sizes) <= LIMIT and len(kept) <= LIMIT, sizes  # 日志只保留获准上限
    assert rc["killed_process_group"] is True and rc["process_group_alive"] is False
    assert L.wait_dead(int((lab.target / "big-output-pid.txt").read_text()))
    assert sorted(p.name for p in (lab.ws / "tmp").iterdir()) == []  # 临时数据已清理
    assert L.execution_status(qa)["latest"]["SMOKE-2"]["outcome"] != "pass"
    report = L.build_report(qa)["test_report"]
    assert report["counts"]["pass"] == 0 and report["counts"]["error"] == 1
    # 修正后可继续：用受审的正常脚本重新执行可以通过
    assert L.run_cases(qa, ["SMOKE-2"], "retest").rc == 0
    assert L.attempts(qa)[-1]["outcome"] == "pass"


@pytest.mark.ac("AC-119")
@pytest.mark.layer("lab")
def test_ac119_action_time_limit_is_not_pass(qa, tmp_path):
    lab = _limited_lab(qa)
    ref = L.reviewed_script(qa, L.write_variant(tmp_path, "slow"), ["SMOKE-2"], "lab/slow.py")
    t0 = time.monotonic()
    r = L.run_cases(qa, ["SMOKE-2"], "execution", script=ref)
    elapsed = time.monotonic() - t0
    assert r.rc in (0, 6), r.describe()
    assert TIMEOUT_S <= elapsed < TIMEOUT_S + 20
    (a,) = L.attempts(qa)
    rc = L.receipt(qa, a["worker_receipt"])["data"]
    assert json.loads(L.evidence_bytes(qa, a["observations_ref"]))["observation"]["status"] == 400
    assert a["outcome"] == "error" and rc["timed_out"] is True
    assert rc["killed_process_group"] is True and rc["process_group_alive"] is False
    assert L.wait_dead(int((lab.target / "slow-pid.txt").read_text()))
    assert sorted(p.name for p in (lab.ws / "tmp").iterdir()) == []
    assert L.execution_status(qa)["latest"]["SMOKE-2"]["outcome"] != "pass"
