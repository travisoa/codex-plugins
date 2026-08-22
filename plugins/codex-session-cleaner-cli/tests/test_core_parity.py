import hashlib
import unittest
from pathlib import Path


class CoreParityTests(unittest.TestCase):
    """两个版本共享同一份 core.py，只改一边会让行为悄悄分叉。"""

    def test_core_is_identical_across_editions(self):
        here = Path(__file__).resolve().parents[1] / "server" / "core.py"
        sibling = (
            Path(__file__).resolve().parents[2]
            / ("codex-session-cleaner" if here.parents[1].name.endswith("-cli")
               else "codex-session-cleaner-cli")
            / "server" / "core.py"
        )
        if not sibling.is_file():
            self.skipTest(f"另一版本不在同一仓库中：{sibling}")
        mine = hashlib.sha256(here.read_bytes()).hexdigest()
        theirs = hashlib.sha256(sibling.read_bytes()).hexdigest()
        self.assertEqual(
            mine, theirs,
            "core.py 两版内容不一致；改动共享核心后请同步到另一个插件目录。",
        )


if __name__ == "__main__":
    unittest.main()
