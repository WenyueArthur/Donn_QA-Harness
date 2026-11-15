"""独立验收：稳定性分析、回归映射与隔离（批次 M4）。

场景：AC-033～AC-036。并按方案 §4.4 在 buggy_duplicate、fixed、buggy_neighbor 三个构建上验证
框架能发现缺陷、验证修复、捕捉相邻回归。
只通过子进程调用公共 CLI；预期值来自 lab-fixtures.json 与独立参考模型。
"""

from __future__ import annotations

import json

import pytest

from support import lab_helpers as L
from support.cli import REPO

FIXTURES_PATH = REPO / "docs" / "qa-flow-rebuild" / "lab-fixtures.json"


@pytest.fixture(scope="module")
def fixtures() -> dict:
    return json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def factory(tmp_path_factory):
    return L.load_factory().build(FIXTURES_PATH, tmp_path_factory.mktemp("lab-factory"))


@pytest.fixture(scope="module")
def matrix(tmp_path_factory):
    """同一工作区依次部署三个构建，经受控 worker 执行全部合成用例（只读复用）。"""
    m = L.build_matrix(L.make_qa(tmp_path_factory.mktemp("lab-matrix")))
    qa = m["lab"].qa
    m["analysis"] = L.snapshots(qa, "test_analysis")[-1]["payload"]
    m["design"] = L.snapshots(qa, "case_design")[-1]["payload"]
    m["report"] = L.build_report(qa)
    return m


def _stability(qa, tc: str) -> dict:
    return qa.api_ok("execution", "stability", "--story", L.STORY, "--tc", tc).result


# ---------------------------------------------------------------------------
# AC-033 同上下文失败后成功标不稳定
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-033")
@pytest.mark.layer("lab")
def test_ac033_same_context_fail_then_pass_is_flaky(qa, tmp_path, fixtures):
    rule = fixtures["policy"]["stability_rule"]
    assert rule["minimum_attempts"] == 3
    L.ready_for_cases(qa, "build2")
    flaky = L.reviewed_script(qa, L.write_variant(tmp_path, "flaky_c1"), ["C1"], "lab/flaky.py")
    # 同一构建、数据集、配置与脚本下执行三次。相同计划重复派发只会回放，因此三次使用不同的执行目的。
    after_two = None
    for i, purpose in enumerate(["execution", "retest", "stability"]):
        r = L.run_cases(qa, ["C1"], purpose, script=flaky)
        assert r.rc == 0 and r.result["state"] == "succeeded" and not r.result.get("replayed"), r.describe()
        if i == 1:
            after_two = _stability(qa, "C1")
    atts = L.attempts(qa)
    assert [a["outcome"] for a in atts] == ["fail", "pass", "pass"], "前置：合成不稳定脚本应产生 fail/pass/pass"
    assert len({a["context_sha256"] for a in atts}) == 1 and {a["build"] for a in atts} == {"build2"}
    assert len({a["script_ref"] for a in atts}) == 1
    # 尝试数不足最小次数时不能下“稳定通过”的结论
    assert after_two["classification"] != "stable_pass"
    res = _stability(qa, "C1")
    assert res["classification"] == rule["same_context_mixed_outcomes"] == "flaky", res
    assert res["first_outcome"] == "fail"
    assert res["attempt_refs"] == [a["id"] for a in atts]  # 三次尝试全部保留
    assert len(res["contexts"]) == 1 and res["contexts"][0]["outcomes"] == ["fail", "pass", "pass"]
    snap = L.envelope(qa, res["assessment_ref"])
    assert snap["payload"]["classification"] == "flaky"
    assert {d["ref"] for d in snap["depends_on"]} == {a["id"] for a in atts}
    # 首轮失败在测试报告中保留，没有被后续通过覆盖
    report = L.build_report(qa)["test_report"]
    first = {x["tc"]: x for x in report["first_run"]["rows"]}["C1"]
    assert first["status"] == "fail" and first["attempt_ref"] == atts[0]["id"]
    assert report["attempt_total"] == 3
    status = L.execution_status(qa)
    assert [x["outcome"] for x in status["attempts"] if x["tc"] == "C1"] == ["fail", "pass", "pass"]


# ---------------------------------------------------------------------------
# AC-034 不同构建不构成同条件波动
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-034")
@pytest.mark.layer("lab")
def test_ac034_different_builds_are_not_same_condition_flaky(qa, fixtures):
    rule = fixtures["policy"]["stability_rule"]
    L.ready_for_cases(qa)  # demo 初始部署 build1（buggy_duplicate）
    assert L.run_cases(qa, ["C1"], "execution").rc == 0
    L.deploy(qa, "build2")
    assert L.run_cases(qa, ["C1"], "execution").rc == 0
    atts = L.attempts(qa)
    assert [(a["build"], a["outcome"]) for a in atts] == [("build1", "fail"), ("build2", "pass")]
    assert atts[0]["script_ref"] == atts[1]["script_ref"]  # 其他条件相同
    assert atts[0]["context_sha256"] != atts[1]["context_sha256"]
    res = _stability(qa, "C1")
    assert res["classification"] == rule["different_context"] == "incomparable", res
    assert res["classification"] != "flaky"
    # 记录版本变化
    assert sorted(tuple(c["builds"]) for c in res["contexts"]) == [("build1",), ("build2",)]
    by_build = {c["builds"][0]: c["outcomes"] for c in res["contexts"]}
    assert by_build == {"build1": ["fail"], "build2": ["pass"]}
    # build1 的失败没有被隐藏
    assert res["first_outcome"] == "fail" and atts[0]["id"] in res["attempt_refs"]
    status = L.execution_status(qa)
    assert {"tc": "C1", "outcome": "fail", "build": "build1"}.items() <= next(
        x for x in status["attempts"] if x["id"] == atts[0]["id"]).items()
    report = L.build_report(qa)["test_report"]
    assert {x["tc"]: x["status"] for x in report["first_run"]["rows"]}["C1"] == "fail"
    # 实际观测与参考模型的差异正是该构建的已知缺陷
    assert L.observation_of(qa, atts[0]) != L.oracle_observation("C1", fixtures)
    assert L.observation_of(qa, atts[1]) == L.oracle_observation("C1", fixtures)


# ---------------------------------------------------------------------------
# AC-035 必回归映射并实际执行
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-035")
@pytest.mark.layer("lab")
def test_ac035_required_regression_chain_resolves_to_real_attempts(matrix, fixtures):
    """fixed 构建上：必回归 C1、C2 的 REG→TP→TC→脚本→尝试 每一环都能从登记记录核对，且都有实测结果。"""
    qa = matrix["lab"].qa
    m = matrix["builds"]["fixed"]
    required = fixtures["policy"]["required_regression"]
    assert required == ["C1", "C2"]
    rows = {r["tc"]: r for r in m["status"]["regression"]["rows"]}
    reg_tp = {r["id"]: r["tp_ids"] for r in matrix["analysis"]["regression"]}
    case_tp = {c["id"]: c["tp_ids"] for c in matrix["design"]["cases"]}
    for tc in required:
        row = rows[tc]
        assert row["required"] is True and row["status"] == "pass", row
        # REG → TP → TC
        assert row["reg_id"] in reg_tp and set(reg_tp[row["reg_id"]]) & set(case_tp[tc])
        # TC → 尝试：受控 worker 在当前构建上的真实尝试
        a = m["attempts"][tc]
        assert row["attempt_ref"] == a["id"]
        assert a["build"] == "build2" and a["producer"]["kind"] == "worker" and a["origin"] == "local_lab"
        assert L.receipt(qa, a["worker_receipt"])["data"]["exit_code"] == 0
        assert L.observation_of(qa, a) == L.oracle_observation(tc, fixtures)
        # TC → 脚本：尝试引用的脚本登记了该用例，并有覆盖其摘要的评审决定
        script = L.envelope(qa, a["script_ref"])
        assert tc in script["payload"]["tc_refs"]
        reviews = qa.query("SELECT subjects FROM decisions WHERE role='reviewer' AND result='approve' "
                           "AND revoked_at IS NULL")
        assert any({"ref": script["id"], "sha256": script["_sha256"]} in json.loads(d["subjects"]) for d in reviews)
    assert m["status"]["regression"]["ok"] is True
    assert m["status"]["regression"]["required_passed"] == m["status"]["regression"]["required_total"]


@pytest.mark.ac("AC-035")
@pytest.mark.layer("lab")
def test_ac035_regression_result_lists_full_chain(matrix):
    """规格：回归结果列出 REG→TP→TC→脚本→尝试。检查产品给出的回归结果本身是否带齐五个环节。"""
    m = matrix["builds"]["fixed"]
    rows = [r for r in m["status"]["regression"]["rows"] if r["tc"] in ("C1", "C2")]
    assert len(rows) == 2
    missing = {r["tc"]: [k for k, present in {
        "REG": bool(r.get("reg_id")), "TP": bool(r.get("tp_ids") or r.get("tp")), "TC": bool(r.get("tc")),
        "脚本": bool(r.get("script_ref") or r.get("script")), "尝试": bool(r.get("attempt_ref"))}.items()
        if not present] for r in rows}
    assert all(not v for v in missing.values()), f"回归结果行缺少环节：{missing}；实际字段：{sorted(rows[0])}"


@pytest.mark.ac("AC-035")
@pytest.mark.layer("lab")
@pytest.mark.parametrize("build", ["buggy_duplicate", "fixed", "buggy_neighbor"])
def test_ac035_three_builds_detect_fix_and_neighbor_regression(matrix, factory, fixtures, build):
    """方案 §4.4：buggy_duplicate 上发现缺陷，fixed 上验证修复，buggy_neighbor 上由必回归捕捉相邻回归。"""
    m = matrix["builds"][build]
    assert m["version"]["build"] == build
    assert m["service_sha256"] == factory.receipt["services"][build]["service_sha256"]
    expected_failing = L.EXPECTED_FAILING[build]
    # 独立判定：工厂隔离部署上的直接观测与参考模型比较
    independent = set()
    for case in L.ALL_CASES:
        ds = factory.dataset(f"ac035-{build}-{case}", build)
        direct = L.direct_observation(ds, case, fixtures)
        assert factory.cleanup(ds)["restored"] is True
        if direct != L.oracle_observation(case, fixtures):
            independent.add(case)
        if case != "C1":  # C1 的退款 ID 每次不同，只比较其余用例的原始观测
            assert m["observations"][case] == direct, (build, case)
    assert independent == expected_failing
    # 框架结论与独立判定一致，不能只在 fixed 上全通过
    outcomes = {tc: a["outcome"] for tc, a in m["attempts"].items()}
    assert set(outcomes) == set(L.ALL_CASES)
    assert {tc for tc, o in outcomes.items() if o == "fail"} == expected_failing, outcomes
    assert {tc for tc, o in outcomes.items() if o == "pass"} == set(L.ALL_CASES) - expected_failing
    # 必回归门禁：有必回归用例失败的构建不得通过
    reg = m["status"]["regression"]
    rows = {r["tc"]: r for r in reg["rows"]}
    for tc in fixtures["policy"]["required_regression"]:
        assert rows[tc]["required"] is True
        assert rows[tc]["status"] == ("fail" if tc in expected_failing else "pass")
        assert rows[tc]["attempt_ref"] == m["attempts"][tc]["id"]
    assert reg["ok"] is (not expected_failing)
    assert m["status"]["build"] == m["build_id"]


@pytest.mark.ac("AC-035")
@pytest.mark.layer("lab")
def test_ac035_neighbor_regression_blocks_exit(matrix):
    """矩阵结束时当前构建是 buggy_neighbor：相邻回归 C2 失败，准出规则不得通过。"""
    rep = matrix["report"]
    assert rep["test_report"]["build"] == "build3"
    latest = {x["tc"]: x["status"] for x in rep["test_report"]["latest"]["rows"]}
    assert latest["C2"] == "fail" and latest["C1"] == "pass"
    crit = {c["id"]: c["result"] for c in rep["exit_report"]["criterion_results"]}
    assert crit["regression"] == "fail" and rep["exit_report"]["verdict"] == "fail"
    # 首轮（buggy_duplicate）上的失败仍在报告中
    first = {x["tc"]: x["status"] for x in rep["test_report"]["first_run"]["rows"]}
    assert first["C1"] == "fail" and first["C3"] == "fail"


# ---------------------------------------------------------------------------
# AC-036 隔离用例不能冒充通过
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-036")
@pytest.mark.layer("lab")
def test_ac036_quarantined_required_case_is_not_pass(qa, fixtures):
    L.ready_for_cases(qa, "build2")
    assert L.run_cases(qa, L.ALL_CASES, "regression").rc == 0
    before = L.execution_status(qa)
    assert before["regression"]["ok"] is True  # C1、C2 均实际通过
    total = before["regression"]["required_total"]
    report_before = L.build_report(qa)
    assert {c["id"]: c["result"] for c in report_before["exit_report"]["criterion_results"]}["regression"] == "pass"
    n_attempts = len(L.attempts(qa))

    r = qa.api("execution", "quarantine", "--story", L.STORY, "--tc", "C2", "--reason", "合成：C2 被隔离")
    assert r.rc == 0, r.describe()
    after = L.execution_status(qa)
    assert after["quarantined"] == ["C2"]
    rows = {x["tc"]: x for x in after["regression"]["rows"]}
    assert rows["C2"]["status"] == "quarantined" and rows["C2"]["required"] is True
    assert rows["C1"]["status"] == "pass"
    reg = after["regression"]
    assert reg["required_total"] == total  # 分母没有缩小
    assert reg["required_passed"] == total - 1 and reg["ok"] is False  # 必回归门禁不通过
    assert len(L.attempts(qa)) == n_attempts  # C2 原有尝试仍保留
    # 回归与准出结果
    rep = L.build_report(qa)
    tr = rep["test_report"]
    assert tr["latest"]["denominator"] == len(L.ALL_CASES)
    assert {x["tc"]: x["status"] for x in tr["latest"]["rows"]}["C2"] == "quarantined"
    assert tr["counts"]["quarantined"] == 1 and tr["counts"]["pass"] == len(L.ALL_CASES) - 1
    assert sum(tr["counts"].values()) == len(L.ALL_CASES)
    assert "C2" in tr["unexecuted"]
    ex = rep["exit_report"]
    crit = {c["id"]: c["result"] for c in ex["criterion_results"]}
    assert crit["regression"] == "fail" and crit["required_cases"] == "fail"
    assert ex["verdict"] != "pass"
    assert rep["stage"]["status"] != "completed" and rep["stage"]["verdict"] != "pass"
    execution = L.evaluate(qa, "test_execution")
    assert execution["verdict"] != "pass" and execution["status"] != "completed"
    assert not L.stage_events(qa, "test_exit", "stage_completed")
    # 解除隔离后可继续：C2 恢复为实际结果
    off = qa.api("execution", "quarantine", "--story", L.STORY, "--tc", "C2", "--off", "--reason", "合成：解除隔离")
    assert off.rc == 0, off.describe()
    restored = L.execution_status(qa)["regression"]
    assert restored["ok"] is True and restored["required_total"] == total
