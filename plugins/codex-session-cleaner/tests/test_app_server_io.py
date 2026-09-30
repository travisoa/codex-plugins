import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


CORE_PATH = Path(__file__).resolve().parents[1] / "server" / "core.py"
SPEC = importlib.util.spec_from_file_location("session_manager_io_core", CORE_PATH)
assert SPEC and SPEC.loader
core = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(core)


class AppServerPipeTests(unittest.TestCase):
    """Use isolated child pipes; never start Codex or touch stored sessions."""

    def client(self, reply_code):
        program = (
            "import json, os, sys, time\n"
            "request = json.loads(sys.stdin.readline())\n"
            + reply_code
            + "\ntime.sleep(2)\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", program],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        client = core.AppServerClient(timeout=0.7)
        client.process = process

        def cleanup():
            client.close()
            process.communicate(timeout=2)

        self.addCleanup(cleanup)
        return client

    def test_notification_and_response_in_one_write_are_both_consumed(self):
        client = self.client(
            "frames = ["
            "{'method': 'thread/status/changed', 'params': {}}, "
            "{'id': request['id'], 'result': {'ok': True}}]\n"
            "os.write(sys.stdout.fileno(), "
            "(chr(10).join(map(json.dumps, frames)) + chr(10)).encode())\n"
        )
        self.assertEqual(
            client.request("thread/list", {}, ensure_started=False), {"ok": True}
        )

    def test_other_request_response_does_not_hide_the_expected_response(self):
        client = self.client(
            "frames = ["
            "{'id': 999, 'result': 'stale'}, "
            "{'id': request['id'], 'result': 'expected'}]\n"
            "os.write(sys.stdout.fileno(), "
            "(chr(10).join(map(json.dumps, frames)) + chr(10)).encode())\n"
        )
        self.assertEqual(
            client.request("thread/read", {}, ensure_started=False), "expected"
        )

    def test_a_utf8_character_split_between_reads_is_preserved(self):
        client = self.client(
            "frame = json.dumps({'id': request['id'], "
            "'result': '会话已读取'}, ensure_ascii=False).encode() + bytes([10])\n"
            "split = frame.index('会'.encode()) + 1\n"
            "os.write(sys.stdout.fileno(), frame[:split])\n"
            "time.sleep(0.03)\n"
            "os.write(sys.stdout.fileno(), frame[split:])\n"
        )
        self.assertEqual(
            client.request("thread/read", {}, ensure_started=False), "会话已读取"
        )

    def test_missing_response_has_a_distinct_timeout_type(self):
        client = self.client("pass\n")
        client.timeout = 0.05
        with self.assertRaises(core.AppServerTimeout):
            client.request("thread/delete", {}, ensure_started=False)


class FullSelectionTests(unittest.TestCase):
    def test_validation_accepts_a_full_list_and_rejects_above_the_cap(self):
        capacity = core.LIST_MAX_PAGES * core.LIST_PAGE_LIMIT * 2
        self.assertEqual(core.BATCH_LIMIT, capacity)
        ids = [f"thread-{index}" for index in range(capacity)]
        self.assertEqual(core.validate_ids(ids), ids)
        with self.assertRaisesRegex(ValueError, f"最多处理 {capacity}"):
            core.validate_ids(ids + ["extra"])

    def test_deleting_more_than_100_preserves_global_history_dependency_order(self):
        active = [
            {"id": f"thread-{index}", "name": f"Session {index}", "updatedAt": index}
            for index in range(150)
        ]
        history = [
            {"id": "thread-148", "historyBaseThreadId": "thread-0"},
            {"id": "thread-149", "historyBaseThreadId": "thread-148"},
        ]

        class FakeApp:
            def __init__(self):
                self.deleted = []

            def request(self, method, params):
                if method == "thread/list":
                    return {"data": [] if params["archived"] else active, "nextCursor": None}
                if method == "thread/delete":
                    self.deleted.append(params["threadId"])
                    return {}
                raise AssertionError(method)

        app = FakeApp()
        with (
            patch.object(core, "APP", app),
            patch.object(core, "_history_threads", return_value=history),
            patch.object(core, "_busy_thread_ids", return_value=set()),
            patch.object(core, "sync_desktop_sidebar", return_value={"ok": True}),
        ):
            result = core.delete_sessions(
                core.validate_ids([row["id"] for row in active]), "删除", "manager"
            )
        self.assertEqual(len(app.deleted), 150)
        self.assertTrue(all(row["ok"] for row in result["results"]))
        self.assertLess(app.deleted.index("thread-149"), app.deleted.index("thread-148"))
        self.assertLess(app.deleted.index("thread-148"), app.deleted.index("thread-0"))


class BatchDeadlineTests(unittest.TestCase):
    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

    def run_operation(self, operation, *, timeout=False, expire=False, preflight_expire=False):
        clock = self.Clock()
        active = [
            {"id": thread_id, "name": thread_id, "updatedAt": 1}
            for thread_id in ("source", "fork", "other")
        ]
        history = [{"id": "fork", "historyBaseThreadId": "source"}]

        class FakeApp:
            def __init__(self):
                self.changed = []

            def request(self, method, params):
                if method == "thread/list":
                    if preflight_expire:
                        clock.now += core.BATCH_OPERATION_TIMEOUT_SECONDS + 1
                        return {"data": active, "nextCursor": "more"}
                    return {"data": [] if params["archived"] else active, "nextCursor": None}
                if method in ("thread/archive", "thread/delete"):
                    self.changed.append(params["threadId"])
                    if timeout:
                        raise core.AppServerTimeout(f"等待 {method} 响应超时。")
                    if expire:
                        clock.now += core.BATCH_OPERATION_TIMEOUT_SECONDS + 1
                    return {}
                raise AssertionError(method)

        app = FakeApp()
        with (
            patch.object(core, "APP", app),
            patch.object(core, "time") as fake_time,
            patch.object(core, "_history_threads", return_value=history),
            patch.object(core, "_busy_thread_ids", return_value=set()),
            patch.object(core, "sync_desktop_sidebar", return_value={"ok": True}),
            patch.object(core, "_notify_desktop_sidebar", return_value={"error": None}),
        ):
            fake_time.monotonic.side_effect = clock.monotonic
            if operation == "delete":
                result = core.delete_sessions(["source", "fork", "other"], "删除", "manager")
            else:
                result = core.archive_sessions(["fork", "source", "other"], "manager")
        return result, app.changed

    def test_deadline_stops_unstarted_items_and_keeps_successful_results(self):
        for operation in ("archive", "delete"):
            with self.subTest(operation=operation):
                result, changed = self.run_operation(operation, expire=True)
                self.assertEqual(changed, ["fork"])
                rows = result["results"]
                self.assertTrue(rows[0]["ok"])
                self.assertEqual(len(rows), 3)
                self.assertTrue(all(not row["ok"] and row["notStarted"] for row in rows[1:]))
                self.assertFalse(result["requiresReopen"])
                if operation == "delete":
                    self.assertEqual([row["threadId"] for row in rows], result["operationOrder"])

    def test_one_request_timeout_stops_the_batch_without_retrying(self):
        for operation in ("archive", "delete"):
            with self.subTest(operation=operation):
                result, changed = self.run_operation(operation, timeout=True)
                self.assertEqual(changed, ["fork"])
                rows = result["results"]
                self.assertTrue(result["requiresReopen"])
                self.assertFalse(rows[0]["ok"])
                self.assertTrue(rows[0]["outcomeUnknown"])
                self.assertTrue(all(row["notStarted"] for row in rows[1:]))
                self.assertTrue(all(not row["ok"] for row in rows))

    def test_preflight_pagination_also_stops_at_the_batch_deadline(self):
        for operation in ("archive", "delete"):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(core.AppServerError, "尚未执行归档或删除"):
                    self.run_operation(operation, preflight_expire=True)


if __name__ == "__main__":
    unittest.main()
