import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from nova_core.parser.platform.xiaoheihe import XiaoheiheParser


class XiaoheiheParserTests(unittest.TestCase):
    @staticmethod
    def _comment(comment_id, username, uid, likes, message, timestamp):
        return {
            "comment": [
                {
                    "commentid": comment_id,
                    "text": message,
                    "up": likes,
                    "create_at": timestamp,
                    "user": {
                        "username": username,
                        "userid": uid,
                        "avatar": f"https://cdn.example/{uid}.jpg",
                    },
                }
            ]
        }

    def test_hot_comments_are_normalized_sorted_and_limited(self):
        data = {
            "comments": [
                self._comment("1", "First", "11", 3, "First reply", 1_756_000_000),
                self._comment("2", "Second", "22", 21, "Second reply", 1_756_000_100),
                self._comment("3", "Third", "33", 8, "Third reply", 1_756_000_200),
            ]
        }

        comments = XiaoheiheParser._normalize_hot_comments(data, 2)

        self.assertEqual([item["comment_id"] for item in comments], ["2", "3"])
        self.assertEqual(comments[0]["username"], "Second")
        self.assertEqual(comments[0]["uid"], "22")
        self.assertEqual(comments[0]["likes"], 21)
        self.assertEqual(comments[0]["message"], "Second reply")

    def test_text_only_bbs_post_can_include_hot_comments(self):
        parser = XiaoheiheParser(hot_comment_count=2)
        parser._fetch_signed_api = AsyncMock(
            return_value={
                "link": {
                    "link_id": "174972336",
                    "title": "Text-only BBS post",
                    "text": json.dumps(
                        [{"type": "text", "text": "正文内容"}],
                        ensure_ascii=False,
                    ),
                    "user": {"nickname": "Alice", "uid": "10001"},
                },
                "comments": [
                    self._comment(
                        "9",
                        "Reader",
                        "20002",
                        12,
                        "热评内容",
                        1_756_000_000,
                    )
                ],
            }
        )

        result = asyncio.run(
            parser._parse_bbs_link(
                None,
                "https://www.xiaoheihe.cn/app/bbs/link/174972336",
                "174972336",
            )
        )

        self.assertEqual(result["desc"], "正文内容")
        self.assertEqual(result["video_urls"], [])
        self.assertEqual(result["image_urls"], [])
        self.assertEqual(result["hot_comments"][0]["message"], "热评内容")


if __name__ == "__main__":
    unittest.main()
