import json
import os
import unittest
from dataclasses import fields
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
    DEFAULT_MAX_VIDEO_SIZE_MB,
    TRANSLATION_OUTPUT_CARD_AND_TEXT,
    TRANSLATION_OUTPUT_CARD_ONLY,
    ConfigManager,
    DownloadConfig,
)
from nova_core.constants import Config
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
        """X、旧版 YouTube 皮肤和「跟随平台」哨兵保持可解析。"""
        self.assertEqual(ConfigManager._parse_card_skin("推特卡片"), CARD_SKIN_X)
        self.assertEqual(ConfigManager._parse_card_skin("X（推特）"), CARD_SKIN_X)
        self.assertEqual(ConfigManager._parse_card_skin("油管卡片"), CARD_SKIN_YOUTUBE)
        self.assertEqual(ConfigManager._parse_card_skin("YouTube"), CARD_SKIN_YOUTUBE)
        for raw in ("跟随平台", "auto", "  AUTO  "):
            with self.subTest(raw=raw):
                self.assertEqual(ConfigManager._parse_card_skin(raw), CARD_SKIN_AUTO)
        for key in (CARD_SKIN_X, CARD_SKIN_YOUTUBE, CARD_SKIN_AUTO):
            self.assertIn(key, CARD_SKINS)

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


class DownloadConfigSchemaTests(unittest.TestCase):
    """确保 download 配置在 dataclass、解析逻辑与 WebUI schema 三处保持一致。"""

    SCHEMA_PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "_conf_schema.json",
    )
    # schema 键名与 dataclass 字段名不同名时的映射
    KEY_ALIASES = {"max_concurrent": "max_concurrent_downloads"}
    # 运行期自行推导、无需暴露给用户的字段
    RUNTIME_ONLY_FIELDS = {"cache_dir_available"}

    @classmethod
    def setUpClass(cls):
        with open(cls.SCHEMA_PATH, "r", encoding="utf-8") as handle:
            cls.schema = json.load(handle)
        cls.download_items = cls.schema["download"]["items"]

    def test_send_video_max_mb_defaults_to_platform_limit(self):
        self.assertEqual(
            DownloadConfig().send_video_max_mb,
            Config.DEFAULT_SEND_VIDEO_MAX_MB,
        )
        self.assertEqual(
            ConfigManager({}).download.send_video_max_mb,
            Config.DEFAULT_SEND_VIDEO_MAX_MB,
        )

    def test_send_video_max_mb_accepts_numbers_and_strings(self):
        cases = {64: 64.0, "80.5": 80.5, 0: 0.0, "0": 0.0}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                config = ConfigManager({"download": {"send_video_max_mb": raw}})
                self.assertEqual(config.download.send_video_max_mb, expected)

    def test_send_video_max_mb_normalizes_invalid_values(self):
        # 非法值回落默认；负数按 0（不限制）处理，与其它体积上限的口径一致
        cases = {
            "abc": Config.DEFAULT_SEND_VIDEO_MAX_MB,
            None: Config.DEFAULT_SEND_VIDEO_MAX_MB,
            -5: 0.0,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                config = ConfigManager({"download": {"send_video_max_mb": raw}})
                self.assertEqual(config.download.send_video_max_mb, expected)

    def test_schema_keys_map_to_dataclass_fields(self):
        field_names = {item.name for item in fields(DownloadConfig)}
        for key in self.download_items:
            with self.subTest(key=key):
                self.assertIn(self.KEY_ALIASES.get(key, key), field_names)

    def test_every_user_facing_field_is_exposed_in_schema(self):
        exposed = {self.KEY_ALIASES.get(key, key) for key in self.download_items}
        for item in fields(DownloadConfig):
            if item.name in self.RUNTIME_ONLY_FIELDS:
                continue
            with self.subTest(field=item.name):
                self.assertIn(item.name, exposed)

    def test_schema_defaults_match_code_defaults(self):
        expected = {
            "max_video_size_mb": DEFAULT_MAX_VIDEO_SIZE_MB,
            "large_video_threshold_mb": Config.MAX_LARGE_VIDEO_THRESHOLD_MB,
            "send_video_max_mb": Config.DEFAULT_SEND_VIDEO_MAX_MB,
            "max_concurrent": float(Config.DOWNLOAD_MANAGER_MAX_CONCURRENT),
        }
        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertEqual(float(self.download_items[key]["default"]), value)

    def test_send_video_max_mb_schema_entry_explains_platform_limit(self):
        entry = self.download_items["send_video_max_mb"]
        self.assertEqual(entry["type"], "float")
        hint = entry.get("hint", "")
        self.assertIn("102902", hint)
        self.assertIn("填 0 不限制", hint)


if __name__ == "__main__":
    unittest.main()
