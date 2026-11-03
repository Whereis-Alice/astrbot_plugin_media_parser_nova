"""访问限制必须在仅卡片、仅富媒体和普通文本输出中可见。"""

import unittest
import importlib
import sys
from pathlib import Path
from unittest.mock import patch

from astrbot.api.message_components import Image, Plain

from nova_core.message_adapter.node_builder import (
    _build_node_parts_for_link,
    build_media_notice_node,
    build_text_node,
)
from nova_core.rika_render.adapter import build_parse_result


NOTICE = "该知乎内容仅返回公开预览，完整内容可能需要登录或购买权限"


def restricted_metadata(**extra):
    metadata = {
        "url": "https://www.zhihu.com/question/1/answer/2",
        "platform": "zhihu", "title": "公开标题", "desc": "公开摘要",
        "access_status": "restricted", "is_preview_only": True,
        "access_message": NOTICE, "video_urls": [], "image_urls": [],
    }
    metadata.update(extra)
    return metadata


class AccessNoticeTextTests(unittest.TestCase):
    def test_rich_only_restriction_without_media_is_not_filtered_before_sending(self):
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root.parent))
        try:
            plugin_class = importlib.import_module(f"{root.name}.main").MediaParserNovaPlugin
            plugin = plugin_class.__new__(plugin_class)
            metadata = restricted_metadata(_enable_text_metadata=False, _enable_rich_media=True)
            self.assertTrue(plugin._metadata_has_output_candidate(metadata))
            nodes, _, _ = _build_node_parts_for_link(metadata)
            self.assertTrue(any(isinstance(node, Plain) and NOTICE in node.text for node in nodes))
        finally:
            sys.path.pop(0)

    def test_notice_is_an_independent_media_notice(self):
        node = build_media_notice_node(restricted_metadata())
        self.assertEqual(node.text, "提示：" + NOTICE)

    def test_normal_text_contains_access_message_once(self):
        node = build_text_node(restricted_metadata())
        self.assertEqual(node.text.count(NOTICE), 1)
        self.assertIn("简介/正文：", node.text)

    def test_disabled_text_metadata_still_shows_access_notice(self):
        node = build_text_node(restricted_metadata(), enable_text_metadata=False)
        self.assertIn(NOTICE, node.text)
        self.assertNotIn("公开标题", node.text)
        self.assertNotIn("公开摘要", node.text)

    def test_error_and_access_notice_are_not_repeated(self):
        node = build_text_node(restricted_metadata(error="播放权限不足"))
        self.assertEqual(node.text.count("播放权限不足"), 1)
        self.assertEqual(node.text.count(NOTICE), 1)

    def test_bilibili_preview_preserves_duration_label_and_single_notice(self):
        message = "可解析 03:00 / 全长 10:00"
        node = build_text_node(restricted_metadata(
            access_status="preview", access_message=message,
            available_length_ms=180000, timelength_ms=600000,
        ))
        self.assertEqual(node.text.count(message), 1)
        self.assertIn("时长：" + message, node.text)

    def test_missing_access_status_does_not_hide_explicit_preview_notice(self):
        node = build_media_notice_node(restricted_metadata(access_status=None))
        self.assertIn(NOTICE, node.text)

    def test_full_access_does_not_create_a_warning(self):
        metadata = restricted_metadata(access_status="full", is_preview_only=False)
        self.assertIsNone(build_media_notice_node(metadata))
        self.assertIsNone(build_text_node(metadata, enable_text_metadata=False))

    def test_no_access_message_preserves_rich_media_only_behavior(self):
        metadata = restricted_metadata(access_status=None, access_message="", is_preview_only=False)
        metadata["video_skip_reasons"] = ["无法下载"]
        self.assertIsNone(build_text_node(metadata, enable_text_metadata=False))
        metadata["_enable_text_metadata"] = False
        nodes, _, _ = _build_node_parts_for_link(metadata)
        self.assertFalse(any(isinstance(node, Plain) for node in nodes))


class AccessNoticeCardTests(unittest.TestCase):
    def test_card_only_notice_survives_disabled_text_metadata(self):
        metadata = restricted_metadata(
            _enable_text_metadata=False, _card_mode="仅卡片", _card_file_path="card.png",
        )
        with patch("nova_core.message_adapter.node_builder.os.path.isfile", return_value=True), \
                patch.object(Image, "fromFileSystem", return_value=Image(file="card.png")):
            nodes, _, delivery = _build_node_parts_for_link(metadata)
        texts = [node.text for node in nodes if isinstance(node, Plain)]
        self.assertEqual("\n".join(texts).count(NOTICE), 1)
        self.assertNotIn("公开摘要", "\n".join(texts))
        self.assertIsNotNone(delivery["card_node"])

    def test_combined_card_mode_emits_only_one_plain_access_notice(self):
        metadata = restricted_metadata(
            _card_mode="卡片+文本同条发送", _card_file_path="card.png",
        )
        with patch("nova_core.message_adapter.node_builder.os.path.isfile", return_value=True), \
                patch.object(Image, "fromFileSystem", return_value=Image(file="card.png")):
            nodes, _, _ = _build_node_parts_for_link(metadata)
        self.assertEqual("\n".join(node.text for node in nodes if isinstance(node, Plain)).count(NOTICE), 1)

    def test_card_warnings_include_access_notice_without_mutating_metadata(self):
        metadata = restricted_metadata(limit_warnings=["既有媒体提示"])
        result = build_parse_result(metadata)
        self.assertEqual(result.extra["limit_warnings"], ["既有媒体提示", NOTICE])
        self.assertEqual(metadata["limit_warnings"], ["既有媒体提示"])

    def test_card_warnings_do_not_duplicate_existing_access_notice(self):
        result = build_parse_result(restricted_metadata(limit_warnings=[NOTICE]))
        self.assertEqual(result.extra["limit_warnings"], [NOTICE])

    def test_card_full_access_is_not_misrepresented_as_restricted(self):
        result = build_parse_result(restricted_metadata(access_status="full", is_preview_only=False))
        self.assertNotIn("limit_warnings", result.extra)


if __name__ == "__main__":
    unittest.main()
