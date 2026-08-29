"""YouTube Cookie 失效提醒管理器。

YouTube 没有对外的扫码登录接口，Cookie 只能由管理员手动重新导出，
所以这里不做任何登录状态机，只负责「带冷却地私聊提醒一次」。
"""

import time
from typing import Any, Optional

from astrbot.api.event import AstrMessageEvent

from ....logger import logger
from ...base import AdminAssistManager

_REASON_TEXTS = {
    "player_login_required": "视频仍被 YouTube 机器人验证挡下（playabilityStatus=LOGIN_REQUIRED）",
    "innertube_logged_out": "Innertube 返回 loggedOut=true，服务端已把当前 Cookie 当成未登录",
    "keepalive_logged_out": (
        "定期 Cookie 保鲜请求被 YouTube 判定为未登录，"
        "说明这份 Cookie 已经无法靠自动跟进轮换救回来"
    ),
}


class YouTubeCookieNoticeManager(AdminAssistManager):
    """检测到 YouTube Cookie 失效时，私聊提醒管理员重新导出。"""

    def __init__(
        self,
        context: Any,
        admin_id: str,
        enabled: bool,
        request_cooldown_minutes: int = 120,
    ):
        """初始化 YouTube Cookie 提醒管理器。"""
        super().__init__(
            context=context,
            admin_id=admin_id,
            enabled=enabled,
            # 本管理器不等待任何回复，超时参数仅为满足基类签名。
            reply_timeout_minutes=1,
            request_cooldown_minutes=request_cooldown_minutes,
        )

    async def handle_admin_reply(
        self, event: AstrMessageEvent, *args: Any, **kwargs: Any
    ) -> bool:
        """本管理器为单向通知，不消费任何管理员回复。"""
        return False

    def trigger_assist_request(self, reason: str) -> None:
        """发起一次 Cookie 失效提醒（带冷却，后台执行不阻塞解析链）。"""
        if not self.enabled:
            return
        self._new_task(self._notify(reason))

    @staticmethod
    def describe_reason(reason: str) -> str:
        """把内部原因码翻译成人类可读文案。"""
        return _REASON_TEXTS.get(reason, reason or "cookie_expired")

    async def _notify(self, reason: str) -> None:
        """执行一次带冷却的私聊提醒。"""
        async with self._lock:
            now = time.monotonic()
            if now - self._last_request_at < self.request_cooldown_seconds:
                return
            origin: Optional[str] = self._admin_private_origin
            if not origin:
                logger.warning(
                    "[youtube] 检测到 Cookie 失效，但没有可用的管理员私聊会话，"
                    "无法发送提醒（请让管理员先私聊机器人一次）"
                )
                return
            previous_request_at = self._last_request_at
            self._last_request_at = now

        cooldown_minutes = int(self.request_cooldown_seconds / 60)
        text = "\n".join(
            [
                "检测到 YouTube Cookie 已失效，视频解析会退化成只发封面。",
                f"原因: {self.describe_reason(reason)}",
                "处理方式（YouTube 无法扫码登录，只能手动更新）:",
                "1. 用无痕窗口登录 YouTube 小号；",
                "2. 在同一标签页打开 youtube.com/robots.txt，用 Cookie 导出扩展导出；",
                "3. 把导出的 Cookie 填进插件配置 youtube.cookie；",
                "4. 直接关掉整个无痕窗口，千万不要点登出（登出会作废这份 Cookie）。",
                "重新填一次之后插件会自动跟进服务端的 Cookie 轮换，正常情况下不需要再定期回来更新。",
                "如果这条提醒反复出现，多半是出口 IP 信誉问题，建议给 proxy.youtube 配住宅代理。",
                f"本提醒 {cooldown_minutes} 分钟内只发一次。",
            ]
        )
        try:
            await self._send_private_text(origin, text)
        except Exception:
            async with self._lock:
                if self._last_request_at == now:
                    self._last_request_at = previous_request_at
            raise
