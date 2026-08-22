import importlib.util
import sys
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = PLUGIN_ROOT / "server" / "server.py"
TUI_PATH = PLUGIN_ROOT / "tui" / "session_tui.py"


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# 与 session_tui.load_server 用同一个模块名，确保 TUI 复用的是同一份 server 实例。
server = _load("session_cleaner_server", SERVER_PATH)
tui = _load("session_cleaner_tui", TUI_PATH)


def session(thread_id, **overrides):
    row = {
        "id": thread_id,
        "title": thread_id,
        "projectName": "project",
        "updatedAt": 1,
        "archived": False,
        "ephemeral": False,
        "current": False,
        "deletable": True,
        "tags": [{"key": "general", "label": "常规任务"}],
        "blockingForkCount": 0,
    }
    row.update(overrides)
    return row


class WidthTests(unittest.TestCase):
    def test_cjk_characters_count_as_two_columns(self):
        self.assertEqual(tui.display_width("abc"), 3)
        self.assertEqual(tui.display_width("会话"), 4)
        self.assertEqual(tui.display_width("会话ab"), 6)

    def test_truncate_respects_column_budget_not_character_count(self):
        self.assertEqual(tui.truncate("会话清理器", 100), "会话清理器")
        truncated = tui.truncate("会话清理器", 6)
        self.assertLessEqual(tui.display_width(truncated), 6)
        self.assertTrue(truncated.endswith("…"))

    def test_pad_aligns_mixed_width_text_to_the_same_column(self):
        self.assertEqual(tui.display_width(tui.pad("会话", 10)), 10)
        self.assertEqual(tui.display_width(tui.pad("session", 10)), 10)

    def test_truncate_flattens_newlines_that_would_break_the_layout(self):
        self.assertEqual(tui.truncate("a\nb", 10), "a b")


class ContextTests(unittest.TestCase):
    def test_cli_run_without_a_thread_uses_the_dedicated_context_id(self):
        current_id, bound = tui.detect_current_thread(None)
        self.assertEqual(current_id, tui.CLI_CONTEXT_ID)
        self.assertFalse(bound)

    def test_explicit_thread_is_reported_as_bound(self):
        current_id, bound = tui.detect_current_thread("  thread-1  ")
        self.assertEqual(current_id, "thread-1")
        self.assertTrue(bound)

    def test_locale_falls_back_to_environment(self):
        self.assertEqual(tui.detect_locale("en"), "en")
        self.assertEqual(tui.detect_locale("zh"), "zh")


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.state = tui.TuiState(locale="zh")
        self.state.apply({
            "sessions": [session("a"), session("b"), session("temp", deletable=False, ephemeral=True)],
            "availableTags": [{"key": "general", "label": "常规任务", "count": 3}],
        })

    def test_protected_rows_cannot_be_selected(self):
        self.state.cursor = 2
        self.assertFalse(self.state.toggle_current())
        self.assertEqual(self.state.targets(), [])

    def test_targets_only_include_rows_present_in_the_current_listing(self):
        self.state.cursor = 0
        self.state.toggle_current()
        self.state.cursor = 1
        self.state.toggle_current()
        self.assertEqual(self.state.targets(), ["a", "b"])

        # 换一批结果后，已不在列表中的选择不得再影响归档或删除。
        self.state.apply({"sessions": [session("b")], "availableTags": []})
        self.assertEqual(self.state.targets(), ["b"])

    def test_targets_drop_rows_that_stopped_being_deletable(self):
        self.state.cursor = 0
        self.state.toggle_current()
        self.state.apply({"sessions": [session("a", deletable=False)], "availableTags": []})
        self.assertEqual(self.state.targets(), [])

    def test_filter_changes_reset_the_selection(self):
        self.state.cursor = 0
        self.state.toggle_current()
        self.assertEqual(len(self.state.targets()), 1)
        self.state.set_filter(search="anything")
        self.assertEqual(self.state.targets(), [])

    def test_cycling_a_filter_also_resets_the_selection(self):
        self.state.cursor = 0
        self.state.toggle_current()
        self.state.cycle("scope", tui.SCOPES, 1)
        self.assertEqual(self.state.scope, "active")
        self.assertEqual(self.state.targets(), [])

    def test_unknown_tag_is_cleared_when_it_leaves_the_available_list(self):
        self.state.tag = "lark"
        self.state.apply({"sessions": [], "availableTags": [{"key": "general", "label": "常规任务"}]})
        self.assertEqual(self.state.tag, "")

    def test_cursor_stays_inside_the_listing(self):
        self.state.move(50)
        self.assertEqual(self.state.cursor, 2)
        self.state.move(-50)
        self.assertEqual(self.state.cursor, 0)
        self.state.apply({"sessions": [], "availableTags": []})
        self.assertEqual(self.state.cursor, 0)
        self.assertIsNone(self.state.current_row())


class FakeScreen:
    """Minimal curses stand-in so the render path can be exercised headlessly."""

    def __init__(self, height=24, width=80):
        self.size = (height, width)
        self.writes = []

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.writes.clear()

    def addstr(self, y, x, text, attr=0):
        self.writes.append((y, x, text))

    def noutrefresh(self):
        pass


class RenderTests(unittest.TestCase):
    def render(self, width, height=24, rows=None):
        screen = FakeScreen(height, width)
        state = tui.TuiState(locale="zh")
        state.apply({
            "sessions": rows if rows is not None else [
                session("a", title="修复登录问题", projectName="项目A"),
                session("b", title="Temporary test run", projectName="project-b", archived=True),
                session("c", title="数据分析", projectName="项目C", blockingForkCount=2),
            ],
            "availableTags": [{"key": "general", "label": "常规任务", "count": 3}],
        })
        state.selected.add("a")
        view = tui.SessionTui(screen, server, state)
        original = tui.curses.doupdate
        tui.curses.doupdate = lambda: None
        try:
            view.draw()
        finally:
            tui.curses.doupdate = original
        return screen

    def test_no_write_exceeds_the_terminal_width(self):
        for width in (80, 120, 40, 24):
            screen = self.render(width)
            self.assertTrue(screen.writes)
            for y, x, text in screen.writes:
                self.assertLessEqual(
                    x + tui.display_width(text), width,
                    f"width={width} row={y} overflowed: {text!r}",
                )

    def test_narrow_and_short_terminals_do_not_crash(self):
        for width, height in ((20, 10), (12, 6), (200, 60)):
            self.render(width, height)

    def test_empty_listing_renders_the_placeholder(self):
        screen = self.render(80, rows=[])
        self.assertTrue(any("没有符合条件的会话" in text for _, _, text in screen.writes))

    def test_selected_row_shows_a_checked_box(self):
        screen = self.render(120)
        self.assertTrue(any(text.startswith("[x]") for _, _, text in screen.writes))


class ServerReuseTests(unittest.TestCase):
    def test_tui_loads_the_same_server_module_instance(self):
        self.assertIs(tui.load_server(), server)

    def test_summary_counts_only_actionable_targets(self):
        state = tui.TuiState(locale="zh")
        state.apply({
            "sessions": [session("a"), session("temp", deletable=False)],
            "availableTags": [],
        })
        state.cursor = 0
        state.toggle_current()
        state.cursor = 1
        state.toggle_current()
        self.assertIn("已选 1", state.summary())


if __name__ == "__main__":
    unittest.main()
