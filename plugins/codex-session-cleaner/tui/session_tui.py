#!/usr/bin/env python3
"""Terminal UI for the Codex session cleaner.

Reuses server.py for every listing, archive and delete rule so the CLI and the
MCP Apps manager page cannot drift apart on safety behaviour. Standard library
only: rendering is plain curses, matching the plugin's zero-dependency promise.
"""

from __future__ import annotations

import argparse
import curses
import importlib.util
import os
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
SERVER_MODULE = "session_cleaner_server"
# CLI 会话不属于任何 Codex 线程，用固定上下文 ID 走完整的后端校验。
CLI_CONTEXT_ID = "codex-session-cleaner-cli"
CURRENT_THREAD_ENV = ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_CONVERSATION_ID")
SCOPES = ("all", "active", "archived")
DATE_PRESETS = (
    "all",
    "within_1_day",
    "within_1_week",
    "within_1_month",
    "older_than_1_week",
    "older_than_1_month",
    "older_than_3_months",
)


def load_server() -> Any:
    """Load server.py once, sharing the instance with tests that already loaded it."""
    existing = sys.modules.get(SERVER_MODULE)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(SERVER_MODULE, ROOT / "server" / "server.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 server.py。")
    module = importlib.util.module_from_spec(spec)
    sys.modules[SERVER_MODULE] = module
    spec.loader.exec_module(module)
    return module


TEXT = {
    "zh": {
        "title": "Codex 会话清理器",
        "cliContext": "CLI 模式 · 未绑定当前会话",
        "boundContext": lambda value: f"当前会话 {value[:8]}",
        "loading": "正在读取会话…",
        "empty": "没有符合条件的会话",
        "summary": lambda total, selected: f"{total} 个会话 · 已选 {selected}",
        "scope": "范围",
        "tag": "标签",
        "date": "日期",
        "search": "搜索",
        "none": "－",
        "scopes": {"all": "全部", "active": "当前", "archived": "已归档"},
        "dates": {
            "all": "不限",
            "within_1_day": "1 天内",
            "within_1_week": "1 周内",
            "within_1_month": "1 个月内",
            "older_than_1_week": "1 周前",
            "older_than_1_month": "1 个月前",
            "older_than_3_months": "3 个月前",
        },
        "keys": "空格 勾选   / 搜索   F 筛选   I 文件线索   A 归档   D 删除   R 刷新   Q 退出",
        "archived": "已归档",
        "ephemeral": "临时",
        "current": "当前",
        "protectedRow": "受保护",
        "blocked": lambda count: f"{count} 个引用分叉",
        "unknownProject": "未知项目",
        "unknownTime": "时间未知",
        "searchPrompt": "搜索（回车确认，Esc 取消）：",
        "filterTitle": "筛选",
        "filterHint": "←/→ 调整   ↑/↓ 切换项   回车 应用   Esc 取消",
        "filesTitle": lambda title: f"文件线索 · {title}",
        "filesHint": "↑/↓ 滚动   任意其他键返回",
        "filesLoading": "正在读取会话文件线索…",
        "filesNote": "文件线索来自会话记录，可能不完整。",
        "changedFiles": "修改文件",
        "referencedFiles": "命令引用文件",
        "noFiles": "未从会话记录中发现",
        "confirmWord": "删除",
        "deleteTitle": lambda count: f"永久删除 {count} 个会话？",
        "deleteBody": "会一并删除派生会话和持久化元数据，此操作不可撤销。项目文件不会被删除。",
        "deletePrompt": lambda word: f"输入 {word} 确认（Esc 取消）：",
        "noSelection": "尚未选择任何会话",
        "archiving": "正在归档…",
        "deleting": "正在删除…",
        "archived_ok": "所选会话已归档",
        "deleted_ok": "所选会话已永久删除，侧边栏已同步",
        "syncFailed": "会话已删除；侧边栏同步失败，请刷新或重启 Codex",
        "failed": lambda count, error: f"{count} 个会话失败：{error}",
        "unknownError": "未知错误",
        "loadFailed": lambda error: f"读取会话失败：{error}",
    },
    "en": {
        "title": "Codex Session Cleaner",
        "cliContext": "CLI mode · no current session bound",
        "boundContext": lambda value: f"Current session {value[:8]}",
        "loading": "Loading sessions…",
        "empty": "No matching sessions",
        "summary": lambda total, selected: f"{total} sessions · {selected} selected",
        "scope": "Scope",
        "tag": "Tag",
        "date": "Date",
        "search": "Search",
        "none": "-",
        "scopes": {"all": "All", "active": "Current", "archived": "Archived"},
        "dates": {
            "all": "Any",
            "within_1_day": "Within 1 day",
            "within_1_week": "Within 1 week",
            "within_1_month": "Within 1 month",
            "older_than_1_week": "Over 1 week",
            "older_than_1_month": "Over 1 month",
            "older_than_3_months": "Over 3 months",
        },
        "keys": "Space select   / search   F filter   I files   A archive   D delete   R refresh   Q quit",
        "archived": "Archived",
        "ephemeral": "Temp",
        "current": "Current",
        "protectedRow": "Protected",
        "blocked": lambda count: f"{count} forks",
        "unknownProject": "Unknown project",
        "unknownTime": "Unknown time",
        "searchPrompt": "Search (Enter to apply, Esc to cancel): ",
        "filterTitle": "Filters",
        "filterHint": "←/→ change   ↑/↓ move   Enter apply   Esc cancel",
        "filesTitle": lambda title: f"File clues · {title}",
        "filesHint": "↑/↓ scroll   any other key to return",
        "filesLoading": "Loading session file clues…",
        "filesNote": "File clues come from session records and may be incomplete.",
        "changedFiles": "Changed files",
        "referencedFiles": "Files referenced by commands",
        "noFiles": "None found in session records",
        "confirmWord": "delete",
        "deleteTitle": lambda count: f"Permanently delete {count} sessions?",
        "deleteBody": "Derived sessions and persisted metadata go too. This cannot be undone. Project files are kept.",
        "deletePrompt": lambda word: f"Type {word} to confirm (Esc to cancel): ",
        "noSelection": "Nothing selected yet",
        "archiving": "Archiving…",
        "deleting": "Deleting…",
        "archived_ok": "Selected sessions archived",
        "deleted_ok": "Selected sessions permanently deleted and sidebar synced",
        "syncFailed": "Sessions deleted, but sidebar sync failed. Refresh or restart Codex.",
        "failed": lambda count, error: f"{count} failed: {error}",
        "unknownError": "Unknown error",
        "loadFailed": lambda error: f"Failed to load sessions: {error}",
    },
}


def detect_locale(explicit: str | None = None) -> str:
    if explicit in ("zh", "en"):
        return explicit
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name)
        if value:
            return "zh" if value.lower().startswith("zh") else "en"
    return "zh"


def detect_current_thread(explicit: str | None = None) -> tuple[str, bool]:
    """Return the context ID plus whether it is a real Codex thread."""
    if explicit and explicit.strip():
        return explicit.strip(), True
    for name in CURRENT_THREAD_ENV:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip(), True
    return CLI_CONTEXT_ID, False


def char_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text: str) -> int:
    return sum(char_width(char) for char in text)


def truncate(text: str, limit: int) -> str:
    """Cut to a terminal column budget, counting CJK characters as two columns."""
    text = str(text or "").replace("\n", " ").replace("\t", " ")
    if limit <= 0:
        return ""
    if display_width(text) <= limit:
        return text
    kept: list[str] = []
    used = 0
    for char in text:
        width = char_width(char)
        if used + width > limit - 1:
            break
        kept.append(char)
        used += width
    return "".join(kept) + "…"


def pad(text: str, width: int) -> str:
    text = truncate(text, width)
    return text + " " * max(0, width - display_width(text))


def format_time(value: Any, copy: dict[str, Any]) -> str:
    # 走 load_server() 而不是翻 sys.modules：模块名改了也不会静默退化成“时间未知”。
    seconds = load_server()._timestamp_seconds(value)
    if seconds is None:
        return copy["unknownTime"]
    return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M")


class TuiState:
    """Interaction state kept free of curses so the rules stay testable."""

    def __init__(self, locale: str = "zh", current_id: str = CLI_CONTEXT_ID, bound: bool = False) -> None:
        self.locale = locale
        self.current_id = current_id
        self.bound = bound
        self.sessions: list[dict[str, Any]] = []
        self.available_tags: list[dict[str, Any]] = []
        self.selected: set[str] = set()
        self.cursor = 0
        self.top = 0
        self.scope = "all"
        self.search = ""
        self.tag = ""
        self.date_preset = "all"
        self.status = ""
        self.loading = False

    @property
    def copy(self) -> dict[str, Any]:
        return TEXT[self.locale]

    def apply(self, data: dict[str, Any]) -> None:
        self.sessions = list(data.get("sessions") or [])
        self.available_tags = list(data.get("availableTags") or [])
        known = {item.get("key") for item in self.available_tags}
        if self.tag and self.tag not in known:
            self.tag = ""
        self.selected = {item["id"] for item in self.sessions if item["id"] in self.selected}
        self.clamp()

    def clamp(self) -> None:
        self.cursor = max(0, min(self.cursor, len(self.sessions) - 1)) if self.sessions else 0

    def move(self, delta: int) -> None:
        if not self.sessions:
            self.cursor = 0
            return
        self.cursor = max(0, min(self.cursor + delta, len(self.sessions) - 1))

    def current_row(self) -> dict[str, Any] | None:
        if not self.sessions or not 0 <= self.cursor < len(self.sessions):
            return None
        return self.sessions[self.cursor]

    def toggle_current(self) -> bool:
        row = self.current_row()
        if row is None or not row.get("deletable"):
            return False
        if row["id"] in self.selected:
            self.selected.discard(row["id"])
        else:
            self.selected.add(row["id"])
        return True

    def targets(self) -> list[str]:
        """Only sessions in the current listing that are still deletable.

        Mirrors the manager page: a filter change must never leave a hidden
        selection that a later archive or delete would silently act on.
        """
        return [item["id"] for item in self.sessions if item["id"] in self.selected and item.get("deletable")]

    def reset_selection(self) -> None:
        self.selected.clear()

    def set_filter(self, **changes: Any) -> None:
        """Any filter change drops the selection, since the listing is about to change."""
        for key, value in changes.items():
            setattr(self, key, value)
        self.reset_selection()

    def cycle(self, field: str, options: tuple[str, ...], delta: int) -> None:
        values = list(options)
        current = getattr(self, field)
        index = values.index(current) if current in values else 0
        setattr(self, field, values[(index + delta) % len(values)])
        self.reset_selection()

    def tag_options(self) -> list[str]:
        return [""] + [str(item.get("key")) for item in self.available_tags]

    def summary(self) -> str:
        copy = self.copy
        return copy["summary"](len(self.sessions), len(self.targets()))

    def filter_line(self) -> str:
        copy = self.copy
        tag_label = copy["none"]
        for item in self.available_tags:
            if item.get("key") == self.tag:
                tag_label = str(item.get("label") or self.tag)
        parts = [
            f"{copy['scope']} {copy['scopes'][self.scope]}",
            f"{copy['tag']} {tag_label}",
            f"{copy['date']} {copy['dates'][self.date_preset]}",
        ]
        if self.search:
            parts.append(f"{copy['search']} {self.search}")
        return " · ".join(parts)


class SessionTui:
    def __init__(self, screen: Any, server: Any, state: TuiState) -> None:
        self.screen = screen
        self.server = server
        self.state = state
        self.colors: dict[str, int] = {}

    # ---- rendering helpers -------------------------------------------------

    def setup_colors(self) -> None:
        if not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            return
        pairs = (
            ("accent", curses.COLOR_CYAN),
            ("muted", curses.COLOR_BLUE),
            ("danger", curses.COLOR_RED),
            ("warn", curses.COLOR_YELLOW),
            ("ok", curses.COLOR_GREEN),
        )
        for index, (name, color) in enumerate(pairs, start=1):
            try:
                curses.init_pair(index, color, -1)
                self.colors[name] = curses.color_pair(index)
            except curses.error:
                self.colors[name] = 0

    def color(self, name: str) -> int:
        return self.colors.get(name, 0)

    def put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        height, width = self.screen.getmaxyx()
        if not 0 <= y < height or x >= width:
            return
        text = truncate(text, width - x - 1)
        if not text:
            return
        try:
            self.screen.addstr(y, x, text, attr)
        except curses.error:
            pass

    # ---- main screen -------------------------------------------------------

    def draw(self) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        state = self.state
        copy = state.copy

        context = copy["boundContext"](state.current_id) if state.bound else copy["cliContext"]
        self.put(0, 1, copy["title"], curses.A_BOLD)
        self.put(0, max(1, width - display_width(context) - 2), context, self.color("muted"))
        self.put(1, 1, state.filter_line(), self.color("muted"))

        list_top = 3
        list_height = max(1, height - list_top - 3)
        self.draw_rows(list_top, list_height, width)

        summary_y = height - 3
        self.put(summary_y, 1, state.summary(), curses.A_BOLD)
        if state.status:
            self.put(height - 2, 1, truncate(state.status, width - 2), self.color("warn"))
        self.put(height - 1, 1, copy["keys"], self.color("muted"))
        self.screen.noutrefresh()
        curses.doupdate()

    def draw_rows(self, top: int, height: int, width: int) -> None:
        state = self.state
        copy = state.copy
        if state.loading and not state.sessions:
            self.put(top, 2, copy["loading"], self.color("muted"))
            return
        if not state.sessions:
            self.put(top, 2, copy["empty"], self.color("muted"))
            return

        if state.cursor < state.top:
            state.top = state.cursor
        if state.cursor >= state.top + height:
            state.top = state.cursor - height + 1
        state.top = max(0, min(state.top, max(0, len(state.sessions) - height)))

        project_width = 18
        time_width = 16
        flag_width = 12
        title_width = max(10, width - project_width - time_width - flag_width - 10)

        for offset in range(height):
            index = state.top + offset
            if index >= len(state.sessions):
                break
            row = state.sessions[index]
            focused = index == state.cursor
            deletable = bool(row.get("deletable"))
            mark = "x" if row["id"] in state.selected else " "
            box = f"[{mark}]" if deletable else " · "
            project = row.get("projectName") or copy["unknownProject"]
            flags = []
            if row.get("current"):
                flags.append(copy["current"])
            if row.get("archived"):
                flags.append(copy["archived"])
            if row.get("ephemeral"):
                flags.append(copy["ephemeral"])
            if row.get("blockingForkCount"):
                flags.append(copy["blocked"](row["blockingForkCount"]))
            line = "".join(
                (
                    f"{box} ",
                    pad(project, project_width),
                    " ",
                    pad(row.get("title") or "", title_width),
                    " ",
                    pad(format_time(row.get("updatedAt"), copy), time_width),
                    " ",
                    pad(" ".join(flags), flag_width),
                )
            )
            attr = curses.A_REVERSE if focused else 0
            if not deletable and not focused:
                attr |= self.color("muted")
            elif row.get("blockingForkCount") and not focused:
                attr |= self.color("warn")
            self.put(top + offset, 1, line, attr)

    # ---- overlays ----------------------------------------------------------

    def prompt(self, label: str) -> str | None:
        """Read a line with get_wch so CJK input works; Esc cancels."""
        height, width = self.screen.getmaxyx()
        buffer: list[str] = []
        curses.curs_set(1)
        try:
            while True:
                text = "".join(buffer)
                self.put(height - 2, 1, pad(label + text, width - 3), curses.A_BOLD)
                self.screen.noutrefresh()
                curses.doupdate()
                try:
                    key = self.screen.get_wch()
                except curses.error:
                    continue
                if key in ("\x1b",):
                    return None
                if key in ("\n", "\r", curses.KEY_ENTER):
                    return "".join(buffer)
                if key in ("\x7f", "\b", curses.KEY_BACKSPACE, 263):
                    if buffer:
                        buffer.pop()
                    continue
                if isinstance(key, str) and key.isprintable():
                    buffer.append(key)
        finally:
            curses.curs_set(0)

    def filter_overlay(self) -> None:
        state = self.state
        copy = state.copy
        fields = ("scope", "tag", "date_preset")
        original = (state.scope, state.tag, state.date_preset)
        row = 0
        while True:
            height, width = self.screen.getmaxyx()
            self.draw()
            box_top = max(1, height // 2 - 4)
            self.put(box_top, 2, pad(copy["filterTitle"], width - 6), curses.A_BOLD | curses.A_REVERSE)
            tag_label = copy["none"]
            for item in state.available_tags:
                if item.get("key") == state.tag:
                    tag_label = str(item.get("label") or state.tag)
            values = (
                f"{copy['scope']}: {copy['scopes'][state.scope]}",
                f"{copy['tag']}: {tag_label}",
                f"{copy['date']}: {copy['dates'][state.date_preset]}",
            )
            for index, value in enumerate(values):
                attr = curses.A_REVERSE if index == row else 0
                self.put(box_top + 1 + index, 3, pad(value, width - 8), attr)
            self.put(box_top + 5, 3, copy["filterHint"], self.color("muted"))
            self.screen.noutrefresh()
            curses.doupdate()

            key = self.screen.getch()
            if key in (27, ord("q")):
                state.scope, state.tag, state.date_preset = original
                return
            if key in (10, 13, curses.KEY_ENTER):
                if (state.scope, state.tag, state.date_preset) != original:
                    self.reload()
                return
            if key in (curses.KEY_UP, ord("k")):
                row = (row - 1) % len(fields)
            elif key in (curses.KEY_DOWN, ord("j")):
                row = (row + 1) % len(fields)
            elif key in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("h"), ord("l")):
                delta = 1 if key in (curses.KEY_RIGHT, ord("l")) else -1
                if fields[row] == "scope":
                    state.cycle("scope", SCOPES, delta)
                elif fields[row] == "date_preset":
                    state.cycle("date_preset", DATE_PRESETS, delta)
                else:
                    state.cycle("tag", tuple(state.tag_options()), delta)

    def files_overlay(self, row: dict[str, Any]) -> None:
        state = self.state
        copy = state.copy
        self.state.status = copy["filesLoading"]
        self.draw()
        try:
            data = self.server.inspect_files(row["id"])
            error = None
        except Exception as exc:  # 展示错误而不是让 TUI 崩掉
            data, error = None, str(exc)
        self.state.status = ""

        lines: list[tuple[str, int]] = []
        if error is not None or data is None:
            lines.append((error or copy["unknownError"], self.color("danger")))
        else:
            lines.append((copy["filesNote"], self.color("muted")))
            groups = (
                (copy["changedFiles"], data.get("changedFiles") or []),
                (copy["referencedFiles"], data.get("referencedFiles") or []),
            )
            for title, rows in groups:
                lines.append(("", 0))
                lines.append((f"{title} · {len(rows)}", curses.A_BOLD))
                if not rows:
                    lines.append((f"  {copy['noFiles']}", self.color("muted")))
                for entry in rows:
                    lines.append((f"  {entry.get('path')}", 0))

        offset = 0
        while True:
            height = self.screen.getmaxyx()[0]
            self.screen.erase()
            self.put(0, 1, copy["filesTitle"](row.get("title") or row["id"]), curses.A_BOLD)
            body_height = max(1, height - 3)
            for index in range(body_height):
                position = offset + index
                if position >= len(lines):
                    break
                text, attr = lines[position]
                self.put(1 + index, 2, text, attr)
            self.put(height - 1, 1, copy["filesHint"], self.color("muted"))
            self.screen.noutrefresh()
            curses.doupdate()

            key = self.screen.getch()
            if key in (curses.KEY_DOWN, ord("j")):
                offset = min(offset + 1, max(0, len(lines) - body_height))
            elif key in (curses.KEY_UP, ord("k")):
                offset = max(0, offset - 1)
            elif key == curses.KEY_NPAGE:
                offset = min(offset + body_height, max(0, len(lines) - body_height))
            elif key == curses.KEY_PPAGE:
                offset = max(0, offset - body_height)
            else:
                return

    def confirm_delete(self, count: int) -> bool:
        copy = self.state.copy
        height, width = self.screen.getmaxyx()
        self.draw()
        box_top = max(1, height // 2 - 3)
        self.put(box_top, 2, pad(copy["deleteTitle"](count), width - 6), curses.A_BOLD | self.color("danger"))
        self.put(box_top + 1, 2, truncate(copy["deleteBody"], width - 6), self.color("muted"))
        self.screen.noutrefresh()
        curses.doupdate()
        answer = self.prompt(copy["deletePrompt"](copy["confirmWord"]))
        return answer is not None and answer.strip() == copy["confirmWord"]

    # ---- actions -----------------------------------------------------------

    def reload(self) -> None:
        state = self.state
        state.loading = True
        try:
            data = self.server.list_sessions(
                state.current_id,
                state.scope,
                state.search,
                state.date_preset,
                "",
                "",
                state.tag,
            )
            state.apply(data)
            state.status = ""
        except Exception as exc:
            state.status = state.copy["loadFailed"](str(exc))
        finally:
            state.loading = False

    def report(self, results: list[dict[str, Any]], success: str) -> None:
        copy = self.state.copy
        failed = [item for item in results if not item.get("ok")]
        if failed:
            self.state.status = copy["failed"](len(failed), failed[0].get("error") or copy["unknownError"])
        else:
            self.state.status = success

    def archive(self) -> None:
        targets = self.state.targets()
        copy = self.state.copy
        if not targets:
            self.state.status = copy["noSelection"]
            return
        self.state.status = copy["archiving"]
        self.draw()
        try:
            data = self.server.archive_sessions(targets, self.state.current_id)
            self.state.reset_selection()
            self.reload()
            self.report(data.get("results") or [], copy["archived_ok"])
        except Exception as exc:
            self.state.status = str(exc)

    def delete(self) -> None:
        targets = self.state.targets()
        copy = self.state.copy
        if not targets:
            self.state.status = copy["noSelection"]
            return
        if not self.confirm_delete(len(targets)):
            self.state.status = ""
            return
        self.state.status = copy["deleting"]
        self.draw()
        try:
            data = self.server.delete_sessions(targets, copy["confirmWord"], self.state.current_id)
            self.state.reset_selection()
            self.reload()
            sync = data.get("sidebarSync") or {}
            success = copy["deleted_ok"] if sync.get("ok", True) else copy["syncFailed"]
            self.report(data.get("results") or [], success)
        except Exception as exc:
            self.state.status = str(exc)

    def search_prompt(self) -> None:
        copy = self.state.copy
        answer = self.prompt(copy["searchPrompt"])
        if answer is None:
            return
        self.state.set_filter(search=answer.strip())
        self.reload()

    # ---- loop --------------------------------------------------------------

    def run(self) -> None:
        curses.curs_set(0)
        self.screen.keypad(True)
        self.setup_colors()
        self.reload()
        while True:
            self.draw()
            key = self.screen.getch()
            if key in (ord("q"), ord("Q")):
                return
            if key in (curses.KEY_DOWN, ord("j")):
                self.state.move(1)
            elif key in (curses.KEY_UP, ord("k")):
                self.state.move(-1)
            elif key == curses.KEY_NPAGE:
                self.state.move(10)
            elif key == curses.KEY_PPAGE:
                self.state.move(-10)
            elif key == curses.KEY_HOME:
                self.state.cursor = 0
            elif key == curses.KEY_END:
                self.state.move(len(self.state.sessions))
            elif key == ord(" "):
                self.state.toggle_current()
            elif key in (ord("r"), ord("R")):
                self.reload()
            elif key == ord("/"):
                self.search_prompt()
            elif key in (ord("f"), ord("F")):
                self.filter_overlay()
            elif key in (ord("i"), ord("I")):
                row = self.state.current_row()
                if row is not None:
                    self.files_overlay(row)
            elif key in (ord("a"), ord("A")):
                self.archive()
            elif key in (ord("d"), ord("D")):
                self.delete()
            elif key == curses.KEY_RESIZE:
                continue


def build_state(args: argparse.Namespace) -> TuiState:
    locale = detect_locale(args.lang)
    current_id, bound = detect_current_thread(args.current)
    return TuiState(locale=locale, current_id=current_id, bound=bound)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Codex 会话清理器终端界面")
    parser.add_argument("--lang", choices=("zh", "en"), help="界面语言，默认跟随环境变量")
    parser.add_argument(
        "--current",
        help="当前 Codex 会话 ID；提供后该会话受保护，不可归档或删除",
    )
    args = parser.parse_args(argv)

    if not sys.stdout.isatty():
        print("终端界面需要在交互式终端中运行。", file=sys.stderr)
        return 2

    server = load_server()
    state = build_state(args)
    try:
        curses.wrapper(lambda screen: SessionTui(screen, server, state).run())
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
