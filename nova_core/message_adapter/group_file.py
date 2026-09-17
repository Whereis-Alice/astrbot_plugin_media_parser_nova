"""OneBot QQ 群文件投递；与普通消息链发送保持隔离。"""

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from astrbot.api.event import AstrMessageEvent

_INVALID_FILE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


class GroupFileUploadError(RuntimeError):
    """群文件投递不可用或执行失败。"""


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

    @staticmethod
    async def _call_action(event: AstrMessageEvent, payload: dict) -> Any:
        bot = getattr(event, "bot", None)
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
        try:
            return await asyncio.wait_for(
                self._call_action(event, payload),
                timeout=self.timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            raise GroupFileUploadError(
                f"群文件上传超时（超过 {self.timeout_seconds} 秒）"
            ) from exc
        except GroupFileUploadError:
            raise
        except Exception as exc:
            raise GroupFileUploadError(f"群文件上传失败：{exc}") from exc
