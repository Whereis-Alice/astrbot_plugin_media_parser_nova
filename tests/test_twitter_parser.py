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

    def test_parse_does_not_generate_redundant_tweet_title(self):
        parser = TwitterParser()
        parser._fetch_media_info = AsyncMock(
            return_value={
                "images": ["https://cdn.example/one.jpg"],
                "videos": [],
                "text": "卡比拥有星星眼。",
                "title": "",
                "author": "SwagKirb(@Swag_K1RBY)",
                "avatar_url": "https://cdn.example/avatar.jpg",
                "timestamp": "2026-08-24 00:44:12",
            }
        )

        result = asyncio.run(
            parser.parse(None, "https://x.com/Swag_K1RBY/status/2091687989021667511")
        )

        self.assertEqual(result["title"], "")
        self.assertEqual(result["author"], "SwagKirb(@Swag_K1RBY)")

    def test_parser_manager_adds_avatar_fallback(self):
        manager = ParserManager([])
        parser = SimpleNamespace(name="twitter")

        result = manager._normalize_metadata(
            "https://x.com/example/status/1", parser, {}
        )

        self.assertEqual(result["avatar_url"], "")

    def test_logged_out_public_replies_are_normalized_and_sorted(self):
        html = """
        {"@id":"https://x.com/low/status/101","@type":"Comment",author:$R[1]={alternateName:"low",name:"Low Reply",identifier:"11",image:"https://cdn.example/low.jpg"},commentCount:0,datePublished:"2026-08-24T01:02:03Z",identifier:"101",interactionStatistic:[{interactionType:"https://schema.org/LikeAction",userInteractionCount:2}],text:"Low &amp; reply"}
        {"@id":"https://x.com/high/status/102","@type":"Comment",author:$R[2]={alternateName:"high",name:"High Reply",identifier:"22",image:"https://cdn.example/high.jpg"},commentCount:0,datePublished:"2026-08-24T02:03:04Z",identifier:"102",interactionStatistic:[{interactionType:"https://schema.org/LikeAction",userInteractionCount:19}],text:"High reply with \\"quote\\""}
        """

        comments = TwitterParser._parse_logged_out_comments_html(html, 2)

        self.assertEqual([item["comment_id"] for item in comments], ["102", "101"])
        self.assertEqual(comments[0]["username"], "High Reply(@high)")
        self.assertEqual(comments[0]["uid"], "22")
        self.assertEqual(comments[0]["likes"], 19)
        self.assertEqual(comments[0]["message"], 'High reply with "quote"')
        self.assertEqual(comments[1]["message"], "Low & reply")


if __name__ == "__main__":
    unittest.main()
