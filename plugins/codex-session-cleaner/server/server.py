#!/usr/bin/env python3
"""Local MCP server for safely managing Codex threads."""

from __future__ import annotations

import atexit
import calendar
import json
import os
import queue
import re
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
MANAGER_CONTEXT_TTL_SECONDS = 2 * 60 * 60
DATE_PRESETS = {
    "all",
    "within_1_day",
    "within_1_week",
    "within_1_month",
    "older_than_3_months",
    "older_than_1_month",
    "older_than_1_week",
    "custom",
}
TAG_LABELS = {
    "hidden-fork": "隐藏分叉",
    "session-management": "会话管理",
    "automation": "自动化",
    "plugin-development": "插件开发",
    "lark": "飞书协作",
    "documents": "文档表格",
    "media": "图像视频",
    "development": "代码开发",
    "general": "常规任务",
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
                    [self.process.stdout], [], [], max(0.0, min(0.25, deadline - time.monotonic()))
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
_MANAGER_CONTEXTS: dict[str, tuple[str, float]] = {}
# initialize 握手里的宿主信息，用于判断客户端能否渲染 MCP Apps 组件。
_HOST: dict[str, Any] = {}


class HostError(RuntimeError):
    pass


def _write_message(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


class HostBridge:
    """Reverse JSON-RPC channel to the MCP host, used for elicitation.

    Reads stdin inline while waiting, so anything the host sends meanwhile is
    parked in `deferred` for the main loop to process afterwards.
    """

    def __init__(self) -> None:
        self._next_id = 1
        self.deferred: list[dict[str, Any]] = []

    def request(self, method: str, params: dict[str, Any], timeout: float = 120.0) -> Any:
        # 字符串前缀避免和宿主自己的数字 id 撞车。
        request_id = f"scc-{self._next_id}"
        self._next_id += 1
        _write_message({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = sys.stdin.readline()
            if not line:
                raise HostError("宿主连接已关闭。")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == request_id and ("result" in message or "error" in message):
                if "error" in message:
                    error = message["error"]
                    detail = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                    raise HostError(detail)
                return message.get("result")
            self.deferred.append(message)
        raise HostError(f"等待宿主响应 {method} 超时。")


HOST = HostBridge()


def _host_supports_elicitation() -> bool:
    capabilities = _HOST.get("capabilities")
    return isinstance(capabilities, dict) and "elicitation" in capabilities


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


def _locale_from_meta(meta: Any) -> str | None:
    if not isinstance(meta, dict):
        return None
    for key in ("openai/locale", "locale", "language", "openai/language"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return "zh" if value.strip().lower().startswith("zh") else "en"
    encoded = meta.get("x-codex-turn-metadata")
    if isinstance(encoded, str):
        try:
            return _locale_from_meta(json.loads(encoded))
        except json.JSONDecodeError:
            return None
    if isinstance(encoded, dict):
        return _locale_from_meta(encoded)
    return None


def _purge_manager_contexts(now: float | None = None) -> None:
    current = time.monotonic() if now is None else now
    expired = [token for token, (_, expires_at) in _MANAGER_CONTEXTS.items() if expires_at <= current]
    for token in expired:
        _MANAGER_CONTEXTS.pop(token, None)


def _create_manager_context(current_id: str | None) -> str | None:
    if not current_id:
        return None
    _purge_manager_contexts()
    token = uuid.uuid4().hex
    _MANAGER_CONTEXTS[token] = (current_id, time.monotonic() + MANAGER_CONTEXT_TTL_SECONDS)
    return token


def _current_id_for_call(
    meta: Any,
    arguments: dict[str, Any],
    *,
    require: bool = False,
) -> tuple[str | None, str | None]:
    """Resolve the current thread from host metadata or an opaque manager-page context."""
    current_id = _thread_id_from_meta(meta)
    token_value = arguments.get("managerContext")
    token = token_value.strip() if isinstance(token_value, str) else ""
    if token:
        _purge_manager_contexts()
        context = _MANAGER_CONTEXTS.get(token)
        if context is None:
            raise ValueError("管理页上下文已失效，请重新打开 Codex 会话管理页。")
        context_id, _ = context
        if current_id and current_id != context_id:
            raise ValueError("管理页上下文与当前任务不一致，请重新打开会话管理页。")
        _MANAGER_CONTEXTS[token] = (
            context_id,
            time.monotonic() + MANAGER_CONTEXT_TTL_SECONDS,
        )
        return context_id, token
    if require and not current_id:
        raise ValueError("缺少当前任务上下文；为避免误操作，请重新打开 Codex 会话管理页。")
    return current_id, None


def _decode_source(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def _nested_parent_thread_id(value: Any) -> str | None:
    value = _decode_source(value)
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).replace("_", "").lower()
            if normalized == "parentthreadid" and isinstance(nested, str) and nested.strip():
                return nested.strip()
        for nested in value.values():
            parent = _nested_parent_thread_id(nested)
            if parent:
                return parent
    elif isinstance(value, list):
        for nested in value:
            parent = _nested_parent_thread_id(nested)
            if parent:
                return parent
    return None


def _parent_thread_id(thread: dict[str, Any]) -> str | None:
    for key in ("parentThreadId", "parent_thread_id"):
        value = thread.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _nested_parent_thread_id(thread.get("source"))


def _is_derived_thread(thread: dict[str, Any]) -> bool:
    if _parent_thread_id(thread):
        return True
    thread_source = str(thread.get("threadSource") or thread.get("thread_source") or "").lower()
    if thread_source == "subagent":
        return True
    source = _decode_source(thread.get("source"))
    if isinstance(source, dict):
        return any(str(key).replace("_", "").lower().startswith("subagent") for key in source)
    return "subagent" in str(source or "").replace("_", "").lower()


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


def _iso_timestamp_seconds(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


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
    if preset == "within_1_day":
        return (current - timedelta(days=1)).timestamp(), None
    if preset == "within_1_week":
        return (current - timedelta(days=7)).timestamp(), None
    if preset == "within_1_month":
        return _months_before(current, 1).timestamp(), None
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


def _state_database() -> Path | None:
    candidates = (_codex_home() / "state_5.sqlite", _codex_home() / "sqlite" / "state_5.sqlite")
    return next((path for path in candidates if path.is_file()), None)


def _state_thread_metadata(thread_ids: set[str]) -> dict[str, dict[str, Any]]:
    database = _state_database()
    if database is None or not thread_ids:
        return {}
    connection: sqlite3.Connection | None = None
    output: dict[str, dict[str, Any]] = {}
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2.0)
        connection.row_factory = sqlite3.Row
        ids = sorted(thread_ids)
        for offset in range(0, len(ids), 500):
            chunk = ids[offset : offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"""SELECT id, name, title, preview, cwd, source, thread_source,
                           created_at, updated_at, archived, git_sha, git_branch,
                           git_origin_url, has_user_event, tokens_used
                    FROM threads WHERE id IN ({placeholders})""",
                chunk,
            )
            for row in rows:
                output[str(row["id"])] = dict(row)
    except sqlite3.Error:
        return {}
    finally:
        if connection is not None:
            connection.close()
    return output


def _scan_history_base_threads() -> list[dict[str, Any]]:
    """Read paginated-history links, including forks hidden from thread/list."""
    records: dict[str, dict[str, Any]] = {}
    roots = (
        (_codex_home() / "sessions", False),
        (_codex_home() / "archived_sessions", True),
    )
    for root, archived in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.jsonl"):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    envelope = json.loads(handle.readline())
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(envelope, dict) or envelope.get("type") != "session_meta":
                continue
            payload = envelope.get("payload")
            if not isinstance(payload, dict):
                continue
            history_base = payload.get("history_base")
            if not isinstance(history_base, dict):
                continue
            thread_id = str(payload.get("id") or "").strip()
            base_id = str(history_base.get("thread_id") or "").strip()
            if not thread_id or not base_id or thread_id == base_id:
                continue
            records[thread_id] = {
                "id": thread_id,
                "name": "",
                "preview": "",
                "cwd": str(payload.get("cwd") or ""),
                "source": payload.get("source"),
                "threadSource": payload.get("thread_source"),
                "createdAt": _iso_timestamp_seconds(payload.get("timestamp")),
                "updatedAt": _iso_timestamp_seconds(payload.get("timestamp")),
                "archived": archived,
                "ephemeral": False,
                "status": {"type": "notLoaded"},
                "historyBaseThreadId": base_id,
                "hiddenFromList": True,
                "rolloutPath": str(path),
            }

    state_rows = _state_thread_metadata(set(records))
    for thread_id, record in records.items():
        row = state_rows.get(thread_id)
        if not row:
            record["name"] = f"隐藏分叉 {thread_id[:8]}"
            continue
        record.update(
            {
                "name": str(row.get("name") or row.get("title") or "").strip()
                or f"隐藏分叉 {thread_id[:8]}",
                "preview": str(row.get("preview") or ""),
                "cwd": str(row.get("cwd") or record["cwd"]),
                "source": row.get("source") or record.get("source"),
                "threadSource": row.get("thread_source") or record.get("threadSource"),
                "createdAt": row.get("created_at") or record.get("createdAt"),
                "updatedAt": row.get("updated_at") or record.get("updatedAt"),
                "archived": bool(row.get("archived")),
                "gitInfo": {
                    "sha": row.get("git_sha"),
                    "branch": row.get("git_branch"),
                    "originUrl": row.get("git_origin_url"),
                },
                "hasUserEvent": bool(row.get("has_user_event")),
                "tokensUsed": row.get("tokens_used") or 0,
            }
        )
    return list(records.values())


def _tag(key: str) -> dict[str, str]:
    return {"key": key, "label": TAG_LABELS[key]}


def _classification_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"\[[^\]\n]*\]\(\s*plugin://[^)\s]+\s*\)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"plugin://[^\s)]+", " ", text, flags=re.IGNORECASE)
    return " ".join(text.lower().split())


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _is_plugin_development(text: str) -> bool:
    plugin = r"(?:plugins?|插件)"
    action = r"(?:build|create|develop|fix|update|upgrade|debug|publish|maintain|开发|创建|制作|新增|实现|修复|更新|升级|维护|调试|发布|编写|搭建|改造|优化)"
    return bool(re.search(rf"(?:{action}.{{0,20}}{plugin}|{plugin}.{{0,20}}{action})", text))


def _thread_tags(item: dict[str, Any]) -> list[dict[str, str]]:
    content = " ".join(
        _classification_text(item.get(key)) for key in ("title", "preview")
    )
    context = " ".join(
        _classification_text(item.get(key)) for key in ("cwd", "projectName")
    )
    tags: list[str] = []

    def add(key: str) -> None:
        if key not in tags:
            tags.append(key)

    if item.get("hiddenFromList"):
        add("hidden-fork")
    content_categories = (
        ("session-management", _contains_any(content, ("会话清理器", "session cleaner", "session-manager", "会话管理"))),
        ("automation", "automation:" in content or "自动化" in content),
        ("plugin-development", _is_plugin_development(content)),
        ("lark", _contains_any(content, ("飞书", "feishu", "lark", "多维表格"))),
        ("documents", _contains_any(content, ("excel", "xlsx", "spreadsheet", "表格", "word", "docx", "文档", "pdf", "ppt", "幻灯片"))),
        ("media", _contains_any(content, ("image", "图片", "图像", "视频", "video", "remotion", "海报", "视觉"))),
    )
    primary = next((key for key, matched in content_categories if matched), "")
    if not primary:
        if _contains_any(context, ("codex-session-cleaner", "session-cleaner")):
            primary = "session-management"
        elif _contains_any(context, ("codex-plugins", "/plugins/")) or re.search(
            r"(?:^|[-_/])plugins?(?:[-_/]|$)", context
        ):
            primary = "plugin-development"
        elif _contains_any(context, ("feishu", "lark")):
            primary = "lark"
        elif bool(item.get("branch")) or _contains_any(
            content, ("代码", "修复", "开发", "bug", "git", "github", "code")
        ):
            primary = "development"
        else:
            primary = "general"
    add(primary)
    return [_tag(key) for key in tags]


def _matches_search_filter(item: dict[str, Any], search: str) -> bool:
    query = search.strip().lower()
    if not query:
        return True
    values = [
        item.get("id"), item.get("title"), item.get("preview"), item.get("cwd"),
        item.get("branch"), item.get("historyBaseThreadId"),
    ]
    values.extend(tag.get("label") for tag in item.get("tags", []) if isinstance(tag, dict))
    return any(query in str(value or "").lower() for value in values)


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
        "archived": bool(thread.get("archived", archived)),
        "ephemeral": bool(thread.get("ephemeral")),
        "parentThreadId": _parent_thread_id(thread),
        "status": _status_name(thread.get("status")),
        "source": thread.get("source"),
        "projectId": thread.get("projectId"),
        "historyBaseThreadId": thread.get("historyBaseThreadId"),
        "hiddenFromList": bool(thread.get("hiddenFromList")),
        "current": str(thread.get("id") or "") == current_id,
        "protected": str(thread.get("id") or "") == current_id,
    }


def _list_one(archived: bool) -> list[dict[str, Any]]:
    """List every thread; searching happens locally so plugin-only fields stay matchable."""
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
    tag: str = "",
    now: datetime | None = None,
    *,
    history_threads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    date_start, date_end = _date_filter_bounds(
        date_preset, custom_start, custom_end, now
    )
    raw: list[tuple[dict[str, Any], bool]] = []
    if scope in ("active", "all"):
        raw.extend((item, False) for item in _list_one(False))
    if scope in ("archived", "all"):
        raw.extend((item, True) for item in _list_one(True))
    if history_threads is None:
        history_threads = _scan_history_base_threads()
    history_by_id = {str(item.get("id") or ""): item for item in history_threads}
    visible_ids = {str(item.get("id") or "") for item, _ in raw}
    for index, (thread, archived) in enumerate(raw):
        thread_id = str(thread.get("id") or "")
        history = history_by_id.get(thread_id)
        if history:
            annotated = dict(thread)
            annotated["historyBaseThreadId"] = history.get("historyBaseThreadId")
            raw[index] = (annotated, archived)
    for history in history_threads:
        thread_id = str(history.get("id") or "")
        archived = bool(history.get("archived"))
        if thread_id in visible_ids:
            continue
        if scope == "active" and archived:
            continue
        if scope == "archived" and not archived:
            continue
        raw.append((history, archived))
    children: dict[str, list[str]] = {}
    for thread, _ in raw:
        parent = _parent_thread_id(thread)
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

    blockers: dict[str, list[str]] = {}
    for history in history_threads:
        base_id = str(history.get("historyBaseThreadId") or "")
        referrer_id = str(history.get("id") or "")
        if base_id and referrer_id:
            blockers.setdefault(base_id, []).append(referrer_id)

    normalized = []
    for thread, archived in raw:
        if _is_derived_thread(thread) and not thread.get("hiddenFromList"):
            continue
        item = _normalize_thread(thread, archived, current_id)
        item["tags"] = _thread_tags(item)
        item["blockingForkIds"] = sorted(set(blockers.get(item["id"], [])))
        item["blockingForkCount"] = len(item["blockingForkIds"])
        matches_tag = not tag or any(entry["key"] == tag for entry in item["tags"])
        if (
            item["id"]
            and _matches_date_filter(item, date_start, date_end)
            and _matches_search_filter(item, search)
            and matches_tag
        ):
            item["descendantCount"] = descendants(item["id"])
            item["deletable"] = bool(current_id) and not item["current"] and not item["protected"] and not item["ephemeral"]
            normalized.append(item)
    normalized.sort(key=lambda x: _timestamp_seconds(x.get("updatedAt")) or 0, reverse=True)
    tag_counts: dict[str, int] = {}
    for item in normalized:
        for entry in item["tags"]:
            tag_counts[entry["key"]] = tag_counts.get(entry["key"], 0) + 1
    return {
        "sessions": normalized,
        "total": len(normalized),
        "truncated": False,
        "currentThreadId": current_id,
        "scope": scope,
        "search": search,
        "tagFilter": tag,
        "availableTags": [
            {"key": key, "label": TAG_LABELS[key], "count": tag_counts[key]}
            for key in TAG_LABELS
            if key in tag_counts
        ],
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
    if not current_id:
        raise ValueError("缺少当前任务上下文；为避免误操作，请重新打开 Codex 会话管理页。")
    # 复用删除路径的可管理性判断：必须是列表中的顶层会话，且不是当前会话或临时会话。
    by_id = {item["id"]: item for item in list_sessions(current_id, "all", "")["sessions"]}
    results = []
    for thread_id in ids:
        item = by_id.get(thread_id)
        if thread_id == current_id:
            results.append({"threadId": thread_id, "ok": False, "error": "当前管理会话受保护。"})
            continue
        if item is None:
            results.append(
                {"threadId": thread_id, "ok": False, "error": "会话已不存在或不是顶层会话，请刷新后重试。"}
            )
            continue
        if not item["deletable"]:
            results.append({"threadId": thread_id, "ok": False, "error": "会话不可归档。"})
            continue
        try:
            APP.request("thread/archive", {"threadId": thread_id})
            results.append({"threadId": thread_id, "ok": True})
        except Exception as exc:
            results.append({"threadId": thread_id, "ok": False, "error": str(exc)})
    return {"operation": "archive", "results": results}


def _history_reference_map(history_threads: list[dict[str, Any]]) -> dict[str, list[str]]:
    references: dict[str, list[str]] = {}
    for thread in history_threads:
        thread_id = str(thread.get("id") or "")
        base_id = str(thread.get("historyBaseThreadId") or "")
        if thread_id and base_id:
            references.setdefault(base_id, []).append(thread_id)
    return references


def _delete_order(ids: list[str], history_threads: list[dict[str, Any]]) -> list[str]:
    """Order selected history referrers before the source threads they depend on."""
    selected = set(ids)
    edges: dict[str, set[str]] = {thread_id: set() for thread_id in ids}
    indegree: dict[str, int] = {thread_id: 0 for thread_id in ids}
    for thread in history_threads:
        referrer = str(thread.get("id") or "")
        base = str(thread.get("historyBaseThreadId") or "")
        if referrer in selected and base in selected and base not in edges[referrer]:
            edges[referrer].add(base)
            indegree[base] += 1
    pending = [thread_id for thread_id in ids if indegree[thread_id] == 0]
    ordered: list[str] = []
    while pending:
        thread_id = pending.pop(0)
        ordered.append(thread_id)
        for dependent in edges[thread_id]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                pending.append(dependent)
    return ordered + [thread_id for thread_id in ids if thread_id not in ordered]


def _describe_blockers(
    blocker_ids: list[str], by_id: dict[str, dict[str, Any]], current_id: str | None
) -> str:
    descriptions = []
    for blocker_id in blocker_ids[:3]:
        blocker = by_id.get(blocker_id, {})
        title = str(blocker.get("title") or "隐藏分叉")
        marker = "（当前会话）" if blocker_id == current_id else ""
        descriptions.append(f"{title} [{blocker_id}]{marker}")
    suffix = "；另有更多阻塞分叉" if len(blocker_ids) > 3 else ""
    return "、".join(descriptions) + suffix


def delete_sessions(ids: list[str], confirmation: str, current_id: str | None) -> dict[str, Any]:
    if confirmation not in ("删除", "delete"):
        raise ValueError("确认词不正确，中文界面请输入“删除”，英文界面请输入“delete”。")
    if not current_id:
        raise ValueError("缺少当前会话元数据；为避免误删，已拒绝操作。请从 Codex 会话管理页执行。")
    if current_id in ids:
        raise ValueError("选中项包含当前管理会话，已拒绝整批删除。")
    history_threads = _scan_history_base_threads()
    sessions = list_sessions(current_id, "all", "", history_threads=history_threads)["sessions"]
    by_id = {item["id"]: item for item in sessions}
    unavailable = [thread_id for thread_id in ids if thread_id not in by_id]
    unsafe = [thread_id for thread_id in ids if thread_id in by_id and not by_id[thread_id]["deletable"]]
    if unavailable:
        raise ValueError("部分会话已不存在或不是顶层会话，请刷新后重试：" + ", ".join(unavailable))
    if unsafe:
        raise ValueError("部分会话不可删除：" + ", ".join(unsafe))
    references = _history_reference_map(history_threads)
    selected = set(ids)
    unselected_blockers = {
        thread_id: sorted(referrer for referrer in references.get(thread_id, []) if referrer not in selected)
        for thread_id in ids
    }
    selected_referrers = {
        thread_id: sorted(referrer for referrer in references.get(thread_id, []) if referrer in selected)
        for thread_id in ids
    }
    results = []
    deleted_ids: list[str] = []
    deleted: set[str] = set()
    operation_order = _delete_order(ids, history_threads)
    for thread_id in operation_order:
        blocking = unselected_blockers[thread_id]
        reason = "仍被分叉历史引用，请先选择并删除阻塞分叉："
        if not blocking:
            # 同批选中的引用分叉必须先删除成功，否则源会话会留下悬空引用。
            blocking = [referrer for referrer in selected_referrers[thread_id] if referrer not in deleted]
            reason = "同批选中的引用分叉未删除成功，已阻止删除源会话："
        if blocking:
            results.append(
                {
                    "threadId": thread_id,
                    "ok": False,
                    "error": reason + _describe_blockers(blocking, by_id, current_id),
                    "blockingThreadIds": blocking,
                }
            )
            continue
        try:
            APP.request("thread/delete", {"threadId": thread_id})
            deleted_ids.append(thread_id)
            deleted.add(thread_id)
            results.append({"threadId": thread_id, "ok": True})
        except Exception as exc:
            results.append({"threadId": thread_id, "ok": False, "error": str(exc)})
    cwd_by_id = {thread_id: str(by_id[thread_id].get("cwd") or "") for thread_id in deleted_ids}
    sidebar_sync = sync_desktop_sidebar(deleted_ids, cwd_by_id)
    return {
        "operation": "delete",
        "operationOrder": operation_order,
        "results": results,
        "sidebarSync": sidebar_sync,
    }


# 逐字段渲染的宿主上，字段太多会让用户按很多次回车，超过就改用整体确认。
ELICIT_FIELD_LIMIT = 8
# 序号输入只占一个字段，候选分页列出，避免一次塞进过长的提示语。
PICK_PAGE_SIZE = 10
# 只为挡住异常宿主造成的死循环，正常翻页远达不到这个次数。
PICK_MAX_ROUNDS = 500


def _should_elicit() -> bool:
    """仅在宿主能弹表单、但渲染不了管理页组件时接管确认（即 Codex CLI）。"""
    return _host_renders_ui() is False and _host_supports_elicitation()


def _elicit_target_labels(thread_id: str, item: dict[str, Any]) -> tuple[str, str]:
    """与管理页卡片保持同样的判断依据：影响删除范围的信息都要能看到。"""
    title = str(item.get("title") or "未命名会话")
    # 删除确认要能分辨同名项目，所以用完整路径而不是目录名。
    parts = [str(item.get("cwd") or "未知项目路径"), _format_timestamp(item.get("updatedAt"))]
    if item.get("hiddenFromList"):
        parts.append("隐藏分叉")
    if item.get("archived"):
        parts.append("已归档")
    if item.get("ephemeral"):
        parts.append("临时")
    if item.get("descendantCount"):
        parts.append(f"连带 {item['descendantCount']} 个派生会话")
    if item.get("blockingForkCount"):
        parts.append(f"被 {item['blockingForkCount']} 个分叉引用")
    parts.append(thread_id[:8])
    return title, " · ".join(parts)


def _confirm_delete_targets(
    ids: list[str], by_id: dict[str, dict[str, Any]]
) -> tuple[list[str], str | None]:
    """Let the user pick the final delete list on hosts that render no component.

    Returns (confirmed ids, refusal reason). The reason is set whenever the
    deletion must not proceed, so a failed or declined prompt never falls
    through to deleting everything the model proposed.
    """
    if not _should_elicit():
        return ids, None

    if len(ids) <= ELICIT_FIELD_LIMIT:
        properties: dict[str, Any] = {}
        for thread_id in ids:
            title, detail = _elicit_target_labels(thread_id, by_id.get(thread_id, {}))
            properties[thread_id] = {
                "type": "boolean",
                "title": title,
                "description": detail,
                "default": False,
            }
        params = {
            "message": f"即将永久删除以下 {len(ids)} 个会话，请把要删除的选为 True（不可撤销，项目文件不受影响）。",
            "requestedSchema": {"type": "object", "properties": properties},
        }
    else:
        preview = "；".join(
            _elicit_target_labels(thread_id, by_id.get(thread_id, {}))[0] for thread_id in ids[:5]
        )
        params = {
            "message": f"即将永久删除 {len(ids)} 个会话，例如：{preview}……",
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "confirm": {
                        "type": "string",
                        "title": f"确认永久删除这 {len(ids)} 个会话？",
                        "description": "此操作不可撤销；如需逐个挑选，请先缩小选择范围。",
                        "enum": ["cancel", "delete_all"],
                        "enumNames": ["取消", f"确认删除全部 {len(ids)} 个"],
                    }
                },
                "required": ["confirm"],
            },
        }

    try:
        result = HOST.request("elicitation/create", params, timeout=120.0)
    except Exception as exc:
        return [], f"未能向你确认删除名单（{exc}），已取消删除。"

    result = result if isinstance(result, dict) else {}
    if str(result.get("action") or "") != "accept":
        return [], "你已取消删除。"
    content = result.get("content")
    content = content if isinstance(content, dict) else {}

    if len(ids) <= ELICIT_FIELD_LIMIT:
        confirmed = [thread_id for thread_id in ids if content.get(thread_id) is True]
        if not confirmed:
            return [], "没有勾选任何会话，已取消删除。"
        return confirmed, None

    if content.get("confirm") != "delete_all":
        return [], "你已取消删除。"
    return ids, None


DATE_PRESET_LABELS = {
    "all": "不限",
    "within_1_day": "1 天内",
    "within_1_week": "1 周内",
    "within_1_month": "1 个月内",
    "older_than_1_week": "1 周前（更早）",
    "older_than_1_month": "1 个月前（更早）",
    "older_than_3_months": "3 个月前（更早）",
}


def _elicit_filter(available_tags: list[dict[str, Any]], total: int) -> dict[str, str] | None:
    """第一段：先把上百个会话收窄到能逐条勾选的规模。"""
    tag_keys = [""] + [str(tag.get("key")) for tag in available_tags]
    tag_names = ["不限"] + [
        f"{tag.get('label')}（{tag.get('count')}）" for tag in available_tags
    ]
    params = {
        "message": f"共 {total} 个可管理会话。请先选择筛选条件，随后从结果中勾选要处理的会话。",
        "requestedSchema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "title": "会话范围",
                    "enum": ["all", "active", "archived"],
                    "enumNames": ["全部", "当前", "已归档"],
                    "default": "all",
                },
                "datePreset": {
                    "type": "string",
                    "title": "最后更新时间",
                    "enum": list(DATE_PRESET_LABELS),
                    "enumNames": list(DATE_PRESET_LABELS.values()),
                    "default": "all",
                },
                "tag": {
                    "type": "string",
                    "title": "类别标签",
                    "enum": tag_keys,
                    "enumNames": tag_names,
                    "default": "",
                },
            },
        },
    }
    try:
        result = HOST.request("elicitation/create", params, timeout=120.0)
    except Exception:
        return None
    result = result if isinstance(result, dict) else {}
    if str(result.get("action") or "") != "accept":
        return None
    content = result.get("content")
    content = content if isinstance(content, dict) else {}
    scope = str(content.get("scope") or "all")
    date_preset = str(content.get("datePreset") or "all")
    tag = str(content.get("tag") or "")
    return {
        "scope": scope if scope in ("all", "active", "archived") else "all",
        "datePreset": date_preset if date_preset in DATE_PRESET_LABELS else "all",
        "tag": tag,
    }


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
            raise ValueError(f"无法识别的序号“{chunk}”，请输入如 1,3,5-7 的形式。")
        for number in numbers:
            if not 1 <= number <= len(sessions):
                raise ValueError(f"序号 {number} 超出范围 1-{len(sessions)}。")
            thread_id = sessions[number - 1]["id"]
            if thread_id not in picked:
                picked.append(thread_id)
    return picked


CLEAR_WORDS = {"clear", "reset", "清空", "重选"}
SELECT_ALL_WORDS = {"all", "全部", "*"}


def _pick_row(index: int, item: dict[str, Any], chosen: bool) -> str:
    title, detail = _elicit_target_labels(item["id"], item)
    if item.get("current"):
        mark, suffix = "⊘", "（当前会话，受保护）"
    elif item.get("ephemeral"):
        mark, suffix = "⊘", "（临时会话，不可操作）"
    elif not item.get("deletable"):
        mark, suffix = "⊘", "（不可操作）"
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
    header = f"筛选到 {len(sessions)} 个会话"
    if pages > 1:
        header += f"（第 {page + 1}/{pages} 页，显示第 {first + 1}-{first + len(window)} 个）"
    lines.append(header + "：")
    for offset, item in enumerate(window, start=first + 1):
        lines.append(_pick_row(offset, item, item["id"] in chosen))
    lines.append("")
    if selected:
        numbers = [
            str(index)
            for index, item in enumerate(sessions, start=1)
            if item["id"] in chosen
        ]
        lines.append(f"已选 {len(selected)} 个：{', '.join(numbers)}")
    lines.append(
        "输入序号可累加选择（如 1,3,5-7；all 选全部，clear 清空）；"
        + ("选择下一页/上一页可继续浏览，" if pages > 1 else "")
        + "选择“完成选择”提交。"
    )
    return "\n".join(lines)


def _elicit_pick(sessions: list[dict[str, Any]]) -> tuple[list[str] | None, str | None]:
    """分页列出候选，序号跨页累加，翻页与提交由选项字段控制。"""
    pages = max(1, -(-len(sessions) // PICK_PAGE_SIZE))
    page = 0
    warning = ""
    selected: list[str] = []
    for _ in range(PICK_MAX_ROUNDS):
        properties: dict[str, Any] = {
            "selection": {
                "type": "string",
                "title": f"要处理的序号（1-{len(sessions)}）",
                "description": "多个用逗号分隔，可用区间；留空表示不新增选择。",
            }
        }
        if pages > 1:
            properties["page"] = {
                "type": "string",
                "title": "翻页 / 提交",
                "enum": ["done", "next", "prev"],
                "enumNames": ["完成选择", "下一页", "上一页"],
                "default": "done",
            }
        params = {
            "message": _pick_message(sessions, page, pages, selected, warning),
            "requestedSchema": {"type": "object", "properties": properties},
        }
        try:
            result = HOST.request("elicitation/create", params, timeout=120.0)
        except Exception:
            return None, None
        result = result if isinstance(result, dict) else {}
        if str(result.get("action") or "") != "accept":
            return None, None
        content = result.get("content")
        content = content if isinstance(content, dict) else {}

        warning = ""
        written = str(content.get("selection") or "").strip()
        if written.lower() in CLEAR_WORDS:
            selected = []
            written = ""
        elif written:
            try:
                picked = _parse_selection(written, sessions)
            except ValueError as exc:
                warning = f"{exc}请重新输入。"
                continue
            by_id = {item["id"]: item for item in sessions}
            if written.lower() in SELECT_ALL_WORDS:
                # “全选”指全选可操作的，不该因为列表里混有受保护会话而报错。
                picked = [
                    thread_id for thread_id in picked if by_id[thread_id].get("deletable")
                ]
            blocked = [
                index
                for index, item in enumerate(sessions, start=1)
                if item["id"] in picked and not item.get("deletable")
            ]
            if blocked:
                labels = "、".join(str(number) for number in blocked[:3])
                warning = f"第 {labels} 项是当前会话或受保护会话，不能操作，请重新输入。"
                continue
            for thread_id in picked:
                if thread_id not in selected and thread_id in by_id:
                    selected.append(thread_id)

        move = str(content.get("page") or "done")
        if move == "next":
            warning = "已经是最后一页。" if page >= pages - 1 else ""
            page = min(page + 1, pages - 1)
            continue
        if move == "prev":
            warning = "已经是第一页。" if page == 0 else ""
            page = max(page - 1, 0)
            continue
        return selected, None
    return None, "多次输入未能确定选择，请重新打开会话管理页。"


def _elicit_action(count: int) -> str:
    """第三段：让用户直接选操作，默认取消。"""
    params = {
        "message": f"已选择 {count} 个会话，请选择要执行的操作。",
        "requestedSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "title": "操作",
                    "enum": ["cancel", "archive", "delete"],
                    "enumNames": [
                        "取消，不做任何操作",
                        f"归档这 {count} 个会话",
                        f"永久删除这 {count} 个会话（不可撤销）",
                    ],
                    "default": "cancel",
                }
            },
            "required": ["action"],
        },
    }
    try:
        result = HOST.request("elicitation/create", params, timeout=120.0)
    except Exception:
        return "cancel"
    result = result if isinstance(result, dict) else {}
    if str(result.get("action") or "") != "accept":
        return "cancel"
    content = result.get("content")
    content = content if isinstance(content, dict) else {}
    choice = str(content.get("action") or "cancel")
    return choice if choice in ("cancel", "archive", "delete") else "cancel"


def _run_picked_action(
    action: str, picked: list[str], current_id: str | None, outcome: dict[str, Any]
) -> None:
    if action == "cancel":
        outcome["performed"] = "none"
        outcome["note"] = f"已选择 {len(picked)} 个会话，但你选择了取消，未做任何改动。"
        return
    try:
        if action == "archive":
            result = archive_sessions(picked, current_id)
        else:
            # 会话与操作都已由用户在表单中敲定，这里不再重复确认。
            result = delete_sessions(picked, "删除", current_id)
    except Exception as exc:
        outcome["performed"] = "failed"
        outcome["note"] = f"执行失败：{exc}"
        return
    outcome["performed"] = action
    outcome["results"] = result.get("results") or []
    if action == "delete":
        outcome["sidebarSync"] = result.get("sidebarSync")
    failed = [row for row in outcome["results"] if not row.get("ok")]
    label = "归档" if action == "archive" else "永久删除"
    outcome["note"] = (
        f"{label}完成：成功 {len(outcome['results']) - len(failed)} 个，失败 {len(failed)} 个。"
    )


def _interactive_pick(current_id: str | None, data: dict[str, Any]) -> dict[str, Any] | None:
    """CLI 类宿主上把“打开管理页”变成筛选 + 勾选两步交互。"""
    if not _should_elicit():
        return None
    sessions = data.get("sessions") or []
    chosen = _elicit_filter(data.get("availableTags") or [], len(sessions))
    if chosen is None:
        return None

    filtered = list_sessions(
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

    picked, parse_error = _elicit_pick(candidates)
    if parse_error:
        outcome["note"] = f"{parse_error} 请重新打开会话管理页再选一次。"
    elif picked is None:
        outcome["note"] = "你已取消选择，未做任何改动。"
    elif not picked:
        outcome["note"] = "没有勾选任何会话，未做任何改动。"
    else:
        outcome["selectedThreadIds"] = picked
        _run_picked_action(_elicit_action(len(picked)), picked, current_id, outcome)
    return {"data": filtered, "outcome": outcome}


def _interactive_text(data: dict[str, Any], outcome: dict[str, Any]) -> str:
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


def _tool_definitions() -> list[dict[str, Any]]:
    ui_meta = {"ui": {"resourceUri": RESOURCE_URI}, "openai/outputTemplate": RESOURCE_URI}
    return [
        {
            "name": "open_session_manager",
            "title": "打开 Codex 会话管理页",
            "description": (
                "列出本地 Codex 会话并打开管理界面。"
                "在支持交互表单的命令行客户端上会引导用户筛选、选择会话并选定归档或删除操作，"
                "该操作由用户在表单中直接确认。"
            ),
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": {"readOnlyHint": False, "destructiveHint": True},
            "_meta": ui_meta,
        },
        {
            "name": "list_sessions",
            "title": "列出 Codex 会话",
            "description": "按活动/归档状态、搜索词、类别标签和最后更新时间列出会话。",
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
                    "managerContext": {"type": "string", "description": "管理页内部上下文令牌。"},
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
                "type": "object",
                "properties": {
                    "threadIds": {"type": "array", "items": {"type": "string"}},
                    "managerContext": {"type": "string", "description": "管理页内部上下文令牌。"},
                },
                "required": ["threadIds"],
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
                    "confirmation": {"type": "string", "enum": ["删除", "delete"]},
                    "managerContext": {"type": "string", "description": "管理页内部上下文令牌。"},
                },
                "required": ["threadIds", "confirmation"],
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": True},
        },
    ]


def _record_host(params: Any) -> None:
    if not isinstance(params, dict):
        return
    _HOST["clientInfo"] = params.get("clientInfo")
    _HOST["capabilities"] = params.get("capabilities")
    _HOST["protocolVersion"] = params.get("protocolVersion")


def _host_renders_ui() -> bool | None:
    """True/False 表示宿主是否声明了组件渲染能力，None 表示还无从判断。

    未知时一律按“支持”处理，避免把新版宿主错误降级成纯文本。
    """
    capabilities = _HOST.get("capabilities")
    if not isinstance(capabilities, dict):
        return None
    for key in ("ui", "components", "outputTemplates", "apps"):
        if key in capabilities:
            return True
    experimental = capabilities.get("experimental")
    if isinstance(experimental, dict):
        for key in experimental:
            lowered = str(key).lower()
            if "ui" in lowered or "app" in lowered or "template" in lowered:
                return True
    return False


def _tui_hint() -> str:
    if _host_renders_ui() is not False:
        return ""
    if _host_supports_elicitation():
        return "\n\n如需勾选式批量操作，可再次调用 open_session_manager 进入交互式筛选与勾选。"
    return (
        "\n\n当前客户端既不能渲染管理页组件，也不支持交互表单。"
        "需要勾选式批量操作时，可在终端运行插件目录下的 scripts/launch_tui.sh。"
    )


def _session_line(index: int, item: dict[str, Any]) -> list[str]:
    flags = []
    if item.get("current"):
        flags.append("当前会话")
    if item.get("archived"):
        flags.append("已归档")
    if item.get("ephemeral"):
        flags.append("临时")
    if not item.get("deletable"):
        flags.append("不可删除")
    if item.get("blockingForkCount"):
        flags.append(f"{item['blockingForkCount']} 个引用分叉阻塞删除")
    tags = "/".join(str(tag.get("label")) for tag in item.get("tags") or [])
    head = f"{index}. [{tags}] {item.get('projectName') or '未知项目'} · {item.get('title') or ''}"
    if flags:
        head += f" （{'、'.join(flags)}）"
    detail = f"   ID {item.get('id')} · 更新于 {_format_timestamp(item.get('updatedAt'))}"
    if item.get("cwd"):
        detail += f" · {item['cwd']}"
    return [head, detail]


def _format_timestamp(value: Any) -> str:
    seconds = _timestamp_seconds(value)
    if seconds is None:
        return "时间未知"
    return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M")


def _sessions_text(data: dict[str, Any], limit: int = 30) -> str:
    """Readable listing for clients that cannot render the manager component."""
    sessions = data.get("sessions") or []
    total = data.get("total", len(sessions))
    if not sessions:
        return "没有符合条件的 Codex 会话。" + _tui_hint()
    shown = sessions[:limit]
    lines = [f"共 {total} 个可管理 Codex 会话" + (f"，以下为前 {len(shown)} 个：" if total > len(shown) else "：")]
    for index, item in enumerate(shown, start=1):
        lines.extend(_session_line(index, item))
    if total > len(shown):
        lines.append(f"…… 另有 {total - len(shown)} 个会话未列出，可用 search、tag 或 datePreset 参数缩小范围。")
    available = data.get("availableTags") or []
    if available:
        lines.append("可用标签：" + "、".join(f"{tag['label']}({tag['count']})" for tag in available))
    return "\n".join(lines) + _tui_hint()


def _operation_text(data: dict[str, Any], action: str) -> str:
    results = data.get("results") or []
    ok = [item for item in results if item.get("ok")]
    failed = [item for item in results if not item.get("ok")]
    lines = [f"{action}完成：成功 {len(ok)} 个，失败 {len(failed)} 个。"]
    for item in ok:
        lines.append(f"  ✓ {item.get('threadId')}")
    for item in failed:
        lines.append(f"  ✗ {item.get('threadId')}：{item.get('error') or '未知错误'}")
    return "\n".join(lines)


def _files_text(data: dict[str, Any], limit: int = 40) -> str:
    lines = [f"会话 {data.get('threadId')} 的文件线索（来自会话记录，可能不完整）："]
    for title, key in (("修改文件", "changedFiles"), ("命令引用文件", "referencedFiles")):
        rows = data.get(key) or []
        lines.append(f"{title} · {len(rows)}")
        if not rows:
            lines.append("  未从会话记录中发现")
        for row in rows[:limit]:
            lines.append(f"  {row.get('path')}")
        if len(rows) > limit:
            lines.append(f"  …… 另有 {len(rows) - limit} 个未列出")
    return "\n".join(lines)


def _text_result(data: dict[str, Any], summary: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": summary}],
        "structuredContent": data,
        "isError": False,
    }


def call_tool(name: str, arguments: dict[str, Any], meta: Any) -> dict[str, Any]:
    if name == "open_session_manager":
        current_id = _thread_id_from_meta(meta)
        manager_context = _create_manager_context(current_id)
        data = list_sessions(current_id, "all", "")
        locale = _locale_from_meta(meta)
        if locale:
            data["locale"] = locale
        if manager_context:
            data["managerContext"] = manager_context
        interactive = _interactive_pick(current_id, data)
        if interactive is not None:
            payload = interactive["data"]
            payload["interactive"] = interactive["outcome"]
            if manager_context:
                payload["managerContext"] = manager_context
            return _text_result(payload, _interactive_text(payload, interactive["outcome"]))
        return _text_result(data, _sessions_text(data))
    if name == "list_sessions":
        current_id, manager_context = _current_id_for_call(meta, arguments)
        scope = str(arguments.get("scope") or "all")
        if scope not in ("active", "archived", "all"):
            raise ValueError("scope 必须是 active、archived 或 all。")
        data = list_sessions(
            current_id,
            scope,
            str(arguments.get("search") or ""),
            str(arguments.get("datePreset") or "all"),
            str(arguments.get("customStart") or ""),
            str(arguments.get("customEnd") or ""),
            str(arguments.get("tag") or ""),
        )
        if manager_context:
            data["managerContext"] = manager_context
        return _text_result(data, _sessions_text(data))
    if name == "inspect_session_files":
        thread_id = str(arguments.get("threadId") or "").strip()
        if not thread_id:
            raise ValueError("threadId 不能为空。")
        data = inspect_files(thread_id)
        return _text_result(data, _files_text(data))
    if name == "archive_sessions":
        current_id, _ = _current_id_for_call(meta, arguments, require=True)
        data = archive_sessions(_validate_ids(arguments.get("threadIds")), current_id)
        return _text_result(data, _operation_text(data, "归档"))
    if name == "delete_sessions":
        current_id, _ = _current_id_for_call(meta, arguments, require=True)
        requested = _validate_ids(arguments.get("threadIds"))
        confirmed, refusal = _confirm_delete_targets(
            requested,
            {item["id"]: item for item in list_sessions(current_id, "all", "")["sessions"]},
        )
        if refusal:
            return _text_result(
                {"operation": "delete", "results": [], "cancelled": True, "requestedThreadIds": requested},
                refusal,
            )
        data = delete_sessions(
            confirmed,
            str(arguments.get("confirmation") or ""),
            current_id,
        )
        if confirmed != requested:
            data["requestedThreadIds"] = requested
        summary = _operation_text(data, "永久删除")
        if not data["sidebarSync"]["ok"]:
            summary += "\n侧边栏同步未完全成功；请刷新或重启 Codex。"
        return _text_result(data, summary)
    raise ValueError(f"未知工具：{name}")


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    if request_id is None:
        return None
    try:
        if method == "initialize":
            _record_host(request.get("params"))
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


def _dispatch(payload: Any) -> None:
    try:
        request = json.loads(payload) if isinstance(payload, str) else payload
        response = handle(request)
    except Exception as exc:
        response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
    if response is not None:
        _write_message(response)


def main() -> None:
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        _dispatch(line)
        # 处理 elicitation 等待期间宿主插进来的消息。
        while HOST.deferred:
            _dispatch(HOST.deferred.pop(0))


if __name__ == "__main__":
    main()
