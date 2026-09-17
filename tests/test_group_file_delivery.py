import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.api.message_components import Plain

from nova_core.downloader.manager import DownloadManager
from nova_core.message_adapter.group_file import (
    GroupFileUploader,
    build_group_file_name,
)
from nova_core.message_adapter.node_builder import build_all_nodes
from nova_core.message_adapter.sender import MessageSender


class GroupFileUploaderTests(unittest.TestCase):
    def _event(self, *, group_id="123456", platform="aiocqhttp"):
        bot = SimpleNamespace(call_action=AsyncMock(return_value={"status": "ok"}))
        return SimpleNamespace(
            bot=bot,
            get_group_id=lambda: group_id,
            get_platform_name=lambda: platform,
        )

    def test_standard_onebot_action_and_parameters(self):
        with tempfile.TemporaryDirectory() as work_dir:
            path = os.path.join(work_dir, "video.mp4")
            with open(path, "wb") as handle:
                handle.write(b"video")
            event = self._event()
            asyncio.run(GroupFileUploader(60).upload(event, path, "标题.mp4"))

        event.bot.call_action.assert_awaited_once_with(
            "upload_group_file",
            group_id=123456,
            file=os.path.abspath(path),
            name="标题.mp4",
        )

    def test_private_and_non_onebot_events_are_rejected(self):
        uploader = GroupFileUploader()
        self.assertFalse(uploader.can_upload(self._event(group_id="")))
        self.assertFalse(uploader.can_upload(self._event(platform="telegram")))

    def test_file_name_is_sanitized_without_losing_unicode(self):
        name = build_group_file_name(
            {"title": '名探偵:プリキュア?/"最終話"'},
            "/tmp/cache.bin",
        )
        self.assertEqual(name, "名探偵_プリキュア_最終話.bin")


class GroupFileDownloadPlanningTests(unittest.TestCase):
    def _run(self, *, group_file_available: bool):
        with tempfile.TemporaryDirectory() as cache_dir:
            manager = DownloadManager(
                max_video_size_mb=1000,
                send_video_max_mb=100,
                cache_dir=cache_dir,
                cache_dir_available=True,
                oversize_delivery="group_file",
                transcode_oversize_video=False,
            )

            async def fake_download(*, session, media_items, cache_dir):
                results = []
                for item in media_items:
                    path = os.path.join(cache_dir, f"{item['position']}.mp4")
                    with open(path, "wb") as handle:
                        handle.write(b"video")
                    results.append(
                        {
                            "kind": item["kind"],
                            "position": item["position"],
                            "success": True,
                            "file_path": path,
                            "size_mb": 129.0,
                        }
                    )
                return results

            manager._download_local_items = fake_download
            result = asyncio.run(
                manager.process_metadata(
                    session=None,
                    metadata={
                        "url": "https://youtu.be/example",
                        "title": "示例视频",
                        "platform": "youtube",
                        "video_urls": [["dash:https://v||https://a"]],
                        "image_urls": [],
                    },
                    group_file_available=group_file_available,
                )
            )
            if group_file_available:
                built = build_all_nodes([result], 100.0, 1000.0, True, True)
                group_files = built.link_metadata[0]["group_files"]
                self.assertEqual(len(group_files), 1)
                self.assertEqual(group_files[0]["name"], "示例视频.mp4")
                self.assertEqual(built.link_metadata[0]["media_nodes"], [])
            return result

    def test_oversize_group_video_is_kept_for_group_file_upload(self):
        result = self._run(group_file_available=True)
        self.assertEqual(result["video_modes"], ["group_file"])
        self.assertIsNotNone(result["file_paths"][0])
        self.assertTrue(result["has_valid_media"])
        self.assertTrue(result["send_limit_exceeded"])

    def test_private_fallback_keeps_existing_cover_only_behavior(self):
        result = self._run(group_file_available=False)
        self.assertEqual(result["video_modes"], ["skip"])
        self.assertIsNone(result["file_paths"][0])
        self.assertIn("可发送上限", result["video_skip_reasons"][0])

    def test_rich_media_only_builds_group_file_without_video_node(self):
        result = build_all_nodes(
            [
                {
                    "url": "https://youtu.be/example",
                    "title": "示例视频",
                    "video_urls": [["dash:https://v||https://a"]],
                    "image_urls": [],
                    "file_paths": ["/cache/video.mp4"],
                    "video_sizes": [80.0],
                    "video_modes": ["group_file"],
                    "image_modes": [],
                    "has_valid_media": True,
                    "use_local_files": True,
                    "_enable_text_metadata": False,
                    "_enable_rich_media": True,
                }
            ]
        )

        self.assertEqual(result.all_link_nodes, [[]])
        self.assertEqual(len(result.link_metadata[0]["group_files"]), 1)
        self.assertEqual(result.link_metadata[0]["media_nodes"], [])


class GroupFileSenderTests(unittest.TestCase):
    def _event(self):
        return SimpleNamespace(
            send=AsyncMock(),
            chain_result=lambda chain: chain,
            plain_result=lambda text: [Plain(text)],
        )

    def test_group_file_is_uploaded_outside_message_chain(self):
        uploader = SimpleNamespace(can_upload=lambda event: True, upload=AsyncMock())
        sender = MessageSender(group_file_uploader=uploader)
        event = self._event()
        metadata = {
            "group_files": [
                {"path": "/cache/video.mp4", "name": "video.mp4", "size_mb": 80}
            ],
            "source_url": "https://youtu.be/example",
        }

        asyncio.run(sender.send_individual_results(event, [[]], [metadata]))

        uploader.upload.assert_awaited_once_with(
            event,
            "/cache/video.mp4",
            "video.mp4",
        )
        event.send.assert_not_awaited()

    def test_upload_failure_is_reported_when_other_content_succeeds(self):
        uploader = SimpleNamespace(
            can_upload=lambda event: True,
            upload=AsyncMock(side_effect=RuntimeError("上传接口拒绝")),
        )
        sender = MessageSender(group_file_uploader=uploader)
        event = self._event()
        metadata = {
            "group_files": [{"path": "/cache/video.mp4", "name": "video.mp4"}],
            "source_url": "https://youtu.be/example",
        }

        asyncio.run(
            sender.send_individual_results(
                event,
                [[Plain("解析信息")]],
                [metadata],
            )
        )

        self.assertEqual(event.send.await_count, 2)
        notice = event.send.await_args_list[-1].args[0][0].text
        self.assertIn("1/2", notice)
        self.assertIn("上传接口拒绝", notice)
        self.assertIn("https://youtu.be/example", notice)

    def test_only_group_file_failure_still_reports_reason_and_link(self):
        uploader = SimpleNamespace(
            can_upload=lambda event: True,
            upload=AsyncMock(side_effect=RuntimeError("上传接口拒绝")),
        )
        sender = MessageSender(group_file_uploader=uploader)
        event = self._event()
        metadata = {
            "group_files": [{"path": "/cache/video.mp4", "name": "video.mp4"}],
            "source_url": "https://youtu.be/example",
        }

        asyncio.run(sender.send_individual_results(event, [[]], [metadata]))

        event.send.assert_awaited_once()
        notice = event.send.await_args.args[0][0].text
        self.assertIn("1/1", notice)
        self.assertIn("上传接口拒绝", notice)
        self.assertIn("https://youtu.be/example", notice)

    def test_aggregate_mode_does_not_send_separator_only_forward(self):
        uploader = SimpleNamespace(can_upload=lambda event: True, upload=AsyncMock())
        sender = MessageSender(group_file_uploader=uploader)
        event = self._event()
        metadata = [
            {
                "link_nodes": [],
                "is_normal": True,
                "group_files": [
                    {"path": f"/cache/video-{index}.mp4", "name": f"video-{index}.mp4"}
                ],
                "source_url": f"https://youtu.be/example-{index}",
            }
            for index in range(2)
        ]

        asyncio.run(
            sender.send_aggregated_results(
                event,
                metadata,
                "Nova解析",
                10000,
            )
        )

        self.assertEqual(uploader.upload.await_count, 2)
        event.send.assert_not_awaited()

    def test_individual_mode_does_not_send_separator_between_group_files(self):
        uploader = SimpleNamespace(can_upload=lambda event: True, upload=AsyncMock())
        sender = MessageSender(group_file_uploader=uploader)
        event = self._event()
        metadata = [
            {
                "group_files": [
                    {"path": f"/cache/video-{index}.mp4", "name": f"video-{index}.mp4"}
                ],
                "source_url": f"https://youtu.be/example-{index}",
            }
            for index in range(2)
        ]

        asyncio.run(sender.send_individual_results(event, [[], []], metadata))

        self.assertEqual(uploader.upload.await_count, 2)
        event.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
