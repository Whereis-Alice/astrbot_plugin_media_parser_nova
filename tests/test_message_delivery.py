import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.api.message_components import Image, Nodes, Plain, Reply

from nova_core.message_adapter import node_builder
from nova_core.message_adapter.node_builder import (
    build_all_nodes,
    build_media_notice_node,
    build_text_node,
)
from nova_core.message_adapter.sender import MessageSender


class MessageDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.sender = MessageSender()
        self.card = Image(file="card.png")
        self.summary = Plain("解析摘要")
        self.overflow = Plain("超长文本的后续分片")
        self.media = Image(file="media.png")

    def _metadata(self, mode):
        return {
            "card_node": self.card,
            "card_mode": mode,
            "display_text_nodes": [self.summary, self.overflow],
            "media_nodes": [self.media],
            "metadata_text_node": self.summary,
        }

    def test_combined_mode_keeps_card_and_first_text_in_one_chain(self):
        chains = self.sender._delivery_chains(
            [self.card, self.summary, self.overflow, self.media],
            self._metadata("卡片+文本同条发送"),
        )

        self.assertEqual(chains[0], [self.card, self.summary])
        self.assertEqual(chains[1], [self.overflow])
        self.assertEqual(chains[2], [self.media])

    def test_split_mode_sends_card_and_text_separately(self):
        chains = self.sender._delivery_chains(
            [self.card, self.summary, self.overflow, self.media],
            self._metadata("卡片+文本分开发"),
        )

        self.assertEqual(
            chains,
            [[self.card], [self.summary], [self.overflow], [self.media]],
        )

    def test_card_only_mode_keeps_original_link_with_card(self):
        original_link = Plain("原始链接：https://x.com/i/status/1")
        metadata = self._metadata("仅卡片")
        metadata["display_text_nodes"] = [original_link]

        chains = self.sender._delivery_chains(
            [self.card, original_link, self.media],
            metadata,
        )

        self.assertEqual(chains, [[self.card, original_link], [self.media]])

    def test_individual_send_builds_reply_image_and_text_chain(self):
        event = SimpleNamespace(
            send=AsyncMock(),
            chain_result=lambda chain: chain,
            plain_result=lambda text: [Plain(text)],
        )
        metadata = self._metadata("卡片+文本同条发送")
        metadata["display_text_nodes"] = [self.summary]
        metadata["media_nodes"] = []

        asyncio.run(
            self.sender.send_individual_results(
                event,
                [[self.card, self.summary]],
                [metadata],
                quote_user_message=True,
                quote_message_id="source-message-id",
            )
        )

        event.send.assert_awaited_once()
        chain = event.send.await_args.args[0]
        self.assertEqual(len(chain), 3)
        self.assertIsInstance(chain[0], Reply)
        self.assertIs(chain[1], self.card)
        self.assertIs(chain[2], self.summary)

    def test_aggregated_send_keeps_card_and_text_in_one_forward_node(self):
        event = SimpleNamespace(
            send=AsyncMock(),
            chain_result=lambda chain: chain,
            plain_result=lambda text: [Plain(text)],
        )
        metadata = self._metadata("卡片+文本同条发送")
        metadata.update(
            {
                "link_nodes": [self.card, self.summary],
                "display_text_nodes": [self.summary],
                "media_nodes": [],
                "is_normal": True,
            }
        )

        asyncio.run(
            self.sender.send_aggregated_results(
                event,
                [metadata],
                "Nova解析",
                10000,
            )
        )

        event.send.assert_awaited_once()
        outer_chain = event.send.await_args.args[0]
        self.assertEqual(len(outer_chain), 1)
        self.assertIsInstance(outer_chain[0], Nodes)
        forward_content = outer_chain[0].nodes[0].content
        self.assertEqual(forward_content, [self.card, self.summary])

    def test_partial_delivery_failure_notifies_chat(self):
        event = SimpleNamespace(
            send=AsyncMock(),
            plain_result=lambda text: [Plain(text)],
        )

        asyncio.run(
            self.sender._finish_best_effort_delivery(
                event,
                label="解析结果",
                expected=2,
                succeeded=1,
                errors=[
                    RuntimeError(
                        "[Highway] httpUpload Error uploading block at offset 1: "
                        "HTTP Upload failed with code 102902"
                    )
                ],
                failed_urls=["https://youtu.be/abc"],
            )
        )

        event.send.assert_awaited_once()
        notice = event.send.await_args.args[0][0].text
        self.assertIn("1/2", notice)
        self.assertIn("聊天平台拒收", notice)
        self.assertIn("https://youtu.be/abc", notice)

    def test_partial_delivery_failure_notice_failure_is_swallowed(self):
        event = SimpleNamespace(
            send=AsyncMock(side_effect=RuntimeError("offline")),
            plain_result=lambda text: [Plain(text)],
        )

        asyncio.run(
            self.sender._finish_best_effort_delivery(
                event,
                label="解析结果",
                expected=2,
                succeeded=1,
                errors=[RuntimeError("boom")],
            )
        )

        event.send.assert_awaited_once()

    def test_hot_comments_can_be_hidden_from_plain_text_only(self):
        result = build_all_nodes(
            [
                {
                    "url": "https://example.com/post/1",
                    "title": "解析标题",
                    "desc": "解析正文",
                    "hot_comments": [{"username": "Alice", "message": "热评正文"}],
                    "_enable_text_metadata": True,
                    "_enable_rich_media": False,
                    "_enable_hot_comments_text": False,
                }
            ],
            50,
            50,
            True,
            True,
        )

        plain_text = "\n".join(
            node.text
            for node in result.all_link_nodes[0]
            if isinstance(node, Plain)
        )
        self.assertIn("解析标题", plain_text)
        self.assertIn("解析正文", plain_text)
        self.assertNotIn("热评正文", plain_text)


if __name__ == "__main__":
    unittest.main()


class CardOnlyNoticeTests(unittest.TestCase):
    """仅卡片模式：视频被跳过的原因不能被卡片吞掉。"""

    def setUp(self):
        handle, self.card_path = tempfile.mkstemp(suffix=".png")
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(self.card_path)
                        and os.unlink(self.card_path))

    SEND_LIMIT_REASON = (
        "视频体积超过可发送上限（129.1MB > 100.0MB），"
        "聊天平台会拒收，已改为只发信息与封面"
    )

    def _card_only_texts(self, **extra):
        metadata = {
            "url": "https://www.youtube.com/watch?v=TNwnccdoxJQ",
            "title": "样本标题",
            "platform": "youtube",
            "_card_file_path": self.card_path,
            "_card_mode": "仅卡片",
        }
        metadata.update(extra)
        nodes, _, delivery = node_builder._build_node_parts_for_link(metadata)
        self.assertEqual(delivery["card_mode"], "仅卡片")
        return [node.text for node in nodes if isinstance(node, Plain)]

    def test_send_limit_reason_survives_card_only_mode(self):
        texts = self._card_only_texts(
            video_count=1,
            image_count=1,
            has_valid_media=True,
            send_limit_exceeded=True,
            video_skip_reasons=[self.SEND_LIMIT_REASON],
        )
        joined = "\n".join(texts)
        self.assertIn("可发送上限", joined)
        self.assertIn("聊天平台会拒收", joined)
        # 提示要排在原始链接前面，读起来才是"发生了什么 + 去哪看"。
        self.assertTrue(texts[-1].startswith("原始链接："))
        # 仅卡片模式仍然不该把完整图文正文倒出来。
        self.assertNotIn("样本标题", joined)

    def test_card_only_mode_without_notice_keeps_only_link(self):
        texts = self._card_only_texts()
        self.assertEqual(len(texts), 1)
        self.assertTrue(texts[0].startswith("原始链接："))

    def test_send_limit_suppresses_generic_no_media_error(self):
        # 视频因为发不出去被跳过，不能再报一句"直链内未找到有效媒体"。
        texts = self._card_only_texts(
            video_count=1,
            image_count=0,
            has_valid_media=False,
            video_urls=[["https://v/1.mp4"]],
            send_limit_exceeded=True,
            video_skip_reasons=[self.SEND_LIMIT_REASON],
        )
        joined = "\n".join(texts)
        self.assertNotIn("直链内未找到有效媒体", joined)
        self.assertIn("可发送上限", joined)

    def test_notice_node_is_none_when_there_is_nothing_to_report(self):
        self.assertIsNone(build_media_notice_node({}))
        self.assertIsNone(
            build_media_notice_node({"title": "只有标题", "video_count": 1})
        )

    def test_notice_node_reports_error_and_skip_reason(self):
        node = build_media_notice_node(
            {
                "error": "取流失败",
                "video_count": 1,
                "video_skip_reasons": [self.SEND_LIMIT_REASON],
            }
        )
        self.assertIsNotNone(node)
        self.assertIn("解析失败：取流失败", node.text)
        self.assertIn("媒体跳过：视频 1/1", node.text)

    def test_source_url_is_exposed_for_delivery_fallback(self):
        metadata = {
            "url": "https://www.youtube.com/watch?v=TNwnccdoxJQ",
            "title": "样本标题",
        }
        result = build_all_nodes([metadata], 100.0, 1000.0, True, True)
        self.assertEqual(
            result.link_metadata[0]["source_url"],
            "https://www.youtube.com/watch?v=TNwnccdoxJQ",
        )


class TextNodeAccessMessageTests(unittest.TestCase):
    """限制说明的标签归属：B站讲时长，YouTube 讲原因。"""

    GATE = "被 YouTube 机器人验证挡下，仅展示封面与信息"

    def _text(self, **extra):
        metadata = {"title": "样本标题", "platform": "youtube"}
        metadata.update(extra)
        node = build_text_node(metadata)
        self.assertIsNotNone(node)
        return node.text

    def test_restriction_without_length_uses_hint_label(self):
        text = self._text(
            access_status="gated",
            access_message=self.GATE,
            timelength_ms=84000,
        )
        self.assertIn("时长：01:24", text)
        self.assertIn("提示：" + self.GATE, text)
        self.assertNotIn("时长：" + self.GATE, text)

    def test_restriction_without_any_duration_omits_duration_line(self):
        text = self._text(access_status="gated", access_message=self.GATE)
        self.assertIn("提示：" + self.GATE, text)
        self.assertNotIn("时长：", text)

    def test_partial_access_keeps_duration_label(self):
        message = "可解析 03:00 / 全长 10:00"
        text = self._text(
            access_status="preview",
            access_message=message,
            available_length_ms=180000,
            timelength_ms=600000,
        )
        self.assertIn("时长：" + message, text)
        self.assertNotIn("提示：", text)
