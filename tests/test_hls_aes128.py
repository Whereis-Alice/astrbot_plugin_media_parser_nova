import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from nova_core.downloader.budget import ByteBudget
from nova_core.downloader.handler.m3u8 import M3U8DownloadError, M3U8Handler


class Response:
    def __init__(self, payload):
        self.payload = payload
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def raise_for_status(self):
        pass

    async def iter_chunked(self, size):
        # Arbitrary boundaries exercise the streaming block/padding buffers.
        step = min(7, size)
        for index in range(0, len(self.payload), step):
            yield self.payload[index : index + step]


def encrypt(plain, key, iv):
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(padded) + enc.finalize()


class HLSAES128Tests(unittest.IsolatedAsyncioTestCase):
    async def test_media_sequence_iv_and_key_rotation(self):
        handler = M3U8Handler(None)
        handler.fetch_text = AsyncMock(
            return_value="""#EXTM3U
#EXT-X-MEDIA-SEQUENCE:41
#EXT-X-KEY:METHOD=AES-128,URI="keys/first"
one.ts
two.ts
#EXT-X-KEY:METHOD=AES-128,URI="keys/second",IV=0x000000000000000000000000000000ff
three.ts
#EXT-X-KEY:METHOD=NONE
clear.ts
"""
        )
        init, resources = await handler.parse_m3u8(
            "https://cdn.example/movie/index.m3u8"
        )
        self.assertIsNone(init)
        self.assertEqual(resources[0], "https://cdn.example/movie/one.ts")
        self.assertEqual(resources[0].key_uri, "https://cdn.example/movie/keys/first")
        self.assertEqual(resources[0].iv, (41).to_bytes(16, "big"))
        self.assertEqual(resources[1].iv, (42).to_bytes(16, "big"))
        self.assertEqual(resources[2].iv, (255).to_bytes(16, "big"))
        self.assertFalse(hasattr(resources[3], "key_uri"))

    async def test_aes_streaming_download_and_shared_key_budget(self):
        key = b"niconico-key-128"
        self.assertEqual(len(key), 16)
        handler = M3U8Handler(
            None, headers={"Cookie": "domand_bid=opaque"}, proxy="http://proxy:80"
        )
        handler.fetch_text = AsyncMock(
            return_value="""#EXTM3U
#EXT-X-MEDIA-SEQUENCE:7
#EXT-X-KEY:METHOD=AES-128,URI="key"
one.ts
two.ts
"""
        )
        _, resources = await handler.parse_m3u8("https://cdn.example/index.m3u8")
        plain = [b"first media segment" * 4, b"second media segment" * 5]
        payloads = {
            str(resource): encrypt(data, key, resource.iv)
            for resource, data in zip(resources, plain)
        }
        payloads["https://cdn.example/key"] = key
        calls = []

        async def request(*_args, **kwargs):
            calls.append((_args[2], kwargs))
            return Response(payloads[str(_args[2])])

        budget = ByteBudget(4096)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "nova_core.downloader.handler.m3u8.safe_request", side_effect=request
            ),
        ):
            files = await handler.download_segments(resources, directory, budget=budget)
            for path, expected in zip(files, plain):
                with open(path, "rb") as stream:
                    self.assertEqual(stream.read(), expected)
            self.assertEqual(
                [name for name in os.listdir(directory) if not name.startswith(".")],
                ["seg_00000.m4s", "seg_00001.m4s"],
            )
        key_calls = [call for call in calls if call[0] == "https://cdn.example/key"]
        self.assertEqual(len(key_calls), 1)
        self.assertEqual(key_calls[0][1]["headers"]["Cookie"], "domand_bid=opaque")
        self.assertEqual(key_calls[0][1]["proxy"], "http://proxy:80")
        self.assertEqual(
            budget.used,
            16
            + sum(
                len(value)
                for name, value in payloads.items()
                if name != "https://cdn.example/key"
            ),
        )

    async def test_encrypted_initialization_segment(self):
        key = b"0123456789abcdef"
        iv = b"\x00" * 16
        handler = M3U8Handler(None)
        handler.fetch_text = AsyncMock(
            return_value="""#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="key",IV=0x00000000000000000000000000000000
#EXT-X-MAP:URI="init.mp4"
seg.m4s
"""
        )
        init, _ = await handler.parse_m3u8("https://cdn.example/index.m3u8")
        payload = b"initialization-boxes"
        handler._get_aes_key = AsyncMock(return_value=key)
        with patch(
            "nova_core.downloader.handler.m3u8.safe_request",
            AsyncMock(return_value=Response(encrypt(payload, key, iv))),
        ):
            self.assertEqual(await handler.fetch_bytes(init), payload)

    async def test_drm_malformed_iv_and_missing_map_iv_are_rejected(self):
        for line in (
            '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key"',
            '#EXT-X-KEY:METHOD=AES-128,KEYFORMAT="com.apple.streamingkeydelivery",URI="key"',
            '#EXT-X-KEY:METHOD=AES-128,URI="key",IV=0x01',
            "#EXT-X-KEY:METHOD=AES-128",
            '#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXT-X-MAP:URI="init.mp4"',
        ):
            handler = M3U8Handler(None)
            handler.fetch_text = AsyncMock(
                return_value="#EXTM3U\n" + line + "\nseg.ts\n"
            )
            with self.subTest(line=line), self.assertRaises(M3U8DownloadError):
                await handler.parse_m3u8("https://cdn.example/index.m3u8")

    async def test_invalid_key_lengths_rejected_without_budget_leak(self):
        for payload in (b"short", b"a" * 17, b"a" * 1024):
            handler = M3U8Handler(None)
            budget = ByteBudget(4096)
            with (
                self.subTest(length=len(payload)),
                patch(
                    "nova_core.downloader.handler.m3u8.safe_request",
                    AsyncMock(return_value=Response(payload)),
                ),
            ):
                with self.assertRaisesRegex(M3U8DownloadError, "16字节"):
                    await handler._get_aes_key("https://cdn.example/key", budget)
                self.assertEqual(budget.used, 0)

    async def test_wrong_ciphertext_cleans_partial_file_and_returns_budget(self):
        handler = M3U8Handler(None)
        handler.fetch_text = AsyncMock(
            return_value='#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\nseg.ts\n'
        )
        _, resources = await handler.parse_m3u8("https://cdn.example/index.m3u8")
        handler._get_aes_key = AsyncMock(return_value=b"0123456789abcdef")
        budget = ByteBudget(4096)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "nova_core.downloader.handler.m3u8.safe_request",
                AsyncMock(return_value=Response(b"not-a-block")),
            ),
        ):
            with self.assertRaisesRegex(M3U8DownloadError, "解密失败"):
                await handler.download_file(
                    resources[0], os.path.join(directory, "output.mp4"), budget
                )
            self.assertFalse(os.path.exists(directory) and os.listdir(directory))
            self.assertEqual(budget.used, 0)

    async def test_byte_limit_still_applies_to_encrypted_media(self):
        handler = M3U8Handler(None)
        handler.fetch_text = AsyncMock(
            return_value='#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\nseg.ts\n'
        )
        _, resources = await handler.parse_m3u8("https://cdn.example/index.m3u8")
        key = b"0123456789abcdef"
        handler._get_aes_key = AsyncMock(return_value=key)
        budget = ByteBudget(20)
        encrypted = encrypt(b"a" * 100, key, resources[0].iv)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "nova_core.downloader.handler.m3u8.safe_request",
                AsyncMock(return_value=Response(encrypted)),
            ),
        ):
            with self.assertRaisesRegex(M3U8DownloadError, "硬限制"):
                await handler.download_file(
                    resources[0], os.path.join(directory, "output.mp4"), budget
                )
            self.assertFalse(os.path.exists(directory) and os.listdir(directory))
            self.assertEqual(budget.used, 0)

    async def test_cancellation_of_key_request_propagates(self):
        handler = M3U8Handler(None)
        with patch(
            "nova_core.downloader.handler.m3u8.safe_request",
            AsyncMock(side_effect=asyncio.CancelledError),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await handler._get_aes_key("https://cdn.example/key", ByteBudget(100))


if __name__ == "__main__":
    unittest.main()
