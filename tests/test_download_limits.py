import asyncio
import math
import os
import tempfile
import unittest

from nova_core.constants import Config
from nova_core.downloader.manager import DownloadManager


class SizeCapNormalizationTests(unittest.TestCase):
    """体积上限归一化：非法/非正一律当作不限制。"""

    def test_invalid_and_non_positive_values_mean_unlimited(self):
        for raw in (None, "", "abc", 0, -1, -0.5, float("nan"), float("inf")):
            with self.subTest(raw=raw):
                self.assertEqual(DownloadManager._normalize_size_cap(raw), 0.0)

    def test_numeric_strings_are_accepted(self):
        self.assertEqual(DownloadManager._normalize_size_cap("128.5"), 128.5)
        self.assertEqual(DownloadManager._normalize_size_cap(96), 96.0)

    def test_constructor_stores_all_three_caps(self):
        manager = DownloadManager(
            max_video_size_mb=1000,
            large_video_threshold_mb=100,
            send_video_max_mb=90,
            cache_dir_available=False,
        )
        self.assertEqual(manager.max_video_size_mb, 1000.0)
        self.assertEqual(manager.large_video_threshold_mb, 100.0)
        self.assertEqual(manager.send_video_max_mb, 90.0)

    def test_default_threshold_comes_from_config(self):
        manager = DownloadManager(cache_dir_available=False)
        self.assertEqual(
            manager.large_video_threshold_mb,
            Config.DEFAULT_LARGE_VIDEO_THRESHOLD_MB,
        )
        self.assertEqual(manager.send_video_max_mb, 0.0)


class EffectiveVideoCapTests(unittest.TestCase):
    """下载阶段生效上限：管理员硬上限与可发送上限取更小者。"""

    def _manager(self, max_mb, send_mb):
        return DownloadManager(
            max_video_size_mb=max_mb,
            send_video_max_mb=send_mb,
            cache_dir_available=False,
        )

    def test_takes_the_smaller_cap(self):
        self.assertEqual(self._manager(1000, 100).effective_video_cap_mb, 100.0)
        self.assertEqual(self._manager(80, 100).effective_video_cap_mb, 80.0)

    def test_single_or_no_cap(self):
        self.assertEqual(self._manager(0, 100).effective_video_cap_mb, 100.0)
        self.assertEqual(self._manager(1000, 0).effective_video_cap_mb, 1000.0)
        self.assertEqual(self._manager(0, 0).effective_video_cap_mb, 0.0)

    def test_send_cap_is_effective_only_when_it_is_the_binding_one(self):
        self.assertTrue(self._manager(1000, 100)._send_cap_is_effective)
        self.assertTrue(self._manager(0, 100)._send_cap_is_effective)
        self.assertFalse(self._manager(80, 100)._send_cap_is_effective)
        self.assertFalse(self._manager(1000, 0)._send_cap_is_effective)


class VideoSizeLimitReasonTests(unittest.TestCase):
    """超限判定与文案：管理员上限与可发送上限要说清是谁挡下的。"""

    def setUp(self):
        self.manager = DownloadManager(
            max_video_size_mb=1000,
            send_video_max_mb=100,
            cache_dir_available=False,
        )

    def test_within_all_caps(self):
        self.assertEqual(self.manager._video_size_limit(64.0), ("", 0.0))

    def test_send_cap_hit_first(self):
        self.assertEqual(
            self.manager._video_size_limit(129.08), ("send", 100.0)
        )

    def test_max_cap_takes_priority_over_send_cap(self):
        # 同时越过两条线时按管理员硬上限报，避免误导成"平台收不下"。
        self.assertEqual(
            self.manager._video_size_limit(1200.0), ("max", 1000.0)
        )

    def test_send_reason_mentions_platform_rejection(self):
        reason = self.manager._video_size_limit_reason("send", 129.08, 100.0)
        self.assertIn("129.1MB", reason)
        self.assertIn("100.0MB", reason)
        self.assertIn("可发送上限", reason)
        self.assertIn("聊天平台会拒收", reason)

    def test_max_reason_distinguishes_pre_and_post_download(self):
        before = self.manager._video_size_limit_reason("max", 1200.0, 1000.0)
        after = self.manager._video_size_limit_reason(
            "max", 1200.0, 1000.0, downloaded=True
        )
        self.assertTrue(before.startswith("视频大小超过限制"))
        self.assertTrue(after.startswith("下载后视频大小超过限制"))


class SizeEstimatePlanningTests(unittest.TestCase):
    """解析器给出的体积预估要能在下载前拦住必然发不出去的视频。"""

    def _manager(self, send_mb=100.0):
        return DownloadManager(
            max_video_size_mb=1000,
            send_video_max_mb=send_mb,
            cache_dir_available=False,
        )

    def test_oversize_estimate_is_planned_and_cover_appended(self):
        manager = self._manager()
        metadata = {
            "video_size_estimates": [129.08],
            "cover_url": "https://i.ytimg.com/vi/x/maxres.jpg",
        }
        images = []
        limited = manager._plan_size_limited_videos(
            metadata, [["https://v/1.mp4"]], images
        )
        self.assertEqual(limited, {0: ("send", 129.08, 100.0)})
        self.assertEqual(images, [["https://i.ytimg.com/vi/x/maxres.jpg"]])

    def test_estimate_within_cap_is_not_planned(self):
        manager = self._manager()
        images = []
        limited = manager._plan_size_limited_videos(
            {"video_size_estimates": [42.0], "cover_url": "https://c/1.jpg"},
            [["https://v/1.mp4"]],
            images,
        )
        self.assertEqual(limited, {})
        self.assertEqual(images, [])

    def test_missing_or_broken_estimates_are_ignored(self):
        manager = self._manager()
        for estimates in (None, "129", [], [None], ["abc"], [0], [-3]):
            with self.subTest(estimates=estimates):
                images = []
                limited = manager._plan_size_limited_videos(
                    {"video_size_estimates": estimates, "cover_url": "https://c"},
                    [["https://v/1.mp4"]],
                    images,
                )
                self.assertEqual(limited, {})
                self.assertEqual(images, [])

    def test_non_finite_estimate_is_ignored(self):
        manager = self._manager()
        images = []
        limited = manager._plan_size_limited_videos(
            {"video_size_estimates": [math.inf], "cover_url": "https://c"},
            [["https://v/1.mp4"]],
            images,
        )
        self.assertEqual(limited, {})
        self.assertEqual(images, [])

    def test_cover_already_present_is_not_duplicated(self):
        manager = self._manager()
        images = [["https://c/1.jpg"]]
        manager._plan_size_limited_videos(
            {"video_size_estimates": [400.0], "cover_url": "https://c/1.jpg"},
            [["https://v/1.mp4"]],
            images,
        )
        self.assertEqual(images, [["https://c/1.jpg"]])

    def test_unlimited_caps_never_plan(self):
        manager = DownloadManager(cache_dir_available=False)
        images = []
        limited = manager._plan_size_limited_videos(
            {"video_size_estimates": [4096.0], "cover_url": "https://c"},
            [["https://v/1.mp4"]],
            images,
        )
        self.assertEqual(limited, {})
        self.assertEqual(images, [])


class ProcessMetadataSendLimitTests(unittest.TestCase):
    """端到端：预估超过可发送上限时不下载、不静默，改发封面。"""

    def _run(self, metadata, **kwargs):
        with tempfile.TemporaryDirectory() as cache_dir:
            manager = DownloadManager(
                cache_dir=cache_dir,
                cache_dir_available=True,
                **kwargs,
            )

            async def fake_download(*, session, media_items, cache_dir):
                results = []
                for item in media_items:
                    name = str(item["position"]) + ".bin"
                    results.append(
                        {
                            "kind": item["kind"],
                            "position": item["position"],
                            "success": True,
                            "file_path": os.path.join(cache_dir, name),
                            "size_mb": 1.0,
                        }
                    )
                return results

            manager._download_local_items = fake_download
            return asyncio.run(
                manager.process_metadata(session=None, metadata=metadata)
            )

    def test_oversize_video_is_skipped_with_reason_and_cover(self):
        result = self._run(
            {
                "url": "https://www.youtube.com/watch?v=TNwnccdoxJQ",
                "platform": "youtube",
                "video_urls": [["https://v/1.mp4"]],
                "image_urls": [],
                "video_size_estimates": [129.08],
                "cover_url": "https://i.ytimg.com/vi/x/maxres.jpg",
            },
            max_video_size_mb=1000,
            send_video_max_mb=100,
        )

        self.assertEqual(result["video_modes"], ["skip"])
        self.assertIn("可发送上限", result["video_skip_reasons"][0])
        self.assertTrue(result["send_limit_exceeded"])
        self.assertEqual(result["send_video_max_mb"], 100.0)
        self.assertEqual(result["send_limit_video_size_mb"], 129.08)
        # 硬上限没被越过，不该报"超过最大体积"。
        self.assertFalse(result["exceeds_max_size"])
        # 封面补进图片位，用户至少能看到封面而不是空手而归。
        self.assertEqual(
            result["image_urls"], [["https://i.ytimg.com/vi/x/maxres.jpg"]]
        )
        self.assertEqual(result["image_modes"], ["local"])
        self.assertTrue(result["has_valid_media"])

    def test_within_cap_video_still_downloads(self):
        result = self._run(
            {
                "url": "https://www.youtube.com/watch?v=ok",
                "platform": "youtube",
                "video_urls": [["dash:https://v/1.mp4||https://a/1.m4a"]],
                "image_urls": [],
                "video_size_estimates": [42.0],
                "cover_url": "https://i.ytimg.com/vi/ok/maxres.jpg",
            },
            max_video_size_mb=1000,
            send_video_max_mb=100,
        )

        self.assertEqual(result["video_modes"], ["local"])
        self.assertFalse(result["send_limit_exceeded"])
        self.assertIsNone(result["send_limit_video_size_mb"])
        self.assertEqual(result["image_urls"], [])


if __name__ == "__main__":
    unittest.main()