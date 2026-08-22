#!/usr/bin/env python3
"""Local MCP server for safely managing Codex threads."""

from __future__ import annotations

import atexit
import calendar
import json
import os
import queue
import select
import shutil
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


VERSION = "0.1.0"
RESOURCE_URI = "ui://codex-session-cleaner/manager-v1.html"
SOURCE_KINDS = [
    "cli", "vscode", "exec", "appServer", "subAgent", "subAgentReview",
    "subAgentCompact", "subAgentThreadSpawn", "subAgentOther", "unknown",
]
DATE_PRESETS = {
    "all",
    "older_than_3_months",
    "older_than_1_month",
    "older_than_1_week",
    "custom",
}
ROOT = Path(__file__).resolve().parent.parent
UI_PATH = ROOT / "web" / "manager.html"


class AppServerError(RuntimeError):
    pass


class AppServerClient:
    """Small synchronous client for the line-delimited Codex app-server protocol."""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self.process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._next_id = 1
        self._stderr: queue.Queue[str] = queue.Queue(maxsize=100)

    @staticmethod
    def _find_codex() -> str:
        candidates = [
            os.environ.get("CODEX_CLI_PATH"),
            shutil.which("codex"),
            "/opt/homebrew/bin/codex",
            "/usr/local/bin/codex",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return candidate
        raise AppServerError("未找到 codex CLI。可通过 CODEX_CLI_PATH 指定路径。")

    def _drain_stderr(self) -> None:
        assert self.process and self.process.stderr
        for line in self.process.stderr:
            try:
                self._stderr.put_nowait(line.rstrip())
            except queue.Full:
                try:
                    self._stderr.get_nowait()
                    self._stderr.put_nowait(line.rstrip())
                except queue.Empty:
                    pass

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return
        self.process = subprocess.Popen(
            [self._find_codex(), "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_session_cleaner",
                    "title": "Codex Session Cleaner",
                    "version": VERSION,
                },
                "capabilities": {
                    "optOutNotificationMethods": [
                        "thread/status/changed", "thread/name/updated",
                        "thread/tokenUsage/updated", "turn/started", "turn/completed",
                        "item/started", "item/completed", "item/agentMessage/delta",
                    ]
                },
            },
            ensure_started=False,
        )
        self.notify("initialized", {})

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.start()
        assert self.process and self.process.stdin
        self.process.stdin.write(json.dumps({"method": method, "params": params}) + "\n")
        self.process.stdin.flush()

    def request(
        self, method: str, params: dict[str, Any], *, ensure_started: bool = True
    ) -> Any:
        if ensure_started:
            self.start()
        assert self.process and self.process.stdin and self.process.stdout
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            message = {"method": method, "id": request_id, "params": params}
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    errors = "\n".join(list(self._stderr.queue)[-8:])
                    raise AppServerError(f"Codex app-server 已退出。{errors}")
                ready, _, _ = select.select(
                    [self.process.stdout], [], [], min(0.25, deadline - time.monotonic())
                )
                if not ready:
                    continue
                line = self.process.stdout.readline()
                if not line:
                    continue
                try:
                    response = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if response.get("id") != request_id:
                    continue
                if "error" in response:
                    err = response["error"]
                    detail = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    raise AppServerError(f"{method} 失败：{detail}")
                return response.get("result")
        raise AppServerError(f"等待 {method} 响应超时。")

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()


APP = AppServerClient()
atexit.register(APP.close)
PROTECTED_THREAD_IDS: set[str] = set()


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _remove_from_desktop_catalog(thread_ids: list[str]) -> dict[str, Any]:
    """Remove exact, already-deleted thread IDs from the desktop sidebar catalog."""
    database = _codex_home() / "sqlite" / "codex-dev.db"
    result: dict[str, Any] = {
        "available": database.is_file(),
        "removedThreadIds": [],
        "error": None,
    }
    if not database.is_file() or not thread_ids:
        return result

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(database, timeout=2.0)
        connection.execute("PRAGMA busy_timeout = 2000")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        required_tables = {
            "local_thread_catalog",
            "local_thread_catalog_sync_state",
            "local_thread_catalog_metadata",
        }
        if not required_tables.issubset(tables):
            raise RuntimeError("桌面会话目录结构不兼容，已跳过侧边栏同步。")
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(local_thread_catalog)")
        }
        if not {"host_id", "thread_id", "missing_candidate"}.issubset(columns):
            raise RuntimeError("桌面会话目录字段不兼容，已跳过侧边栏同步。")

        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """UPDATE local_thread_catalog_sync_state
               SET observation_sequence = observation_sequence + 1
               WHERE host_id = ?""",
            ("local",),
        )
        visible_removed = []
        for thread_id in thread_ids:
            row = connection.execute(
                """DELETE FROM local_thread_catalog
                   WHERE host_id = ? AND thread_id = ?
                   RETURNING missing_candidate""",
                ("local", thread_id),
            ).fetchone()
            if row is not None:
                result["removedThreadIds"].append(thread_id)
                if row[0] == 0:
                    visible_removed.append(thread_id)
        if visible_removed:
            connection.execute(
                """UPDATE local_thread_catalog_metadata
                   SET catalog_revision = catalog_revision + 1
                   WHERE id = 1"""
            )
        connection.commit()
    except Exception as exc:
        if connection is not None:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
        result["error"] = str(exc)
    finally:
        if connection is not None:
            connection.close()
    return result


def _read_ipc_frame(stream: socket.socket, timeout: float = 2.0) -> dict[str, Any]:
    stream.settimeout(timeout)

    def receive_exact(length: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < length:
            chunk = stream.recv(length - len(chunks))
            if not chunk:
                raise RuntimeError("Codex 桌面 IPC 已断开。")
            chunks.extend(chunk)
        return bytes(chunks)

    length = struct.unpack("<I", receive_exact(4))[0]
    if length <= 0 or length > 256 * 1024 * 1024:
        raise RuntimeError("Codex 桌面 IPC 返回了无效数据帧。")
    message = json.loads(receive_exact(length).decode("utf-8"))
    if not isinstance(message, dict):
        raise RuntimeError("Codex 桌面 IPC 返回格式无效。")
    return message


def _write_ipc_frame(stream: socket.socket, message: dict[str, Any]) -> None:
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
    stream.sendall(struct.pack("<I", len(payload)) + payload)


def _notify_desktop_sidebar(thread_ids: list[str], cwd_by_id: dict[str, str]) -> dict[str, Any]:
    """Notify the running desktop windows after catalog rows are removed."""
    endpoint = _codex_home() / "ipc" / "ipc.sock"
    result: dict[str, Any] = {
        "available": endpoint.exists(),
        "notifiedThreadIds": [],
        "error": None,
    }
    if not endpoint.exists() or not thread_ids:
        return result
    try:
        endpoint_stat = endpoint.stat()
        parent_stat = endpoint.parent.stat()
        if (
            not stat.S_ISSOCK(endpoint_stat.st_mode)
            or endpoint_stat.st_uid != os.getuid()
            or parent_stat.st_uid != os.getuid()
            or parent_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError("Codex 桌面 IPC 所有权或权限不安全，已拒绝连接。")

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.settimeout(2.0)
            stream.connect(str(endpoint))
            request_id = str(uuid.uuid4())
            _write_ipc_frame(
                stream,
                {
                    "type": "request",
                    "requestId": request_id,
                    "sourceClientId": "initializing-client",
                    "version": 0,
                    "method": "initialize",
                    "params": {"clientType": "codex-session-cleaner"},
                    "timeoutMs": 2000,
                },
            )
            response = _read_ipc_frame(stream)
            while response.get("type") != "response" or response.get("requestId") != request_id:
                response = _read_ipc_frame(stream)
            if response.get("resultType") != "success":
                raise RuntimeError("Codex 桌面 IPC 初始化失败。")
            client_id = str((response.get("result") or {}).get("clientId") or "")
            if not client_id:
                raise RuntimeError("Codex 桌面 IPC 未返回客户端 ID。")
            for thread_id in thread_ids:
                _write_ipc_frame(
                    stream,
                    {
                        "type": "broadcast",
                        "method": "thread-archived",
                        "sourceClientId": client_id,
                        "version": 2,
                        "params": {
                            "hostId": "local",
                            "conversationId": thread_id,
                            "cwd": cwd_by_id.get(thread_id, ""),
                        },
                    },
                )
                result["notifiedThreadIds"].append(thread_id)
    except Exception as exc:
        result["error"] = str(exc)
    return result


def sync_desktop_sidebar(thread_ids: list[str], cwd_by_id: dict[str, str]) -> dict[str, Any]:
    catalog = _remove_from_desktop_catalog(thread_ids)
    notification = _notify_desktop_sidebar(thread_ids, cwd_by_id)
    errors = [item for item in (catalog.get("error"), notification.get("error")) if item]
    return {
        "ok": not errors,
        "catalog": catalog,
        "notification": notification,
        "warnings": errors,
    }


def _thread_id_from_meta(meta: Any) -> str | None:
    if not isinstance(meta, dict):
        return None
    keys = (
        "openai/threadId", "openai/thread_id", "codexThreadId", "codex_thread_id",
        "threadId", "thread_id",
    )
    for key in keys:
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    encoded = meta.get("x-codex-turn-metadata")
    if isinstance(encoded, str):
        try:
            return _thread_id_from_meta(json.loads(encoded))
        except json.JSONDecodeError:
            return None
    if isinstance(encoded, dict):
        return _thread_id_from_meta(encoded)
    return None


def _status_name(status: Any) -> str:
    if isinstance(status, dict):
        return str(status.get("type") or status.get("status") or "unknown")
    return str(status or "unknown")


def _timestamp_seconds(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number / 1000 if number >= 10_000_000_000 else number


def _months_before(moment: datetime, months: int) -> datetime:
    month_index = moment.year * 12 + moment.month - 1 - months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def _parse_filter_date(value: str, label: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"{label}必须是 YYYY-MM-DD 格式。") from exc


def _date_filter_bounds(
    preset: str,
    custom_start: str = "",
    custom_end: str = "",
    now: datetime | None = None,
) -> tuple[float | None, float | None]:
    if preset not in DATE_PRESETS:
        raise ValueError("datePreset 参数无效。")
    current = now or datetime.now()
    if preset == "all":
        return None, None
    if preset == "older_than_3_months":
        return None, _months_before(current, 3).timestamp()
    if preset == "older_than_1_month":
        return None, _months_before(current, 1).timestamp()
    if preset == "older_than_1_week":
        return None, (current - timedelta(days=7)).timestamp()
    if not custom_start and not custom_end:
        raise ValueError("自定义日期至少需要填写开始日期或结束日期。")
    start = _parse_filter_date(custom_start, "开始日期") if custom_start else None
    end = _parse_filter_date(custom_end, "结束日期") if custom_end else None
    if start is not None and end is not None and start > end:
        raise ValueError("开始日期不能晚于结束日期。")
    return (
        start.timestamp() if start is not None else None,
        (end + timedelta(days=1)).timestamp() if end is not None else None,
    )


def _matches_date_filter(item: dict[str, Any], start: float | None, end: float | None) -> bool:
    if start is None and end is None:
        return True
    updated_at = _timestamp_seconds(item.get("updatedAt"))
    if updated_at is None:
        return False
    return (start is None or updated_at >= start) and (end is None or updated_at < end)


def _normalize_thread(thread: dict[str, Any], archived: bool, current_id: str | None) -> dict[str, Any]:
    cwd = str(thread.get("cwd") or "")
    git = thread.get("gitInfo") if isinstance(thread.get("gitInfo"), dict) else {}
    preview = str(thread.get("preview") or "").strip().replace("\n", " ")
    title = str(thread.get("name") or "").strip() or preview[:90] or "未命名会话"
    return {
        "id": str(thread.get("id") or thread.get("sessionId") or ""),
        "title": title,
        "preview": preview[:240],
        "cwd": cwd,
        "projectName": Path(cwd).name if cwd else "未知项目",
        "branch": git.get("branch"),
        "sha": git.get("sha"),
        "originUrl": git.get("originUrl"),
        "createdAt": thread.get("createdAt"),
        "updatedAt": thread.get("updatedAt"),
        "archived": archived,
        "ephemeral": bool(thread.get("ephemeral")),
        "parentThreadId": thread.get("parentThreadId"),
        "status": _status_name(thread.get("status")),
        "source": thread.get("source"),
        "projectId": thread.get("projectId"),
        "current": str(thread.get("id") or "") == current_id,
        "protected": str(thread.get("id") or "") in PROTECTED_THREAD_IDS,
    }


def _list_one(archived: bool, search: str = "") -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(50):
        params: dict[str, Any] = {
            "archived": archived,
            "cursor": cursor,
            "limit": 100,
            "sortKey": "updated_at",
            "sortDirection": "desc",
            "sourceKinds": SOURCE_KINDS,
            "useStateDbOnly": True,
        }
        if search:
            params["searchTerm"] = search
        result = APP.request("thread/list", params) or {}
        page = result.get("data", []) if isinstance(result, dict) else []
        output.extend(item for item in page if isinstance(item, dict))
        cursor = result.get("nextCursor") if isinstance(result, dict) else None
        if not cursor:
            break
    return output


def list_sessions(
    current_id: str | None,
    scope: str = "all",
    search: str = "",
    date_preset: str = "all",
    custom_start: str = "",
    custom_end: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    date_start, date_end = _date_filter_bounds(
        date_preset, custom_start, custom_end, now
    )
    if current_id:
        PROTECTED_THREAD_IDS.add(current_id)
    raw: list[tuple[dict[str, Any], bool]] = []
    if scope in ("active", "all"):
        raw.extend((item, False) for item in _list_one(False, search))
    if scope in ("archived", "all"):
        raw.extend((item, True) for item in _list_one(True, search))
    children: dict[str, list[str]] = {}
    for thread, _ in raw:
        parent = thread.get("parentThreadId")
        child = thread.get("id")
        if parent and child:
            children.setdefault(str(parent), []).append(str(child))

    def descendants(thread_id: str) -> int:
        seen: set[str] = set()
        stack = list(children.get(thread_id, []))
        while stack:
            item = stack.pop()
            if item in seen:
                continue
            seen.add(item)
            stack.extend(children.get(item, []))
        return len(seen)

    normalized = []
    for thread, archived in raw:
        if thread.get("parentThreadId") is not None:
            continue
        item = _normalize_thread(thread, archived, current_id)
        if item["id"] and _matches_date_filter(item, date_start, date_end):
            item["descendantCount"] = descendants(item["id"])
            item["deletable"] = not item["current"] and not item["protected"] and not item["ephemeral"]
            normalized.append(item)
    normalized.sort(key=lambda x: x.get("updatedAt") or 0, reverse=True)
    return {
        "sessions": normalized,
        "total": len(normalized),
        "truncated": False,
        "currentThreadId": current_id,
        "scope": scope,
        "search": search,
        "dateFilter": {
            "preset": date_preset,
            "customStart": custom_start,
            "customEnd": custom_end,
        },
    }


def _resolve_file(path: str, cwd: str) -> str:
    candidate = Path(os.path.expanduser(path))
    if not candidate.is_absolute() and cwd:
        candidate = Path(cwd) / candidate
    return os.path.normpath(str(candidate))


def inspect_files(thread_id: str) -> dict[str, Any]:
    result = APP.request("thread/read", {"threadId": thread_id, "includeTurns": True}) or {}
    thread = result.get("thread", result) if isinstance(result, dict) else {}
    cwd = str(thread.get("cwd") or "") if isinstance(thread, dict) else ""
    changed: dict[str, set[str]] = {}
    referenced: set[str] = set()
    turns = thread.get("turns", []) if isinstance(thread, dict) else []
    for turn in turns if isinstance(turns, list) else []:
        for item in turn.get("items", []) if isinstance(turn, dict) else []:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            item_cwd = str(item.get("cwd") or cwd)
            if item_type == "fileChange":
                for change in item.get("changes", []):
                    if not isinstance(change, dict) or not change.get("path"):
                        continue
                    path = _resolve_file(str(change["path"]), item_cwd)
                    changed.setdefault(path, set()).add(str(change.get("kind") or "change"))
            elif item_type == "commandExecution":
                script_path = item.get("scriptPath")
                if isinstance(script_path, str) and script_path:
                    referenced.add(_resolve_file(script_path, item_cwd))
                for action in item.get("commandActions", []) or []:
                    if isinstance(action, dict) and isinstance(action.get("path"), str):
                        referenced.add(_resolve_file(action["path"], item_cwd))
    changed_rows = [
        {"path": path, "kinds": sorted(kinds), "insideProject": bool(cwd and (path == cwd or path.startswith(cwd + os.sep)))}
        for path, kinds in sorted(changed.items())
    ]
    referenced_rows = [
        {"path": path, "insideProject": bool(cwd and (path == cwd or path.startswith(cwd + os.sep)))}
        for path in sorted(referenced - set(changed))
    ]
    return {
        "threadId": thread_id,
        "cwd": cwd,
        "gitInfo": thread.get("gitInfo") if isinstance(thread, dict) else None,
        "changedFiles": changed_rows[:200],
        "referencedFiles": referenced_rows[:200],
        "truncated": len(changed_rows) > 200 or len(referenced_rows) > 200,
        "note": "文件来自会话记录中的修改与命令动作线索，不是项目完整文件清单。",
    }


def _validate_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("threadIds 必须是数组。")
    ids = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("会话 ID 不能为空。")
        item = item.strip()
        if item not in ids:
            ids.append(item)
    if not ids:
        raise ValueError("请至少选择一个会话。")
    if len(ids) > 100:
        raise ValueError("单次最多处理 100 个会话。")
    return ids


def archive_sessions(ids: list[str], current_id: str | None) -> dict[str, Any]:
    results = []
    protected = PROTECTED_THREAD_IDS | ({current_id} if current_id else set())
    for thread_id in ids:
        if thread_id in protected:
            results.append({"threadId": thread_id, "ok": False, "error": "当前管理会话受保护。"})
            continue
        try:
            APP.request("thread/archive", {"threadId": thread_id})
            results.append({"threadId": thread_id, "ok": True})
        except Exception as exc:
            results.append({"threadId": thread_id, "ok": False, "error": str(exc)})
    return {"operation": "archive", "results": results}


def delete_sessions(ids: list[str], confirmation: str, current_id: str | None) -> dict[str, Any]:
    if confirmation != "永久删除":
        raise ValueError("确认词不正确，必须输入“永久删除”。")
    if not current_id:
        raise ValueError("缺少当前会话元数据；为避免误删，已拒绝操作。请从 Codex 会话管理页执行。")
    PROTECTED_THREAD_IDS.add(current_id)
    protected = PROTECTED_THREAD_IDS | {current_id}
    if any(thread_id in protected for thread_id in ids):
        raise ValueError("选中项包含当前或受保护会话，已拒绝整批删除。")
    sessions = list_sessions(current_id, "all", "")["sessions"]
    by_id = {item["id"]: item for item in sessions}
    unavailable = [thread_id for thread_id in ids if thread_id not in by_id]
    unsafe = [thread_id for thread_id in ids if thread_id in by_id and not by_id[thread_id]["deletable"]]
    if unavailable:
        raise ValueError("部分会话已不存在或不是顶层会话，请刷新后重试：" + ", ".join(unavailable))
    if unsafe:
        raise ValueError("部分会话不可删除：" + ", ".join(unsafe))
    results = []
    deleted_ids = []
    for thread_id in ids:
        try:
            APP.request("thread/delete", {"threadId": thread_id})
            deleted_ids.append(thread_id)
            results.append({"threadId": thread_id, "ok": True})
        except Exception as exc:
            results.append({"threadId": thread_id, "ok": False, "error": str(exc)})
    cwd_by_id = {thread_id: str(by_id[thread_id].get("cwd") or "") for thread_id in deleted_ids}
    sidebar_sync = sync_desktop_sidebar(deleted_ids, cwd_by_id)
    return {"operation": "delete", "results": results, "sidebarSync": sidebar_sync}


def _tool_definitions() -> list[dict[str, Any]]:
    ui_meta = {"ui": {"resourceUri": RESOURCE_URI}, "openai/outputTemplate": RESOURCE_URI}
    return [
        {
            "name": "open_session_manager",
            "title": "打开 Codex 会话管理页",
            "description": "列出本地 Codex 会话并打开管理界面。",
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
            "_meta": ui_meta,
        },
        {
            "name": "list_sessions",
            "title": "列出 Codex 会话",
            "description": "按活动/归档状态、搜索词和最后更新时间列出会话。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": ["active", "archived", "all"], "default": "all"},
                    "search": {"type": "string", "default": ""},
                    "datePreset": {
                        "type": "string",
                        "enum": ["all", "older_than_3_months", "older_than_1_month", "older_than_1_week", "custom"],
                        "default": "all",
                    },
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
            "description": "批量归档所选顶层会话；不会归档当前管理会话。",
            "inputSchema": {
                "type": "object", "properties": {"threadIds": {"type": "array", "items": {"type": "string"}}}, "required": ["threadIds"]
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        },
        {
            "name": "delete_sessions",
            "title": "永久删除 Codex 会话",
            "description": "永久删除所选会话及其派生会话；不会删除项目文件，且拒绝删除当前管理会话。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "threadIds": {"type": "array", "items": {"type": "string"}},
                    "confirmation": {"type": "string", "const": "永久删除"},
                },
                "required": ["threadIds", "confirmation"],
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": True},
        },
    ]


def _text_result(data: dict[str, Any], summary: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": summary}],
        "structuredContent": data,
        "isError": False,
    }


def call_tool(name: str, arguments: dict[str, Any], meta: Any) -> dict[str, Any]:
    current_id = _thread_id_from_meta(meta)
    if current_id:
        PROTECTED_THREAD_IDS.add(current_id)
    if name in ("open_session_manager", "list_sessions"):
        scope = "all" if name == "open_session_manager" else str(arguments.get("scope") or "all")
        if scope not in ("active", "archived", "all"):
            raise ValueError("scope 必须是 active、archived 或 all。")
        data = list_sessions(
            current_id,
            scope,
            str(arguments.get("search") or ""),
            str(arguments.get("datePreset") or "all"),
            str(arguments.get("customStart") or ""),
            str(arguments.get("customEnd") or ""),
        )
        return _text_result(data, f"已列出 {data['total']} 个顶层 Codex 会话。")
    if name == "inspect_session_files":
        thread_id = str(arguments.get("threadId") or "").strip()
        if not thread_id:
            raise ValueError("threadId 不能为空。")
        data = inspect_files(thread_id)
        return _text_result(data, f"已提取 {len(data['changedFiles'])} 个修改文件和 {len(data['referencedFiles'])} 个引用文件线索。")
    if name == "archive_sessions":
        data = archive_sessions(_validate_ids(arguments.get("threadIds")), current_id)
        return _text_result(data, "归档操作已完成。")
    if name == "delete_sessions":
        data = delete_sessions(
            _validate_ids(arguments.get("threadIds")),
            str(arguments.get("confirmation") or ""),
            current_id,
        )
        summary = "永久删除操作已完成。"
        if not data["sidebarSync"]["ok"]:
            summary += " 侧边栏同步未完全成功；请刷新或重启 Codex。"
        return _text_result(data, summary)
    raise ValueError(f"未知工具：{name}")


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    if request_id is None:
        return None
    try:
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False}},
                "serverInfo": {"name": "codex-session-cleaner", "version": VERSION},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": _tool_definitions()}
        elif method == "tools/call":
            params = request.get("params") or {}
            result = call_tool(str(params.get("name") or ""), params.get("arguments") or {}, params.get("_meta"))
        elif method == "resources/list":
            result = {"resources": [{"uri": RESOURCE_URI, "name": "Codex 会话管理页", "mimeType": "text/html;profile=mcp-app"}]}
        elif method == "resources/read":
            uri = (request.get("params") or {}).get("uri")
            if uri != RESOURCE_URI:
                raise ValueError("未知资源。")
            result = {"contents": [{"uri": RESOURCE_URI, "mimeType": "text/html;profile=mcp-app", "text": UI_PATH.read_text(encoding="utf-8")}]}
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as exc:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": str(exc)}}


def main() -> None:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = handle(request)
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
