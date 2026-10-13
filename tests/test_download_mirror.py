import asyncio
import os
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlsplit

import aiohttp

from nova_core.downloader import mirror
from nova_core.downloader.handler import base


_BAD_MIRROR_URL = (
    "https://upos-sz-mirrorcosov.bilivideo.com/upgcxcode/41/77/42345367741/"
    "42345367741-1-192.mp4?os=cosovbv&upsig=abc123&uparams=e,os"
)
_AKAMAI_HOST = "upos-hz-mirrorakam.akamaized.net"


async def _noop_sleep(attempt):
    """测试中跳过指数退避等待。"""
    return None


class _FakeContent:
    """模拟 aiohttp response.content：按脚本产出分片。"""

    def __init__(self, data):
        self._data = data

    def iter_chunked(self, size):
        data = self._data
        chunk_size = max(1, int(size))

        async def _gen():
            for offset in range(0, len(data), chunk_size):
                yield data[offset : offset + chunk_size]

        return _gen()


class _FakeResponse:
    """模拟 download_media_from_url 需要的响应接口。"""

    def __init__(self, body=b"", headers=None, status=200):
        self.status = status
        self.headers = headers or {}
        self.content = _FakeContent(body)

    def raise_for_status(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class MirrorCandidateTests(unittest.TestCase):
    """候选源生成：只认 B站 CDN，原始链接优先，Akamai 作为首选替补。"""

    def test_bilibili_cdn_detection(self):
        self.assertTrue(mirror.is_bilibili_cdn_url(_BAD_MIRROR_URL))
        self.assertTrue(
            mirror.is_bilibili_cdn_url(
                "https://upos-sz-mirrorali.bilivideo.com/x/y.mp4"
            )
        )
        self.assertTrue(
            mirror.is_bilibili_cdn_url(
                "https://upos-hz-mirrorakam.akamaized.net/x/y.mp4"
            )
        )
        self.assertFalse(mirror.is_bilibili_cdn_url("https://cdn/video.mp4"))
        self.assertFalse(mirror.is_bilibili_cdn_url(""))
        self.assertFalse(mirror.is_bilibili_cdn_url(None))

    def test_non_bilibili_url_is_single_candidate(self):
        url = "https://cdn.example.com/video.mp4?token=1"
        self.assertEqual(mirror.build_mirror_candidates(url), [url])

    def test_bilibili_url_keeps_signature_and_swaps_host(self):
        candidates = mirror.build_mirror_candidates(_BAD_MIRROR_URL)
        self.assertEqual(candidates[0], _BAD_MIRROR_URL)
        self.assertGreater(len(candidates), 1)
        second = candidates[1]
        self.assertIn(_AKAMAI_HOST, second)
        self.assertIn("upsig=abc123", second)
        self.assertIn("/upgcxcode/41/77/42345367741/", second)

    def test_existing_akamai_host_is_not_duplicated(self):
        url = mirror.swap_url_host(_BAD_MIRROR_URL, _AKAMAI_HOST)
        candidates = mirror.build_mirror_candidates(url)
        self.assertEqual(candidates[0], url)
        self.assertEqual(len(candidates), len(set(candidates)))
        self.assertNotEqual(candidates[1], url)

    def test_candidate_limit(self):
        candidates = mirror.build_mirror_candidates(_BAD_MIRROR_URL, max_candidates=2)
        self.assertEqual(len(candidates), 2)


class RangeMirrorFallbackTests(unittest.TestCase):
    """坏镜像在文件大小探测阶段就换源，不再死磕同一个节点。"""

    def test_range_file_size_probe_switches_mirror(self):
        file_size = 3 * 1024 * 1024
        probed_hosts = []

        async def fake_get_size(session, url, headers, proxy):
            probed_hosts.append(urlsplit(url).netloc)
            return None if "mirrorcosov" in url else file_size

        async def fake_download_range(
            session, url, start, end, headers, proxy, chunk_index, total
        ):
            return b"\x00" * (end - start + 1)

        with tempfile.TemporaryDirectory() as cache_dir:
            out = os.path.join(cache_dir, "range.mp4")
            with mock.patch.object(base, "_get_file_size", fake_get_size), mock.patch.object(
                base, "_download_range", fake_download_range
            ):
                result = asyncio.run(
                    base.range_download_file(
                        session=None,
                        url=_BAD_MIRROR_URL,
                        output_path=out,
                    )
                )

            self.assertIsNotNone(result)
            self.assertTrue(os.path.exists(out))
            self.assertEqual(os.path.getsize(out), file_size)
            self.assertGreaterEqual(len(probed_hosts), 2)
            self.assertIn(_AKAMAI_HOST, probed_hosts[1])

    def test_range_chunk_uses_single_attempt_per_mirror(self):
        file_size = 3 * 1024 * 1024
        calls = {"chunks": 0}

        async def fake_get_size(session, url, headers, proxy):
            return file_size

        async def fake_download_range(
            session, url, start, end, headers, proxy, chunk_index, total
        ):
            calls["chunks"] += 1
            return None

        with tempfile.TemporaryDirectory() as cache_dir:
            out = os.path.join(cache_dir, "range.mp4")
            with mock.patch.object(base, "_get_file_size", fake_get_size), mock.patch.object(
                base, "_download_range", fake_download_range
            ):
                result = asyncio.run(
                    base.range_download_file(
                        session=None,
                        url=_BAD_MIRROR_URL,
                        output_path=out,
                    )
                )

        # 多候选源每个镜像每片只尝试 1 次：2 个分片 × 全部候选，不做片内重复
        self.assertIsNone(result)
        expected_candidates = len(mirror.build_mirror_candidates(_BAD_MIRROR_URL))
        self.assertEqual(calls["chunks"], 2 * expected_candidates)

    def test_non_bilibili_url_keeps_single_attempt_per_chunk(self):
        file_size = 3 * 1024 * 1024
        calls = {"chunks": 0}

        async def fake_get_size(session, url, headers, proxy):
            return file_size

        async def fake_download_range(
            session, url, start, end, headers, proxy, chunk_index, total
        ):
            calls["chunks"] += 1
            return None

        with tempfile.TemporaryDirectory() as cache_dir:
            out = os.path.join(cache_dir, "range.mp4")
            with mock.patch.object(base, "_get_file_size", fake_get_size), mock.patch.object(
                base, "_download_range", fake_download_range
            ), mock.patch.object(base, "_sleep_before_retry", _noop_sleep):
                result = asyncio.run(
                    base.range_download_file(
                        session=None,
                        url="https://cdn.example.com/video.mp4",
                        output_path=out,
                    )
                )

        self.assertIsNone(result)
        # 单候选源仍按配置重试：2 个分片 × 默认 3 次
        self.assertEqual(calls["chunks"], 2 * base.Config.DOWNLOAD_RETRY_ATTEMPTS)


class NormalMirrorFallbackTests(unittest.TestCase):
    """普通流式下载：主镜像失败后换源并成功返回文件。"""

    def test_download_media_from_url_switches_mirror(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            path = os.path.join(cache_dir, "out.mp4")
            body = b"x" * 1024
            calls = {"urls": []}

            async def fake_safe_request(session, method, url, **kwargs):
                calls["urls"].append(url)
                if len(calls["urls"]) == 1:
                    raise aiohttp.ClientResponseError(
                        request_info=None, history=(), status=403, message="Forbidden"
                    )
                headers = {
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(body)),
                }
                return _FakeResponse(body=body, headers=headers)

            async def fake_validate(response, url, *, is_video, allow_read_content):
                return True, b""

            def path_gen(content_type, media_url):
                return path

            with mock.patch.object(base, "safe_request", fake_safe_request), mock.patch.object(
                base, "validate_media_response", fake_validate
            ):
                result = asyncio.run(
                    base.download_media_from_url(
                        session=None,
                        media_url=_BAD_MIRROR_URL,
                        file_path_generator=path_gen,
                        is_video=True,
                    )
                )

            file_path, size_mb, status, error = result
            self.assertIsNone(error)
            self.assertTrue(os.path.exists(file_path))
            self.assertEqual(os.path.getsize(file_path), len(body))
            self.assertEqual(len(calls["urls"]), 2)
            self.assertIn("mirrorcosov", calls["urls"][0])
            self.assertIn(_AKAMAI_HOST, calls["urls"][1])


if __name__ == "__main__":
    unittest.main()
