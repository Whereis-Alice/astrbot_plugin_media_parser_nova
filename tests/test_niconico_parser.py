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

COMMENT_INFO = {
    "server": "https://public.nvcomment.nicovideo.jp",
    "threadKey": "official-thread-key",
    "params": {
        "targets": [
            {"id": "1173108780", "fork": "owner"},
            {"id": "1173108780", "fork": "main"},
            {"id": "1173108780", "fork": "easy"},
        ],
        "language": "ja-jp",
    },
}

COMMENT_RESPONSE = {
    "meta": {"status": 200},
    "data": {
        "threads": [
            {
                "fork": "owner",
                "comments": [
                    {"body": "/owner script", "postedAt": "2026-11-04T00:00:00Z"}
                ],
            },
            {
                "fork": "main",
                "comments": [
                    {
                        "id": "a",
                        "body": "older &amp; liked",
                        "vposMs": 125000,
                        "postedAt": "2026-11-03T10:00:00Z",
                        "score": -2200,
                        "nicoruCount": 7,
                        "userId": "anonymous-hash",
                    },
                    {
                        "id": "b",
                        "body": "newest",
                        "vposMs": 30000,
                        "postedAt": "2026-11-03T12:00:00Z",
                        "score": 0,
                        "nicoruCount": 0,
                    },
                    {
                        "id": "c",
                        "body": "same message",
                        "vposMs": 65000,
                        "postedAt": "2026-11-03T11:00:00Z",
                        "score": 100000,
                        "nicoruCount": 2,
                    },
                    {"body": "", "postedAt": "2026-11-04T00:00:00Z"},
                    {"body": "deleted", "isDeleted": True},
                    None,
                ],
            },
            {
                "fork": "easy",
                "comments": [
                    {
                        "body": "same message",
                        "postedAt": "2026-11-03T09:00:00Z",
                        "nicoruCount": 9,
                    }
                ],
            },
        ]
    },
}


class NiconicoParserTests(unittest.IsolatedAsyncioTestCase):
    def _watch_with_comment_info(self):
        response = copy.deepcopy(WATCH_RESPONSE)
        response["data"]["comment"] = {"nvComment": copy.deepcopy(COMMENT_INFO)}
        return response

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

    async def test_danmaku_disabled_does_not_request_comment_server(self):
        parser = NiconicoParser(hot_comment_count=0, include_media=False)
        parser._request_json = AsyncMock(
            return_value=(self._watch_with_comment_info(), {})
        )
        result = await parser.parse(None, "https://www.nicovideo.jp/watch/sm9")
        self.assertNotIn("hot_comments", result)
        parser._request_json.assert_awaited_once()

    async def test_text_only_can_select_official_danmaku(self):
        parser = NiconicoParser(hot_comment_count=3, include_media=False)
        parser._request_json = AsyncMock(
            side_effect=[
                (self._watch_with_comment_info(), {}),
                (copy.deepcopy(COMMENT_RESPONSE), {}),
            ]
        )
        result = await parser.parse(None, "https://www.nicovideo.jp/watch/sm9")
        self.assertEqual(result["comment_section_title"], "弹幕摘选")
        self.assertEqual(
            [comment["message"] for comment in result["hot_comments"]],
            ["newest", "same message", "older & liked"],
        )
        older = result["hot_comments"][2]
        self.assertEqual(older["likes"], 7)
        self.assertEqual(older["nicoru_count"], 7)
        self.assertEqual(older["score"], -2200)
        self.assertEqual(older["likes_label"], "Nicoる")
        self.assertEqual(older["username"], "弹幕用户")
        self.assertEqual(older["uid"], "")
        self.assertEqual(older["position_ms"], 125000)
        self.assertEqual(older["position_text"], "弹幕位置 02:05")
        self.assertEqual(older["comment_kind"], "danmaku")
        self.assertEqual(result["video_urls"], [])
        call = parser._request_json.await_args_list[1]
        self.assertEqual(
            call.args[2], "https://public.nvcomment.nicovideo.jp/v1/threads"
        )
        self.assertEqual(call.kwargs["body"]["threadKey"], "official-thread-key")
        self.assertEqual(
            [item["fork"] for item in call.kwargs["body"]["params"]["targets"]],
            ["main", "easy"],
        )
        self.assertEqual(
            call.kwargs["headers"]["Content-Type"], "text/plain;charset=UTF-8"
        )
        self.assertLessEqual(call.kwargs["timeout_seconds"], 10)
        self.assertLessEqual(call.kwargs["max_bytes"], 2 * 1024 * 1024)
        self.assertNotIn("Cookie", call.kwargs["headers"])

    async def test_comment_failure_does_not_interrupt_media_authorization(self):
        parser = NiconicoParser(hot_comment_count=3)
        parser._request_json = AsyncMock(
            side_effect=[
                (self._watch_with_comment_info(), {}),
                TimeoutError(),
                (copy.deepcopy(ACCESS_RESPONSE), {"domand_bid": "cookie"}),
            ]
        )
        result = await parser.parse(None, "https://www.nicovideo.jp/watch/sm9")
        self.assertEqual(len(result["video_urls"]), 1)
        self.assertNotIn("hot_comments", result)
        self.assertEqual(parser._request_json.await_count, 3)

    async def test_comment_request_cancellation_propagates(self):
        parser = NiconicoParser(hot_comment_count=3, include_media=False)
        parser._request_json = AsyncMock(
            side_effect=[
                (self._watch_with_comment_info(), {}),
                asyncio.CancelledError(),
            ]
        )
        with self.assertRaises(asyncio.CancelledError):
            await parser.parse(None, "https://www.nicovideo.jp/watch/sm9")

    async def test_comment_response_byte_limit_is_enforced_before_json_decode(self):
        class Content:
            async def iter_chunked(self, _size):
                yield b"123456"

        class Response:
            status = 200
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class Session:
            def request(self, *args, **kwargs):
                return Response()

        parser = NiconicoParser()
        with self.assertRaisesRegex(ValueError, "大小限制"):
            await parser._request_json(
                Session(),
                "POST",
                "https://public.nvcomment.nicovideo.jp/v1/threads",
                headers={},
                max_bytes=5,
                timeout_seconds=10,
            )

    async def test_comment_server_whitelist_blocks_foreign_or_credential_urls(self):
        for server in (
            "https://public.nvcomment.nicovideo.jp.evil.com",
            "https://user:key@public.nvcomment.nicovideo.jp",
            "https://public.nvcomment.nicovideo.jp:8080",
            "http://public.nvcomment.nicovideo.jp",
            "https://127.0.0.1",
            "https://public.nvcomment.nicovideo.jp/path",
            "https://public.nvcomment.nicovideo.jp?redirect=https://evil.com",
        ):
            response = self._watch_with_comment_info()
            response["data"]["comment"]["nvComment"]["server"] = server
            parser = NiconicoParser(hot_comment_count=3, include_media=False)
            parser._request_json = AsyncMock(return_value=(response, {}))
            with self.subTest(server=server):
                result = await parser.parse(None, "https://www.nicovideo.jp/watch/sm9")
                self.assertNotIn("hot_comments", result)
                parser._request_json.assert_awaited_once()

    async def test_malformed_comment_shapes_are_optional(self):
        for comment_info in (
            None,
            [],
            {**COMMENT_INFO, "threadKey": None},
            {**COMMENT_INFO, "params": []},
            {**COMMENT_INFO, "params": {"targets": None}},
        ):
            response = self._watch_with_comment_info()
            response["data"]["comment"]["nvComment"] = comment_info
            parser = NiconicoParser(hot_comment_count=3, include_media=False)
            parser._request_json = AsyncMock(return_value=(response, {}))
            with self.subTest(comment_info=comment_info):
                result = await parser.parse(None, "https://www.nicovideo.jp/watch/sm9")
                self.assertEqual(result["title"], "陰陽師")
                self.assertNotIn("hot_comments", result)
                parser._request_json.assert_awaited_once()

    def test_danmaku_normalizer_handles_null_threads_and_missing_numbers(self):
        for response in (
            {"data": None},
            {"data": {"threads": None}},
            {
                "data": {
                    "threads": [
                        None,
                        {"fork": [], "comments": []},
                        {"fork": "main", "comments": None},
                    ]
                }
            },
        ):
            with self.subTest(response=response):
                self.assertEqual(NiconicoParser._normalize_danmaku(response, 3), [])
        result = NiconicoParser._normalize_danmaku(
            {
                "data": {
                    "threads": [
                        {
                            "fork": "main",
                            "comments": [
                                {
                                    "body": "valid message",
                                    "nicoruCount": "invalid",
                                    "vposMs": "invalid",
                                    "score": [1],
                                }
                            ],
                        }
                    ]
                }
            },
            3,
        )
        self.assertEqual(result[0]["likes"], 0)
        self.assertIsNone(result[0]["position_ms"])
        self.assertEqual(result[0]["position_text"], "")
        self.assertIsNone(result[0]["score"])
        self.assertEqual(NiconicoParser._normalize_danmaku(COMMENT_RESPONSE, 0), [])

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
