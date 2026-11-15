"""命令行：参数、统一回执、展示与退出码。不包含评分或准出规则。

日常帮助只展示四类任务与五个日常命令（另有 demo）；内部命令放在 api 与 admin 帮助中。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from qa_flow import clock
from qa_flow.errors import EXIT_OK, QAError
from qa_flow.security.sanitize import scrub_for_display

DAILY_HELP = """日常只需四类任务：
  开始一个需求      start --source <需求文件或链接>
  继续未完成工作    resume
  处理业务确认      review
  查看或交付        status；export --output <目录>
首次体验：demo（合成实验，不需要账号或网络）

管理员命令：admin --help；开发与排障的机器接口：api --help
"""


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # 参数错误统一为退出码 2
        raise QAError("INVALID_ARGUMENT", f"参数错误：{message}")


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--workspace", help="工作区根目录（日常使用无需填写）")
    p.add_argument("--json", action="store_true", help="输出机器回执")
    p.add_argument("--details", action="store_true", help="展示证据与详情")
    p.add_argument("--non-interactive", action="store_true", help="不等待终端输入；缺少选择时立即返回")


def build_parser() -> argparse.ArgumentParser:
    p = Parser(prog="python -m qa_flow", description="QA 全流程（受控实现）", epilog=DAILY_HELP,
               formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", metavar="<命令>", parser_class=Parser)

    d = sub.add_parser("demo", help="离线首次实验（合成数据）")
    _common(d)
    s = sub.add_parser("start", help="开始一个需求")
    _common(s)
    s.add_argument("--source", action="append", required=True, help="需求文件或链接，可重复")
    s.add_argument("--title", help="来源无法识别名称时提供业务名称")
    s.add_argument("--project", help="存在多个项目时按名称选择")
    r = sub.add_parser("resume", help="继续未完成工作")
    _common(r)
    r.add_argument("--task", help="存在多个需求时按业务名称选择")
    rv = sub.add_parser("review", help="处理待确认事项")
    _common(rv)
    rv.add_argument("--task")
    rv.add_argument("--answer", action="append", default=[], help="逐项答复，如 Q1=A")
    rv.add_argument("--note", action="append", default=[], help="答复说明，如 Q1=原因")
    rv.add_argument("--transfer", action="append", default=[], help="转交责任人，如 Q2=product")
    st = sub.add_parser("status", help="查看进度与下一步")
    _common(st)
    st.add_argument("--task")
    ex = sub.add_parser("export", help="导出本地交付报告")
    _common(ex)
    ex.add_argument("--task")
    ex.add_argument("--output", required=True)

    ad = sub.add_parser("admin", help="管理员：配置与能力检查")
    _common(ad)
    asub = ad.add_subparsers(dest="admin_cmd", metavar="<管理命令>", parser_class=Parser)
    a = asub.add_parser("setup", help="预览并原子应用项目配置")
    _common(a)
    a.add_argument("--config", required=True)
    a.add_argument("--name", help="工作区目录名（默认项目 ID）")
    a.add_argument("--apply", action="store_true")
    a = asub.add_parser("doctor", help="报告运行时版本、能力与边界")
    _common(a)
    a = asub.add_parser("legacy-freeze", help="M0：冻结旧仓库规则源码摘要（只读）")
    _common(a)
    a.add_argument("--legacy-root", required=True)
    a.add_argument("--output", required=True)
    a = asub.add_parser("parity-record", help="用冻结旧校验器记录样本基线")
    _common(a)
    a.add_argument("--samples", required=True)
    a.add_argument("--output", required=True)
    a = asub.add_parser("parity-check", help="对照基线检查旧规则输出是否一致")
    _common(a)
    a.add_argument("--samples", required=True)
    a.add_argument("--baseline", required=True)
    a = asub.add_parser("expire-evidence", help="按保留策略销毁私有证据（记录销毁事件）")
    _common(a)
    a.add_argument("--id", required=True)
    a.add_argument("--reason", required=True)

    api = sub.add_parser("api", help="机器接口（开发与排障）",
                         description="所有变更命令内部使用 command_id/expected_revision；调用者可显式提供。")
    _common(api)
    from qa_flow import api as api_mod

    api_mod.register(api)
    return p


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _receipt(result: dict[str, Any], *, command_id: str | None = None, revision: int | None = None,
             status: str = "ok", warnings: list[str] | None = None) -> dict[str, Any]:
    return {"command_id": command_id, "status": status, "committed": status == "ok", "revision": revision,
            "result": result, "errors": [], "warnings": warnings or []}


def _print_view(v: dict[str, Any], details: bool, footer: str = "本次：只读查看，未执行任何操作，未修改缺陷或发布系统。") -> None:
    t = v["task"]
    lab = "（合成数据）" if v["lab"] and "合成" not in t["display_title"] else ""
    print(f"需求：{t['display_title']}{lab}")
    print(f"来源：{'、'.join(t['sources'])}    环境：{t['environment']}")
    s = v["summary"]
    print(f"当前：{s['headline']}。")
    if s["reasons"]:
        print("原因：" + "；".join(s["reasons"][:3]) + ("。" if not s["reasons"][0].endswith("。") else ""))
    if s["have"]:
        print("已有：" + "、".join(s["have"]) + " 有效。")
    for u in v["unknown_actions"]:
        print(f"结果未知：{u['kind']}（{u.get('error') or '待核对'}）——不会重发，继续时先核对。")
    if v["reviews"]["others"]:
        print("需他人决定：" + "；".join(f"{c['business_question']}（{c['role_label']}）" for c in v["reviews"]["others"]))
    for item in v.get("admin_items") or []:
        print(f"需管理员处理：{item}")
    if v.get("injection_hints"):
        print("提示：来源中含诱导性文字（如“" + v["injection_hints"][0] + "”），已仅作为资料处理，不影响授权。")
    for n in s["next_actions"][:3]:
        print(f"下一步：{n['effect']}；使用 “{n['command']}”（范围：{n['scope']}；{n['risk']}）。")
    if v.get("worker_last_tick_at") is None:
        print("同步：worker 未运行，自动刷新与提醒未启用。")
    if v["lab"]:
        print("说明：以上为合成实验结果，不代表真实环境或生产通过。")
    print(footer)
    if details:
        print("\n阶段详情：")
        from qa_flow.reporting.render import STATUS_ZH, VERDICT_ZH

        for st in v["stages"]:
            print(f"  - {st['title']}：{STATUS_ZH.get(st['status'], st['status'])}／{VERDICT_ZH.get(st['verdict'], st['verdict'])}"
                  + (f"；{'；'.join(st['reasons'][:2])}" if st["reasons"] else ""))
            if st.get("evaluation_ref"):
                print(f"      判定记录：{st['evaluation_ref']}；输入：{', '.join(st.get('inputs', [])[:4])}")
        if v.get("test_report"):
            tr = v["test_report"]
            print(f"  测试结果（构建 {tr.get('build')}）：" + "，".join(f"{k}={n}" for k, n in tr["counts"].items() if n))
        for d in v["defects"]:
            print(f"  缺陷 {d['id']}（{d['tc_ref']}，{d['severity']}）：{d['status']}；平台对象 {d.get('external_id') or '-'}；"
                  f"附件回读{'已核对' if d.get('attachments_verified') else '未核对'}")


def _act_footer(v: dict[str, Any]) -> str:
    if v["lab"]:
        return "本次：只操作本机实验资源与实验平台桩；未写入真实平台，未发布系统。"
    return "本次：未写入真实平台，未发布系统。"


def _print_notes(notes: list[str]) -> None:
    for n in notes:
        print(f"· {n}")
    if notes:
        print()


def _render_error(err: QAError, as_json: bool) -> int:
    d = err.to_dict()
    d["message"] = scrub_for_display(d["message"])
    if as_json:
        status = "unknown" if err.code == "OUTCOME_UNKNOWN" else "error"
        print(json.dumps({"command_id": None, "status": status, "committed": False, "revision": None, "result": None,
                          "errors": [_scrub(d)], "warnings": []}, ensure_ascii=False, indent=2))
    else:
        print(f"未完成：{d['message']}（代码 {err.code}）", file=sys.stderr)
        cands = (err.details or {}).get("candidates")
        if cands:
            print("可选项：", file=sys.stderr)
            for c in cands:
                if isinstance(c, dict):
                    print(f"  - {c.get('title')}（来源：{c.get('source')}；环境：{c.get('environment')}）", file=sys.stderr)
                else:
                    print(f"  - {c}", file=sys.stderr)
        items = (err.details or {}).get("items")
        if items:
            for c in items:
                print(f"  [{c['handle']}] {c['business_question']}", file=sys.stderr)
                for b in c.get("basis") or []:
                    if b.get("quote"):
                        loc = f"（{b['location']}）" if b.get("location") else ""
                        print(f"     依据：{scrub_for_display(b['quote'])}{loc}", file=sys.stderr)
                if c.get("affected_scope"):
                    print(f"     影响：{c['affected_scope']}", file=sys.stderr)
                for o in c["options"]:
                    print(f"     {o['key']}. {o['label']}", file=sys.stderr)
            print("  答复示例：review --answer " + f"{items[0]['handle']}={items[0]['options'][0]['key']}"
                  if items[0]["options"] else "", file=sys.stderr)
        others = (err.details or {}).get("others")
        if others:
            print("  需他人决定（你不能代签，可用 review --transfer 转交）：", file=sys.stderr)
            for c in others:
                print(f"  [{c['handle']}] {c['business_question']}（{c.get('role_label') or c.get('responsible_role')}）",
                      file=sys.stderr)
        issues = (err.details or {}).get("issues")
        if issues:
            for i in issues[:10]:
                print(f"  - {i.get('path')}：{scrub_for_display(i.get('message', ''))}", file=sys.stderr)
        print(f"建议：{err.recovery}", file=sys.stderr)
    return err.exit_code


def _scrub(obj: Any) -> Any:
    if isinstance(obj, str):
        return scrub_for_display(obj)
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


def _view_receipt(v: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    result = {"task": {k: v["task"][k] for k in ("task_id", "display_title", "sources", "environment", "project")},
              "lab": v["lab"], "verdict": v["verdict"], "summary": v["summary"],
              "stages": [{"stage": s["stage"], "title": s["title"], "status": s["status"], "verdict": s["verdict"],
                          "reasons": s["reasons"], "evaluation_ref": s.get("evaluation_ref")} for s in v["stages"]],
              "reviews": v["reviews"], "unknown_actions": v["unknown_actions"], "defects": [
                  {"id": d["id"], "tc": d["tc_ref"], "status": d["status"], "severity": d["severity"],
                   "external_id": d.get("external_id")} for d in v["defects"]],
              "unverified": v["unverified"], "last_verified_at": v["last_verified_at"],
              "worker_last_tick_at": v["worker_last_tick_at"]}
    if extra:
        result.update(extra)
    return _receipt(result, command_id=v["task"]["checkpoint"]["last_operation_ref"], revision=v["revision"])


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    try:
        parser = build_parser()
        if not argv:
            parser.print_help()
            return EXIT_OK
        args = parser.parse_args(argv)
        return _dispatch(args)
    except QAError as err:
        return _render_error(err, as_json)
    except sqlite3.DatabaseError as exc:
        return _render_error(_db_error(exc), as_json)
    except OSError as exc:
        return _render_error(QAError("IO_ERROR", f"文件读写失败：{exc.strerror or exc}",
                                     path=str(getattr(exc, "filename", "") or "")), as_json)
    except KeyboardInterrupt:
        print("已中断；已提交的工作会保留，使用 resume 继续。", file=sys.stderr)
        return 130


def _db_error(exc: sqlite3.DatabaseError) -> QAError:
    text = str(exc).lower()
    if isinstance(exc, sqlite3.OperationalError) and ("locked" in text or "busy" in text):
        return QAError("DEPENDENCY_UNAVAILABLE", "工作区数据库正被其他操作占用，稍后重试；本次未写入。")
    return QAError("WORKSPACE_INVALID", f"工作区数据库损坏或不可读（{exc}）；已停止推进，未写入任何内容。"
                   "请管理员用 backup verify 核对备份后恢复到新目录。")


def _dispatch(args: argparse.Namespace) -> int:
    from qa_flow.application import task_service as ts

    interactive = not args.non_interactive and sys.stdin.isatty()
    ts.allow_context_selection(not args.non_interactive)
    root = ts.find_root(getattr(args, "workspace", None))
    if args.cmd == "demo":
        res = ts.demo(root, interactive)
        v = ts.view(res["ctx"], res["task"])
        if args.json:
            print(json.dumps(_view_receipt(v, {"notes": res["notes"], "workspace": res["workspace"]}),
                             ensure_ascii=False, indent=2))
        else:
            print(f"合成实验目录：{res['workspace']}（保留到你明确清理为止）\n")
            _print_notes(res["notes"])
            _print_view(v, args.details, _act_footer(v))
        return EXIT_OK
    if args.cmd == "start":
        res = ts.start(root, args.source, project=args.project, title=args.title, interactive=interactive)
        sel = ts.select_task(root, res["task"]["task_id"], False)
        v = ts.view(sel["ctx"], sel["task"])
        if args.json:
            print(json.dumps(_view_receipt(v, {"notes": res.get("notes", []), "existing": res.get("existing", False)}),
                             ensure_ascii=False, indent=2))
        else:
            if res.get("existing"):
                print("该来源已有任务，继续使用原任务。\n")
            _print_notes(res.get("notes", []))
            _print_view(v, args.details, _act_footer(v))
        return EXIT_OK
    if args.cmd == "resume":
        res = ts.resume(root, args.task, interactive)
        v = ts.view(res["ctx"], ts.select_task(root, res["task"]["task_id"], False)["task"])
        if args.json:
            print(json.dumps(_view_receipt(v, {"notes": res["notes"], "source_changes": res["source_changes"]}),
                             ensure_ascii=False, indent=2))
        else:
            _print_notes(res["notes"])
            _print_view(v, args.details, _act_footer(v))
        return EXIT_OK
    if args.cmd == "review":
        res = ts.review_task(root, args.task, args.answer, args.transfer, args.note, interactive)
        v = res["view"]
        if args.json:
            print(json.dumps(_view_receipt(v, {"answers": res["results"]}), ensure_ascii=False, indent=2))
        else:
            for r in res["results"]:
                if r.get("reused"):
                    print(f"· {r['handle']} 已有有效答复，未重复提问。")
                elif "transferred_to" in r:
                    print(f"· {r['handle']}：{r.get('note')}")
                else:
                    print(f"· 已记录 {r['handle']} 的答复（{r.get('verification')}）。{r.get('note') or ''}")
            if not res["results"] and not v["reviews"]["mine"]:
                print("没有需要你确认的事项。")
            print()
            _print_view(v, args.details, _act_footer(v))
        return EXIT_OK
    if args.cmd == "status":
        res = ts.status(root, args.task, interactive)
        if args.json:
            print(json.dumps(_view_receipt(res["view"]), ensure_ascii=False, indent=2))
        else:
            _print_view(res["view"], args.details)
        return EXIT_OK
    if args.cmd == "export":
        res = ts.export(root, args.task, args.output, interactive)
        if args.json:
            print(json.dumps(_view_receipt(res["view"], {"files": res["files"], "note": res["note"]}),
                             ensure_ascii=False, indent=2))
        else:
            print("已导出本地报告（可直接打开）：")
            for k, pth in res["files"].items():
                print(f"  {k}: {pth}")
            print(f"结论：{res['view']['summary']['headline']}")
            print("未验证范围：" + "；".join(res["view"]["unverified"]))
            print(res["note"])
        return EXIT_OK
    if args.cmd == "admin":
        return _admin(args, root)
    if args.cmd == "api":
        from qa_flow import api as api_mod

        out = api_mod.run(args, root)
        # 机器回执同样按展示规则脱敏：适配器错误体、校验输出中的密钥不随读取接口原样输出
        print(json.dumps(_scrub(out), ensure_ascii=False, indent=2, default=str))
        return EXIT_OK
    raise QAError("INVALID_ARGUMENT", "未知命令")


def _admin(args: argparse.Namespace, root: Path) -> int:
    from qa_flow.application import admin
    from qa_flow.application.context import Ctx
    from qa_flow.application import task_service as ts

    def ctx_or_none() -> Ctx | None:
        wss = ts.workspaces(root)
        return Ctx(wss[0]) if len(wss) == 1 else None

    cmd = args.admin_cmd
    if cmd == "setup":
        out = admin.setup(root, args.name, Path(args.config), args.apply)
        code = EXIT_OK if not out["problems"] else 6
    elif cmd == "doctor":
        out = admin.doctor(ctx_or_none(), root)
        code = EXIT_OK
    elif cmd == "legacy-freeze":
        ts.guard_output(root, args.output)
        out = admin.legacy_freeze(Path(args.legacy_root), Path(args.output))
        code = EXIT_OK
    elif cmd in ("parity-record", "parity-check", "expire-evidence"):
        ctx = ctx_or_none()
        if ctx is None:
            raise QAError("SELECTION_REQUIRED", "需要用 --workspace 指定单个项目工作区")
        if cmd == "parity-record":
            out = admin.parity_record(ctx, Path(args.samples), Path(args.output))
        elif cmd == "parity-check":
            out = admin.parity_check(ctx, Path(args.samples), Path(args.baseline))
        else:
            out = admin.expire_evidence(ctx, args.id, args.reason)
        code = EXIT_OK
    else:
        print("管理命令：setup、doctor、legacy-freeze、parity-record、parity-check、expire-evidence")
        return EXIT_OK
    if args.json or cmd != "doctor":
        print(json.dumps(_scrub(out), ensure_ascii=False, indent=2, default=str))
    else:
        print(f"qa-flow {out['qa_flow']}；Python {out['python']}；SQLite {out['sqlite']}；{out['platform']}")
        ws = out.get("workspace")
        if ws:
            print(f"工作区：{ws['root']}（{ws['profile']}，{ws['project']}）；journal_mode={ws['journal_mode']}，"
                  f"synchronous={ws['synchronous']}，完整性={ws['integrity']}")
        for c in out["checks"]:
            print(f"  - {c['item']}：{c['status']}；{c['detail']}")
        iso = out["executor"]["isolation"]
        print("执行器隔离：" + "，".join(f"{k}={'有' if v else '无'}" for k, v in iso.items()
                                       if isinstance(v, bool)) + (f"（{iso.get('note')}）" if iso.get("note") else ""))
        print("边界：")
        for b in out["boundaries"]:
            print(f"  - {b}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
