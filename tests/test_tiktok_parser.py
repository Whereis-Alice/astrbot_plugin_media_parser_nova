"""TikTok 页面兼容性、作品身份和请求预算回归测试。"""

import gzip
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from nova_core.parser.platform import tiktok as tiktok_module
from nova_core.parser.platform.tiktok import TikTokParser, TikTokRedirectError


PAGE_URL = "https://www.tiktok.com/@example/video/123"


class CommentResponse:
    def __init__(self, data=None, status=200, stream_body=None):
        self.data = data
        self.status = status
        self.stream_body = stream_body
        self.content = self
        self.stream_bytes_yielded = 0
        self.closed = False
        self.headers = {}

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


def comment(cid="a", likes=10, text="公开评论", aweme_id="123"):
    return {
        "cid": cid, "aweme_id": aweme_id, "text": text,
        "digg_count": likes, "create_time": 1565097817,
        "user": {"uid": "99", "nickname": "昵称", "unique_id": "example",
                 "avatar_thumb": {"url_list": ["https://cdn.test/avatar.jpg"]}},
    }


class TikTokUrlTests(unittest.TestCase):
    def test_supports_video_photo_and_short_shares(self):
        parser = TikTokParser()
        for url in (PAGE_URL, "https://www.tiktok.com/@example/photo/123",
                    "https://vm.tiktok.com/ABC/", "https://vt.tiktok.com/ABC/",
                    "https://www.tiktok.com/t/ABC/", "https://m.tiktok.com/v/123.html"):
            with self.subTest(url=url):
                self.assertTrue(parser.can_parse(url))

    def test_rejects_spoofed_domains_and_profile_pages(self):
        parser = TikTokParser()
        for url in ("https://www.tiktok.com.evil.test/@example/video/123",
                    "https://user@www.tiktok.com/@example/video/123",
                    "https://www.tiktok.com:8080/@example/video/123",
                    "https://www.tiktok.com/@example"):
            self.assertFalse(parser.can_parse(url))
        with self.assertRaises(TikTokRedirectError):
            parser._trusted_redirect_target(PAGE_URL, "http://127.0.0.1/video/123")

    def test_target_item_is_not_replaced_by_recommendation(self):
        data = {"__DEFAULT_SCOPE__": None, "ItemModule": {"456": {
            "id": "456", "video": {"playAddr": "https://cdn.test/other.mp4"}
        }}}
        self.assertIsNone(TikTokParser._extract_tiktok_item_from_json(data, "123"))


class TikTokParseTests(unittest.IsolatedAsyncioTestCase):
    async def test_null_json_scope_does_not_hide_valid_sigi_state(self):
        parser = TikTokParser()
        parser.fetch_tiktok_oembed = AsyncMock(return_value=None)
        item = {"id": "123", "author": None, "desc": "hello", "video": {
            "playAddr": "https://cdn.test/video.mp4", "bitrateInfo": [None, "invalid"]
        }}
        html = (
            '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'
            '{"__DEFAULT_SCOPE__": null}</script>'
            '<script id="SIGI_STATE">' + json.dumps({"ItemModule": {"123": item}}) + '</script>'
        )
        result = await parser.fetch_tiktok_info(None, PAGE_URL, response_text=html)
        self.assertEqual(result["video_url_list"], ["https://cdn.test/video.mp4"])
        self.assertEqual(result["author"], "")

    async def test_photo_gallery_keeps_each_photo_and_ignores_background_video(self):
        parser = TikTokParser()
        result = parser._build_tiktok_result_from_item({
            "id": "123", "author": {}, "video": {"playAddr": "https://cdn.test/music.mp4"},
            "imagePost": {"images": [
                {"imageURL": {"urlList": ["https://cdn.test/a.jpg", "https://mirror.test/a.jpg"]}},
                {"imageURL": {"urlList": ["https://cdn.test/b.jpg"]}},
            ]},
        }, PAGE_URL)
        self.assertEqual(result["image_url_lists"], [
            ["https://cdn.test/a.jpg", "https://mirror.test/a.jpg"], ["https://cdn.test/b.jpg"]
        ])
        self.assertEqual(result["video_url_list"], [])

    async def test_cookie_is_used_for_page_requests_and_not_leaked_to_media(self):
        parser = TikTokParser(cookie="sessionid=private-value")
        self.assertEqual(parser.tiktok_headers["Cookie"], "sessionid=private-value")
        process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(
            b"page\n__CURL_RESPONSE_METADATA__:200\n\nhttps://www.tiktok.com/@example/video/123", b""
        )))
        with patch.object(tiktok_module.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)) as create:
            await parser._run_curl_page_request("curl", PAGE_URL, 5)
        self.assertIn("Cookie: sessionid=private-value", create.call_args.args)
        self.assertNotIn("Cookie", parser._build_result_headers("test-agent")["video_headers"])

    async def test_curl_retries_share_one_time_budget(self):
        parser = TikTokParser()
        clock = SimpleNamespace(now=0.0)
        budgets = []

        async def request(_curl, url, remaining):
            budgets.append(remaining)
            clock.now += 25.0
            return {"status": 200, "effective_url": url, "html": "Please wait..."}

        parser._run_curl_page_request = AsyncMock(side_effect=request)
        with patch.object(tiktok_module.shutil, "which", return_value="curl"), \
                patch.object(tiktok_module.asyncio, "get_running_loop", return_value=SimpleNamespace(time=lambda: clock.now)), \
                patch.object(tiktok_module.asyncio, "sleep", AsyncMock()):
            await parser.fetch_tiktok_page(PAGE_URL)
        self.assertEqual(budgets, [30.0, 5.0])

    async def test_curl_cancellation_kills_and_reaps_child_process(self):
        parser = TikTokParser()
        process = SimpleNamespace(returncode=None, kill=Mock(), communicate=AsyncMock(
            side_effect=[tiktok_module.asyncio.CancelledError(), (b"", b"")]
        ))
        with patch.object(tiktok_module.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
            with self.assertRaises(tiktok_module.asyncio.CancelledError):
                await parser._run_curl_page_request("curl", PAGE_URL, 5)
        process.kill.assert_called_once()
        self.assertEqual(process.communicate.await_count, 2)


class TikTokCommentTests(unittest.IsolatedAsyncioTestCase):
    def media_result(self, **extra):
        result = {
            "title": "视频", "display_url": PAGE_URL, "item_id": "123",
            "video_url_list": ["https://cdn.test/video.mp4"], "is_gallery": False,
        }
        result.update(extra)
        return result

    async def test_count_zero_does_not_make_extra_comment_requests(self):
        parser = TikTokParser(hot_comment_count=0)
        parser._parse_tiktok = AsyncMock(return_value=self.media_result())
        session = Mock()
        result = await parser.parse(session, PAGE_URL)
        session.get.assert_not_called()
        self.assertNotIn("hot_comments", result)

    async def test_official_comment_fields_are_normalized_and_limited(self):
        parser = TikTokParser(hot_comment_count=2, cookie="sessionid=fixture",
                              use_proxy=True, proxy_url="http://proxy.test:8080")
        items = [comment("a", 10), comment("b", 100, "高赞"), comment("a", 10),
                 comment("foreign", 10000, aweme_id="456"), comment("blank", 1000, "")]
        session = Mock(get=Mock(return_value=CommentResponse({"status_code": 0, "comments": items})))
        result = await parser._fetch_hot_comments(session, "123", PAGE_URL)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["message"], "高赞")
        self.assertEqual(result[0]["likes"], 100)
        self.assertEqual(result[0]["username"], "昵称(@example)")
        self.assertEqual(result[0]["uid"], "99")
        self.assertEqual(result[0]["avatar_url"], "https://cdn.test/avatar.jpg")
        self.assertTrue(result[0]["time"])
        kwargs = session.get.call_args.kwargs
        self.assertEqual(session.get.call_args.args[0], "https://www.tiktok.com/api/comment/list/")
        self.assertEqual(kwargs["headers"]["Cookie"], "sessionid=fixture")
        self.assertEqual(kwargs["proxy"], "http://proxy.test:8080")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertLessEqual(kwargs["timeout"].total, 15)

    async def test_missing_or_nonintegral_likes_stay_unknown(self):
        parser = TikTokParser(hot_comment_count=3)
        for raw in (None, "1.2K", 1.5, True, -1):
            normalized = parser._normalize_hot_comment_item(comment(likes=raw))
            self.assertIsNone(normalized["likes"])
            self.assertEqual(normalized["likes_text"], "未知")
        self.assertEqual(parser._normalize_hot_comment_item(comment(likes=0))["likes"], 0)

    async def test_comment_failure_keeps_media_result(self):
        parser = TikTokParser(hot_comment_count=3)
        parser._parse_tiktok = AsyncMock(return_value=self.media_result())
        session = Mock(get=Mock(return_value=CommentResponse({"status_code": 10204})))
        result = await parser.parse(session, PAGE_URL)
        self.assertEqual(result["video_urls"], [["https://cdn.test/video.mp4"]])
        self.assertNotIn("hot_comments", result)

    async def test_comment_cancelled_request_propagates(self):
        parser = TikTokParser(hot_comment_count=3)
        parser._parse_tiktok = AsyncMock(return_value=self.media_result())
        session = Mock(get=Mock(return_value=CommentResponse(tiktok_module.asyncio.CancelledError())))
        with self.assertRaises(tiktok_module.asyncio.CancelledError):
            await parser.parse(session, PAGE_URL)

    async def test_photo_gallery_attaches_comments_using_resolved_item_id(self):
        parser = TikTokParser(hot_comment_count=1)
        parser._parse_tiktok = AsyncMock(return_value=self.media_result(
            display_url="https://www.tiktok.com/@example/photo/123", is_gallery=True,
            image_url_lists=[["https://cdn.test/a.jpg"]], video_url_list=[],
        ))
        session = Mock(get=Mock(return_value=CommentResponse({"status_code": 0, "comments": [comment()]})))
        result = await parser.parse(session, "https://vm.tiktok.com/SHORT/")
        self.assertEqual(len(result["hot_comments"]), 1)
        self.assertEqual(result["image_urls"], [["https://cdn.test/a.jpg"]])
        self.assertEqual(session.get.call_args.kwargs["params"]["aweme_id"], "123")

    async def test_invalid_item_id_is_not_requested(self):
        parser = TikTokParser(hot_comment_count=3)
        session = Mock()
        self.assertEqual(await parser._fetch_hot_comments(session, "not-an-id"), [])
        session.get.assert_not_called()

    async def test_empty_comment_list_is_valid(self):
        parser = TikTokParser(hot_comment_count=3)
        session = Mock(get=Mock(return_value=CommentResponse({"status_code": 0, "comments": None})))
        self.assertEqual(await parser._fetch_hot_comments(session, "123"), [])

    async def test_comment_response_accepts_exact_limit(self):
        limit = tiktok_module.MAX_COMMENT_RESPONSE_BYTES
        response = CommentResponse(stream_body=b"{}" + b" " * (limit - 2))
        self.assertEqual(await TikTokParser._read_comment_json(response), {})

    async def test_oversize_decoded_comment_stream_stops_early_and_keeps_video(self):
        parser = TikTokParser(hot_comment_count=3)
        parser._parse_tiktok = AsyncMock(return_value=self.media_result())
        limit = tiktok_module.MAX_COMMENT_RESPONSE_BYTES
        response = CommentResponse(stream_body=b" " * (limit + 128 * 1024) + b"{}")
        # Compressed Content-Length cannot override the decoded byte budget.
        response.headers = {"Content-Length": "10", "Content-Encoding": "gzip"}
        session = Mock(get=Mock(return_value=response))
        result = await parser.parse(session, PAGE_URL)
        self.assertEqual(result["video_urls"], [["https://cdn.test/video.mp4"]])
        self.assertNotIn("hot_comments", result)
        self.assertLess(response.stream_bytes_yielded, len(response.stream_body))
        self.assertTrue(response.closed)

    async def test_invalid_comment_json_does_not_discard_video(self):
        parser = TikTokParser(hot_comment_count=3)
        parser._parse_tiktok = AsyncMock(return_value=self.media_result())
        session = Mock(get=Mock(return_value=CommentResponse(stream_body=b"<html>login page</html>")))
        result = await parser.parse(session, PAGE_URL)
        self.assertEqual(result["video_urls"], [["https://cdn.test/video.mp4"]])
        self.assertNotIn("hot_comments", result)

    async def test_broken_comment_stream_preserves_media_and_hides_payload_error(self):
        parser = TikTokParser(hot_comment_count=3)
        parser._parse_tiktok = AsyncMock(return_value=self.media_result())
        session = Mock(get=Mock(return_value=CommentResponse(aiohttp.ClientPayloadError("private-response-fragment"))))
        with patch.object(tiktok_module.logger, "warning") as warning:
            result = await parser.parse(session, PAGE_URL)
        self.assertTrue(result["video_urls"])
        self.assertNotIn("private-response-fragment", str(warning.call_args))

    async def test_real_gzip_response_is_limited_after_decompression(self):
        decoded = b" " * (tiktok_module.MAX_COMMENT_RESPONSE_BYTES + 1) + b"{}"
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
                    await TikTokParser._read_comment_json(response)


if __name__ == "__main__":
    unittest.main()
