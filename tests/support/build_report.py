"""把一次正式 pytest 运行记录汇总为 verification_report.json 与人读报告（方案 §12）。

用法：python tests/support/build_report.py <artifacts/runs/run-*.json> [--label 名称]
只读取运行记录与规格清单，不调用产品代码，不改写历史运行记录。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SPEC = REPO / "docs" / "qa-flow-rebuild"
ORDER = ["fail", "blocked", "skipped", "pass"]
ZH = {"pass": "通过", "fail": "失败", "blocked": "受阻", "skipped": "跳过", "not_run": "未编写或未执行"}


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(run_file: Path, label: str) -> dict:
    run = json.loads(run_file.read_text(encoding="utf-8"))
    cases = json.loads((SPEC / "acceptance-cases.json").read_text(encoding="utf-8"))["cases"]
    by_ac: dict[str, list[dict]] = {}
    for rec in run["records"]:
        for ac in rec["ac"]:
            by_ac.setdefault(ac, []).append(rec)

    scenarios = []
    for c in cases:
        recs = by_ac.get(c["id"], [])
        statuses = {r["status"] for r in recs}
        status = next((s for s in ORDER if s in statuses), "not_run")
        scenarios.append({
            "id": c["id"], "title": c["title"], "layer": c["layer"], "milestone": c["milestone"],
            "priority": c["priority"], "requirements": c["requirements"], "status": status,
            "tests": [{"nodeid": r["nodeid"], "status": r["status"], "reason": r["reason"],
                       "duration_s": r["duration_s"]} for r in recs]})

    req_status: dict[str, dict] = {}
    for s in scenarios:
        for rid in s["requirements"]:
            d = req_status.setdefault(rid, {"scenarios": {}, "counts": Counter()})
            d["scenarios"][s["id"]] = s["status"]
            d["counts"][s["status"]] += 1
    requirements = []
    for rid in sorted(req_status):
        cnt = req_status[rid]["counts"]
        if cnt["fail"]:
            verdict = "fail"
        elif cnt["not_run"] or cnt["skipped"]:
            verdict = "incomplete"
        elif cnt["blocked"]:
            verdict = "partial_blocked"
        else:
            verdict = "automated_pass"
        requirements.append({"id": rid, "automated_verdict": verdict, "scenarios": req_status[rid]["scenarios"]})

    def table(key: str) -> dict:
        out: dict[str, Counter] = {}
        for s in scenarios:
            out.setdefault(s[key], Counter())[s["status"]] += 1
        return {k: dict(v) for k, v in sorted(out.items())}

    return {
        "schema": "qa-flow.verification_report", "label": label,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "implementation": {"commit": _git("rev-parse", "HEAD"),
                           "worktree_clean": _git("status", "--porcelain") == "",
                           "worktree_changes": _git("status", "--porcelain").splitlines()},
        "spec_digests": {p.name: _sha(p) for p in sorted(SPEC.iterdir()) if p.is_file()},
        "environment": {"os": platform.platform(), "python": sys.version.split()[0],
                        "sqlite": sqlite3.sqlite_version, "executor": "local_subprocess（无网络、凭证或文件隔离）",
                        "model": "实验模型桩（未接入真实模型）", "platform_adapter": "实验平台桩（未接入真实平台）"},
        "run": {"file": str(run_file.relative_to(REPO)), "pytest_args": run["started_args"],
                "pytest_exit_status": run["exit_status"], "finished_at": run["finished_at"],
                "test_functions": len(run["records"])},
        "totals": dict(Counter(s["status"] for s in scenarios)),
        "by_layer": table("layer"), "by_milestone": table("milestone"),
        "scenarios": scenarios, "requirements": requirements,
        "usability": {"status": "not_conducted", "note": "未开展 5 名新人各 5 项任务的真人实测；不得据此声明新人已可用。"},
        "manual_review": {"reviewer": None, "identity_verification": None,
                          "note": "尚无人工审阅人；本报告由自动化验收生成，Agent 不代替人工审阅或签字。"},
        "claims_allowed": ["仅可声明下列自动化场景在本机、合成实验范围内的实际结果"],
        "claims_not_allowed": ["本地实现可验收（存在失败或未执行场景时）", "指定测试平台可用", "生产只读验证通过",
                               "新人可用性已通过"],
    }


def render_md(rep: dict) -> str:
    t = rep["totals"]
    lines = [f"# 验收记录：{rep['label']}", "",
             f"- 生成时间：{rep['generated_at']}",
             f"- 实现提交：`{rep['implementation']['commit']}`；工作区干净：{'是' if rep['implementation']['worktree_clean'] else '否'}",
             f"- 环境：{rep['environment']['os']}；Python {rep['environment']['python']}；SQLite {rep['environment']['sqlite']}",
             f"- 执行命令：`pytest {' '.join(rep['run']['pytest_args'])}`；退出码 {rep['run']['pytest_exit_status']}；测试函数 {rep['run']['test_functions']} 个",
             "- 证据范围：离线与本机合成实验。未接入真实模型、真实平台、原生审批和生产环境；未开展新人实测。", "",
             "## 场景结果", "",
             "| 结果 | 场景数 |", "|---|---:|"]
    for k in ("pass", "fail", "blocked", "skipped", "not_run"):
        if t.get(k):
            lines.append(f"| {ZH[k]} | {t[k]} |")
    lines += [f"| 合计 | {sum(t.values())} |", "", "### 按层", "",
              "| 层 | 通过 | 失败 | 受阻 | 未执行 |", "|---|---:|---:|---:|---:|"]
    for k, v in rep["by_layer"].items():
        lines.append(f"| {k} | {v.get('pass', 0)} | {v.get('fail', 0)} | {v.get('blocked', 0)} | "
                     f"{v.get('not_run', 0) + v.get('skipped', 0)} |")
    lines += ["", "### 按批次", "", "| 批次 | 通过 | 失败 | 受阻 | 未执行 |", "|---|---:|---:|---:|---:|"]
    for k, v in rep["by_milestone"].items():
        lines.append(f"| {k} | {v.get('pass', 0)} | {v.get('fail', 0)} | {v.get('blocked', 0)} | "
                     f"{v.get('not_run', 0) + v.get('skipped', 0)} |")
    for status, title in (("fail", "失败场景"), ("blocked", "受阻场景"), ("not_run", "未编写或未执行的场景")):
        rows = [s for s in rep["scenarios"] if s["status"] == status]
        if not rows:
            continue
        lines += ["", f"## {title}（{len(rows)}）", ""]
        for s in rows:
            lines.append(f"### {s['id']} {s['title']}（{s['layer']}，{s['milestone']}，{'、'.join(s['requirements'])}）")
            for x in s["tests"]:
                if x["status"] == status:
                    reason = " ".join(x["reason"].split())[:400]
                    lines.append(f"- `{x['nodeid']}`：{reason}")
            lines.append("")
    lines += ["", "## 全部场景", "", "| 场景 | 标题 | 层 | 批次 | 结果 | 测试数 |", "|---|---|---|---|---|---:|"]
    for s in rep["scenarios"]:
        lines.append(f"| {s['id']} | {s['title']} | {s['layer']} | {s['milestone']} | {ZH[s['status']]} | {len(s['tests'])} |")
    bad = [r for r in rep["requirements"] if r["automated_verdict"] != "automated_pass"]
    lines += ["", "## 需求覆盖", "",
              f"60 项需求中，{len(rep['requirements']) - len(bad)} 项的全部对应场景自动化通过；"
              f"{len(bad)} 项存在失败、受阻或未执行场景。", "",
              "| 需求 | 自动化结论 | 非通过场景 |", "|---|---|---|"]
    for r in bad:
        non = "、".join(f"{k}（{ZH[v]}）" for k, v in r["scenarios"].items() if v != "pass")
        lines.append(f"| {r['id']} | {r['automated_verdict']} | {non} |")
    lines += ["", "## 结论边界", "",
              "- 自动化通过只证明对应场景在本机合成实验中的行为，不证明需求语义没有遗漏。",
              "- 受阻场景不计入通过；真实接入与新人实测完成前，不能声明全范围验收通过。",
              f"- 人工审阅：{rep['manual_review']['note']}", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_file")
    ap.add_argument("--label", default="第 1 轮独立验收")
    ap.add_argument("--output-dir", default=str(REPO / "artifacts"))
    a = ap.parse_args()
    rep = build(Path(a.run_file).resolve(), a.label)
    out = Path(a.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = Path(a.run_file).stem.replace("run-", "verification-")
    (out / f"{stem}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / f"{stem}.md").write_text(render_md(rep), encoding="utf-8")
    print(json.dumps({"json": str(out / f"{stem}.json"), "md": str(out / f"{stem}.md"), "totals": rep["totals"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
