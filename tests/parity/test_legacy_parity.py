"""旧规则等价、迁移边界与验收器自检（AC-103、AC-104、AC-114）。

旧仓库 /Users/donnie/PycharmProjects/qa-skills 全程只读：本文件只读取其文件并计算摘要，
需要改动的实验一律在临时副本中进行。等价比较的预期必须来自预先审定的旧校验输出，
不由新系统在测试运行时动态生成。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from support import flow
from support import metrics_helpers as mh
from support.cli import REPO, blocked, write_json

pytestmark = pytest.mark.layer("parity")

LEGACY = mh.LEGACY_ROOT
MANIFEST = mh.LEGACY_MANIFEST
BASELINE_DIR = REPO / "docs" / "baseline"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args], capture_output=True, text=True,
                          timeout=60).stdout


def _legacy_state() -> dict:
    """旧仓库的只读指纹：冻结文件摘要、qa-mainflow 文件清单、Git 提交、引用与工作区状态。"""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    files = {f["path"]: _sha(LEGACY / f["path"]) if (LEGACY / f["path"]).is_file() else None for f in manifest["files"]}
    listing = sorted(str(p.relative_to(LEGACY)) for p in (LEGACY / "qa-mainflow").rglob("*"))
    return {"files": files, "listing": listing, "head": _git(LEGACY, "rev-parse", "HEAD"),
            "refs": _git(LEGACY, "for-each-ref"), "status": _git(LEGACY, "status", "--porcelain")}


@pytest.fixture()
def legacy_guard():
    if not (LEGACY / "qa-mainflow").is_dir():
        blocked(f"旧仓库不可访问：{LEGACY}")
    before = _legacy_state()
    yield before
    after = _legacy_state()
    assert after["files"] == before["files"], "旧仓库冻结文件的摘要发生变化"
    assert after["listing"] == before["listing"], (
        "旧仓库 qa-mainflow 文件清单发生变化：" + str(sorted(set(after["listing"]) ^ set(before["listing"]))[:10]))
    assert after["head"] == before["head"] and after["refs"] == before["refs"], "旧仓库提交或引用发生变化"
    assert after["status"] == before["status"], "旧仓库工作区状态发生变化"


def _bridge_profile(cfg_dir: Path, manifest_src: Path, project_id: str = "PRJ-PARITY") -> Path:
    """基于实验配置生成使用旧规则桥接的真实项目配置（目标仍只指向本机实验资源）。"""
    profile = json.loads((mh.SRC / "qa_flow" / "resources" / "lab" / "profile.json").read_text(encoding="utf-8"))
    cfg_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(manifest_src, cfg_dir / "legacy-rule-manifest.json")
    profile["profile"] = "project"
    profile["project"] = {"id": project_id, "name": "等价比较（临时项目）"}
    profile["identity"]["adapter"] = "none"
    profile["rules"] = {"kind": "legacy_bridge", "manifest": "legacy-rule-manifest.json",
                        "manifest_sha256": _sha(cfg_dir / "legacy-rule-manifest.json"),
                        "policy": profile["rules"]["policy"]}
    return write_json(cfg_dir / "profile.json", profile)


def _samples(dst: Path, legacy_root: Path = LEGACY) -> Path:
    """正样本取自旧仓库自带示例（只读复制到临时目录）；反样本为缺少必填根字段的报告。"""
    pairs = {
        "req_gate": "qa-mainflow/requirement-quality-gate/examples/order-cancel/订单取消-需求质检报告.json",
        "tech_gate": "qa-mainflow/tech-design-quality-gate/scripts/tests/fixtures/valid_enhanced_tech_gate.json",
        "case_qc": "qa-mainflow/test-case-quality-review/examples/order-cancel/订单取消-用例质检报告.json",
    }
    for stage, rel in pairs.items():
        (dst / stage).mkdir(parents=True, exist_ok=True)
        shutil.copy(legacy_root / rel, dst / stage / "positive.json")
        write_json(dst / stage / "negative.json", {"verdict": "pass", "note": "缺少 schema 等必填根字段"})
    return dst


def _approved_baselines() -> list[Path]:
    out = []
    for p in sorted(BASELINE_DIR.rglob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("schema") == "qa.parity_baseline":
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# AC-104 迁移不改历史和共享安装
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-104")
def test_frozen_manifest_matches_legacy_worktree(legacy_guard):
    """独立逐文件计算摘要：冻结清单与旧仓库当前字节一致，校验器都在清单内，规则来源不是安装镜像。"""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["schema"] == "qa.legacy_rule_manifest"
    assert Path(manifest["legacy_root"]) == LEGACY
    paths = {f["path"] for f in manifest["files"]}
    assert len(paths) == len(manifest["files"]) == 838
    mismatched = [p for p, sha in legacy_guard["files"].items()
                  if sha != next(f["sha256"] for f in manifest["files"] if f["path"] == p)]
    assert not mismatched, f"{len(mismatched)} 个冻结文件与旧仓库不一致：{mismatched[:5]}"
    for stage, v in manifest["validators"].items():
        assert v["script"] in paths, f"{stage} 的校验器不在冻结清单中"
        assert not any(w in Path(v["script"]).name for w in ("finalize", "upload", "install", "sync", "ledger"))
    # 规则来源是旧仓库工作区，不是个人目录下的 Skill 安装镜像
    assert not any(part in (".claude", ".cursor", ".codex", "site-packages") for part in Path(manifest["legacy_root"]).parts)
    assert _git(LEGACY, "rev-parse", "HEAD").strip() == manifest["git_commit"], "旧仓库提交与冻结时不同"


@pytest.mark.ac("AC-104")
def test_migration_rehearsal_leaves_legacy_untouched(qa, tmp_path, legacy_guard):
    """执行新项目导入（配置应用）、能力检查、旧校验器桥接运行：旧仓库字节、清单与 Git 状态不变。"""
    cfg = _bridge_profile(tmp_path / "cfg", MANIFEST)
    s = qa.run("admin", "setup", "--config", str(cfg), "--apply")
    assert s.rc == 0 and s.json["applied"] is True, s.describe()
    d = qa.ok("admin", "doctor").json
    rules = [c for c in d["checks"] if c["item"] == "rules"]
    assert rules and rules[0]["status"] == "ok" and "838" in rules[0]["detail"], rules
    samples = _samples(tmp_path / "samples")
    out = tmp_path / "rehearsal-output.json"
    r = qa.run("admin", "parity-record", "--samples", str(samples), "--output", str(out))
    assert r.rc == 0 and r.json["samples"] == 6, r.describe()
    recorded = {(x["stage"], x["sample"]): x for x in json.loads(out.read_text(encoding="utf-8"))["samples"]}
    for stage in ("req_gate", "tech_gate", "case_qc"):
        pos, neg = recorded[(stage, "positive.json")], recorded[(stage, "negative.json")]
        assert pos["exit_code"] == 0 and pos["verdict"] == "pass", pos
        assert neg["exit_code"] == 1 and neg["verdict"] == "fail" and neg["output"]["errors"], neg
        manifest_sha = next(f["sha256"] for f in json.loads(MANIFEST.read_text("utf-8"))["files"]
                            if f["path"] == json.loads(MANIFEST.read_text("utf-8"))["validators"][stage]["script"])
        assert pos["validator_sha256"] == manifest_sha
    # 未授权不安装、不推送：被测进程的 HOME 中没有任何新文件；旧仓库引用不变由 legacy_guard 核对
    assert not [p for p in qa.home.rglob("*") if p.is_file()], list(qa.home.rglob("*"))
    # 输出只写到指定位置与私有工作区
    assert out.is_file() and qa.workspace().parent == qa.root


@pytest.mark.ac("AC-104")
def test_missing_or_changed_rules_block(qa, tmp_path, legacy_guard):
    """规则缺失或变化就阻断：在旧仓库的临时副本上冻结，再改动副本，新系统必须拒绝运行旧规则。"""
    copy_root = tmp_path / "legacy copy"
    shutil.copytree(LEGACY / "qa-mainflow", copy_root / "qa-mainflow",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules", ".pytest_cache"))
    manifest = tmp_path / "frozen" / "manifest.json"
    f = qa.run("admin", "legacy-freeze", "--legacy-root", str(copy_root), "--output", str(manifest), workspace=False)
    assert f.rc == 0 and f.json["file_count"] > 100, f.describe()
    cfg = _bridge_profile(tmp_path / "cfg", manifest, "PRJ-BLOCK")
    assert qa.run("admin", "setup", "--config", str(cfg), "--apply").rc == 0
    samples = _samples(tmp_path / "samples", copy_root)
    ok = qa.run("admin", "parity-record", "--samples", str(samples), "--output", str(tmp_path / "ok.json"))
    assert ok.rc == 0, ok.describe()

    validator = copy_root / "qa-mainflow" / "requirement-quality-gate" / "scripts" / "validate_report.py"
    original = validator.read_bytes()

    def assert_blocked(label: str):
        out = tmp_path / f"blocked-{label}.json"
        r = qa.run("admin", "parity-record", "--samples", str(samples), "--output", str(out))
        assert r.rc == 6 and r.error_code == "RULES_UNAVAILABLE", f"{label}：规则不可用时没有阻断\n" + r.describe()
        assert not out.exists(), f"{label}：阻断后仍写出了结果"
        checks = [c for c in qa.ok("admin", "doctor").json["checks"] if c["item"] == "rules"]
        assert checks and checks[0]["status"] != "ok", checks

    # 1) 校验器被改动（即使是“恒通过”的改法）
    validator.write_bytes(original + b"\n# changed\n")
    assert_blocked("changed")
    # 2) 校验器缺失：不得改用其他位置的旧脚本
    validator.unlink()
    assert_blocked("missing")
    # 3) 恢复后可以继续
    validator.write_bytes(original)
    again = qa.run("admin", "parity-record", "--samples", str(samples), "--output", str(tmp_path / "again.json"))
    assert again.rc == 0, again.describe()
    # 4) 冻结清单本身被改动（与配置中登记的摘要不符）
    live = qa.workspace() / "config" / "legacy-rule-manifest.json"
    data = json.loads(live.read_text(encoding="utf-8"))
    data["files"] = data["files"][:-1]
    live.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    assert_blocked("manifest-tampered")
    # 5) 旧仓库根整体不可访问
    shutil.rmtree(copy_root)
    assert_blocked("root-missing")


@pytest.mark.ac("AC-104")
def test_setup_rejects_manifest_that_does_not_match(qa, tmp_path, legacy_guard):
    """配置预演：清单中的摘要与旧仓库不符时，配置不得被应用。"""
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    data["files"][0]["sha256"] = "0" * 64
    bad = write_json(tmp_path / "bad" / "legacy-rule-manifest.json", data)
    cfg = _bridge_profile(tmp_path / "cfg", bad, "PRJ-BAD")
    s = qa.run("admin", "setup", "--config", str(cfg), "--apply")
    assert s.rc == 6 and s.json["applied"] is False, s.describe()
    assert any(p["path"] == "rules.manifest" for p in s.json["problems"]), s.json["problems"]
    assert not list(qa.root.rglob("qa.db"))


# ---------------------------------------------------------------------------
# AC-103 旧规则结果逐项等价
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-103")
def test_parity_against_approved_baseline(qa, tmp_path, legacy_guard):
    """用预先审定的旧校验输出逐项比较。没有审定基线时不能用现场生成的输出充当预期。"""
    baselines = _approved_baselines()
    if not baselines:
        blocked("docs/baseline 下没有预先审定的旧校验输出基线（schema=qa.parity_baseline）；"
                "docs/implementation-status.md 也说明正反样本基线尚未录制，需由责任人录制并审定")
    cfg = _bridge_profile(tmp_path / "cfg", MANIFEST)
    assert qa.run("admin", "setup", "--config", str(cfg), "--apply").rc == 0
    for b in baselines:
        samples = b.parent / "samples"
        r = qa.run("admin", "parity-check", "--samples", str(samples), "--baseline", str(b))
        assert r.rc == 0 and r.json["equivalent"] is True and r.json["differences"] == [], r.describe()


@pytest.mark.ac("AC-103")
def test_new_validators_are_migrated_for_comparison(qa):
    """场景前提：旧规则的纯函数已迁入新包，可分别运行原校验器与新校验器。"""
    rep = qa.api_ok("coverage", "export").json
    sys12 = next(i for i in rep["items"] if i["id"] == "SYS-12")
    assert sys12["limitation"] is None, (
        "产品自述旧规则尚未迁入新包，只有只读桥接，无法做“原校验器对新校验器”的逐项比较：" + str(sys12["limitation"]))


@pytest.mark.ac("AC-103")
def test_parity_check_detects_differences(qa, tmp_path, legacy_guard):
    """比较机制本身：基线被改或样本输入变化时必须报告差异（这不是等价结论，只证明比较不会恒通过）。"""
    cfg = _bridge_profile(tmp_path / "cfg", MANIFEST)
    assert qa.run("admin", "setup", "--config", str(cfg), "--apply").rc == 0
    samples = _samples(tmp_path / "samples")
    base = tmp_path / "recorded.json"
    assert qa.run("admin", "parity-record", "--samples", str(samples), "--output", str(base)).rc == 0
    same = qa.run("admin", "parity-check", "--samples", str(samples), "--baseline", str(base))
    assert same.rc == 0 and same.json == {**same.json, "samples": 6, "differences": [], "equivalent": True}
    # 把基线中一个反样本的预期改成“通过”
    data = json.loads(base.read_text(encoding="utf-8"))
    target = next(s for s in data["samples"] if s["stage"] == "req_gate" and s["sample"] == "negative.json")
    target.update({"verdict": "pass", "exit_code": 0})
    forged = write_json(tmp_path / "forged.json", data)
    diff = qa.run("admin", "parity-check", "--samples", str(samples), "--baseline", str(forged))
    assert diff.json["equivalent"] is False, diff.describe()
    fields = {(d.get("sample"), d.get("stage"), d.get("field")) for d in diff.json["differences"]}
    assert ("negative.json", "req_gate", "verdict") in fields and ("negative.json", "req_gate", "exit_code") in fields
    # 样本输入被改动
    write_json(samples / "case_qc" / "negative.json", {"verdict": "fail", "changed": True})
    changed = qa.run("admin", "parity-check", "--samples", str(samples), "--baseline", str(base))
    assert changed.json["equivalent"] is False
    assert {"sample": "negative.json", "kind": "input_changed"} in changed.json["differences"], changed.json


# ---------------------------------------------------------------------------
# AC-114 验收器自检：校验器恒通过时负向验收必须失败
# ---------------------------------------------------------------------------

def negative_checks(qa, tmp: Path) -> dict[str, bool]:
    """四条负向验收断言（拒绝路径、结果伪造、未证实知识、准出失败）。True 表示产品行为符合预期。

    预期全部是冻结在本文件里的常量，不向被测产品询问。
    """
    out: dict[str, bool] = {}
    # 1) 拒绝路径：准出早于提测的非法时序必须被拒绝
    data = mh.fixture_data("time_metrics")
    bad = dict(data, entry_accepted_at=data["exit_decided_at"], exit_decided_at=data["entry_accepted_at"])
    r = mh.compute(qa, tmp, "elapsed_time", bad, name="neg")
    out["illegal_time_order_rejected"] = r.rc == 4 and r.error_code == "VALIDATION_FAILED"
    # 2)–4) 需要一个推进到结束的实验工作区
    try:
        flow.drive(qa)
    except AssertionError:
        pass  # 被破坏的副本可能中途停下；下面只读取已经落盘的事实
    story = mh.story_id(qa)
    # 2) 结果伪造：直接提交执行结果必须被拒绝
    runs_before = qa.query("SELECT COUNT(*) AS n FROM snapshots WHERE kind='execution_run'")[0]["n"]
    forged = write_json(tmp / "forged-run.json", {"cases": [{"tc": "C1", "outcome": "pass"}], "outcome": "pass"})
    f = qa.api("artifact", "submit", "--story", story, "--kind", "execution_run", "--file", str(forged))
    runs_after = qa.query("SELECT COUNT(*) AS n FROM snapshots WHERE kind='execution_run'")[0]["n"]
    out["forged_result_rejected"] = f.rc == 2 and f.error_code == "UNSUPPORTED_CONTRACT" and runs_after == runs_before
    # 3) 与已确认规则冲突的知识候选按自动策略申请入库：必须保持待确认
    cand = {"material_type": "rule", "claims": [{"text": "冲突条目NEG114：退款金额可以为 0", "claim_type": "fact",
                                                  "evidence_refs": [], "environment": "production"}],
            "sources": [], "conflicts": ["已确认规则：退款金额必须大于 0"], "confirmation_method": "policy",
            "risk": "low"}
    k = qa.api("knowledge", "propose", "--story", story, "--file", str(write_json(tmp / "neg-kc.json", cand)))
    qa.api("knowledge", "apply", "--story", story)
    hits = qa.api("knowledge", "search", "--query", "NEG114")
    out["conflicting_knowledge_stays_pending"] = (k.rc == 0 and k.result["status"] == "candidate"
                                                  and hits.rc == 0 and hits.json["results"] == [])
    # 4) 准出失败：缺陷构建期间的准出报告不得为通过
    reports = mh.exit_reports_by_build(qa)
    buggy = [x for x in reports if x["build"] != "build2"]
    out["no_exit_pass_on_buggy_build"] = bool(buggy) and all(x["verdict"] != "pass" for x in buggy)
    return out


PATCHES = [
    # (文件, 原文本, 恒通过的替换)
    ("qa_flow/domain/metrics.py", "    if e < s:\n", "    if False:\n"),
    ("qa_flow/api.py", "        if args.kind in SYSTEM_ONLY:\n", "        if False:\n"),
    ("qa_flow/domain/learning.py",
     'return {"needs_human": needs_human, "policy_confirm_allowed": policy_allowed, "reasons": reasons}',
     'return {"needs_human": False, "policy_confirm_allowed": True, "reasons": []}'),
    # 准出规则恒通过：规则计算结果固定为通过，并去掉“上游未完成不能通过”的保护
    ("qa_flow/application/stages.py", "    rule_verdict = drep.evaluate_criteria(crit)\n", '    rule_verdict = "pass"\n'),
    ("qa_flow/application/stages.py", '    if upstream_missing and rule_verdict == "pass":\n', "    if False:\n"),
]


@pytest.mark.ac("AC-114")
def test_negative_checks_hold_on_real_source(qa, tmp_path):
    """对照组：真实源码下四条负向断言全部成立。"""
    assert mh.module_origin(qa).startswith(str(mh.SRC)), "对照组应加载仓库源码"
    res = negative_checks(qa, tmp_path)
    assert res == {k: True for k in res} and len(res) == 4, res


@pytest.mark.ac("AC-114")
def test_always_pass_validators_make_negative_checks_fail(qa, tmp_path):
    """在临时副本中把校验器改为恒通过：对应的负向验收必须失败；仓库真实源码不被改动。"""
    src_before = mh.tree_digest(mh.SRC)
    status_before = _git(REPO, "status", "--porcelain")
    copy = mh.copy_src(tmp_path / "被破坏的 副本" / "src")
    for rel, old, new in PATCHES:
        mh.patch_text(copy / rel, old, new)
    broken = qa.with_env(PYTHONPATH=str(copy), PYTHONDONTWRITEBYTECODE="1")
    origin = mh.module_origin(broken)
    assert origin.startswith(str(copy)), f"被测进程没有加载被破坏的副本：{origin}"
    res = negative_checks(broken, tmp_path)
    caught = [k for k, ok in res.items() if not ok]
    assert caught, f"校验器恒通过时没有任何负向验收失败：{res}"
    # 每个被破坏的校验器都应被对应的断言抓住
    assert res == {k: False for k in res}, f"部分恒通过的校验器没有被负向验收发现：{res}"
    # 真实源代码未被改动
    assert mh.tree_digest(mh.SRC) == src_before
    assert _git(REPO, "status", "--porcelain") == status_before
