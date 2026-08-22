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

    def test_host_without_ui_capability_is_detected(self):
        # 取自 Codex CLI v0.149.0 的真实 initialize 握手。
        server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"elicitation": {"form": {}, "url": {}}},
                "clientInfo": {"name": "codex-mcp-client", "title": "Codex", "version": "0.149.0"},
            },
        })
        self.assertFalse(server._host_renders_ui())
        self.assertEqual(server._HOST["clientInfo"]["title"], "Codex")

    def test_unknown_host_is_never_downgraded_to_text_only(self):
        server._HOST.clear()
        self.assertIsNone(server._host_renders_ui())
        self.assertEqual(server._tui_hint(), "")

    def test_host_declaring_a_ui_capability_keeps_the_component(self):
        for capabilities in (
            {"ui": {}},
            {"experimental": {"openai/outputTemplate": {}}},
            {"experimental": {"mcpApps": {}}},
        ):
            server.handle({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"capabilities": capabilities},
            })
            self.assertTrue(server._host_renders_ui(), capabilities)
            self.assertEqual(server._tui_hint(), "")

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

    def test_text_fallback_points_each_host_at_the_right_affordance(self):
        server.APP = FakeApp(active=[{"id": "t", "name": "n", "cwd": "/tmp", "updatedAt": 1}])

        def body(capabilities):
            server.handle({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"capabilities": capabilities},
            })
            return server.call_tool("list_sessions", {}, {"threadId": "m"})["content"][0]["text"]

        # 能弹表单的宿主应被引导去交互式勾选，而不是另开一个 TUI。
        with_elicitation = body({"elicitation": {"form": {}}})
        self.assertIn("交互式筛选与勾选", with_elicitation)
        self.assertNotIn("launch_tui.sh", with_elicitation)

        # 两种能力都没有的宿主才建议用终端界面。
        self.assertIn("launch_tui.sh", body({"tools": {}}))

        # 能渲染管理页的宿主不需要任何额外提示。
        rendered = body({"ui": {}})
        self.assertNotIn("launch_tui.sh", rendered)
        self.assertNotIn("交互式筛选与勾选", rendered)

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
        server._MANAGER_CONTEXTS.clear()
        server._HOST.clear()
        self.prompts = []

    def tearDown(self):
        server.APP = self.original_app
        server.HOST.request = self.original_request
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
        return server.call_tool("open_session_manager", {}, {"threadId": "manager"})

    def test_filter_then_pick_returns_the_chosen_sessions(self):
        self.host(5)
        self.answers(
            {"action": "accept", "content": {"scope": "all", "datePreset": "all", "tag": ""}},
            {"action": "accept", "content": {"t-1": True, "t-3": True}},
        )
        result = self.open()
        outcome = result["structuredContent"]["interactive"]
        self.assertEqual(outcome["selectedThreadIds"], ["t-1", "t-3"])
        self.assertIn("用户勾选了 2 个会话", result["content"][0]["text"])
        # 第一张表单收筛选条件，第二张逐条勾选。
        self.assertEqual(
            list(self.prompts[0]["requestedSchema"]["properties"]),
            ["scope", "datePreset", "tag"],
        )
        self.assertTrue(
            all(field["type"] == "boolean"
                for field in self.prompts[1]["requestedSchema"]["properties"].values())
        )

    def test_filter_form_offers_the_tags_actually_present(self):
        self.host(3)
        self.answers({"action": "accept", "content": {"scope": "all", "datePreset": "all", "tag": ""}},
                     {"action": "accept", "content": {}})
        self.open()
        tag_field = self.prompts[0]["requestedSchema"]["properties"]["tag"]
        self.assertEqual(tag_field["enum"][0], "")
        self.assertEqual(tag_field["enumNames"][0], "不限")
        self.assertGreater(len(tag_field["enum"]), 1)

    def test_too_many_matches_ask_for_a_narrower_filter_instead_of_prompting(self):
        self.host(20)
        self.answers({"action": "accept", "content": {"scope": "all", "datePreset": "all", "tag": ""}})
        result = self.open()
        outcome = result["structuredContent"]["interactive"]
        self.assertNotIn("selectedThreadIds", outcome)
        self.assertIn("超过一次勾选上限", outcome["note"])
        self.assertEqual(len(self.prompts), 1)  # 没有弹出无法使用的勾选表单

    def test_cancelling_the_filter_falls_back_to_the_text_listing(self):
        self.host(3)
        self.answers({"action": "decline"})
        result = self.open()
        self.assertNotIn("interactive", result["structuredContent"])
        self.assertIn("共 3 个可管理 Codex 会话", result["content"][0]["text"])

    def test_picking_nothing_changes_nothing(self):
        self.host(3)
        self.answers({"action": "accept", "content": {"scope": "all", "datePreset": "all", "tag": ""}},
                     {"action": "accept", "content": {}})
        outcome = self.open()["structuredContent"]["interactive"]
        self.assertNotIn("selectedThreadIds", outcome)
        self.assertIn("没有勾选", outcome["note"])

    def test_hosts_with_a_manager_page_keep_the_component_flow(self):
        self.host(3, capabilities={"ui": {}, "elicitation": {"form": {}}})
        self.answers({"action": "decline"})
        result = self.open()
        self.assertEqual(self.prompts, [])
        self.assertNotIn("interactive", result["structuredContent"])


class ElicitationConfirmTests(unittest.TestCase):
    """CLI 类宿主上，删除前必须由用户在表单里敲定最终名单。"""

    CLI = {"elicitation": {"form": {}, "url": {}}}
    DESKTOP = {"ui": {}, "elicitation": {"form": {}}}

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

    def tearDown(self):
        server.APP = self.original_app
        server.HOST.request = self.original_request
        server.sync_desktop_sidebar = self.original_sync
        server._scan_history_base_threads = self.original_scan
        server._MANAGER_CONTEXTS.clear()
        server._HOST.clear()

    def host(self, capabilities, count=3):
        server.APP = FakeApp(active=[
            {"id": f"t-{index}", "name": f"会话 {index}", "cwd": f"/tmp/p{index}",
             "source": "vscode", "updatedAt": 1787411471 - index}
            for index in range(count)
        ])
        server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"capabilities": capabilities,
                       "clientInfo": {"name": "codex-mcp-client", "title": "Codex"}},
        })

    def answer(self, response):
        def fake_request(method, params, timeout=120.0):
            self.prompts.append((method, params))
            return response
        server.HOST.request = fake_request

    def delete(self, ids):
        return server.call_tool(
            "delete_sessions",
            {"threadIds": ids, "confirmation": "删除"},
            {"threadId": "manager"},
        )

    def deleted(self, result):
        return [row["threadId"] for row in result["structuredContent"]["results"] if row["ok"]]

    def test_only_the_boxes_the_user_ticked_are_deleted(self):
        self.host(self.CLI)
        self.answer({"action": "accept", "content": {"t-0": True, "t-2": True}})
        result = self.delete(["t-0", "t-1", "t-2"])
        self.assertEqual(self.deleted(result), ["t-0", "t-2"])
        self.assertEqual(result["structuredContent"]["requestedThreadIds"], ["t-0", "t-1", "t-2"])
        schema = self.prompts[0][1]["requestedSchema"]["properties"]
        self.assertEqual(list(schema), ["t-0", "t-1", "t-2"])
        # 危险操作默认不勾选，用户必须主动选 True。
        self.assertTrue(all(field["default"] is False for field in schema.values()))
        self.assertIn("会话 0", schema["t-0"]["title"])
        self.assertIn("/tmp/p0", schema["t-0"]["description"])

    def test_ticking_nothing_deletes_nothing(self):
        self.host(self.CLI)
        self.answer({"action": "accept", "content": {}})
        result = self.delete(["t-0", "t-1"])
        self.assertTrue(result["structuredContent"]["cancelled"])
        self.assertEqual(result["structuredContent"]["results"], [])
        self.assertIn("没有勾选", result["content"][0]["text"])

    def test_declining_the_prompt_deletes_nothing(self):
        self.host(self.CLI)
        self.answer({"action": "decline"})
        result = self.delete(["t-0"])
        self.assertEqual(result["structuredContent"]["results"], [])
        self.assertIn("取消", result["content"][0]["text"])

    def test_a_failed_prompt_never_falls_through_to_deleting(self):
        self.host(self.CLI)
        def boom(method, params, timeout=120.0):
            raise server.HostError("宿主连接已关闭。")
        server.HOST.request = boom
        result = self.delete(["t-0", "t-1"])
        self.assertEqual(result["structuredContent"]["results"], [])
        self.assertIn("已取消删除", result["content"][0]["text"])

    def test_large_batches_switch_to_a_single_confirmation(self):
        self.host(self.CLI, count=12)
        self.answer({"action": "accept", "content": {"confirm": "delete_all"}})
        ids = [f"t-{index}" for index in range(12)]
        result = self.delete(ids)
        self.assertEqual(len(self.deleted(result)), 12)
        schema = self.prompts[0][1]["requestedSchema"]["properties"]
        self.assertEqual(list(schema), ["confirm"])
        self.assertEqual(schema["confirm"]["enum"], ["cancel", "delete_all"])

    def test_large_batch_cancel_deletes_nothing(self):
        self.host(self.CLI, count=12)
        self.answer({"action": "accept", "content": {"confirm": "cancel"}})
        result = self.delete([f"t-{index}" for index in range(12)])
        self.assertEqual(result["structuredContent"]["results"], [])

    def test_hosts_with_a_manager_page_are_not_prompted_again(self):
        self.host(self.DESKTOP)
        self.answer({"action": "decline"})
        result = self.delete(["t-0"])
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.deleted(result), ["t-0"])

    def test_hosts_without_elicitation_are_not_prompted(self):
        self.host({"tools": {}})
        self.answer({"action": "decline"})
        result = self.delete(["t-0"])
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.deleted(result), ["t-0"])


if __name__ == "__main__":
    unittest.main()
