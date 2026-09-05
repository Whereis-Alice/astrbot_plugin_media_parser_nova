import asyncio
import os
import tempfile
import unittest
from unittest import mock

from nova_core.constants import Config
from nova_core.downloader import transcode as transcode_mod
from nova_core.downloader.manager import DownloadManager
from nova_core.downloader.transcode import (
    TranscodePlan,
    TranscodeResult,
    VideoProbe,
    _build_ffmpeg_args,
    _describe_note,
    plan_transcode,
    transcode_video_to_size,
)
from nova_core.message_adapter.node_builder import build_media_notice_node

MB = 1024 * 1024


class TranscodeTimeoutNormalizationTests(unittest.TestCase):
    """压缩超时要夹在合理区间内，非法值回落默认。"""

    def test_values_are_clamped(self):
        self.assertEqual(
            DownloadManager._normalize_transcode_timeout(5),
            Config.MIN_TRANSCODE_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            DownloadManager._normalize_transcode_timeout(999999),
            Config.MAX_TRANSCODE_TIMEOUT_SECONDS,
        )
        self.assertEqual(DownloadManager._normalize_transcode_timeout(120), 120)

    def test_invalid_values_fall_back_to_default(self):
        for raw in (None, "", "abc", object()):
            with self.subTest(raw=raw):
                self.assertEqual(
                    DownloadManager._normalize_transcode_timeout(raw),
                    Config.DEFAULT_TRANSCODE_TIMEOUT_SECONDS,
                )


class TranscodeGateTests(unittest.TestCase):
    """开关、缓存目录、可发送上限三者齐备才启用压缩。"""

    def _manager(self, **kwargs):
        params = {
            "max_video_size_mb": 1000,
            "send_video_max_mb": 100,
            "cache_dir_available": True,
            "transcode_oversize_video": True,
        }
        params.update(kwargs)
        return DownloadManager(**params)

    def test_enabled_requires_switch_cache_and_send_cap(self):
        self.assertTrue(self._manager().transcode_enabled)
        self.assertFalse(
            self._manager(transcode_oversize_video=False).transcode_enabled
        )
        self.assertFalse(self._manager(send_video_max_mb=0).transcode_enabled)
        self.assertFalse(self._manager(cache_dir_available=False).transcode_enabled)

    def test_download_cap_ignores_send_cap_while_transcoding(self):
        # 能压缩就得先把原片下下来，否则压缩无从下手。
        self.assertEqual(self._manager().video_download_cap_mb, 1000.0)
        self.assertEqual(
            self._manager(transcode_oversize_video=False).video_download_cap_mb,
            100.0,
        )

    def test_send_cap_rewrite_is_disabled_while_transcoding(self):
        self.assertFalse(self._manager()._send_cap_is_effective)
        self.assertTrue(
            self._manager(transcode_oversize_video=False)._send_cap_is_effective
        )

    def test_constructor_defaults_to_off(self):
        manager = DownloadManager(cache_dir_available=True, send_video_max_mb=100)
        self.assertFalse(manager.transcode_oversize_video)
        self.assertFalse(manager.transcode_enabled)

    def test_skip_planning_still_applies_when_transcoding_is_off(self):
        manager = self._manager(transcode_oversize_video=False)
        images = []
        limited = manager._plan_size_limited_videos(
            {"video_size_estimates": [129.08], "cover_url": "https://c/1.jpg"},
            [["https://v/1.mp4"]],
            images,
        )
        self.assertEqual(limited, {0: ("send", 129.08, 100.0)})

    def test_send_limit_estimate_is_not_pre_skipped_when_transcoding(self):
        manager = self._manager()
        images = []
        limited = manager._plan_size_limited_videos(
            {"video_size_estimates": [129.08], "cover_url": "https://c/1.jpg"},
            [["https://v/1.mp4"]],
            images,
        )
        self.assertEqual(limited, {})
        self.assertEqual(images, [])

    def test_max_cap_estimate_is_still_pre_skipped_when_transcoding(self):
        # 管理员硬上限是"根本不该下载"，压缩开关不能把它绕过去。
        manager = self._manager()
        images = []
        limited = manager._plan_size_limited_videos(
            {"video_size_estimates": [4096.0], "cover_url": "https://c/1.jpg"},
            [["https://v/1.mp4"]],
            images,
        )
        self.assertEqual(limited, {0: ("max", 4096.0, 1000.0)})


class PlanTranscodeTests(unittest.TestCase):
    """码率与分辨率规划。"""

    def test_short_clip_keeps_resolution_and_picks_top_audio(self):
        plan, reject = plan_transcode(
            VideoProbe(duration=60.0, width=1280, height=720, fps=30.0, has_audio=True),
            50 * MB,
        )
        self.assertEqual(reject, "")
        self.assertIsNotNone(plan)
        self.assertEqual(plan.audio_kbps, 128)
        self.assertEqual(plan.height, 0)
        self.assertGreater(plan.video_kbps, 3000)
        self.assertEqual(plan.fps_cap, 0.0)

    def test_tight_budget_scales_down_and_caps_fps(self):
        # 10 分钟 1080p60 压到 100MB 只剩约 1.2Mbps，必须降分辨率并锁 30fps。
        plan, reject = plan_transcode(
            VideoProbe(
                duration=600.0, width=1920, height=1080, fps=60.0, has_audio=True
            ),
            100 * MB,
        )
        self.assertEqual(reject, "")
        self.assertEqual(plan.height, 540)
        self.assertEqual(plan.fps_cap, 30.0)
        self.assertEqual(plan.audio_kbps, 128)
        self.assertGreaterEqual(plan.video_kbps, 120)

    def test_roomy_budget_keeps_more_pixels_than_a_tight_one(self):
        probe = VideoProbe(
            duration=600.0, width=1920, height=1080, fps=30.0, has_audio=True
        )
        roomy, _ = plan_transcode(probe, 300 * MB)
        tight, _ = plan_transcode(probe, 60 * MB)
        self.assertEqual(roomy.height, 0)  # 3.5Mbps 以上保留 1080p
        self.assertEqual(tight.height, 480)
        self.assertGreater(roomy.video_kbps, tight.video_kbps)

    def test_portrait_resolution_is_judged_by_short_edge(self):
        # 竖屏 720x1280 的清晰度是 720p，不该被当成 1280p 再缩一次。
        plan, _ = plan_transcode(
            VideoProbe(duration=60.0, width=720, height=1280, has_audio=True),
            50 * MB,
        )
        self.assertEqual(plan.height, 0)

    def test_audio_ladder_steps_down_on_tight_budget(self):
        plan, _ = plan_transcode(
            VideoProbe(duration=1200.0, width=1280, height=720, has_audio=True),
            50 * MB,
        )
        self.assertIn(plan.audio_kbps, (64, 96))

    def test_silent_source_drops_audio_budget(self):
        plan, _ = plan_transcode(
            VideoProbe(duration=60.0, width=1280, height=720, has_audio=False),
            20 * MB,
        )
        self.assertEqual(plan.audio_kbps, 0)

    def test_very_long_video_is_rejected(self):
        plan, reject = plan_transcode(
            VideoProbe(
                duration=7200.0, width=1920, height=1080, has_audio=True
            ),
            100 * MB,
        )
        self.assertIsNone(plan)
        self.assertIn("2.0 小时", reject)
        self.assertIn("画质无法接受", reject)
        self.assertIn("至少需要", reject)

    def test_short_clip_with_absurd_target_is_not_blamed_on_length(self):
        # 目标体积离谱地小时，不该反过来说一段 20 秒的视频"太长"。
        plan, reject = plan_transcode(
            VideoProbe(duration=20.0, width=1280, height=720, has_audio=True),
            1024,
        )
        self.assertIsNone(plan)
        self.assertIn("20 秒", reject)
        self.assertNotIn("太长", reject)
        self.assertIn("至少需要 0.5MB", reject)

    def test_missing_probe_or_target_is_rejected(self):
        self.assertEqual(plan_transcode(None, 100 * MB)[0], None)
        self.assertEqual(
            plan_transcode(VideoProbe(duration=0.0), 100 * MB)[0], None
        )
        self.assertEqual(
            plan_transcode(VideoProbe(duration=60.0), 0)[0], None
        )


class FfmpegArgumentTests(unittest.TestCase):
    """ffmpeg 参数拼装：缩放方向、帧率、音轨。"""

    def _args(self, plan, probe):
        return _build_ffmpeg_args("in.mp4", "out.mp4", plan, probe)

    def test_landscape_scales_by_height(self):
        args = self._args(
            TranscodePlan(video_kbps=1200, audio_kbps=128, height=720),
            VideoProbe(duration=60.0, width=1920, height=1080),
        )
        self.assertIn("scale=-2:720", args)

    def test_portrait_scales_by_width(self):
        args = self._args(
            TranscodePlan(video_kbps=1200, audio_kbps=128, height=720),
            VideoProbe(duration=60.0, width=1080, height=1920),
        )
        self.assertIn("scale=720:-2", args)

    def test_odd_dimensions_are_aligned_when_not_scaling(self):
        args = self._args(
            TranscodePlan(video_kbps=1200, audio_kbps=128, height=0),
            VideoProbe(duration=60.0, width=1081, height=607),
        )
        self.assertIn("scale=trunc(iw/2)*2:trunc(ih/2)*2", args)

    def test_fps_cap_only_applies_to_higher_framerate(self):
        plan = TranscodePlan(video_kbps=1200, audio_kbps=128, fps_cap=30.0)
        high = self._args(plan, VideoProbe(duration=60.0, fps=60.0))
        low = self._args(plan, VideoProbe(duration=60.0, fps=24.0))
        self.assertIn("-r", high)
        self.assertIn("30", high)
        self.assertNotIn("-r", low)

    def test_silent_output_disables_audio_stream(self):
        args = self._args(
            TranscodePlan(video_kbps=1200, audio_kbps=0),
            VideoProbe(duration=60.0, width=1280, height=720),
        )
        self.assertIn("-an", args)
        self.assertNotIn("aac", args)

    def test_output_is_faststart_mp4(self):
        args = self._args(
            TranscodePlan(video_kbps=1200, audio_kbps=128),
            VideoProbe(duration=60.0, width=1280, height=720),
        )
        self.assertEqual(args[-1], "out.mp4")
        self.assertIn("+faststart", args)
        self.assertIn("libx264", args)


class DescribeNoteTests(unittest.TestCase):
    """给用户看的一行压缩说明。"""

    def test_scaled_note_shows_both_resolutions(self):
        note = _describe_note(
            135 * MB,
            96 * MB,
            TranscodePlan(video_kbps=1200, audio_kbps=128, height=720),
            VideoProbe(duration=60.0, width=1920, height=1080),
        )
        self.assertIn("1080p → 720p", note)
        self.assertIn("135.0MB", note)
        self.assertIn("96.0MB", note)

    def test_unscaled_note_says_resolution_is_kept(self):
        note = _describe_note(
            135 * MB,
            96 * MB,
            TranscodePlan(video_kbps=1200, audio_kbps=128, height=0),
            VideoProbe(duration=60.0, width=1280, height=720),
        )
        self.assertIn("保持 720p", note)


class TranscodeVideoToSizeTests(unittest.TestCase):
    """压缩主流程：用假的 ffmpeg 产出验证收敛、替换与清理。"""

    def _probe(self):
        return VideoProbe(
            duration=10.0, width=1920, height=1080, fps=60.0, has_audio=True
        )

    def _run(self, payload_bytes, target_bytes, source_bytes=3 * MB):
        calls = []

        async def fake_run(args, timeout):
            calls.append(list(args))
            with open(args[-1], "wb") as handle:
                handle.write(b"0" * payload_bytes)
            return 0, "", ""

        with tempfile.TemporaryDirectory() as work_dir:
            source = os.path.join(work_dir, "clip.mp4")
            with open(source, "wb") as handle:
                handle.write(b"0" * source_bytes)
            with mock.patch.object(
                transcode_mod, "probe_video", return_value=self._probe()
            ), mock.patch.object(transcode_mod, "_run_capture", fake_run):
                result = asyncio.run(
                    transcode_video_to_size(source, target_bytes)
                )
            leftovers = sorted(os.listdir(work_dir))
        return result, calls, leftovers

    def test_successful_pass_replaces_temp_file(self):
        result, calls, leftovers = self._run(900_000, MB)
        self.assertIsNone(result.error)
        self.assertTrue(result.file_path.endswith("clip_fit.mp4"))
        self.assertAlmostEqual(result.size_mb, 900_000 / MB, places=3)
        self.assertIn("1080p → ", result.note)
        self.assertIn("第1轮", result.summary)
        self.assertEqual(len(calls), 1)
        # 中间产物不能留在缓存目录里。
        self.assertEqual(leftovers, ["clip.mp4", "clip_fit.mp4"])

    def test_second_pass_tightens_budget_then_gives_up(self):
        result, calls, leftovers = self._run(2_000_000, MB)
        self.assertIsNone(result.file_path)
        self.assertIn("压缩后仍有", result.error)
        self.assertEqual(len(calls), 2)
        first_rate = calls[0][calls[0].index("-b:v") + 1]
        second_rate = calls[1][calls[1].index("-b:v") + 1]
        self.assertLess(int(second_rate[:-1]), int(first_rate[:-1]))
        self.assertEqual(leftovers, ["clip.mp4"])

    def test_missing_ffmpeg_is_reported_not_raised(self):
        async def boom(args, timeout):
            raise FileNotFoundError(args[0])

        with tempfile.TemporaryDirectory() as work_dir:
            source = os.path.join(work_dir, "clip.mp4")
            with open(source, "wb") as handle:
                handle.write(b"0" * MB)
            with mock.patch.object(
                transcode_mod, "probe_video", return_value=self._probe()
            ), mock.patch.object(transcode_mod, "_run_capture", boom):
                result = asyncio.run(transcode_video_to_size(source, MB))
        self.assertIsNone(result.file_path)
        self.assertIn("ffmpeg 未找到", result.error)

    def test_timeout_is_reported(self):
        async def timed_out(args, timeout):
            return None, "", ""

        with tempfile.TemporaryDirectory() as work_dir:
            source = os.path.join(work_dir, "clip.mp4")
            with open(source, "wb") as handle:
                handle.write(b"0" * MB)
            with mock.patch.object(
                transcode_mod, "probe_video", return_value=self._probe()
            ), mock.patch.object(transcode_mod, "_run_capture", timed_out):
                result = asyncio.run(
                    transcode_video_to_size(source, MB, timeout_seconds=30)
                )
        self.assertIsNone(result.file_path)
        self.assertIn("压缩超时", result.error)

    def test_unreadable_probe_is_reported(self):
        with tempfile.TemporaryDirectory() as work_dir:
            source = os.path.join(work_dir, "clip.mp4")
            with open(source, "wb") as handle:
                handle.write(b"0" * MB)
            with mock.patch.object(
                transcode_mod, "probe_video", return_value=None
            ):
                result = asyncio.run(transcode_video_to_size(source, MB))
        self.assertIsNone(result.file_path)
        self.assertIn("读不出视频信息", result.error)

    def test_invalid_target_is_rejected_early(self):
        result = asyncio.run(transcode_video_to_size("missing.mp4", 0))
        self.assertIn("目标体积无效", result.error)


class ProcessMetadataTranscodeTests(unittest.TestCase):
    """端到端：超过可发送上限时先压缩，压不下来才退回封面。"""

    def _run(self, transcode_result, downloaded_mb=129.08, **kwargs):
        seen = {}

        with tempfile.TemporaryDirectory() as cache_dir:
            params = {
                "max_video_size_mb": 1000,
                "send_video_max_mb": 100,
                "transcode_oversize_video": True,
            }
            params.update(kwargs)
            manager = DownloadManager(
                cache_dir=cache_dir, cache_dir_available=True, **params
            )

            async def fake_download(*, session, media_items, cache_dir):
                results = []
                for item in media_items:
                    is_video = item["kind"] == "video"
                    results.append(
                        {
                            "kind": item["kind"],
                            "position": item["position"],
                            "success": True,
                            "file_path": os.path.join(
                                cache_dir, str(item["position"]) + ".bin"
                            ),
                            "size_mb": downloaded_mb if is_video else 1.0,
                        }
                    )
                return results

            async def fake_transcode(path, target_bytes, *, timeout_seconds):
                seen["path"] = path
                seen["target_bytes"] = target_bytes
                seen["timeout_seconds"] = timeout_seconds
                return transcode_result

            manager._download_local_items = fake_download
            with mock.patch(
                "nova_core.downloader.manager.transcode_video_to_size",
                fake_transcode,
            ):
                result = asyncio.run(
                    manager.process_metadata(
                        session=None,
                        metadata={
                            "url": "https://www.youtube.com/watch?v=TNwnccdoxJQ",
                            "platform": "youtube",
                            # dash 形式让预检直接跳过，测试里没有真实网络。
                            "video_urls": [
                                ["dash:https://v/1.mp4||https://a/1.m4a"]
                            ],
                            "image_urls": [],
                        },
                    )
                )
        return result, seen

    def test_compressed_video_is_sent_with_a_note(self):
        result, seen = self._run(
            TranscodeResult(
                file_path="C:/tmp/clip_fit.mp4",
                size_mb=92.4,
                summary="129.1MB -> 92.4MB",
                note="129.1MB → 92.4MB（1080p → 720p）",
            )
        )
        self.assertEqual(result["video_modes"], ["local"])
        self.assertIsNone(result["video_skip_reasons"][0])
        self.assertEqual(result["file_paths"][0], "C:/tmp/clip_fit.mp4")
        self.assertEqual(result["video_sizes"], [92.4])
        self.assertEqual(
            result["video_transcode_notes"],
            ["129.1MB → 92.4MB（1080p → 720p）"],
        )
        self.assertFalse(result["send_limit_exceeded"])
        self.assertTrue(result["has_valid_media"])
        self.assertEqual(seen["target_bytes"], int(100 * MB))
        self.assertEqual(
            seen["timeout_seconds"], Config.DEFAULT_TRANSCODE_TIMEOUT_SECONDS
        )

    def test_failed_compression_falls_back_to_cover_with_reason(self):
        result, _ = self._run(
            TranscodeResult(error="2.0 小时的视频压到 100MB 后画质无法接受（至少需要 1160MB）")
        )
        self.assertEqual(result["video_modes"], ["skip"])
        reason = result["video_skip_reasons"][0]
        self.assertIn("可发送上限", reason)
        self.assertIn("自动压缩未成功", reason)
        self.assertIn("画质无法接受", reason)
        self.assertTrue(result["send_limit_exceeded"])
        self.assertEqual(result["video_transcode_notes"], [None])

    def test_still_oversize_after_compression_is_skipped(self):
        result, _ = self._run(
            TranscodeResult(
                file_path="C:/tmp/clip_fit.mp4",
                size_mb=118.0,
                note="129.1MB → 118.0MB（1080p → 720p）",
            )
        )
        self.assertEqual(result["video_modes"], ["skip"])
        self.assertIn("118.0MB", result["video_skip_reasons"][0])
        self.assertNotIn("自动压缩未成功", result["video_skip_reasons"][0])
        self.assertTrue(result["send_limit_exceeded"])
        self.assertEqual(result["video_transcode_notes"], [None])

    def test_video_within_cap_is_never_transcoded(self):
        result, seen = self._run(TranscodeResult(error="不该被调用"), downloaded_mb=42.0)
        self.assertEqual(result["video_modes"], ["local"])
        self.assertEqual(seen, {})
        self.assertEqual(result["video_transcode_notes"], [None])

    def test_switch_off_keeps_the_old_skip_behaviour(self):
        result, seen = self._run(
            TranscodeResult(error="不该被调用"),
            transcode_oversize_video=False,
        )
        self.assertEqual(seen, {})
        self.assertEqual(result["video_modes"], ["skip"])
        reason = result["video_skip_reasons"][0]
        self.assertIn("聊天平台会拒收", reason)
        self.assertNotIn("自动压缩", reason)

    def test_max_cap_breach_is_not_compressed(self):
        result, seen = self._run(
            TranscodeResult(error="不该被调用"), downloaded_mb=1200.0
        )
        self.assertEqual(seen, {})
        self.assertEqual(result["video_modes"], ["skip"])
        self.assertTrue(result["exceeds_max_size"])


class TranscodeNoticeTests(unittest.TestCase):
    """压缩结果要在消息里说明，避免画质变化显得莫名其妙。"""

    def test_single_video_note(self):
        node = build_media_notice_node(
            {
                "video_count": 1,
                "video_urls": [["https://v/1.mp4"]],
                "video_transcode_notes": ["129.1MB → 92.4MB（1080p → 720p）"],
            }
        )
        self.assertIsNotNone(node)
        self.assertIn("视频已压缩：129.1MB → 92.4MB（1080p → 720p）", node.text)

    def test_multiple_videos_are_numbered(self):
        node = build_media_notice_node(
            {
                "video_count": 2,
                "video_urls": [["https://v/1.mp4"], ["https://v/2.mp4"]],
                "video_transcode_notes": [None, "200.0MB → 95.0MB（1080p → 540p）"],
            }
        )
        self.assertIn("视频[2]已压缩", node.text)

    def test_no_note_means_no_extra_output(self):
        self.assertIsNone(
            build_media_notice_node(
                {
                    "video_count": 1,
                    "video_urls": [["https://v/1.mp4"]],
                    "video_transcode_notes": [None],
                }
            )
        )


if __name__ == "__main__":
    unittest.main()