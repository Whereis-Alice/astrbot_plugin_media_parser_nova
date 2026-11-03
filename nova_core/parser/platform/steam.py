"""Steam 商店游戏信息、截图与宣传片解析。"""

import asyncio
import html
import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import aiohttp

from ...constants import Config
from ...logger import logger
from ...types import MediaMetadata
from ..utils import trim_url_tail
from .base import BaseVideoParser

STEAM_APP_HOSTS = frozenset(
    {"store.steampowered.com", "steamcommunity.com", "www.steamcommunity.com", "s.team"}
)
STEAM_API = "https://store.steampowered.com/api/appdetails"
STEAM_REVIEWS_API = "https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
REVIEW_TIMEOUT_SECONDS = 12
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)


def _safe_http_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    candidate = html.unescape(value.strip())
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    try:
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        if parsed.username is not None or parsed.password is not None:
            return ""
        if parsed.port not in {None, 80 if parsed.scheme == "http" else 443}:
            return ""
    except (TypeError, ValueError):
        return ""
    return candidate


def _bounded_count(value: Any, default: int, maximum: int) -> int:
    try:
        return min(maximum, max(0, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = re.sub(r"<(?:br\s*/?|/p|/div|/li)\s*>", "\n", value, flags=re.IGNORECASE)
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


class SteamParser(BaseVideoParser):
    """通过 Steam 官方商店接口解析游戏，兼容当前 HLS 和旧 MP4 宣传片。"""

    def __init__(
        self,
        use_proxy: bool = False,
        proxy_url: str | None = None,
        language: str = "schinese",
        country_code: str = "CN",
        max_screenshots: int = 6,
        max_trailers: int = 1,
        hot_comment_count: int = 0,
    ) -> None:
        super().__init__("steam")
        self.use_proxy = bool(use_proxy)
        self.proxy_url = proxy_url
        self.language = (
            language if re.fullmatch(r"[a-z]{2,24}", str(language)) else "schinese"
        )
        self.country_code = (
            country_code.upper()
            if re.fullmatch(r"[A-Za-z]{2}", str(country_code))
            else "CN"
        )
        self.max_screenshots = _bounded_count(max_screenshots, 6, 20)
        self.max_trailers = _bounded_count(max_trailers, 1, 5)
        self.hot_comment_count = _bounded_count(hot_comment_count, 0, 100)
        self.semaphore = asyncio.Semaphore(Config.PARSER_MAX_CONCURRENT)

    @staticmethod
    def _extract_app_id(url: str) -> str:
        candidate = _safe_http_url(url)
        if not candidate:
            return ""
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
        if host not in STEAM_APP_HOSTS:
            return ""
        pattern = (
            r"/a/(\d{1,12})/?"
            if host == "s.team"
            else r"/(?:agecheck/)?app/(\d{1,12})(?:/[^/]*)?/?"
        )
        match = re.fullmatch(pattern, parsed.path)
        if not match or int(match.group(1)) <= 0:
            return ""
        return str(int(match.group(1)))

    def can_parse(self, url: str) -> bool:
        return bool(self._extract_app_id(url))

    def extract_links(self, text: str) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for match in re.finditer(r"https?://[^\s<>\"']+", text or "", re.IGNORECASE):
            link = trim_url_tail(match.group(0))
            app_id = self._extract_app_id(link)
            if app_id and app_id not in seen:
                seen.add(app_id)
                result.append(link)
        return result

    def _get_proxy(self) -> str | None:
        return self.proxy_url if self.use_proxy and self.proxy_url else None

    @staticmethod
    def _media_url(value: Any) -> str:
        candidate = _safe_http_url(value)
        if not candidate:
            return ""
        host = (urlparse(candidate).hostname or "").lower()
        if any(
            host == domain or host.endswith("." + domain)
            for domain in (
                "steamstatic.com",
                "steamcontent.com",
                "akamaihd.net",
                "steampowered.com",
            )
        ):
            return candidate
        return ""

    async def _fetch_app(
        self, session: aiohttp.ClientSession, app_id: str
    ) -> dict[str, Any]:
        async with session.get(
            STEAM_API,
            params={"appids": app_id, "l": self.language, "cc": self.country_code},
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            proxy=self._get_proxy(),
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=Config.DEFAULT_TIMEOUT),
        ) as response:
            if response.status == 429:
                raise RuntimeError("Steam 请求过于频繁，请稍后重试")
            if response.status != 200:
                raise RuntimeError(f"Steam 商店接口请求失败（HTTP {response.status}）")
            chunks = []
            received = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                received += len(chunk)
                if received > MAX_RESPONSE_BYTES:
                    raise RuntimeError("Steam 商店响应超过大小限制")
                chunks.append(chunk)
            try:
                payload = json.loads(b"".join(chunks))
            except (ValueError, UnicodeDecodeError) as exc:
                raise RuntimeError("Steam 商店返回了无效数据") from exc
        entry = payload.get(app_id) if isinstance(payload, dict) else None
        if not isinstance(entry, dict) or not entry.get("success"):
            raise RuntimeError("Steam 游戏不存在、已下架或在当前商店地区不可用")
        app = entry.get("data")
        if not isinstance(app, dict) or not isinstance(app.get("name"), str):
            raise RuntimeError("Steam 商店返回的游戏信息不完整")  # noqa: TRY004 - invalid remote response, not a caller type error
        return app

    def _build_metadata(self, app_id: str, app: dict[str, Any]) -> MediaMetadata:
        canonical_url = f"https://store.steampowered.com/app/{app_id}/"
        headers = {"User-Agent": USER_AGENT, "Referer": canonical_url}
        developer_values = app.get("developers")
        developers = (
            [v for v in developer_values if isinstance(v, str)]
            if isinstance(developer_values, list)
            else []
        )
        description = _text(
            app.get("short_description") or app.get("detailed_description")
        )
        details: list[str] = []
        price = app.get("price_overview")
        if app.get("is_free"):
            details.append("价格：免费")
        elif isinstance(price, dict) and isinstance(price.get("final_formatted"), str):
            details.append("价格：" + price["final_formatted"])
        release = app.get("release_date")
        timestamp = _text(release.get("date")) if isinstance(release, dict) else ""
        if timestamp:
            details.append(
                ("预计发行：" if release.get("coming_soon") else "发行日期：")
                + timestamp
            )
        genres = app.get("genres")
        if isinstance(genres, list):
            names = [
                _text(item.get("description"))
                for item in genres
                if isinstance(item, dict)
            ]
            if any(names):
                details.append("类型：" + " / ".join(name for name in names if name))
        if details:
            description = "\n\n".join(
                value for value in (description, "\n".join(details)) if value
            )

        images: list[list[str]] = []
        image_seen: set[str] = set()
        cover = self._media_url(app.get("header_image"))
        if cover:
            images.append([cover])
            image_seen.add(cover)
        screenshots = app.get("screenshots")
        screenshot_count = 0
        for item in screenshots if isinstance(screenshots, list) else []:
            if screenshot_count >= self.max_screenshots:
                break
            if not isinstance(item, dict):
                continue
            candidates = [
                self._media_url(item.get(key))
                for key in ("path_full", "path_thumbnail")
            ]
            candidates = list(dict.fromkeys(value for value in candidates if value))
            if candidates and candidates[0] not in image_seen:
                images.append(candidates)
                image_seen.add(candidates[0])
                screenshot_count += 1

        videos: list[list[str]] = []
        covers: list[list[str]] = []
        video_seen: set[str] = set()
        movies = app.get("movies")
        for movie in movies if isinstance(movies, list) else []:
            if len(videos) >= self.max_trailers:
                break
            if not isinstance(movie, dict):
                continue
            sources: list[str] = []
            # Steam now supplies HLS manifests; legacy app entries may still contain MP4.
            mp4 = movie.get("mp4")
            if isinstance(mp4, dict):
                for key in ("max", "480"):
                    if value := self._media_url(mp4.get(key)):
                        sources.append(value)
            if hls := self._media_url(movie.get("hls_h264")):
                sources.append("m3u8:" + hls)
            webm = movie.get("webm")
            if isinstance(webm, dict):
                for key in ("max", "480"):
                    if value := self._media_url(webm.get(key)):
                        sources.append(value)
            sources = list(dict.fromkeys(sources))
            if sources and sources[0] not in video_seen:
                video_seen.add(sources[0])
                videos.append(sources)
                thumbnail = self._media_url(movie.get("thumbnail")) or cover
                covers.append([thumbnail] if thumbnail else [])

        return {
            "url": canonical_url,
            "title": _text(app.get("name")),
            "author": " / ".join(developers),
            "desc": description,
            "timestamp": timestamp,
            "platform": self.name,
            "image_urls": images,
            "video_urls": videos,
            "video_cover_urls": covers,
            "image_headers": headers.copy(),
            "video_headers": headers.copy(),
            "video_force_download": bool(videos),
            "use_image_proxy": bool(self._get_proxy()),
            "use_video_proxy": bool(self._get_proxy()),
            "proxy_url": self.proxy_url or "",
        }

    @staticmethod
    def _normalize_reviews(payload: Any, count: int) -> list[dict[str, Any]]:
        """Keep actual helpful votes; Steam's anonymous API supplies no profile names."""
        if not isinstance(payload, dict) or count <= 0:
            return []
        response = payload.get("response")
        response = response if isinstance(response, dict) else payload
        reviews = response.get("reviews")
        if not isinstance(reviews, list):
            return []
        comments: list[dict[str, Any]] = []
        seen: set[str] = set()
        for review in reviews:
            if not isinstance(review, dict):
                continue
            message = _text(review.get("review"))
            if not message:
                continue
            author = review.get("author")
            author = author if isinstance(author, dict) else {}
            steam_id = str(author.get("steamid") or "").strip()
            comment_id = str(review.get("recommendationid") or "").strip()
            identity = comment_id or f"{steam_id}:{message}"
            if identity in seen:
                continue
            seen.add(identity)
            try:
                timestamp = float(review.get("timestamp_created") or 0)
                time_text = (
                    datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M:%S")
                    if timestamp > 0
                    else ""
                )
            except (TypeError, ValueError, OverflowError, OSError):
                time_text = ""
            recommendation = review.get("voted_up")
            if isinstance(recommendation, bool):
                message = ("推荐" if recommendation else "不推荐") + "\n" + message
            comments.append(
                {
                    "comment_id": comment_id,
                    "username": "Steam 玩家",
                    "uid": steam_id,
                    "avatar_url": "",
                    "message": message,
                    "likes": _bounded_count(review.get("votes_up"), 0, 2**31 - 1),
                    "likes_label": "有帮助",
                    "time": time_text,
                }
            )
        comments.sort(key=lambda item: item["likes"], reverse=True)
        return comments[:count]

    async def _fetch_hot_comments(
        self, session: aiohttp.ClientSession, app_id: str
    ) -> list[dict[str, Any]]:
        if self.hot_comment_count <= 0:
            return []
        # The old /appreviews endpoint now redirects to the store home page.
        # Its official replacement accepts anonymous input_json calls and enum filters.
        parameters = {
            "appid": int(app_id),
            "filter": 0,
            "languages": ["all"],
            "day_range": 0,
            "cursor": "*",
            "review_type": 0,
            "purchase_type": 1,
            "num_per_page": min(100, max(20, self.hot_comment_count)),
            "display_language": self.language,
        }
        async with session.get(
            STEAM_REVIEWS_API,
            params={"input_json": json.dumps(parameters, separators=(",", ":"))},
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            proxy=self._get_proxy(),
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=REVIEW_TIMEOUT_SECONDS),
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"Steam 玩家评测请求失败（HTTP {response.status}）")
            chunks: list[bytes] = []
            received = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                received += len(chunk)
                if received > MAX_RESPONSE_BYTES:
                    raise RuntimeError("Steam 玩家评测响应超过大小限制")
                chunks.append(chunk)
            payload = json.loads(b"".join(chunks))
        return self._normalize_reviews(payload, self.hot_comment_count)

    async def _attach_hot_comments(
        self, session: aiohttp.ClientSession, app_id: str, result: MediaMetadata
    ) -> None:
        if self.hot_comment_count <= 0:
            return
        try:
            comments = await asyncio.wait_for(
                self._fetch_hot_comments(session, app_id),
                timeout=REVIEW_TIMEOUT_SECONDS,
            )
            if comments:
                result["hot_comments"] = comments
                result["comment_section_title"] = "玩家评测"
        except (
            TimeoutError,
            aiohttp.ClientError,
            RuntimeError,
            ValueError,
            TypeError,
            OSError,
        ) as exc:
            # Optional reviews must never hide an otherwise valid game or trailer.
            logger.debug("Steam 玩家评测暂不可用：%s", type(exc).__name__)

    async def parse(self, session: aiohttp.ClientSession, url: str) -> MediaMetadata:
        app_id = self._extract_app_id(url)
        if not app_id:
            raise ValueError("不支持的 Steam 游戏链接")
        async with self.semaphore:
            app = await self._fetch_app(session, app_id)
            result = self._build_metadata(app_id, app)
            await self._attach_hot_comments(session, app_id, result)
        return result
