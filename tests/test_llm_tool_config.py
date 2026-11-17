"""内容工具配置和独立解析器工厂的边界与状态隔离。"""

import json
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from nova_core.config_manager import ConfigManager, LLMToolConfig, PARSER_OUTPUT_KEYS
from nova_core.parser.platform.bilibili import BilibiliParser
from nova_core.parser.runtime_manager.bilibili.auth import BilibiliAuthRuntime


COMMENT_PLATFORMS = (
    "bilibili", "tiktok", "weibo", "xiaohongshu", "xiaoheihe",
    "twitter", "niconico", "steam", "zhihu", "tieba",
)


def platform_config(platform, mode="全部发送", **extra):
    modes = {key: "关闭" for key in PARSER_OUTPUT_KEYS}
    modes[platform] = mode
    return {"parsers": modes, **extra}


class LLMToolConfigTests(unittest.TestCase):
    def test_defaults_and_schema_match(self):
        defaults = asdict(LLMToolConfig())
        self.assertEqual(asdict(ConfigManager({}).llm_tool), defaults)
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )["llm_tool"]["items"]
        self.assertEqual({key: value["default"] for key, value in schema.items()}, defaults)
        self.assertNotIn("max_comments", defaults)
        self.assertNotIn("max_comments", schema)

    def test_switch_accepts_explicit_booleans_and_fails_closed(self):
        for value, enabled in ((True, True), ("true", True), (1, True),
                               (False, False), ("false", False), (0, False),
                               (None, False), ([], False), ({}, False),
                               ("unknown", False), (2, False), (1.0, False)):
            with self.subTest(value=value):
                cfg = ConfigManager({"llm_tool": {"enabled": value}})
                self.assertEqual(cfg.llm_tool.enabled, enabled)
        for malformed in (None, [], "enabled", True):
            with self.subTest(section=malformed):
                self.assertFalse(ConfigManager({"llm_tool": malformed}).llm_tool.enabled)
        self.assertFalse(ConfigManager(None).llm_tool.enabled)

    def test_limits_clamp_and_invalid_types_fall_back(self):
        bounds = {
            "max_links": (3, 1, 5),
            "max_output_chars": (24000, 4000, 60000),
            "timeout_seconds": (45, 5, 120),
            "max_concurrent_calls": (2, 1, 4),
        }
        for field, (default, minimum, maximum) in bounds.items():
            for value, expected in ((-999999, minimum), (999999, maximum),
                                    (str(maximum), maximum), (True, default),
                                    (None, default), ([], default), ({}, default),
                                    (1.5, default), (float("inf"), default),
                                    (float("nan"), default), ("1.5", default)):
                with self.subTest(field=field, value=value):
                    config = ConfigManager({"llm_tool": {field: value}})
                    self.assertEqual(getattr(config.llm_tool, field), expected)


class LLMParserFactoryTests(unittest.TestCase):
    def test_tool_comments_ignore_chat_zero_and_rich_only_modes(self):
        for platform in COMMENT_PLATFORMS:
            for mode in ("全部发送", "仅文本", "仅富媒体"):
                with self.subTest(platform=platform, mode=mode):
                    cfg = ConfigManager(platform_config(
                        platform, mode,
                        message={"hot_comments": {"count": 0}},
                    ))
                    chat_parser = cfg.create_parsers()[0]
                    tool_parser = cfg.create_parsers(for_llm=True)[0]
                    self.assertEqual(chat_parser.hot_comment_count, 0)
                    self.assertEqual(tool_parser.hot_comment_count, 5)
                    self.assertIsNot(tool_parser, chat_parser)
                    self.assertEqual(chat_parser.hot_comment_count, 0)

    def test_disabled_platforms_and_tool_switch_stay_disabled(self):
        for mode in ("关闭", "unknown", None):
            with self.subTest(mode=mode):
                config = ConfigManager(platform_config("bilibili", mode))
                self.assertEqual(config.create_parsers(for_llm=True), [])
        cfg = ConfigManager(platform_config("steam", llm_tool={"enabled": False}))
        self.assertEqual(cfg.create_parsers(for_llm=True), [])
        self.assertEqual([parser.name for parser in cfg.create_parsers()], ["steam"])

    def test_platform_comment_switches_and_tool_comment_range(self):
        for platform in COMMENT_PLATFORMS:
            with self.subTest(platform=platform):
                cfg = ConfigManager(platform_config(
                    platform, message={"hot_comments": {platform: False}}
                ))
                self.assertEqual(cfg.create_parsers(for_llm=True)[0].hot_comment_count, 0)
        cfg = ConfigManager(platform_config("steam"))
        for requested, expected in ((None, 5), (2, 2), (10, 10), (20, 20),
                                    (99, 20), (-3, 0), (0, 0), ("3", 3),
                                    (True, 5), ([], 5), ({}, 5), (1.5, 5),
                                    (float("inf"), 5), ("invalid", 5)):
            with self.subTest(requested=requested):
                parser = cfg.create_parsers(for_llm=True, hot_comment_count=requested)[0]
                self.assertEqual(parser.hot_comment_count, expected)

    def test_legacy_comment_ceiling_is_ignored(self):
        for platform in COMMENT_PLATFORMS:
            for legacy_limit in (0, 4, 5):
                cfg = ConfigManager(platform_config(
                    platform, llm_tool={"max_comments": legacy_limit}
                ))
                self.assertFalse(hasattr(cfg.llm_tool, "max_comments"))
                for requested, expected in ((None, 5), (10, 10), (20, 20)):
                    with self.subTest(platform=platform, legacy_limit=legacy_limit,
                                      requested=requested):
                        parser = cfg.create_parsers(
                            for_llm=True, hot_comment_count=requested
                        )[0]
                        self.assertEqual(parser.hot_comment_count, expected)

    def test_invalid_platform_comment_switches_fail_closed(self):
        for platform in COMMENT_PLATFORMS:
            for value in (None, "unknown", [], 2):
                with self.subTest(platform=platform, value=value):
                    cfg = ConfigManager(platform_config(
                        platform, message={"hot_comments": {"count": 3, platform: value}}
                    ))
                    self.assertEqual(cfg.create_parsers()[0].hot_comment_count, 0)
                    self.assertEqual(cfg.create_parsers(for_llm=True)[0].hot_comment_count, 0)

    def test_media_options_are_independent_and_toutiao_refresh_is_bounded(self):
        for platform in ("niconico", "zhihu", "tieba"):
            with self.subTest(platform=platform):
                cfg = ConfigManager(platform_config(platform, "仅文本"))
                chat_parser = cfg.create_parsers()[0]
                self.assertFalse(chat_parser.include_media)
                self.assertTrue(cfg.create_parsers(for_llm=True)[0].include_media)
                self.assertFalse(cfg.create_parsers(for_llm=True, include_media=False)[0].include_media)
                self.assertFalse(chat_parser.include_media)
        cfg = ConfigManager(platform_config("toutiao"))
        cfg.download.cache_dir_available = True
        self.assertGreater(cfg.create_parsers()[0].article_image_refreshes, 1)
        self.assertEqual(cfg.create_parsers(for_llm=True)[0].article_image_refreshes, 1)

    def test_bilibili_tool_shares_runtime_without_changing_main_parser(self):
        cfg = ConfigManager(platform_config("bilibili", message={"hot_comments": {"count": 3}}))
        cfg.bilibili.enable_admin_assist = True
        cfg.bilibili.cookie_runtime_enabled = True
        primary = cfg.create_parsers()[0]
        primary._mark_assist_request("primary_reason")
        with patch("nova_core.parser.platform.bilibili.BilibiliAuthRuntime") as factory:
            tools = [cfg.create_parsers(for_llm=True)[0] for _ in range(2)]
        factory.assert_not_called()
        self.assertIs(cfg.bilibili_parser, primary)
        self.assertEqual(primary.hot_comment_count, 3)
        self.assertTrue(primary.admin_assist_enabled)
        self.assertEqual(primary.consume_assist_request(), "primary_reason")
        for parser in tools:
            self.assertIsNot(parser, primary)
            self.assertIs(parser.get_auth_runtime(), primary.get_auth_runtime())
            self.assertFalse(parser.admin_assist_enabled)
            parser._mark_assist_request("tool_reason")
            self.assertIsNone(parser.consume_assist_request())
        self.assertIsNot(tools[0], tools[1])

    def test_bilibili_runtime_is_shared_when_tool_factory_runs_first(self):
        cfg = ConfigManager(platform_config("bilibili"))
        tool_parser = cfg.create_parsers(for_llm=True)[0]
        self.assertIsNone(cfg.bilibili_parser)
        with patch("nova_core.parser.platform.bilibili.BilibiliAuthRuntime") as factory:
            primary = cfg.create_parsers()[0]
        factory.assert_not_called()
        self.assertIs(primary.get_auth_runtime(), tool_parser.get_auth_runtime())
        self.assertIs(cfg.bilibili_parser, primary)

    def test_bilibili_constructor_uses_injected_runtime(self):
        runtime = BilibiliAuthRuntime(enabled=False)
        with patch("nova_core.parser.platform.bilibili.BilibiliAuthRuntime") as factory:
            parser = BilibiliParser(auth_runtime=runtime)
        factory.assert_not_called()
        self.assertIs(parser.get_auth_runtime(), runtime)


if __name__ == "__main__":
    unittest.main()
