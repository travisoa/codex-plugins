#!/usr/bin/env python3
"""Interactive session picker for the command-line edition.

Codex CLI cannot render the MCP Apps manager page but can show elicitation
forms, so this walks the user through filter -> pick -> act. Each form carries
exactly one field: the CLI asks every field of a form in turn and submits them
together, so a two-field form would ask for numbers again right after the user
chose 完成选择, and the client does not preserve field order either.

Codex's ElicitationSchema only accepts string, number, integer and boolean, so
there is no native list-multi-select to use; the pick step prints a numbered
list and takes one text field instead.
"""

from __future__ import annotations

import os
import re
from typing import Any

import core


TAG_LABELS_EN = {
    "hidden-fork": "Hidden fork",
    "session-management": "Session management",
    "automation": "Automation",
    "plugin-development": "Plugin development",
    "lark": "Lark collaboration",
    "documents": "Documents & sheets",
    "media": "Images & video",
    "development": "Code development",
    "general": "General",
}

ELICIT_TEXT = {
    "zh": {
        "filterMessage": lambda total: f"共 {total} 个可管理会话。请先选择筛选条件，随后从结果中选择要处理的会话。",
        "scope": "会话范围",
        "scopeNames": ["全部", "未归档", "已归档"],
        "date": "最后更新时间",
        "dateNames": ["不限", "1 天内", "1 周内", "1 个月内", "1 周前（更早）", "1 个月前（更早）", "3 个月前（更早）"],
        "tag": "类别标签",
        "anyTag": "不限",
        "tagCount": lambda label, count: f"{label}（{count}）",
        "matched": lambda total: f"筛选到 {total} 个会话",
        "colon": "：",
        "pageInfo": lambda page, pages, first, last: f"（第 {page}/{pages} 页，显示第 {first}-{last} 个）",
        "chosenSummary": lambda count, numbers: f"已选 {count} 个：{numbers}",
        "pickHintPaged": "输入序号可累加选择（如 1,3,5-7；all 选全部，clear 清空）；留空直接回车即可，下一步再决定翻页或提交。",
        "pickHintSingle": "请输入要处理的序号（此步只做选择，不会归档或删除）。",
        "pickTitle": lambda total: f"要处理的序号（1-{total}）",
        "pickDescription": "多个用逗号分隔，可用区间；留空表示不新增选择。",
        "pageField": "翻页 / 提交",
        "pageMessage": lambda count, page, pages: (
            f"当前第 {page}/{pages} 页，已选 {count} 个会话。"
            "选择“完成选择”提交，或继续翻页浏览。"
        ),
        "pageNames": ["完成选择", "下一页", "上一页"],
        "lastPage": "已经是最后一页。",
        "firstPage": "已经是第一页。",
        "protectedRows": lambda numbers: f"第 {numbers} 项是当前会话或受保护会话，不能操作，请重新输入。",
        "retry": lambda reason: f"{reason}请重新输入。",
        "unnamed": "未命名会话",
        "unknownPath": "未知项目路径",
        "hiddenFork": "隐藏分叉",
        "archived": "已归档",
        "ephemeral": "临时",
        "descendants": lambda count: f"连带 {count} 个派生会话",
        "blockedBy": lambda count: f"被 {count} 个分叉引用",
        "currentRow": "（当前会话，受保护）",
        "ephemeralRow": "（临时会话，不可操作）",
        "protectedRow": "（不可操作）",
        "actionMessage": lambda count: f"已选择 {count} 个会话，请选择要执行的操作。",
        "actionField": "操作",
        "actionNames": lambda count: [
            "取消，不做任何操作", f"归档这 {count} 个会话", f"永久删除这 {count} 个会话（不可撤销）",
        ],
        "outOfRange": lambda number, total: f"序号 {number} 超出范围 1-{total}。",
        "unparsable": lambda chunk: f"无法识别的序号“{chunk}”，请输入如 1,3,5-7 的形式。",
    },
    "en": {
        "filterMessage": lambda total: f"{total} sessions available. Choose filters first, then pick the ones to act on.",
        "scope": "Session scope",
        "scopeNames": ["All", "Not archived", "Archived"],
        "date": "Last updated",
        "dateNames": ["Any", "Within 1 day", "Within 1 week", "Within 1 month", "Over 1 week ago", "Over 1 month ago", "Over 3 months ago"],
        "tag": "Category tag",
        "anyTag": "Any",
        "tagCount": lambda label, count: f"{label} ({count})",
        "matched": lambda total: f"{total} sessions matched",
        "colon": ":",
        "pageInfo": lambda page, pages, first, last: f" (page {page}/{pages}, showing {first}-{last})",
        "chosenSummary": lambda count, numbers: f"{count} selected: {numbers}",
        "pickHintPaged": "Type numbers to add to the selection (e.g. 1,3,5-7; all selects everything, clear resets); leave empty and press enter — paging or submitting is the next step.",
        "pickHintSingle": "Type the numbers to act on (this step only selects; nothing is archived or deleted).",
        "pickTitle": lambda total: f"Numbers to act on (1-{total})",
        "pickDescription": "Comma-separated, ranges allowed; leave empty to add nothing.",
        "pageField": "Page / submit",
        "pageMessage": lambda count, page, pages: (
            f"Page {page}/{pages}, {count} session(s) selected. "
            "Choose Done to submit, or keep paging."
        ),
        "pageNames": ["Done", "Next page", "Previous page"],
        "lastPage": "Already on the last page.",
        "firstPage": "Already on the first page.",
        "protectedRows": lambda numbers: f"Item {numbers} is the current or a protected session and cannot be acted on. Please re-enter.",
        "retry": lambda reason: f"{reason} Please re-enter.",
        "unnamed": "Untitled session",
        "unknownPath": "Unknown project path",
        "hiddenFork": "hidden fork",
        "archived": "archived",
        "ephemeral": "temporary",
        "descendants": lambda count: f"takes {count} derived session(s) with it",
        "blockedBy": lambda count: f"referenced by {count} fork(s)",
        "currentRow": " (current session, protected)",
        "ephemeralRow": " (temporary session, not actionable)",
        "protectedRow": " (not actionable)",
        "actionMessage": lambda count: f"{count} session(s) selected. Choose what to do.",
        "actionField": "Action",
        "actionNames": lambda count: [
            "Cancel, do nothing", f"Archive these {count}", f"Permanently delete these {count} (cannot be undone)",
        ],
        "outOfRange": lambda number, total: f"Number {number} is outside 1-{total}.",
        "unparsable": lambda chunk: f"Cannot read \"{chunk}\"; use a form like 1,3,5-7.",
    },
}


def _elicit_locale() -> str:
    """宿主给了语言就用它，否则跟随进程环境，最后回落中文。"""
    value = core._HOST.get("locale")
    if value in ("zh", "en"):
        return str(value)
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        raw = os.environ.get(name)
        if raw:
            return "zh" if raw.lower().startswith("zh") else "en"
    return "zh"


def _t() -> dict[str, Any]:
    return ELICIT_TEXT[_elicit_locale()]


def _tag_label(key: str) -> str:
    if _elicit_locale() == "en":
        return TAG_LABELS_EN.get(key, core.TAG_LABELS.get(key, key))
    return core.TAG_LABELS.get(key, key)


ELICIT_TIMEOUT_SECONDS = 900
PICK_PAGE_SIZE = 10
PICK_MAX_ROUNDS = 500


def _elicit_target_labels(thread_id: str, item: dict[str, Any]) -> tuple[str, str]:
    """与管理页卡片保持同样的判断依据：影响删除范围的信息都要能看到。"""
    copy = _t()
    title = str(item.get("title") or copy["unnamed"])
    # 删除确认要能分辨同名项目，所以用完整路径而不是目录名。
    parts = [str(item.get("cwd") or copy["unknownPath"]), core.format_timestamp(item.get("updatedAt"))]
    if item.get("hiddenFromList"):
        parts.append(copy["hiddenFork"])
    if item.get("archived"):
        parts.append(copy["archived"])
    if item.get("ephemeral"):
        parts.append(copy["ephemeral"])
    if item.get("descendantCount"):
        parts.append(copy["descendants"](item["descendantCount"]))
    if item.get("blockingForkCount"):
        parts.append(copy["blockedBy"](item["blockingForkCount"]))
    parts.append(thread_id[:8])
    return title, " · ".join(parts)


DATE_PRESET_LABELS = {
    "all": "不限",
    "within_1_day": "1 天内",
    "within_1_week": "1 周内",
    "within_1_month": "1 个月内",
    "older_than_1_week": "1 周前（更早）",
    "older_than_1_month": "1 个月前（更早）",
    "older_than_3_months": "3 个月前（更早）",
}


def _elicit_filter(
    available_tags: list[dict[str, Any]], total: int
) -> tuple[dict[str, str] | None, str | None]:
    """第一段：先把上百个会话收窄到能逐条勾选的规模。"""
    tag_keys = [""] + [str(tag.get("key")) for tag in available_tags]
    copy = _t()
    tag_names = [copy["anyTag"]] + [
        copy["tagCount"](_tag_label(str(tag.get("key"))), tag.get("count")) for tag in available_tags
    ]
    params = {
        "message": copy["filterMessage"](total),
        "requestedSchema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "title": copy["scope"],
                    "enum": ["all", "active", "archived"],
                    "enumNames": copy["scopeNames"],
                    "default": "all",
                },
                "datePreset": {
                    "type": "string",
                    "title": copy["date"],
                    "enum": list(DATE_PRESET_LABELS),
                    "enumNames": copy["dateNames"],
                    "default": "all",
                },
                "tag": {
                    "type": "string",
                    "title": copy["tag"],
                    "enum": tag_keys,
                    "enumNames": tag_names,
                    "default": "",
                },
            },
        },
    }
    try:
        result = core.HOST.request("elicitation/create", params, timeout=ELICIT_TIMEOUT_SECONDS)
    except Exception as exc:
        return None, str(exc)
    result = result if isinstance(result, dict) else {}
    if str(result.get("action") or "") != "accept":
        return None, None
    content = result.get("content")
    content = content if isinstance(content, dict) else {}
    scope = str(content.get("scope") or "all")
    date_preset = str(content.get("datePreset") or "all")
    tag = str(content.get("tag") or "")
    return {
        "scope": scope if scope in ("all", "active", "archived") else "all",
        "datePreset": date_preset if date_preset in DATE_PRESET_LABELS else "all",
        "tag": tag,
    }, None


def _parse_selection(raw: Any, sessions: list[dict[str, Any]]) -> list[str]:
    """把 '1,3,5-7' / 'all' 这类输入解析成会话 ID，序号非法时报错而不是猜。"""
    written = str(raw or "").strip()
    if not written:
        return []
    if written.lower() in SELECT_ALL_WORDS:
        return [item["id"] for item in sessions]
    picked: list[str] = []
    for chunk in re.split(r"[,，、;；\s]+", written):
        if not chunk:
            continue
        span = re.fullmatch(r"(\d+)\s*[-~—]\s*(\d+)", chunk)
        if span:
            first, last = int(span.group(1)), int(span.group(2))
            numbers = range(min(first, last), max(first, last) + 1)
        elif chunk.isdigit():
            numbers = range(int(chunk), int(chunk) + 1)
        else:
            raise ValueError(_t()["unparsable"](chunk))
        for number in numbers:
            if not 1 <= number <= len(sessions):
                raise ValueError(_t()["outOfRange"](number, len(sessions)))
            thread_id = sessions[number - 1]["id"]
            if thread_id not in picked:
                picked.append(thread_id)
    return picked


CLEAR_WORDS = {"clear", "reset", "清空", "重选"}
SELECT_ALL_WORDS = {"all", "全部", "*"}


def _pick_row(index: int, item: dict[str, Any], chosen: bool) -> str:
    title, detail = _elicit_target_labels(item["id"], item)
    copy = _t()
    if item.get("current"):
        mark, suffix = "⊘", copy["currentRow"]
    elif item.get("ephemeral"):
        mark, suffix = "⊘", copy["ephemeralRow"]
    elif not item.get("deletable"):
        mark, suffix = "⊘", copy["protectedRow"]
    else:
        mark, suffix = ("✓" if chosen else "·"), ""
    return f"{index}. {mark} {title}{suffix} — {detail}"


def _pick_message(
    sessions: list[dict[str, Any]],
    page: int,
    pages: int,
    selected: list[str],
    warning: str = "",
) -> str:
    first = page * PICK_PAGE_SIZE
    window = sessions[first : first + PICK_PAGE_SIZE]
    chosen = set(selected)
    lines = []
    if warning:
        lines.extend([f"⚠ {warning}", ""])
    copy = _t()
    header = copy["matched"](len(sessions))
    if pages > 1:
        header += copy["pageInfo"](page + 1, pages, first + 1, first + len(window))
    lines.append(header + copy["colon"])
    for offset, item in enumerate(window, start=first + 1):
        lines.append(_pick_row(offset, item, item["id"] in chosen))
    lines.append("")
    if selected:
        numbers = [
            str(index)
            for index, item in enumerate(sessions, start=1)
            if item["id"] in chosen
        ]
        lines.append(copy["chosenSummary"](len(selected), ", ".join(numbers)))
    lines.append(copy["pickHintPaged"] if pages > 1 else copy["pickHintSingle"])
    return "\n".join(lines)


def _elicit_pick(sessions: list[dict[str, Any]]) -> tuple[list[str] | None, str | None]:
    """分页列出候选，序号跨页累加。

    每张表单只放一个字段：Codex CLI 会把一张表单的所有字段依次问一遍再统一提交，
    把“翻页/提交”和“序号”放在一起，用户选完“完成选择”还会被再问一次序号，
    而且字段先后顺序由客户端决定，读起来是反的。
    """
    pages = max(1, -(-len(sessions) // PICK_PAGE_SIZE))
    page = 0
    warning = ""
    selected: list[str] = []
    for _ in range(PICK_MAX_ROUNDS):
        copy = _t()
        entry = {
            "message": _pick_message(sessions, page, pages, selected, warning),
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "selection": {
                        "type": "string",
                        "title": copy["pickTitle"](len(sessions)),
                        "description": copy["pickDescription"],
                    }
                },
            },
        }
        try:
            result = core.HOST.request("elicitation/create", entry, timeout=ELICIT_TIMEOUT_SECONDS)
        except Exception as exc:
            return None, str(exc)
        result = result if isinstance(result, dict) else {}
        if str(result.get("action") or "") != "accept":
            return None, None
        content = result.get("content")
        content = content if isinstance(content, dict) else {}

        warning = ""
        written = str(content.get("selection") or "").strip()
        if written.lower() in CLEAR_WORDS:
            selected = []
        elif written:
            try:
                picked = _parse_selection(written, sessions)
            except ValueError as exc:
                warning = copy["retry"](str(exc))
                continue
            by_id = {item["id"]: item for item in sessions}
            if written.lower() in SELECT_ALL_WORDS:
                picked = [t for t in picked if by_id[t].get("deletable")]
            blocked = [
                index
                for index, item in enumerate(sessions, start=1)
                if item["id"] in picked and not item.get("deletable")
            ]
            if blocked:
                warning = copy["protectedRows"]("、".join(str(n) for n in blocked[:3]))
                continue
            for thread_id in picked:
                if thread_id not in selected and thread_id in by_id:
                    selected.append(thread_id)

        if pages == 1:
            return selected, None  # 只有一页时没什么可翻，直接提交

        move, failure = _elicit_page_move(selected, page, pages)
        if failure:
            return None, failure
        if move == "done":
            return selected, None
        if move == "next":
            warning = copy["lastPage"] if page >= pages - 1 else ""
            page = min(page + 1, pages - 1)
        else:
            warning = copy["firstPage"] if page == 0 else ""
            page = max(page - 1, 0)
    return None, "多次输入未能确定选择，请重新运行 select_sessions。"


def _elicit_page_move(
    selected: list[str], page: int, pages: int
) -> tuple[str, str | None]:
    """单独一张表单只问“翻页还是提交”，默认停在“完成选择”。"""
    copy = _t()
    params = {
        "message": copy["pageMessage"](len(selected), page + 1, pages),
        "requestedSchema": {
            "type": "object",
            "properties": {
                "page": {
                    "type": "string",
                    "title": copy["pageField"],
                    "enum": ["done", "next", "prev"],
                    "enumNames": copy["pageNames"],
                    "default": "done",
                }
            },
            "required": ["page"],
        },
    }
    try:
        result = core.HOST.request("elicitation/create", params, timeout=ELICIT_TIMEOUT_SECONDS)
    except Exception as exc:
        return "done", str(exc)
    result = result if isinstance(result, dict) else {}
    if str(result.get("action") or "") != "accept":
        return "done", None
    content = result.get("content")
    content = content if isinstance(content, dict) else {}
    move = str(content.get("page") or "done")
    return (move if move in ("done", "next", "prev") else "done"), None


def _elicit_action(count: int) -> tuple[str, str | None]:
    """第三段：让用户直接选操作，默认取消。"""
    copy = _t()
    params = {
        "message": copy["actionMessage"](count),
        "requestedSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "title": copy["actionField"],
                    "enum": ["cancel", "archive", "delete"],
                    "enumNames": copy["actionNames"](count),
                    "default": "cancel",
                }
            },
            "required": ["action"],
        },
    }
    try:
        result = core.HOST.request("elicitation/create", params, timeout=ELICIT_TIMEOUT_SECONDS)
    except Exception as exc:
        return "cancel", str(exc)
    result = result if isinstance(result, dict) else {}
    if str(result.get("action") or "") != "accept":
        return "cancel", None
    content = result.get("content")
    content = content if isinstance(content, dict) else {}
    choice = str(content.get("action") or "cancel")
    return (choice if choice in ("cancel", "archive", "delete") else "cancel"), None


def _run_picked_action(
    action: str, picked: list[str], current_id: str | None, outcome: dict[str, Any]
) -> None:
    if action == "cancel":
        outcome["performed"] = "none"
        outcome["note"] = f"已选择 {len(picked)} 个会话，但你选择了取消，未做任何改动。"
        return
    if len(picked) > core.BATCH_LIMIT:
        outcome["performed"] = "none"
        outcome["note"] = (
            f"一次最多处理 {core.BATCH_LIMIT} 个会话，本次选中 {len(picked)} 个；"
            "请缩小筛选范围后分批处理。"
        )
        return
    try:
        if action == "archive":
            result = core.archive_sessions(picked, current_id)
        else:
            # 会话与操作都已由用户在表单中敲定，这里不再重复确认。
            result = core.delete_sessions(picked, "删除", current_id)
    except Exception as exc:
        outcome["performed"] = "failed"
        outcome["note"] = f"执行失败：{exc}"
        return
    outcome["performed"] = action
    outcome["results"] = result.get("results") or []
    outcome["sidebarSync"] = result.get("sidebarSync")
    failed = [row for row in outcome["results"] if not row.get("ok")]
    label = "归档" if action == "archive" else "永久删除"
    outcome["note"] = (
        f"{label}完成：成功 {len(outcome['results']) - len(failed)} 个，失败 {len(failed)} 个。"
    )


def pick(current_id: str | None, data: dict[str, Any]) -> dict[str, Any]:
    """筛选 → 选择 → 操作三步交互。"""
    sessions = data.get("sessions") or []
    chosen, failure = _elicit_filter(data.get("availableTags") or [], len(sessions))
    if failure:
        return {"data": data, "outcome": {"note": f"未能显示筛选表单（{failure}），未做任何改动。"}}
    if chosen is None:
        return {"data": data, "outcome": {"note": "你已取消，未做任何改动。"}}

    if (chosen["scope"], chosen["datePreset"], chosen["tag"]) == ("all", "all", ""):
        filtered = data  # 条件没变就不必重新拉一遍全量列表
    else:
        filtered = core.list_sessions(
            current_id, chosen["scope"], "", chosen["datePreset"], "", "", chosen["tag"]
        )
    outcome: dict[str, Any] = {"filter": chosen, "matched": filtered["total"]}
    # 列出全部结果，当前会话也显示出来（标注受保护），避免用户以为它被漏掉了。
    candidates = list(filtered["sessions"])
    if not candidates:
        outcome["note"] = "该筛选条件下没有会话，请换个条件重试。"
        return {"data": filtered, "outcome": outcome}
    if not any(item.get("deletable") for item in candidates):
        outcome["note"] = "该筛选条件下的会话都受保护，无法归档或删除。"
        return {"data": filtered, "outcome": outcome}

    picked, failure = _elicit_pick(candidates)
    if failure:
        outcome["note"] = f"未能完成选择（{failure}），未做任何改动。"
    elif picked is None:
        outcome["note"] = "你已取消选择，未做任何改动。"
    elif not picked:
        outcome["note"] = "没有勾选任何会话，未做任何改动。"
    else:
        outcome["selectedThreadIds"] = picked
        action, failure = _elicit_action(len(picked))
        if failure:
            outcome["performed"] = "none"
            outcome["note"] = f"未能显示操作表单（{failure}），已选中的会话未做任何改动。"
        else:
            _run_picked_action(action, picked, current_id, outcome)
    return {"data": filtered, "outcome": outcome}


def summary(data: dict[str, Any], outcome: dict[str, Any]) -> str:
    chosen = outcome.get("filter") or {}
    lines = [
        "已在交互界面完成筛选。",
        f"筛选条件：范围 {chosen.get('scope')} · 时间 {DATE_PRESET_LABELS.get(chosen.get('datePreset'), '不限')}"
        + (f" · 标签 {chosen.get('tag')}" if chosen.get("tag") else ""),
        f"匹配 {outcome.get('matched', 0)} 个会话。",
        "",
    ]
    selected = outcome.get("selectedThreadIds") or []
    if selected:
        by_id = {item["id"]: item for item in data.get("sessions") or []}
        lines.append(f"用户选择了 {len(selected)} 个会话：")
        for index, thread_id in enumerate(selected, start=1):
            item = by_id.get(thread_id, {})
            lines.append(f"  {index}. {item.get('title') or thread_id}")
            lines.append(f"     ID {thread_id} · {item.get('cwd') or ''}")
    for row in outcome.get("results") or []:
        mark = "✓" if row.get("ok") else "✗"
        detail = "" if row.get("ok") else f"：{row.get('error') or '未知错误'}"
        lines.append(f"  {mark} {row.get('threadId')}{detail}")
    lines.append(outcome.get("note") or "")
    if outcome.get("performed") in ("archive", "delete", "none"):
        lines.append("以上操作已由用户在交互界面中直接确认并执行，不要再重复执行或扩大范围。")
    return "\n".join(line for line in lines if line is not None)
