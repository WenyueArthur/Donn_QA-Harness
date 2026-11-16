"""飞书来源包：登记由 Agent 读取的飞书项目工作项与挂载文档。

qa-flow 不直接连接飞书。Cursor、Codex、Claude Code 等 Agent 已配置飞书项目 MCP 与飞书文档工具，
为避免重复实现同一能力，由 Agent 读取并把工具的原始输出保存为文件，再交给 qa-flow 登记。

本模块只做确定性处理，不访问网络：

- 解析工作项原始 JSON（``get_workitem_brief`` 与 ``list_workitem_comments`` 的输出），
  渲染成稳定的 Markdown 来源文本，供引文逐字核对。
- 从字段值与评论中抽取挂载的飞书文档链接，并按所在字段推断用途（需求文档、技术方案等）。
- 校验来源包是否完整：发现的每个文档链接都必须已读取，或如实登记读取失败原因。

来源包格式（``qa.source_bundle``，文件路径相对来源包所在目录）::

    {
      "schema": "qa.source_bundle",
      "agent": "claude-code",
      "workitem": {
        "url": "https://project.feishu.cn/<空间>/story/detail/<工作项 ID>",
        "brief_file": "workitem.json",        # 或文件列表（字段分页时）
        "comments_file": "comments.json",    # 或文件列表（评论分页时）
        "fetched_at": "2026-11-16T06:10:00Z",
        "tool": "FeishuProjectMcp.get_workitem_brief"
      },
      "documents": [
        {"url": "https://xxx.feishu.cn/docx/<token>", "file": "prd.md",
         "fetched_at": "2026-11-16T06:11:00Z", "tool": "lark-cli docs +fetch"},
        {"url": "https://xxx.feishu.cn/wiki/<token>", "read_status": "unreadable",
         "failure_reason": "没有文档访问权限", "fetched_at": "2026-11-16T06:11:00Z"}
      ]
    }
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qa_flow import clock
from qa_flow.errors import QAError, ValidationIssues, issue

BUNDLE_SCHEMA = "qa.source_bundle"

# 飞书项目工作项链接：https://project.feishu.cn/<空间>/<类型>/detail/<ID>
_PROJECT_URL = re.compile(r"^https?://project\.(?:feishu\.cn|larksuite\.com)/([^/?#]+)/([^/?#]+)/detail/(\d+)",
                          re.IGNORECASE)
# 飞书云文档链接：docx / docs / wiki / sheets / base / file / mindnotes / slides
_DOC_URL = re.compile(r"https?://([A-Za-z0-9.-]+\.(?:feishu\.cn|larksuite\.com|larkoffice\.com))/"
                      r"(docx|docs|wiki|sheets|base|file|mindnotes|slides)/([A-Za-z0-9_-]+)")

ROLE_LABEL = {"workitem": "飞书项目工作项", "requirement": "需求文档", "tech_design": "技术方案",
              "test_doc": "测试文档", "other": "关联文档"}
ROLES = tuple(ROLE_LABEL)

# 文档类型中可按正文读取的类型；其余类型（表格、多维表格等）允许如实登记为读取受阻
TEXT_DOC_KINDS = ("docx", "docs", "wiki")


# ---------------------------------------------------------------------------
# 链接识别
# ---------------------------------------------------------------------------

def is_project_url(uri: str) -> bool:
    return bool(_PROJECT_URL.match(uri.strip()))


def is_doc_url(uri: str) -> bool:
    return bool(_DOC_URL.match(uri.strip()))


def is_feishu_url(uri: str) -> bool:
    return is_project_url(uri) or is_doc_url(uri)


def canonical_doc_url(url: str) -> str:
    """去掉查询参数与锚点，得到文档的稳定链接。"""
    m = _DOC_URL.match(url.strip())
    if not m:
        raise QAError("INVALID_ARGUMENT", f"不是可识别的飞书文档链接：{url}", path="documents.url")
    host, kind, token = m.groups()
    return f"https://{host.lower()}/{kind.lower()}/{token}"


def doc_key(url: str) -> str:
    """用于去重比对的文档身份：文档类型 + token（不区分租户域名写法）。"""
    m = _DOC_URL.match(url.strip())
    if not m:
        return url.strip()
    _, kind, token = m.groups()
    return f"{kind.lower()}/{token}"


def find_doc_links(text: str) -> list[str]:
    out: list[str] = []
    for m in _DOC_URL.finditer(text or ""):
        url = canonical_doc_url(m.group(0))
        if url not in out:
            out.append(url)
    return out


def role_for_field(name: str) -> str:
    if "技术" in name or "方案" in name or "设计" in name:
        return "tech_design"
    if "测试" in name or "用例" in name:
        return "test_doc"
    if "需求" in name or "产品" in name or "PRD" in name.upper() or name in ("wiki", "描述", "description"):
        return "requirement"
    return "other"


# ---------------------------------------------------------------------------
# 原始输出解析
# ---------------------------------------------------------------------------

def parse_tool_json(text: str, label: str) -> Any:
    """解析工具原始输出。MCP 文本输出可能在 JSON 之后附带 ``log_id: ...`` 等行，只取第一个 JSON 值。"""
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValidationIssues("VALIDATION_FAILED", [issue(label, "文件中没有 JSON 内容；请保存工具的原始输出")])
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError as exc:
        raise ValidationIssues("VALIDATION_FAILED", [issue(label, f"JSON 无法解析：{exc}")]) from exc
    return value


def _render_value(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        parts = [_render_value(x) for x in v]
        return "、".join(p for p in parts if p)
    if isinstance(v, dict):
        if "start_time" in v and "end_time" in v:
            a, b = _render_value(v["start_time"]), _render_value(v["end_time"])
            return f"{a} 至 {b}" if a or b else ""
        for key in ("label", "name", "iso_time", "title", "value"):
            if v.get(key) not in (None, ""):
                return _render_value(v[key])
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return str(v)


def _ms_to_iso(v: Any) -> str:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return _render_value(v)
    if n > 1e11:  # 毫秒时间戳
        n /= 1000
    return clock.to_iso(datetime.fromtimestamp(n, tz=timezone.utc))


def _comment_parts(c: Any) -> tuple[str, str, str]:
    """评论的（作者、时间、正文）。字段名按常见写法逐个尝试，取不到时保留原始 JSON，不丢内容。"""
    if not isinstance(c, dict):
        return "", "", _render_value(c)
    author = ""
    for key in ("creator", "operator", "user", "author", "created_by"):
        if c.get(key):
            author = _render_value(c[key])
            break
    when = ""
    for key in ("created_at", "create_time", "created_time", "update_time"):
        if c.get(key) not in (None, ""):
            when = _ms_to_iso(c[key])
            break
    body = ""
    for key in ("content", "text", "comment", "body", "rich_text"):
        if c.get(key) not in (None, ""):
            body = c[key] if isinstance(c[key], str) else json.dumps(c[key], ensure_ascii=False)
            break
    if not body:
        body = json.dumps(c, ensure_ascii=False, sort_keys=True)
    return author, when, body


def render_workitem(briefs: list[dict[str, Any]], comment_pages: list[Any]) -> dict[str, Any]:
    """把工作项原始 JSON 渲染成 Markdown，并抽取挂载的文档链接。"""
    first = briefs[0]
    attr = first.get("work_item_attribute") or {}
    name = attr.get("work_item_name")
    wid = str(attr.get("work_item_id") or "")
    if not name or not wid:
        raise ValidationIssues("VALIDATION_FAILED", [issue("workitem.brief_file",
                                                           "不是 get_workitem_brief 的输出：缺少工作项名称或 ID")])
    proj = attr.get("owned_project") or {}
    wtype = attr.get("work_item_type") or {}
    fields: list[dict[str, Any]] = []
    seen = set()
    for b in briefs:
        for f in b.get("work_item_fields") or []:
            if f.get("key") in seen:
                continue
            seen.add(f.get("key"))
            fields.append(f)
    last_page = briefs[-1].get("pagination") or {}
    if last_page.get("has_more"):
        raise ValidationIssues("VALIDATION_FAILED", [issue("workitem.brief_file",
                                                           "字段还有下一页（has_more=true）；请按 page_token 继续读取并全部保存")])

    lines = [f"# {name}", ""]
    simple = proj.get("simple_name") or proj.get("key") or ""
    lines.append(f"- 来源：飞书项目工作项（空间 {proj.get('name') or simple}，ID {wid}）")
    if wtype.get("name"):
        lines.append(f"- 类型：{wtype['name']}")
    status = (attr.get("work_item_status") or {}).get("name")
    if status:
        lines.append(f"- 状态：{status}")
    if attr.get("create_time"):
        lines.append(f"- 创建：{_render_value(attr.get('create_by'))} {attr['create_time']}")
    if attr.get("update_time"):
        lines.append(f"- 更新：{_render_value(attr.get('updated_by'))} {attr['update_time']}")
    roles = [(r.get("name") or r.get("key"), "、".join(m.get("name", "") for m in r.get("members") or []))
             for r in attr.get("role_members") or []]
    roles = [(n, m) for n, m in roles if m]
    if roles:
        lines += ["", "## 角色", ""] + [f"- {n}：{m}" for n, m in roles]
    nodes = first.get("work_item_current_node") or []
    if nodes:
        lines += ["", "## 当前节点", ""]
        for n in nodes:
            owners = "、".join(o.get("name", "") for o in n.get("owners") or [])
            lines.append(f"- {n.get('name')}" + (f"：{owners}" if owners else ""))

    links: list[dict[str, Any]] = []

    def add_links(text: str, found_in: str, role: str) -> None:
        for url in find_doc_links(text):
            if all(doc_key(url) != doc_key(x["url"]) for x in links):
                links.append({"url": url, "found_in": found_in, "role": role})

    field_lines: list[str] = []
    for f in fields:
        text = _render_value(f.get("value"))
        if not text.strip():
            continue
        fname = f.get("name") or f.get("key")
        field_lines += [f"### {fname}", "", text, ""]
        add_links(text, f"字段「{fname}」", role_for_field(f"{fname} {f.get('key', '')}"))
    if field_lines:
        lines += ["", "## 字段", ""] + field_lines[:-1]

    comments: list[Any] = []
    total_pages = 0
    for page in comment_pages:
        if isinstance(page, dict):
            comments += page.get("comments") or page.get("list") or []
            total_pages = max(total_pages, int((page.get("pagination") or {}).get("total_pages") or 0))
        elif isinstance(page, list):
            comments += page
    if total_pages > len(comment_pages):
        raise ValidationIssues("VALIDATION_FAILED", [issue(
            "workitem.comments_file", f"评论共 {total_pages} 页，只保存了 {len(comment_pages)} 页；请全部读取")])
    lines += ["", "## 评论", ""]
    if not comments:
        lines.append("（无评论）")
    for i, c in enumerate(comments, 1):
        author, when, body = _comment_parts(c)
        lines += [f"### 评论 {i}" + (f"：{author}" if author else "") + (f"（{when}）" if when else ""), "", body, ""]
        add_links(body, f"评论 {i}", "other")
    text = "\n".join(lines).rstrip() + "\n"
    canonical = f"https://project.feishu.cn/{simple}/{wtype.get('key') or 'story'}/detail/{wid}"
    return {"title": name, "work_item_id": wid, "url": canonical, "markdown": text, "links": links,
            "comment_count": len(comments)}


def _doc_text(raw: str) -> tuple[str, str | None]:
    """文档文件可以是正文 Markdown，也可以是 ``lark-cli docs +fetch`` 的原始 JSON 输出。"""
    stripped = raw.lstrip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except ValueError:
            return raw, None
        doc = ((obj.get("data") or {}).get("document") or {}) if isinstance(obj, dict) else {}
        if isinstance(doc.get("content"), str):
            return doc["content"], doc.get("title")
        if isinstance(obj, dict) and obj.get("ok") is False:
            raise ValidationIssues("VALIDATION_FAILED", [issue("documents.file",
                                                               "文件是读取失败的输出；请改用 read_status 登记失败原因")])
    return raw, None


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()[:80] or None
    return None


def _check_time(value: Any, path: str, problems: list[dict[str, Any]]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        problems.append(issue(path, "缺少读取时间（ISO-8601，带时区）"))
        return None
    try:
        ts = clock.parse_ts(value)
    except ValueError:
        problems.append(issue(path, f"读取时间格式无效：{value}"))
        return None
    if (ts - clock.now()).total_seconds() > 300:
        problems.append(issue(path, f"读取时间晚于当前时间：{value}"))
        return None
    return clock.to_iso(ts)


def _files(spec: Any) -> list[str]:
    if isinstance(spec, str) and spec.strip():
        return [spec]
    if isinstance(spec, list) and spec and all(isinstance(x, str) and x.strip() for x in spec):
        return spec
    return []


# ---------------------------------------------------------------------------
# 来源包加载与完整性校验
# ---------------------------------------------------------------------------

def load(path: Path, *, max_bytes: int, strict: bool = True) -> dict[str, Any]:
    """读取来源包。strict 时任何不完整都拒绝；否则把问题放进 problems 供预检查看。"""
    path = Path(path).expanduser().resolve()
    if path.is_dir():
        path = path / "bundle.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise QAError("NOT_FOUND", f"来源包不存在：{path}", path="bundle") from exc
    except ValueError as exc:
        raise ValidationIssues("VALIDATION_FAILED", [issue("bundle", f"来源包不是有效 JSON：{exc}")]) from exc
    base = path.parent
    problems: list[dict[str, Any]] = []
    if not isinstance(manifest, dict) or manifest.get("schema") != BUNDLE_SCHEMA:
        raise ValidationIssues("VALIDATION_FAILED", [issue("schema", f"来源包 schema 必须为 {BUNDLE_SCHEMA}")])
    agent = manifest.get("agent")
    if not isinstance(agent, str) or not agent.strip():
        problems.append(issue("agent", "写明读取内容的 Agent，如 claude-code、cursor、codex"))
        agent = "unknown"
    wi = manifest.get("workitem")
    if not isinstance(wi, dict):
        raise ValidationIssues("VALIDATION_FAILED", [issue("workitem", "缺少飞书项目工作项")])
    url = wi.get("url") or ""
    m = _PROJECT_URL.match(url.strip())
    if not m:
        raise ValidationIssues("VALIDATION_FAILED", [issue("workitem.url", f"不是飞书项目工作项链接：{url}")])

    def read(rel: str, label: str) -> str:
        p = (base / rel).resolve()
        try:
            data = p.read_bytes()
        except OSError as exc:
            raise ValidationIssues("VALIDATION_FAILED", [issue(label, f"文件无法读取：{p}（{exc.__class__.__name__}）")]) \
                from exc
        if len(data) > max_bytes:
            raise ValidationIssues("VALIDATION_FAILED", [issue(label, f"文件超过私有存储限额（{len(data)} 字节）")])
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationIssues("VALIDATION_FAILED", [issue(label, "文件不是 UTF-8 文本")]) from exc

    brief_files = _files(wi.get("brief_file"))
    comment_files = _files(wi.get("comments_file"))
    if not brief_files:
        raise ValidationIssues("VALIDATION_FAILED", [issue("workitem.brief_file",
                                                           "缺少工作项原始输出（get_workitem_brief，fields=[\"_all\"]）")])
    if not comment_files:
        problems.append(issue("workitem.comments_file",
                              "缺少评论原始输出（list_workitem_comments）；评论常含技术方案与测试文档链接，无评论也需保存空结果"))
    briefs = [parse_tool_json(read(f, "workitem.brief_file"), "workitem.brief_file") for f in brief_files]
    if not all(isinstance(b, dict) for b in briefs):
        raise ValidationIssues("VALIDATION_FAILED", [issue("workitem.brief_file", "工作项原始输出应为 JSON 对象")])
    pages = [parse_tool_json(read(f, "workitem.comments_file"), "workitem.comments_file") for f in comment_files]
    rendered = render_workitem(briefs, pages)
    if rendered["work_item_id"] != m.group(3):
        problems.append(issue("workitem.url", f"链接中的工作项 ID {m.group(3)} 与内容中的 ID {rendered['work_item_id']} 不一致"))
    wi_time = _check_time(wi.get("fetched_at"), "workitem.fetched_at", problems)
    root = {"uri": rendered["url"], "kind": "feishu_workitem", "role": "workitem", "found_in": "工作项本身",
            "title": rendered["title"], "data": rendered["markdown"].encode("utf-8"), "read_status": "available",
            "fetched_at": wi_time, "tool": str(wi.get("tool") or "unknown"), "agent": agent,
            "failure_reason": None}

    docs_in = manifest.get("documents") or []
    if not isinstance(docs_in, list):
        raise ValidationIssues("VALIDATION_FAILED", [issue("documents", "documents 应为列表")])
    discovered = {doc_key(x["url"]): x for x in rendered["links"]}
    items: list[dict[str, Any]] = []
    keys = set()
    for i, d in enumerate(docs_in):
        p = f"documents[{i}]"
        if not isinstance(d, dict) or not is_doc_url(str(d.get("url") or "")):
            problems.append(issue(f"{p}.url", "不是可识别的飞书文档链接"))
            continue
        canon = canonical_doc_url(d["url"])
        key = doc_key(canon)
        if key in keys:
            problems.append(issue(f"{p}.url", f"文档重复：{canon}"))
            continue
        keys.add(key)
        found = discovered.get(key)
        role = d.get("role") or (found or {}).get("role") or "other"
        if role not in ROLES or role == "workitem":
            problems.append(issue(f"{p}.role", f"用途只能是 {[r for r in ROLES if r != 'workitem']} 之一"))
            role = "other"
        fetched = _check_time(d.get("fetched_at"), f"{p}.fetched_at", problems)
        status = d.get("read_status") or ("available" if d.get("file") else None)
        item = {"uri": canon, "kind": "feishu_doc", "role": role,
                "found_in": (found or {}).get("found_in") or str(d.get("found_in") or "Agent 补充（正文引用或检索）"),
                "title": d.get("title"), "data": None, "read_status": status, "fetched_at": fetched,
                "tool": str(d.get("tool") or "unknown"), "agent": agent, "failure_reason": None}
        if status == "available":
            if not d.get("file"):
                problems.append(issue(f"{p}.file", "已读取的文档必须提供正文文件"))
                continue
            text, title = _doc_text(read(d["file"], f"{p}.file"))
            if not text.strip():
                problems.append(issue(f"{p}.file", "文档正文为空；读取失败时请用 read_status 登记"))
                continue
            item["data"] = text.encode("utf-8")
            item["title"] = item["title"] or title or _first_heading(text) or ROLE_LABEL[role]
        elif status in ("unreadable", "missing"):
            reason = d.get("failure_reason")
            if not isinstance(reason, str) or not reason.strip():
                problems.append(issue(f"{p}.failure_reason", "读取失败时必须写明原因"))
                reason = "未说明原因"
            item["failure_reason"] = reason
            item["title"] = item["title"] or key
        else:
            problems.append(issue(f"{p}.read_status", "read_status 只能是 available、unreadable 或 missing"))
            continue
        items.append(item)
    missing = [x for k, x in discovered.items() if k not in keys]
    for x in missing:
        problems.append(issue("documents", f"未读取工作项中挂载的文档：{x['url']}（{x['found_in']}）"))
    result = {"title": rendered["title"], "root": root, "items": [root, *items], "discovered": rendered["links"],
              "missing": missing, "problems": problems, "agent": agent, "comment_count": rendered["comment_count"],
              "bundle_path": str(path)}
    if strict and problems:
        raise ValidationIssues("VALIDATION_FAILED", problems)
    return result


def describe(bundle: dict[str, Any]) -> dict[str, Any]:
    """来源包预检结果（不含正文）。"""
    return {
        "title": bundle["title"], "agent": bundle["agent"], "complete": not bundle["problems"],
        "problems": bundle["problems"], "comment_count": bundle["comment_count"],
        "discovered_links": bundle["discovered"],
        "items": [{"uri": i["uri"], "role": ROLE_LABEL[i["role"]], "title": i["title"], "read_status": i["read_status"],
                   "found_in": i["found_in"], "bytes": len(i["data"]) if i["data"] else 0,
                   "failure_reason": i["failure_reason"]} for i in bundle["items"]],
    }


def display_name(item: dict[str, Any]) -> str:
    return f"{ROLE_LABEL.get(item['role'], '关联文档')}《{item['title']}》"
