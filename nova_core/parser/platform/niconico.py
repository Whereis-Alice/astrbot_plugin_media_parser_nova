"""Niconico watch API and Domand HLS parsing (without third-party gateways)."""

import asyncio
import html
import json
import re
import time
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from ...constants import Config
from ...types import MediaMetadata
from ..utils import trim_url_tail
from .base import BaseVideoParser

NICONICO_ORIGIN = "https://www.nicovideo.jp"
NICONICO_PAGE_HOSTS = frozenset(
    {
        "nicovideo.jp",
        "www.nicovideo.jp",
        "sp.nicovideo.jp",
        "embed.nicovideo.jp",
        "nico.ms",
    }
)
MAX_API_BYTES = 4 * 1024 * 1024
_VIDEO_ID = r"(?:sm|nm|so|ss)?\d+"
_RESTRICTIONS = {
    "DOMESTIC_VIDEO": "此视频仅限日本地区观看",
    "HIGH_RISK_COUNTRY_VIDEO": "此视频受地区限制",
    "CHANNEL_MEMBER_ONLY": "此视频需要频道会员权限",
    "PREMIUM_ONLY": "此视频需要高级会员权限",
    "PPV_VIDEO": "此视频需要购买观看权限",
    "HARMFUL_VIDEO": "此视频需要登录并允许敏感内容",
    "HIDDEN_VIDEO": "此视频尚未公开或已设为私密",
    "ADMINISTRATOR_DELETE_VIDEO": "此视频已被删除",
    "DELETED_CHANNEL_VIDEO": "此视频所属频道已关闭",
    "NOT_FOUND": "此视频不存在或已被删除",
}


def _object(value: Any) -> dict[str, Any]:
    """Optional API objects can be null or absent; ignore malformed values."""
    return value if isinstance(value, dict) else {}


class NiconicoParser(BaseVideoParser):
    """Parse normal, mobile, embedded, short, and nico.ms video links."""

    def __init__(
        self,
        use_proxy: bool = False,
        proxy_url: str | None = None,
        cookie: str = "",
        include_media: bool = True,
    ) -> None:
        super().__init__("niconico")
        self.proxy_url = proxy_url if use_proxy else None
        self.cookie = str(cookie or "").strip()
        self.include_media = bool(include_media)
        self.semaphore = asyncio.Semaphore(Config.PARSER_MAX_CONCURRENT)
        self.headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": NICONICO_ORIGIN + "/",
            "X-Frontend-Id": "6",
            "X-Frontend-Version": "0",
            # The watch endpoint's content negotiation rejects a JSON-only Accept.
            "Accept": "*/*",
        }

    @staticmethod
    def _video_id(url: str) -> str | None:
        try:
            parsed = urlsplit(str(url))
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or (parsed.hostname or "").lower() not in NICONICO_PAGE_HOSTS
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port
                not in {None, 80 if parsed.scheme.lower() == "http" else 443}
            ):
                return None
            pattern = (
                rf"/({_VIDEO_ID})/?"
                if parsed.hostname.lower() == "nico.ms"
                else rf"/(?:watch|shorts)/({_VIDEO_ID})/?"
            )
            match = re.fullmatch(pattern, parsed.path, re.IGNORECASE)
            return match.group(1).lower() if match else None
        except (TypeError, ValueError):
            return None

    def can_parse(self, url: str) -> bool:
        return self._video_id(url) is not None

    def extract_links(self, text: str) -> list[str]:
        links = []
        seen = set()
        for match in re.finditer(
            r"https?://[^\s<>\"'\x80-\U0010ffff]+",
            str(text or ""),
            re.IGNORECASE | re.ASCII,
        ):
            url = trim_url_tail(match.group(0))
            video_id = self._video_id(url)
            if video_id and video_id not in seen:
                seen.add(video_id)
                links.append(url)
        return links

    @staticmethod
    def _media_url(value: Any, *, domand_only: bool = False) -> str:
        """Reject credentials, non-web URLs and foreign access-right endpoints."""
        if not isinstance(value, str):
            return ""
        value = value.strip()
        if value.startswith("//"):
            value = "https:" + value
        try:
            parsed = urlsplit(value)
            host = (parsed.hostname or "").lower()
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not host
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port
                not in {None, 80 if parsed.scheme.lower() == "http" else 443}
            ):
                return ""
            if domand_only and (
                parsed.scheme.lower() != "https"
                or not (
                    host == "domand.nicovideo.jp"
                    or host.endswith(".domand.nicovideo.jp")
                )
            ):
                return ""
            return value
        except ValueError:
            return ""

    async def _request_json(
        self,
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        async with session.request(
            method,
            url,
            headers=headers,
            params=params,
            json=body,
            proxy=self.proxy_url,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=Config.DEFAULT_TIMEOUT),
        ) as response:
            if response.status in {301, 302, 303, 307, 308}:
                raise ValueError("niconico API 返回了意外重定向，请检查登录或代理设置")
            if response.status == 406:
                raise ValueError(
                    "niconico 拒绝当前请求（HTTP 406）；请检查访问地区或代理设置"
                )
            # The API returns useful reason codes for deleted/private videos.
            if response.status not in {200, 400, 401, 403, 404}:
                response.raise_for_status()
            chunks = []
            total = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > MAX_API_BYTES:
                    raise ValueError("niconico API 响应超过大小限制")
                chunks.append(chunk)
            try:
                result = json.loads(b"".join(chunks))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError(
                    "niconico 返回了非 JSON 响应，请检查地区或登录限制"
                ) from exc
            if not isinstance(result, dict):
                raise ValueError("niconico API 响应格式无效")
            # Only this short-lived CDN cookie may be sent to the video CDN.
            cookie = response.cookies.get("domand_bid")
            cookies = {"domand_bid": cookie.value} if cookie else {}
            return result, cookies

    @staticmethod
    def _restriction_message(response: dict[str, Any]) -> str:
        data = _object(response.get("data"))
        meta = _object(response.get("meta"))
        reason = str(data.get("reasonCode") or meta.get("errorCode") or "")
        return _RESTRICTIONS.get(
            reason, f"视频暂不可访问（{reason or meta.get('status') or '权限限制'}）"
        )

    @classmethod
    def _metadata(cls, video_id: str, data: dict[str, Any]) -> MediaMetadata:
        video = _object(data.get("video"))
        owner = _object(data.get("owner")) or _object(data.get("channel"))
        thumbnail = _object(video.get("thumbnail"))
        cover = ""
        for key in ("ogp", "player", "largeUrl", "middleUrl", "url", "short"):
            if cover := cls._media_url(thumbnail.get(key)):
                break
        description = re.sub(
            r"<br\s*/?>", "\n", str(video.get("description") or ""), flags=re.IGNORECASE
        )
        description = html.unescape(re.sub(r"<[^>]+>", "", description)).strip()
        result: MediaMetadata = {
            "url": f"{NICONICO_ORIGIN}/{'shorts' if video_id.startswith('ss') else 'watch'}/{video_id}",
            "platform": "niconico",
            "title": str(video.get("title") or ""),
            "author": str(owner.get("nickname") or owner.get("name") or ""),
            "avatar_url": cls._media_url(owner.get("iconUrl"))
            or cls._extract_avatar_url(owner),
            "desc": description,
            "timestamp": str(video.get("registeredAt") or ""),
            "video_urls": [],
            "video_cover_urls": [[cover]] if cover else [],
            "image_urls": [],
            "image_headers": {
                "User-Agent": "Mozilla/5.0",
                "Referer": NICONICO_ORIGIN + "/",
            },
            "video_headers": {},
            "video_force_download": True,
        }
        if cover:
            result["cover_url"] = cover
        duration = video.get("duration")
        if isinstance(duration, (int, float)) and duration >= 0:
            result["timelength_ms"] = int(duration * 1000)
        count = _object(video.get("count"))
        result["stats"] = {
            key: count.get(source, 0)
            for key, source in (
                ("views", "view"),
                ("likes", "like"),
                ("comments", "comment"),
            )
        }
        return result

    def _apply_media_proxy(self, result: MediaMetadata) -> None:
        if self.proxy_url:
            result.update(
                use_video_proxy=True, use_image_proxy=True, proxy_url=self.proxy_url
            )

    @staticmethod
    def _available_streams(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [
            item
            for item in value
            if isinstance(item, dict)
            and item.get("isAvailable") is True
            and isinstance(item.get("id"), str)
            and item["id"]
        ]

    async def parse(
        self, session: aiohttp.ClientSession, url: str
    ) -> MediaMetadata | None:
        video_id = self._video_id(url)
        if not video_id:
            raise ValueError("不是支持的 niconico 视频链接")
        async with self.semaphore:
            headers = dict(self.headers)
            if self.cookie:
                headers["Cookie"] = self.cookie
            is_logged_in = bool(re.search(r"(?:^|;\s*)user_session=", self.cookie))
            path = "v3" if is_logged_in else "v3_guest"
            response, _ = await self._request_json(
                session,
                "GET",
                f"{NICONICO_ORIGIN}/api/watch/{path}/{video_id}",
                headers=headers,
                params={"actionTrackId": f"AAAAAAAAAA_{int(time.time() * 1000)}"},
            )
            if not isinstance(response, dict):
                raise ValueError("niconico watch API 响应格式无效")
            data = response.get("data") or {}
            if not isinstance(data, dict) or not isinstance(response.get("meta"), dict):
                raise ValueError("niconico watch API 响应格式无效")
            if response["meta"].get("status") != 200:
                message = self._restriction_message(response)
                if not isinstance(data.get("video"), dict):
                    raise ValueError(message)
                result = self._metadata(video_id, data)
                self._apply_media_proxy(result)
                result.update(
                    access_status="restricted",
                    access_message=message,
                    can_access_full_video=False,
                )
                return result
            if not isinstance(data.get("video"), dict):
                raise ValueError("niconico watch API 未返回有效视频信息")
            result = self._metadata(video_id, data)
            self._apply_media_proxy(result)
            if not self.include_media:
                return result
            domand = _object(_object(data.get("media")).get("domand"))
            videos = self._available_streams(domand.get("videos"))
            audios = self._available_streams(domand.get("audios"))
            access_key = domand.get("accessRightKey")
            track_id = _object(data.get("client")).get("watchTrackId")
            if not (
                videos
                and audios
                and isinstance(access_key, str)
                and access_key
                and isinstance(track_id, str)
                and track_id
            ):
                result.update(
                    access_status="restricted",
                    access_message="未取得视频播放权限；可配置 niconico Cookie 后重试",
                    can_access_full_video=False,
                )
                return result

            # Selecting a single pair keeps the server manifest and media work bounded.
            def quality(item: dict[str, Any]) -> tuple[float, float]:
                def number(value: Any) -> float:
                    try:
                        return float(value or 0)
                    except (TypeError, ValueError):
                        return 0

                return number(item.get("qualityLevel")), number(item.get("bitRate"))

            video = max(videos, key=quality)
            audio = max(audios, key=quality)
            access_headers = {
                **headers,
                "Accept": "application/json;charset=utf-8",
                "Content-Type": "application/json",
                "X-Request-With": NICONICO_ORIGIN,
                "X-Access-Right-Key": str(access_key),
            }
            access, cookies = await self._request_json(
                session,
                "POST",
                f"https://nvapi.nicovideo.jp/v1/watch/{video_id}/access-rights/hls",
                headers=access_headers,
                params={"actionTrackId": str(track_id)},
                body={"outputs": [[video["id"], audio["id"]]]},
            )
            if not isinstance(access, dict):
                raise ValueError("niconico 视频授权 API 响应格式无效")
            access_status = _object(access.get("meta")).get("status")
            if not isinstance(access_status, int) or not 200 <= access_status < 300:
                raise ValueError(self._restriction_message(access))
            content_url = self._media_url(
                _object(access.get("data")).get("contentUrl"), domand_only=True
            )
            if not content_url:
                raise ValueError("niconico 返回了无效的视频 CDN 地址")
            result["video_urls"] = [["m3u8:" + content_url]]
            result["video_headers"] = {
                "User-Agent": self.headers["User-Agent"],
                "Referer": NICONICO_ORIGIN + "/",
                "Origin": NICONICO_ORIGIN,
            }
            if cookies.get("domand_bid"):
                result["video_headers"]["Cookie"] = (
                    "domand_bid=" + cookies["domand_bid"]
                )
            result.update(access_status="available", can_access_full_video=True)
            return result
