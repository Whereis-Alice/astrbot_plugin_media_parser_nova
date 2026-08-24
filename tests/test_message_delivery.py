import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.api.message_components import Image, Nodes, Plain, Reply

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


if __name__ == "__main__":
    unittest.main()
