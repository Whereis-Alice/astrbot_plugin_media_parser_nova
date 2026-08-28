"""提链清洗测试：链接尾部粘连正文时必须被裁掉，不能把中文吃进 URL。"""

import unittest

from nova_core.parser.platform.bilibili import BilibiliParser
from nova_core.parser.utils import trim_url_tail


class TrimUrlTailTests(unittest.TestCase):
    """``trim_url_tail`` 只保留合法 URL 字符前缀，并剥掉尾部标点。"""

    def test_strips_chinese_text_glued_to_a_short_link(self) -> None:
        # 用户在群里常把指令和链接连着发：https://b23.tv/5AeLOCA媒体解析
        self.assertEqual(
            trim_url_tail("https://b23.tv/5AeLOCA媒体解析"),
            "https://b23.tv/5AeLOCA",
        )

    def test_keeps_query_and_fragment_intact(self) -> None:
        url = "https://www.bilibili.com/video/BV1xx?spm_id_from=333.1007&p=2#reply123"
        self.assertEqual(trim_url_tail(url), url)

    def test_plain_ascii_link_is_returned_unchanged(self) -> None:
        url = "https://x.com/i/status/2092446937500860496"
        self.assertEqual(trim_url_tail(url), url)

    def test_strips_trailing_punctuation(self) -> None:
        self.assertEqual(
            trim_url_tail("https://b23.tv/5AeLOCA。"),
            "https://b23.tv/5AeLOCA",
        )
        self.assertEqual(
            trim_url_tail("https://b23.tv/5AeLOCA)"),
            "https://b23.tv/5AeLOCA",
        )

    def test_full_width_and_emoji_tails_are_cut(self) -> None:
        self.assertEqual(
            trim_url_tail("https://b23.tv/5AeLOCA\U0001f602"),
            "https://b23.tv/5AeLOCA",
        )

    def test_never_returns_empty_for_a_non_url_input(self) -> None:
        # 全是非法字符时保留原值，交给上层的 can_parse 去否决。
        self.assertEqual(trim_url_tail("媒体解析"), "媒体解析")
        self.assertEqual(trim_url_tail(""), "")


class B23LinkExtractionTests(unittest.TestCase):
    """B 站提链正则对短链走白名单，紧跟的中文不会被拼进 slug。"""

    def setUp(self) -> None:
        self.parser = BilibiliParser()

    def test_extracts_short_link_without_the_following_text(self) -> None:
        links = self.parser.extract_links("https://b23.tv/5AeLOCA媒体解析")

        self.assertIn("https://b23.tv/5AeLOCA", links)
        self.assertFalse(
            [link for link in links if "媒体解析" in link],
            f"提链结果里不应残留正文：{links}",
        )

    def test_extracts_short_link_from_a_normal_sentence(self) -> None:
        links = self.parser.extract_links("看看这个 https://b23.tv/5AeLOCA 挺有意思")

        self.assertIn("https://b23.tv/5AeLOCA", links)


if __name__ == "__main__":
    unittest.main()
