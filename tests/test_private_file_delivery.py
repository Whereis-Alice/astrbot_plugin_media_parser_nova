import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiocqhttp.exceptions import NetworkError
from astrbot.api.message_components import Plain

from nova_core.config_manager import ConfigManager
from nova_core.downloader.manager import DownloadManager
from nova_core.message_adapter.group_file import GroupFileUploader
from nova_core.message_adapter.node_builder import build_all_nodes
from nova_core.message_adapter.sender import MessageSender


def private_event(
    bot, *, private=True, user_id="456789", group_id="", platform="aiocqhttp"
):
    return SimpleNamespace(
        bot=bot,
        get_group_id=lambda: group_id,
        is_private_chat=lambda: private,
        get_sender_id=lambda: user_id,
        get_platform_name=lambda: platform,
        send=AsyncMock(),
        chain_result=lambda nodes: nodes,
        plain_result=lambda text: [Plain(text)],
    )


@pytest.mark.parametrize("style", ["call_action", "api", "method"])
def test_private_file_uses_current_sender_and_correct_action(tmp_path, style):
    call = AsyncMock(return_value={"file_id": "file"})
    bot = {
        "call_action": SimpleNamespace(call_action=call),
        "api": SimpleNamespace(api=SimpleNamespace(call_action=call)),
        "method": SimpleNamespace(upload_private_file=call),
    }[style]
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    event = private_event(bot)
    assert GroupFileUploader.can_upload(event)
    asyncio.run(GroupFileUploader().upload(event, str(path), "视频.mp4"))
    params = {"user_id": "456789", "file": str(path.resolve()), "name": "视频.mp4"}
    if style == "method":
        call.assert_awaited_once_with(**params)
    else:
        call.assert_awaited_once_with("upload_private_file", **params)


@pytest.mark.parametrize(
    "changes",
    [
        {"private": False},
        {"user_id": ""},
        {"user_id": "invalid"},
        {"platform": "telegram"},
    ],
)
def test_missing_or_non_private_context_is_not_used_as_upload_target(changes):
    bot = SimpleNamespace(call_action=AsyncMock())
    assert not GroupFileUploader.can_upload(private_event(bot, **changes))
    bot.call_action.assert_not_called()


def test_group_context_never_falls_back_to_private_upload(tmp_path):
    bot = SimpleNamespace(call_action=AsyncMock(side_effect=RuntimeError("无权限")))
    event = private_event(bot, private=False, group_id="123456")
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    with pytest.raises(RuntimeError, match="无权限"):
        asyncio.run(GroupFileUploader().upload(event, str(path), path.name))
    bot.call_action.assert_awaited_once_with(
        "upload_group_file",
        group_id=123456,
        file=str(path.resolve()),
        name=path.name,
    )


@pytest.mark.parametrize(
    "value", ["上传为群文件", "上传为文件（群聊/私聊）", "group_file"]
)
def test_old_and_new_config_labels_enable_the_same_policy(value):
    config = ConfigManager({"download": {"oversize_delivery": value}})
    assert config.download.group_file_enabled


@pytest.mark.parametrize("aggregated", [False, True])
def test_oversize_private_video_reaches_file_api_without_forcing_compression(
    tmp_path, aggregated
):
    async def run():
        bot = SimpleNamespace(call_action=AsyncMock(return_value={"file_id": "file"}))
        event = private_event(bot)
        sender = MessageSender()
        manager = DownloadManager(
            max_video_size_mb=1000,
            send_video_max_mb=48,
            cache_dir=str(tmp_path),
            cache_dir_available=True,
            oversize_delivery="group_file",
            transcode_oversize_video=False,
        )
        path = tmp_path / "video.mp4"
        path.write_bytes(b"video")
        manager._download_local_items = AsyncMock(
            return_value=[
                {
                    "kind": "video",
                    "position": 0,
                    "success": True,
                    "file_path": str(path),
                    "size_mb": 436.0,
                }
            ]
        )
        manager._fit_video_to_send_limit = AsyncMock()
        metadata = await manager.process_metadata(
            session=None,
            metadata={
                "url": "https://example.com/video",
                "title": "示例视频",
                "video_urls": [
                    ["dash:https://video.example/v||https://video.example/a"]
                ],
                "image_urls": [],
                "_enable_text_metadata": False,
                "_enable_rich_media": True,
            },
            group_file_available=sender.can_upload_group_file(event),
        )
        built = build_all_nodes([metadata])
        if aggregated:
            await sender.send_aggregated_results(
                event, built.link_metadata, "Nova", "987654"
            )
        else:
            await sender.send_individual_results(
                event, built.all_link_nodes, built.link_metadata
            )
        bot.call_action.assert_awaited_once_with(
            "upload_private_file",
            user_id="456789",
            file=str(path.resolve()),
            name="示例视频.mp4",
        )
        manager._fit_video_to_send_limit.assert_not_awaited()
        event.send.assert_not_awaited()
        assert path.exists()
        assert not sender.pending_group_file_paths(built.link_metadata)

    asyncio.run(run())


@pytest.mark.parametrize("timeout", [False, True])
def test_private_failure_and_timeout_keep_correct_notice_and_link(tmp_path, timeout):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    error = (
        NetworkError("WebSocket API call timeout")
        if timeout
        else RuntimeError("upload_private_file 不支持")
    )
    event = private_event(SimpleNamespace(call_action=AsyncMock(side_effect=error)))
    sender = MessageSender()
    metadata = [
        {
            "source_url": "https://example.com/video",
            "group_files": [{"path": str(path), "name": path.name}],
        }
    ]
    asyncio.run(sender.send_individual_results(event, [[]], metadata))
    notice = event.send.await_args.args[0][0].text
    assert "https://example.com/video" in notice
    assert "群文件" not in notice and "体积过大" not in notice
    if timeout:
        assert "尚未确认" in notice and "未能发出" not in notice
        assert sender.pending_group_file_paths(metadata) == {str(path)}
    else:
        assert "不支持" in notice and "未能发出" in notice
        assert not sender.pending_group_file_paths(metadata)
    assert event.bot.call_action.await_count == 1
