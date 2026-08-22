import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


SERVER_PATH = Path(__file__).resolve().parents[1] / "server" / "server.py"
SPEC = importlib.util.spec_from_file_location("session_cleaner_server", SERVER_PATH)
server = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = server
SPEC.loader.exec_module(server)


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
        self.original_app = server.APP
        self.original_sync = server.sync_desktop_sidebar
        self.original_history_scan = server._scan_history_base_threads
        server._MANAGER_CONTEXTS.clear()
        server._HOST.clear()
        server._scan_history_base_threads = lambda: []
        server.sync_desktop_sidebar = lambda ids, cwd_by_id: {
            "ok": True,
            "catalog": {"removedThreadIds": list(ids), "error": None},
            "notification": {"notifiedThreadIds": list(ids), "error": None},
            "warnings": [],
        }

    def tearDown(self):
        server.APP = self.original_app
        server.sync_desktop_sidebar = self.original_sync
        server._scan_history_base_threads = self.original_history_scan
        server._MANAGER_CONTEXTS.clear()
        server._HOST.clear()

    def test_extracts_thread_id_from_supported_metadata(self):
        self.assertEqual(server._thread_id_from_meta({"openai/threadId": "abc"}), "abc")
        encoded = json.dumps({"thread_id": "nested"})
        self.assertEqual(server._thread_id_from_meta({"x-codex-turn-metadata": encoded}), "nested")

    def test_extracts_ui_locale_from_supported_metadata(self):
        self.assertEqual(server._locale_from_meta({"openai/locale": "zh-CN"}), "zh")
        encoded = json.dumps({"locale": "en-US"})
        self.assertEqual(server._locale_from_meta({"x-codex-turn-metadata": encoded}), "en")

    def test_list_only_returns_roots_and_counts_descendants(self):
        server.APP = FakeApp(active=[
            {"id": "root", "name": "Root", "cwd": "/tmp/project", "parentThreadId": None, "updatedAt": 3},
            {"id": "child", "name": "Child", "cwd": "/tmp/project", "parentThreadId": "root", "updatedAt": 2},
            {"id": "grandchild", "name": "Grand", "cwd": "/tmp/project", "parentThreadId": "child", "updatedAt": 1},
        ])
        data = server.list_sessions("manager", "active")
        self.assertEqual([row["id"] for row in data["sessions"]], ["root"])
        self.assertEqual(data["sessions"][0]["descendantCount"], 2)
        self.assertTrue(data["sessions"][0]["deletable"])

    def test_list_hides_all_subagent_source_shapes(self):
        server.APP = FakeApp(active=[
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
        data = server.list_sessions("manager", "active")
        self.assertEqual([row["id"] for row in data["sessions"]], ["root"])
        self.assertEqual(data["sessions"][0]["descendantCount"], 1)

    def test_current_thread_is_not_deletable(self):
        server.APP = FakeApp(active=[
            {"id": "manager", "name": "Manager", "cwd": "/tmp/project", "parentThreadId": None},
        ])
        row = server.list_sessions("manager", "active")["sessions"][0]
        self.assertTrue(row["current"])
        self.assertFalse(row["deletable"])

    def test_sessions_are_not_selectable_without_current_context(self):
        server.APP = FakeApp(active=[
            {"id": "victim", "name": "Victim", "cwd": "/tmp/project", "source": "vscode"},
        ])
        row = server.list_sessions(None, "active")["sessions"][0]
        self.assertFalse(row["deletable"])

    def test_previous_manager_thread_can_be_deleted_from_new_manager_thread(self):
        server.APP = FakeApp(active=[
            {"id": "old-manager", "name": "Old manager", "cwd": "/tmp", "parentThreadId": None},
            {"id": "new-manager", "name": "New manager", "cwd": "/tmp", "parentThreadId": None},
        ])
        server.list_sessions("old-manager", "all")

        rows = server.list_sessions("new-manager", "all")["sessions"]
        old_manager = next(row for row in rows if row["id"] == "old-manager")
        self.assertTrue(old_manager["deletable"])

        data = server.delete_sessions(["old-manager"], "删除", "new-manager")
        self.assertTrue(data["results"][0]["ok"])
        self.assertIn(("thread/delete", {"threadId": "old-manager"}), server.APP.calls)

    def test_manager_context_survives_ui_calls_without_host_metadata(self):
        server.APP = FakeApp(active=[
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
        self.assertIn(("thread/delete", {"threadId": "old-manager"}), server.APP.calls)

    def test_open_manager_passes_host_locale_to_ui(self):
        server.APP = FakeApp(active=[])
        opened = server.call_tool(
            "open_session_manager", {}, {"openai/threadId": "manager", "openai/locale": "en-US"}
        )["structuredContent"]
        self.assertEqual(opened["locale"], "en")

    def test_invalid_manager_context_is_rejected_before_deletion(self):
        server.APP = FakeApp(active=[{"id": "victim", "name": "Victim", "source": "vscode"}])
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
        self.assertFalse(any(method == "thread/delete" for method, _ in server.APP.calls))

    def test_session_listing_is_not_silently_truncated(self):
        active = [
            {"id": f"root-{index}", "name": f"Root {index}", "cwd": "/tmp/project", "parentThreadId": None, "updatedAt": index}
            for index in range(550)
        ]
        server.APP = FakeApp(active=active)
        data = server.list_sessions("manager", "active")
        self.assertEqual(data["total"], 550)
        self.assertEqual(len(data["sessions"]), 550)
        self.assertFalse(data["truncated"])

    def test_date_presets_filter_by_last_updated_time(self):
        now = datetime(2026, 8, 22, 12, 0, 0)
        server.APP = FakeApp(active=[
            {"id": "old", "name": "Old", "updatedAt": (now - timedelta(days=100)).timestamp()},
            {"id": "month", "name": "Month", "updatedAt": (now - timedelta(days=40)).timestamp()},
            {"id": "recent", "name": "Recent", "updatedAt": (now - timedelta(days=3)).timestamp()},
        ])
        three_months = server.list_sessions(
            "manager", "active", date_preset="older_than_3_months", now=now
        )
        one_month = server.list_sessions(
            "manager", "active", date_preset="older_than_1_month", now=now
        )
        one_week = server.list_sessions(
            "manager", "active", date_preset="older_than_1_week", now=now
        )
        self.assertEqual([row["id"] for row in three_months["sessions"]], ["old"])
        self.assertEqual({row["id"] for row in one_month["sessions"]}, {"old", "month"})
        self.assertEqual({row["id"] for row in one_week["sessions"]}, {"old", "month"})

    def test_recent_date_presets_filter_by_last_updated_time(self):
        now = datetime(2026, 8, 22, 12, 0, 0)
        server.APP = FakeApp(active=[
            {"id": "hours", "name": "Hours", "updatedAt": (now - timedelta(hours=12)).timestamp()},
            {"id": "days", "name": "Days", "updatedAt": (now - timedelta(days=3)).timestamp()},
            {"id": "weeks", "name": "Weeks", "updatedAt": (now - timedelta(days=20)).timestamp()},
            {"id": "months", "name": "Months", "updatedAt": (now - timedelta(days=50)).timestamp()},
        ])
        one_day = server.list_sessions("manager", "active", date_preset="within_1_day", now=now)
        one_week = server.list_sessions("manager", "active", date_preset="within_1_week", now=now)
        one_month = server.list_sessions("manager", "active", date_preset="within_1_month", now=now)
        self.assertEqual([row["id"] for row in one_day["sessions"]], ["hours"])
        self.assertEqual({row["id"] for row in one_week["sessions"]}, {"hours", "days"})
        self.assertEqual({row["id"] for row in one_month["sessions"]}, {"hours", "days", "weeks"})

    def test_hidden_history_fork_is_listed_searchable_and_marks_source(self):
        server.APP = FakeApp(active=[
            {"id": "source", "name": "Source", "cwd": "/tmp/project", "updatedAt": 2},
        ])
        server._scan_history_base_threads = lambda: [{
            "id": "hidden-fork",
            "name": "Source (2)",
            "cwd": "/tmp/project",
            "source": "vscode",
            "updatedAt": 3,
            "archived": False,
            "historyBaseThreadId": "source",
            "hiddenFromList": True,
        }]
        data = server.list_sessions("manager", "all")
        by_id = {row["id"]: row for row in data["sessions"]}
        self.assertEqual(set(by_id), {"source", "hidden-fork"})
        self.assertTrue(by_id["hidden-fork"]["hiddenFromList"])
        self.assertIn("hidden-fork", {tag["key"] for tag in by_id["hidden-fork"]["tags"]})
        self.assertEqual(by_id["source"]["blockingForkIds"], ["hidden-fork"])
        searched = server.list_sessions("manager", "all", search="hidden-fork")
        self.assertEqual([row["id"] for row in searched["sessions"]], ["hidden-fork"])

    def test_tag_filter_uses_generated_category_labels(self):
        server.APP = FakeApp(active=[
            {"id": "plugin", "name": "修复插件", "cwd": "/tmp/codex-plugins", "updatedAt": 2},
            {"id": "general", "name": "普通问答", "cwd": "/tmp", "updatedAt": 1},
        ])
        data = server.list_sessions("manager", "active", tag="plugin-development")
        self.assertEqual([row["id"] for row in data["sessions"]], ["plugin"])
        self.assertIn("plugin-development", {tag["key"] for tag in data["sessions"][0]["tags"]})

    def test_plugin_invocation_links_do_not_force_plugin_development(self):
        tags = server._thread_tags({
            "title": "制作 AI 成果展示视频",
            "preview": "[@remotion](plugin://remotion@openai-curated-remote) 根据报告制作视频",
            "cwd": "/tmp/codex-feishu-demo",
            "projectName": "codex-feishu-demo",
            "source": "vscode",
        })
        self.assertEqual([tag["key"] for tag in tags], ["media"])

    def test_specific_content_beats_project_context(self):
        tags = server._thread_tags({
            "title": "制作产品演示视频",
            "preview": "生成视频并检查画面",
            "cwd": "/tmp/codex-feishu-demo",
            "projectName": "codex-feishu-demo",
            "branch": "main",
        })
        self.assertEqual([tag["key"] for tag in tags], ["media"])

    def test_generic_plugin_mentions_are_not_plugin_development(self):
        tags = server._thread_tags({
            "title": "推荐可安装插件",
            "preview": "查看有哪些插件可以使用",
            "cwd": "/tmp",
            "source": "plugin://catalog",
        })
        self.assertEqual([tag["key"] for tag in tags], ["general"])

    def test_plugin_project_context_remains_plugin_development(self):
        tags = server._thread_tags({
            "title": "继续修复相关问题",
            "cwd": "/tmp/codex-plugins",
            "projectName": "codex-plugins",
            "branch": "main",
        })
        self.assertEqual([tag["key"] for tag in tags], ["plugin-development"])

    def test_custom_date_filter_is_inclusive_for_both_dates(self):
        server.APP = FakeApp(active=[
            {"id": "inside", "name": "Inside", "updatedAt": datetime(2026, 8, 10, 23, 30).timestamp()},
            {"id": "outside", "name": "Outside", "updatedAt": datetime(2026, 8, 11, 0, 0).timestamp()},
        ])
        data = server.list_sessions(
            "manager",
            "active",
            date_preset="custom",
            custom_start="2026-08-10",
            custom_end="2026-08-10",
        )
        self.assertEqual([row["id"] for row in data["sessions"]], ["inside"])

    def test_custom_date_filter_rejects_reversed_range(self):
        server.APP = FakeApp()
        with self.assertRaisesRegex(ValueError, "开始日期不能晚于结束日期"):
            server.list_sessions(
                "manager",
                "active",
                date_preset="custom",
                custom_start="2026-08-11",
                custom_end="2026-08-10",
            )

    def test_file_inspection_separates_changes_and_references(self):
        server.APP = FakeApp(thread={
            "id": "t1", "cwd": "/work/project",
            "turns": [{"items": [
                {"type": "fileChange", "changes": [{"path": "src/a.py", "kind": "update"}]},
                {"type": "commandExecution", "cwd": "/work/project", "scriptPath": "scripts/run.sh", "commandActions": [{"path": "README.md"}]},
            ]}],
        })
        data = server.inspect_files("t1")
        self.assertEqual(data["changedFiles"][0]["path"], "/work/project/src/a.py")
        self.assertEqual({x["path"] for x in data["referencedFiles"]}, {"/work/project/scripts/run.sh", "/work/project/README.md"})

    def test_delete_requires_exact_confirmation(self):
        server.APP = FakeApp()
        with self.assertRaisesRegex(ValueError, "确认词"):
            server.delete_sessions(["victim"], "永久删除", "manager")
        self.assertFalse(any(method == "thread/delete" for method, _ in server.APP.calls))

    def test_delete_requires_current_thread_metadata(self):
        server.APP = FakeApp()
        with self.assertRaisesRegex(ValueError, "当前会话元数据"):
            server.delete_sessions(["victim"], "删除", None)
        self.assertFalse(any(method == "thread/delete" for method, _ in server.APP.calls))

    def test_delete_rejects_current_thread_as_atomic_batch(self):
        server.APP = FakeApp(active=[
            {"id": "manager", "name": "Manager", "cwd": "/tmp", "parentThreadId": None},
            {"id": "victim", "name": "Victim", "cwd": "/tmp", "parentThreadId": None},
        ])
        with self.assertRaisesRegex(ValueError, "当前管理会话"):
            server.delete_sessions(["victim", "manager"], "删除", "manager")
        self.assertFalse(any(method == "thread/delete" for method, _ in server.APP.calls))

    def test_delete_revalidates_then_uses_official_method(self):
        server.APP = FakeApp(active=[
            {"id": "victim", "name": "Victim", "cwd": "/tmp/project", "parentThreadId": None},
        ])
        data = server.delete_sessions(["victim"], "删除", "manager")
        self.assertTrue(data["results"][0]["ok"])
        self.assertTrue(data["sidebarSync"]["ok"])
        self.assertIn(("thread/delete", {"threadId": "victim"}), server.APP.calls)

    def test_delete_accepts_english_confirmation(self):
        server.APP = FakeApp(active=[
            {"id": "victim", "name": "Victim", "cwd": "/tmp/project", "parentThreadId": None},
        ])
        data = server.delete_sessions(["victim"], "delete", "manager")
        self.assertTrue(data["results"][0]["ok"])

    def test_delete_blocks_source_when_hidden_fork_is_not_selected(self):
        server.APP = FakeApp(active=[
            {"id": "source", "name": "Source", "cwd": "/tmp/project"},
        ])
        server._scan_history_base_threads = lambda: [{
            "id": "hidden-fork",
            "name": "Source (2)",
            "cwd": "/tmp/project",
            "archived": False,
            "historyBaseThreadId": "source",
            "hiddenFromList": True,
        }]
        data = server.delete_sessions(["source"], "删除", "manager")
        self.assertFalse(data["results"][0]["ok"])
        self.assertEqual(data["results"][0]["blockingThreadIds"], ["hidden-fork"])
        self.assertFalse(any(method == "thread/delete" for method, _ in server.APP.calls))

    def test_delete_orders_selected_history_fork_before_source(self):
        server.APP = FakeApp(active=[
            {"id": "source", "name": "Source", "cwd": "/tmp/project"},
        ])
        server._scan_history_base_threads = lambda: [{
            "id": "hidden-fork",
            "name": "Source (2)",
            "cwd": "/tmp/project",
            "archived": False,
            "historyBaseThreadId": "source",
            "hiddenFromList": True,
        }]
        data = server.delete_sessions(["source", "hidden-fork"], "删除", "manager")
        delete_calls = [params["threadId"] for method, params in server.APP.calls if method == "thread/delete"]
        self.assertEqual(delete_calls, ["hidden-fork", "source"])
        self.assertEqual(data["operationOrder"], ["hidden-fork", "source"])
        self.assertTrue(all(result["ok"] for result in data["results"]))

    def test_archive_rejects_targets_missing_from_the_manageable_list(self):
        server.APP = FakeApp(active=[
            {"id": "root", "name": "Root", "cwd": "/tmp/project", "source": "vscode"},
            {"id": "child", "name": "Child", "cwd": "/tmp/project", "parentThreadId": "root"},
            {"id": "temp", "name": "Temp", "cwd": "/tmp/project", "ephemeral": True},
        ])
        data = server.archive_sessions(["child", "temp", "ghost", "manager"], "manager")
        by_id = {result["threadId"]: result for result in data["results"]}
        self.assertFalse(by_id["child"]["ok"])
        self.assertFalse(by_id["temp"]["ok"])
        self.assertFalse(by_id["ghost"]["ok"])
        self.assertFalse(by_id["manager"]["ok"])
        self.assertIn("当前管理会话", by_id["manager"]["error"])
        self.assertFalse(any(method == "thread/archive" for method, _ in server.APP.calls))

    def test_archive_still_accepts_top_level_threads(self):
        server.APP = FakeApp(active=[
            {"id": "root", "name": "Root", "cwd": "/tmp/project", "source": "vscode"},
        ])
        data = server.archive_sessions(["root"], "manager")
        self.assertTrue(data["results"][0]["ok"])
        self.assertIn(("thread/archive", {"threadId": "root"}), server.APP.calls)

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

        client = server.AppServerClient(timeout=1.0)
        client.process = Process()
        # 截止时间在 while 判断之后才被越过，正是产生负超时的时序。
        ticks = iter([0.0, 0.9, 2.0, 2.1])
        last = [0.0]

        def monotonic():
            last[0] = next(ticks, last[0])
            return last[0]

        original = server.time.monotonic
        server.time.monotonic = monotonic
        try:
            with self.assertRaisesRegex(server.AppServerError, "超时"):
                client.request("thread/list", {}, ensure_started=False)
        finally:
            server.time.monotonic = original

    def test_delete_scans_history_directory_only_once(self):
        server.APP = FakeApp(active=[
            {"id": "victim", "name": "Victim", "cwd": "/tmp/project", "source": "vscode"},
        ])
        scans = []

        def counted_scan():
            scans.append(1)
            return []

        server._scan_history_base_threads = counted_scan
        data = server.delete_sessions(["victim"], "删除", "manager")
        self.assertTrue(data["results"][0]["ok"])
        self.assertEqual(len(scans), 1)

    def test_delete_keeps_source_when_selected_fork_deletion_fails(self):
        server.APP = FakeApp(
            active=[{"id": "source", "name": "Source", "cwd": "/tmp/project"}],
            failing_deletes={"hidden-fork"},
        )
        server._scan_history_base_threads = lambda: [{
            "id": "hidden-fork",
            "name": "Source (2)",
            "cwd": "/tmp/project",
            "archived": False,
            "historyBaseThreadId": "source",
            "hiddenFromList": True,
        }]
        data = server.delete_sessions(["source", "hidden-fork"], "删除", "manager")
        by_id = {result["threadId"]: result for result in data["results"]}
        self.assertFalse(by_id["hidden-fork"]["ok"])
        self.assertFalse(by_id["source"]["ok"])
        self.assertEqual(by_id["source"]["blockingThreadIds"], ["hidden-fork"])
        self.assertIn("未删除成功", by_id["source"]["error"])
        deleted = [params["threadId"] for method, params in server.APP.calls if method == "thread/delete"]
        self.assertEqual(deleted, ["hidden-fork"])

    def test_search_matches_generated_tag_labels(self):
        server.APP = FakeApp(active=[
            {"id": "plugin", "name": "修复插件", "cwd": "/tmp/codex-plugins", "updatedAt": 2},
            {"id": "general", "name": "普通问答", "cwd": "/tmp", "updatedAt": 1},
        ])
        data = server.list_sessions("manager", "active", search="插件开发")
        self.assertEqual([row["id"] for row in data["sessions"]], ["plugin"])

    def test_search_is_applied_locally_not_pushed_to_app_server(self):
        server.APP = FakeApp(active=[
            {"id": "branch", "name": "重构", "cwd": "/tmp/project", "gitInfo": {"branch": "feature/cleanup"}},
        ])
        data = server.list_sessions("manager", "active", search="feature/cleanup")
        self.assertEqual([row["id"] for row in data["sessions"]], ["branch"])
        listed = [params for method, params in server.APP.calls if method == "thread/list"]
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
                result = server._remove_from_desktop_catalog(["victim"])
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
        self.assertEqual(server._HOST["clientInfo"]["title"], "Codex")
        self.assertTrue(server._host_supports_elicitation())

    def test_elicitation_support_is_not_inferred_from_missing_keys(self):
        """能力只按明确声明判断，不从“没有某个键”反推客户端类型。"""
        server._HOST.clear()
        self.assertFalse(server._host_supports_elicitation())
        server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"capabilities": {"tools": {}}},
        })
        self.assertFalse(server._host_supports_elicitation())

    def test_text_fallback_lists_sessions_with_ids(self):
        server._HOST.clear()
        server.APP = FakeApp(active=[
            {"id": "thread-1", "name": "继续修复相关问题", "cwd": "/tmp/codex-plugins", "updatedAt": 1787411471},
        ])
        result = server.call_tool("list_sessions", {"scope": "all"}, {"threadId": "manager"})
        body = result["content"][0]["text"]
        self.assertIn("thread-1", body)
        self.assertIn("继续修复相关问题", body)
        self.assertIn("/tmp/codex-plugins", body)
        self.assertIn("可用标签", body)

    def test_text_fallback_caps_the_listing_and_says_how_many_remain(self):
        server._HOST.clear()
        server.APP = FakeApp(active=[
            {"id": f"thread-{index}", "name": f"会话 {index}", "cwd": "/tmp", "updatedAt": index}
            for index in range(80)
        ])
        body = server.call_tool("list_sessions", {"scope": "all"}, {"threadId": "manager"})["content"][0]["text"]
        self.assertIn("共 80 个可管理 Codex 会话", body)
        self.assertIn("另有 50 个会话未列出", body)

    def test_the_next_step_is_stated_first_and_as_an_instruction(self):
        """提示压在上百行列表末尾、写成“可以…”，模型会当成可选建议而停下。"""
        server.APP = FakeApp(active=[
            {"id": f"t-{i}", "name": f"会话 {i}", "cwd": "/tmp", "updatedAt": 100 - i}
            for i in range(40)
        ])
        server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"capabilities": {"elicitation": {"form": {}}}},
        })
        body = server.call_tool("open_session_manager", {}, {"openai/threadId": "m"})["content"][0]["text"]
        head = body.split("\n")[0]
        self.assertIn("【下一步】", head)
        self.assertIn("select_sessions", head)
        self.assertIn("请立即调用", head)
        self.assertIn("不要在此停下等待", head)
        # 指令必须在会话清单之前，不能被列表淹没。
        self.assertLess(body.index("select_sessions"), body.index("1. ["))

    def test_text_fallback_points_at_the_right_affordance(self):
        server.APP = FakeApp(active=[{"id": "t", "name": "n", "cwd": "/tmp", "updatedAt": 1}])

        def body(capabilities):
            server.handle({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"capabilities": capabilities},
            })
            return server.call_tool("list_sessions", {}, {"threadId": "m"})["content"][0]["text"]

        # 能弹表单的客户端被指向交互工具，否则才建议终端界面。
        with_elicitation = body({"elicitation": {"form": {}}})
        self.assertIn("select_sessions", with_elicitation)
        self.assertNotIn("launch_tui.sh", with_elicitation)
        self.assertIn("launch_tui.sh", body({"tools": {}}))

    def test_operation_text_reports_each_failure(self):
        server._HOST.clear()
        server.APP = FakeApp(
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
        server._HOST.clear()
        server.APP = FakeApp(thread={
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
        self.assertLess(
            html.index("if (copyTextWithSelection(text))"),
            html.index("navigator.clipboard?.writeText"),
        )
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


class InteractivePickerTests(unittest.TestCase):
    """CLI 类宿主上，open_session_manager 走筛选 + 勾选两步交互。"""

    CLI = {"elicitation": {"form": {}, "url": {}}}

    def setUp(self):
        self.original_app = server.APP
        self.original_request = server.HOST.request
        self.original_scan = server._scan_history_base_threads
        server._scan_history_base_threads = lambda: []
        self.original_sync = server.sync_desktop_sidebar
        server.sync_desktop_sidebar = lambda ids, cwd_by_id: {"ok": True, "warnings": []}
        server._MANAGER_CONTEXTS.clear()
        server._HOST.clear()
        self.prompts = []

    def tearDown(self):
        server.APP = self.original_app
        server.HOST.request = self.original_request
        server.sync_desktop_sidebar = self.original_sync
        server._scan_history_base_threads = self.original_scan
        server._MANAGER_CONTEXTS.clear()
        server._HOST.clear()

    def host(self, count, capabilities=None):
        server.APP = FakeApp(active=[
            {"id": f"t-{index}", "name": f"会话 {index}", "cwd": f"/tmp/p{index}",
             "source": "vscode", "updatedAt": 1787411471 - index}
            for index in range(count)
        ])
        server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"capabilities": capabilities or self.CLI},
        })

    def answers(self, *responses):
        queue = list(responses)
        def fake_request(method, params, timeout=120.0):
            self.prompts.append(params)
            return queue.pop(0)
        server.HOST.request = fake_request

    def open(self):
        return server.call_tool("select_sessions", {}, {"threadId": "manager"})

    FILTER_OK = {"action": "accept", "content": {"scope": "all", "datePreset": "all", "tag": ""}}

    def test_filter_pick_then_act_runs_the_chosen_operation(self):
        self.host(5)
        self.answers(
            self.FILTER_OK,
            {"action": "accept", "content": {"selection": "2,4"}},
            {"action": "accept", "content": {"action": "archive"}},
        )
        result = self.open()
        outcome = result["structuredContent"]["interactive"]
        self.assertEqual(outcome["selectedThreadIds"], ["t-1", "t-3"])
        self.assertEqual(outcome["performed"], "archive")
        self.assertTrue(all(row["ok"] for row in outcome["results"]))
        # 三张表单：筛选、序号输入、操作选择。
        self.assertEqual(list(self.prompts[0]["requestedSchema"]["properties"]),
                         ["scope", "datePreset", "tag"])
        self.assertEqual(list(self.prompts[1]["requestedSchema"]["properties"]), ["selection"])
        self.assertEqual(self.prompts[2]["requestedSchema"]["properties"]["action"]["enum"],
                         ["cancel", "archive", "delete"])

    def test_the_pick_form_lists_every_candidate_with_a_number(self):
        self.host(3)
        self.answers(self.FILTER_OK,
                     {"action": "accept", "content": {"selection": ""}})
        self.open()
        message = self.prompts[1]["message"]
        for number in (1, 2, 3):
            self.assertIn(f"{number}. ", message)
        self.assertIn("会话 0", message)
        self.assertIn("/tmp/p0", message)

    def test_selection_accepts_ranges_and_all(self):
        sessions = [{"id": f"t-{index}"} for index in range(6)]
        self.assertEqual(server._parse_selection("1,3", sessions), ["t-0", "t-2"])
        self.assertEqual(server._parse_selection("2-4", sessions), ["t-1", "t-2", "t-3"])
        self.assertEqual(server._parse_selection("4-2", sessions), ["t-1", "t-2", "t-3"])
        self.assertEqual(server._parse_selection("1, 1 2", sessions), ["t-0", "t-1"])
        self.assertEqual(len(server._parse_selection("all", sessions)), 6)
        self.assertEqual(server._parse_selection("", sessions), [])

    def test_out_of_range_or_garbled_input_is_refused_not_guessed(self):
        sessions = [{"id": f"t-{index}"} for index in range(3)]
        with self.assertRaisesRegex(ValueError, "超出范围"):
            server._parse_selection("5", sessions)
        with self.assertRaisesRegex(ValueError, "无法识别"):
            server._parse_selection("abc", sessions)

    def test_a_bad_selection_reprompts_instead_of_starting_over(self):
        self.host(3)
        self.answers(
            self.FILTER_OK,
            {"action": "accept", "content": {"selection": "9"}},
            {"action": "accept", "content": {"selection": "2"}},
            {"action": "accept", "content": {"action": "cancel"}},
        )
        outcome = self.open()["structuredContent"]["interactive"]
        self.assertEqual(outcome["selectedThreadIds"], ["t-1"])
        # 出错后在同一张表单里重来，并把原因显示出来。
        self.assertIn("超出范围", self.prompts[2]["message"])
        self.assertIn("请重新输入", self.prompts[2]["message"])

    def test_paging_is_chosen_from_options_and_accumulates_selection(self):
        self.host(25)
        self.answers(
            self.FILTER_OK,
            {"action": "accept", "content": {"selection": "3", "page": "next"}},
            {"action": "accept", "content": {"selection": "15", "page": "next"}},
            {"action": "accept", "content": {"selection": "", "page": "prev"}},
            {"action": "accept", "content": {"selection": "", "page": "done"}},
            {"action": "accept", "content": {"action": "cancel"}},
        )
        outcome = self.open()["structuredContent"]["interactive"]
        # 每页选的都累加下来，不用最后一次性重输。
        self.assertEqual(outcome["selectedThreadIds"], ["t-2", "t-14"])
        page_field = self.prompts[1]["requestedSchema"]["properties"]["page"]
        self.assertEqual(page_field["enum"], ["done", "next", "prev"])
        self.assertEqual(page_field["enumNames"], ["完成选择", "下一页", "上一页"])
        self.assertEqual(page_field["default"], "done")
        self.assertIn("第 1/3 页", self.prompts[1]["message"])
        self.assertIn("第 2/3 页", self.prompts[2]["message"])
        self.assertIn("已选 1 个：3", self.prompts[2]["message"])
        self.assertIn("已选 2 个：3, 15", self.prompts[3]["message"])

    def test_already_chosen_rows_are_ticked_and_clear_resets_them(self):
        self.host(15)  # 多页时才有“完成选择”一步，可以边看边加
        self.answers(
            self.FILTER_OK,
            {"action": "accept", "content": {"selection": "2", "page": "next"}},
            {"action": "accept", "content": {"selection": "", "page": "prev"}},
            {"action": "accept", "content": {"selection": "clear", "page": "prev"}},
            {"action": "accept", "content": {"selection": "", "page": "done"}},
        )
        self.open()
        listing = self.prompts[3]["message"]
        self.assertIn("2. ✓ ", listing)
        self.assertIn("已选 1 个：2", listing)
        # clear 之后回到未选状态。
        self.assertNotIn("已选", self.prompts[4]["message"])
        self.assertIn("2. · ", self.prompts[4]["message"])

    def test_a_single_page_submits_straight_away(self):
        self.host(4)
        self.answers(
            self.FILTER_OK,
            {"action": "accept", "content": {"selection": "1,3"}},
            {"action": "accept", "content": {"action": "cancel"}},
        )
        outcome = self.open()["structuredContent"]["interactive"]
        self.assertEqual(outcome["selectedThreadIds"], ["t-0", "t-2"])
        self.assertEqual(len(self.prompts), 3)  # 筛选 + 一次选择 + 操作

    def test_the_current_session_is_listed_but_refused(self):
        self.host(4)
        server.APP.active[1]["id"] = "manager"  # 让第 2 项成为当前会话
        self.answers(
            self.FILTER_OK,
            {"action": "accept", "content": {"selection": "2"}},
            {"action": "accept", "content": {"selection": ""}},
        )
        self.open()
        listing = self.prompts[1]["message"]
        self.assertIn("⊘", listing)
        self.assertIn("当前会话，受保护", listing)
        # 选中受保护项要被挡下并说明原因，而不是静默跳过。
        self.assertIn("不能操作", self.prompts[2]["message"])

    def test_paging_past_the_last_page_says_so(self):
        self.host(15)
        self.answers(
            self.FILTER_OK,
            {"action": "accept", "content": {"selection": "", "page": "next"}},
            {"action": "accept", "content": {"selection": "", "page": "next"}},
            {"action": "accept", "content": {"selection": "", "page": "done"}},
        )
        self.open()
        self.assertIn("已经是最后一页", self.prompts[3]["message"])

    def test_a_single_page_listing_shows_no_paging_hints(self):
        self.host(4)
        self.answers(self.FILTER_OK, {"action": "accept", "content": {"selection": ""}})
        self.open()
        message = self.prompts[1]["message"]
        self.assertNotIn("页", message)
        self.assertNotIn("page", self.prompts[1]["requestedSchema"]["properties"])

    def test_choosing_cancel_performs_nothing(self):
        self.host(4)
        self.answers(
            self.FILTER_OK,
            {"action": "accept", "content": {"selection": "1,2"}},
            {"action": "accept", "content": {"action": "cancel"}},
        )
        outcome = self.open()["structuredContent"]["interactive"]
        self.assertEqual(outcome["selectedThreadIds"], ["t-0", "t-1"])
        self.assertEqual(outcome["performed"], "none")
        self.assertNotIn("results", outcome)

    def test_declining_the_action_form_performs_nothing(self):
        self.host(4)
        self.answers(
            self.FILTER_OK,
            {"action": "accept", "content": {"selection": "1"}},
            {"action": "decline"},
        )
        outcome = self.open()["structuredContent"]["interactive"]
        self.assertEqual(outcome["performed"], "none")

    def test_delete_through_the_picker_is_not_confirmed_twice(self):
        self.host(4)
        self.answers(
            self.FILTER_OK,
            {"action": "accept", "content": {"selection": "1"}},
            {"action": "accept", "content": {"action": "delete"}},
        )
        server.sync_desktop_sidebar = lambda ids, cwd_by_id: {"ok": True, "warnings": []}
        outcome = self.open()["structuredContent"]["interactive"]
        self.assertEqual(outcome["performed"], "delete")
        self.assertEqual([row["threadId"] for row in outcome["results"]], ["t-0"])
        self.assertEqual(len(self.prompts), 3)  # 筛选 + 选择 + 操作，没有第四张确认表单

    def test_filter_form_offers_the_tags_actually_present(self):
        self.host(3)
        self.answers(self.FILTER_OK, {"action": "accept", "content": {"selection": ""}})
        self.open()
        tag_field = self.prompts[0]["requestedSchema"]["properties"]["tag"]
        self.assertEqual(tag_field["enum"][0], "")
        self.assertEqual(tag_field["enumNames"][0], "不限")
        self.assertGreater(len(tag_field["enum"]), 1)

    def test_a_moderate_result_set_is_paged_rather_than_refused(self):
        self.host(40)
        self.answers(self.FILTER_OK,
                     {"action": "accept", "content": {"selection": "", "page": "done"}})
        outcome = self.open()["structuredContent"]["interactive"]
        self.assertIn("没有勾选", outcome["note"])
        self.assertIn("第 1/4 页", self.prompts[1]["message"])

    def test_a_large_result_set_is_never_refused(self):
        self.host(163)  # 与真实会话量相当
        self.answers(
            self.FILTER_OK,
            {"action": "accept", "content": {"selection": "7,101", "page": "done"}},
            {"action": "accept", "content": {"action": "cancel"}},
        )
        outcome = self.open()["structuredContent"]["interactive"]
        self.assertEqual(outcome["selectedThreadIds"], ["t-6", "t-100"])
        self.assertIn("第 1/17 页", self.prompts[1]["message"])

    def test_all_selects_every_operable_session_and_skips_protected_ones(self):
        self.host(5)
        server.APP.active[2]["id"] = "manager"  # 第 3 项为当前会话
        self.answers(
            self.FILTER_OK,
            {"action": "accept", "content": {"selection": "all"}},
            {"action": "accept", "content": {"action": "cancel"}},
        )
        outcome = self.open()["structuredContent"]["interactive"]
        # all 不该因为列表里混有受保护会话而被拒绝。
        self.assertEqual(outcome["selectedThreadIds"], ["t-0", "t-1", "t-3", "t-4"])

    def test_cancelling_the_filter_reports_no_change(self):
        self.host(3)
        self.answers({"action": "decline"})
        outcome = self.open()["structuredContent"]["interactive"]
        self.assertNotIn("selectedThreadIds", outcome)
        self.assertIn("取消", outcome["note"])

    def test_picking_nothing_changes_nothing(self):
        self.host(3)
        self.answers(self.FILTER_OK, {"action": "accept", "content": {"selection": ""}})
        outcome = self.open()["structuredContent"]["interactive"]
        self.assertNotIn("selectedThreadIds", outcome)
        self.assertIn("没有勾选", outcome["note"])

    def test_the_listing_matches_what_the_manager_page_would_show(self):
        """两条路径共用 list_sessions，派生会话同样不单独出现，判断依据也要齐。"""
        server.APP = FakeApp(active=[
            {"id": "root-1", "name": "顶层会话", "cwd": "/tmp/a", "updatedAt": 90},
            {"id": "sub-1", "name": "子代理审查", "cwd": "/tmp/a",
             "parentThreadId": "root-1", "updatedAt": 80},
            {"id": "sub-2", "name": "子代理压缩", "cwd": "/tmp/a",
             "source": {"subAgent": {"threadSpawn": {"parentThreadId": "root-1"}}}, "updatedAt": 70},
            {"id": "root-2", "name": "另一个顶层", "cwd": "/tmp/b", "updatedAt": 60},
            {"id": "temp-1", "name": "临时问答", "cwd": "/tmp/a",
             "ephemeral": True, "updatedAt": 50},
        ])
        server._scan_history_base_threads = lambda: [{
            "id": "fork-1", "name": "隐藏分叉", "cwd": "/tmp/b", "archived": False,
            "historyBaseThreadId": "root-2", "hiddenFromList": True, "updatedAt": 55,
        }]
        server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"capabilities": self.CLI},
        })
        self.answers(self.FILTER_OK, {"action": "accept", "content": {"selection": ""}})
        server.call_tool("select_sessions", {}, {"threadId": "root-1"})
        listing = self.prompts[1]["message"]

        # 子代理会话不单独出现，和管理页一致。
        self.assertNotIn("子代理审查", listing)
        self.assertNotIn("子代理压缩", listing)
        # 但它们的存在要通过派生数量体现出来，否则看不出删除的影响面。
        self.assertIn("连带 2 个派生会话", listing)
        # 管理页会高亮的这几类状态，命令行同样要标出来。
        self.assertIn("隐藏分叉", listing)
        self.assertIn("被 1 个分叉引用", listing)
        self.assertIn("（临时会话，不可操作）", listing)
        self.assertIn("（当前会话，受保护）", listing)

    def test_open_session_manager_never_prompts_on_any_host(self):
        """打开管理页只列会话；交互由 select_sessions 显式承担。"""
        for capabilities in ({"elicitation": {"form": {}}}, {"ui": {}}, {"tools": {}}):
            self.prompts = []
            self.host(3, capabilities=capabilities)
            self.answers({"action": "decline"})
            result = server.call_tool("open_session_manager", {}, {"threadId": "manager"})
            self.assertEqual(self.prompts, [], capabilities)
            self.assertNotIn("interactive", result["structuredContent"], capabilities)

    def test_select_sessions_refuses_hosts_without_elicitation(self):
        self.host(3, capabilities={"tools": {}})
        with self.assertRaisesRegex(ValueError, "不支持交互表单"):
            server.call_tool("select_sessions", {}, {"threadId": "manager"})

    def test_default_filter_reuses_the_listing_already_fetched(self):
        self.host(6)
        calls = []
        original = server.list_sessions
        def counted(*args, **kwargs):
            calls.append(args[1:4])
            return original(*args, **kwargs)
        server.list_sessions = counted
        try:
            self.answers(self.FILTER_OK,
                         {"action": "accept", "content": {"selection": ""}})
            self.open()
        finally:
            server.list_sessions = original
        # 条件仍是默认值时不该再拉一次全量列表。
        self.assertEqual(len(calls), 1)

    def test_a_narrowed_filter_does_refetch(self):
        self.host(6)
        calls = []
        original = server.list_sessions
        def counted(*args, **kwargs):
            calls.append(args[1:4])
            return original(*args, **kwargs)
        server.list_sessions = counted
        try:
            self.answers(
                {"action": "accept",
                 "content": {"scope": "archived", "datePreset": "all", "tag": ""}},
                {"action": "accept", "content": {"selection": ""}},
            )
            self.open()
        finally:
            server.list_sessions = original
        self.assertEqual(len(calls), 2)

    def test_an_oversized_batch_is_refused_like_the_tool_path(self):
        self.host(server.BATCH_LIMIT + 20)
        self.answers(
            self.FILTER_OK,
            {"action": "accept", "content": {"selection": "all", "page": "done"}},
            {"action": "accept", "content": {"action": "delete"}},
        )
        outcome = self.open()["structuredContent"]["interactive"]
        self.assertEqual(outcome["performed"], "none")
        self.assertIn(f"一次最多处理 {server.BATCH_LIMIT} 个会话", outcome["note"])


class ElicitationLocaleTests(unittest.TestCase):
    """表单直接呈现给用户，不经模型转述，必须跟随宿主语言。"""

    def setUp(self):
        self.original_app = server.APP
        self.original_request = server.HOST.request
        self.original_scan = server._scan_history_base_threads
        server._scan_history_base_threads = lambda: []
        server._HOST.clear()
        server._MANAGER_CONTEXTS.clear()
        server.APP = FakeApp(active=[
            {"id": f"t-{i}", "name": f"Session {i}", "cwd": "/tmp/demo",
             "source": "vscode", "updatedAt": 100 - i}
            for i in range(3)
        ])
        server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"capabilities": {"elicitation": {"form": {}}}},
        })
        self.prompts = []

    def tearDown(self):
        server.APP = self.original_app
        server.HOST.request = self.original_request
        server._scan_history_base_threads = self.original_scan
        server._HOST.clear()
        server._MANAGER_CONTEXTS.clear()

    def run_flow(self, meta):
        queue = [
            {"action": "accept", "content": {"scope": "all", "datePreset": "all", "tag": ""}},
            {"action": "accept", "content": {"selection": "1"}},
            {"action": "accept", "content": {"action": "cancel"}},
        ]
        def fake(method, params, timeout=120.0):
            self.prompts.append(params)
            return queue[len(self.prompts) - 1]
        server.HOST.request = fake
        server.call_tool("select_sessions", {}, meta)

    def test_an_english_host_gets_english_forms(self):
        self.run_flow({"openai/threadId": "mgr", "openai/locale": "en-US"})
        filter_form, pick_form, action_form = self.prompts
        self.assertIn("Choose filters first", filter_form["message"])
        self.assertEqual(
            filter_form["requestedSchema"]["properties"]["scope"]["enumNames"],
            ["All", "Current", "Archived"],
        )
        self.assertEqual(
            filter_form["requestedSchema"]["properties"]["tag"]["enumNames"][0], "Any"
        )
        self.assertIn("sessions matched", pick_form["message"])
        self.assertIn(
            "Permanently delete",
            " ".join(action_form["requestedSchema"]["properties"]["action"]["enumNames"]),
        )
        # 英文界面里不该混入中文标点或词句。
        for form in self.prompts:
            self.assertNotIn("：", json.dumps(form, ensure_ascii=False))
            self.assertNotIn("会话", json.dumps(form, ensure_ascii=False))

    def test_a_chinese_host_gets_chinese_forms(self):
        self.run_flow({"openai/threadId": "mgr", "openai/locale": "zh-CN"})
        self.assertIn("请先选择筛选条件", self.prompts[0]["message"])
        self.assertEqual(
            self.prompts[0]["requestedSchema"]["properties"]["scope"]["enumNames"],
            ["全部", "当前", "已归档"],
        )

    def test_category_tags_are_localised_too(self):
        server._HOST["locale"] = "en"
        self.assertEqual(server._tag_label("plugin-development"), "Plugin development")
        server._HOST["locale"] = "zh"
        self.assertEqual(server._tag_label("plugin-development"), "插件开发")

    def test_selection_errors_follow_the_host_language(self):
        sessions = [{"id": "t-0"}]
        server._HOST["locale"] = "en"
        with self.assertRaisesRegex(ValueError, "outside 1-1"):
            server._parse_selection("9", sessions)
        server._HOST["locale"] = "zh"
        with self.assertRaisesRegex(ValueError, "超出范围"):
            server._parse_selection("9", sessions)


class ElicitationFailureTests(unittest.TestCase):
    """表单送不出去是故障，不能报成“用户取消了”。"""

    def setUp(self):
        self.original_app = server.APP
        self.original_request = server.HOST.request
        self.original_scan = server._scan_history_base_threads
        server._scan_history_base_threads = lambda: []
        server._HOST.clear()
        server._MANAGER_CONTEXTS.clear()
        server.APP = FakeApp(active=[
            {"id": f"t-{i}", "name": f"会话 {i}", "cwd": "/tmp/p", "source": "vscode",
             "updatedAt": 100 - i}
            for i in range(3)
        ])
        server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"capabilities": {"elicitation": {"form": {}}}},
        })

    def tearDown(self):
        server.APP = self.original_app
        server.HOST.request = self.original_request
        server._scan_history_base_threads = self.original_scan
        server._HOST.clear()
        server._MANAGER_CONTEXTS.clear()

    def answers(self, *responses):
        queue = list(responses)
        def fake(method, params, timeout=120.0):
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        server.HOST.request = fake

    def open(self):
        return server.call_tool("select_sessions", {}, {"threadId": "manager"})

    FILTER_OK = {"action": "accept", "content": {"scope": "all", "datePreset": "all", "tag": ""}}

    def test_a_timed_out_filter_form_is_not_reported_as_cancelled(self):
        self.answers(server.HostError("等待宿主响应 elicitation/create 超时。"))
        note = self.open()["structuredContent"]["interactive"]["note"]
        self.assertIn("未能显示筛选表单", note)
        self.assertIn("超时", note)
        self.assertNotIn("你已取消", note)

    def test_a_declined_filter_form_still_reads_as_cancelled(self):
        self.answers({"action": "decline"})
        note = self.open()["structuredContent"]["interactive"]["note"]
        self.assertIn("你已取消", note)
        self.assertNotIn("未能显示", note)

    def test_a_failed_pick_form_is_not_reported_as_cancelled(self):
        self.answers(self.FILTER_OK, server.HostError("宿主连接已关闭。"))
        note = self.open()["structuredContent"]["interactive"]["note"]
        self.assertIn("未能完成选择", note)
        self.assertNotIn("取消", note)

    def test_a_failed_action_form_does_not_claim_the_user_cancelled(self):
        self.answers(
            self.FILTER_OK,
            {"action": "accept", "content": {"selection": "1"}},
            server.HostError("等待宿主响应 elicitation/create 超时。"),
        )
        outcome = self.open()["structuredContent"]["interactive"]
        # 会话确实选好了，不能说成用户选了取消。
        self.assertEqual(outcome["selectedThreadIds"], ["t-0"])
        self.assertEqual(outcome["performed"], "none")
        self.assertIn("未能显示操作表单", outcome["note"])
        self.assertNotIn("你选择了取消", outcome["note"])

    def test_a_genuinely_cancelled_action_still_says_so(self):
        self.answers(
            self.FILTER_OK,
            {"action": "accept", "content": {"selection": "1"}},
            {"action": "accept", "content": {"action": "cancel"}},
        )
        outcome = self.open()["structuredContent"]["interactive"]
        self.assertEqual(outcome["performed"], "none")
        self.assertIn("取消", outcome["note"])
        self.assertNotIn("未能显示", outcome["note"])


class DeleteToolTests(unittest.TestCase):
    """delete_sessions 不再自行弹表单：确认由管理页或 select_sessions 各自完成。"""

    def setUp(self):
        self.original_app = server.APP
        self.original_request = server.HOST.request
        self.original_sync = server.sync_desktop_sidebar
        self.original_scan = server._scan_history_base_threads
        server._scan_history_base_threads = lambda: []
        server.sync_desktop_sidebar = lambda ids, cwd_by_id: {"ok": True, "warnings": []}
        server._MANAGER_CONTEXTS.clear()
        server._HOST.clear()
        self.prompts = []
        def fake(method, params, timeout=120.0):
            self.prompts.append(params)
            return {"action": "decline"}
        server.HOST.request = fake

    def tearDown(self):
        server.APP = self.original_app
        server.HOST.request = self.original_request
        server.sync_desktop_sidebar = self.original_sync
        server._scan_history_base_threads = self.original_scan
        server._MANAGER_CONTEXTS.clear()
        server._HOST.clear()

    def host(self, capabilities):
        server.APP = FakeApp(active=[
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
        with self.assertRaisesRegex(ValueError, f"最多处理 {server.BATCH_LIMIT}"):
            server.call_tool(
                "delete_sessions",
                {"threadIds": [f"x-{i}" for i in range(server.BATCH_LIMIT + 1)],
                 "confirmation": "删除"},
                {"threadId": "manager"},
            )


if __name__ == "__main__":
    unittest.main()
