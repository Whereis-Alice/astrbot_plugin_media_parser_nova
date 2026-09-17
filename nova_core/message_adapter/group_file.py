"""OneBot QQ 群文件投递；与普通消息链发送保持隔离。"""

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from astrbot.api.event import AstrMessageEvent

from ..logger import logger

_INVALID_FILE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


class GroupFileUploadError(RuntimeError):
    """群文件投递不可用或执行失败。"""


class GroupFileUploadUnconfirmed(GroupFileUploadError):
    """未收到完成回执，协议端可能仍在上传，不能按失败重试。"""


def _scoped_upload_api(bot: Any, timeout_seconds: float) -> Any:
    """复用 aiocqhttp 连接，仅给本次调用设置超时，不修改共享客户端。"""
    try:
        from aiocqhttp import CQHttp
        from aiocqhttp.api_impl import HttpApi, UnifiedApi, WebSocketReverseApi
    except ImportError:
        return None
    if not isinstance(bot, CQHttp):
        return None
    if getattr(bot.call_action, "__func__", None) is not CQHttp.call_action:
        return None
    api = vars(bot).get("_api")
    if type(api) is not UnifiedApi:
        return None
    transports = {}
    for attr, expected_type in (
        ("_wsr_api", WebSocketReverseApi),
        ("_http_api", HttpApi),
    ):
        transport = vars(api).get(attr)
        if transport is None:
            continue
        # 未识别的扩展实现保留原调用路径，避免绕过第三方包装。
        if type(transport) is not expected_type:
            return None
        state = vars(transport)
        try:
            if expected_type is WebSocketReverseApi:
                transports[attr] = WebSocketReverseApi(
                    state["_api_clients"], state["_event_clients"], timeout_seconds
                )
            else:
                transports[attr] = HttpApi(
                    state["_api_root"], state["_access_token"], timeout_seconds
                )
        except KeyError:
            return None
    return UnifiedApi(
        http_api=transports.get("_http_api"),
        wsr_api=transports.get("_wsr_api"),
    )


def _receipt_is_uncertain(exc: Exception) -> bool:
    if isinstance(exc, asyncio.TimeoutError):
        return True
    try:
        from aiocqhttp.exceptions import NetworkError
    except ImportError:
        pass
    else:
        if isinstance(exc, NetworkError):
            return True
    text = str(exc).lower()
    return any(
        word in text
        for word in (
            "websocket api call timeout",
            "timed out",
            "timeout",
            "超时",
            "connection closed",
            "connection reset",
        )
    )


def build_group_file_name(
    metadata: dict,
    file_path: str,
    *,
    video_index: int = 0,
    video_count: int = 1,
) -> str:
    """生成可读且能被 QQ 接受的文件名，不改变磁盘上的缓存文件。"""
    title = str(metadata.get("title") or "媒体视频").strip()
    title = _INVALID_FILE_NAME.sub("_", title).strip(" ._") or "媒体视频"
    if video_count > 1:
        title += f" [{video_index + 1}]"
    suffix = Path(file_path).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        suffix = ".mp4"
    max_stem_length = max(1, 180 - len(suffix))
    return title[:max_stem_length].rstrip(" .") + suffix


class GroupFileUploader:
    """通过 AstrBot 的 aiocqhttp 事件调用标准 OneBot v11 action。"""

    def __init__(self, timeout_seconds: int = 600):
        self.timeout_seconds = max(30, min(3600, int(timeout_seconds or 600)))

    @staticmethod
    def can_upload(event: AstrMessageEvent) -> bool:
        try:
            group_id = str(event.get_group_id() or "").strip()
        except Exception:
            return False
        if not group_id:
            return False

        get_platform_name = getattr(event, "get_platform_name", None)
        if callable(get_platform_name):
            try:
                platform_name = str(get_platform_name() or "").strip().lower()
            except Exception:
                return False
            if platform_name and platform_name != "aiocqhttp":
                return False

        bot = getattr(event, "bot", None)
        if bot is None:
            return False
        api = getattr(bot, "api", None)
        return bool(
            callable(getattr(bot, "call_action", None))
            or callable(getattr(api, "call_action", None))
            or callable(getattr(bot, "upload_group_file", None))
        )

    async def _call_action(self, event: AstrMessageEvent, payload: dict) -> Any:
        bot = getattr(event, "bot", None)
        api = _scoped_upload_api(bot, self.timeout_seconds)
        if api is not None:
            get_self_id = getattr(event, "get_self_id", None)
            self_id = str(get_self_id() or "") if callable(get_self_id) else ""
            if self_id:
                payload = {**payload, "self_id": self_id}
            return await api.call_action("upload_group_file", **payload)
        call_action = getattr(bot, "call_action", None)
        if callable(call_action):
            return await call_action("upload_group_file", **payload)

        api = getattr(bot, "api", None)
        api_call_action = getattr(api, "call_action", None)
        if callable(api_call_action):
            return await api_call_action("upload_group_file", **payload)

        upload = getattr(bot, "upload_group_file", None)
        if callable(upload):
            return await upload(**payload)
        raise GroupFileUploadError("当前 OneBot 客户端未暴露 upload_group_file")

    async def upload(
        self,
        event: AstrMessageEvent,
        file_path: str,
        file_name: str,
    ) -> Any:
        if not self.can_upload(event):
            raise GroupFileUploadError("当前会话不是支持群文件上传的 QQ 群聊")
        path = os.path.abspath(str(file_path or ""))
        if not os.path.isfile(path):
            raise GroupFileUploadError("待上传的视频文件不存在")

        raw_group_id = str(event.get_group_id() or "").strip()
        group_id: Any = int(raw_group_id) if raw_group_id.isdigit() else raw_group_id
        payload = {
            "group_id": group_id,
            "file": path,
            "name": str(file_name or Path(path).name),
        }
        logger.info(
            f"开始上传群文件: {payload['name']}，回执等待上限 {self.timeout_seconds} 秒"
        )
        try:
            result = await asyncio.wait_for(
                self._call_action(event, payload),
                timeout=self.timeout_seconds,
            )
            # 某些适配器直接返回 OneBot 响应包装，不会抛 ActionFailed。
            if isinstance(result, dict):
                if result.get("status") == "failed":
                    detail = (
                        result.get("wording") or result.get("message") or "接口拒绝"
                    )
                    raise GroupFileUploadError(f"群文件上传失败：{detail}")
                if result.get("status") == "async" or result.get("retcode") == 1:
                    raise GroupFileUploadUnconfirmed(
                        "协议端已受理，尚未确认群文件上传完成"
                    )
            return result
        except asyncio.CancelledError:
            raise
        except GroupFileUploadError:
            raise
        except Exception as exc:
            if _receipt_is_uncertain(exc):
                raise GroupFileUploadUnconfirmed(
                    "群文件上传回执未确认，文件可能仍在上传或已发送，请稍后查看群文件"
                ) from exc
            raise GroupFileUploadError(f"群文件上传失败：{exc}") from exc
