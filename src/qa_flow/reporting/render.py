"""根据已登记事实生成派生报告。渲染不修正业务数据。"""

from __future__ import annotations

import html
import json
from typing import Any

VERDICT_ZH = {"pass": "通过", "fail": "不通过", "indeterminate": "不可判定", "not_applicable": "不适用"}
STATUS_ZH = {"pending": "待处理", "running": "进行中", "blocked": "受阻", "completed": "已完成", "failed": "未通过",
             "stale": "已过期", "waived": "已豁免（不适用）"}
OUTCOME_ZH = {"pass": "通过", "fail": "失败", "error": "执行错误", "blocked": "受阻", "not_run": "未执行",
              "cancelled": "已取消", "quarantined": "已隔离"}


def report_markdown(payload: dict[str, Any], kind: str) -> str:
    lines = [f"# {kind}", ""]
    if "verdict" in payload:
        lines.append(f"结论：{VERDICT_ZH.get(payload['verdict'], payload['verdict'])}")
    for c in payload.get("criterion_results", []):
        lines.append(f"- {c['label']}：{VERDICT_ZH.get(c['result'], c['result'])}"
                     + (f"（{c['reason']}）" if c.get("reason") else ""))
    if "counts" in payload:
        lines.append("")
        lines.append("| 结果 | 数量 |")
        lines.append("|---|---:|")
        for k, v in payload["counts"].items():
            lines.append(f"| {OUTCOME_ZH.get(k, k)} | {v} |")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    lines.append("```")
    return "\n".join(lines) + "\n"


def export_markdown(view: dict[str, Any]) -> str:
    t = view["task"]
    out = [f"# QA 交付报告：{t['display_title']}", ""]
    if view.get("lab"):
        out += ["> **合成实验**：本报告的全部结果来自合成需求、合成数据与本机实验桩，"
                "不代表真实环境验收、真实业务或生产通过。", ""]
    out += [f"- 需求来源：{', '.join(t['sources'])}", f"- 环境：{t['environment']}",
            f"- 当前结论：{view['summary']['headline']}", f"- 导出时间：{view['exported_at']}",
            f"- 数据版本（事件序号）：{view['revision']}", ""]
    out += ["## 阶段状态", "", "| 阶段 | 状态 | 判定 | 原因 |", "|---|---|---|---|"]
    for s in view["stages"]:
        out.append(f"| {s['title']} | {STATUS_ZH.get(s['status'], s['status'])} | "
                   f"{VERDICT_ZH.get(s['verdict'], s['verdict'])} | {'；'.join(s['reasons'])[:200]} |")
    tr = view.get("test_report")
    if tr:
        out += ["", "## 测试结果", "", f"冻结范围：{tr['latest']['denominator']} 条用例；累计尝试 {tr['attempt_total']} 次。", "",
                "| 用例 | 首轮 | 最新 |", "|---|---|---|"]
        first = {r["tc"]: r["status"] for r in tr["first_run"]["rows"]}
        for r in tr["latest"]["rows"]:
            out.append(f"| {r['tc']} | {OUTCOME_ZH.get(first.get(r['tc']), first.get(r['tc']))} | "
                       f"{OUTCOME_ZH.get(r['status'], r['status'])} |")
        cov = tr.get("coverage") or {}
        if cov:
            out += ["", "覆盖（分别计算，不能统称 100% 覆盖）：",
                    f"- 设计覆盖：{cov['design']['numerator']}/{cov['design']['denominator']} 个测试点",
                    f"- 脚本映射：{cov['script_mapping']['numerator']}/{cov['script_mapping']['denominator']} 条用例",
                    f"- 实际执行：{cov['execution']['numerator']}/{cov['execution']['denominator']} 条用例",
                    "- 代码覆盖：未测量"]
    if view.get("defects"):
        out += ["", "## 缺陷", "", "| 缺陷 | 用例 | 严重程度 | 状态 | 平台对象 |", "|---|---|---|---|---|"]
        for d in view["defects"]:
            out.append(f"| {d['id']} | {d['tc_ref']} | {d['severity']} | {d['status']} | {d.get('external_id') or '-'} |")
    if view.get("open_reviews"):
        out += ["", "## 仍待确认", ""]
        for r in view["open_reviews"]:
            out.append(f"- {r['business_question']}（需要：{r['role_label']}）")
    out += ["", "## 未验证范围", ""] + [f"- {x}" for x in view["unverified"]]
    out += ["", "## 本次导出", "", "- 仅生成本地文件；未写入飞书、未修改缺陷、未发布系统。", ""]
    return "\n".join(out)


def export_html(markdown_text: str, title: str) -> str:
    body = html.escape(markdown_text)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{ --bg:#ffffff; --fg:#1f2328; --muted:#59636e; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0d1117; --fg:#e6edf3; --muted:#9198a1; }} }}
body {{ background:var(--bg); color:var(--fg); margin:0; padding:16px; font:14px/1.6 -apple-system,"PingFang SC",sans-serif; }}
pre {{ white-space:pre-wrap; word-break:break-word; max-width:960px; margin:0 auto; }}
</style></head><body><pre>{body}</pre></body></html>
"""
