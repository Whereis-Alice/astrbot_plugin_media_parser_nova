import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nova_core.parser.manager import ParserManager
from nova_core.parser.platform.twitter import (
    TwitterParser,
)


class TwitterParserTests(unittest.TestCase):
    def test_extract_links_keeps_message_order(self):
        parser = TwitterParser()
        text = (
            "先看 https://x.com/example/status/2090756349701554395，"
            "再看 https://twitter.com/example/status/2090756349701554396。"
        )

        self.assertEqual(
            parser.extract_links(text),
            [
                "https://x.com/example/status/2090756349701554395",
                "https://twitter.com/example/status/2090756349701554396",
            ],
        )

    def test_parse_uses_avatar_from_media_info(self):
        parser = TwitterParser()
        parser._fetch_media_info = AsyncMock(
            return_value={
                "images": [],
                "videos": [],
                "text": "hello",
                "title": "Example tweet",
                "author": "Example(@example)",
                "avatar_url": "https://cdn.example/avatar.jpg",
                "timestamp": "2026-08-22 10:01:48",
            }
        )

        result = asyncio.run(
            parser.parse(None, "https://x.com/example/status/2090756349701554395")
        )

        self.assertEqual(result["avatar_url"], "https://cdn.example/avatar.jpg")
        self.assertEqual(result["desc"], "hello")

    def test_parser_manager_adds_avatar_fallback(self):
        manager = ParserManager([])
        parser = SimpleNamespace(name="twitter")

        result = manager._normalize_metadata(
            "https://x.com/example/status/1", parser, {}
        )

        self.assertEqual(result["avatar_url"], "")


if __name__ == "__main__":
    unittest.main()
