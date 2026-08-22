import importlib.util
import json
import sys
import unittest
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
        server.PROTECTED_THREAD_IDS.clear()

    def tearDown(self):
        server.APP = self.original_app
        server.PROTECTED_THREAD_IDS.clear()

    def test_extracts_thread_id_from_supported_metadata(self):
        self.assertEqual(server._thread_id_from_meta({"openai/threadId": "abc"}), "abc")
        encoded = json.dumps({"thread_id": "nested"})
        self.assertEqual(server._thread_id_from_meta({"x-codex-turn-metadata": encoded}), "nested")

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

    def test_current_thread_is_not_deletable(self):
        server.APP = FakeApp(active=[
            {"id": "manager", "name": "Manager", "cwd": "/tmp/project", "parentThreadId": None},
        ])
        row = server.list_sessions("manager", "active")["sessions"][0]
        self.assertTrue(row["current"])
        self.assertFalse(row["deletable"])

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
            server.delete_sessions(["victim"], "删除", "manager")
        self.assertFalse(any(method == "thread/delete" for method, _ in server.APP.calls))

    def test_delete_requires_current_thread_metadata(self):
        server.APP = FakeApp()
        with self.assertRaisesRegex(ValueError, "当前会话元数据"):
            server.delete_sessions(["victim"], "永久删除", None)
        self.assertFalse(any(method == "thread/delete" for method, _ in server.APP.calls))

    def test_delete_rejects_protected_thread_as_atomic_batch(self):
        server.APP = FakeApp(active=[
            {"id": "manager", "name": "Manager", "cwd": "/tmp", "parentThreadId": None},
            {"id": "victim", "name": "Victim", "cwd": "/tmp", "parentThreadId": None},
        ])
        with self.assertRaisesRegex(ValueError, "受保护"):
            server.delete_sessions(["victim", "manager"], "永久删除", "manager")
        self.assertFalse(any(method == "thread/delete" for method, _ in server.APP.calls))

    def test_delete_revalidates_then_uses_official_method(self):
        server.APP = FakeApp(active=[
            {"id": "victim", "name": "Victim", "cwd": "/tmp/project", "parentThreadId": None},
        ])
        data = server.delete_sessions(["victim"], "永久删除", "manager")
        self.assertTrue(data["results"][0]["ok"])
        self.assertIn(("thread/delete", {"threadId": "victim"}), server.APP.calls)

    def test_mcp_resource_is_self_contained_html(self):
        response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": server.RESOURCE_URI}})
        content = response["result"]["contents"][0]
        self.assertEqual(content["mimeType"], "text/html;profile=mcp-app")
        self.assertIn("Codex 会话清理器", content["text"])
        self.assertIn("tools/call", content["text"])


if __name__ == "__main__":
    unittest.main()
