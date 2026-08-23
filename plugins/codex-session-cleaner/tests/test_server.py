import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER_DIR))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


core = _load("core", SERVER_DIR / "core.py")

server = _load("session_cleaner_server", SERVER_DIR / "server.py")


class FakeApp:
    def __init__(self, active=None, archived=None, thread=None, failing_deletes=()):
        self.active = active or []
        self.archived = archived or []
        self.thread = thread or {}
        self.failing_deletes = set(failing_deletes)
        self.calls = []

    def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/list":
            data = self.archived if params.get("archived") else self.active
            # 真实 app-server 只认识自己存储的字段，不知道插件本地生成的类别标签。
            term = params.get("searchTerm")
            if term:
                data = [
                    row for row in data
                    if any(term.lower() in str(row.get(key) or "").lower()
                           for key in ("id", "name", "preview", "cwd"))
                ]
            return {"data": data, "nextCursor": None}
        if method == "thread/read":
            return {"thread": self.thread}
        if method == "thread/delete":
            if params["threadId"] in self.failing_deletes:
                raise RuntimeError("thread/delete 失败：会话正忙")
            return {}
        if method == "thread/archive":
            return {}
        raise AssertionError(method)


class SessionCleanerTests(unittest.TestCase):
    def setUp(self):
        self.original_app = core.APP
        self.original_sync = core.sync_desktop_sidebar
        self.original_history_scan = core._scan_history_base_threads
        core._MANAGER_CONTEXTS.clear()
        core._HOST.clear()
        self.original_notify = core._notify_desktop_sidebar
        core._notify_desktop_sidebar = lambda ids, cwd_by_id: {
            "available": False, "notifiedThreadIds": [], "error": None
        }
        core._scan_history_base_threads = lambda: []
        core.sync_desktop_sidebar = lambda ids, cwd_by_id: {
            "ok": True,
            "catalog": {"removedThreadIds": list(ids), "error": None},
            "notification": {"notifiedThreadIds": list(ids), "error": None},
            "warnings": [],
        }

    def tearDown(self):
        core.APP = self.original_app
        core.sync_desktop_sidebar = self.original_sync
        core._scan_history_base_threads = self.original_history_scan
        core._MANAGER_CONTEXTS.clear()
        core._HOST.clear()
        core._notify_desktop_sidebar = self.original_notify

    def test_extracts_thread_id_from_supported_metadata(self):
        self.assertEqual(core._thread_id_from_meta({"openai/threadId": "abc"}), "abc")
        encoded = json.dumps({"thread_id": "nested"})
        self.assertEqual(core._thread_id_from_meta({"x-codex-turn-metadata": encoded}), "nested")

    def test_extracts_ui_locale_from_supported_metadata(self):
        self.assertEqual(core._locale_from_meta({"openai/locale": "zh-CN"}), "zh")
        encoded = json.dumps({"locale": "en-US"})
        self.assertEqual(core._locale_from_meta({"x-codex-turn-metadata": encoded}), "en")

    def test_list_only_returns_roots_and_counts_descendants(self):
        core.APP = FakeApp(active=[
            {"id": "root", "name": "Root", "cwd": "/tmp/project", "parentThreadId": None, "updatedAt": 3},
            {"id": "child", "name": "Child", "cwd": "/tmp/project", "parentThreadId": "root", "updatedAt": 2},
            {"id": "grandchild", "name": "Grand", "cwd": "/tmp/project", "parentThreadId": "child", "updatedAt": 1},
        ])
        data = core.list_sessions("manager", "active")
        self.assertEqual([row["id"] for row in data["sessions"]], ["root"])
        self.assertEqual(data["sessions"][0]["descendantCount"], 2)
        self.assertTrue(data["sessions"][0]["deletable"])

    def test_list_hides_all_subagent_source_shapes(self):
        core.APP = FakeApp(active=[
            {"id": "root", "name": "Root", "source": "vscode", "updatedAt": 4},
            {
                "id": "spawned-child",
                "name": "Spawned child",
                "source": {"subAgent": {"threadSpawn": {"parentThreadId": "root"}}},
                "updatedAt": 3,
            },
            {
                "id": "guardian",
                "name": "Guardian",
                "source": json.dumps({"subagent": {"other": "guardian"}}),
                "updatedAt": 2,
            },
            {"id": "legacy-child", "name": "Legacy child", "source": "cli", "threadSource": "subagent", "updatedAt": 1},
        ])
        data = core.list_sessions("manager", "active")
        self.assertEqual([row["id"] for row in data["sessions"]], ["root"])
        self.assertEqual(data["sessions"][0]["descendantCount"], 1)

    def test_current_thread_is_not_deletable(self):
        core.APP = FakeApp(active=[
            {"id": "manager", "name": "Manager", "cwd": "/tmp/project", "parentThreadId": None},
        ])
        row = core.list_sessions("manager", "active")["sessions"][0]
        self.assertTrue(row["current"])
        self.assertFalse(row["deletable"])

    def test_sessions_are_not_selectable_without_current_context(self):
        core.APP = FakeApp(active=[
            {"id": "victim", "name": "Victim", "cwd": "/tmp/project", "source": "vscode"},
        ])
        row = core.list_sessions(None, "active")["sessions"][0]
        self.assertFalse(row["deletable"])

    def test_previous_manager_thread_can_be_deleted_from_new_manager_thread(self):
        core.APP = FakeApp(active=[
            {"id": "old-manager", "name": "Old manager", "cwd": "/tmp", "parentThreadId": None},
            {"id": "new-manager", "name": "New manager", "cwd": "/tmp", "parentThreadId": None},
        ])
        core.list_sessions("old-manager", "all")

        rows = core.list_sessions("new-manager", "all")["sessions"]
        old_manager = next(row for row in rows if row["id"] == "old-manager")
        self.assertTrue(old_manager["deletable"])

        data = core.delete_sessions(["old-manager"], "删除", "new-manager")
        self.assertTrue(data["results"][0]["ok"])
        self.assertIn(("thread/delete", {"threadId": "old-manager"}), core.APP.calls)

    def test_manager_context_survives_ui_calls_without_host_metadata(self):
        core.APP = FakeApp(active=[
            {"id": "old-manager", "name": "Old manager", "cwd": "/tmp", "source": "vscode"},
            {"id": "new-manager", "name": "New manager", "cwd": "/tmp", "source": "vscode"},
        ])
        opened = server.call_tool(
            "open_session_manager", {}, {"openai/threadId": "new-manager"}
        )["structuredContent"]
        context = opened["managerContext"]

        refreshed = server.call_tool(
            "list_sessions", {"scope": "all", "managerContext": context}, None
        )["structuredContent"]
        self.assertEqual(refreshed["currentThreadId"], "new-manager")
        self.assertEqual(refreshed["managerContext"], context)
        old_manager = next(row for row in refreshed["sessions"] if row["id"] == "old-manager")
        self.assertTrue(old_manager["deletable"])

        deleted = server.call_tool(
            "delete_sessions",
            {
                "threadIds": ["old-manager"],
                "confirmation": "删除",
                "managerContext": context,
            },
            None,
        )["structuredContent"]
        self.assertTrue(deleted["results"][0]["ok"])
        self.assertIn(("thread/delete", {"threadId": "old-manager"}), core.APP.calls)

    def test_open_manager_passes_host_locale_to_ui(self):
        core.APP = FakeApp(active=[])
        opened = server.call_tool(
            "open_session_manager", {}, {"openai/threadId": "manager", "openai/locale": "en-US"}
        )["structuredContent"]
        self.assertEqual(opened["locale"], "en")

    def test_invalid_manager_context_is_rejected_before_deletion(self):
        core.APP = FakeApp(active=[{"id": "victim", "name": "Victim", "source": "vscode"}])
        with self.assertRaisesRegex(ValueError, "上下文已失效"):
            server.call_tool(
                "delete_sessions",
                {
                    "threadIds": ["victim"],
                    "confirmation": "删除",
                    "managerContext": "expired",
                },
                None,
            )
        self.assertFalse(any(method == "thread/delete" for method, _ in core.APP.calls))

    def test_session_listing_is_not_silently_truncated(self):
        active = [
            {"id": f"root-{index}", "name": f"Root {index}", "cwd": "/tmp/project", "parentThreadId": None, "updatedAt": index}
            for index in range(550)
        ]
        core.APP = FakeApp(active=active)
        data = core.list_sessions("manager", "active")
        self.assertEqual(data["total"], 550)
        self.assertEqual(len(data["sessions"]), 550)
        self.assertFalse(data["truncated"])

    def test_date_presets_filter_by_last_updated_time(self):
        now = datetime(2026, 8, 22, 12, 0, 0)
        core.APP = FakeApp(active=[
            {"id": "old", "name": "Old", "updatedAt": (now - timedelta(days=100)).timestamp()},
            {"id": "month", "name": "Month", "updatedAt": (now - timedelta(days=40)).timestamp()},
            {"id": "recent", "name": "Recent", "updatedAt": (now - timedelta(days=3)).timestamp()},
        ])
        three_months = core.list_sessions(
            "manager", "active", date_preset="older_than_3_months", now=now
        )
        one_month = core.list_sessions(
            "manager", "active", date_preset="older_than_1_month", now=now
        )
        one_week = core.list_sessions(
            "manager", "active", date_preset="older_than_1_week", now=now
        )
        self.assertEqual([row["id"] for row in three_months["sessions"]], ["old"])
        self.assertEqual({row["id"] for row in one_month["sessions"]}, {"old", "month"})
        self.assertEqual({row["id"] for row in one_week["sessions"]}, {"old", "month"})

    def test_recent_date_presets_filter_by_last_updated_time(self):
        now = datetime(2026, 8, 22, 12, 0, 0)
        core.APP = FakeApp(active=[
            {"id": "hours", "name": "Hours", "updatedAt": (now - timedelta(hours=12)).timestamp()},
            {"id": "days", "name": "Days", "updatedAt": (now - timedelta(days=3)).timestamp()},
            {"id": "weeks", "name": "Weeks", "updatedAt": (now - timedelta(days=20)).timestamp()},
            {"id": "months", "name": "Months", "updatedAt": (now - timedelta(days=50)).timestamp()},
        ])
        one_day = core.list_sessions("manager", "active", date_preset="within_1_day", now=now)
        one_week = core.list_sessions("manager", "active", date_preset="within_1_week", now=now)
        one_month = core.list_sessions("manager", "active", date_preset="within_1_month", now=now)
        self.assertEqual([row["id"] for row in one_day["sessions"]], ["hours"])
        self.assertEqual({row["id"] for row in one_week["sessions"]}, {"hours", "days"})
        self.assertEqual({row["id"] for row in one_month["sessions"]}, {"hours", "days", "weeks"})

    def test_hidden_history_fork_is_listed_searchable_and_marks_source(self):
        core.APP = FakeApp(active=[
            {"id": "source", "name": "Source", "cwd": "/tmp/project", "updatedAt": 2},
        ])
        core._scan_history_base_threads = lambda: [{
            "id": "hidden-fork",
            "name": "Source (2)",
            "cwd": "/tmp/project",
            "source": "vscode",
            "updatedAt": 3,
            "archived": False,
            "historyBaseThreadId": "source",
            "hiddenFromList": True,
        }]
        data = core.list_sessions("manager", "all")
        by_id = {row["id"]: row for row in data["sessions"]}
        self.assertEqual(set(by_id), {"source", "hidden-fork"})
        self.assertTrue(by_id["hidden-fork"]["hiddenFromList"])
        self.assertIn("hidden-fork", {tag["key"] for tag in by_id["hidden-fork"]["tags"]})
        self.assertEqual(by_id["source"]["blockingForkIds"], ["hidden-fork"])
        searched = core.list_sessions("manager", "all", search="hidden-fork")
        self.assertEqual([row["id"] for row in searched["sessions"]], ["hidden-fork"])

    def test_tag_filter_uses_generated_category_labels(self):
        core.APP = FakeApp(active=[
            {"id": "plugin", "name": "修复插件", "cwd": "/tmp/codex-plugins", "updatedAt": 2},
            {"id": "general", "name": "普通问答", "cwd": "/tmp", "updatedAt": 1},
        ])
        data = core.list_sessions("manager", "active", tag="plugin-development")
        self.assertEqual([row["id"] for row in data["sessions"]], ["plugin"])
        self.assertIn("plugin-development", {tag["key"] for tag in data["sessions"][0]["tags"]})

    def test_plugin_invocation_links_do_not_force_plugin_development(self):
        tags = core._thread_tags({
            "title": "制作 AI 成果展示视频",
            "preview": "[@remotion](plugin://remotion@openai-curated-remote) 根据报告制作视频",
            "cwd": "/tmp/codex-feishu-demo",
            "projectName": "codex-feishu-demo",
            "source": "vscode",
        })
        self.assertEqual([tag["key"] for tag in tags], ["media"])

    def test_specific_content_beats_project_context(self):
        tags = core._thread_tags({
            "title": "制作产品演示视频",
            "preview": "生成视频并检查画面",
            "cwd": "/tmp/codex-feishu-demo",
            "projectName": "codex-feishu-demo",
            "branch": "main",
        })
        self.assertEqual([tag["key"] for tag in tags], ["media"])

    def test_generic_plugin_mentions_are_not_plugin_development(self):
        tags = core._thread_tags({
            "title": "推荐可安装插件",
            "preview": "查看有哪些插件可以使用",
            "cwd": "/tmp",
            "source": "plugin://catalog",
        })
        self.assertEqual([tag["key"] for tag in tags], ["general"])

    def test_plugin_project_context_remains_plugin_development(self):
        tags = core._thread_tags({
            "title": "继续修复相关问题",
            "cwd": "/tmp/codex-plugins",
            "projectName": "codex-plugins",
            "branch": "main",
        })
        self.assertEqual([tag["key"] for tag in tags], ["plugin-development"])

    def test_custom_date_filter_is_inclusive_for_both_dates(self):
        core.APP = FakeApp(active=[
            {"id": "inside", "name": "Inside", "updatedAt": datetime(2026, 8, 10, 23, 30).timestamp()},
            {"id": "outside", "name": "Outside", "updatedAt": datetime(2026, 8, 11, 0, 0).timestamp()},
        ])
        data = core.list_sessions(
            "manager",
            "active",
            date_preset="custom",
            custom_start="2026-08-10",
            custom_end="2026-08-10",
        )
        self.assertEqual([row["id"] for row in data["sessions"]], ["inside"])

    def test_custom_date_filter_rejects_reversed_range(self):
        core.APP = FakeApp()
        with self.assertRaisesRegex(ValueError, "开始日期不能晚于结束日期"):
            core.list_sessions(
                "manager",
                "active",
                date_preset="custom",
                custom_start="2026-08-11",
                custom_end="2026-08-10",
            )

    def test_file_inspection_separates_changes_and_references(self):
        core.APP = FakeApp(thread={
            "id": "t1", "cwd": "/work/project",
            "turns": [{"items": [
                {"type": "fileChange", "changes": [{"path": "src/a.py", "kind": "update"}]},
                {"type": "commandExecution", "cwd": "/work/project", "scriptPath": "scripts/run.sh", "commandActions": [{"path": "README.md"}]},
            ]}],
        })
        data = core.inspect_files("t1")
        self.assertEqual(data["changedFiles"][0]["path"], "/work/project/src/a.py")
        self.assertEqual({x["path"] for x in data["referencedFiles"]}, {"/work/project/scripts/run.sh", "/work/project/README.md"})

    def test_delete_requires_exact_confirmation(self):
        core.APP = FakeApp()
        with self.assertRaisesRegex(ValueError, "确认词"):
            core.delete_sessions(["victim"], "永久删除", "manager")
        self.assertFalse(any(method == "thread/delete" for method, _ in core.APP.calls))

    def test_delete_requires_current_thread_metadata(self):
        core.APP = FakeApp()
        with self.assertRaisesRegex(ValueError, "当前会话元数据"):
            core.delete_sessions(["victim"], "删除", None)
        self.assertFalse(any(method == "thread/delete" for method, _ in core.APP.calls))

    def test_delete_rejects_current_thread_as_atomic_batch(self):
        core.APP = FakeApp(active=[
            {"id": "manager", "name": "Manager", "cwd": "/tmp", "parentThreadId": None},
            {"id": "victim", "name": "Victim", "cwd": "/tmp", "parentThreadId": None},
        ])
        with self.assertRaisesRegex(ValueError, "当前管理会话"):
            core.delete_sessions(["victim", "manager"], "删除", "manager")
        self.assertFalse(any(method == "thread/delete" for method, _ in core.APP.calls))

    def test_delete_revalidates_then_uses_official_method(self):
        core.APP = FakeApp(active=[
            {"id": "victim", "name": "Victim", "cwd": "/tmp/project", "parentThreadId": None},
        ])
        data = core.delete_sessions(["victim"], "删除", "manager")
        self.assertTrue(data["results"][0]["ok"])
        self.assertTrue(data["sidebarSync"]["ok"])
        self.assertIn(("thread/delete", {"threadId": "victim"}), core.APP.calls)

    def test_delete_accepts_english_confirmation(self):
        core.APP = FakeApp(active=[
            {"id": "victim", "name": "Victim", "cwd": "/tmp/project", "parentThreadId": None},
        ])
        data = core.delete_sessions(["victim"], "delete", "manager")
        self.assertTrue(data["results"][0]["ok"])

    def test_delete_blocks_source_when_hidden_fork_is_not_selected(self):
        core.APP = FakeApp(active=[
            {"id": "source", "name": "Source", "cwd": "/tmp/project"},
        ])
        core._scan_history_base_threads = lambda: [{
            "id": "hidden-fork",
            "name": "Source (2)",
            "cwd": "/tmp/project",
            "archived": False,
            "historyBaseThreadId": "source",
            "hiddenFromList": True,
        }]
        data = core.delete_sessions(["source"], "删除", "manager")
        self.assertFalse(data["results"][0]["ok"])
        self.assertEqual(data["results"][0]["blockingThreadIds"], ["hidden-fork"])
        self.assertFalse(any(method == "thread/delete" for method, _ in core.APP.calls))

    def test_delete_orders_selected_history_fork_before_source(self):
        core.APP = FakeApp(active=[
            {"id": "source", "name": "Source", "cwd": "/tmp/project"},
        ])
        core._scan_history_base_threads = lambda: [{
            "id": "hidden-fork",
            "name": "Source (2)",
            "cwd": "/tmp/project",
            "archived": False,
            "historyBaseThreadId": "source",
            "hiddenFromList": True,
        }]
        data = core.delete_sessions(["source", "hidden-fork"], "删除", "manager")
        delete_calls = [params["threadId"] for method, params in core.APP.calls if method == "thread/delete"]
        self.assertEqual(delete_calls, ["hidden-fork", "source"])
        self.assertEqual(data["operationOrder"], ["hidden-fork", "source"])
        self.assertTrue(all(result["ok"] for result in data["results"]))

    def test_archive_rejects_targets_missing_from_the_manageable_list(self):
        core.APP = FakeApp(active=[
            {"id": "root", "name": "Root", "cwd": "/tmp/project", "source": "vscode"},
            {"id": "child", "name": "Child", "cwd": "/tmp/project", "parentThreadId": "root"},
            {"id": "temp", "name": "Temp", "cwd": "/tmp/project", "ephemeral": True},
        ])
        data = core.archive_sessions(["child", "temp", "ghost", "manager"], "manager")
        by_id = {result["threadId"]: result for result in data["results"]}
        self.assertFalse(by_id["child"]["ok"])
        self.assertFalse(by_id["temp"]["ok"])
        self.assertFalse(by_id["ghost"]["ok"])
        self.assertFalse(by_id["manager"]["ok"])
        self.assertIn("当前管理会话", by_id["manager"]["error"])
        self.assertFalse(any(method == "thread/archive" for method, _ in core.APP.calls))

    def test_archiving_notifies_the_desktop_sidebar(self):
        """只调 thread/archive 的话，桌面端侧边栏不会刷新，会话看着还在活动列表里。"""
        core.APP = FakeApp(active=[
            {"id": "root", "name": "Root", "cwd": "/tmp/project", "source": "vscode"},
            {"id": "other", "name": "Other", "cwd": "/tmp/second", "source": "vscode"},
        ])
        notified = {}
        core._notify_desktop_sidebar = lambda ids, cwd_by_id: notified.update(
            ids=list(ids), cwd=dict(cwd_by_id)
        ) or {"available": True, "notifiedThreadIds": list(ids), "error": None}
        try:
            data = core.archive_sessions(["root", "other"], "manager")
        finally:
            core._notify_desktop_sidebar = self.original_notify
        self.assertEqual(notified["ids"], ["root", "other"])
        self.assertEqual(notified["cwd"]["root"], "/tmp/project")
        self.assertTrue(data["sidebarSync"]["ok"])

    def test_a_failed_archive_is_not_announced_to_the_sidebar(self):
        core.APP = FakeApp(active=[
            {"id": "root", "name": "Root", "cwd": "/tmp/project", "source": "vscode"},
        ])
        seen = {}
        core._notify_desktop_sidebar = lambda ids, cwd_by_id: seen.update(ids=list(ids)) or {
            "available": True, "notifiedThreadIds": [], "error": None
        }
        try:
            # ghost 不在可管理列表里，归档会失败，不该被当成已归档广播出去。
            data = core.archive_sessions(["root", "ghost"], "manager")
        finally:
            core._notify_desktop_sidebar = self.original_notify
        self.assertEqual(seen["ids"], ["root"])
        self.assertFalse([r for r in data["results"] if r["threadId"] == "ghost"][0]["ok"])

    def test_a_sidebar_failure_surfaces_in_the_archive_summary(self):
        core.APP = FakeApp(active=[
            {"id": "root", "name": "Root", "cwd": "/tmp/project", "source": "vscode"},
        ])
        core._notify_desktop_sidebar = lambda ids, cwd_by_id: {
            "available": True, "notifiedThreadIds": [], "error": "Codex 桌面 IPC 已断开。"
        }
        try:
            result = server.call_tool(
                "archive_sessions", {"threadIds": ["root"]}, {"threadId": "manager"}
            )
        finally:
            core._notify_desktop_sidebar = self.original_notify
        self.assertFalse(result["structuredContent"]["sidebarSync"]["ok"])
        self.assertIn("侧边栏同步未完全成功", result["content"][0]["text"])

    def test_archive_still_accepts_top_level_threads(self):
        core.APP = FakeApp(active=[
            {"id": "root", "name": "Root", "cwd": "/tmp/project", "source": "vscode"},
        ])
        data = core.archive_sessions(["root"], "manager")
        self.assertTrue(data["results"][0]["ok"])
        self.assertIn(("thread/archive", {"threadId": "root"}), core.APP.calls)

    def test_app_server_timeout_does_not_pass_negative_select_timeout(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)

        class Stream:
            def write(self, data):
                pass

            def flush(self):
                pass

            def readline(self):
                return ""

            def fileno(self):
                return read_fd

        class Process:
            def __init__(self):
                self.stdin = Stream()
                self.stdout = Stream()

            def poll(self):
                return None

        client = core.AppServerClient(timeout=1.0)
        client.process = Process()
        # 截止时间在 while 判断之后才被越过，正是产生负超时的时序。
        ticks = iter([0.0, 0.9, 2.0, 2.1])
        last = [0.0]

        def monotonic():
            last[0] = next(ticks, last[0])
            return last[0]

        original = core.time.monotonic
        core.time.monotonic = monotonic
        try:
            with self.assertRaisesRegex(core.AppServerError, "超时"):
                client.request("thread/list", {}, ensure_started=False)
        finally:
            core.time.monotonic = original

    def test_delete_scans_history_directory_only_once(self):
        core.APP = FakeApp(active=[
            {"id": "victim", "name": "Victim", "cwd": "/tmp/project", "source": "vscode"},
        ])
        scans = []

        def counted_scan():
            scans.append(1)
            return []

        core._scan_history_base_threads = counted_scan
        data = core.delete_sessions(["victim"], "删除", "manager")
        self.assertTrue(data["results"][0]["ok"])
        self.assertEqual(len(scans), 1)

    def test_delete_keeps_source_when_selected_fork_deletion_fails(self):
        core.APP = FakeApp(
            active=[{"id": "source", "name": "Source", "cwd": "/tmp/project"}],
            failing_deletes={"hidden-fork"},
        )
        core._scan_history_base_threads = lambda: [{
            "id": "hidden-fork",
            "name": "Source (2)",
            "cwd": "/tmp/project",
            "archived": False,
            "historyBaseThreadId": "source",
            "hiddenFromList": True,
        }]
        data = core.delete_sessions(["source", "hidden-fork"], "删除", "manager")
        by_id = {result["threadId"]: result for result in data["results"]}
        self.assertFalse(by_id["hidden-fork"]["ok"])
        self.assertFalse(by_id["source"]["ok"])
        self.assertEqual(by_id["source"]["blockingThreadIds"], ["hidden-fork"])
        self.assertIn("未删除成功", by_id["source"]["error"])
        deleted = [params["threadId"] for method, params in core.APP.calls if method == "thread/delete"]
        self.assertEqual(deleted, ["hidden-fork"])

    def test_search_matches_generated_tag_labels(self):
        core.APP = FakeApp(active=[
            {"id": "plugin", "name": "修复插件", "cwd": "/tmp/codex-plugins", "updatedAt": 2},
            {"id": "general", "name": "普通问答", "cwd": "/tmp", "updatedAt": 1},
        ])
        data = core.list_sessions("manager", "active", search="插件开发")
        self.assertEqual([row["id"] for row in data["sessions"]], ["plugin"])

    def test_search_is_applied_locally_not_pushed_to_app_server(self):
        core.APP = FakeApp(active=[
            {"id": "branch", "name": "重构", "cwd": "/tmp/project", "gitInfo": {"branch": "feature/cleanup"}},
        ])
        data = core.list_sessions("manager", "active", search="feature/cleanup")
        self.assertEqual([row["id"] for row in data["sessions"]], ["branch"])
        listed = [params for method, params in core.APP.calls if method == "thread/list"]
        self.assertTrue(listed)
        self.assertFalse(any("searchTerm" in params for params in listed))

    def test_catalog_cleanup_is_exact_and_updates_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            database = codex_home / "sqlite" / "codex-dev.db"
            database.parent.mkdir()
            connection = sqlite3.connect(database)
            connection.executescript("""
                CREATE TABLE local_thread_catalog (
                    host_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    missing_candidate INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE local_thread_catalog_sync_state (
                    host_id TEXT PRIMARY KEY,
                    observation_sequence INTEGER NOT NULL
                );
                CREATE TABLE local_thread_catalog_metadata (
                    id INTEGER PRIMARY KEY,
                    catalog_revision INTEGER NOT NULL
                );
                INSERT INTO local_thread_catalog VALUES ('local', 'victim', 0);
                INSERT INTO local_thread_catalog VALUES ('local', 'keeper', 0);
                INSERT INTO local_thread_catalog_sync_state VALUES ('local', 7);
                INSERT INTO local_thread_catalog_metadata VALUES (1, 11);
            """)
            connection.commit()
            connection.close()
            previous = os.environ.get("CODEX_HOME")
            os.environ["CODEX_HOME"] = str(codex_home)
            try:
                result = core._remove_from_desktop_catalog(["victim"])
            finally:
                if previous is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = previous
            self.assertIsNone(result["error"])
            self.assertEqual(result["removedThreadIds"], ["victim"])
            connection = sqlite3.connect(database)
            self.assertEqual(
                connection.execute("SELECT thread_id FROM local_thread_catalog").fetchall(),
                [("keeper",)],
            )
            self.assertEqual(
                connection.execute("SELECT observation_sequence FROM local_thread_catalog_sync_state").fetchone()[0],
                8,
            )
            self.assertEqual(
                connection.execute("SELECT catalog_revision FROM local_thread_catalog_metadata").fetchone()[0],
                12,
            )
            connection.close()

    def test_host_handshake_is_recorded(self):
        # 取自 Codex CLI v0.149.0 的真实 initialize 握手。
        server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"elicitation": {"form": {}, "url": {}}},
                "clientInfo": {"name": "codex-mcp-client", "title": "Codex", "version": "0.149.0"},
            },
        })
        self.assertEqual(core._HOST["clientInfo"]["title"], "Codex")
        self.assertTrue(core._host_supports_elicitation())

    def test_elicitation_support_is_not_inferred_from_missing_keys(self):
        """能力只按明确声明判断，不从“没有某个键”反推客户端类型。"""
        core._HOST.clear()
        self.assertFalse(core._host_supports_elicitation())
        server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"capabilities": {"tools": {}}},
        })
        self.assertFalse(core._host_supports_elicitation())

    def test_text_fallback_lists_sessions_with_ids(self):
        core._HOST.clear()
        core.APP = FakeApp(active=[
            {"id": "thread-1", "name": "继续修复相关问题", "cwd": "/tmp/codex-plugins", "updatedAt": 1787411471},
        ])
        result = server.call_tool("list_sessions", {"scope": "all"}, {"threadId": "manager"})
        body = result["content"][0]["text"]
        self.assertIn("thread-1", body)
        self.assertIn("继续修复相关问题", body)
        self.assertIn("/tmp/codex-plugins", body)
        self.assertIn("可用标签", body)

    def test_text_fallback_caps_the_listing_and_says_how_many_remain(self):
        core._HOST.clear()
        core.APP = FakeApp(active=[
            {"id": f"thread-{index}", "name": f"会话 {index}", "cwd": "/tmp", "updatedAt": index}
            for index in range(80)
        ])
        body = server.call_tool("list_sessions", {"scope": "all"}, {"threadId": "manager"})["content"][0]["text"]
        self.assertIn("共 80 个可管理 Codex 会话", body)
        self.assertIn("另有 50 个会话未列出", body)

    def test_a_host_without_the_component_is_pointed_at_the_cli_edition(self):
        """这版只服务管理页；渲染不了组件的客户端应被引导去装命令行版。"""
        core.APP = FakeApp(active=[
            {"id": f"t-{i}", "name": f"会话 {i}", "cwd": "/tmp", "updatedAt": 100 - i}
            for i in range(40)
        ])
        server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"capabilities": {"elicitation": {"form": {}}}},
        })
        body = server.call_tool("open_session_manager", {}, {"openai/threadId": "m"})["content"][0]["text"]
        self.assertIn("codex-session-cleaner-cli", body.split("\n")[0])
        # 本版没有交互工具，绝不能引导模型去调一个并不存在的 select_sessions。
        self.assertNotIn("select_sessions", body)
        self.assertNotIn(
            "select_sessions", json.dumps(server._tool_definitions(), ensure_ascii=False)
        )
        # 指引必须在清单之前，别被上百行列表淹没。
        self.assertLess(body.index("codex-session-cleaner-cli"), body.index("1. ["))

    def test_operation_text_reports_each_failure(self):
        core._HOST.clear()
        core.APP = FakeApp(
            active=[
                {"id": "ok-1", "name": "A", "cwd": "/tmp", "source": "vscode"},
                {"id": "bad-1", "name": "B", "cwd": "/tmp", "source": "vscode"},
            ],
            failing_deletes={"bad-1"},
        )
        result = server.call_tool(
            "delete_sessions",
            {"threadIds": ["ok-1", "bad-1"], "confirmation": "删除"},
            {"threadId": "manager"},
        )
        body = result["content"][0]["text"]
        self.assertIn("成功 1 个，失败 1 个", body)
        self.assertIn("✓ ok-1", body)
        self.assertIn("✗ bad-1", body)

    def test_file_clue_text_lists_paths(self):
        core._HOST.clear()
        core.APP = FakeApp(thread={
            "id": "t1", "cwd": "/work/project",
            "turns": [{"items": [
                {"type": "fileChange", "changes": [{"path": "src/a.py", "kind": "update"}]},
            ]}],
        })
        body = server.call_tool("inspect_session_files", {"threadId": "t1"}, None)["content"][0]["text"]
        self.assertIn("/work/project/src/a.py", body)
        self.assertIn("修改文件 · 1", body)
        self.assertIn("未从会话记录中发现", body)

    def test_mcp_resource_is_self_contained_html(self):
        response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": server.RESOURCE_URI}})
        content = response["result"]["contents"][0]
        self.assertEqual(content["mimeType"], "text/html;profile=mcp-app")
        self.assertIn("Codex 会话清理器", content["text"])
        self.assertIn("tools/call", content["text"])

    def test_ui_has_selection_based_clipboard_fallback(self):
        html = server.UI_PATH.read_text(encoding="utf-8")
        self.assertIn("function copyTextWithSelection(text)", html)
        self.assertIn("document.execCommand('copy')", html)
        # clipboard API 会如实报成败，execCommand 在受限 iframe 里可能假成功，
        # 所以前者必须先试；两者都不成时要留一个手动复制的出口。
        self.assertLess(
            html.index("navigator.clipboard?.writeText"),
            html.index("if (copyTextWithSelection(text))"),
        )
        self.assertIn("function showManualCopy(text)", html)
        self.assertIn("showManualCopy(text);", html)
        self.assertIn('id="copyDialog"', html)
        self.assertIn("copySessionId", html)
        self.assertIn("copyText(session.id)", html)
        self.assertIn('value="older_than_3_months"', html)
        self.assertIn('value="within_1_day"', html)
        self.assertIn('value="within_1_week"', html)
        self.assertIn('value="within_1_month"', html)
        self.assertIn('id="tagFilter"', html)
        self.assertIn('<option value="">标签</option>', html)
        self.assertIn('<option value="all">日期</option>', html)
        self.assertIn("new Option(t().tag, '')", html)
        self.assertIn('id="customStart"', html)
        self.assertIn('id="customEnd"', html)
        self.assertIn("managerContext: ''", html)
        self.assertIn("{ ...args, managerContext: state.managerContext }", html)
        self.assertIn("failed[0].error", html)

    def test_ui_localizes_from_host_and_uses_language_specific_delete_confirmation(self):
        html = server.UI_PATH.read_text(encoding="utf-8")
        self.assertIn("localeFrom(navigator.language)", html)
        self.assertIn('id="languageSwitch"', html)
        self.assertIn('data-locale="zh"', html)
        self.assertIn('data-locale="en"', html)
        self.assertIn("localeMode: 'auto'", html)
        self.assertIn("state.localeMode === 'manual'", html)
        self.assertIn("setLocale(button.dataset.locale, 'manual')", html)
        self.assertIn("ui/notifications/host-context-changed", html)
        self.assertIn("Codex Session Cleaner", html)
        self.assertIn("confirmation: '删除'", html)
        self.assertIn("confirmation: 'delete'", html)
        self.assertIn("confirmation: t().confirmation", html)
        self.assertIn("event.target.value !== t().confirmation", html)
        self.assertIn('<button id="cancelDelete" type="button"', html)
        self.assertIn("$('cancelDelete').addEventListener('click', () => $('deleteDialog').close())", html)

    def test_ui_confines_destructive_actions_to_visible_selection(self):
        html = server.UI_PATH.read_text(encoding="utf-8")
        self.assertIn("function visibleSelection(sessions = filteredSessions())", html)
        self.assertIn("session.deletable", html)
        self.assertIn(
            "state.search = event.target.value; state.selected.clear();", html
        )
        self.assertIn("state.scope = button.dataset.scope; state.selected.clear();", html)
        self.assertIn("const targets = visibleSelection();", html)
        self.assertIn("tool('archive_sessions', { threadIds: targets })", html)
        self.assertIn(
            "tool('delete_sessions', { threadIds: targets, confirmation: t().confirmation })", html
        )
        self.assertNotIn("threadIds: [...state.selected]", html)

    def test_ui_ignores_messages_from_other_windows(self):
        html = server.UI_PATH.read_text(encoding="utf-8")
        self.assertIn("if (event.source !== window.parent) return;", html)
        self.assertLess(
            html.index("if (event.source !== window.parent) return;"),
            html.index("if (!message || message.jsonrpc !== '2.0') return;"),
        )

    def test_date_filter_options_keep_contrast_in_native_popup(self):
        html = server.UI_PATH.read_text(encoding="utf-8")
        self.assertIn(".date-select option", html)
        self.assertIn("color:#17191d", html)
        self.assertIn("background:#fff", html)

    def test_selection_actions_use_compact_wide_toolbar(self):
        html = server.UI_PATH.read_text(encoding="utf-8")
        toolbar_start = html.index('<section id="toolbar" class="toolbar"')
        summary = html.index('<div class="summary">')
        toolbar_end = html.index("</section>", toolbar_start)
        self.assertLess(toolbar_start, summary)
        self.assertLess(summary, toolbar_end)
        self.assertIn(".app { max-width:1340px", html)
        self.assertIn(".toolbar { position:sticky", html)
        self.assertIn("grid-template-columns:minmax(220px,1fr) auto 112px 124px auto", html)
        self.assertIn(".date-select,.tag-select { min-width:0; width:100%", html)
        self.assertIn(".segments button {", html)
        self.assertIn("cursor:pointer; white-space:nowrap", html)
        self.assertIn(".summary { grid-column:1 / -1", html)
        self.assertIn("padding:8px 9px", html)
        self.assertIn("padding-top:6px", html)


class DeleteToolTests(unittest.TestCase):
    """delete_sessions 不再自行弹表单：确认由管理页或 select_sessions 各自完成。"""

    def setUp(self):
        self.original_app = core.APP
        self.original_request = core.HOST.request
        self.original_sync = core.sync_desktop_sidebar
        self.original_scan = core._scan_history_base_threads
        core._scan_history_base_threads = lambda: []
        core.sync_desktop_sidebar = lambda ids, cwd_by_id: {"ok": True, "warnings": []}
        core._MANAGER_CONTEXTS.clear()
        core._HOST.clear()
        self.prompts = []
        def fake(method, params, timeout=120.0):
            self.prompts.append(params)
            return {"action": "decline"}
        core.HOST.request = fake

    def tearDown(self):
        core.APP = self.original_app
        core.HOST.request = self.original_request
        core.sync_desktop_sidebar = self.original_sync
        core._scan_history_base_threads = self.original_scan
        core._MANAGER_CONTEXTS.clear()
        core._HOST.clear()

    def host(self, capabilities):
        core.APP = FakeApp(active=[
            {"id": f"t-{i}", "name": f"会话 {i}", "cwd": f"/tmp/p{i}", "source": "vscode"}
            for i in range(3)
        ])
        server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"capabilities": capabilities},
        })

    def test_no_host_gets_an_extra_form_on_delete(self):
        """管理页已收过确认词，CLI 该走 select_sessions；这里再弹一次只会重复。"""
        for capabilities in (
            {"ui": {}, "elicitation": {"form": {}}},   # 桌面端
            {"elicitation": {"form": {}, "url": {}}},  # CLI
            {"tools": {}},                             # 两者都不支持
        ):
            self.prompts = []
            self.host(capabilities)
            result = server.call_tool(
                "delete_sessions",
                {"threadIds": ["t-0"], "confirmation": "删除"},
                {"threadId": "manager"},
            )
            self.assertEqual(self.prompts, [], capabilities)
            self.assertTrue(result["structuredContent"]["results"][0]["ok"], capabilities)

    def test_the_confirmation_word_is_still_required(self):
        self.host({"elicitation": {"form": {}}})
        with self.assertRaisesRegex(ValueError, "确认词"):
            server.call_tool(
                "delete_sessions",
                {"threadIds": ["t-0"], "confirmation": "yes"},
                {"threadId": "manager"},
            )

    def test_batch_cap_still_applies(self):
        self.host({"tools": {}})
        with self.assertRaisesRegex(ValueError, f"最多处理 {core.BATCH_LIMIT}"):
            server.call_tool(
                "delete_sessions",
                {"threadIds": [f"x-{i}" for i in range(core.BATCH_LIMIT + 1)],
                 "confirmation": "删除"},
                {"threadId": "manager"},
            )

class BusyThreadErrorTests(unittest.TestCase):
    """Codex 对会话有跨进程写锁，报错必须让用户知道该怎么办。"""

    def setUp(self):
        self.original_app = core.APP
        self.original_scan = core._scan_history_base_threads
        self.original_notify = core._notify_desktop_sidebar
        core._scan_history_base_threads = lambda: []
        core._notify_desktop_sidebar = lambda ids, cwd: {
            "available": False, "notifiedThreadIds": [], "error": None
        }

    def tearDown(self):
        core.APP = self.original_app
        core._scan_history_base_threads = self.original_scan
        core._notify_desktop_sidebar = self.original_notify

    def test_a_locked_session_explains_how_to_proceed(self):
        class LockedApp(FakeApp):
            def request(self, method, params):
                if method == "thread/archive":
                    raise RuntimeError(
                        "thread/archive 失败：thread t-0 already has an active writer"
                    )
                return super().request(method, params)

        core.APP = LockedApp(active=[
            {"id": "t-0", "name": "会话", "cwd": "/tmp", "source": "vscode"},
        ])
        error = core.archive_sessions(["t-0"], "manager")["results"][0]["error"]
        self.assertIn("正在 Codex 中打开", error)
        self.assertIn("侧边栏", error)
        # 原始英文报错保留在括号里，便于排查。
        self.assertIn("active writer", error)

    def test_unrelated_failures_are_passed_through_unchanged(self):
        detail = "no rollout found for thread id t-0"
        self.assertEqual(core._friendly_thread_error(RuntimeError(detail)), detail)


class BusySessionTests(unittest.TestCase):
    """被别的 Codex 进程持有的会话改不动，得在列表里就标出来。"""

    def setUp(self):
        self.original_app = core.APP
        self.original_scan = core._scan_history_base_threads
        self.original_busy = core._busy_thread_ids
        core._scan_history_base_threads = lambda: []
        core.APP = FakeApp(active=[
            {"id": "free", "name": "空闲会话", "cwd": "/tmp/a", "source": "vscode", "updatedAt": 9},
            {"id": "held", "name": "正在用的会话", "cwd": "/tmp/b", "source": "vscode", "updatedAt": 8},
        ])
        core._busy_thread_ids = lambda ids: {"held"} & set(ids)

    def tearDown(self):
        core.APP = self.original_app
        core._scan_history_base_threads = self.original_scan
        core._busy_thread_ids = self.original_busy

    def test_a_held_session_is_flagged_and_cannot_be_selected(self):
        rows = {r["id"]: r for r in core.list_sessions("mgr", "all", "")["sessions"]}
        self.assertTrue(rows["held"]["busy"])
        self.assertFalse(rows["held"]["deletable"])
        self.assertFalse(rows["free"]["busy"])
        self.assertTrue(rows["free"]["deletable"])

    def test_the_listing_says_why(self):
        body = core.sessions_text(core.list_sessions("mgr", "all", ""))
        self.assertIn("使用中", body)
        self.assertIn("侧边栏", body)

    def test_probing_a_stale_lock_file_does_not_flag_it(self):
        """锁文件多数是残留，光看文件在不在会误标一堆。"""
        core._busy_thread_ids = self.original_busy
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            locks = Path(tmp) / "thread-writer-locks"
            locks.mkdir()
            (locks / "stale.lock").write_bytes(b"")
            previous = os.environ.get("CODEX_HOME")
            os.environ["CODEX_HOME"] = tmp
            try:
                self.assertEqual(core._busy_thread_ids({"stale"}), set())
            finally:
                if previous is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = previous

    def test_a_genuinely_locked_file_is_detected(self):
        core._busy_thread_ids = self.original_busy
        import fcntl, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            locks = Path(tmp) / "thread-writer-locks"
            locks.mkdir()
            target = locks / "taken.lock"
            target.write_bytes(b"")
            previous = os.environ.get("CODEX_HOME")
            os.environ["CODEX_HOME"] = tmp
            handle = os.open(target, os.O_RDWR)
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                self.assertEqual(core._busy_thread_ids({"taken"}), {"taken"})
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
                os.close(handle)
                if previous is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = previous

class _StdinWithDescriptor:
    """只暴露 fileno 的 stdin 替身，让 StdinLines 走真正的 select + os.read 路径。"""

    def __init__(self, descriptor):
        self._descriptor = descriptor

    def fileno(self):
        return self._descriptor


class SharedCoreRegressionTests(unittest.TestCase):
    """真出过问题的几处；改动共享核心时这些必须仍然成立。"""

    def test_two_messages_in_one_write_are_both_delivered(self):
        """宿主常把多条消息一次写出，第二条会留在缓冲里。

        只靠 select 判断可读，就看不见这条已经读进来的消息：交互表单会一直空转到
        超时，然后谎报“宿主没响应”，而用户其实早就答完了。
        """
        read_fd, write_fd = os.pipe()
        write_open = True
        original_stdin = sys.stdin
        sys.stdin = _StdinWithDescriptor(read_fd)
        try:
            reader = core.StdinLines()
            os.write(write_fd, b'{"id": "a"}\n{"id": "b"}\n')  # 同一次写入
            self.assertEqual(reader.readline(2.0), '{"id": "a"}\n')
            # 此刻 fd 上已无新数据，第二条只存在于缓冲里，必须照样立刻拿到。
            self.assertEqual(reader.readline(2.0), '{"id": "b"}\n')
            self.assertIsNone(reader.readline(0.05))  # 没有就是没有，超时返回 None
            os.close(write_fd)
            write_open = False
            self.assertEqual(reader.readline(2.0), "")  # 关闭后才是空字符串
        finally:
            sys.stdin = original_stdin
            os.close(read_fd)
            if write_open:
                os.close(write_fd)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root 能打开任何文件")
    def test_an_unreadable_lock_file_is_not_reported_as_busy(self):
        """打不开锁文件说明不了谁持有它。

        误判成占用，这个会话在页面和终端里都会永远不可勾选，而提示里让用户去侧边栏
        释放的那个持有者根本不存在。
        """
        with tempfile.TemporaryDirectory() as tmp:
            locks = Path(tmp) / "thread-writer-locks"
            locks.mkdir()
            sealed = locks / "sealed.lock"
            sealed.write_bytes(b"")
            sealed.chmod(0o000)
            previous = os.environ.get("CODEX_HOME")
            os.environ["CODEX_HOME"] = tmp
            try:
                self.assertEqual(core._busy_thread_ids({"sealed"}), set())
            finally:
                sealed.chmod(0o644)
                if previous is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = previous

    def test_hitting_the_page_cap_is_reported_as_truncated(self):
        """翻页上限是失控保护，不是容量承诺；截断了就得说出来。"""

        class EndlessApp:
            def __init__(self):
                self.pages = 0

            def request(self, method, params):
                assert method == "thread/list", method
                self.pages += 1
                return {
                    "data": [{"id": f"t{self.pages}", "name": "T", "updatedAt": self.pages}],
                    "nextCursor": f"cursor-{self.pages}",
                }

        original_app, original_scan = core.APP, core._scan_history_base_threads
        core.APP = EndlessApp()
        core._scan_history_base_threads = lambda: []
        core._forget_history_threads()
        try:
            data = core.list_sessions("mgr", "active")
            self.assertEqual(len(data["sessions"]), core.LIST_MAX_PAGES)
            self.assertTrue(data["truncated"])
            self.assertIn("未纳入本次列表", core.sessions_text(data))
        finally:
            core.APP, core._scan_history_base_threads = original_app, original_scan
            core._forget_history_threads()

    def test_the_history_scan_is_reused_and_dropped_after_a_delete(self):
        """扫描要读遍每个 rollout 文件，是列表开销的大头；但删除后必须重扫。"""
        scans = []

        def counted_scan():
            scans.append(1)
            return []

        original_app, original_scan = core.APP, core._scan_history_base_threads
        original_sync = core.sync_desktop_sidebar
        core.APP = FakeApp(active=[{"id": "victim", "name": "Victim", "cwd": "/tmp/p"}])
        core._scan_history_base_threads = counted_scan
        core.sync_desktop_sidebar = lambda ids, cwd_by_id: {
            "ok": True, "catalog": {}, "notification": {}, "warnings": []
        }
        core._forget_history_threads()
        try:
            core.list_sessions("mgr", "active")
            core.list_sessions("mgr", "active")
            self.assertEqual(len(scans), 1)  # 第二次列表复用同一份扫描结果
            core.delete_sessions(["victim"], "删除", "mgr")
            core.list_sessions("mgr", "active")
            self.assertEqual(len(scans), 2)  # 删除复用列表那次，删完作废后才重扫
        finally:
            core.APP, core._scan_history_base_threads = original_app, original_scan
            core.sync_desktop_sidebar = original_sync
            core._forget_history_threads()

class ManagerBootstrapTests(unittest.TestCase):
    """没有上下文令牌时后端会把每一行都标成不可操作，这种列表不能盖掉好数据。"""

    def html(self):
        return server.UI_PATH.read_text(encoding="utf-8")

    def test_bootstrap_waits_for_the_context_before_falling_back(self):
        html = self.html()
        self.assertIn("if (state.sessions.length || state.managerContext) return;", html)
        self.assertNotIn("setTimeout(() => { if (!state.sessions.length) refresh(); }, 500);", html)

    def test_a_refresh_that_started_without_a_context_is_discarded(self):
        html = self.html()
        self.assertIn("const contextAtStart = state.managerContext;", html)
        self.assertIn("if (!contextAtStart && state.managerContext) {", html)
        # 判断必须发生在把数据写进 state 之前，否则灰列表已经盖上去了。
        refresh = html[html.index("async function refresh()"):]
        refresh = refresh[: refresh.index("async function archiveSelected")]
        self.assertLess(
            refresh.index("if (!contextAtStart && state.managerContext)"),
            refresh.index("applySessions(data);"),
        )

    def test_the_scope_button_says_what_it_filters(self):
        """active 是“未归档”，写成“当前”会被读成“当前这个会话”。"""
        html = self.html()
        self.assertIn('<button data-scope="active">未归档</button>', html)
        self.assertIn("scopeActive: '未归档'", html)
        self.assertIn("scopeActive: 'Not archived'", html)


if __name__ == "__main__":
    unittest.main()
