import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from nova_core.parser.platform.steam import SteamParser


class _Stream:
    def __init__(self, raw):
        self.raw = raw

    async def iter_chunked(self, _size):
        yield self.raw


class _Response:
    def __init__(self, payload, status=200):
        self.status = status
        self.content = _Stream(json.dumps(payload).encode())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _Session:
    def __init__(self, payload, status=200):
        self.response = _Response(payload, status)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class SteamParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = SteamParser()

    @staticmethod
    def app():
        return {
            "name": "Example &amp; Game",
            "short_description": "<p>游戏介绍</p>",
            "developers": ["Studio", None],
            "is_free": False,
            "price_overview": {"final_formatted": "¥ 18.00"},
            "release_date": {"date": "2026 年 1 月 1 日", "coming_soon": False},
            "genres": [{"description": "角色扮演"}],
            "header_image": "https://shared.akamai.steamstatic.com/header.jpg",
            "screenshots": [
                {"path_full": "https://shared.akamai.steamstatic.com/screen.jpg"}
            ],
            "movies": [
                {
                    "hls_h264": "https://video.akamai.steamstatic.com/hls_master.m3u8?t=1",
                    "thumbnail": "https://shared.akamai.steamstatic.com/movie.jpg",
                }
            ],
        }

    def test_current_hls_trailer_and_game_information(self):
        self.parser._fetch_app = AsyncMock(return_value=self.app())
        result = asyncio.run(
            self.parser.parse(
                None, "https://store.steampowered.com/app/570/Dota_2/?snr=1"
            )
        )
        self.assertEqual(result["title"], "Example & Game")
        self.assertEqual(result["author"], "Studio")
        self.assertIn("价格：¥ 18.00", result["desc"])
        self.assertEqual(
            result["video_urls"],
            [["m3u8:https://video.akamai.steamstatic.com/hls_master.m3u8?t=1"]],
        )
        self.assertEqual(
            result["video_cover_urls"],
            [["https://shared.akamai.steamstatic.com/movie.jpg"]],
        )
        self.assertTrue(result["video_force_download"])
        self.assertEqual(len(result["image_urls"]), 2)

    def test_old_mp4_sources_are_kept_with_hls_fallback(self):
        app = self.app()
        app["movies"][0]["mp4"] = {
            "max": "https://cdn.akamai.steamstatic.com/max.mp4",
            "480": "https://cdn.akamai.steamstatic.com/480.mp4",
        }
        result = self.parser._build_metadata("570", app)
        self.assertEqual(
            result["video_urls"][0][:2],
            [
                "https://cdn.akamai.steamstatic.com/max.mp4",
                "https://cdn.akamai.steamstatic.com/480.mp4",
            ],
        )
        self.assertTrue(result["video_urls"][0][2].startswith("m3u8:"))

    def test_limits_can_disable_screenshots_and_trailers(self):
        result = SteamParser(max_screenshots=0, max_trailers=0)._build_metadata(
            "570", self.app()
        )
        self.assertEqual(len(result["image_urls"]), 1)
        self.assertEqual(result["video_urls"], [])
        self.assertFalse(result["video_force_download"])

    def test_legacy_webm_only_trailer_is_available(self):
        app = self.app()
        app["movies"] = [{"webm": {"max": "https://cdn.akamai.steamstatic.com/old.webm"}}]
        result = self.parser._build_metadata("570", app)
        self.assertEqual(result["video_urls"], [["https://cdn.akamai.steamstatic.com/old.webm"]])

    def test_default_limits_avoid_sending_every_trailer(self):
        app = self.app()
        app["movies"] += [
            {"hls_h264": f"https://video.akamai.steamstatic.com/{i}.m3u8"}
            for i in range(5)
        ]
        result = self.parser._build_metadata("570", app)
        self.assertEqual(len(result["video_urls"]), 1)

    def test_official_links_and_short_links_are_deduplicated(self):
        text = "https://s.team/a/570，看看 https://store.steampowered.com/app/570/Dota_2/ https://steamcommunity.com/app/730/"
        self.assertEqual(
            self.parser.extract_links(text),
            ["https://s.team/a/570", "https://steamcommunity.com/app/730/"],
        )
        self.assertTrue(
            self.parser.can_parse("https://store.steampowered.com/agecheck/app/570/")
        )

    def test_untrusted_and_unsupported_links_are_rejected(self):
        for url in (
            "https://store.steampowered.com.evil.test/app/570/",
            "https://evil.test/?url=https://store.steampowered.com/app/570/",
            "https://user:pass@store.steampowered.com/app/570/",
            "https://store.steampowered.com:444/app/570/",
            "http://127.0.0.1/app/570/",
            "https://store.steampowered.com/app/0/",
            "https://store.steampowered.com/app/570/reviews/foo",
            "https://store.steampowered.com/bundle/123/",
            "file://store.steampowered.com/app/570/",
        ):
            with self.subTest(url=url):
                self.assertFalse(self.parser.can_parse(url))

    def test_malformed_optional_fields_and_untrusted_media_do_not_crash(self):
        app = self.app()
        app.update(
            {
                "developers": "bad",
                "genres": [None, "bad"],
                "screenshots": [None, {"path_full": "https://evil.test/a.jpg"}],
                "movies": [
                    None,
                    {
                        "hls_h264": "https://user:pass@video.akamai.steamstatic.com/video.m3u8"
                    },
                ],
                "release_date": None,
            }
        )
        result = self.parser._build_metadata("570", app)
        self.assertEqual(result["author"], "")
        self.assertEqual(result["video_urls"], [])
        self.assertEqual(len(result["image_urls"]), 1)

    def test_proxy_and_region_are_used_for_official_api(self):
        parser = SteamParser(
            use_proxy=True,
            proxy_url="http://127.0.0.1:7890",
            language="english",
            country_code="us",
        )
        session = _Session({"570": {"success": True, "data": self.app()}})
        asyncio.run(parser.parse(session, "https://s.team/a/570"))
        call = session.calls[0][1]
        self.assertEqual(call["params"], {"appids": "570", "l": "english", "cc": "US"})
        self.assertEqual(call["proxy"], "http://127.0.0.1:7890")
        self.assertFalse(call["allow_redirects"])

    def test_unavailable_app_returns_a_clear_error(self):
        with self.assertRaisesRegex(RuntimeError, "不存在|下架|不可用"):
            asyncio.run(
                self.parser.parse(
                    _Session({"570": {"success": False}}), "https://s.team/a/570"
                )
            )

    def test_rate_limit_is_not_reported_as_valid_content(self):
        with self.assertRaisesRegex(RuntimeError, "频繁"):
            asyncio.run(
                self.parser.parse(_Session({}, status=429), "https://s.team/a/570")
            )


if __name__ == "__main__":
    unittest.main()
