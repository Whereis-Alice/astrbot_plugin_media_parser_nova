import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp

from nova_core.downloader.security import (
    create_public_only_connector,
    session_uses_public_only_connector,
)
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


class TwitterNitterExtrasTests(unittest.TestCase):
    """Nitter 热评/统计来源与 X 公开页兜底之间的调度逻辑。"""

    NITTER_PAGE = (
        '<div id="m" class="main-tweet">'
        '<div class="timeline-item " data-username="kuuu_Arcana">'
        '<a class="tweet-link" href="/kuuu_Arcana/status/999#m"></a>'
        '<img class="avatar round" src="/pic/profile_images%2F7%2Fkuuu_bigger.jpg" />'
        '<a class="fullname" href="/kuuu_Arcana" title="Kuuu">Kuuu</a>'
        '<div class="tweet-content media-body" dir="auto">main text</div>'
        '<div class="tweet-stats">'
        '<span class="tweet-stat"><div class="icon-container">'
        '<span class="icon-heart" title=""></span> 1,234</div></span>'
        "</div></div></div>"
        '<div id="r" class="replies">'
        '<div class="reply thread thread-line">'
        '<div class="timeline-item thread-last " data-username="fan">'
        '<a class="tweet-link" href="/fan/status/1000#m"></a>'
        '<a class="fullname" href="/fan" title="Fan">Fan</a>'
        '<div class="tweet-content media-body" dir="auto">nice art</div>'
        '<div class="tweet-stats">'
        '<span class="tweet-stat"><div class="icon-container">'
        '<span class="icon-heart" title=""></span> 9</div></span>'
        "</div></div></div></div>"
    )

    def setUp(self):
        # 提示日志按类属性做冷却，逐个测试重置以免互相影响。
        twitter_module.TwitterParser._nitter_hint_at = 0.0

    def test_nitter_success_skips_public_page(self):
        parser = TwitterParser(
            hot_comment_count=3,
            nitter_base_url="http://127.0.0.1:8585/",
        )
        parser._fetch_nitter_page = AsyncMock(return_value=self.NITTER_PAGE)
        parser._fetch_hot_comments = AsyncMock(return_value=[])

        extras = asyncio.run(
            parser._collect_thread_extras(object(), "999", "kuuu_Arcana")
        )

        self.assertEqual(len(extras["comments"]), 1)
        self.assertEqual(extras["comments"][0]["message"], "nice art")
        self.assertEqual(extras["stats_line"], "\u2764\ufe0f1,234")
        self.assertEqual(
            extras["author_avatar"],
            "https://pbs.twimg.com/profile_images/7/kuuu_400x400.jpg",
        )
        parser._fetch_hot_comments.assert_not_awaited()
        self.assertEqual(
            parser._fetch_nitter_page.await_args.args[1],
            "http://127.0.0.1:8585/kuuu_Arcana/status/999",
        )

    def test_nitter_failure_falls_back_to_public_page(self):
        parser = TwitterParser(
            hot_comment_count=2,
            nitter_base_url="http://a.test, http://b.test",
        )
        parser._fetch_nitter_page = AsyncMock(side_effect=OSError("connect refused"))
        parser._fetch_hot_comments = AsyncMock(
            return_value=[{"username": "x", "message": "fallback", "likes": 1}]
        )

        extras = asyncio.run(parser._collect_thread_extras(object(), "999"))

        self.assertEqual(extras["comments"][0]["message"], "fallback")
        self.assertEqual(extras["stats_line"], "")
        # 两个实例都要试过再兜底。
        self.assertEqual(parser._fetch_nitter_page.await_count, 2)
        parser._fetch_hot_comments.assert_awaited_once()

    def test_stats_survive_when_only_comments_missing(self):
        page = self.NITTER_PAGE.split('<div id="r"')[0]
        parser = TwitterParser(
            hot_comment_count=2,
            nitter_base_url="http://a.test",
        )
        parser._fetch_nitter_page = AsyncMock(return_value=page)
        parser._fetch_hot_comments = AsyncMock(return_value=[])

        extras = asyncio.run(parser._collect_thread_extras(object(), "999"))

        self.assertEqual(extras["stats_line"], "\u2764\ufe0f1,234")
        self.assertEqual(extras["comments"], [])
        # Nitter 已给出可用结果（这条推文本来就没有回复），不再白等 X 公开页。
        parser._fetch_hot_comments.assert_not_awaited()

    def test_public_page_skipped_only_when_nitter_usable(self):
        parser = TwitterParser(
            hot_comment_count=2,
            nitter_base_url="http://a.test",
        )
        # Nitter 页面完全解析不出内容时，仍然要回落到 X 公开页。
        parser._fetch_nitter_page = AsyncMock(return_value="<html></html>")
        parser._fetch_hot_comments = AsyncMock(return_value=[])

        asyncio.run(parser._collect_thread_extras(object(), "999"))

        parser._fetch_hot_comments.assert_awaited_once()

    def test_nitter_uses_session_without_public_only_guard(self):
        """自建 Nitter 是内网地址，不能被下载会话的 SSRF 防护拦下。"""
        parser = TwitterParser(
            hot_comment_count=1,
            nitter_base_url="http://127.0.0.1:8585",
        )
        parser._fetch_nitter_page = AsyncMock(return_value=self.NITTER_PAGE)

        async def run():
            connector = create_public_only_connector()
            async with aiohttp.ClientSession(connector=connector) as guarded:
                self.assertTrue(session_uses_public_only_connector(guarded))
                extras = await parser._fetch_nitter_extras(guarded, "999", "kuuu")
                used = parser._fetch_nitter_page.await_args.args[0]
                return extras, used, guarded

        extras, used, guarded = asyncio.run(run())

        self.assertEqual(len(extras.get("comments") or []), 1)
        self.assertIsNot(used, guarded)
        self.assertFalse(session_uses_public_only_connector(used))
        self.assertTrue(used.closed)

    def test_nitter_fetch_reaches_loopback_instance(self):
        """端到端验证：带 SSRF 防护的会话下，仍能抓到 127.0.0.1 上的 Nitter。"""
        from aiohttp import web

        async def handler(_request: web.Request) -> web.Response:
            return web.Response(text=self.NITTER_PAGE, content_type="text/html")

        async def run():
            app = web.Application()
            app.router.add_get("/{tail:.*}", handler)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = runner.addresses[0][1]
            parser = TwitterParser(
                hot_comment_count=3,
                nitter_base_url=f"http://127.0.0.1:{port}",
            )
            connector = create_public_only_connector()
            try:
                async with aiohttp.ClientSession(connector=connector) as guarded:
                    return await parser._fetch_nitter_extras(
                        guarded, "999", "kuuu_Arcana"
                    )
            finally:
                await runner.cleanup()

        extras = asyncio.run(run())

        self.assertEqual(len(extras.get("comments") or []), 1)
        self.assertEqual(extras["stats_line"], "\u2764\ufe0f1,234")

    def test_nitter_reuses_unguarded_session(self):
        parser = TwitterParser(
            hot_comment_count=1,
            nitter_base_url="http://a.test",
        )
        parser._fetch_nitter_page = AsyncMock(return_value=self.NITTER_PAGE)

        async def run():
            async with aiohttp.ClientSession() as plain:
                await parser._fetch_nitter_extras(plain, "999", "kuuu")
                return parser._fetch_nitter_page.await_args.args[0], plain

        used, plain = asyncio.run(run())

        self.assertIs(used, plain)

    def test_hint_logged_once_when_nitter_not_configured(self):
        parser = TwitterParser(hot_comment_count=2)
        parser._fetch_hot_comments = AsyncMock(return_value=[])

        with patch.object(twitter_module.logger, "info") as info:
            asyncio.run(parser._collect_thread_extras(object(), "1"))
            asyncio.run(parser._collect_thread_extras(object(), "2"))

        self.assertEqual(info.call_count, 1)

    def test_disabled_hot_comments_still_fetch_nitter_stats(self):
        parser = TwitterParser(
            hot_comment_count=0,
            nitter_base_url="http://a.test",
        )
        parser._fetch_nitter_page = AsyncMock(return_value=self.NITTER_PAGE)
        parser._fetch_hot_comments = AsyncMock(return_value=[])

        extras = asyncio.run(parser._collect_thread_extras(object(), "999"))

        self.assertEqual(extras["stats_line"], "\u2764\ufe0f1,234")
        self.assertEqual(extras["comments"], [])
        parser._fetch_hot_comments.assert_not_awaited()

    def test_local_nitter_bypasses_parse_proxy(self):
        parser = TwitterParser(
            use_parse_proxy=True,
            proxy_url="http://127.0.0.1:7890",
            nitter_base_url="http://127.0.0.1:8585, https://nitter.example",
        )

        self.assertIsNone(parser._nitter_proxy("http://127.0.0.1:8585"))
        self.assertIsNone(parser._nitter_proxy("http://192.168.1.9:8585"))
        self.assertIsNone(parser._nitter_proxy("http://172.20.0.3:8585"))
        self.assertEqual(
            parser._nitter_proxy("https://nitter.example"),
            "http://127.0.0.1:7890",
        )

    def test_parse_publishes_stats_line_and_avatar_fallback(self):
        parser = TwitterParser(
            hot_comment_count=1,
            nitter_base_url="http://127.0.0.1:8585",
        )
        parser._fetch_media_info = AsyncMock(
            return_value={
                "images": [],
                "videos": [],
                "text": "main text",
                "title": "",
                "author": "Kuuu(@kuuu_Arcana)",
                "avatar_url": "",
                "timestamp": "2026-08-26 11:00:00",
            }
        )
        parser._fetch_nitter_page = AsyncMock(return_value=self.NITTER_PAGE)

        result = asyncio.run(
            parser.parse(object(), "https://x.com/kuuu_Arcana/status/999")
        )

        self.assertEqual(result["stats_line"], "\u2764\ufe0f1,234")
        self.assertEqual(
            result["avatar_url"],
            "https://pbs.twimg.com/profile_images/7/kuuu_400x400.jpg",
        )
        self.assertEqual(len(result.get("hot_comments") or []), 1)


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
