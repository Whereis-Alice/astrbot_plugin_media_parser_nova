"""微博公开正文、移动分享、混合媒体与访问限制回归测试。"""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock

from nova_core.parser.platform.weibo import WeiboParser
from nova_core.parser.utils import SkipParse


class Response:
    def __init__(self, data=None, status=200, text="", location=""):
        self.data = data
        self.status = status
        self.body = text
        self.headers = {"Location": location} if location else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self, **_kwargs):
        if isinstance(self.data, Exception):
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


if __name__ == "__main__":
    unittest.main()
