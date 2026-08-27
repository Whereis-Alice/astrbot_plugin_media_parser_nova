import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp

from nova_core.parser.manager import ParserManager
from nova_core.parser.platform import twitter as twitter_module
from nova_core.parser.platform.twitter import (
    TwitterParser,
)


def _forbidden(*_args, **_kwargs) -> aiohttp.ClientResponseError:
    """构造一个与 X 风控一致的 403 异常。"""
    return aiohttp.ClientResponseError(
        request_info=SimpleNamespace(
            real_url="https://x.com/i/status/123",
            url="https://x.com/i/status/123",
            method="GET",
            headers={},
        ),
        history=(),
        status=403,
        message="Forbidden",
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


_JSONLD_PAGE = """
<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "SocialMediaPosting",
  "identifier": "2092232578006925597",
  "comment": [
    {
      "@type": "Comment",
      "@id": "https://x.com/alpha/status/111",
      "identifier": "111",
      "text": "\u666e\u901a\u56de\u590d",
      "datePublished": "2026-08-25T04:00:00.000Z",
      "author": {
        "@type": "Person",
        "name": "\u963f\u5c14\u6cd5",
        "alternateName": "alpha",
        "identifier": "9001",
        "image": {"@type": "ImageObject", "contentUrl": "https://cdn.example/a.jpg"}
      },
      "interactionStatistic": [
        {"@type": "InteractionCounter",
         "interactionType": "https://schema.org/LikeAction",
         "userInteractionCount": 7}
      ]
    },
    {
      "@type": "Comment",
      "@id": "https://x.com/beta/status/222",
      "identifier": "222",
      "text": "\u70ed\u95e8\u56de\u590d 5 &gt; 3",
      "datePublished": "2026-08-25T05:00:00.000Z",
      "author": {
        "@type": "Person",
        "name": "Beta",
        "url": "https://x.com/beta",
        "image": "https://cdn.example/b.jpg"
      },
      "interactionStatistic": [
        {"@type": "InteractionCounter",
         "interactionType": "https://schema.org/ReplyAction",
         "userInteractionCount": 3},
        {"@type": "InteractionCounter",
         "interactionType": "https://schema.org/LikeAction",
         "userInteractionCount": 99}
      ]
    }
  ]
}
</script>
</head><body></body></html>
"""


class TwitterPublicCommentTests(unittest.TestCase):
    def test_jsonld_comments_sorted_by_likes(self):
        comments = TwitterParser._parse_jsonld_comments(_JSONLD_PAGE, 5)

        self.assertEqual(len(comments), 2)
        self.assertEqual(comments[0]["likes"], 99)
        self.assertEqual(comments[0]["username"], "Beta")
        self.assertEqual(comments[0]["message"], "\u70ed\u95e8\u56de\u590d 5 > 3")
        self.assertEqual(comments[0]["avatar_url"], "https://cdn.example/b.jpg")
        self.assertEqual(comments[1]["username"], "\u963f\u5c14\u6cd5(@alpha)")
        self.assertEqual(comments[1]["uid"], "9001")
        self.assertEqual(comments[1]["avatar_url"], "https://cdn.example/a.jpg")
        self.assertTrue(comments[1]["time"])

    def test_jsonld_comments_respect_limit(self):
        comments = TwitterParser._parse_jsonld_comments(_JSONLD_PAGE, 1)

        self.assertEqual([item["likes"] for item in comments], [99])

    def test_extract_public_comments_on_empty_page(self):
        self.assertEqual(TwitterParser._extract_public_comments("<html></html>", 3), [])

    def test_fetch_hot_comments_returns_empty_when_page_has_no_replies(self):
        parser = TwitterParser(hot_comment_count=3)
        parser._fetch_public_page = AsyncMock(return_value="<html>no data</html>")

        comments = asyncio.run(parser._fetch_hot_comments(object(), "123"))

        self.assertEqual(comments, [])
        # 页面能取回就说明 UA 没被拒，不应再用第二个爬虫 UA 重复请求同一地址。
        self.assertEqual(parser._fetch_public_page.await_count, 1)

    def test_fetch_hot_comments_retries_through_proxy_when_direct_fails(self):
        parser = TwitterParser(
            use_parse_proxy=False,
            proxy_url="http://127.0.0.1:7890",
            hot_comment_count=2,
        )
        calls = []

        async def fake_page(session, url, proxy, user_agent=None, timeout=None):
            calls.append((url, proxy))
            if proxy is None:
                raise OSError("connect timeout")
            return _JSONLD_PAGE

        parser._fetch_public_page = fake_page

        comments = asyncio.run(parser._fetch_hot_comments(object(), "123"))

        self.assertEqual(len(comments), 2)
        self.assertEqual(calls[-1][1], "http://127.0.0.1:7890")
        # 直连是传输层失败，同一代理下不应继续换 UA/换路径空转。
        self.assertEqual(len([item for item in calls if item[1] is None]), 1)

    def test_fetch_hot_comments_uses_canonical_path_first(self):
        parser = TwitterParser(hot_comment_count=2)
        calls = []

        async def fake_page(session, url, proxy, user_agent=None, timeout=None):
            calls.append(url)
            raise _forbidden()

        parser._fetch_public_page = fake_page

        self.assertEqual(
            asyncio.run(parser._fetch_hot_comments(object(), "123", "kuuu_Arcana")),
            [],
        )
        self.assertEqual(calls[0], "https://x.com/kuuu_Arcana/status/123")
        self.assertIn("https://x.com/i/status/123", calls)
        # twitter.com 会 301 到 x.com，不再作为"第二来源"重复请求。
        self.assertFalse([url for url in calls if "twitter.com" in url])

    def test_fetch_hot_comments_switches_user_agent_on_forbidden(self):
        parser = TwitterParser(hot_comment_count=2)
        seen = []

        async def fake_page(session, url, proxy, user_agent=None, timeout=None):
            seen.append(user_agent)
            raise _forbidden()

        parser._fetch_public_page = fake_page

        asyncio.run(parser._fetch_hot_comments(object(), "123"))

        self.assertEqual(len(set(seen)), 2)

    def test_forbidden_only_failures_do_not_emit_warning(self):
        parser = TwitterParser(hot_comment_count=2)
        parser._fetch_public_page = AsyncMock(side_effect=_forbidden())
        TwitterParser._blocked_notice_at = 0.0

        with patch.object(twitter_module.logger, "warning") as warn:
            asyncio.run(parser._fetch_hot_comments(object(), "123"))

        warn.assert_not_called()

    def test_unexpected_failure_still_warns(self):
        parser = TwitterParser(hot_comment_count=2)
        parser._fetch_public_page = AsyncMock(side_effect=OSError("boom"))

        with patch.object(twitter_module.logger, "warning") as warn:
            asyncio.run(parser._fetch_hot_comments(object(), "123"))

        warn.assert_called_once()

    def test_blocked_notice_is_rate_limited(self):
        parser = TwitterParser(hot_comment_count=2)
        parser._fetch_public_page = AsyncMock(side_effect=_forbidden())
        TwitterParser._blocked_notice_at = 0.0

        with patch.object(twitter_module.logger, "info") as info:
            asyncio.run(parser._fetch_hot_comments(object(), "123"))
            asyncio.run(parser._fetch_hot_comments(object(), "456"))

        info.assert_called_once()

    def test_screen_name_from_url(self):
        self.assertEqual(
            TwitterParser._screen_name_from_url(
                "https://x.com/kuuu_Arcana/status/2092232578006925597"
            ),
            "kuuu_Arcana",
        )
        self.assertEqual(
            TwitterParser._screen_name_from_url("https://x.com/i/status/123"),
            "i",
        )
        # handle 为 i 时不该拼出与 /i/status 重复的地址
        self.assertEqual(
            TwitterParser._public_page_urls("123", "i"),
            ["https://x.com/i/status/123"],
        )

    def test_fetch_hot_comments_disabled_returns_empty(self):
        parser = TwitterParser(hot_comment_count=0)
        parser._fetch_public_page = AsyncMock(return_value=_JSONLD_PAGE)

        self.assertEqual(asyncio.run(parser._fetch_hot_comments(object(), "1")), [])
        parser._fetch_public_page.assert_not_awaited()


class TwitterTextEntityTests(unittest.TestCase):
    def test_twitter_text_unescapes_html_entities(self):
        text = TwitterParser._twitter_text({"text": "(&gt;_&lt;) &amp; more"})

        self.assertEqual(text, "(>_<) & more")

    def test_graphql_text_unescapes_after_display_range(self):
        tweet = {
            "legacy": {
                "full_text": "@someone hello &amp; bye",
                "display_text_range": [9, 24],
            }
        }

        self.assertEqual(TwitterParser._graphql_tweet_text(tweet), "hello & bye")


if __name__ == "__main__":
    unittest.main()
