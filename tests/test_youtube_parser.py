"""YouTube 解析器的单元测试。

只覆盖纯函数部分（URL 识别、媒体流挑选、Innertube 响应字段提取），
样本按真实 player / next 响应裁剪，保留解析依赖的结构特征。
"""

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import unittest

from nova_core.parser.platform.youtube import (
    COOKIE_PLAYER_CLIENTS,
    DEFAULT_PLAYER_CLIENTS,
    INNERTUBE_CLIENTS,
    METADATA_PLAYER_CLIENTS,
    YouTubeParser,
    build_sapisid_authorization,
    build_youtube_stats_line,
    detect_youtube_login_state,
    extract_youtube_comment_count,
    extract_youtube_comments,
    extract_youtube_like_count,
    extract_youtube_links,
    extract_youtube_owner,
    extract_youtube_publish_date,
    extract_youtube_view_count,
    find_comment_continuation,
    parse_compact_number,
    parse_cookie_header,
    parse_watch_html,
    parse_youtube_identity,
    select_youtube_media,
    thumbnail_candidates,
)
from nova_core.parser.platform.youtube import _Deadline
from nova_core.parser.runtime_manager.youtube import (
    YouTubeCookieRuntime,
    collect_set_cookie_headers,
    normalize_cookie_input,
)

VID = "dQw4w9WgXcQ"


def _gated_next_payload(with_accessibility: bool = True) -> dict:
    """按被机器人门禁拦下的真实 next 响应裁剪出的样本。

    特征：likeCountEntity 只剩空壳，点赞数只能从无障碍文案或
    buttonViewModel.title 取；播放量只在 videoViewCountRenderer 里。
    """
    like_button = {
        "iconName": "LIKE",
        "title": "6.5K",
    }
    if with_accessibility:
        like_button["accessibilityText"] = (
            "like this video along with 6,550 other people"
        )
    return {
        "frameworkUpdates": {
            "entityBatchUpdate": {
                "mutations": [
                    {
                        "payload": {
                            "likeCountEntity": {
                                "key": "unset_like_count_entity_key"
                            }
                        }
                    }
                ]
            }
        },
        "contents": {
            "twoColumnWatchNextResults": {
                "results": {
                    "results": {
                        "contents": [
                            {
                                "videoPrimaryInfoRenderer": {
                                    "viewCount": {
                                        "videoViewCountRenderer": {
                                            "viewCount": {
                                                "simpleText": (
                                                    "238,963 views"
                                                )
                                            },
                                            "shortViewCount": {
                                                "simpleText": "238K views"
                                            },
                                            "originalViewCount": "0",
                                        }
                                    },
                                    "videoActions": {
                                        "menuRenderer": {
                                            "topLevelButtons": [
                                                {
                                                    "segmentedLikeDislikeButtonViewModel": {
                                                        "likeButtonViewModel": {
                                                            "buttonViewModel": like_button
                                                        },
                                                        "dislikeButtonViewModel": {
                                                            "buttonViewModel": {
                                                                "iconName": (
                                                                    "DISLIKE"
                                                                ),
                                                                "title": (
                                                                    "Dislike"
                                                                ),
                                                            }
                                                        },
                                                    }
                                                }
                                            ]
                                        }
                                    },
                                }
                            }
                        ]
                    }
                }
            }
        },
    }


class ParseIdentityTest(unittest.TestCase):
    """URL → 视频 ID 的识别与安全校验。"""

    def test_accepts_common_shapes(self):
        cases = [
            f"https://www.youtube.com/watch?v={VID}",
            f"https://youtube.com/watch?v={VID}&t=42s",
            f"http://m.youtube.com/watch?v={VID}",
            f"https://music.youtube.com/watch?v={VID}&list=RD",
            f"https://youtu.be/{VID}",
            f"https://youtu.be/{VID}?t=90",
            f"https://www.youtube.com/shorts/{VID}",
            f"https://www.youtube.com/live/{VID}",
            f"https://www.youtube.com/embed/{VID}?rel=0",
            f"https://www.youtube-nocookie.com/embed/{VID}",
            f"https://www.youtube.com/v/{VID}",
            f"www.youtube.com/watch?v={VID}",
        ]
        for url in cases:
            with self.subTest(url=url):
                self.assertEqual(parse_youtube_identity(url), VID)

    def test_attribution_link_is_unwrapped(self):
        url = (
            "https://www.youtube.com/attribution_link"
            f"?a=xyz&u=%2Fwatch%3Fv%3D{VID}%26feature%3Dshare"
        )
        self.assertEqual(parse_youtube_identity(url), VID)

    def test_rejects_unrelated_or_unsafe_urls(self):
        cases = [
            "",
            "   ",
            None,
            "https://www.bilibili.com/video/BV1xx411c7mD",
            "https://youtube.com.evil.example/watch?v=" + VID,
            "https://www.youtube.com/watch?v=tooshort",
            "https://www.youtube.com/@somechannel",
            "https://www.youtube.com/playlist?list=PL123456",
            f"https://user:pass@www.youtube.com/watch?v={VID}",
            f"https://www.youtube.com:8080/watch?v={VID}",
            f"ftp://www.youtube.com/watch?v={VID}",
        ]
        for url in cases:
            with self.subTest(url=url):
                self.assertIsNone(parse_youtube_identity(url))

    def test_thumbnail_candidates_are_ordered(self):
        covers = thumbnail_candidates(VID)
        self.assertTrue(covers[0].endswith("maxresdefault.jpg"))
        self.assertEqual(len(covers), 4)
        self.assertTrue(all(VID in item for item in covers))


class ExtractLinksTest(unittest.TestCase):
    """文本 → 链接列表。"""

    def test_dedupes_by_video_id(self):
        text = (
            f"看这个 https://youtu.be/{VID} "
            f"还有 https://www.youtube.com/watch?v={VID} "
            "以及 https://www.youtube.com/shorts/abcdefghijk"
        )
        links = extract_youtube_links(text)
        self.assertEqual(len(links), 2)
        self.assertEqual(parse_youtube_identity(links[0]), VID)
        self.assertEqual(parse_youtube_identity(links[1]), "abcdefghijk")

    def test_strips_chinese_tail_and_punctuation(self):
        cases = [
            f"https://youtu.be/{VID}媒体解析",
            f"（https://www.youtube.com/watch?v={VID}）",
            f"https://youtu.be/{VID}。",
            f"请解析 https://youtu.be/{VID}，谢谢",
        ]
        for text in cases:
            with self.subTest(text=text):
                links = extract_youtube_links(text)
                self.assertEqual(len(links), 1)
                self.assertEqual(parse_youtube_identity(links[0]), VID)

    def test_ignores_non_youtube_text(self):
        self.assertEqual(extract_youtube_links("没有链接的一句话"), [])
        self.assertEqual(extract_youtube_links(""), [])


def _fmt(**kwargs):
    """构造一条 streamingData format。"""
    return dict(kwargs)


class SelectMediaTest(unittest.TestCase):
    """streamingData → 下载地址。"""

    def _player(self, progressive=None, adaptive=None, hls=None):
        streaming = {}
        if progressive is not None:
            streaming["formats"] = progressive
        if adaptive is not None:
            streaming["adaptiveFormats"] = adaptive
        if hls is not None:
            streaming["hlsManifestUrl"] = hls
        return {"streamingData": streaming}

    def test_prefers_dash_pair_with_avc1_and_mp4a(self):
        player = self._player(
            progressive=[
                _fmt(
                    url="https://x/prog360",
                    mimeType='video/mp4; codecs="avc1.42001E, mp4a.40.2"',
                    height=360,
                    bitrate=500,
                ),
            ],
            adaptive=[
                _fmt(
                    url="https://x/vp9_1080",
                    mimeType='video/webm; codecs="vp9"',
                    height=1080,
                    bitrate=4000,
                ),
                _fmt(
                    url="https://x/avc_1080",
                    mimeType='video/mp4; codecs="avc1.640028"',
                    height=1080,
                    bitrate=3500,
                ),
                _fmt(
                    url="https://x/opus",
                    mimeType='audio/webm; codecs="opus"',
                    bitrate=130,
                ),
                _fmt(
                    url="https://x/aac",
                    mimeType='audio/mp4; codecs="mp4a.40.2"',
                    bitrate=128,
                ),
            ],
        )
        url, kind, height = select_youtube_media(player, max_height=1080)
        self.assertEqual(kind, "dash")
        self.assertEqual(height, 1080)
        self.assertEqual(url, "dash:https://x/avc_1080||https://x/aac")

    def test_max_height_caps_selection(self):
        player = self._player(
            adaptive=[
                _fmt(
                    url="https://x/v1080",
                    mimeType='video/mp4; codecs="avc1"',
                    height=1080,
                    bitrate=3500,
                ),
                _fmt(
                    url="https://x/v720",
                    mimeType='video/mp4; codecs="avc1"',
                    height=720,
                    bitrate=1800,
                ),
                _fmt(
                    url="https://x/aac",
                    mimeType='audio/mp4; codecs="mp4a.40.2"',
                    bitrate=128,
                ),
            ],
        )
        url, kind, height = select_youtube_media(player, max_height=720)
        self.assertEqual(kind, "dash")
        self.assertEqual(height, 720)
        self.assertIn("v720", url)

    def test_skips_signature_cipher_streams(self):
        player = self._player(
            progressive=[
                _fmt(
                    signatureCipher="s=abc&url=https://x/blocked",
                    mimeType='video/mp4; codecs="avc1, mp4a.40.2"',
                    height=720,
                ),
                _fmt(
                    url="https://x/plain360",
                    mimeType='video/mp4; codecs="avc1, mp4a.40.2"',
                    height=360,
                    bitrate=500,
                ),
            ],
        )
        url, kind, height = select_youtube_media(player)
        self.assertEqual(kind, "progressive")
        self.assertEqual(url, "https://x/plain360")
        self.assertEqual(height, 360)

    def test_progressive_requires_audio_track(self):
        player = self._player(
            progressive=[
                _fmt(
                    url="https://x/mute720",
                    mimeType='video/mp4; codecs="avc1.4d401f"',
                    height=720,
                    bitrate=1500,
                ),
            ],
        )
        url, kind, _height = select_youtube_media(player)
        self.assertEqual(url, "")
        self.assertEqual(kind, "none")

    def test_allow_dash_off_falls_back_to_progressive(self):
        player = self._player(
            progressive=[
                _fmt(
                    url="https://x/prog720",
                    mimeType='video/mp4; codecs="avc1, mp4a.40.2"',
                    height=720,
                    bitrate=1500,
                ),
            ],
            adaptive=[
                _fmt(
                    url="https://x/v1080",
                    mimeType='video/mp4; codecs="avc1"',
                    height=1080,
                    bitrate=3500,
                ),
                _fmt(
                    url="https://x/aac",
                    mimeType='audio/mp4; codecs="mp4a.40.2"',
                    bitrate=128,
                ),
            ],
        )
        url, kind, height = select_youtube_media(player, allow_dash=False)
        self.assertEqual(kind, "progressive")
        self.assertEqual(url, "https://x/prog720")
        self.assertEqual(height, 720)

    def test_hls_used_for_live(self):
        player = self._player(hls="https://x/master.m3u8")
        url, kind, _height = select_youtube_media(player)
        self.assertEqual(kind, "hls")
        self.assertEqual(url, "m3u8:https://x/master.m3u8")

    def test_video_only_is_last_resort(self):
        player = self._player(
            adaptive=[
                _fmt(
                    url="https://x/v480",
                    mimeType='video/mp4; codecs="avc1"',
                    height=480,
                    bitrate=900,
                ),
            ],
        )
        url, kind, height = select_youtube_media(player)
        self.assertEqual(kind, "video_only")
        self.assertEqual(url, "https://x/v480")
        self.assertEqual(height, 480)

    def test_empty_payload_is_safe(self):
        for payload in (None, {}, {"streamingData": None}, "junk"):
            with self.subTest(payload=payload):
                self.assertEqual(
                    select_youtube_media(payload), ("", "none", 0)
                )


class NumberFormatTest(unittest.TestCase):
    """紧凑计数解析与统计行拼装。"""

    def test_parse_compact_number(self):
        cases = {
            "1,234": 1234,
            "1.2K": 1200,
            "3.4M": 3400000,
            "2B": 2000000000,
            "1.5万": 15000,
            "2億": 200000000,
            "12345 likes": 12345,
            "": 0,
            "no digits": 0,
            4567: 4567,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_compact_number(raw), expected)

    def test_stats_line_skips_zero_entries(self):
        line = build_youtube_stats_line(123456, 0, 42)
        self.assertIn("12.3万", line)
        self.assertIn("42", line)
        self.assertNotIn("\U0001f44d", line)
        self.assertEqual(build_youtube_stats_line(0, 0, 0), "")

    def test_stats_line_order_is_views_likes_comments(self):
        line = build_youtube_stats_line(10, 20, 30)
        self.assertEqual(
            line, "\U0001f44010 \U0001f44d20 \U0001f4ac30"
        )


class NextPayloadTest(unittest.TestCase):
    """next 端点响应的字段提取。"""

    def _owner_payload(self):
        return {
            "contents": {
                "twoColumnWatchNextResults": {
                    "results": {
                        "contents": [
                            {
                                "videoSecondaryInfoRenderer": {
                                    "owner": {
                                        "videoOwnerRenderer": {
                                            "title": {
                                                "runs": [
                                                    {"text": "Rick Astley"}
                                                ]
                                            },
                                            "thumbnail": {
                                                "thumbnails": [
                                                    {
                                                        "url": "//i/ava48.jpg",
                                                        "width": 48,
                                                        "height": 48,
                                                    },
                                                    {
                                                        "url": (
                                                            "https://i/"
                                                            "ava176.jpg"
                                                        ),
                                                        "width": 176,
                                                        "height": 176,
                                                    },
                                                ]
                                            },
                                            "navigationEndpoint": {
                                                "browseEndpoint": {
                                                    "browseId": "UCabcdef"
                                                }
                                            },
                                        }
                                    }
                                }
                            }
                        ]
                    }
                }
            }
        }

    def test_extract_owner_picks_largest_avatar(self):
        name, avatar, channel_id = extract_youtube_owner(
            self._owner_payload()
        )
        self.assertEqual(name, "Rick Astley")
        self.assertEqual(avatar, "https://i/ava176.jpg")
        self.assertEqual(channel_id, "UCabcdef")

    def test_extract_owner_normalizes_protocol_relative_avatar(self):
        payload = {"avatar": {"thumbnails": [{"url": "//i/a.jpg"}]}}
        _name, avatar, _cid = extract_youtube_owner(payload)
        self.assertEqual(avatar, "https://i/a.jpg")

    def test_like_count_from_entity(self):
        payload = {
            "frameworkUpdates": {
                "entityBatchUpdate": {
                    "mutations": [
                        {
                            "payload": {
                                "likeCountEntity": {
                                    "likeCountIfIndifferentNumber": "1.7M",
                                    "likeCountIfLikedNumber": "1.7M",
                                }
                            }
                        }
                    ]
                }
            }
        }
        self.assertEqual(extract_youtube_like_count(payload), 1700000)

    def test_like_count_from_accessibility_text(self):
        payload = {
            "buttons": [
                {
                    "accessibilityText": "Share this video",
                },
                {
                    "accessibilityText": "1,234,567 likes",
                },
            ]
        }
        self.assertEqual(extract_youtube_like_count(payload), 1234567)

    def test_like_count_missing_returns_zero(self):
        self.assertEqual(extract_youtube_like_count({}), 0)

    def test_like_count_from_gated_next_payload(self):
        """门禁视频只给空壳 likeCountEntity，精确值在无障碍文案里。"""
        self.assertEqual(
            extract_youtube_like_count(_gated_next_payload()), 6550
        )

    def test_like_count_from_button_view_model(self):
        """连无障碍文案都没有时，退回新版 buttonViewModel 的 title。"""
        payload = _gated_next_payload(with_accessibility=False)
        self.assertEqual(extract_youtube_like_count(payload), 6500)

    def test_like_count_ignores_dislike_button(self):
        payload = {
            "segmentedLikeDislikeButtonViewModel": {
                "dislikeButtonViewModel": {
                    "buttonViewModel": {
                        "iconName": "DISLIKE",
                        "title": "42",
                    }
                }
            }
        }
        self.assertEqual(extract_youtube_like_count(payload), 0)

    def test_like_count_prefers_like_button_scope(self):
        """点赞按钮子树优先，外面的干扰文案不该被读到。"""
        payload = {
            "segmentedLikeDislikeButtonViewModel": {
                "likeButtonViewModel": {
                    "buttonViewModel": {
                        "iconName": "LIKE",
                        "title": "6.5K",
                    }
                }
            },
            "commentTeaser": {"accessibilityText": "111 likes"},
        }
        self.assertEqual(extract_youtube_like_count(payload), 6500)

    def test_like_count_prefers_exact_number_over_compact(self):
        payload = {
            "likeCountEntity": {
                "expandedLikeCountIfIndifferent": {"content": "19,355,277"},
                "likeCountIfIndifferent": {"content": "19M"},
            }
        }
        self.assertEqual(extract_youtube_like_count(payload), 19355277)

    def test_view_count_from_next_payload(self):
        self.assertEqual(
            extract_youtube_view_count(_gated_next_payload()), 238963
        )

    def test_view_count_falls_back_to_short_text(self):
        payload = {
            "videoViewCountRenderer": {
                "shortViewCount": {"simpleText": "238K views"},
                "originalViewCount": "0",
            }
        }
        self.assertEqual(extract_youtube_view_count(payload), 238000)

    def test_view_count_missing_returns_zero(self):
        self.assertEqual(extract_youtube_view_count({}), 0)

    def test_comment_count(self):
        payload = {
            "engagementPanels": [
                {
                    "commentsEntryPointHeaderRenderer": {
                        "commentCount": {"simpleText": "2.3M"}
                    }
                }
            ]
        }
        self.assertEqual(extract_youtube_comment_count(payload), 2300000)

    def test_find_comment_continuation_prefers_comment_section(self):
        payload = {
            "contents": [
                {
                    "itemSectionRenderer": {
                        "sectionIdentifier": "related-items",
                        "contents": [
                            {
                                "continuationItemRenderer": {
                                    "continuationEndpoint": {
                                        "continuationCommand": {
                                            "token": "RELATED"
                                        }
                                    }
                                }
                            }
                        ],
                    }
                },
                {
                    "itemSectionRenderer": {
                        "sectionIdentifier": "comment-item-section",
                        "contents": [
                            {
                                "continuationItemRenderer": {
                                    "continuationEndpoint": {
                                        "continuationCommand": {
                                            "token": "COMMENTS"
                                        }
                                    }
                                }
                            }
                        ],
                    }
                },
            ]
        }
        self.assertEqual(find_comment_continuation(payload), "COMMENTS")

    def test_find_comment_continuation_without_section_identifier(self):
        payload = {
            "engagementPanels": [
                {"commentsEntryPointHeaderRenderer": {"commentCount": {}}}
            ],
            "continuationCommand": {"token": "FALLBACK"},
        }
        self.assertEqual(find_comment_continuation(payload), "FALLBACK")

    def test_find_comment_continuation_absent(self):
        self.assertEqual(
            find_comment_continuation({"continuationCommand": {"token": "x"}}),
            "",
        )


class CommentExtractionTest(unittest.TestCase):
    """新旧两种评论结构的提取、去重与排序。"""

    def _entity(self, cid, name, text, likes, published="2 天前"):
        return {
            "payload": {
                "commentEntityPayload": {
                    "properties": {
                        "commentId": cid,
                        "content": {"content": text},
                        "publishedTime": published,
                    },
                    "author": {
                        "displayName": name,
                        "channelId": "UC" + cid,
                        "avatarThumbnailUrl": "//i/" + cid + ".jpg",
                    },
                    "toolbar": {"likeCountNotliked": likes},
                }
            }
        }

    def test_entity_format(self):
        payload = {
            "frameworkUpdates": {
                "entityBatchUpdate": {
                    "mutations": [
                        self._entity("c1", "阿离", "第一条", "12"),
                        self._entity("c2", "小明", "第二条", "3.4K"),
                        self._entity("c3", "路人", "第三条", "5"),
                    ]
                }
            }
        }
        comments = extract_youtube_comments(payload, limit=5)
        self.assertEqual(len(comments), 3)
        self.assertEqual(comments[0]["message"], "第二条")
        self.assertEqual(comments[0]["likes"], 3400)
        self.assertEqual(comments[0]["username"], "小明")
        self.assertEqual(comments[0]["avatar_url"], "https://i/c2.jpg")
        self.assertEqual(comments[0]["uid"], "UCc2")
        self.assertEqual(comments[0]["time"], "2 天前")
        self.assertEqual(
            [item["likes"] for item in comments], [3400, 12, 5]
        )

    def test_entity_limit_applied(self):
        payload = {
            "mutations": [
                self._entity("c" + str(i), "u" + str(i), "m" + str(i), str(i))
                for i in range(10)
            ]
        }
        self.assertEqual(len(extract_youtube_comments(payload, limit=3)), 3)
        self.assertEqual(extract_youtube_comments(payload, limit=0), [])

    def test_entity_duplicates_removed(self):
        payload = {
            "a": [self._entity("c1", "阿离", "同一条", "9")],
            "b": [self._entity("c1", "阿离", "同一条", "9")],
        }
        self.assertEqual(len(extract_youtube_comments(payload, limit=5)), 1)

    def test_legacy_renderer_format(self):
        payload = {
            "contents": [
                {
                    "commentThreadRenderer": {
                        "comment": {
                            "commentRenderer": {
                                "commentId": "old1",
                                "authorText": {"simpleText": "老用户"},
                                "authorExternalChannelId": "UCold",
                                "contentText": {
                                    "runs": [
                                        {"text": "旧版"},
                                        {"text": "结构"},
                                    ]
                                },
                                "voteCount": {"simpleText": "1.2K"},
                                "publishedTimeText": {"simpleText": "1 年前"},
                                "authorThumbnail": {
                                    "thumbnails": [
                                        {
                                            "url": "https://i/old.jpg",
                                            "width": 88,
                                            "height": 88,
                                        }
                                    ]
                                },
                            }
                        }
                    }
                }
            ]
        }
        comments = extract_youtube_comments(payload, limit=5)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["message"], "旧版结构")
        self.assertEqual(comments[0]["likes"], 1200)
        self.assertEqual(comments[0]["username"], "老用户")
        self.assertEqual(comments[0]["avatar_url"], "https://i/old.jpg")

    def test_empty_payload(self):
        self.assertEqual(extract_youtube_comments({}, limit=5), [])
        self.assertEqual(extract_youtube_comments(None, limit=5), [])


class WatchHtmlTest(unittest.TestCase):
    """watch 页面内嵌 JSON 的括号配对提取。"""

    def test_extracts_both_payloads(self):
        html = (
            "<html><script>var ytInitialPlayerResponse = "
            '{"videoDetails": {"title": "带 } 括号的标题", '
            '"author": "某人"}};'
            "</script><script>var ytInitialData = "
            '{"contents": {"ok": true}};</script></html>'
        )
        player, initial = parse_watch_html(html)
        self.assertIsInstance(player, dict)
        self.assertEqual(
            player["videoDetails"]["title"], "带 } 括号的标题"
        )
        self.assertEqual(initial["contents"]["ok"], True)

    def test_handles_escaped_quotes(self):
        html = (
            "ytInitialPlayerResponse = "
            '{"videoDetails": {"title": "He said \\"hi\\" {"}}'
        )
        player, _initial = parse_watch_html(html)
        self.assertIsInstance(player, dict)
        self.assertEqual(
            player["videoDetails"]["title"], 'He said "hi" {'
        )

    def test_missing_marker_returns_none(self):
        player, initial = parse_watch_html("<html>nothing</html>")
        self.assertIsNone(player)
        self.assertIsNone(initial)

    def test_malformed_json_returns_none(self):
        player, _initial = parse_watch_html(
            "ytInitialPlayerResponse = {not valid json}"
        )
        self.assertIsNone(player)


class ParserWiringTest(unittest.TestCase):
    """解析器构造参数的归一化。"""

    def test_client_list_normalization(self):
        parser = YouTubeParser(player_clients="tv, ios ; web")
        self.assertEqual(parser.player_clients, ("tv", "ios", "web"))

    def test_unknown_clients_dropped_and_deduped(self):
        parser = YouTubeParser(player_clients="ios,ios,android_bogus")
        self.assertEqual(parser.player_clients, ("ios",))

    def test_empty_client_list_falls_back_to_default(self):
        for raw in ("", "   ", "nope", None, 123, []):
            with self.subTest(raw=raw):
                parser = YouTubeParser(player_clients=raw)
                self.assertEqual(
                    parser.player_clients, DEFAULT_PLAYER_CLIENTS
                )

    def test_list_input_accepted(self):
        parser = YouTubeParser(player_clients=["WEB", "MWEB"])
        self.assertEqual(parser.player_clients, ("web", "mweb"))

    def test_budget_has_floor(self):
        self.assertEqual(
            YouTubeParser(total_budget_seconds=1).total_budget_seconds, 8.0
        )
        self.assertEqual(
            YouTubeParser(total_budget_seconds=0).total_budget_seconds, 45.0
        )

    def test_can_parse_and_extract_links_delegate(self):
        parser = YouTubeParser()
        self.assertTrue(parser.can_parse(f"https://youtu.be/{VID}"))
        self.assertFalse(parser.can_parse("https://example.com/a"))
        self.assertEqual(
            parser.extract_links(f"x https://youtu.be/{VID} y"),
            [f"https://youtu.be/{VID}"],
        )

    def test_max_height_normalized(self):
        self.assertEqual(YouTubeParser(max_height=-5).max_height, 0)
        self.assertEqual(YouTubeParser(max_height=720).max_height, 720)


class PublishDateTest(unittest.TestCase):
    """发布时间：player 的 microformat + next 的 dateText 双来源。"""

    def test_reads_microformat_publish_date(self):
        player = {
            "microformat": {
                "playerMicroformatRenderer": {
                    "publishDate": "2009-10-24T00:00:00-07:00",
                }
            }
        }
        self.assertEqual(extract_youtube_publish_date(player), "2009-10-24")

    def test_microformat_keeps_time_when_present(self):
        player = {
            "microformat": {
                "playerMicroformatRenderer": {
                    "uploadDate": "2024-03-05T14:30:00Z",
                }
            }
        }
        self.assertEqual(
            extract_youtube_publish_date(player), "2024-03-05 14:30"
        )

    def test_falls_back_to_next_date_text(self):
        # ios / android_vr / tv 的 player 响应没有 microformat。
        player = {"videoDetails": {"title": "t"}}
        next_payload = {
            "contents": {
                "videoPrimaryInfoRenderer": {
                    "dateText": {"simpleText": "Oct 24, 2009"},
                }
            }
        }
        self.assertEqual(
            extract_youtube_publish_date(player, next_payload), "2009-10-24"
        )

    def test_date_text_tolerates_prefix_and_full_month(self):
        for text, expect in (
            ("Premiered Oct 24, 2009", "2009-10-24"),
            ("Streamed live on October 4, 2021", "2021-10-04"),
            ("Sep. 1, 2020", "2020-09-01"),
        ):
            payload = {"dateText": {"simpleText": text}}
            self.assertEqual(
                extract_youtube_publish_date({}, payload), expect, text
            )

    def test_rejects_relative_and_bogus_dates(self):
        self.assertEqual(
            extract_youtube_publish_date(
                {}, {"dateText": {"simpleText": "2 days ago"}}
            ),
            "",
        )
        self.assertEqual(
            extract_youtube_publish_date(
                {}, {"dateText": {"simpleText": "Foo 99, 2009"}}
            ),
            "",
        )
        self.assertEqual(extract_youtube_publish_date({}, {}), "")
        self.assertEqual(extract_youtube_publish_date(None, None), "")


class CommentCountPanelTest(unittest.TestCase):
    """原生客户端的评论数在评论面板标题的 contextualInfo 里。"""

    @staticmethod
    def _panel(panel_id, title, contextual):
        return {
            "engagementPanels": [
                {
                    "engagementPanelSectionListRenderer": {
                        "panelIdentifier": panel_id,
                        "header": {
                            "engagementPanelTitleHeaderRenderer": {
                                "title": {"runs": [{"text": title}]},
                                "contextualInfo": {
                                    "runs": [{"text": contextual}]
                                },
                            }
                        },
                    }
                }
            ]
        }

    def test_reads_contextual_info_from_comments_panel(self):
        payload = self._panel(
            "engagement-panel-comments-section", "Comments", "2.4M"
        )
        self.assertEqual(extract_youtube_comment_count(payload), 2400000)

    def test_ignores_other_panels_contextual_info(self):
        payload = self._panel(
            "engagement-panel-macro-markers-description-chapters",
            "Chapters",
            "12",
        )
        self.assertEqual(extract_youtube_comment_count(payload), 0)

    def test_matches_by_title_when_panel_id_unknown(self):
        payload = self._panel("engagement-panel-unknown", "Comments", "530")
        self.assertEqual(extract_youtube_comment_count(payload), 530)

    def test_entry_point_header_still_wins(self):
        payload = self._panel(
            "engagement-panel-comments-section", "Comments", "2.4M"
        )
        payload["commentsEntryPointHeaderRenderer"] = {
            "commentCount": {"simpleText": "1,234"}
        }
        self.assertEqual(extract_youtube_comment_count(payload), 1234)

    def test_continuation_fallback_accepts_comments_panel(self):
        payload = self._panel(
            "engagement-panel-comments-section", "Comments", "2.4M"
        )
        payload["continuations"] = {
            "continuationCommand": {"token": "TOKEN"}
        }
        self.assertEqual(find_comment_continuation(payload), "TOKEN")


class CookieAuthTest(unittest.TestCase):
    """Cookie 鉴权：SAPISIDHASH 生成与按客户端分发。"""

    COOKIE = "SID=abc; SAPISID=SECRET; HSID=zzz"

    def test_parse_cookie_header(self):
        self.assertEqual(
            parse_cookie_header("a=1; b = 2 ;;bad;c="),
            {"a": "1", "b": "2", "c": ""},
        )

    def test_parse_cookie_header_empty(self):
        self.assertEqual(parse_cookie_header(""), {})
        self.assertEqual(parse_cookie_header("nonsense"), {})

    def test_sapisidhash_matches_reference_algorithm(self):
        origin = "https://www.youtube.com"
        header = build_sapisid_authorization(
            self.COOKIE, origin=origin, timestamp=1700000000
        )
        expected = hashlib.sha1(
            f"1700000000 SECRET {origin}".encode("utf-8")
        ).hexdigest()
        self.assertEqual(header, f"SAPISIDHASH 1700000000_{expected}")

    def test_sapisidhash_accepts_secure_3papisid(self):
        header = build_sapisid_authorization(
            "__Secure-3PAPISID=THREEP", timestamp=1
        )
        self.assertTrue(header.startswith("SAPISIDHASH 1_"))

    def test_sapisidhash_prefers_plain_sapisid(self):
        both = build_sapisid_authorization(
            "__Secure-3PAPISID=THREEP; SAPISID=SECRET", timestamp=1
        )
        plain = build_sapisid_authorization("SAPISID=SECRET", timestamp=1)
        self.assertEqual(both, plain)

    def test_sapisidhash_absent_without_usable_cookie(self):
        for raw in ("", "SID=abc", "SAPISID=", None):
            with self.subTest(raw=raw):
                self.assertEqual(build_sapisid_authorization(raw or ""), "")

    def test_native_clients_never_receive_credentials(self):
        parser = YouTubeParser(cookie=self.COOKIE)
        for client in ("ios", "android_vr"):
            with self.subTest(client=client):
                headers = parser._innertube_headers(client)
                self.assertNotIn("Cookie", headers)
                self.assertNotIn("Authorization", headers)

    def test_web_clients_receive_credentials(self):
        parser = YouTubeParser(cookie=self.COOKIE)
        for client in ("web", "mweb", "tv"):
            with self.subTest(client=client):
                headers = parser._innertube_headers(client)
                self.assertEqual(headers["Cookie"], self.COOKIE)
                self.assertTrue(
                    headers["Authorization"].startswith("SAPISIDHASH ")
                )
                self.assertEqual(
                    headers["X-Origin"], "https://www.youtube.com"
                )
                self.assertEqual(headers["X-Goog-AuthUser"], "0")

    def test_no_credentials_without_cookie(self):
        headers = YouTubeParser()._innertube_headers("web")
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("Authorization", headers)

    def test_cookie_appends_auth_capable_clients(self):
        parser = YouTubeParser(cookie=self.COOKIE)
        self.assertEqual(
            parser.player_clients,
            DEFAULT_PLAYER_CLIENTS + COOKIE_PLAYER_CLIENTS,
        )

    def test_cookie_append_keeps_explicit_order_without_duplicates(self):
        parser = YouTubeParser(cookie=self.COOKIE, player_clients="web,ios")
        self.assertEqual(parser.player_clients, ("web", "ios", "tv"))

    def test_cookie_without_sapisid_changes_nothing(self):
        parser = YouTubeParser(cookie="SID=abc")
        self.assertFalse(parser.cookie_authenticated)
        self.assertEqual(parser.player_clients, DEFAULT_PLAYER_CLIENTS)
        self.assertNotIn("Cookie", parser._innertube_headers("ios"))

    def test_default_clients_are_stream_capable_only(self):
        for client in DEFAULT_PLAYER_CLIENTS:
            with self.subTest(client=client):
                self.assertTrue(INNERTUBE_CLIENTS[client]["media"])
                self.assertFalse(INNERTUBE_CLIENTS[client]["cookies"])

    def test_cookie_clients_all_support_cookies(self):
        for client in COOKIE_PLAYER_CLIENTS:
            with self.subTest(client=client):
                self.assertTrue(INNERTUBE_CLIENTS[client]["cookies"])


if __name__ == "__main__":
    unittest.main()


class CookieExpiryDetectionTest(unittest.TestCase):
    """Cookie 失效检测：responseContext.loggedOut → 登录态判定 → 待通知标记。"""

    COOKIE = "SAPISID=secret; __Secure-3PAPISID=secret"

    def test_reads_logged_out_flag_from_response_context(self):
        payload = {
            "responseContext": {"mainAppWebResponseContext": {"loggedOut": True}}
        }
        self.assertIs(detect_youtube_login_state(payload), False)

    def test_logged_out_false_means_authenticated(self):
        payload = {
            "responseContext": {"mainAppWebResponseContext": {"loggedOut": False}}
        }
        self.assertIs(detect_youtube_login_state(payload), True)

    def test_accepts_string_flag_and_nested_placement(self):
        self.assertIs(
            detect_youtube_login_state(
                {"contents": {"mainAppWebResponseContext": {"loggedOut": "true"}}}
            ),
            False,
        )
        self.assertIs(
            detect_youtube_login_state(
                {"contents": {"mainAppWebResponseContext": {"loggedOut": "FALSE"}}}
            ),
            True,
        )

    def test_returns_none_without_the_signal(self):
        for payload in (None, {}, [], {"responseContext": {}}, "x", 3):
            with self.subTest(payload=payload):
                self.assertIsNone(detect_youtube_login_state(payload))
        self.assertIsNone(
            detect_youtube_login_state(
                {"responseContext": {"mainAppWebResponseContext": {"loggedOut": 1}}}
            )
        )

    def test_alert_is_pending_once_and_then_consumed(self):
        parser = YouTubeParser(cookie=self.COOKIE, cookie_alert_enabled=True)
        self.assertIsNone(parser.consume_cookie_alert())

        parser._mark_cookie_alert("logged_out")
        self.assertEqual(parser.consume_cookie_alert(), "logged_out")
        self.assertIsNone(parser.consume_cookie_alert())

    def test_alert_defaults_the_reason(self):
        parser = YouTubeParser(cookie=self.COOKIE, cookie_alert_enabled=True)
        parser._mark_cookie_alert("")
        self.assertEqual(parser.consume_cookie_alert(), "cookie_expired")

    def test_alert_stays_silent_when_disabled(self):
        parser = YouTubeParser(cookie=self.COOKIE, cookie_alert_enabled=False)
        parser._mark_cookie_alert("logged_out")
        self.assertIsNone(parser.consume_cookie_alert())

    def test_alert_stays_silent_without_authenticated_cookie(self):
        # 没填 Cookie、或 Cookie 里缺 SAPISID 时谈不上"失效"，不该骚扰管理员。
        for cookie in ("", "SID=abc"):
            with self.subTest(cookie=cookie):
                parser = YouTubeParser(cookie=cookie, cookie_alert_enabled=True)
                parser._mark_cookie_alert("logged_out")
                self.assertIsNone(parser.consume_cookie_alert())

    def test_gate_advice_points_at_cookie_and_proxy(self):
        advice = YouTubeParser._gate_advice("LOGIN_REQUIRED", False)
        self.assertIn("youtube.cookie", advice)
        self.assertIn("proxy.youtube", advice)

        expired = YouTubeParser._gate_advice("OK", True)
        self.assertIn("重新导出", expired)

        self.assertEqual(YouTubeParser._gate_advice("OK", False), "")

    def test_login_label_reflects_credential_state(self):
        self.assertEqual(YouTubeParser()._login_label(False), "匿名")
        self.assertIn(
            "缺少 SAPISID",
            YouTubeParser(cookie="SID=abc")._login_label(False),
        )
        authed = YouTubeParser(cookie=self.COOKIE)
        self.assertEqual(authed._login_label(False), "cookie(已鉴权)")
        self.assertEqual(authed._login_label(True), "cookie(已失效)")

    def test_client_chain_is_readable(self):
        parser = YouTubeParser(player_clients="ios,android_vr")
        self.assertEqual(parser._client_chain(), "ios > android_vr")


class MetadataFallbackTest(unittest.TestCase):
    """门禁吞掉 videoDetails 时的元数据兜底客户端。"""

    DETAILS = {
        "videoId": "2sm0UuaOm_s",
        "title": "样本标题",
        "author": "样本作者",
        "lengthSeconds": "84",
        "viewCount": "239339",
    }

    @staticmethod
    def _run(parser, failures):
        return asyncio.run(
            parser._fetch_player_metadata(
                None, "2sm0UuaOm_s", _Deadline(30.0), failures
            )
        )

    def test_metadata_clients_are_registered(self):
        self.assertTrue(METADATA_PLAYER_CLIENTS)
        for key in METADATA_PLAYER_CLIENTS:
            with self.subTest(client=key):
                self.assertIn(key, INNERTUBE_CLIENTS)
                # 元数据客户端只用来补字段，不参与取流。
                self.assertFalse(INNERTUBE_CLIENTS[key].get("media", False))
                self.assertNotIn(key, DEFAULT_PLAYER_CLIENTS)

    def test_returns_first_payload_with_title(self):
        parser = YouTubeParser()
        calls = []

        async def fake_post(session, endpoint, client_key, body, deadline):
            calls.append((endpoint, client_key, body.get("videoId")))
            return {"videoDetails": dict(self.DETAILS)}

        parser._post_innertube = fake_post
        failures = []
        player = self._run(parser, failures)
        self.assertEqual(player["videoDetails"]["lengthSeconds"], "84")
        self.assertEqual(failures, [])
        self.assertEqual(
            calls, [("player", METADATA_PLAYER_CLIENTS[0], "2sm0UuaOm_s")]
        )

    def test_missing_details_is_recorded_as_failure(self):
        parser = YouTubeParser()

        async def fake_post(session, endpoint, client_key, body, deadline):
            return {"playabilityStatus": {"status": "LOGIN_REQUIRED"}}

        parser._post_innertube = fake_post
        failures = []
        self.assertEqual(self._run(parser, failures), {})
        self.assertEqual(len(failures), len(METADATA_PLAYER_CLIENTS))
        self.assertIn("元数据", failures[0])

    def test_exception_is_recorded_and_swallowed(self):
        parser = YouTubeParser()

        async def fake_post(session, endpoint, client_key, body, deadline):
            raise RuntimeError("boom")

        parser._post_innertube = fake_post
        failures = []
        self.assertEqual(self._run(parser, failures), {})
        self.assertIn("RuntimeError: boom", failures[0])

    def test_client_already_in_main_chain_is_skipped(self):
        parser = YouTubeParser(
            player_clients=",".join(METADATA_PLAYER_CLIENTS)
        )
        calls = []

        async def fake_post(session, endpoint, client_key, body, deadline):
            calls.append(client_key)
            return {"videoDetails": dict(self.DETAILS)}

        parser._post_innertube = fake_post
        failures = []
        self.assertEqual(self._run(parser, failures), {})
        self.assertEqual(calls, [])
        self.assertEqual(failures, [])


class _FakeMultiHeaders:
    """模拟 aiohttp 的多值 headers（支持 getall）。"""

    def __init__(self, set_cookies):
        self._items = list(set_cookies)

    def getall(self, name, default=None):
        if name.lower() != "set-cookie":
            return list(default or [])
        return list(self._items)

    def get(self, name, default=""):
        if name.lower() != "set-cookie":
            return default
        return self._items[0] if self._items else default


class _FakeSingleHeaders:
    """模拟只支持单值 get 的 headers 实现。"""

    def __init__(self, value):
        self._value = value

    def get(self, name, default=""):
        if name.lower() != "set-cookie":
            return default
        return self._value


class _FakeCookieResponse:
    def __init__(self, headers, status=200, text=""):
        self.headers = headers
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeCookieSession:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response


class YouTubeCookieRuntimeTest(unittest.TestCase):
    """Cookie 运行时：轮换吸收、白名单防护、落盘接续与主动保鲜。"""

    COOKIE = "SID=abc; SAPISID=SECRET; __Secure-3PSIDTS=old-ts"

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="nova-yt-cookie-")
        self.state_path = os.path.join(self.tmpdir, "cookie.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @staticmethod
    def _run(coro):
        return asyncio.run(coro)

    # ── 回放与吸收 ──

    def test_header_is_byte_identical_before_any_rotation(self):
        runtime = YouTubeCookieRuntime(self.COOKIE)
        self.assertEqual(runtime.header(), self.COOKIE)
        self.assertEqual(runtime.revision, 0)
        self.assertTrue(runtime.authenticated)

    def test_absorb_merges_rotating_cookie_and_keeps_identity(self):
        runtime = YouTubeCookieRuntime(self.COOKIE)
        changed = runtime.absorb(
            ["__Secure-3PSIDTS=new-ts; Path=/; Secure; HttpOnly"]
        )
        self.assertTrue(changed)
        self.assertEqual(runtime.revision, 1)
        header = runtime.header()
        self.assertIn("__Secure-3PSIDTS=new-ts", header)
        self.assertNotIn("old-ts", header)
        self.assertIn("SAPISID=SECRET", header)
        self.assertIn("SID=abc", header)
        self.assertTrue(runtime.authenticated)

    def test_absorb_same_value_is_not_counted_as_rotation(self):
        runtime = YouTubeCookieRuntime(self.COOKIE)
        self.assertFalse(runtime.absorb(["__Secure-3PSIDTS=old-ts"]))
        self.assertEqual(runtime.revision, 0)
        self.assertEqual(runtime.header(), self.COOKIE)

    def test_deletion_directives_never_clear_the_jar(self):
        runtime = YouTubeCookieRuntime(self.COOKIE + "; SIDCC=live")
        for raw in (
            "SIDCC=EXPIRED; Max-Age=0",
            "SIDCC=; Path=/",
            "SIDCC=deleted; Max-Age=-1",
        ):
            with self.subTest(raw=raw):
                self.assertFalse(runtime.absorb([raw]))
        self.assertIn("SIDCC=live", runtime.header())

    def test_unknown_cookie_names_stay_out_of_the_jar(self):
        runtime = YouTubeCookieRuntime(self.COOKIE)
        self.assertFalse(runtime.absorb(["__utma=tracking; Path=/"]))
        self.assertNotIn("__utma", runtime.names())
        self.assertEqual(runtime.header(), self.COOKIE)

    def test_malformed_set_cookie_is_skipped(self):
        runtime = YouTubeCookieRuntime(self.COOKIE)
        self.assertFalse(runtime.absorb(["", "   ", "garbage-without-equals"]))
        self.assertEqual(runtime.header(), self.COOKIE)

    def test_auto_refresh_disabled_absorbs_nothing(self):
        runtime = YouTubeCookieRuntime(self.COOKIE, auto_refresh=False)
        self.assertFalse(runtime.absorb(["__Secure-3PSIDTS=new-ts"]))
        self.assertEqual(runtime.header(), self.COOKIE)

    def test_unconfigured_runtime_is_inert(self):
        runtime = YouTubeCookieRuntime("")
        self.assertFalse(runtime.absorb(["__Secure-3PSIDTS=new-ts"]))
        self.assertEqual(runtime.header(), "")
        self.assertFalse(runtime.authenticated)
        self.assertEqual(runtime.status_line(), "未配置")

    def test_absorb_response_reads_multi_value_headers(self):
        runtime = YouTubeCookieRuntime(self.COOKIE)
        response = _FakeCookieResponse(
            _FakeMultiHeaders(["SIDCC=one; Path=/", "YSC=two; Path=/"])
        )
        self.assertTrue(runtime.absorb_response(response))
        self.assertIn("SIDCC", runtime.names())
        self.assertIn("YSC", runtime.names())

    def test_collect_set_cookie_supports_both_header_shapes(self):
        multi = _FakeCookieResponse(_FakeMultiHeaders(["a=1", "b=2"]))
        self.assertEqual(collect_set_cookie_headers(multi), ["a=1", "b=2"])
        single = _FakeCookieResponse(_FakeSingleHeaders("a=1"))
        self.assertEqual(collect_set_cookie_headers(single), ["a=1"])
        self.assertEqual(
            collect_set_cookie_headers(_FakeCookieResponse(_FakeSingleHeaders(""))),
            [],
        )
        self.assertEqual(collect_set_cookie_headers(object()), [])

    # ── 落盘与接续 ──

    def test_rotation_survives_restart_through_state_file(self):
        first = YouTubeCookieRuntime(self.COOKIE, state_path=self.state_path)
        self.assertTrue(first.absorb(["__Secure-3PSIDTS=new-ts"]))
        self.assertTrue(self._run(first.flush()))
        self.assertTrue(os.path.exists(self.state_path))

        second = YouTubeCookieRuntime(self.COOKIE, state_path=self.state_path)
        self.assertIn("__Secure-3PSIDTS=new-ts", second.header())
        self.assertNotIn("old-ts", second.header())
        self.assertTrue(second.authenticated)

    def test_state_file_is_discarded_when_config_cookie_changes(self):
        first = YouTubeCookieRuntime(self.COOKIE, state_path=self.state_path)
        first.absorb(["__Secure-3PSIDTS=new-ts"])
        self._run(first.flush())

        replaced = "SID=zzz; SAPISID=OTHER; __Secure-3PSIDTS=fresh"
        second = YouTubeCookieRuntime(replaced, state_path=self.state_path)
        self.assertEqual(second.header(), replaced)
        self.assertNotIn("new-ts", second.header())

    def test_state_file_only_stores_cookie_pairs_and_a_hash(self):
        runtime = YouTubeCookieRuntime(self.COOKIE, state_path=self.state_path)
        runtime.absorb(["SIDCC=fresh"])
        self._run(runtime.flush())
        with open(self.state_path, "r", encoding="utf-8") as file_obj:
            data = json.load(file_obj)
        self.assertEqual(
            set(data),
            {"fingerprint", "cookies", "updated_at", "revision"},
        )
        self.assertEqual(data["cookies"]["SIDCC"], "fresh")
        self.assertNotIn("SECRET", data["fingerprint"])

    def test_flush_without_pending_rotation_writes_nothing(self):
        runtime = YouTubeCookieRuntime(self.COOKIE, state_path=self.state_path)
        self.assertFalse(self._run(runtime.flush()))
        self.assertFalse(os.path.exists(self.state_path))

    def test_corrupt_state_file_is_tolerated(self):
        with open(self.state_path, "w", encoding="utf-8") as file_obj:
            file_obj.write("not json at all")
        runtime = YouTubeCookieRuntime(self.COOKIE, state_path=self.state_path)
        self.assertEqual(runtime.header(), self.COOKIE)

    # ── 保鲜 ──

    def test_keepalive_sends_credentials_and_absorbs_rotation(self):
        runtime = YouTubeCookieRuntime(self.COOKIE, state_path=self.state_path)
        session = _FakeCookieSession(
            _FakeCookieResponse(
                _FakeMultiHeaders(["__Secure-3PSIDTS=rotated; Path=/"]),
                text='{"LOGGED_IN":true}',
            )
        )
        logged_in, detail = self._run(runtime.keepalive(session))
        self.assertIs(logged_in, True)
        self.assertIn("已吸收轮换", detail)

        url, kwargs = session.calls[0]
        self.assertTrue(url.startswith("https://www.youtube.com/"))
        headers = kwargs["headers"]
        self.assertEqual(headers["Cookie"], self.COOKIE)
        self.assertTrue(headers["Authorization"].startswith("SAPISIDHASH "))
        self.assertEqual(headers["X-Origin"], "https://www.youtube.com")
        self.assertEqual(headers["X-Goog-AuthUser"], "0")

        self.assertIn("__Secure-3PSIDTS=rotated", runtime.header())
        self.assertTrue(os.path.exists(self.state_path))
        self.assertIn("保鲜正常", runtime.status_line())

    def test_keepalive_reports_logged_out_state(self):
        runtime = YouTubeCookieRuntime(self.COOKIE)
        session = _FakeCookieSession(
            _FakeCookieResponse(
                _FakeMultiHeaders([]), text='{"logged_in":"0"}'
            )
        )
        logged_in, detail = self._run(runtime.keepalive(session))
        self.assertIs(logged_in, False)
        self.assertIn("未登录", detail)
        self.assertIn("保鲜未通过", runtime.status_line())

    def test_keepalive_unknown_login_state_is_not_a_failure(self):
        runtime = YouTubeCookieRuntime(self.COOKIE)
        session = _FakeCookieSession(
            _FakeCookieResponse(_FakeMultiHeaders([]), text="<html></html>")
        )
        logged_in, detail = self._run(runtime.keepalive(session))
        self.assertIsNone(logged_in)
        self.assertIn("未读出登录态", detail)

    def test_keepalive_without_cookie_skips_the_request(self):
        runtime = YouTubeCookieRuntime("")
        session = _FakeCookieSession(
            _FakeCookieResponse(_FakeMultiHeaders([]))
        )
        logged_in, detail = self._run(runtime.keepalive(session))
        self.assertIsNone(logged_in)
        self.assertEqual(session.calls, [])
        self.assertIn("跳过", detail)

    def test_keepalive_network_failure_is_reported_not_raised(self):
        runtime = YouTubeCookieRuntime(self.COOKIE)

        class _Boom:
            def get(self, *args, **kwargs):
                raise RuntimeError("boom")

        logged_in, detail = self._run(runtime.keepalive(_Boom()))
        self.assertIsNone(logged_in)
        self.assertIn("RuntimeError", detail)

    # ── 安全约定 ──

    def test_status_line_never_leaks_cookie_values(self):
        runtime = YouTubeCookieRuntime(self.COOKIE)
        runtime.absorb(["SIDCC=super-secret-value"])
        line = runtime.status_line()
        for secret in ("SECRET", "old-ts", "super-secret-value"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, line)

    def test_parser_requests_follow_the_rotated_cookie(self):
        parser = YouTubeParser(cookie=self.COOKIE)
        self.assertEqual(parser.cookie_runtime.header(), self.COOKIE)
        self.assertTrue(parser.cookie_authenticated)
        parser.cookie_runtime.absorb(["__Secure-3PSIDTS=rotated"])
        headers = parser._innertube_headers("web")
        self.assertIn("__Secure-3PSIDTS=rotated", headers["Cookie"])
        self.assertNotIn("old-ts", headers["Cookie"])
        self.assertTrue(headers["Authorization"].startswith("SAPISIDHASH "))


class CookieInputNormalizationTest(unittest.TestCase):
    """配置里粘什么格式都要能用：请求头 / cookies.txt / 扩展 JSON。"""

    HEADER = "SAPISID=abc; __Secure-3PSID=def; SIDCC=ghi"

    def test_plain_header_is_returned_unchanged(self):
        self.assertEqual(normalize_cookie_input(self.HEADER), self.HEADER)

    def test_blank_input_yields_empty_string(self):
        for raw in ("", "   ", "\n\t ", None):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_cookie_input(raw), "")

    def test_pasted_header_with_line_breaks_is_collapsed(self):
        raw = "SAPISID=abc;\n  __Secure-3PSID=def;\r\n\tSIDCC=ghi"
        self.assertEqual(normalize_cookie_input(raw), self.HEADER)

    def test_bom_prefix_is_stripped(self):
        self.assertEqual(
            normalize_cookie_input("\ufeff" + self.HEADER), self.HEADER
        )

    def test_netscape_cookies_txt_is_converted(self):
        raw = "\n".join(
            (
                "# Netscape HTTP Cookie File",
                "# This file is generated by Get cookies.txt LOCALLY",
                "",
                ".youtube.com\tTRUE\t/\tTRUE\t1800000000\tSAPISID\tabc",
                "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1800000000"
                "\t__Secure-3PSID\tdef",
                ".youtube.com\tTRUE\t/\tFALSE\t1800000000\tSIDCC\tghi",
            )
        )
        self.assertEqual(normalize_cookie_input(raw), self.HEADER)

    def test_netscape_line_without_value_keeps_empty_value(self):
        raw = ".youtube.com\tTRUE\t/\tTRUE\t1800000000\tSAPISID"
        self.assertEqual(normalize_cookie_input(raw), "SAPISID=")

    def test_netscape_with_spaces_instead_of_tabs_still_parses(self):
        raw = "\n".join(
            (
                "# Netscape HTTP Cookie File",
                ".youtube.com TRUE / TRUE 1800000000 SAPISID abc",
                ".youtube.com TRUE / TRUE 1800000000 __Secure-3PSID def",
            )
        )
        self.assertEqual(
            normalize_cookie_input(raw), "SAPISID=abc; __Secure-3PSID=def"
        )

    def test_cookie_editor_json_array_is_converted(self):
        raw = json.dumps(
            [
                {"domain": ".youtube.com", "name": "SAPISID", "value": "abc"},
                {"domain": ".youtube.com", "name": "__Secure-3PSID",
                 "value": "def"},
                {"domain": ".youtube.com", "name": "SIDCC", "value": "ghi"},
            ]
        )
        self.assertEqual(normalize_cookie_input(raw), self.HEADER)

    def test_json_wrapped_in_cookies_key_is_converted(self):
        raw = json.dumps({"cookies": [{"name": "SAPISID", "value": "abc"}]})
        self.assertEqual(normalize_cookie_input(raw), "SAPISID=abc")

    def test_json_entries_without_name_are_skipped(self):
        raw = json.dumps(
            [
                {"value": "orphan"},
                "not-a-dict",
                {"name": "  ", "value": "blank"},
                {"name": "SAPISID", "value": "abc"},
                {"name": "SIDCC", "value": None},
            ]
        )
        self.assertEqual(normalize_cookie_input(raw), "SAPISID=abc; SIDCC=")

    def test_broken_json_falls_back_to_header_collapsing(self):
        self.assertEqual(normalize_cookie_input("[oops"), "[oops")

    def test_normalized_cookie_drives_sapisid_authorization(self):
        raw = ".youtube.com\tTRUE\t/\tTRUE\t1800000000\tSAPISID\tabc"
        runtime = YouTubeCookieRuntime(normalize_cookie_input(raw))
        self.assertTrue(runtime.authenticated)
        self.assertEqual(runtime.header(), "SAPISID=abc")

    # ── 换行被 WebUI 吞掉的整段粘贴 ──────────────────────

    # AstrBot WebUI 里 type=string 的配置项是单行输入框，整段 cookies.txt
    # 粘进去以后换行会被压成空格：文本塌成一行、首字符还是注释号，按行
    # 解析会一条都取不到，于是静默退回匿名请求（线上真实踩到过）。
    COLLAPSED_HEAD = "# Netscape HTTP Cookie File"

    def test_single_line_netscape_paste_keeps_tabs_is_recovered(self):
        raw = " ".join(
            (
                self.COLLAPSED_HEAD,
                "# https://curl.haxx.se/rfc/cookie_spec.html",
                "# This is a generated file! Do not edit.",
                ".youtube.com	TRUE	/	TRUE	1800000000	SAPISID	abc",
                ".youtube.com	TRUE	/	TRUE	1800000000	__Secure-3PSID	def",
                ".youtube.com	TRUE	/	FALSE	1800000000	SIDCC	ghi",
            )
        )
        self.assertNotIn("\n", raw)
        self.assertEqual(normalize_cookie_input(raw), self.HEADER)

    def test_single_line_netscape_paste_without_tabs_is_recovered(self):
        raw = " ".join(
            (
                self.COLLAPSED_HEAD,
                ".youtube.com TRUE / TRUE 1800000000 SAPISID abc",
                "#HttpOnly_.youtube.com TRUE / TRUE 1800000000"
                " __Secure-3PSID def",
                ".youtube.com TRUE / FALSE 1800000000 SIDCC ghi",
            )
        )
        self.assertEqual(normalize_cookie_input(raw), self.HEADER)

    def test_collapsed_paste_keeps_empty_value_cookie(self):
        raw = " ".join(
            (
                self.COLLAPSED_HEAD,
                ".youtube.com	TRUE	/	TRUE	0	YSC",
                ".youtube.com	TRUE	/	TRUE	1800000000	SAPISID	abc",
            )
        )
        self.assertEqual(normalize_cookie_input(raw), "YSC=; SAPISID=abc")

    def test_collapsed_paste_drives_sapisid_authorization(self):
        raw = " ".join(
            (
                self.COLLAPSED_HEAD,
                ".youtube.com	TRUE	/	FALSE	1800000000	HSID	hs",
                ".youtube.com	TRUE	/	TRUE	1800000000	SAPISID	abc",
            )
        )
        runtime = YouTubeCookieRuntime(normalize_cookie_input(raw))
        self.assertTrue(runtime.authenticated)
        self.assertEqual(runtime.names(), ("HSID", "SAPISID"))

    def test_header_containing_the_word_true_is_left_alone(self):
        raw = "PREF=hl TRUE en; SAPISID=abc"
        self.assertEqual(normalize_cookie_input(raw), raw)

    def test_comment_only_text_yields_no_cookies(self):
        raw = " ".join((self.COLLAPSED_HEAD, "# nothing useful here"))
        self.assertEqual(parse_cookie_header(normalize_cookie_input(raw)), {})
