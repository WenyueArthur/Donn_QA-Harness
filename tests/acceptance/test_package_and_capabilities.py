"""干净安装、任意路径、能力探测与真实展示（AC-099、AC-100、AC-101、AC-102）。

AC-099 在临时目录的源码副本上离线构建 wheel，并安装到全新的虚拟环境；不改动仓库与仓库的 .venv。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from support import flow
from support import isolation_probe
from support import metrics_helpers as mh
from support.cli import REPO, blocked, write_json


# ---------------------------------------------------------------------------
# AC-099 干净安装
# ---------------------------------------------------------------------------

def _git_status() -> str:
    return subprocess.run(["git", "--no-optional-locks", "-C", str(REPO), "status", "--porcelain"],
                          capture_output=True, text=True, timeout=30).stdout


@pytest.fixture(scope="module")
def clean_install(tmp_path_factory):
    """构建 wheel 并安装到新虚拟环境（路径含中文与空格）。返回环境信息。"""
    base = tmp_path_factory.mktemp("干净 安装")
    home = base / "home"
    home.mkdir()
    py = mh.base_python()
    probe = mh.run_plain([py, "-c", "import setuptools, pip; print(setuptools.__version__)"], base, home)
    if probe.returncode != 0:
        blocked("基础解释器没有 setuptools，离线无法构建 wheel（构建需要联网下载 setuptools）")
    repo_status = _git_status()
    src_before = mh.tree_digest(mh.SRC)
    # 在源码副本上构建，避免在仓库内留下 build/ 与 egg-info
    stage = base / "build src"
    stage.mkdir()
    shutil.copy(REPO / "pyproject.toml", stage / "pyproject.toml")
    shutil.copy(REPO / "README.md", stage / "README.md")
    mh.copy_src(stage / "src")
    dist = base / "dist"
    b = mh.run_plain([py, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--no-index", "-w", str(dist),
                      str(stage)], base, home)
    assert b.returncode == 0, f"wheel 构建失败\n{b.stdout[-2000:]}\n{b.stderr[-2000:]}"
    wheels = list(dist.glob("qa_flow-*.whl"))
    assert len(wheels) == 1, list(dist.iterdir())
    venv = base / "新 环境" / "venv"
    v = mh.run_plain([py, "-m", "venv", str(venv)], base, home)
    assert v.returncode == 0, v.stderr
    vpy = str(venv / "bin" / "python")
    i = mh.run_plain([vpy, "-m", "pip", "install", "--no-index", "--no-deps", str(wheels[0])], base, home)
    assert i.returncode == 0, f"安装失败\n{i.stdout[-2000:]}\n{i.stderr[-2000:]}"
    shutil.rmtree(stage)  # 构建用的源码副本不再存在：安装结果不能依赖它
    return {"base": base, "home": home, "venv": venv, "python": vpy, "wheel": wheels[0],
            "repo_status": repo_status, "src_before": src_before}


@pytest.mark.ac("AC-099")
@pytest.mark.layer("package")
def test_wheel_contains_own_resources_and_no_runtime_deps(clean_install):
    with zipfile.ZipFile(clean_install["wheel"]) as z:
        names = set(z.namelist())
        meta = next(n for n in names if n.endswith(".dist-info/METADATA"))
        metadata = z.read(meta).decode("utf-8")
    for res in ("qa_flow/resources/requirements.json", "qa_flow/resources/stages.json",
                "qa_flow/resources/usability_spec.json", "qa_flow/resources/lab/profile.json",
                "qa_flow/resources/lab/rules.json", "qa_flow/resources/lab/demo/refund-requirement.md",
                "qa_flow/resources/lab/demo/refund-tech.md", "qa_flow/resources/lab/demo/refund-selftest.md",
                "qa_flow/resources/lab/scripts/refund_cases.py", "qa_flow/resources/lab/scripts/refund_service.py"):
        assert res in names, f"wheel 缺少自身资源 {res}"
    assert not [n for n in names if n.startswith(("tests/", "docs/"))]
    # 真实运行时依赖：只有标准库，元数据中不应声明第三方依赖
    assert "Requires-Dist" not in metadata, metadata
    assert "Requires-Python: >=3.13" in metadata, metadata
    show = mh.run_plain([clean_install["python"], "-m", "pip", "show", "qa-flow"], clean_install["base"],
                        clean_install["home"])
    assert show.returncode == 0, show.stderr
    fields = dict(line.split(": ", 1) for line in show.stdout.splitlines() if ": " in line)
    assert fields.get("Requires", "") == "" and str(clean_install["venv"]) in fields["Location"], show.stdout
    assert "Editable project location" not in show.stdout


@pytest.mark.ac("AC-099")
@pytest.mark.layer("package")
def test_installed_package_runs_lab_flow_from_any_directory(clean_install):
    base = clean_install["base"]
    qa = mh.new_qa(base / "运行 目录" / "子 目录", root_name="工作区 中文 空格", python=clean_install["python"])
    # 被测进程加载的是虚拟环境里的包，不是仓库源码
    origin = mh.module_origin(qa)
    assert origin.startswith(str(clean_install["venv"])), origin
    assert str(REPO) not in origin
    # 只读命令：从其他目录、空 HOME 运行
    h = qa.text("--help", workspace=False, non_interactive=False)
    assert h.rc == 0 and "demo" in h.out, h.describe()
    d = qa.run("admin", "doctor")
    assert d.rc == 0, d.describe()
    ver = mh.run_plain([clean_install["python"], "-c",
                        "import sys, sqlite3; print(sys.version.split()[0]); print(sqlite3.sqlite_version)"],
                       qa.cwd, qa.home)
    py_ver, sqlite_ver = ver.stdout.split()
    assert d.json["python"] == py_ver and d.json["sqlite"] == sqlite_ver, "doctor 应记录真实运行时版本"
    cov = qa.api("coverage", "export")
    assert cov.rc == 0 and len(cov.json["items"]) == 60, cov.describe()[:800]
    # 受控 lab 流程：完整走到准出与知识回流
    v = flow.drive(qa)
    st = flow.stage_map(v)
    for stage in ("req_gate", "case_design", "test_entry", "test_execution", "test_exit", "retrospective"):
        assert st[stage]["status"] == "completed", (stage, st[stage])
    ws = qa.workspace()
    assert ws.parent == qa.root and (ws / "qa.db").is_file()
    assert sorted(d["status"] for d in v["defects"]) == ["closed", "closed"]
    # 不使用 --workspace：以当前目录定位工作区，不依赖仓库路径
    other = base / "另一个 目录"
    other.mkdir()
    r = qa.run("demo", cwd=other, workspace=False)
    assert r.rc == 0, r.describe()
    created = [p for p in other.rglob("qa.db")]
    assert len(created) == 1, created
    # 运行期间没有写入 HOME，也没有改动仓库
    assert not [p for p in qa.home.rglob("*") if p.is_file()], list(qa.home.rglob("*"))
    assert _git_status() == clean_install["repo_status"]
    assert mh.tree_digest(mh.SRC) == clean_install["src_before"]


@pytest.mark.ac("AC-099")
@pytest.mark.layer("package")
def test_installed_package_does_not_need_home(clean_install):
    """HOME 指向不存在的目录时，只读命令与 demo 仍可运行（不读取个人目录中的 Skill 镜像或配置）。"""
    base = clean_install["base"]
    qa = mh.new_qa(base / "无 HOME", python=clean_install["python"])
    ghost = base / "不存在的 home"
    qa = mh.PyQA(qa.root, ghost, qa.cwd, python=clean_install["python"])
    r = qa.run("demo")
    assert r.rc == 0, r.describe()
    assert qa.run("status").rc == 0
    assert not ghost.exists(), "不应创建 HOME 目录"


# ---------------------------------------------------------------------------
# AC-100 不支持的能力不静默降级
# ---------------------------------------------------------------------------

@pytest.mark.ac("AC-100")
@pytest.mark.layer("package")
def test_sync_drive_workspace_is_rejected(tmp_path):
    """工作区根位于同步网盘目录（按目录名识别）：demo 与 admin setup 均拒绝，且不创建任何内容。"""
    qa = mh.new_qa(tmp_path / "Dropbox" / "团队 共享", root_name="ws")
    r = qa.run("demo")
    assert r.rc == 6 and r.error_code == "WORKSPACE_INVALID", r.describe()
    assert "同步网盘" in r.errors[0]["message"] or "网络" in r.errors[0]["message"]
    assert not list(qa.root.rglob("qa.db"))
    profile = json.loads((REPO / "src" / "qa_flow" / "resources" / "lab" / "profile.json").read_text("utf-8"))
    cfg = write_json(tmp_path / "cfg" / "profile.json", profile)
    s = qa.run("admin", "setup", "--config", str(cfg), "--apply")
    assert s.rc == 6 and s.json["applied"] is False, s.describe()
    assert any(p["path"] == "workspace" for p in s.json["problems"]), s.json["problems"]
    assert not list(qa.root.iterdir())
    # 修正位置后可以继续，没有永久误锁
    ok = mh.new_qa(tmp_path / "本地 目录", root_name="ws")
    assert ok.run("admin", "setup", "--config", str(cfg), "--apply").rc == 0


@pytest.mark.ac("AC-100")
@pytest.mark.layer("package")
def test_doctor_states_boundaries_without_claiming_platform_acceptance(tmp_path):
    qa = mh.new_qa(tmp_path / "plain")
    qa.ok("demo")
    d = qa.ok("admin", "doctor").json
    iso = d["executor"]["isolation"]
    for cap in ("credentials", "filesystem", "network", "process_tree_cleanup"):
        assert isinstance(iso.get(cap), bool), f"doctor 应逐项列出执行器能力 {cap}: {iso}"
    # 网络隔离的声明必须与本机实际机制一致（独立探测，不调用产品内部函数）
    actual_network = isolation_probe.os_network_sandbox_works()
    assert iso["network"] is actual_network, f"doctor 声明 network={iso['network']}，本机实际探测为 {actual_network}"
    assert iso["filesystem"] is False and d["executor"]["untrusted_scripts"] == "不支持"
    text = json.dumps(d, ensure_ascii=False)
    assert "未完成任何平台的验收" in text, "doctor 不得宣称全平台验收通过"
    for claim in ("全平台验收通过", "所有平台已验证", "all platforms verified"):
        assert claim not in text
    assert d["workspace"]["journal_mode"] and d["workspace"]["integrity"] == "ok"
    human = qa.text("admin", "doctor").out
    assert "process_tree_cleanup" in human and f"network={'有' if actual_network else '无'}" in human, human


@pytest.mark.ac("AC-100")
@pytest.mark.layer("package")
def test_doctor_reports_unsupported_workspace_location(tmp_path):
    """doctor 指向同步网盘位置时，应明确说明该位置不支持，而不是只显示“未选择工作区”。"""
    qa = mh.new_qa(tmp_path / "Dropbox" / "共享", root_name="ws")
    d = qa.run("admin", "doctor")
    assert d.rc == 0, d.describe()
    text = json.dumps(d.json, ensure_ascii=False)
    assert "同步网盘" in text or "网络文件系统" in text or "网络共享" in text, (
        "doctor 没有报告工作区位置不受支持：\n" + json.dumps(d.json["checks"], ensure_ascii=False))


@pytest.mark.ac("AC-100")
@pytest.mark.layer("package")
def test_existing_workspace_on_sync_drive_refuses_scheduling(tmp_path):
    """已有工作区被放到同步网盘目录：只读查询可用并单独显示，执行调度必须拒绝。"""
    src = mh.new_qa(tmp_path / "本地")
    src.ok("demo")
    shared_root = tmp_path / "OneDrive" / "共享 盘" / "ws"
    shutil.copytree(src.root, shared_root)
    qa = mh.QA(shared_root, src.home, src.cwd)
    st = qa.run("status")
    assert st.rc == 0, "只读查询应仍可用\n" + st.describe()
    before = qa.query("SELECT COUNT(*) AS n FROM actions")[0]["n"]
    attempts_before = qa.query("SELECT COUNT(*) AS n FROM snapshots WHERE kind='case_attempt'")[0]["n"]
    # 按日常方式继续：答复自己负责的事项、转交他人事项、resume；记录每次命令的退出码与错误码
    outcomes = []
    for _ in range(4):
        v = qa.run("status").result
        for c in v["reviews"]["mine"]:
            a = qa.run("review", "--answer", f"{c['handle']}={c['options'][0]['key']}")
            outcomes.append(("review", a.rc, a.error_code))
        for c in v["reviews"]["others"]:
            if c.get("kind") == "关键业务预期确认" and c.get("status") in (None, "open", "pending"):
                qa.run("review", "--transfer", f"{c['handle']}=product")
        r = qa.run("resume")
        outcomes.append(("resume", r.rc, r.error_code))
    after = qa.query("SELECT COUNT(*) AS n FROM actions")[0]["n"]
    attempts_after = qa.query("SELECT COUNT(*) AS n FROM snapshots WHERE kind='case_attempt'")[0]["n"]
    assert after == before and attempts_after == attempts_before, (
        f"位于同步网盘的工作区仍调度了执行：新增操作 {after - before}，新增用例尝试 "
        f"{attempts_after - attempts_before}；命令结果 {outcomes}")
    assert any(code in ("WORKSPACE_INVALID", "CAPABILITY_UNAVAILABLE", "UNSUPPORTED_CAPABILITY",
                        "DEPENDENCY_UNAVAILABLE") for _, _, code in outcomes), outcomes
    assert after == before
    d = qa.run("admin", "doctor").json
    assert "同步网盘" in json.dumps(d, ensure_ascii=False)


# ---------------------------------------------------------------------------
# AC-101 / AC-102 能力展示
# ---------------------------------------------------------------------------

COLUMNS = ("planned", "implemented", "offline_verified", "external_verified")


def _export(qa, tmp: Path, cases: list[dict] | None, name: str = "v"):
    args = ["coverage", "export"]
    if cases is not None:
        args += ["--verification", str(write_json(tmp / f"{name}.json", {"cases": cases}))]
    r = qa.api(*args)
    assert r.rc == 0, r.describe()[:1500]
    return r.json


def _items(rep: dict) -> dict[str, dict]:
    return {i["id"]: i for i in rep["items"]}


def _assert_no_single_percentage(rep: dict):
    keys = {k.lower() for k in mh.all_keys(rep)}
    bad = [k for k in keys if any(w in k for w in ("percent", "ratio", "progress", "completion", "score", "完成率"))]
    assert not bad, f"能力报告出现单一完成度字段：{bad}"
    text = json.dumps(rep, ensure_ascii=False)
    assert "100%" not in text and "已全部完成" not in text


@pytest.mark.ac("AC-101")
@pytest.mark.layer("offline")
def test_capability_report_columns_come_from_registry_and_results(qa, tmp_path, spec):
    reqs = {r["id"]: r for r in spec["requirements"]["requirements"]}
    # 取规格中第一个 external 层场景，且其需求与 MET-01、MET-04 不重叠
    ext_id = next(c["id"] for c in spec["cases"] if c["layer"] == "external"
                  and not set(c["requirements"]) & {"MET-01", "MET-04"})
    cases = [
        {"id": "AC-073", "requirements": ["MET-01"], "layer": "offline", "result": "pass"},
        {"id": "AC-074", "requirements": ["MET-01"], "layer": "offline", "result": "pass"},
        {"id": ext_id, "requirements": reqs_for(spec, ext_id), "layer": "external", "result": "pass"},
        {"id": "AC-079", "requirements": ["MET-04"], "layer": "offline", "result": "blocked"},
    ]
    rep = _export(qa, tmp_path, cases)
    items = _items(rep)
    assert set(items) == set(reqs) and len(items) == 60
    for rid, it in items.items():
        for col in COLUMNS:
            assert isinstance(it[col], bool), f"{rid} 的 {col} 应单独成列"
        assert it["planned"] is True
        assert it["title"] == reqs[rid]["title"] and it["test_ids"] == reqs[rid]["test_ids"]
    assert items["MET-01"]["offline_verified"] is True and items["MET-01"]["offline_evidence"] == ["AC-073", "AC-074"]
    assert items["MET-01"]["external_verified"] is False
    assert items["MET-04"]["offline_verified"] is False, "blocked 不能算已验证"
    ext_reqs = reqs_for(spec, ext_id)
    assert layer_of(spec, ext_id) == "external" and ext_reqs
    for rid in ext_reqs:
        assert items[rid]["external_verified"] is True and items[rid]["external_evidence"] == [ext_id], items[rid]
        assert items[rid]["offline_verified"] is False, "外部接入通过不能反推本地验证"
    untouched = [i for rid, i in items.items() if rid not in {"MET-01", *ext_reqs}]
    assert all(not i["offline_verified"] and not i["external_verified"] for i in untouched)
    assert rep["totals"] == {"planned": 60, "implemented": sum(i["implemented"] for i in items.values()),
                             "offline_verified": 1, "external_verified": len(ext_reqs)}
    _assert_no_single_percentage(rep)
    # --output 导出与标准输出一致
    out = tmp_path / "能力 报告.json"
    r = qa.api("coverage", "export", "--verification", str(tmp_path / "v.json"), "--output", str(out))
    assert r.rc == 0 and json.loads(out.read_text(encoding="utf-8")) == r.json


def reqs_for(spec: dict, ac: str) -> list[str]:
    return next(c["requirements"] for c in spec["cases"] if c["id"] == ac)


def layer_of(spec: dict, ac: str) -> str:
    return next(c["layer"] for c in spec["cases"] if c["id"] == ac)


@pytest.mark.ac("AC-101")
@pytest.mark.layer("offline")
def test_capability_report_evidence_links_resolve(qa, tmp_path):
    """implemented 列的证据链接应指向真实存在的源码位置。"""
    rep = _export(qa, tmp_path, None)
    broken = []
    for it in rep["items"]:
        if it["implemented"]:
            assert it["implementation_refs"], f"{it['id']} 标为已实现但没有证据链接"
        for ref in it["implementation_refs"]:
            path, _, symbol = ref.partition(":")
            f = (REPO / path) if path == "pyproject.toml" else (mh.SRC / "qa_flow" / path)
            if not f.is_file():
                broken.append(f"{it['id']}: {ref}（文件不存在）")
                continue
            name = symbol.split("(")[0].strip()
            if name and name not in f.read_text(encoding="utf-8"):
                broken.append(f"{it['id']}: {ref}（文件中没有 {name}）")
    assert not broken, "能力报告的实现证据链接无法解析：\n" + "\n".join(broken)


@pytest.mark.ac("AC-101")
@pytest.mark.layer("offline")
def test_verification_must_match_registered_cases(qa, tmp_path):
    """验收结果只能按注册的需求—场景对应关系计入：记录自称覆盖无关需求时不得计入。"""
    cases = [{"id": "AC-073", "requirements": ["FLOW-01", "SYS-05"], "layer": "offline", "result": "pass"}]
    items = _items(_export(qa, tmp_path, cases))
    for rid in ("FLOW-01", "SYS-05"):
        assert "AC-073" not in items[rid]["test_ids"]
        assert items[rid]["offline_verified"] is False, (
            f"{rid} 的注册场景是 {items[rid]['test_ids']}，却因无关的 AC-073 记录显示为已验证")


@pytest.mark.ac("AC-102")
@pytest.mark.layer("offline")
def test_skill_names_without_results_do_not_count(qa, tmp_path, spec):
    """给全部步骤填技能名，但执行与验收记录为空：真实验证数量仍为 0。"""
    named = [{"id": c["id"], "requirements": c["requirements"], "layer": c["layer"], "skill": f"skill-{c['id'].lower()}",
              "skill_name": "已填写", "owner": "someone"} for c in spec["cases"]]
    for label, cases in (("none", None), ("empty", []), ("named", named),
                         ("unknown", [dict(c, result="unknown") for c in named]),
                         ("not-run", [dict(c, result="", status="pass") for c in named])):
        rep = _export(qa, tmp_path, cases, label)
        assert rep["totals"]["offline_verified"] == 0 and rep["totals"]["external_verified"] == 0, (label, rep["totals"])
        assert all(not i["offline_verified"] and not i["external_verified"] for i in rep["items"]), label
        assert all(i["offline_evidence"] == [] and i["external_evidence"] == [] for i in rep["items"]), label
        assert rep["totals"]["planned"] == 60
        _assert_no_single_percentage(rep)


@pytest.mark.ac("AC-102")
@pytest.mark.layer("offline")
def test_failed_and_unknown_results_are_visible(qa, tmp_path):
    """同一需求下一个场景通过、另一个失败：失败必须可见，不能只显示“已验证”。"""
    cases = [
        {"id": "AC-073", "requirements": ["MET-01"], "layer": "offline", "result": "pass"},
        {"id": "AC-074", "requirements": ["MET-01"], "layer": "offline", "result": "fail"},
        {"id": "AC-079", "requirements": ["MET-04"], "layer": "offline", "result": "fail"},
    ]
    rep = _export(qa, tmp_path, cases)
    items = _items(rep)
    assert items["MET-04"]["offline_verified"] is False
    met04 = json.dumps({k: v for k, v in items["MET-04"].items() if k != "test_ids"}, ensure_ascii=False)
    assert "AC-079" in met04 or "fail" in met04, f"MET-04 的失败记录在报告中不可见：{items['MET-04']}"
    met01 = json.dumps({k: v for k, v in items["MET-01"].items() if k != "test_ids"}, ensure_ascii=False)
    assert items["MET-01"]["offline_verified"] is False or "AC-074" in met01.replace('"offline_evidence": ["AC-073"]', ""), (
        f"MET-01 有失败场景 AC-074，却只显示已验证：{items['MET-01']}")
