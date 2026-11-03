import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from nova_core.parser.platform.zhihu import ZhihuParser


class _Stream:
    def __init__(self, raw):
        self.raw = raw

    async def iter_chunked(self, _size):
        yield self.raw


class _Response:
    def __init__(self, text="", status=200, headers=None):
        self.status = status
        self.headers = headers or {}
        self.charset = "utf-8"
        self.content = _Stream(text.encode())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


class ZhihuParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = ZhihuParser()

    @staticmethod
    def answer():
        return {
            "id": "456",
            "question": {"title": "为什么？"},
            "content": '<p>第一段 &amp; 第二段</p><img src="data:placeholder" data-original="https://pic1.zhimg.com/image.jpg"><script>secret()</script>',
            "author": {
                "name": "作者",
                "avatar_url": "https://pic1.zhimg.com/avatar.jpg",
            },
            "created_time": 1700000000,
        }

    @staticmethod
    def initial_page(entities):
        data = json.dumps(
            {"initialState": {"entities": entities}}, ensure_ascii=False
        ).replace("<", "\\u003c")
        return '<script id="js-initialData" type="text/json">' + data + "</script>"

    def test_answer_content_images_and_author(self):
        self.parser._fetch_json = AsyncMock(return_value=self.answer())
        result = asyncio.run(
            self.parser.parse(
                None, "https://www.zhihu.com/question/123/answer/456?utm_source=share"
            )
        )
        self.assertEqual(result["title"], "为什么？")
        self.assertEqual(result["desc"], "第一段 & 第二段")
        self.assertEqual(result["author"], "作者")
        self.assertEqual(result["image_urls"], [["https://pic1.zhimg.com/image.jpg"]])
        self.assertEqual(result["url"], "https://www.zhihu.com/question/123/answer/456")
        self.assertNotIn("access_status", result)

    def test_links_are_validated_and_deduplicated_by_content_id(self):
        text = "https://www.zhihu.com/question/123/answer/456， https://www.zhihu.com/answer/456 https://zhuanlan.zhihu.com/p/789看看 https://www.zhihu.com/question/123"
        self.assertEqual(
            self.parser.extract_links(text),
            [
                "https://www.zhihu.com/question/123/answer/456",
                "https://zhuanlan.zhihu.com/p/789",
                "https://www.zhihu.com/question/123",
            ],
        )
        self.assertTrue(self.parser.can_parse("https://www.zhihu.com/zvideo/12345"))

    def test_untrusted_links_do_not_match(self):
        for url in (
            "https://www.zhihu.com.evil.test/question/123",
            "https://evil.test/question/123",
            "http://127.0.0.1/answer/456",
            "https://user:pass@www.zhihu.com/answer/456",
            "https://www.zhihu.com:444/answer/456",
            "https://www.zhihu.com/people/alice",
            "https://zhuanlan.zhihu.com/answer/456",
            "https://www.zhihu.com/question/123/answer/456/other",
        ):
            with self.subTest(url=url):
                self.assertFalse(self.parser.can_parse(url))

    def test_api_failure_falls_back_to_requested_page_entity(self):
        self.parser._fetch_json = AsyncMock(side_effect=RuntimeError("403"))
        self.parser._fetch_page = AsyncMock(
            return_value=self.initial_page(
                {"answers": {"456": self.answer(), "777": {"content": "不相关"}}}
            )
        )
        result = asyncio.run(
            self.parser.parse(None, "https://www.zhihu.com/answer/456")
        )
        self.assertEqual(result["desc"], "第一段 & 第二段")

    def test_entity_references_resolve_author_and_question(self):
        answer = self.answer()
        answer["author"] = "alice"
        answer["question"] = "123"
        page = self.initial_page(
            {
                "answers": {"456": answer},
                "users": {"alice": {"name": "Alice"}},
                "questions": {"123": {"title": "Title"}},
            }
        )
        entity = self.parser._entity_from_page(page, "answer", "456")
        self.assertEqual(entity["author"]["name"], "Alice")
        self.assertEqual(entity["question"]["title"], "Title")

    def test_question_does_not_send_an_unrequested_answer(self):
        self.parser._fetch_json = AsyncMock(
            return_value={
                "id": "123",
                "title": "问题",
                "detail": "问题说明",
                "answers": [self.answer()],
            }
        )
        result = asyncio.run(
            self.parser.parse(None, "https://www.zhihu.com/question/123")
        )
        self.assertEqual(result["desc"], "问题说明")
        self.assertEqual(result["image_urls"], [])

    def test_excerpt_is_explicitly_labeled_as_preview(self):
        self.parser._fetch_json = AsyncMock(
            return_value={
                "id": "456",
                "question": {"title": "问题"},
                "excerpt": "仅公开预览",
            }
        )
        result = asyncio.run(
            self.parser.parse(None, "https://www.zhihu.com/answer/456")
        )
        self.assertEqual(result["desc"], "仅公开预览")
        self.assertTrue(result["is_preview_only"])
        self.assertEqual(result["restriction_label"], "仅公开预览")

    def test_paid_content_is_labeled(self):
        data = self.answer()
        data["is_paid"] = True
        result = self.parser._build_metadata(
            "https://www.zhihu.com/answer/456", "answer", data
        )
        self.assertTrue(result["is_preview_only"])

    def test_purchased_full_content_is_not_mislabeled_as_preview(self):
        data = self.answer()
        data["paid_info"] = {"is_paid": True, "is_bought": True}
        result = self.parser._build_metadata(
            "https://www.zhihu.com/answer/456", "answer", data
        )
        self.assertNotIn("is_preview_only", result)

    def test_jsonld_article_fallback(self):
        data = {
            "@type": "Article",
            "headline": "文章",
            "articleBody": "公开正文",
            "author": {"name": "作者"},
            "image": "https://pic1.zhimg.com/title.jpg",
        }
        page = (
            '<script type="application/ld+json">'
            + json.dumps(data, ensure_ascii=False)
            + "</script>"
        )
        self.parser._fetch_json = AsyncMock(side_effect=RuntimeError("403"))
        self.parser._fetch_page = AsyncMock(return_value=page)
        result = asyncio.run(
            self.parser.parse(None, "https://zhuanlan.zhihu.com/p/789")
        )
        self.assertEqual(result["title"], "文章")
        self.assertEqual(result["desc"], "公开正文")
        self.assertEqual(result["image_urls"], [["https://pic1.zhimg.com/title.jpg"]])

    def test_jsonld_does_not_select_a_different_answer(self):
        data = {
            "@type": "QAPage",
            "mainEntity": {
                "@type": "Question",
                "name": "问题",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "url": "https://www.zhihu.com/answer/777",
                    "text": "不相关",
                },
            },
        }
        page = '<script type="application/ld+json">' + json.dumps(data) + "</script>"
        self.assertEqual(self.parser._jsonld_from_page(page, "answer", "456"), {})

    def test_embedded_video_is_resolved_without_cookie_leak(self):
        parser = ZhihuParser(cookie="z_c0=private")
        data = self.answer()
        data["content"] += '<a href="https://www.zhihu.com/video/123456">视频</a>'
        parser._fetch_json = AsyncMock(
            side_effect=[
                data,
                {
                    "playlist": {
                        "HD": {"play_url": "https://vdn.vzuu.com/hd.mp4"},
                        "SD": {"play_url": "https://vdn.vzuu.com/sd.mp4"},
                    }
                },
            ]
        )
        result = asyncio.run(parser.parse(None, "https://www.zhihu.com/answer/456"))
        self.assertEqual(
            result["video_urls"],
            [["https://vdn.vzuu.com/hd.mp4", "https://vdn.vzuu.com/sd.mp4"]],
        )
        self.assertFalse(parser._fetch_json.call_args.kwargs["use_cookie"])
        self.assertNotIn("Cookie", result["image_headers"])
        self.assertNotIn("Cookie", result["video_headers"])

    def test_text_only_parser_does_not_request_lens_video_api(self):
        parser = ZhihuParser(include_media=False)
        data = self.answer()
        data["content"] += '<a href="https://www.zhihu.com/video/123456">视频</a>'
        parser._fetch_json = AsyncMock(return_value=data)

        result = asyncio.run(parser.parse(None, "https://www.zhihu.com/answer/456"))

        self.assertEqual(parser._fetch_json.await_count, 1)
        self.assertNotIn("lens.zhihu.com", parser._fetch_json.call_args.args[1])
        self.assertIn("第一段 & 第二段", result["desc"])
        self.assertEqual(result["video_urls"], [])
        self.assertEqual(result["image_urls"], [["https://pic1.zhimg.com/image.jpg"]])

    def test_unsafe_redirect_is_blocked_before_second_request(self):
        session = _Session(
            [_Response(status=302, headers={"Location": "https://evil.test/leak"})]
        )
        with self.assertRaisesRegex(RuntimeError, "不受信任"):
            asyncio.run(
                ZhihuParser(cookie="z_c0=private")._fetch_page(
                    session, "https://www.zhihu.com/answer/456"
                )
            )
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(session.calls[0][1]["allow_redirects"])

    def test_login_redirect_is_a_clear_access_error(self):
        session = _Session(
            [_Response(status=302, headers={"Location": "/signin?next=answer"})]
        )
        with self.assertRaisesRegex(RuntimeError, "访问受限"):
            asyncio.run(
                self.parser._fetch_page(session, "https://www.zhihu.com/answer/456")
            )
        self.assertEqual(len(session.calls), 1)

    def test_access_error_and_empty_page_are_not_success(self):
        self.parser._fetch_json = AsyncMock(side_effect=RuntimeError("403"))
        self.parser._fetch_page = AsyncMock(
            return_value="<title>知乎，让每一次点击都充满意义</title>"
        )
        with self.assertRaisesRegex(RuntimeError, "访问受限"):
            asyncio.run(self.parser.parse(None, "https://www.zhihu.com/answer/456"))

    def test_cancellation_propagates(self):
        self.parser._fetch_json = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(self.parser.parse(None, "https://www.zhihu.com/answer/456"))

    def test_response_for_a_different_content_id_is_not_used(self):
        data = self.answer()
        data["id"] = "777"
        self.parser._fetch_json = AsyncMock(return_value=data)
        self.parser._fetch_page = AsyncMock(
            return_value="<html>no requested answer</html>"
        )
        with self.assertRaisesRegex(RuntimeError, "访问受限"):
            asyncio.run(self.parser.parse(None, "https://www.zhihu.com/answer/456"))

    def test_malformed_optional_fields_are_ignored(self):
        data = {
            "title": "文章",
            "content": "正文",
            "author": [],
            "created": "invalid",
            "image": [{"url": "https://evil.test/image"}, 123],
        }
        result = self.parser._build_metadata(
            "https://zhuanlan.zhihu.com/p/789", "article", data
        )
        self.assertEqual(result["image_urls"], [])
        self.assertEqual(result["author"], "")

    def test_cookie_newlines_are_ignored(self):
        self.assertEqual(ZhihuParser(cookie="z_c0=a\nInjected: header").cookie, "")

    @staticmethod
    def comment(comment_id="1", votes=5):
        return {
            "id": comment_id,
            "type": "comment",
            "content": "<p>评论 &amp; 正文</p>",
            "like_count": votes,
            "created_time": 1700000000,
            "author": {
                "id": "alice",
                "name": "Alice",
                "avatar_url": "https://pic1.zhimg.com/alice.jpg",
            },
        }

    def test_v5_and_legacy_comments_are_normalized_sorted_and_deduplicated(self):
        legacy = {
            "id": "2",
            "content": "<b>旧评论</b>",
            "vote_count": 8,
            "created_time": 1700000001,
            "author": {
                "role": "normal",
                "member": {
                    "id": "bob",
                    "name": "Bob",
                    "avatar_url": "https://pic2.zhimg.com/bob.jpg",
                },
            },
        }
        result = ZhihuParser._normalize_hot_comments(
            {
                "data": [
                    self.comment(),
                    legacy,
                    legacy,
                    None,
                    {"type": "ad", "content": "广告"},
                ]
            },
            2,
        )
        self.assertEqual([item["comment_id"] for item in result], ["2", "1"])
        self.assertEqual(result[0]["username"], "Bob")
        self.assertEqual(result[0]["uid"], "bob")
        self.assertEqual(result[0]["likes"], 8)
        self.assertEqual(result[0]["avatar_url"], "https://pic2.zhimg.com/bob.jpg")
        self.assertEqual(result[1]["message"], "评论 & 正文")

    def test_comment_api_targets_each_supported_content_type(self):
        for kind, bucket in (
            ("answer", "answers"),
            ("question", "questions"),
            ("article", "articles"),
            ("zvideo", "zvideos"),
        ):
            with self.subTest(kind=kind):
                parser = ZhihuParser(hot_comment_count=2)
                parser._fetch_json = AsyncMock(return_value={"data": [self.comment()]})
                result = asyncio.run(parser._fetch_hot_comments(None, kind, "456"))
                self.assertEqual(len(result), 1)
                self.assertIn(
                    f"/comment_v5/{bucket}/456/root_comment?",
                    parser._fetch_json.call_args.args[1],
                )
                self.assertIn("order_by=score", parser._fetch_json.call_args.args[1])

    def test_legacy_comments_are_used_when_v5_endpoint_fails(self):
        parser = ZhihuParser(hot_comment_count=2)
        parser._fetch_json = AsyncMock(
            side_effect=[RuntimeError("HTTP 404"), {"data": [self.comment()]}]
        )
        result = asyncio.run(parser._fetch_hot_comments(None, "answer", "456"))
        self.assertEqual(len(result), 1)
        self.assertEqual(parser._fetch_json.await_count, 2)
        self.assertIn(
            "/api/v4/answers/456/root_comments?", parser._fetch_json.call_args.args[1]
        )

    def test_empty_comment_list_does_not_trigger_a_second_request(self):
        parser = ZhihuParser(hot_comment_count=2)
        parser._fetch_json = AsyncMock(return_value={"data": []})
        self.assertEqual(
            asyncio.run(parser._fetch_hot_comments(None, "answer", "456")), []
        )
        self.assertEqual(parser._fetch_json.await_count, 1)

    def test_hot_comments_are_attached_after_successful_main_content(self):
        parser = ZhihuParser(hot_comment_count=2)
        parser._fetch_json = AsyncMock(
            side_effect=[self.answer(), {"data": [self.comment()]}]
        )
        result = asyncio.run(parser.parse(None, "https://www.zhihu.com/answer/456"))
        self.assertIn("第一段 & 第二段", result["desc"])
        self.assertEqual(result["hot_comments"][0]["likes"], 5)
        self.assertEqual(parser._fetch_json.await_count, 2)

    def test_comment_failure_does_not_block_content(self):
        parser = ZhihuParser(hot_comment_count=2)
        parser._fetch_json = AsyncMock(
            side_effect=[self.answer(), RuntimeError("403"), RuntimeError("403")]
        )
        result = asyncio.run(parser.parse(None, "https://www.zhihu.com/answer/456"))
        self.assertEqual(result["title"], "为什么？")
        self.assertNotIn("hot_comments", result)

    def test_count_zero_never_requests_comments(self):
        parser = ZhihuParser(hot_comment_count=0)
        parser._fetch_json = AsyncMock(return_value=self.answer())
        asyncio.run(parser.parse(None, "https://www.zhihu.com/answer/456"))
        self.assertEqual(parser._fetch_json.await_count, 1)
        self.assertEqual(
            asyncio.run(parser._fetch_hot_comments(None, "answer", "456")), []
        )
        self.assertEqual(parser._fetch_json.await_count, 1)

    def test_inaccessible_main_content_does_not_request_comments(self):
        parser = ZhihuParser(hot_comment_count=2)
        parser._fetch_json = AsyncMock(return_value={"id": "456", "title": "无正文"})
        parser._fetch_hot_comments = AsyncMock(return_value=[])
        with self.assertRaisesRegex(RuntimeError, "未返回可访问"):
            asyncio.run(parser.parse(None, "https://www.zhihu.com/answer/456"))
        parser._fetch_hot_comments.assert_not_awaited()

    def test_comment_cookie_proxy_and_redirect_behavior(self):
        parser = ZhihuParser(
            cookie="z_c0=private",
            use_proxy=True,
            proxy_url="http://127.0.0.1:7890",
            hot_comment_count=2,
        )
        session = _Session([_Response(json.dumps({"data": [self.comment()]}))])
        comments = asyncio.run(parser._fetch_hot_comments(session, "answer", "456"))
        self.assertEqual(len(comments), 1)
        request = session.calls[0][1]
        self.assertEqual(request["headers"]["Cookie"], "z_c0=private")
        self.assertEqual(request["proxy"], "http://127.0.0.1:7890")
        self.assertFalse(request["allow_redirects"])

    def test_comments_have_a_total_timeout_and_are_cancelled(self):
        parser = ZhihuParser(hot_comment_count=2)
        parser._fetch_json = AsyncMock(return_value=self.answer())
        cancelled = []

        async def slow_comments(*_args):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.append(True)

        parser._fetch_hot_comments = slow_comments
        with patch("nova_core.parser.platform.zhihu.COMMENT_TIMEOUT_SECONDS", 0.01):
            result = asyncio.run(parser.parse(None, "https://www.zhihu.com/answer/456"))
        self.assertEqual(result["title"], "为什么？")
        self.assertEqual(cancelled, [True])

    def test_comment_request_cancellation_propagates(self):
        parser = ZhihuParser(hot_comment_count=2)
        parser._fetch_json = AsyncMock(
            side_effect=[self.answer(), asyncio.CancelledError()]
        )
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(parser.parse(None, "https://www.zhihu.com/answer/456"))

    def test_oversized_comment_responses_are_discarded(self):
        parser = ZhihuParser(hot_comment_count=2)
        large_comment = self.comment()
        large_comment["content"] = "x" * 1000
        body = json.dumps({"data": [large_comment]})
        session = _Session([_Response(body), _Response(body)])
        with patch("nova_core.parser.platform.zhihu.MAX_RESPONSE_BYTES", 128):
            self.assertEqual(
                asyncio.run(parser._fetch_hot_comments(session, "answer", "456")), []
            )
        self.assertEqual(len(session.calls), 2)


if __name__ == "__main__":
    unittest.main()
