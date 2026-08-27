"""Nitter 页面解析的单元测试。

固定样本按真实 Nitter 输出裁剪而成，保留了解析所依赖的全部结构特征：
main-tweet / replies / timeline-item / tweet-stats / replying-to /
unavailable-box，以及 Nitter 特有的 /pic/ 代理路径与英文 UTC 时间标题。
"""

import unittest

from nova_core.parser.platform import nitter


def _item(
    handle: str,
    tweet_id: str,
    fullname: str,
    content: str,
    *,
    avatar: str = "",
    date: str = "Aug 26, 2026 \u00b7 3:03 AM UTC",
    likes: str = "",
    retweets: str = "",
    replies: str = "",
    views: str = "",
    replying_to: bool = True,
    classes: str = "timeline-item thread-last ",
) -> str:
    """拼出一条 timeline-item，可控制统计数字与是否带 replying-to。"""
    avatar_src = avatar or "/pic/profile_images%2F1%2F" + handle + "_bigger.jpg"
    stats = ""
    for icon, value in (
        ("comment", replies),
        ("retweet", retweets),
        ("heart", likes),
        ("views", views),
    ):
        if not value:
            continue
        stats += (
            '<span class="tweet-stat"><div class="icon-container">'
            '<span class="icon-' + icon + '" title=""></span> ' + value
            + "</div></span>\n"
        )
    prefix = (
        '<div class="replying-to">Replying to <a href="/other">@other</a></div>\n'
        if replying_to
        else ""
    )
    return (
        '<div class="' + classes + '" data-username="' + handle + '">\n'
        '<a class="tweet-link" href="/' + handle + "/status/" + tweet_id + '#m"></a>\n'
        '<div class="tweet-body">\n'
        '<div><div class="tweet-header">\n'
        '<a class="tweet-avatar" href="/' + handle + '">'
        '<img class="avatar round" src="' + avatar_src + '" alt="" loading="lazy" />'
        "</a>\n"
        '<div class="tweet-name-row"><div class="fullname-and-username">'
        '<a class="fullname" href="/' + handle + '" title="' + fullname + '">'
        + fullname
        + "</a>"
        '<a class="username" href="/' + handle + '" title="@' + handle + '">@'
        + handle
        + "</a></div>\n"
        '<span class="tweet-date"><a href="/' + handle + "/status/" + tweet_id
        + '#m" title="' + date + '">Aug 26</a></span>\n'
        "</div></div></div>\n"
        + prefix
        + '<div class="tweet-content media-body" dir="auto">' + content + "</div>\n"
        '<div class="tweet-stats">\n' + stats + "</div>\n"
        "</div>\n"
        "</div>"
    )


MAIN_TWEET = _item(
    "precure_movie",
    "2092446937500860496",
    "&#39;映画名探偵プリキュア&#39;",
    "\u256d\u2501\u2501\u256e\n\u3000プロフィール帳が届いたよ\n\u2800\n"
    '<a href="/search?f=tweets&amp;q=%23precure">#precure</a>',
    avatar="/pic/profile_images%2F2068484088303112192%2FXiQ0quiY_bigger.jpg",
    date="Aug 26, 2026 \u00b7 3:00 AM UTC",
    replies="83",
    retweets="6,081",
    likes="25,680",
    views="867,413",
    replying_to=False,
    classes="timeline-item ",
)

SAMPLE_HTML = (
    "<html><body>"
    '<div class="conversation"><div class="main-thread">'
    '<div id="m" class="main-tweet">' + MAIN_TWEET + "</div></div>\n"
    '<div id="r" class="replies">\n'
    '<div class="reply thread thread-line">'
    + _item(
        "imo3mochi3",
        "2092447893382922732",
        "いももーち",
        "トップオブリリー",
        likes="733",
        retweets="32",
        views="33,952",
    )
    + "</div>\n"
    '<div class="reply thread thread-line">'
    + _item(
        "second_user",
        "2092447893382922733",
        "second &amp; friends",
        "第一行<br>第二行",
        likes="2,048",
    )
    + "</div>\n"
    '<div class="reply thread thread-line">'
    + _item("third_user", "2092447893382922734", "third", "只是路过")
    + "</div>\n"
    '<div class="reply thread thread-line">'
    '<div class="timeline-item unavailable" data-username="gone">'
    '<div class="unavailable-box">This tweet is unavailable</div>'
    '<div class="tweet-content media-body" dir="auto">残留正文</div>'
    "</div></div>\n"
    '<div class="reply thread thread-line">'
    '<div class="timeline-item " data-username="empty_user">'
    '<div class="tweet-body"></div>'
    "</div></div>\n"
    "</div></div></body></html>"
)


class NormalizeBaseUrlsTests(unittest.TestCase):
    def test_splits_and_completes_scheme(self):
        self.assertEqual(
            nitter.normalize_base_urls(
                "127.0.0.1:8585, https://nitter.example/ ; http://a.b"
            ),
            (
                "http://127.0.0.1:8585",
                "https://nitter.example",
                "http://a.b",
            ),
        )

    def test_dedupes_and_accepts_sequences(self):
        self.assertEqual(
            nitter.normalize_base_urls(
                ["http://x.test/", "http://x.test", " ", "http://y.test"]
            ),
            ("http://x.test", "http://y.test"),
        )

    def test_rejects_blank_and_bad_scheme(self):
        self.assertEqual(nitter.normalize_base_urls(""), ())
        self.assertEqual(nitter.normalize_base_urls(None), ())
        self.assertEqual(nitter.normalize_base_urls("ftp://a.b"), ())


class ThreadUrlTests(unittest.TestCase):
    def test_uses_handle_when_known(self):
        self.assertEqual(
            nitter.thread_url("http://127.0.0.1:8585/", "123", "precure_movie"),
            "http://127.0.0.1:8585/precure_movie/status/123",
        )

    def test_falls_back_to_i_path(self):
        self.assertEqual(
            nitter.thread_url("http://n.test", "123"),
            "http://n.test/i/status/123",
        )

    def test_strips_unsafe_handle_characters(self):
        self.assertEqual(
            nitter.thread_url("http://n.test", "123", "bad/../name?x=1"),
            "http://n.test/badnamex1/status/123",
        )


class RestoreMediaUrlTests(unittest.TestCase):
    def test_restores_proxied_avatar_and_upgrades_size(self):
        self.assertEqual(
            nitter.restore_media_url(
                "/pic/profile_images%2F1%2Fabc_bigger.jpg",
                "http://n.test",
                upgrade_avatar=True,
            ),
            "https://pbs.twimg.com/profile_images/1/abc_400x400.jpg",
        )

    def test_keeps_avatar_size_when_not_upgrading(self):
        self.assertEqual(
            nitter.restore_media_url("/pic/profile_images%2F1%2Fabc_bigger.jpg"),
            "https://pbs.twimg.com/profile_images/1/abc_bigger.jpg",
        )

    def test_restores_orig_media_path(self):
        self.assertEqual(
            nitter.restore_media_url("/pic/orig/media%2FHQ.jpg"),
            "https://pbs.twimg.com/media/HQ.jpg",
        )

    def test_passes_through_absolute_and_hosted_paths(self):
        self.assertEqual(
            nitter.restore_media_url("https://cdn.test/a.jpg"),
            "https://cdn.test/a.jpg",
        )
        self.assertEqual(
            nitter.restore_media_url("/pic/video.twimg.com%2Fx.mp4"),
            "https://video.twimg.com/x.mp4",
        )

    def test_joins_relative_path_with_base(self):
        self.assertEqual(
            nitter.restore_media_url("/other/thing.png", "http://n.test/"),
            "http://n.test/other/thing.png",
        )
        self.assertEqual(nitter.restore_media_url(""), "")


class FormatTimeTests(unittest.TestCase):
    def test_parses_utc_and_returns_local_string(self):
        formatted = nitter.format_time("Aug 26, 2026 \u00b7 3:00 AM UTC")
        self.assertRegex(formatted, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertTrue(formatted.startswith("2026-08-26") or formatted.startswith("2026-08-2"))

    def test_returns_original_when_unparsable(self):
        self.assertEqual(nitter.format_time("not a date"), "not a date")
        self.assertEqual(nitter.format_time(""), "")
        self.assertEqual(nitter.format_time(None), "")


class BuildStatsLineTests(unittest.TestCase):
    def test_orders_icons_and_skips_missing(self):
        self.assertEqual(
            nitter.build_stats_line({"likes": "10", "views": "3,000"}),
            "\u2764\ufe0f10 \U0001f4c83,000",
        )

    def test_returns_blank_for_empty_stats(self):
        self.assertEqual(nitter.build_stats_line({}), "")


class ParseThreadTests(unittest.TestCase):
    def test_extracts_main_tweet_metadata(self):
        result = nitter.parse_thread(SAMPLE_HTML, 0, "http://n.test")

        self.assertEqual(
            result["stats"],
            {
                "likes": "25,680",
                "retweets": "6,081",
                "replies": "83",
                "views": "867,413",
            },
        )
        self.assertEqual(
            result["stats_line"],
            "\u2764\ufe0f25,680 \u21a9\ufe0f6,081 \U0001f4ac83 \U0001f4c8867,413",
        )
        self.assertEqual(
            result["author_avatar"],
            "https://pbs.twimg.com/profile_images/2068484088303112192/XiQ0quiY_400x400.jpg",
        )
        self.assertIn("@precure_movie", result["author_name"])
        self.assertIn("#precure", result["text"])
        self.assertRegex(result["time"], r"^2026-08-2\d \d{2}:\d{2}:\d{2}$")

    def test_limit_zero_skips_replies(self):
        self.assertEqual(nitter.parse_thread(SAMPLE_HTML, 0)["comments"], [])

    def test_sorts_replies_by_likes_and_applies_limit(self):
        comments = nitter.parse_thread(SAMPLE_HTML, 2, "http://n.test")["comments"]

        self.assertEqual([item["likes"] for item in comments], [2048, 733])
        self.assertEqual([item["uid"] for item in comments], ["second_user", "imo3mochi3"])

    def test_reply_fields_are_normalized(self):
        comments = nitter.parse_thread(SAMPLE_HTML, 5, "http://n.test")["comments"]

        self.assertEqual(len(comments), 3)
        by_uid = {item["uid"]: item for item in comments}
        first = by_uid["imo3mochi3"]
        self.assertEqual(first["username"], "いももーち(@imo3mochi3)")
        self.assertEqual(first["message"], "トップオブリリー")
        self.assertEqual(first["comment_id"], "2092447893382922732")
        self.assertEqual(
            first["avatar_url"],
            "https://pbs.twimg.com/profile_images/1/imo3mochi3_400x400.jpg",
        )
        self.assertRegex(first["time"], r"^2026-08-2\d \d{2}:\d{2}:\d{2}$")

        second = by_uid["second_user"]
        self.assertEqual(second["message"], "第一行\n第二行")
        self.assertEqual(second["username"], "second & friends(@second_user)")

        third = by_uid["third_user"]
        self.assertEqual(third["likes"], 0)

    def test_skips_unavailable_and_missing_content(self):
        comments = nitter.parse_thread(SAMPLE_HTML, 10)["comments"]

        uids = [item["uid"] for item in comments]
        self.assertNotIn("gone", uids)
        self.assertNotIn("empty_user", uids)

    def test_handles_blank_and_reply_free_pages(self):
        blank = nitter.parse_thread("", 3)
        self.assertEqual(blank["comments"], [])
        self.assertEqual(blank["stats_line"], "")
        self.assertEqual(blank["author_name"], "")

        no_replies = (
            '<div id="m" class="main-tweet">'
            + _item(
                "solo",
                "1",
                "Solo",
                "只有主推",
                replying_to=False,
                classes="timeline-item ",
            )
            + "</div>"
        )
        parsed = nitter.parse_thread(no_replies, 3)
        self.assertEqual(parsed["comments"], [])
        self.assertEqual(parsed["text"], "只有主推")
        self.assertEqual(parsed["stats_line"], "")

    def test_invalid_limit_is_treated_as_zero(self):
        self.assertEqual(nitter.parse_thread(SAMPLE_HTML, "abc")["comments"], [])


if __name__ == "__main__":
    unittest.main()
