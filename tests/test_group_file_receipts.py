"""Exercise real aiocqhttp receipt waiting without sending anything to QQ."""

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiocqhttp import CQHttp
from aiocqhttp.api_impl import HttpApi, ResultStore, UnifiedApi, WebSocketReverseApi
from aiocqhttp.exceptions import NetworkError
from astrbot.api.message_components import Plain

from nova_core.message_adapter.group_file import (
    GroupFileUploader,
    GroupFileUploadError,
    GroupFileUploadUnconfirmed,
    _scoped_upload_api,
)
from nova_core.message_adapter.sender import MessageSender


def make_event(bot, *, private=False):
    return SimpleNamespace(
        bot=bot,
        get_group_id=lambda: "" if private else "123456",
        is_private_chat=lambda: private,
        get_sender_id=lambda: "456789",
        get_self_id=lambda: "987654",
        get_platform_name=lambda: "aiocqhttp",
        send=AsyncMock(),
        chain_result=lambda chain: chain,
        plain_result=lambda text: [Plain(text)],
    )


def make_bot(api):
    bot = CQHttp.__new__(CQHttp)
    bot._api = api
    return bot


@pytest.mark.parametrize("private", [False, True])
def test_late_websocket_receipt_obeys_upload_timeout_not_shared_timeout(
    tmp_path, private
):
    async def run():
        sent = []
        loop = asyncio.get_running_loop()

        async def send(raw):
            request = json.loads(raw)
            sent.append(request)
            loop.call_later(
                0.04,
                ResultStore.add,
                {
                    "echo": request["echo"],
                    "status": "ok",
                    "retcode": 0,
                    "data": {"file_id": "uploaded"},
                },
            )

        ws = SimpleNamespace(send=send)
        original = WebSocketReverseApi({"987654": ws}, set(), 0.005)
        bot = make_bot(UnifiedApi(wsr_api=original))
        event = make_event(bot, private=private)
        path = tmp_path / "video.mp4"
        path.write_bytes(b"video")
        uploader = GroupFileUploader(600)
        uploader.timeout_seconds = 0.5
        upload_task = asyncio.create_task(
            uploader.upload(event, str(path), "video.mp4")
        )
        # An unrelated API call must still use the shared client's short deadline.
        with pytest.raises(NetworkError):
            await bot.call_action("get_group_info", self_id="987654", group_id=123456)
        result = await upload_task
        assert result["file_id"] == "uploaded"
        uploads = [
            request
            for request in sent
            if request["action"]
            == ("upload_private_file" if private else "upload_group_file")
        ]
        assert len(uploads) == 1
        assert uploads[0]["params"]["self_id"] == "987654"
        assert original._timeout_sec == 0.005
        assert bot._api._wsr_api is original
        assert all(
            request["echo"]["seq"] not in ResultStore._futures for request in sent
        )

    asyncio.run(run())


def test_independent_upload_timeouts_share_connections_only():
    original_ws = WebSocketReverseApi({}, set(), 180)
    original_http = HttpApi("http://localhost:1234", None, 180)
    bot = make_bot(UnifiedApi(wsr_api=original_ws, http_api=original_http))
    first = _scoped_upload_api(bot, 600)
    second = _scoped_upload_api(bot, 1200)
    assert first._wsr_api._api_clients is original_ws._api_clients
    assert first._wsr_api._timeout_sec == first._http_api._timeout_sec == 600
    assert second._wsr_api._timeout_sec == second._http_api._timeout_sec == 1200
    assert original_ws._timeout_sec == original_http._timeout_sec == 180


def test_custom_bot_wrapper_is_not_bypassed():
    bot = make_bot(UnifiedApi())
    bot.call_action = AsyncMock()
    assert _scoped_upload_api(bot, 600) is None


@pytest.mark.parametrize(
    "error",
    [
        asyncio.TimeoutError(),
        NetworkError("WebSocket API call timeout"),
        NetworkError("HTTP request failed"),
        RuntimeError("connection closed"),
    ],
)
def test_timeout_is_unconfirmed_and_upload_is_not_retried(tmp_path, error):
    bot = SimpleNamespace(call_action=AsyncMock(side_effect=error))
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    with pytest.raises(GroupFileUploadUnconfirmed):
        asyncio.run(GroupFileUploader().upload(make_event(bot), str(path), path.name))
    assert bot.call_action.await_count == 1
    assert path.exists()


def test_outer_deadline_is_also_unconfirmed(tmp_path):
    async def run():
        called = 0

        async def upload(*args, **kwargs):
            nonlocal called
            called += 1
            await asyncio.Future()

        path = tmp_path / "video.mp4"
        path.write_bytes(b"video")
        event = make_event(SimpleNamespace(call_action=upload))
        uploader = GroupFileUploader()
        uploader.timeout_seconds = 0.01
        with pytest.raises(GroupFileUploadUnconfirmed):
            await uploader.upload(event, str(path), path.name)
        assert called == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "response, error_type",
    [
        (
            {"status": "failed", "retcode": 1200, "wording": "没有群文件权限"},
            GroupFileUploadError,
        ),
        ({"status": "async", "retcode": 1}, GroupFileUploadUnconfirmed),
    ],
)
def test_raw_onebot_failure_or_async_ack_is_not_completed(
    tmp_path, response, error_type
):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    bot = SimpleNamespace(call_action=AsyncMock(return_value=response))
    with pytest.raises(error_type) as caught:
        asyncio.run(GroupFileUploader().upload(make_event(bot), str(path), path.name))
    assert type(caught.value) is error_type


@pytest.mark.parametrize("aggregated", [False, True])
def test_unconfirmed_upload_does_not_claim_rejection_and_keeps_pending(aggregated):
    event = make_event(None)
    uploader = SimpleNamespace(
        upload=AsyncMock(side_effect=GroupFileUploadUnconfirmed("回执超时"))
    )
    sender = MessageSender(group_file_uploader=uploader)
    meta = [{"link_nodes": [], "group_files": [{"path": "/cache/video.mp4"}]}]
    if aggregated:
        asyncio.run(sender.send_aggregated_results(event, meta, "Nova", "987654"))
    else:
        asyncio.run(sender.send_individual_results(event, [[]], meta))
    notice = event.send.await_args.args[0][0].text
    assert "尚未确认" in notice
    for wrong in ("未能发出", "体积过大", "拒收", "1/1"):
        assert wrong not in notice
    assert sender.pending_group_file_paths(meta) == {"/cache/video.mp4"}
    assert uploader.upload.await_count == 1


def test_cancellation_keeps_upload_source_pending():
    event = make_event(None)
    sender = MessageSender(
        group_file_uploader=SimpleNamespace(
            upload=AsyncMock(side_effect=asyncio.CancelledError()),
        )
    )
    meta = [{"group_files": [{"path": "/cache/video.mp4"}]}]
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sender.send_individual_results(event, [[]], meta))
    assert sender.pending_group_file_paths(meta) == {"/cache/video.mp4"}
    event.send.assert_not_awaited()


def test_explicit_refusal_remains_failure_without_false_size_diagnosis():
    event = make_event(None)
    sender = MessageSender(
        group_file_uploader=SimpleNamespace(
            upload=AsyncMock(
                side_effect=GroupFileUploadError("群文件上传失败：权限不足")
            ),
        )
    )
    meta = [{"group_files": [{"path": "/cache/video.mp4"}]}]
    asyncio.run(sender.send_individual_results(event, [[]], meta))
    notice = event.send.await_args.args[0][0].text
    assert "权限不足" in notice and "1/1" in notice
    assert "体积过大" not in notice
    assert not sender.pending_group_file_paths(meta)


def test_mixed_results_report_only_confirmed_failures_as_failed():
    event = make_event(None)
    asyncio.run(
        MessageSender._finish_best_effort_delivery(
            event,
            label="解析结果",
            expected=3,
            succeeded=1,
            errors=[
                GroupFileUploadError("权限不足"),
                GroupFileUploadUnconfirmed("回执超时"),
            ],
        )
    )
    notice = event.send.await_args.args[0][0].text
    assert "1/3" in notice and "尚未确认" in notice
    assert "2/3" not in notice


@pytest.mark.parametrize("pending", [True, False])
def test_plugin_cleanup_retains_only_uncertain_delivery(tmp_path, monkeypatch, pending):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root.parent))
    module = importlib.import_module(f"{root.name}.main")
    plugin_class = module.MediaParserNovaPlugin
    plugin = plugin_class.__new__(plugin_class)
    plugin.message_sender = MessageSender()
    plugin.config_manager = SimpleNamespace(
        download=SimpleNamespace(group_file_timeout_seconds=600),
        admin=SimpleNamespace(debug_mode=False),
    )
    plugin._cleanup_tasks = set()
    plugin._cache_cleanup_lock = asyncio.Lock()
    cache = tmp_path / "media"
    cache.mkdir()
    path = cache / "video.mp4"
    path.write_bytes(b"video")
    storage = importlib.import_module(f"{root.name}.nova_core.storage.cache_marker")
    storage.stamp_subdir(str(cache))
    meta = [{"group_files": [{"path": str(path), "pending": pending}]}]

    async def run():
        await plugin._cleanup_delivery_files([str(path)], meta)
        assert path.exists() is pending
        if pending:
            assert (cache / storage.EXPIRY_FILE_NAME).exists()
            assert len(plugin._cleanup_tasks) == 1
            await plugin._shutdown_delayed_cleanups()
            # Reload/stop cancels timers; persisted expiry still protects the file.
            assert path.exists()

    asyncio.run(run())
