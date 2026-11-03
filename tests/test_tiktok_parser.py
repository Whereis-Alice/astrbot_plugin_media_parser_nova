"""TikTok 页面兼容性、作品身份和请求预算回归测试。"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from nova_core.parser.platform import tiktok as tiktok_module
from nova_core.parser.platform.tiktok import TikTokParser, TikTokRedirectError


PAGE_URL = "https://www.tiktok.com/@example/video/123"


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


if __name__ == "__main__":
    unittest.main()
