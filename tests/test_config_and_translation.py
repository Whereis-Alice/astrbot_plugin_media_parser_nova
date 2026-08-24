import unittest

from nova_core.config_manager import (
    CARD_SKIN_EDITORIAL,
    CARD_SKIN_NOVA,
    CARD_SKIN_POSTER,
    CARD_SKIN_SIGNAL,
    TRANSLATION_OUTPUT_CARD_AND_TEXT,
    TRANSLATION_OUTPUT_CARD_ONLY,
    ConfigManager,
)
from nova_core.translation import build_card_metadata_list


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
        self.assertEqual(ConfigManager._parse_card_skin("未知皮肤"), CARD_SKIN_NOVA)

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


if __name__ == "__main__":
    unittest.main()
