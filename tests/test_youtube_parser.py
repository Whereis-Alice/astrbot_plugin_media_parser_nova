"""YouTube 解析器的单元测试。

只覆盖纯函数部分（URL 识别、媒体流挑选、Innertube 响应字段提取），
样本按真实 player / next 响应裁剪，保留解析依赖的结构特征。
"""

import hashlib
import unittest

from nova_core.parser.platform.youtube import (
    COOKIE_PLAYER_CLIENTS,
    DEFAULT_PLAYER_CLIENTS,
    INNERTUBE_CLIENTS,
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
    find_comment_continuation,
    parse_compact_number,
    parse_cookie_header,
    parse_watch_html,
    parse_youtube_identity,
    select_youtube_media,
    thumbnail_candidates,
)

VID = "dQw4w9WgXcQ"


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
