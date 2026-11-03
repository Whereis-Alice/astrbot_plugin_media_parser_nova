import unittest

from nova_core.comment_display import comment_likes_text
from nova_core.config_manager import ConfigManager, PARSER_OUTPUT_KEYS
from nova_core.message_adapter.node_builder import build_hot_comments_node
from nova_core.rika_render.adapter import build_parse_result


class CommentDeliveryTests(unittest.TestCase):
    def test_source_label_position_and_metric_are_visible(self):
        metadata = {
            "comment_section_title": "弹幕摘选",
            "hot_comments": [{"username": "匿名弹幕用户", "message": "真实弹幕", "likes": 2, "likes_label": "Nicoる", "position_text": "弹幕位置 00:32"}],
        }
        text = build_hot_comments_node(metadata).text
        self.assertIn("弹幕摘选（1条）", text)
        self.assertIn("Nicoる: 2", text)
        self.assertIn("弹幕位置 00:32", text)
        self.assertNotIn("点赞:", text)

    def test_unknown_and_compact_counts_are_not_invented(self):
        for value in (None, "invalid", -1, float("nan"), 2.5, True):
            self.assertEqual(comment_likes_text({"likes": value}), "未知")
        self.assertEqual(comment_likes_text({"likes": 0}), "0")
        self.assertEqual(comment_likes_text({"likes_text": "1.1K"}), "1.1K")

    def test_disabled_or_empty_comments_do_not_create_text(self):
        self.assertIsNone(build_hot_comments_node({"hot_comments": []}))
        self.assertIsNone(build_hot_comments_node({"hot_comments": [{"message": "评论"}]}, False))

    def test_card_adapter_preserves_comment_provenance(self):
        metadata = {"platform": "steam", "_card_include_hot_comments": True, "comment_section_title": "玩家评测", "hot_comments": [{"username": "玩家", "message": "好评", "likes": 4, "likes_label": "有帮助"}]}
        result = build_parse_result(metadata)
        self.assertEqual(result.extra["comment_section_title"], "玩家评测")
        self.assertEqual(result.extra["hot_comments"][0]["likes_label"], "有帮助")

    def test_each_comment_switch_and_global_zero_disable_requests(self):
        for key in ("niconico", "tiktok", "steam", "zhihu"):
            modes = {platform: "关闭" for platform in PARSER_OUTPUT_KEYS}
            modes[key] = "全部发送"
            config = {"parsers": modes, "message": {"hot_comments": {"count": 3, key: True}}}
            parser = ConfigManager(config).create_parsers()[0]
            self.assertEqual(parser.hot_comment_count, 3)
            config["message"]["hot_comments"][key] = False
            self.assertEqual(ConfigManager(config).create_parsers()[0].hot_comment_count, 0)
            config["message"]["hot_comments"] = {"count": 0, key: True}
            self.assertEqual(ConfigManager(config).create_parsers()[0].hot_comment_count, 0)

