import asyncio
import copy
import json
import unittest
from http.cookies import SimpleCookie
from unittest.mock import AsyncMock

from nova_core.parser.platform.niconico import NiconicoParser

WATCH_RESPONSE = {
    "meta": {"status": 200},
    "data": {
        "video": {
            "id": "sm9",
            "title": "陰陽師",
            "description": "before<br>after &amp; end",
            "registeredAt": "2007-03-06T00:33:00+09:00",
            "duration": 319,
            "thumbnail": {"ogp": "https://img.cdn.nimg.jp/thumb.jpg"},
        },
        "owner": {
            "nickname": "Nico Author",
            "avatar_url": "https://img.cdn.nimg.jp/avatar.jpg",
        },
        "client": {"watchTrackId": "watch-track"},
        "media": {
            "domand": {
                "accessRightKey": "watch-key",
                "videos": [
                    {"id": "video-low", "qualityLevel": 1, "isAvailable": True},
                    {"id": "video-high", "qualityLevel": 9, "isAvailable": True},
                    {
                        "id": "video-unavailable",
                        "qualityLevel": 10,
                        "isAvailable": False,
                    },
                ],
                "audios": [{"id": "audio-aac", "qualityLevel": 1, "isAvailable": True}],
            }
        },
    },
}
ACCESS_RESPONSE = {
    "meta": {"status": 200},
    "data": {"contentUrl": "https://delivery.domand.nicovideo.jp/master.m3u8"},
}


class NiconicoParserTests(unittest.IsolatedAsyncioTestCase):
    def test_supported_links_are_deduplicated_in_input_order(self):
        parser = NiconicoParser()
        self.assertEqual(
            parser.extract_links(
                "https://nico.ms/sm9，重发 https://www.nicovideo.jp/watch/sm9?ref=share "
                "https://sp.nicovideo.jp/watch/so123。https://www.nicovideo.jp/shorts/ss456"
            ),
            [
                "https://nico.ms/sm9",
                "https://sp.nicovideo.jp/watch/so123",
                "https://www.nicovideo.jp/shorts/ss456",
            ],
        )

    def test_domain_and_path_validation(self):
        parser = NiconicoParser()
        for url in (
            "https://nicovideo.jp.evil.com/watch/sm9",
            "https://user:secret@www.nicovideo.jp/watch/sm9",
            "https://www.nicovideo.jp:8080/watch/sm9",
            "https://www.nicovideo.jp/mylist/9",
            "https://www.nicovideo.jp/watch/sm9/more",
            "file://www.nicovideo.jp/watch/sm9",
        ):
            with self.subTest(url=url):
                self.assertFalse(parser.can_parse(url))

    async def test_guest_domand_access_preserves_cdn_cookie_and_proxy(self):
        parser = NiconicoParser(use_proxy=True, proxy_url="http://proxy:8080")
        parser._request_json = AsyncMock(
            side_effect=[
                (copy.deepcopy(WATCH_RESPONSE), {}),
                (copy.deepcopy(ACCESS_RESPONSE), {"domand_bid": "cdn-cookie"}),
            ]
        )
        result = await parser.parse(None, "https://nico.ms/sm9")
        self.assertEqual(result["title"], "陰陽師")
        self.assertEqual(result["desc"], "before\nafter & end")
        self.assertEqual(
            result["video_urls"],
            [["m3u8:https://delivery.domand.nicovideo.jp/master.m3u8"]],
        )
        self.assertTrue(result["video_force_download"])
        self.assertTrue(result["use_video_proxy"])
        self.assertEqual(result["video_headers"]["Cookie"], "domand_bid=cdn-cookie")
        self.assertNotIn("Cookie", result["image_headers"])
        calls = parser._request_json.await_args_list
        self.assertIn("/v3_guest/sm9", calls[0].args[2])
        self.assertEqual(calls[0].kwargs["headers"]["Accept"], "*/*")
        self.assertEqual(
            calls[1].kwargs["body"], {"outputs": [["video-high", "audio-aac"]]}
        )
        self.assertEqual(calls[1].kwargs["headers"]["X-Access-Right-Key"], "watch-key")
        self.assertEqual(
            calls[1].kwargs["headers"]["Accept"], "application/json;charset=utf-8"
        )
        self.assertEqual(calls[1].kwargs["params"], {"actionTrackId": "watch-track"})

    async def test_login_cookie_never_leaks_to_media_headers(self):
        parser = NiconicoParser(cookie="user_session=private; other=value")
        parser._request_json = AsyncMock(
            side_effect=[
                (copy.deepcopy(WATCH_RESPONSE), {}),
                (copy.deepcopy(ACCESS_RESPONSE), {"domand_bid": "cdn-cookie"}),
            ]
        )
        result = await parser.parse(None, "https://www.nicovideo.jp/watch/sm9")
        first = parser._request_json.await_args_list[0]
        self.assertIn("/v3/sm9", first.args[2])
        self.assertEqual(first.kwargs["headers"]["Cookie"], parser.cookie)
        self.assertNotIn("user_session", str(result))

    async def test_text_only_does_not_request_playback_access(self):
        parser = NiconicoParser(include_media=False)
        parser._request_json = AsyncMock(
            return_value=(copy.deepcopy(WATCH_RESPONSE), {})
        )
        result = await parser.parse(None, "https://www.nicovideo.jp/watch/sm9")
        self.assertEqual(result["title"], "陰陽師")
        self.assertEqual(result["video_urls"], [])
        parser._request_json.assert_awaited_once()

    async def test_access_right_created_status_is_success(self):
        access = copy.deepcopy(ACCESS_RESPONSE)
        access["meta"]["status"] = 201
        parser = NiconicoParser()
        parser._request_json = AsyncMock(
            side_effect=[(copy.deepcopy(WATCH_RESPONSE), {}), (access, {})]
        )
        result = await parser.parse(None, "https://www.nicovideo.jp/watch/sm9")
        self.assertEqual(len(result["video_urls"]), 1)

    async def test_private_video_keeps_available_metadata_and_explains_restriction(
        self,
    ):
        response = copy.deepcopy(WATCH_RESPONSE)
        response["meta"]["status"] = 403
        response["data"]["reasonCode"] = "CHANNEL_MEMBER_ONLY"
        parser = NiconicoParser()
        parser._request_json = AsyncMock(return_value=(response, {}))
        result = await parser.parse(None, "https://www.nicovideo.jp/watch/sm9")
        self.assertEqual(result["title"], "陰陽師")
        self.assertEqual(result["video_urls"], [])
        self.assertIn("频道会员", result["access_message"])
        self.assertFalse(result["can_access_full_video"])
        parser._request_json.assert_awaited_once()

    async def test_deleted_video_is_a_specific_error(self):
        parser = NiconicoParser()
        parser._request_json = AsyncMock(
            return_value=(
                {
                    "meta": {"status": 403},
                    "data": {"reasonCode": "ADMINISTRATOR_DELETE_VIDEO"},
                },
                {},
            )
        )
        with self.assertRaisesRegex(ValueError, "已被删除"):
            await parser.parse(None, "https://www.nicovideo.jp/watch/sm9")

    async def test_no_access_key_does_not_attempt_access_right_request(self):
        response = copy.deepcopy(WATCH_RESPONSE)
        del response["data"]["media"]["domand"]["accessRightKey"]
        parser = NiconicoParser()
        parser._request_json = AsyncMock(return_value=(response, {}))
        result = await parser.parse(None, "https://www.nicovideo.jp/watch/sm9")
        self.assertEqual(result["video_urls"], [])
        self.assertIn("Cookie", result["access_message"])
        parser._request_json.assert_awaited_once()

    async def test_foreign_content_url_is_rejected_before_cookie_use(self):
        for url in (
            "https://delivery.domand.nicovideo.jp.evil.com/master.m3u8",
            "http://delivery.domand.nicovideo.jp/master.m3u8",
            "https://user:secret@delivery.domand.nicovideo.jp/master.m3u8",
            "https://127.0.0.1/master.m3u8",
        ):
            parser = NiconicoParser()
            parser._request_json = AsyncMock(
                side_effect=[
                    (copy.deepcopy(WATCH_RESPONSE), {}),
                    (
                        {"meta": {"status": 200}, "data": {"contentUrl": url}},
                        {"domand_bid": "private"},
                    ),
                ]
            )
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "CDN"):
                await parser.parse(None, "https://www.nicovideo.jp/watch/sm9")

    async def test_cancellation_propagates(self):
        parser = NiconicoParser()
        parser._request_json = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await parser.parse(None, "https://www.nicovideo.jp/watch/sm9")

    def test_optional_metadata_objects_have_safe_defaults(self):
        response = copy.deepcopy(WATCH_RESPONSE)
        response["data"]["owner"] = ["invalid"]
        response["data"]["channel"] = "invalid"
        response["data"]["video"]["thumbnail"] = ["invalid"]
        response["data"]["video"]["count"] = None
        result = NiconicoParser._metadata("sm9", response["data"])
        self.assertEqual(result["title"], "陰陽師")
        self.assertEqual(result["author"], "")
        self.assertEqual(result["avatar_url"], "")
        self.assertEqual(result["video_cover_urls"], [])
        self.assertEqual(result["stats"], {"views": 0, "likes": 0, "comments": 0})
        self.assertEqual(NiconicoParser._metadata("sm9", {"video": []})["title"], "")

    def test_malformed_restriction_objects_do_not_hide_the_error(self):
        for response in (
            {"meta": [], "data": []},
            {"meta": {"status": 403}, "data": "invalid"},
            {"meta": None, "data": {"reasonCode": "CHANNEL_MEMBER_ONLY"}},
        ):
            with self.subTest(response=response):
                self.assertTrue(NiconicoParser._restriction_message(response))

    async def test_malformed_watch_objects_raise_descriptive_errors(self):
        for response in (
            [],
            {"meta": [], "data": {}},
            {"meta": {"status": 200}, "data": "invalid"},
            {"meta": {"status": 200}, "data": {"video": []}},
        ):
            parser = NiconicoParser()
            parser._request_json = AsyncMock(return_value=(response, {}))
            with (
                self.subTest(response=response),
                self.assertRaisesRegex(ValueError, "niconico watch API"),
            ):
                await parser.parse(None, "https://www.nicovideo.jp/watch/sm9")

    async def test_missing_or_malformed_stream_objects_keep_metadata_and_proxy(self):
        for branch, field, value in (
            ("data", "media", []),
            ("media", "domand", []),
            ("domand", "videos", None),
            ("domand", "audios", {}),
            ("data", "client", "invalid"),
            ("domand", "accessRightKey", ["invalid"]),
            ("domand", "videos", [{"id": [], "isAvailable": True}]),
        ):
            response = copy.deepcopy(WATCH_RESPONSE)
            data = response["data"]
            target = {
                "data": data,
                "media": data["media"],
                "domand": data["media"]["domand"],
            }[branch]
            target[field] = value
            parser = NiconicoParser(use_proxy=True, proxy_url="http://proxy:8080")
            parser._request_json = AsyncMock(return_value=(response, {}))
            with self.subTest(branch=branch, field=field):
                result = await parser.parse(None, "https://www.nicovideo.jp/watch/sm9")
                self.assertEqual(result["title"], "陰陽師")
                self.assertEqual(result["access_status"], "restricted")
                self.assertEqual(result["video_urls"], [])
                self.assertTrue(result["use_image_proxy"])
                self.assertTrue(result["use_video_proxy"])
                self.assertEqual(result["proxy_url"], "http://proxy:8080")
                parser._request_json.assert_awaited_once()

    async def test_restricted_watch_metadata_keeps_image_proxy(self):
        response = copy.deepcopy(WATCH_RESPONSE)
        response["meta"]["status"] = 403
        response["data"]["reasonCode"] = "CHANNEL_MEMBER_ONLY"
        parser = NiconicoParser(use_proxy=True, proxy_url="http://proxy:8080")
        parser._request_json = AsyncMock(return_value=(response, {}))
        result = await parser.parse(None, "https://www.nicovideo.jp/watch/sm9")
        self.assertEqual(result["access_status"], "restricted")
        self.assertTrue(result["use_image_proxy"])
        self.assertEqual(result["proxy_url"], "http://proxy:8080")

    async def test_malformed_access_right_objects_raise_descriptive_errors(self):
        for access in (
            [],
            {"meta": [], "data": {}},
            {"meta": {"status": 200}, "data": "invalid"},
        ):
            parser = NiconicoParser()
            parser._request_json = AsyncMock(
                side_effect=[(copy.deepcopy(WATCH_RESPONSE), {}), (access, {})]
            )
            with self.subTest(access=access), self.assertRaises(ValueError):
                await parser.parse(None, "https://www.nicovideo.jp/watch/sm9")

    async def test_api_requests_disable_redirects_and_parse_split_utf8(self):
        class Content:
            async def iter_chunked(self, _size):
                payload = json.dumps(WATCH_RESPONSE, ensure_ascii=False).encode()
                for index in range(0, len(payload), 11):
                    yield payload[index : index + 11]

        class Response:
            status = 200
            content = Content()
            cookies = SimpleCookie("domand_bid=opaque")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class Session:
            def request(self, *args, **kwargs):
                self.kwargs = kwargs
                return Response()

        parser = NiconicoParser()
        session = Session()
        data, cookies = await parser._request_json(
            session,
            "GET",
            "https://www.nicovideo.jp/api/watch/v3_guest/sm9",
            headers={},
        )
        self.assertEqual(data["data"]["video"]["title"], "陰陽師")
        self.assertEqual(cookies, {"domand_bid": "opaque"})
        self.assertFalse(session.kwargs["allow_redirects"])


if __name__ == "__main__":
    unittest.main()
