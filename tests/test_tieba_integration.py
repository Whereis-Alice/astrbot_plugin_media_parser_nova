"""贴吧配置、路由和展示复用通用链路。"""

import asyncio
import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_card_skins import render_probe

from nova_core.card import build_model
from nova_core.card.palette import platform_accent
from nova_core.card.theme import resolve_theme_key_for_platform
from nova_core.config_manager import PARSER_OUTPUT_KEYS, ConfigManager
from nova_core.parser.manager import ParserManager
from nova_core.parser.platform import TiebaParser
from nova_core.parser.router import LinkRouter
from nova_core.rika_render.adapter import build_parse_result
from nova_core.translation import MetadataTranslator, build_card_metadata_list


def tieba_config(mode="全部发送", **extra):
    modes = {key: "关闭" for key in PARSER_OUTPUT_KEYS}
    modes["tieba"] = mode
    return {"parsers": modes, **extra}


class TiebaConfigTests(unittest.TestCase):
    def test_tieba_can_be_enabled_and_disabled_independently(self):
        config = ConfigManager(tieba_config())
        self.assertEqual([p.name for p in config.create_parsers()], ["tieba"])
        self.assertEqual(ConfigManager(tieba_config("关闭")).create_parsers(), [])

        modes = {key: "关闭" for key in PARSER_OUTPUT_KEYS}
        modes["steam"] = "全部发送"
        self.assertEqual(
            [p.name for p in ConfigManager({"parsers": modes}).create_parsers()],
            ["steam"],
        )

    def test_factory_honors_each_output_mode(self):
        cases = {
            "关闭": ((False, False), None),
            "全部发送": ((True, True), (True, 3)),
            "仅文本": ((True, False), (False, 3)),
            "仅富媒体": ((False, True), (True, 0)),
        }
        for mode, (flags, expected_kwargs) in cases.items():
            with (
                self.subTest(mode=mode),
                patch("nova_core.config_manager.TiebaParser") as factory,
            ):
                config = ConfigManager(
                    tieba_config(mode, message={"hot_comments": {"count": 3}})
                )
                parsers = config.create_parsers()
                self.assertEqual(
                    config.parser_output.output_for_metadata({"platform": "tieba"}),
                    flags,
                )
                if expected_kwargs is None:
                    factory.assert_not_called()
                    self.assertEqual(parsers, [])
                else:
                    factory.assert_called_once()
                    self.assertEqual(parsers, [factory.return_value])
                    self.assertEqual(
                        factory.call_args.kwargs["include_media"], expected_kwargs[0]
                    )
                    self.assertEqual(
                        factory.call_args.kwargs["hot_comment_count"],
                        expected_kwargs[1],
                    )

    def test_credentials_and_proxy_are_injected_by_config(self):
        config = ConfigManager(
            tieba_config(
                proxy={"address": "http://127.0.0.1:7890", "tieba": True},
                tieba={"cookie": "BDUSS=integration-test"},
            )
        )
        with patch("nova_core.config_manager.TiebaParser") as factory:
            config.create_parsers()
        self.assertEqual(factory.call_args.kwargs["cookie"], "BDUSS=integration-test")
        self.assertTrue(factory.call_args.kwargs["use_proxy"])
        self.assertEqual(factory.call_args.kwargs["proxy_url"], "http://127.0.0.1:7890")

    def test_global_zero_or_tieba_switch_stops_comment_requests(self):
        for options in ({"count": 0, "tieba": True}, {"count": 3, "tieba": False}):
            with (
                self.subTest(options=options),
                patch("nova_core.config_manager.TiebaParser") as factory,
            ):
                config = ConfigManager(tieba_config(message={"hot_comments": options}))
                config.create_parsers()
                self.assertEqual(factory.call_args.kwargs["hot_comment_count"], 0)

    def test_schema_exposes_tieba_defaults(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(set(schema["parsers"]["items"]), set(PARSER_OUTPUT_KEYS))
        entry = schema["parsers"]["items"]["tieba"]
        self.assertEqual(entry["default"], "全部发送")
        self.assertEqual(entry["options"], ["关闭", "全部发送", "仅文本", "仅富媒体"])
        defaults = ConfigManager({})
        self.assertEqual(
            schema["message"]["items"]["hot_comments"]["items"]["tieba"]["default"],
            defaults.message.hot_comments.tieba,
        )
        self.assertEqual(
            schema["proxy"]["items"]["tieba"]["default"],
            defaults.proxy.tieba_use_proxy,
        )
        self.assertEqual(
            schema["tieba"]["items"]["cookie"]["default"], defaults.tieba.cookie
        )


class TiebaPipelineTests(unittest.TestCase):
    URL = "https://tieba.baidu.com/p/1234567890"

    def test_router_and_manager_select_and_normalize_tieba(self):
        parser = TiebaParser()
        router = LinkRouter([parser])
        self.assertIs(router.find_parser(self.URL), parser)
        self.assertEqual(
            router.extract_links_with_parser(f"看看帖子 {self.URL}。"),
            [(self.URL, parser)],
        )
        manager = ParserManager([parser])
        parser.parse = AsyncMock(return_value={"url": self.URL, "title": "帖子标题"})
        results = asyncio.run(manager.parse_text(self.URL, session=None))
        parser.parse.assert_awaited_once_with(None, self.URL)
        self.assertEqual(results[0]["platform"], "tieba")
        self.assertEqual(results[0]["parser_name"], "tieba")
        self.assertEqual(results[0]["source_url"], self.URL)
        self.assertEqual(results[0]["video_urls"], [])
        self.assertEqual(results[0]["image_urls"], [])
        self.assertIsNone(ParserManager([]).find_parser(self.URL))

    def test_post_and_replies_flow_through_translation_and_card(self):
        source = [
            {
                "platform": "tieba",
                "url": self.URL,
                "title": "An English post",
                "desc": "Post body",
                "_card_include_hot_comments": True,
                "comment_section_title": "楼层回复",
                "hot_comments": [
                    {
                        "username": "楼层用户",
                        "uid": "42",
                        "message": "An English reply",
                        "likes": None,
                        "position_text": "2楼",
                    }
                ],
            }
        ]
        translated = copy.deepcopy(source)
        translator = MetadataTranslator.__new__(MetadataTranslator)
        translator.config = SimpleNamespace(
            content_scope="正文和标题",
            max_text_chars_per_request=4000,
            target_language="简体中文",
        )
        groups = translator._collect_item_groups(translated, "简体中文")
        self.assertEqual(
            [item["id"] for group in groups for item in group],
            ["0:title", "0:desc", "0:comment:0"],
        )
        translator._apply_translations(
            translated,
            {"0:title": "翻译标题", "0:desc": "翻译正文", "0:comment:0": "翻译回帖"},
        )
        metadata = build_card_metadata_list(source, translated)[0]
        result = build_parse_result(metadata)
        model = build_model(result, {})
        self.assertEqual(model.platform_key, "tieba")
        self.assertEqual(model.platform_name, "贴吧")
        self.assertEqual(model.title, "翻译标题")
        self.assertEqual(model.body, "翻译正文")
        self.assertEqual(model.comment_section_title, "楼层回复")
        self.assertEqual(model.comments[0].username, "楼层用户")
        self.assertEqual(model.comments[0].message, "翻译回帖")
        self.assertEqual(model.comments[0].time, "2楼")
        self.assertEqual(model.comments[0].likes, "未知")
        self.assertEqual(source[0]["hot_comments"][0]["message"], "An English reply")
        self.assertEqual(source[0]["title"], "An English post")
        self.assertEqual(
            resolve_theme_key_for_platform("跟随平台", model.platform_key), "bilibili"
        )
        self.assertEqual(
            resolve_theme_key_for_platform("霓虹夜景", model.platform_key), "nocturne"
        )
        self.assertEqual(platform_accent(model.platform_key), (51, 133, 255))
        probe = render_probe(
            model,
            theme_key=resolve_theme_key_for_platform("跟随平台", model.platform_key),
            width=640,
        )
        for text in ("贴吧", "翻译标题", "翻译正文", "楼层回复", "翻译回帖", "2楼"):
            self.assertIn(text, probe.joined_text)


if __name__ == "__main__":
    unittest.main()
