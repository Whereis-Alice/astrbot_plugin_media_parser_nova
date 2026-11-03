"""微博公开正文、移动分享、混合媒体与访问限制回归测试。"""

import asyncio
import gzip
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from nova_core.parser.platform import weibo as weibo_module
from nova_core.parser.platform.weibo import WeiboParser
from nova_core.parser.utils import SkipParse


class Response:
    def __init__(self, data=None, status=200, text="", location="", stream_body=None):
        self.data = data
        self.status = status
        self.body = text
        self.headers = {"Location": location} if location else {}
        self.stream_body = stream_body
        self.content = self
        self.stream_bytes_yielded = 0
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True
        return False

    async def iter_chunked(self, size):
        if isinstance(self.data, BaseException):
            raise self.data
        body = self.stream_body if self.stream_body is not None else json.dumps(self.data).encode()
        for start in range(0, len(body), size):
            chunk = body[start:start + size]
            self.stream_bytes_yielded += len(chunk)
            yield chunk

    async def json(self, **_kwargs):
        if isinstance(self.data, BaseException):
            raise self.data
        return self.data

    async def text(self):
        return self.body


class WeiboUrlTests(unittest.TestCase):
    def test_common_mobile_and_short_share_forms(self):
        parser = WeiboParser()
        for url in ("https://m.weibo.cn/status/QdC5HtUjg", "https://m.weibo.cn/detail/123",
                    "https://t.cn/Abc123", "https://weibo.com/123/QdC5HtUjg",
                    "https://weibo.com/tv/show/1034:123", "https://video.weibo.com/show?fid=1034%3A123"):
            self.assertTrue(parser.can_parse(url), url)

    def test_extraction_trims_glued_text_and_preserves_case_sensitive_ids(self):
        parser = WeiboParser()
        self.assertEqual(parser.extract_links(
            "https://m.weibo.cn/status/AbC解析 https://m.weibo.cn/status/abc "
            "https://m.weibo.cn/status/AbC?share=1"
        ), ["https://m.weibo.cn/status/AbC", "https://m.weibo.cn/status/abc"])

    def test_rejects_unsupported_hosts_and_urls(self):
        parser = WeiboParser()
        for url in ("https://weibo.com.evil.test/123/AbC", "https://user@weibo.com/123/AbC",
                    "https://m.weibo.cn:80/status/123", "https://t.cn@127.0.0.1/a",
                    "https://weibo.com/123"):
            self.assertFalse(parser.can_parse(url), url)

    def test_media_is_not_duplicated_and_pic_ids_determine_order(self):
        parser = WeiboParser()
        data = {
            "pic_ids": ["b", "a"], "pic_infos": {
                "a": {"large": {"url": "https://cdn.test/a.jpg"}},
                "b": {"large": {"url": "https://cdn.test/b.jpg"}},
            },
            "pics": [{"large": {"url": "https://cdn.test/a.jpg"}}],
            "page_info": {"media_info": {"stream_url_hd": "//cdn.test/video.mp4"}},
        }
        self.assertEqual(parser._extract_media_urls(data), [
            "https://cdn.test/b.jpg", "https://cdn.test/a.jpg", "https://cdn.test/video.mp4"
        ])
        self.assertEqual(parser._extract_media_urls_m_weibo({"status": data}), parser._extract_media_urls(data))

    def test_current_playback_list_chooses_highest_available_bitrate(self):
        parser = WeiboParser()
        self.assertEqual(parser._extract_video_url_from_media_info({"playback_list": [
            None, {"play_info": "invalid"},
            {"play_info": {"url": "https://cdn.test/low.mp4", "bitrate": 500}},
            {"play_info": {"url": "//cdn.test/high.mp4", "bitrate": 2500}},
        ]}), "https://cdn.test/high.mp4")


class WeiboParseTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_only_mobile_post_is_a_valid_parse(self):
        parser = WeiboParser(cookie="SUB=local-session", use_proxy=True, proxy_url="http://proxy.test:8080")
        status = {"id": "123", "text": "hello<br>world", "user": {"id": 99, "screen_name": "author"}}
        session = Mock(get=Mock(return_value=Response({"ok": 1, "data": status})))
        result = await parser.parse(session, "https://m.weibo.cn/status/123")
        self.assertEqual(result["desc"], "hello world")
        self.assertEqual(result["video_urls"], [])
        self.assertEqual(result["platform"], "weibo")
        self.assertEqual(session.get.call_args.kwargs["headers"]["cookie"], "SUB=local-session")
        self.assertEqual(session.get.call_args.kwargs["proxy"], "http://proxy.test:8080")
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])

    async def test_failed_visitor_session_does_not_block_public_post(self):
        parser = WeiboParser()
        parser._get_visitor_cookies = AsyncMock(side_effect=RuntimeError("visitor unavailable"))
        session = Mock(get=Mock(return_value=Response({"id": "123", "text_raw": "public text"})))
        result = await parser.parse(session, "https://weibo.com/99/123")
        self.assertEqual(result["desc"], "public text")

    async def test_cancelled_visitor_request_propagates(self):
        parser = WeiboParser()
        parser._get_visitor_cookies = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await parser.parse(None, "https://weibo.com/99/123")

    async def test_long_text_is_loaded_from_extend_endpoint(self):
        parser = WeiboParser(cookie="SUB=test")
        session = Mock(get=Mock(side_effect=[
            Response({"ok": 1, "data": {"id": "123", "text": "summary", "isLongText": True}}),
            Response({"ok": 1, "data": {"longTextContent": "complete<br>post"}}),
        ]))
        result = await parser.parse(session, "https://m.weibo.cn/detail/123")
        self.assertEqual(result["desc"], "complete post")
        self.assertFalse(result.get("is_preview_only", False))

    async def test_unavailable_long_text_is_labeled_as_summary(self):
        parser = WeiboParser(cookie="SUB=test")
        session = Mock(get=Mock(side_effect=[
            Response({"ok": 1, "data": {"id": "123", "text": "summary", "isLongText": True}}),
            Response({"ok": 0, "msg": "login required"}),
        ]))
        result = await parser.parse(session, "https://m.weibo.cn/detail/123")
        self.assertTrue(result["is_preview_only"])
        self.assertIn("公开摘要", result["desc"])

    async def test_short_share_preserves_source_without_requesting_foreign_target(self):
        parser = WeiboParser(cookie="SUB=test")
        session = Mock(get=Mock(side_effect=[
            Response(status=302, location="https://m.weibo.cn/detail/123"),
            Response({"ok": 1, "data": {"id": "123", "text": "post"}}),
        ]))
        result = await parser.parse(session, "https://t.cn/Abc")
        self.assertEqual(result["source_url"], "https://t.cn/Abc")
        self.assertEqual(result["url"], "https://m.weibo.cn/detail/123")
        foreign = Mock(get=Mock(return_value=Response(status=302, location="http://127.0.0.1/admin")))
        with self.assertRaises(SkipParse):
            await parser.parse(foreign, "https://t.cn/Abc")
        self.assertEqual(foreign.get.call_count, 1)

    async def test_http_200_visitor_html_has_clear_login_error(self):
        parser = WeiboParser(cookie="SUB=test")
        response = Response(json.JSONDecodeError("not json", "<html>", 0))
        with self.assertRaisesRegex(RuntimeError, "登录或风控"):
            await parser._read_json(response)

    async def test_wrong_post_id_is_rejected(self):
        parser = WeiboParser(cookie="SUB=test")
        session = Mock(get=Mock(return_value=Response({"id": "456", "text_raw": "other post"})))
        with self.assertRaisesRegex(RuntimeError, "其他作品"):
            await parser.parse(session, "https://weibo.com/99/123")


class WeiboCommentTests(unittest.IsolatedAsyncioTestCase):
    def comments(self):
        return [{"id": "comment-1", "text": "公开热评", "like_counts": 27,
                 "created_at": "Thu Nov 13 21:18:29 +0800 2025",
                 "user": {"id": "88", "screen_name": "评论者",
                          "avatar_large": "https://cdn.test/avatar.jpg"}}]

    async def test_count_zero_does_not_make_comment_requests(self):
        parser = WeiboParser(hot_comment_count=0)
        session = Mock()
        self.assertEqual(await parser._fetch_hot_comments(session, "", "123"), [])
        session.get.assert_not_called()

    async def test_tcn_mobile_post_attaches_comments_with_resolved_numeric_id(self):
        parser = WeiboParser(hot_comment_count=1, cookie="SUB=fixture", use_proxy=True,
                             proxy_url="http://proxy.test:8080")
        session = Mock(get=Mock(side_effect=[
            Response(status=302, location="https://m.weibo.cn/status/AbC"),
            Response({"ok": 1, "data": {"id": "123", "mblogid": "AbC", "text": "正文",
                                       "user": {"id": "99", "screen_name": "作者"}}}),
            Response({"data": self.comments()}),
        ]))
        result = await parser.parse(session, "https://t.cn/SHARE")
        self.assertEqual(result["hot_comments"][0]["message"], "公开热评")
        self.assertEqual(result["hot_comments"][0]["likes"], 27)
        self.assertEqual(result["source_url"], "https://t.cn/SHARE")
        kwargs = session.get.call_args.kwargs
        self.assertEqual(kwargs["params"]["id"], "123")
        self.assertEqual(kwargs["params"]["uid"], "99")
        self.assertEqual(kwargs["headers"]["cookie"], "SUB=fixture")
        self.assertEqual(kwargs["proxy"], "http://proxy.test:8080")

    async def test_desktop_failure_falls_back_to_official_mobile_hotflow(self):
        parser = WeiboParser(hot_comment_count=2)
        session = Mock(get=Mock(side_effect=[
            Response(status=302), Response({"ok": 1, "data": {"data": self.comments()}}),
        ]))
        result = await parser._fetch_hot_comments(session, "SUB=fixture", "123")
        self.assertEqual(result[0]["username"], "评论者")
        self.assertEqual(result[0]["avatar_url"], "https://cdn.test/avatar.jpg")
        self.assertEqual(session.get.call_args.args[0], "https://m.weibo.cn/comments/hotflow")
        self.assertEqual(session.get.call_args.kwargs["params"]["mid"], "123")

    async def test_failed_comments_do_not_discard_post(self):
        parser = WeiboParser(hot_comment_count=2, cookie="SUB=fixture")
        session = Mock(get=Mock(side_effect=[
            Response({"id": "123", "text_raw": "正文"}), Response(status=302), Response(status=302),
        ]))
        result = await parser.parse(session, "https://weibo.com/99/123")
        self.assertEqual(result["desc"], "正文")
        self.assertNotIn("hot_comments", result)

    async def test_comment_cancellation_propagates(self):
        parser = WeiboParser(hot_comment_count=2, cookie="SUB=fixture")
        session = Mock(get=Mock(side_effect=[
            Response({"id": "123", "text_raw": "正文"}), Response(asyncio.CancelledError()),
        ]))
        with self.assertRaises(asyncio.CancelledError):
            await parser.parse(session, "https://weibo.com/99/123")

    async def test_two_comment_endpoints_share_total_timeout(self):
        parser = WeiboParser(hot_comment_count=2)
        clock = SimpleNamespace(now=0.0)
        responses = iter([Response(status=302), Response({"data": self.comments()})])

        def request(*_args, **_kwargs):
            clock.now += 10
            return next(responses)

        session = Mock(get=Mock(side_effect=request))
        with patch.object(weibo_module.asyncio, "get_running_loop", return_value=SimpleNamespace(time=lambda: clock.now)):
            await parser._fetch_hot_comments(session, "", "123")
        self.assertEqual([call.kwargs["timeout"].total for call in session.get.call_args_list], [15, 5])

    async def test_unknown_comment_likes_are_not_fabricated(self):
        parser = WeiboParser(hot_comment_count=2)
        for raw in (None, "1.2K", 1.5, True, -1):
            item = self.comments()[0]
            item["like_counts"] = raw
            normalized = parser._normalize_hot_comment_item(item)
            self.assertIsNone(normalized["likes"])
            self.assertEqual(normalized["likes_text"], "未知")

    async def test_comment_count_limit_is_respected(self):
        parser = WeiboParser(hot_comment_count=1)
        items = [*self.comments(), {"text": "高赞", "like_counts": 99, "user": {}}]
        session = Mock(get=Mock(return_value=Response({"data": items})))
        result = await parser._fetch_hot_comments(session, "", "123")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["message"], "高赞")

    async def test_comment_response_accepts_exact_limit(self):
        limit = weibo_module.MAX_COMMENT_RESPONSE_BYTES
        response = Response(stream_body=b"{}" + b" " * (limit - 2))
        self.assertEqual(await WeiboParser._read_comment_json(response), {})

    async def test_oversize_comment_streams_stop_early_and_keep_post(self):
        parser = WeiboParser(hot_comment_count=2, cookie="SUB=fixture")
        limit = weibo_module.MAX_COMMENT_RESPONSE_BYTES
        oversized = [Response(stream_body=b" " * (limit + 128 * 1024) + b"{}") for _ in range(2)]
        for response in oversized:
            response.headers = {"Content-Length": "10", "Content-Encoding": "gzip"}
        session = Mock(get=Mock(side_effect=[Response({"id": "123", "text_raw": "正文"}), *oversized]))
        result = await parser.parse(session, "https://weibo.com/99/123")
        self.assertEqual(result["desc"], "正文")
        self.assertNotIn("hot_comments", result)
        for response in oversized:
            self.assertLess(response.stream_bytes_yielded, len(response.stream_body))
            self.assertTrue(response.closed)

    async def test_invalid_comment_json_does_not_discard_post(self):
        parser = WeiboParser(hot_comment_count=2, cookie="SUB=fixture")
        session = Mock(get=Mock(side_effect=[Response({"id": "123", "text_raw": "正文"}),
            Response(stream_body=b"<html>login page</html>"), Response(stream_body=b"invalid-json")]))
        result = await parser.parse(session, "https://weibo.com/99/123")
        self.assertEqual(result["desc"], "正文")
        self.assertNotIn("hot_comments", result)

    async def test_broken_comment_stream_keeps_post_and_hides_payload_error(self):
        parser = WeiboParser(hot_comment_count=2, cookie="SUB=fixture")
        session = Mock(get=Mock(side_effect=[Response({"id": "123", "text_raw": "正文"}),
            Response(aiohttp.ClientPayloadError("private-response-fragment")), Response(status=302)]))
        with patch.object(weibo_module.logger, "warning") as warning:
            result = await parser.parse(session, "https://weibo.com/99/123")
        self.assertEqual(result["desc"], "正文")
        self.assertNotIn("private-response-fragment", str(warning.call_args))

    async def test_real_gzip_response_is_limited_after_decompression(self):
        decoded = b" " * (weibo_module.MAX_COMMENT_RESPONSE_BYTES + 1) + b"{}"
        compressed = gzip.compress(decoded)
        self.assertLess(len(compressed), 64 * 1024)

        async def handler(_request):
            return web.Response(body=compressed, headers={"Content-Encoding": "gzip"})

        app = web.Application()
        app.router.add_get("/", handler)
        async with TestServer(app) as server, aiohttp.ClientSession() as session:
            async with session.get(server.make_url("/")) as response:
                self.assertEqual(response.content_length, len(compressed))
                with self.assertRaisesRegex(RuntimeError, "5 MiB"):
                    await WeiboParser._read_comment_json(response)


if __name__ == "__main__":
    unittest.main()
