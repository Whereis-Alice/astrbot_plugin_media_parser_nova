"""B 站解析器的结构化字段提取测试。"""

import unittest

from nova_core.parser.platform.bilibili import BilibiliParser


class PolymerStatsTests(unittest.TestCase):
    """图文动态（opus / t.bilibili.com）没有视频 stat 对象，统计挂在 module_stat 上。"""

    def setUp(self) -> None:
        self.parser = BilibiliParser()

    def test_reads_counts_from_nested_dicts(self) -> None:
        modules = {
            "module_stat": {
                "like": {"count": 361},
                "comment": {"count": 33},
                "forward": {"count": 8},
            }
        }

        self.assertEqual(
            self.parser._extract_polymer_stats(modules),
            "\U0001f44d 361 \U0001f4ac 33 \u21a9\ufe0f 8",
        )

    def test_reads_counts_from_bare_values(self) -> None:
        modules = {"module_stat": {"like": 12, "comment": 0, "forward": 3}}

        # 0 不入统计行，避免卡片上出现"评论 0"这种噪声。
        self.assertEqual(
            self.parser._extract_polymer_stats(modules),
            "\U0001f44d 12 \u21a9\ufe0f 3",
        )

    def test_large_numbers_are_compacted(self) -> None:
        modules = {"module_stat": {"like": {"count": 123000}}}

        self.assertEqual(self.parser._extract_polymer_stats(modules), "\U0001f44d 12.3万")

    def test_missing_or_malformed_module_stat_yields_empty_line(self) -> None:
        self.assertEqual(self.parser._extract_polymer_stats({}), "")
        self.assertEqual(self.parser._extract_polymer_stats({"module_stat": None}), "")
        self.assertEqual(self.parser._extract_polymer_stats({"module_stat": []}), "")
        self.assertEqual(self.parser._extract_polymer_stats({"module_stat": {}}), "")


if __name__ == "__main__":
    unittest.main()
