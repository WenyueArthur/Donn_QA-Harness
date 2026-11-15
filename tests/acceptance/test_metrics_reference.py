"""指标的固定参考值（AC-069、AC-070、AC-073 至 AC-080）。

预期值来自方案 §7 与 lab-fixtures.json，由本文件用整数分子分母自行计算；
被测入口只有 `api metric compute --input <json>`，不调用产品聚合函数。
"""

from __future__ import annotations

import copy
from datetime import datetime
from fractions import Fraction

import pytest

from support import metrics_helpers as mh
from support.cli import write_json

pytestmark = pytest.mark.layer("offline")


# ---- 独立参考模型（纯 Python，只依赖夹具原始输入） -------------------------

def ref_adoption(data: dict) -> tuple[int, int]:
    origins = list(dict.fromkeys(data["origin_ids"]))
    accepted = {d["origin"] for d in data["dispositions"] if d["decision"] == "accepted" and d["origin"] in origins}
    return len(accepted), len(origins)


def ref_modification(data: dict) -> tuple[int, int]:
    origins = list(dict.fromkeys(data["origin_ids"]))
    changed = {d["origin"] for d in data["dispositions"] if d.get("human_semantic_change") and d["origin"] in origins}
    return len(changed), len(origins)


def ref_online(data: dict) -> tuple[int, int]:
    valid = [d for d in data["defects"] if d.get("valid") is True and not d.get("duplicate_of")]
    return sum(1 for d in valid if d["found_in"] == "production"), len(valid)


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def ref_time(data: dict) -> tuple[Fraction, Fraction]:
    """返回（日历小时，暂停并集小时），用秒的整数运算。"""
    start, end = _ts(data["entry_accepted_at"]), _ts(data["exit_decided_at"])
    spans = sorted((max(_ts(p["start"]), start), min(_ts(p["end"]), end)) for p in data["pauses"])
    union, cur_s, cur_e = 0, None, None
    for s, e in spans:
        if e <= s:
            continue
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                union += int((cur_e - cur_s).total_seconds())
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        union += int((cur_e - cur_s).total_seconds())
    return Fraction(int((end - start).total_seconds()), 3600), Fraction(union, 3600)


def test_reference_model_matches_plan_section_7():
    """参考模型自检：与方案 §7 写明的数字一致（3/4、2/4、1/3、28h、6h）。"""
    assert ref_adoption(mh.fixture_data("adoption_metrics")) == (3, 4)
    assert ref_modification(mh.fixture_data("adoption_metrics")) == (2, 4)
    assert ref_online(mh.fixture_data("defect_metrics")) == (1, 3)
    assert ref_time(mh.fixture_data("time_metrics")) == (Fraction(28), Fraction(6))


# ---- AC-073 / AC-074 采纳率 -------------------------------------------------

@pytest.mark.ac("AC-073")
def test_adoption_reference_value(qa, tmp_path):
    data = mh.fixture_data("adoption_metrics")
    r = mh.compute(qa, tmp_path, "ai_adoption", data)
    assert r.rc == 0, r.describe()
    rep = r.json
    mh.assert_ratio(rep, 3, 4)
    assert Fraction(str(rep["value"])) == Fraction(3, 4)
    assert rep["data_status"] == "complete" and rep["missing"] == []
    assert rep["cohort"] == "GEN1"


@pytest.mark.ac("AC-073")
def test_adoption_split_and_human_added_do_not_change_denominator(qa, tmp_path):
    """A4 拆成两条后代、另有人工新增 M1 被采纳：分子分母仍是 3/4。"""
    data = mh.fixture_data("adoption_metrics")
    # 拆分后代各带一条决定记录，仍映射回原始 ID A4
    data["dispositions"].append({"origin": "A4", "decision": "accepted", "descendants": ["A4b"],
                                 "human_semantic_change": True})
    # 纯人工新增用例即使有“已采纳”记录也不属于 AI 原始集合
    data["dispositions"].append({"origin": "M1", "decision": "accepted", "descendants": ["M1"],
                                 "human_semantic_change": False})
    data["human_added_ids"] = ["M1", "M2"]
    r = mh.compute(qa, tmp_path, "ai_adoption", data)
    assert r.rc == 0, r.describe()
    mh.assert_ratio(r.json, 3, 4)


@pytest.mark.ac("AC-074")
def test_adoption_pending_is_incomplete(qa, tmp_path):
    data = mh.fixture_data("adoption_metrics")
    data["dispositions"][2]["decision"] = "pending"  # A3 改为未决
    r = mh.compute(qa, tmp_path, "ai_adoption", data)
    assert r.rc == 0, r.describe()
    rep = r.json
    assert rep["data_status"] == "incomplete", rep
    assert rep["value"] is None, rep
    assert any("A3" in m for m in rep["missing"]), rep


@pytest.mark.ac("AC-074")
def test_adoption_missing_disposition_is_incomplete(qa, tmp_path):
    data = mh.fixture_data("adoption_metrics")
    data["dispositions"] = [d for d in data["dispositions"] if d["origin"] != "A2"]
    r = mh.compute(qa, tmp_path, "ai_adoption", data)
    assert r.rc == 0, r.describe()
    assert r.json["data_status"] == "incomplete" and r.json["value"] is None, r.json


@pytest.mark.ac("AC-074")
def test_adoption_empty_origin_set_is_not_applicable(qa, tmp_path):
    data = {"cohort": "GEN-EMPTY", "origin_ids": [], "dispositions": [], "human_added_ids": ["M1"]}
    r = mh.compute(qa, tmp_path, "ai_adoption", data)
    assert r.rc == 0, r.describe()
    rep = r.json
    assert rep["data_status"] == "not_applicable", rep
    assert rep["value"] is None, rep
    assert rep["denominator"] in (0, None), rep


# ---- AC-075 / AC-076 人工修改率 ---------------------------------------------

@pytest.mark.ac("AC-075")
def test_modification_reference_value(qa, tmp_path):
    r = mh.compute(qa, tmp_path, "human_modification", mh.fixture_data("adoption_metrics"))
    assert r.rc == 0, r.describe()
    mh.assert_ratio(r.json, 2, 4)
    assert Fraction(str(r.json["value"])) == Fraction(1, 2)
    assert r.json["data_status"] == "complete"


def _events_data(events: list[dict]) -> dict:
    data = mh.fixture_data("adoption_metrics")
    data["edit_events"] = events
    return data


@pytest.mark.ac("AC-075")
def test_modification_same_origin_counted_once(qa, tmp_path):
    """A2 有三次人工语义修改、A4 的两条后代各有一次：仍为 2/4。"""
    events = [
        {"id": "E1", "origin": "A2", "actor_kind": "human", "change_kind": "semantic", "verified": True},
        {"id": "E2", "origin": "A2", "actor_kind": "human", "change_kind": "semantic", "verified": True},
        {"id": "E3", "origin": "A2", "actor_kind": "human", "change_kind": "semantic", "verified": True},
        {"id": "E4", "origin": "A4", "actor_kind": "human", "change_kind": "semantic", "verified": True},
        {"id": "E5", "origin": "A4", "actor_kind": "human", "change_kind": "semantic", "verified": True},
        # 人工新增用例的修改不属于原始集合
        {"id": "E6", "origin": "M1", "actor_kind": "human", "change_kind": "semantic", "verified": True},
    ]
    r = mh.compute(qa, tmp_path, "human_modification", _events_data(events))
    assert r.rc == 0, r.describe()
    mh.assert_ratio(r.json, 2, 4)


@pytest.mark.ac("AC-076")
def test_modification_format_only_and_agent_instructed(qa, tmp_path):
    """只改格式不计；Agent 按人工指令修改单列，不算人工修改。"""
    events = [
        {"id": "E1", "origin": "A1", "actor_kind": "human", "change_kind": "format", "verified": True},
        {"id": "E2", "origin": "A2", "actor_kind": "human", "change_kind": "semantic", "verified": True},
        {"id": "E3", "origin": "A3", "actor_kind": "agent_on_human_instruction", "change_kind": "semantic",
         "verified": True},
    ]
    r = mh.compute(qa, tmp_path, "human_modification", _events_data(events))
    assert r.rc == 0, r.describe()
    rep = r.json
    mh.assert_ratio(rep, 1, 4)
    assert rep["data_status"] == "complete", rep
    assert rep.get("agent_on_human_instruction") == 1, f"Agent 按人工指令修改应单列为 1：{rep}"


@pytest.mark.ac("AC-076")
def test_modification_unknown_author_is_incomplete(qa, tmp_path):
    """作者身份不明：输出不完整，不猜为人工修改。"""
    events = [
        {"id": "E1", "origin": "A2", "actor_kind": "human", "change_kind": "semantic", "verified": True},
        {"id": "E2", "origin": "A3", "actor_kind": "unknown", "change_kind": "semantic", "verified": False},
        {"id": "E3", "origin": "A4", "change_kind": "semantic"},
    ]
    r = mh.compute(qa, tmp_path, "human_modification", _events_data(events))
    assert r.rc == 0, r.describe()
    rep = r.json
    assert rep["data_status"] == "incomplete", rep
    assert rep["value"] is None, rep
    assert rep["numerator"] in (1, None), f"身份不明的修改不得计入人工修改：{rep}"
    assert rep["missing"], rep


@pytest.mark.ac("AC-076")
def test_modification_unverified_human_event_not_counted(qa, tmp_path):
    """自称人工但没有核验的编辑事件：不得计为人工修改。"""
    events = [{"id": "E1", "origin": "A1", "actor_kind": "human", "change_kind": "semantic", "verified": False}]
    r = mh.compute(qa, tmp_path, "human_modification", _events_data(events))
    assert r.rc == 0, r.describe()
    rep = r.json
    assert rep["numerator"] in (0, None), rep
    assert rep["data_status"] == "incomplete" and rep["value"] is None, rep


# ---- AC-077 / AC-078 缺陷 ---------------------------------------------------

@pytest.mark.ac("AC-077")
def test_online_defect_ratio_reference_value(qa, tmp_path):
    data = mh.fixture_data("defect_metrics")
    r = mh.compute(qa, tmp_path, "online_defect_ratio", data)
    assert r.rc == 0, r.describe()
    rep = r.json
    mh.assert_ratio(rep, 1, 3)
    assert rep["data_status"] == "complete", rep
    # 显示舍入：1/3 保留两位小数的百分比为 33.33
    assert str(rep.get("display_percent")) == "33.33", rep
    assert rep["filters"]["window"] == data["discovery_window"], rep
    assert "R1" in mh.flat_text(rep)


@pytest.mark.ac("AC-077")
def test_confirmed_escapes_counted_by_confirmation_record(qa, tmp_path):
    """重复单、无效单即使带确认标记也不计；未确认的线上缺陷单列。"""
    data = mh.fixture_data("defect_metrics")
    for d in data["defects"]:
        if d["id"] in ("B4", "B5"):
            d["confirmed_escape"] = True
    data["defects"].append({"id": "B6", "valid": True, "found_in": "production", "confirmed_escape": False})
    r = mh.compute(qa, tmp_path, "confirmed_escapes", data)
    assert r.rc == 0, r.describe()
    rep = r.json
    assert rep["value"] == 1 and rep["numerator"] == 1, rep
    assert rep["unit"] == "count", rep
    assert rep.get("unconfirmed") == ["B6"], f"未确认归因应单列 B6：{rep}"
    # 线上占比同时变为 2/4（B3、B6 为线上有效唯一缺陷）
    r2 = mh.compute(qa, tmp_path, "online_defect_ratio", data, name="with-b6")
    mh.assert_ratio(r2.json, 2, 4)


@pytest.mark.ac("AC-077")
def test_confirmed_escapes_fixture_value(qa, tmp_path):
    r = mh.compute(qa, tmp_path, "confirmed_escapes", mh.fixture_data("defect_metrics"))
    assert r.rc == 0, r.describe()
    assert r.json["value"] == 1, r.json
    assert r.json.get("unconfirmed") == [], r.json


@pytest.mark.ac("AC-078")
@pytest.mark.parametrize("drop", ["discovery_window", "release", "window_start", "window_end"])
def test_online_ratio_missing_scope_or_window_not_computable(qa, tmp_path, drop):
    data = mh.fixture_data("defect_metrics")
    if drop == "window_start":
        data["discovery_window"].pop("start")
    elif drop == "window_end":
        data["discovery_window"].pop("end_exclusive")
    else:
        data.pop(drop)
    r = mh.compute(qa, tmp_path, "online_defect_ratio", data, name=drop)
    assert r.rc == 0, r.describe()
    rep = r.json
    assert rep["data_status"] in ("not_computable", "incomplete"), rep
    assert rep["value"] is None, rep
    assert rep["missing"], rep
    assert "display_percent" not in rep, rep


@pytest.mark.ac("AC-078")
def test_online_ratio_zero_denominator_not_applicable(qa, tmp_path):
    data = mh.fixture_data("defect_metrics")
    data["defects"] = [{"id": "B4", "duplicate_of": "B3", "valid": True, "found_in": "production"},
                       {"id": "B5", "valid": False, "found_in": "production"}]
    r = mh.compute(qa, tmp_path, "online_defect_ratio", data)
    assert r.rc == 0, r.describe()
    rep = r.json
    assert rep["data_status"] == "not_applicable", rep
    assert rep["value"] is None, rep
    assert "display_percent" not in rep or rep["display_percent"] in (None, ""), f"不得输出 0%：{rep}"
    r2 = mh.compute(qa, tmp_path, "online_defect_ratio", dict(data, defects=[]), name="empty")
    assert r2.json["data_status"] == "not_applicable" and r2.json["value"] is None, r2.json


@pytest.mark.ac("AC-078")
def test_online_ratio_missing_validity_not_computed(qa, tmp_path):
    """方案 §7.2：缺少有效性时不计算确定比例（不能把未判定有效性的缺陷当作无效而静默排除）。"""
    data = mh.fixture_data("defect_metrics")
    data["defects"][1].pop("valid")  # B2 的有效性未知
    r = mh.compute(qa, tmp_path, "online_defect_ratio", data)
    assert r.rc == 0, r.describe()
    rep = r.json
    assert rep["value"] is None, f"B2 有效性未知时仍输出了确定比例：{rep}"
    assert rep["data_status"] in ("not_computable", "incomplete"), rep


# ---- AC-079 / AC-080 时间与投入 ---------------------------------------------

@pytest.mark.ac("AC-079")
def test_time_metrics_reference_values(qa, tmp_path):
    data = mh.fixture_data("time_metrics")
    elapsed_ref, union_ref = ref_time(data)
    assert (elapsed_ref, union_ref) == (28, 6)
    r = mh.compute(qa, tmp_path, "elapsed_time", data)
    assert r.rc == 0, r.describe()
    rep = r.json
    assert rep["elapsed_hours"] == 28, rep
    assert rep["pause_union_hours"] == 6, rep
    assert rep["active_hours"] == 22, rep
    assert rep["unit"] == "hours", rep
    r2 = mh.compute(qa, tmp_path, "effort", data)
    assert r2.rc == 0, r2.describe()
    eff = r2.json
    assert eff["person_hours"] == 6, eff
    assert Fraction(str(eff["person_days"])) == Fraction(6, 8), eff
    # 不混用单位：人员投入的主值是人时，不能与日历小时共用一个字段
    assert eff["unit"] == "person_hours", eff
    assert "elapsed_hours" not in eff and "person_hours" not in rep, (rep, eff)


@pytest.mark.ac("AC-079")
def test_person_days_not_derived_from_calendar_time(qa, tmp_path):
    """日工时缺失时不计算人天，也不从日历时间估算。"""
    data = mh.fixture_data("time_metrics")
    data.pop("workday_hours")
    r = mh.compute(qa, tmp_path, "effort", data)
    assert r.rc == 0, r.describe()
    assert r.json["person_hours"] == 6 and r.json["person_days"] is None, r.json
    assert r.json["missing"], r.json


@pytest.mark.ac("AC-079")
def test_pause_outside_interval_is_clipped(qa, tmp_path):
    """与提测—准出区间只部分相交的暂停只扣相交部分。"""
    data = mh.fixture_data("time_metrics")
    data["pauses"].append({"start": "2026-11-17T04:00:00Z", "end": "2026-11-17T09:00:00Z"})  # 相交 1 小时
    data["pauses"].append({"start": "2026-11-15T20:00:00Z", "end": "2026-11-16T00:30:00Z"})  # 完全在区间外
    elapsed_ref, union_ref = ref_time(data)
    assert (elapsed_ref, union_ref) == (28, 7)
    r = mh.compute(qa, tmp_path, "elapsed_time", data)
    assert r.rc == 0, r.describe()
    assert r.json["pause_union_hours"] == 7 and r.json["active_hours"] == 21, r.json


@pytest.mark.ac("AC-080")
def test_illegal_time_order_rejected(qa, tmp_path):
    data = mh.fixture_data("time_metrics")
    bad = dict(data, entry_accepted_at=data["exit_decided_at"], exit_decided_at=data["entry_accepted_at"])
    r = mh.compute(qa, tmp_path, "elapsed_time", bad, name="bad-order")
    assert r.rc == 4, r.describe()
    assert r.error_code == "VALIDATION_FAILED", r.describe()
    assert r.json["committed"] is False and r.json["result"] is None
    # 暂停的结束早于开始同样拒绝
    bad2 = copy.deepcopy(data)
    bad2["pauses"][0] = {"start": "2026-11-16T06:00:00Z", "end": "2026-11-16T02:00:00Z"}
    r2 = mh.compute(qa, tmp_path, "elapsed_time", bad2, name="bad-pause")
    assert r2.rc == 4 and r2.error_code == "VALIDATION_FAILED", r2.describe()
    # 修正后可以继续计算
    r3 = mh.compute(qa, tmp_path, "elapsed_time", data, name="fixed")
    assert r3.rc == 0 and r3.json["elapsed_hours"] == 28, r3.describe()


@pytest.mark.ac("AC-080")
def test_duplicate_effort_event_counted_once(qa, tmp_path):
    data = mh.fixture_data("time_metrics")
    data["effort_entries"] = [
        {"id": "W1", "person": "P1", "hours": 2},
        {"id": "W1", "person": "P1", "hours": 2},  # 同一事件重复到达
        {"id": "W2", "person": "P2", "hours": 4},
        {"id": "W3", "person": "P3", "hours": 4},  # 与 W2 并行的另一人
    ]
    r = mh.compute(qa, tmp_path, "effort", data)
    assert r.rc == 0, r.describe()
    assert r.json["person_hours"] == 10, r.json
    assert Fraction(str(r.json["person_days"])) == Fraction(10, 8), r.json


@pytest.mark.ac("AC-080")
def test_negative_effort_rejected(qa, tmp_path):
    data = mh.fixture_data("time_metrics")
    data["effort_entries"].append({"id": "W9", "person": "P1", "hours": -1})
    r = mh.compute(qa, tmp_path, "effort", data)
    assert r.rc == 4 and r.error_code == "VALIDATION_FAILED", r.describe()


# ---- AC-069 / AC-070 指标报告与效果比较 -------------------------------------

@pytest.mark.ac("AC-069")
def test_metric_report_carries_scope_window_and_counts(qa, tmp_path):
    data = mh.fixture_data("defect_metrics")
    r = mh.compute(qa, tmp_path, "online_defect_ratio", data)
    assert r.rc == 0, r.describe()
    rep = r.json
    assert rep["metric_name"] == "online_defect_ratio"
    assert rep["cohort"] == "R1", f"报告应写明范围：{rep}"
    assert rep["filters"]["window"] == data["discovery_window"], f"报告应写明时间窗：{rep}"
    assert (rep["numerator"], rep["denominator"]) == (1, 3)
    text = mh.flat_text(rep)
    for word in ("提升", "改善", "improve", "下降了", "↑", "↓"):
        assert word not in text, f"报告不得自动生成提升结论：{word}"


@pytest.mark.ac("AC-069")
def test_metric_report_states_source_and_method(qa, tmp_path):
    """规格要求报告包含来源和方法（数据来自哪些记录、分子分母如何界定）。"""
    r = mh.compute(qa, tmp_path, "online_defect_ratio", mh.fixture_data("defect_metrics"))
    assert r.rc == 0, r.describe()
    keys = {k.lower() for k in mh.all_keys(r.json)}
    has_source = any(any(w in k for w in ("source", "input_sha", "input_ref", "来源", "provenance")) for k in keys)
    has_method = any(any(w in k for w in ("method", "formula", "definition", "方法", "口径")) for k in keys)
    assert has_source, f"指标报告缺少数据来源字段；现有字段：{sorted(keys)}"
    assert has_method, f"指标报告缺少计算方法字段；现有字段：{sorted(keys)}"


def _comparison_attempts(qa, tmp_path, before: dict, after: dict):
    """尝试公共接口中可能的比较入口；返回第一个成功的结果。"""
    fb = write_json(tmp_path / "before.json", before)
    fa = write_json(tmp_path / "after.json", after)
    both = write_json(tmp_path / "compare.json", {"metric_name": "compare", "data": {"before": before, "after": after}})
    tried = [
        qa.api("metric", "compare", "--before", str(fb), "--after", str(fa)),
        qa.api("metric", "compute", "--input", str(both)),
        qa.api("metric", "compute", "--input", str(fa), "--baseline", str(fb)),
    ]
    return tried


@pytest.mark.ac("AC-070")
def test_incomplete_baseline_comparison_is_not_comparable(qa, tmp_path):
    """历史基线缺执行数据（有未决原始用例）时请求 AI 前后比较：应输出不可比较及缺项。"""
    before = mh.fixture_data("adoption_metrics")
    before["dispositions"][0]["decision"] = "pending"
    before.pop("cohort")  # 缺模型批次
    after = mh.fixture_data("adoption_metrics")
    tried = _comparison_attempts(qa, tmp_path, {"metric_name": "ai_adoption", "data": before},
                                 {"metric_name": "ai_adoption", "data": after})
    ok = [r for r in tried if r.rc == 0 and isinstance(r.json, dict) and "comparable" in mh.all_keys(r.json)]
    assert ok, "公共接口没有效果比较入口：\n" + "\n---\n".join(r.describe()[:600] for r in tried)
    rep = ok[0].result
    text = mh.flat_text(rep)
    assert rep["comparable"] is False, rep
    assert rep.get("delta") is None, rep
    assert rep.get("problems"), rep
    assert "提升" not in text and "%" not in text, rep


@pytest.mark.ac("AC-070")
def test_incomplete_side_never_filled_with_zero(qa, tmp_path):
    """单侧指标本身：样本不完整时 value 为 null，不填 0。"""
    before = mh.fixture_data("adoption_metrics")
    before["dispositions"] = []
    r = mh.compute(qa, tmp_path, "ai_adoption", before)
    assert r.rc == 0, r.describe()
    assert r.json["value"] is None and r.json["data_status"] == "incomplete", r.json
    assert len(r.json["missing"]) == 4, r.json
