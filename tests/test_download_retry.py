import asyncio
import os
import tempfile
import unittest
from unittest import mock

import aiohttp

from nova_core.downloader.handler import base


class _FakeContent:
    """模拟 aiohttp response.content：按脚本产出分片或抛异常。"""

    def __init__(self, script):
        # script: list of ("data", b"..") 或 ("raise", exc)
        self._script = script

    def iter_chunked(self, size):
        script = self._script

        async def _gen():
            for kind, value in script:
                if kind == "raise":
                    raise value
                yield value

        return _gen()


class _FakeResponse:
    """模拟 download_media_from_url / download_media_stream 需要的响应接口。"""

    def __init__(self, script, headers=None, status=200):
        self.status = status
        self.headers = headers or {}
        self.content = _FakeContent(script)

    def raise_for_status(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class DownloadStreamRetryTests(unittest.TestCase):
    """流式下载：中途断连要向上冒泡以便重试，非可重试错误仍吞掉。"""

    def _run_stream(self, script):
        with tempfile.TemporaryDirectory() as cache_dir:
            path = os.path.join(cache_dir, "out.mp4")
            resp = _FakeResponse(script)
            return asyncio.run(base.download_media_stream(resp, path, is_video=True))

    def test_payload_error_is_reraised(self):
        with self.assertRaises(aiohttp.ClientPayloadError):
            self._run_stream(
                [("raise", aiohttp.ClientPayloadError("Response payload is not completed"))]
            )

    def test_non_retryable_error_returns_false(self):
        self.assertFalse(self._run_stream([("raise", ValueError("broken"))]))

    def test_payload_error_classified_retryable(self):
        self.assertTrue(base._is_retryable_exception(aiohttp.ClientPayloadError("x")))
        self.assertFalse(base._is_retryable_exception(ValueError("x")))


class DownloadFromUrlRetryTests(unittest.TestCase):
    """download_media_from_url：流式中途断连应自动重试并最终成功。"""

    def test_retries_after_stream_payload_error(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            path = os.path.join(cache_dir, "video.mp4")

            responses = [
                _FakeResponse(
                    [("raise", aiohttp.ClientPayloadError("payload not completed"))]
                ),
                _FakeResponse([("data", b"abc" * 16)]),
            ]
            calls = {"n": 0}

            async def fake_safe_request(session, method, url, **kwargs):
                idx = calls["n"]
                calls["n"] += 1
                return responses[idx]

            async def fake_validate(response, url, *, is_video, allow_read_content):
                return True, b""

            async def noop_sleep(attempt):
                return None

            def path_gen(content_type, media_url):
                return path

            with mock.patch.object(base, "safe_request", fake_safe_request), mock.patch.object(
                base, "validate_media_response", fake_validate
            ), mock.patch.object(base, "_sleep_before_retry", noop_sleep):
                result = asyncio.run(
                    base.download_media_from_url(
                        session=None,
                        media_url="https://cdn/video.mp4",
                        file_path_generator=path_gen,
                        is_video=True,
                    )
                )

            file_path, size_mb, status, error = result
            self.assertIsNone(error)
            self.assertEqual(calls["n"], 2)  # 第一次失败后确实重试了
            self.assertTrue(os.path.exists(file_path))


class RangeChunkRetryTests(unittest.TestCase):
    """Range 分片：单片瞬时失败要按配置重试，而不是整段放弃。"""

    def test_chunk_retries_on_transient_none(self):
        file_size = 3 * 1024 * 1024  # 2MB + 1MB -> 2 chunks
        attempts = {"n": 0}

        async def fake_get_size(session, url, headers, proxy):
            return file_size

        async def fake_download_range(
            session, url, start, end, headers, proxy, chunk_index, total
        ):
            attempts["n"] += 1
            # 第一次调用（任意分片）模拟 CDN 提前断连
            if attempts["n"] == 1:
                return None
            return b"\x00" * (end - start + 1)

        async def noop_sleep(attempt):
            return None

        with tempfile.TemporaryDirectory() as cache_dir:
            out = os.path.join(cache_dir, "range.mp4")
            with mock.patch.object(base, "_get_file_size", fake_get_size), mock.patch.object(
                base, "_download_range", fake_download_range
            ), mock.patch.object(base, "_sleep_before_retry", noop_sleep):
                result = asyncio.run(
                    base.range_download_file(
                        session=None,
                        url="https://cdn/video.mp4",
                        output_path=out,
                    )
                )

            self.assertIsNotNone(result)
            self.assertTrue(os.path.exists(out))
            self.assertEqual(os.path.getsize(out), file_size)
            # 2 个分片 + 至少 1 次重试
            self.assertGreater(attempts["n"], 2)


if __name__ == "__main__":
    unittest.main()
