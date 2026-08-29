import unittest
from types import SimpleNamespace

from nova_core.config_manager import (
    CARD_SKIN_AUTO,
    CARD_SKIN_BILIBILI,
    CARD_SKIN_EDITORIAL,
    CARD_SKIN_NEON,
    CARD_SKIN_NOVA,
    CARD_SKIN_POSTER,
    CARD_SKIN_SIGNAL,
    CARD_SKIN_X,
    CARD_SKIN_YOUTUBE,
    CARD_SKINS,
    TRANSLATION_OUTPUT_CARD_AND_TEXT,
    TRANSLATION_OUTPUT_CARD_ONLY,
    ConfigManager,
)
from nova_core.translation import MetadataTranslator, build_card_metadata_list


class ConfigAndTranslationTests(unittest.TestCase):
    def test_default_manual_keyword_includes_media_parse(self):
        config = ConfigManager({})
        self.assertIn("媒体解析", config.trigger.keywords)

    def test_card_skin_and_watermark_are_normalized(self):
        config = ConfigManager(
            {
                "message": {
                    "card_render": {
                        "skin": "信号终端",
                        "watermark": "Alice解析",
                    }
                }
            }
        )
        self.assertEqual(config.message.card_render.skin, CARD_SKIN_SIGNAL)
        self.assertEqual(config.message.card_render.watermark, "Alice解析")
        self.assertEqual(ConfigManager._parse_card_skin("编辑室"), CARD_SKIN_EDITORIAL)
        self.assertEqual(ConfigManager._parse_card_skin("海报档案"), CARD_SKIN_POSTER)
        self.assertEqual(ConfigManager._parse_card_skin("霓虹夜景"), CARD_SKIN_NEON)
        self.assertEqual(
            ConfigManager._parse_card_skin("哔哩哔哩风格"),
            CARD_SKIN_BILIBILI,
        )
        self.assertEqual(
            ConfigManager._parse_card_skin("B站动态"),
            CARD_SKIN_BILIBILI,
        )
        self.assertEqual(ConfigManager._parse_card_skin("未知皮肤"), CARD_SKIN_NOVA)

    def test_site_skins_and_auto_sentinel_are_parsed(self):
        """X / YouTube 仿站皮肤与「跟随平台」哨兵都要能从配置里认出来。"""
        self.assertEqual(ConfigManager._parse_card_skin("推特卡片"), CARD_SKIN_X)
        self.assertEqual(ConfigManager._parse_card_skin("X（推特）"), CARD_SKIN_X)
        self.assertEqual(ConfigManager._parse_card_skin("油管卡片"), CARD_SKIN_YOUTUBE)
        self.assertEqual(ConfigManager._parse_card_skin("YouTube"), CARD_SKIN_YOUTUBE)
        for raw in ("跟随平台", "auto", "  AUTO  "):
            with self.subTest(raw=raw):
                self.assertEqual(ConfigManager._parse_card_skin(raw), CARD_SKIN_AUTO)
        for key in (CARD_SKIN_X, CARD_SKIN_YOUTUBE, CARD_SKIN_AUTO):
            self.assertIn(key, CARD_SKINS)

    def test_youtube_cookie_alert_options_are_parsed(self):
        config = ConfigManager(
            {
                "youtube": {
                    "notify_admin_on_cookie_expired": False,
                    "cookie_alert_cooldown_minutes": 30,
                }
            }
        )
        self.assertFalse(config.youtube.notify_admin_on_cookie_expired)
        self.assertEqual(config.youtube.cookie_alert_cooldown_minutes, 30)

        default = ConfigManager({})
        self.assertTrue(default.youtube.notify_admin_on_cookie_expired)
        self.assertEqual(default.youtube.cookie_alert_cooldown_minutes, 120)

    def test_youtube_cookie_keepalive_options_are_parsed(self):
        default = ConfigManager({})
        self.assertTrue(default.youtube.cookie_auto_refresh)
        self.assertEqual(default.youtube.cookie_keepalive_hours, 6)
        # 没填 Cookie 时不需要运行时文件。
        self.assertEqual(default.youtube.cookie_runtime_file, "")

        disabled = ConfigManager(
            {
                "youtube": {
                    "cookie_auto_refresh": False,
                    "cookie_keepalive_hours": 0,
                }
            }
        )
        self.assertFalse(disabled.youtube.cookie_auto_refresh)
        self.assertEqual(disabled.youtube.cookie_keepalive_hours, 0)
        self.assertEqual(disabled.youtube.cookie_runtime_file, "")

        clamped = ConfigManager(
            {"youtube": {"cookie_keepalive_hours": 9999}}
        )
        self.assertEqual(clamped.youtube.cookie_keepalive_hours, 168)

        invalid = ConfigManager(
            {"youtube": {"cookie_keepalive_hours": "不是数字"}}
        )
        self.assertEqual(invalid.youtube.cookie_keepalive_hours, 6)

    def test_card_and_platform_hot_comment_options_are_parsed(self):
        config = ConfigManager(
            {
                "message": {
                    "hot_comments": {
                        "count": 4,
                        "show_in_text": False,
                        "twitter": True,
                        "xiaoheihe": False,
                    },
                    "card_render": {
                        "include_hot_comments": True,
                        "hot_comment_max_chars": 240,
                    },
                }
            }
        )

        self.assertEqual(config.message.hot_comments.count, 4)
        self.assertFalse(config.message.hot_comments.show_in_text)
        self.assertTrue(config.message.hot_comments.twitter)
        self.assertFalse(config.message.hot_comments.xiaoheihe)
        self.assertTrue(config.message.card_render.include_hot_comments)
        self.assertEqual(config.message.card_render.hot_comment_max_chars, 240)
        self.assertEqual(
            ConfigManager(
                {
                    "message": {
                        "card_render": {"hot_comment_max_chars": 9999}
                    }
                }
            ).message.card_render.hot_comment_max_chars,
            600,
        )

    def test_translation_output_mode_is_normalized(self):
        card_only = ConfigManager(
            {"translation": {"output_mode": TRANSLATION_OUTPUT_CARD_ONLY}}
        )
        self.assertEqual(card_only.translation.output_mode, TRANSLATION_OUTPUT_CARD_ONLY)

        invalid = ConfigManager({"translation": {"output_mode": "未知模式"}})
        self.assertEqual(
            invalid.translation.output_mode,
            TRANSLATION_OUTPUT_CARD_AND_TEXT,
        )

        legacy_card_only = ConfigManager(
            {"translation": {"output_mode": "仅作用于卡片"}}
        )
        legacy_card_and_text = ConfigManager(
            {"translation": {"output_mode": "卡片和文本都发送"}}
        )
        self.assertEqual(
            legacy_card_only.translation.apply_scope,
            TRANSLATION_OUTPUT_CARD_ONLY,
        )
        self.assertEqual(
            legacy_card_and_text.translation.apply_scope,
            TRANSLATION_OUTPUT_CARD_AND_TEXT,
        )

    def test_card_metadata_uses_translation_without_mutating_source(self):
        source = [
            {
                "title": "Original title",
                "desc": "Original body",
                "file_paths": ["media.mp4"],
            }
        ]
        translated = [
            {
                "_translated_fields": {
                    "title": "翻译标题",
                    "desc": "翻译正文",
                }
            }
        ]

        card_metadata = build_card_metadata_list(source, translated)

        self.assertEqual(card_metadata[0]["title"], "翻译标题")
        self.assertEqual(card_metadata[0]["desc"], "翻译正文")
        self.assertEqual(source[0]["title"], "Original title")
        self.assertEqual(source[0]["desc"], "Original body")
        self.assertIsNot(card_metadata[0]["file_paths"], source[0]["file_paths"])

    def test_card_metadata_merges_translated_comments_and_preserves_details(self):
        source = [
            {
                "hot_comments": [
                    {
                        "username": "Reply User",
                        "uid": "42",
                        "likes": 17,
                        "time": "2026-08-24 12:00:00",
                        "message": "Original reply",
                    }
                ]
            }
        ]
        translated = [
            {
                "hot_comments": [
                    {
                        "username": "Reply User",
                        "uid": "42",
                        "likes": 17,
                        "time": "2026-08-24 12:00:00",
                        "message": "Original reply",
                        "_translated_message": "已翻译回复",
                    }
                ],
                "translation_target_language": "简体中文",
            }
        ]

        card_metadata = build_card_metadata_list(source, translated)
        comment = card_metadata[0]["hot_comments"][0]

        self.assertEqual(comment["message"], "已翻译回复")
        self.assertEqual(comment["username"], "Reply User")
        self.assertEqual(comment["uid"], "42")
        self.assertEqual(comment["likes"], 17)
        self.assertEqual(source[0]["hot_comments"][0]["message"], "Original reply")

    def test_translation_collects_title_body_and_comment_together(self):
        translator = MetadataTranslator.__new__(MetadataTranslator)
        translator.config = SimpleNamespace(
            content_scope="正文和标题",
            max_text_chars_per_request=4000,
        )
        metadata = [
            {
                "title": "An English title",
                "desc": "An English body",
                "hot_comments": [{"message": "An English reply"}],
            }
        ]

        groups = translator._collect_item_groups(metadata, "简体中文")
        item_ids = [item["id"] for group in groups for item in group]

        self.assertEqual(item_ids, ["0:title", "0:desc", "0:comment:0"])

    def test_translation_applies_to_comment_without_losing_metadata(self):
        translator = MetadataTranslator.__new__(MetadataTranslator)
        translator.config = SimpleNamespace(target_language="简体中文")
        metadata = [
            {
                "hot_comments": [
                    {
                        "username": "Alice",
                        "uid": "10001",
                        "likes": 9,
                        "message": "Hello",
                    }
                ]
            }
        ]

        translator._apply_translations(
            metadata,
            {"0:comment:0": "你好"},
        )

        comment = metadata[0]["hot_comments"][0]
        self.assertEqual(comment["_translated_message"], "你好")
        self.assertEqual(comment["username"], "Alice")
        self.assertEqual(comment["likes"], 9)


if __name__ == "__main__":
    unittest.main()
