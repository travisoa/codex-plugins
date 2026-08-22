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
    def __init__(self, active=None, archived=None, thread=None):
        self.active = active or []
        self.archived = archived or []
        self.thread = thread or {}
        self.calls = []

    def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/list":
            return {"data": self.archived if params.get("archived") else self.active, "nextCursor": None}
        if method == "thread/read":
            return {"thread": self.thread}
        if method in ("thread/delete", "thread/archive"):
            return {}
        raise AssertionError(method)


class SessionCleanerTests(unittest.TestCase):
    def setUp(self):
        self.original_app = server.APP
        self.original_sync = server.sync_desktop_sidebar
        self.original_history_scan = server._scan_history_base_threads
        server._MANAGER_CONTEXTS.clear()
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
        self.assertIn("ui/notifications/host-context-changed", html)
        self.assertIn("Codex Session Cleaner", html)
        self.assertIn("confirmation: '删除'", html)
        self.assertIn("confirmation: 'delete'", html)
        self.assertIn("confirmation: t().confirmation", html)
        self.assertIn("event.target.value !== t().confirmation", html)
        self.assertIn('<button id="cancelDelete" type="button"', html)
        self.assertIn("$('cancelDelete').addEventListener('click', () => $('deleteDialog').close())", html)

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


if __name__ == "__main__":
    unittest.main()
