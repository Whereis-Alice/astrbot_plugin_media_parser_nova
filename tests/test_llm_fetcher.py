import asyncio
import json
import re
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from nova_core.config_manager import ConfigManager
from nova_core.llm_fetcher import MediaContentFetcher, _run_record_operation
from nova_core.llm_tool import CoreNovaFetchContentTool
from nova_core.storage import ParseRecordManager


def event(sender="123", private=False, group="456"):
    return SimpleNamespace(
        get_sender_id=lambda: sender,
        get_group_id=lambda: group,
        is_private_chat=lambda: private,
        get_self_id=lambda: "999",
        get_platform_name=lambda: "test",
        send=Mock(side_effect=AssertionError("Tool must not send chat messages")),
    )


class Parser:
    name = "steam"

    def extract_links(self, text):
        return re.findall(r"https://store\.steampowered\.com/app/\d+", text)

    async def parse(self, session, url):
        return {
            "url": url,
            "title": "Game " + url.rsplit("/", 1)[-1],
            "desc": "游戏正文",
            "hot_comments": [
                {"message": "玩家评测", "likes": 3, "likes_label": "有帮助"}
            ],
            "image_urls": [["https://cdn.example.com/screenshot.png"]],
            "video_headers": {"Cookie": "secret"},
            "proxy_url": "http://user:password@proxy.example.com",
        }


class LLMFetcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = ConfigManager({})
        self.parser = Parser()
        self.cfg.create_parsers = Mock(return_value=[self.parser])
        self.records = ParseRecordManager(record_file="")
        self.fetcher = MediaContentFetcher(self.cfg, self.records)

    async def asyncTearDown(self):
        await self.fetcher.aclose()

    async def test_tool_schema_and_current_event_bridge(self):
        tool = CoreNovaFetchContentTool(fetcher=self.fetcher)
        self.assertIsNone(tool.handler)
        self.assertEqual(tool.parameters["required"], ["url"])
        output = await tool.call(
            SimpleNamespace(context=SimpleNamespace(event=event())),
            url="https://store.steampowered.com/app/1",
        )
        result = json.loads(output)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["results"][0]["desc"], "游戏正文")
        self.assertNotIn("secret", output)
        self.assertNotIn("password", output)

    async def test_permission_checked_before_parsers_and_rate_records(self):
        self.cfg.permission.whitelist_enable = True
        self.records.filter_links = Mock(
            side_effect=AssertionError("No rate mutation before permission")
        )
        output = json.loads(
            await self.fetcher.fetch(
                event(), url="https://store.steampowered.com/app/1"
            )
        )
        self.assertEqual(output["error"]["code"], "permission_denied")
        self.cfg.create_parsers.assert_not_called()

    async def test_missing_context_self_message_and_disabled_are_rejected(self):
        for ev in (None, event(sender="999"), event(sender="")):
            with self.subTest(ev=ev):
                self.assertEqual(
                    json.loads(
                        await self.fetcher.fetch(
                            ev, url="https://store.steampowered.com/app/1"
                        )
                    )["error"]["code"],
                    "permission_denied",
                )
        self.cfg.llm_tool.enabled = False
        self.assertEqual(
            json.loads(
                await self.fetcher.fetch(
                    event(), url="https://store.steampowered.com/app/1"
                )
            )["error"]["code"],
            "disabled",
        )
        self.cfg.create_parsers.assert_not_called()

    async def test_runtime_argument_validation(self):
        for args in (
            {},
            {"url": ""},
            {"url": "x" * 12001},
            {"url": 4},
            {"url": "x", "include_media": "false"},
            {"url": "x", "max_comments": True},
            {"url": "x", "max_comments": 21},
            {"url": "x", "sender_id": "admin"},
        ):
            with self.subTest(args=args):
                result = json.loads(await self.fetcher.fetch(event(), **args))
                self.assertEqual(result["error"]["code"], "invalid_arguments")
        self.cfg.create_parsers.assert_not_called()

    async def test_comment_and_media_controls_passed_to_isolated_factory(self):
        result = json.loads(
            await self.fetcher.fetch(
                event(),
                url="https://store.steampowered.com/app/1",
                include_comments=False,
                include_media=False,
            )
        )
        self.cfg.create_parsers.assert_called_once_with(
            for_llm=True, include_media=False, hot_comment_count=0
        )
        self.assertFalse(result["media_requested"])
        self.assertFalse(result["results"][0].get("images"))
        self.assertFalse(result["results"][0].get("hot_comments"))

    async def test_link_limit_deduplication_and_order(self):
        self.cfg.llm_tool.max_links = 2
        result = json.loads(
            await self.fetcher.fetch(
                event(),
                url=" ".join(
                    f"https://store.steampowered.com/app/{n}" for n in (2, 1, 2, 3)
                ),
            )
        )
        self.assertEqual(
            [item["title"] for item in result["results"]], ["Game 2", "Game 1"]
        )
        self.assertEqual(result["omitted_link_count"], 1)
        self.assertEqual(result["status"], "partial")

    async def test_rate_limits_shared_with_chat_and_final_aliases_recorded(self):
        self.records = ParseRecordManager(
            record_file="", same_user_max_count=1, same_user_window_seconds=60
        )
        self.fetcher.records = self.records
        self.records.filter_links(
            [("https://store.steampowered.com/app/2", self.parser)], user_key="test:123"
        )
        result = json.loads(
            await self.fetcher.fetch(
                event(), url="https://store.steampowered.com/app/1"
            )
        )
        self.assertEqual(result["error"]["code"], "rate_limited")

    async def test_one_failed_link_does_not_hide_other_content_or_exception_secrets(
        self,
    ):
        original = self.parser.parse

        async def parse(session, url):
            if url.endswith("/2"):
                # Return an error as parsers do; never expose it to the model.
                return {"error": "Cookie=secret /root/private.json", "source_url": url}
            return await original(session, url)

        self.parser.parse = parse
        output = await self.fetcher.fetch(
            event(),
            url="https://store.steampowered.com/app/1 https://store.steampowered.com/app/2",
        )
        result = json.loads(output)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["results"][0]["status"], "ok")
        self.assertEqual(result["results"][1]["status"], "error")
        self.assertNotIn("secret", output)
        self.assertNotIn("/root/private", output)

    async def test_timeout_preserves_fast_link(self):
        self.cfg.llm_tool.timeout_seconds = 0.02
        original = self.parser.parse

        async def parse(session, url):
            if url.endswith("/2"):
                await asyncio.Event().wait()
            return await original(session, url)

        self.parser.parse = parse
        result = json.loads(
            await self.fetcher.fetch(
                event(),
                url="https://store.steampowered.com/app/1 https://store.steampowered.com/app/2",
            )
        )
        self.assertEqual(result["status"], "partial")
        self.assertIn("超时", result["results"][1]["error"])

    async def test_concurrency_limit_cancel_and_shutdown(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def parse(session, url):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.parser.parse = parse
        self.cfg.llm_tool.max_concurrent_calls = 1
        first = asyncio.create_task(
            self.fetcher.fetch(event(), url="https://store.steampowered.com/app/1")
        )
        await entered.wait()
        result = json.loads(
            await self.fetcher.fetch(
                event(), url="https://store.steampowered.com/app/2"
            )
        )
        self.assertEqual(result["error"]["code"], "busy")
        await self.fetcher.aclose()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(self.fetcher._tasks), 0)

    async def test_unsafe_source_and_unsupported_are_not_fetched(self):
        self.parser.extract_links = lambda text: [text]
        for url in (
            "http://127.0.0.1/a",
            "http://10.0.0.1/a",
            "file:///root/data",
            "https://user:pass@example.com/a",
            "http://localhost/a",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    json.loads(await self.fetcher.fetch(event(), url=url))["error"][
                        "code"
                    ],
                    "unsupported_url",
                )

    async def test_shutdown_drains_in_progress_persistent_record_thread(self):
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        self.records.same_link.max_count = 1
        self.records.same_link.window_seconds = 60

        def blocking_filter(links, *, user_key):
            loop.call_soon_threadsafe(entered.set)
            release.wait(5)
            return links, []

        self.records.filter_links = blocking_filter
        first = asyncio.create_task(
            self.fetcher.fetch(event(), url="https://store.steampowered.com/app/1")
        )
        await entered.wait()
        closing = asyncio.create_task(self.fetcher.aclose())
        try:
            await asyncio.sleep(0.02)
            self.assertFalse(closing.done())
        finally:
            release.set()
        await closing
        with self.assertRaises(asyncio.CancelledError):
            await first

    async def test_cancelled_record_failure_does_not_log_raw_worker_exception(self):
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        exceptions = []
        original_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda loop, context: exceptions.append(context))

        def fail():
            loop.call_soon_threadsafe(entered.set)
            release.wait(5)
            raise ValueError("credential-bearing exception")

        task = asyncio.create_task(_run_record_operation(fail))
        try:
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
            self.assertEqual(exceptions, [])
        finally:
            release.set()
            loop.set_exception_handler(original_handler)
