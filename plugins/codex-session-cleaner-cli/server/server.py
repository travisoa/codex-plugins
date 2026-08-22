#!/usr/bin/env python3
"""Command-line edition of the Codex session cleaner.

Built for hosts that show elicitation forms but cannot render the MCP Apps
manager page, which is what Codex CLI does. It serves no UI component, so a
desktop client that renders the manager page has no reason to install it — and
the desktop edition in turn exposes no interactive tool the model could reach
for by mistake. That separation is the point of shipping two plugins.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import core
import interactive


SERVER_NAME = "codex-session-cleaner-cli"


def _desktop_edition_hint() -> str:
    if core.host_supports_elicitation():
        return (
            "【下一步】若用户想归档或删除，请立即调用 select_sessions，"
            "由用户在交互表单中筛选、选择并确认操作；"
            "不要让用户手动报会话编号，也不要在此停下等待进一步指示。\n\n"
        )
    return (
        "【提示】当前客户端既不能渲染管理页组件，也不支持交互表单。"
        "需要勾选式批量操作时，请告知用户在终端运行插件目录下的 scripts/launch_tui.sh。\n\n"
    )


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "select_sessions",
            "title": "交互式选择并处理 Codex 会话",
            "description": (
                "命令行下管理会话的主入口：引导用户依次完成筛选、选择会话、"
                "选定归档或永久删除，操作由用户在表单中直接确认。"
            ),
            "inputSchema": {"type": "object", "properties": {
                "managerContext": {"type": "string", "description": "内部上下文令牌。"},
            }},
            "annotations": {"readOnlyHint": False, "destructiveHint": True},
        },
        {
            "name": "list_sessions",
            "title": "列出 Codex 会话",
            "description": "只读查看：按活动/归档状态、搜索词、类别标签和最后更新时间列出会话。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": ["active", "archived", "all"], "default": "all"},
                    "search": {"type": "string", "default": ""},
                    "datePreset": {
                        "type": "string",
                        "enum": [
                            "all", "within_1_day", "within_1_week", "within_1_month",
                            "older_than_3_months", "older_than_1_month", "older_than_1_week", "custom",
                        ],
                        "default": "all",
                    },
                    "tag": {"type": "string", "default": ""},
                    "customStart": {"type": "string", "default": ""},
                    "customEnd": {"type": "string", "default": ""},
                },
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        },
        {
            "name": "inspect_session_files",
            "title": "查看会话文件线索",
            "description": "读取会话记录并提取修改文件和命令引用文件。",
            "inputSchema": {
                "type": "object", "properties": {"threadId": {"type": "string"}}, "required": ["threadId"]
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        },
        {
            "name": "archive_sessions",
            "title": "归档 Codex 会话",
            "description": "批量归档指定的顶层会话；不会归档当前会话。通常应改用 select_sessions 让用户自己勾选。",
            "inputSchema": {
                "type": "object",
                "properties": {"threadIds": {"type": "array", "items": {"type": "string"}}},
                "required": ["threadIds"],
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        },
        {
            "name": "delete_sessions",
            "title": "永久删除 Codex 会话",
            "description": (
                "永久删除指定会话及其派生会话；不会删除项目文件，且拒绝删除当前会话。"
                "通常应改用 select_sessions，由用户在表单中敲定名单。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "threadIds": {"type": "array", "items": {"type": "string"}},
                    "confirmation": {"type": "string", "enum": ["删除", "delete"]},
                },
                "required": ["threadIds", "confirmation"],
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": True},
        },
    ]


def call_tool(name: str, arguments: dict[str, Any], meta: Any) -> dict[str, Any]:
    core.call_with_locale(meta)
    if name == "select_sessions":
        current_id, _ = core.current_id_for_call(meta, arguments, require=True)
        if not core.host_supports_elicitation():
            raise ValueError(
                "当前客户端不支持交互表单。请改用 list_sessions 查看会话，"
                "或在终端运行插件目录下的 scripts/launch_tui.sh。"
            )
        data = core.list_sessions(current_id, "all", "")
        outcome = interactive.pick(current_id, data)
        payload = outcome["data"]
        payload["interactive"] = outcome["outcome"]
        return core.text_result(payload, interactive.summary(payload, outcome["outcome"]))
    if name == "list_sessions":
        current_id, _ = core.current_id_for_call(meta, arguments)
        scope = str(arguments.get("scope") or "all")
        if scope not in ("active", "archived", "all"):
            raise ValueError("scope 必须是 active、archived 或 all。")
        data = core.list_sessions(
            current_id,
            scope,
            str(arguments.get("search") or ""),
            str(arguments.get("datePreset") or "all"),
            str(arguments.get("customStart") or ""),
            str(arguments.get("customEnd") or ""),
            str(arguments.get("tag") or ""),
        )
        return core.text_result(data, core.sessions_text(data, hint=_desktop_edition_hint()))
    if name == "inspect_session_files":
        thread_id = str(arguments.get("threadId") or "").strip()
        if not thread_id:
            raise ValueError("threadId 不能为空。")
        data = core.inspect_files(thread_id)
        return core.text_result(data, core.files_text(data))
    if name == "archive_sessions":
        current_id, _ = core.current_id_for_call(meta, arguments, require=True)
        data = core.archive_sessions(core.validate_ids(arguments.get("threadIds")), current_id)
        summary = core.operation_text(data, "归档")
        if not data["sidebarSync"]["ok"]:
            summary += "\n侧边栏同步未完全成功；请刷新或重启 Codex。"
        return core.text_result(data, summary)
    if name == "delete_sessions":
        current_id, _ = core.current_id_for_call(meta, arguments, require=True)
        data = core.delete_sessions(
            core.validate_ids(arguments.get("threadIds")),
            str(arguments.get("confirmation") or ""),
            current_id,
        )
        summary = core.operation_text(data, "永久删除")
        if not data["sidebarSync"]["ok"]:
            summary += "\n侧边栏同步未完全成功；请刷新或重启 Codex。"
        return core.text_result(data, summary)
    raise ValueError(f"未知工具：{name}")


handle = core.build_handler(SERVER_NAME, _tool_definitions, call_tool)


def main() -> None:
    core.serve(SERVER_NAME, _tool_definitions, call_tool)


if __name__ == "__main__":
    main()
