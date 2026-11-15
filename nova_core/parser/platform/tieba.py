"""Baidu Tieba threads through the official HTTPS client JSON interface.

The public client form signature and endpoint were checked against live Baidu
responses. JSON rich-content type 3 carries signed originals; type 5.link and
thread.video_info carry signed video URLs. No page scraping or protobuf needed.
"""

import asyncio
import hashlib
import html
import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp

from ...constants import Config
from ...types import MediaMetadata
from ..utils import trim_url_tail
from .base import BaseVideoParser

TIEBA_ORIGIN = "https://tieba.baidu.com"
TIEBA_API = "https://tiebac.baidu.com/c/f/pb/page"
TIEBA_PAGE_HOSTS = frozenset({"tieba.baidu.com", "www.tieba.baidu.com"})
TIEBA_IMAGE_HOSTS = frozenset(
    {
        "tiebapic.baidu.com",
        "imgsa.baidu.com",
        "imgsrc.baidu.com",
        "hiphotos.baidu.com",
        "tb.himg.baidu.com",
        "himg.bdimg.com",
        "tb-video.bdstatic.com",
    }
)
TIEBA_VIDEO_HOSTS = frozenset({"tb-video.bdstatic.com"})
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_CONTENT_ITEMS = 2000
MAX_BODY_CHARS = 50000
_THREAD_ID = r"[1-9][0-9]{0,19}"


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        number = int(value)
        return number if 0 <= number <= (1 << 63) - 1 else None
    except (ValueError, OverflowError):
        return None


def _text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    # JSON text is already plain text; preserve literal angle brackets/code.
    value = html.unescape(value).replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)


def _timestamp(value: Any) -> str:
    number = _number(value)
    if not number:
        return ""
    try:
        return datetime.fromtimestamp(number, UTC).isoformat()
    except (ValueError, OverflowError, OSError):
        return ""


class TiebaParser(BaseVideoParser):
    """Read a thread's first floor and optional first-page floor replies."""

    def __init__(
        self,
        use_proxy: bool = False,
        proxy_url: str | None = None,
        cookie: str = "",
        include_media: bool = True,
        hot_comment_count: int = 0,
    ) -> None:
        super().__init__("tieba")
        self.proxy_url = proxy_url if use_proxy else None
        self.cookie = str(cookie or "").strip()
        if "\r" in self.cookie or "\n" in self.cookie:
            self.cookie = ""
        self.include_media = bool(include_media)
        self.hot_comment_count = min(50, _number(hot_comment_count) or 0)
        self.semaphore = asyncio.Semaphore(Config.PARSER_MAX_CONCURRENT)
        self.headers = {"User-Agent": "Mozilla/5.0", "Referer": TIEBA_ORIGIN + "/"}

    @staticmethod
    def _thread_id(url: str) -> str | None:
        if not isinstance(url, str) or re.search(r"[\s\x00-\x1f\x7f]", url):
            return None
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or (parsed.hostname or "").lower() not in TIEBA_PAGE_HOSTS
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port
                not in {None, 80 if parsed.scheme.lower() == "http" else 443}
            ):
                return None
            if match := re.fullmatch(rf"/p/({_THREAD_ID})/?", parsed.path):
                return match.group(1)
            if parsed.path.rstrip("/") == "/mo/q/m":
                keys = ("kz", "tid")
            elif parsed.path.rstrip("/") == "/mo/q/movideo/page":
                keys = ("thread_id",)
            else:
                return None
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=100)
            if keys == ("kz", "tid") and "z" in query:
                # Old WAP uses z for a thread only under its post-list action.
                if query.get("tn") != ["bdPList"]:
                    return None
                keys += ("z",)
            ids = [value for key in keys for value in query.get(key, [])]
            if len(ids) == 1 and re.fullmatch(_THREAD_ID, ids[0]):
                return ids[0]
        except (TypeError, ValueError):
            pass
        return None

    def can_parse(self, url: str) -> bool:
        return self._thread_id(url) is not None

    def extract_links(self, text: str) -> list[str]:
        links: list[str] = []
        seen: set[str] = set()
        for match in re.finditer(
            r"https?://[^\s<>\"'\x80-\U0010ffff]+",
            str(text or ""),
            re.IGNORECASE | re.ASCII,
        ):
            url = trim_url_tail(match.group(0))
            thread_id = self._thread_id(url)
            if thread_id and thread_id not in seen:
                seen.add(thread_id)
                links.append(url)
        return links

    @staticmethod
    def _media_url(value: Any, *, video: bool = False) -> str:
        if not isinstance(value, str):
            return ""
        value = html.unescape(value.strip())
        if value.startswith("//"):
            value = "https:" + value
        if re.search(r"[\s\x00-\x1f\x7f]", value):
            return ""
        try:
            parsed = urlsplit(value)
            hosts = TIEBA_VIDEO_HOSTS if video else TIEBA_IMAGE_HOSTS
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or (parsed.hostname or "").lower() not in hosts
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port
                not in {None, 80 if parsed.scheme.lower() == "http" else 443}
                or not parsed.path
            ):
                return ""
            host = parsed.hostname.lower()
            if host == "tb.himg.baidu.com":
                # Its legacy hostname has a mismatched TLS certificate. The
                # official alias serves the same portrait path with valid TLS.
                if not parsed.path.startswith("/sys/portrait/item/"):
                    return ""
                host = "himg.bdimg.com"
            # Keep signed image/video queries intact when selecting HTTPS.
            return parsed._replace(scheme="https", netloc=host, fragment="").geturl()
        except ValueError:
            return ""

    @classmethod
    def _avatar(cls, user: dict[str, Any]) -> str:
        image = _object(
            _object(_object(user.get("user_show_info")).get("feed_head")).get(
                "image_data"
            )
        )
        for value in (
            image.get("img_url"),
            user.get("avatar_url"),
            user.get("portrait"),
        ):
            if found := cls._media_url(value):
                return found
        portrait = user.get("portrait")
        if isinstance(portrait, str) and re.fullmatch(
            r"[A-Za-z0-9._-]{1,256}(?:\?t=[0-9]{1,20})?", portrait
        ):
            return "https://himg.bdimg.com/sys/portrait/item/" + portrait
        return ""

    @staticmethod
    def _is_ad(post: dict[str, Any]) -> bool:
        return (
            any(
                post.get(key) in (True, 1, "1")
                for key in ("is_ad", "is_ad_post", "is_ad_thread")
            )
            or post.get("post_type") == "ad"
            or post.get("type") == "ad"
            or bool(_object(post.get("ad_info")))
        )

    @classmethod
    def _content(
        cls, value: Any
    ) -> tuple[str, list[list[str]], list[dict[str, Any]], int]:
        parts: list[str] = []
        images: list[list[str]] = []
        videos: list[dict[str, Any]] = []
        image_count = 0
        seen_images: set[str] = set()
        if not isinstance(value, list):
            return "", images, videos, image_count
        for item in value[:MAX_CONTENT_ITEMS]:
            if not isinstance(item, dict):
                continue
            kind = _number(item.get("type"))
            if kind in {0, 1, 4, 9, 18}:
                parts.append(_text(item.get("text")))
            elif kind == 2:
                label = _text(item.get("c")).strip()
                parts.append(f"[{label or '表情'}]")
            elif kind == 3:
                image_count += 1
                candidates = list(
                    dict.fromkeys(
                        url
                        for key in (
                            "origin_src",
                            "big_cdn_src",
                            "cdn_src_active",
                            "cdn_src",
                            "src",
                        )
                        if (url := cls._media_url(item.get(key)))
                    )
                )
                if candidates and candidates[0] not in seen_images:
                    seen_images.add(candidates[0])
                    images.append(candidates)
            elif kind == 5:
                videos.append(
                    {
                        "video_url": item.get("link") or item.get("video_url"),
                        "thumbnail_url": item.get("src"),
                        "video_duration": item.get("during_time"),
                    }
                )
        body = "".join(parts).strip()[:MAX_BODY_CHARS]
        return body, images, videos, image_count

    @staticmethod
    def _api_error(response: dict[str, Any]) -> str:
        code = _number(response.get("error_code"))
        message = _text(response.get("error_msg")).strip()[:200]
        if code == 350008 or re.search(
            r"删除|不存在|not.?found|deleted", message, re.IGNORECASE
        ):
            return "贴吧帖子不存在或已被删除"
        if re.search(r"验证|验证码|安全|风控|captcha|verify", message, re.IGNORECASE):
            return "贴吧触发安全验证，请稍后重试或检查 Cookie/代理"
        if re.search(r"登录|登陆|login|log.?in", message, re.IGNORECASE):
            return "贴吧内容需要登录，请配置贴吧 Cookie 后重试"
        if re.search(r"权限|私密|受限|permission|private", message, re.IGNORECASE):
            return "贴吧内容访问受限：" + (message or "当前账号无访问权限")
        return f"贴吧接口请求失败（错误码 {code if code is not None else '未知'}）" + (
            f"：{message}" if message else ""
        )

    async def _fetch_json(
        self, session: aiohttp.ClientSession, thread_id: str
    ) -> dict[str, Any]:
        params = {
            "_client_type": "2",
            "_client_version": "12.64.1.1",
            "kz": thread_id,
            "pn": "1",
            "rn": str(self.hot_comment_count + 1),
            "lz": "0",
            "with_floor": "0",
        }
        signature = "".join(key + "=" + value for key, value in sorted(params.items()))
        params["sign"] = (
            hashlib.md5(
                (signature + "tiebaclient!!!").encode("utf-8"), usedforsecurity=False
            )
            .hexdigest()
            .upper()
        )
        headers = dict(self.headers)
        if self.cookie:
            headers["Cookie"] = self.cookie
        async with session.post(
            TIEBA_API,
            data=params,
            headers=headers,
            proxy=self.proxy_url,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=Config.DEFAULT_TIMEOUT),
        ) as response:
            if response.status in {301, 302, 303, 307, 308}:
                raise ValueError("贴吧接口返回意外重定向，可能触发登录或安全验证")
            if response.status == 429:
                raise ValueError("贴吧请求过于频繁，请稍后重试")
            if response.status not in {200, 400, 401, 403}:
                response.raise_for_status()
            size = 0
            chunks = []
            async for chunk in response.content.iter_chunked(64 * 1024):
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ValueError("贴吧接口响应超过大小限制")
                chunks.append(chunk)
            raw = b"".join(chunks)
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeDecodeError) as exc:
                text = raw.decode("utf-8", errors="replace")[:2000]
                if re.search(r"安全验证|验证码|captcha|verify", text, re.IGNORECASE):
                    raise ValueError(
                        "贴吧触发安全验证，请稍后重试或检查 Cookie/代理"
                    ) from exc
                if response.status == 401:
                    raise ValueError(
                        "贴吧内容需要登录，请配置贴吧 Cookie 后重试"
                    ) from exc
                raise ValueError("贴吧未返回有效 JSON，可能受访问限制") from exc
            if not isinstance(data, dict):
                raise TypeError("贴吧接口响应格式无效")
            if _number(data.get("error_code")) != 0:
                raise ValueError(self._api_error(data))
            if response.status != 200:
                raise ValueError(
                    "贴吧内容需要登录"
                    if response.status == 401
                    else "贴吧接口访问被拒绝"
                )
            return data

    @classmethod
    def _user(
        cls, post: dict[str, Any], users: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        return _object(post.get("author")) or users.get(str(post.get("author_id")), {})

    @classmethod
    def _replies(
        cls, posts: list[Any], users: dict[str, dict[str, Any]], count: int
    ) -> list[dict[str, Any]]:
        if count <= 0:
            return []
        replies = []
        seen: set[tuple[Any, ...]] = set()
        for post in sorted(
            (item for item in posts[:500] if isinstance(item, dict)),
            key=lambda item: _number(item.get("floor")) or 0,
        ):
            floor = _number(post.get("floor"))
            if (
                not floor
                or floor <= 1
                or cls._is_ad(post)
                or post.get("is_deleted") in (True, 1, "1")
            ):
                continue
            message, _, videos, image_count = cls._content(post.get("content"))
            summaries = []
            if image_count:
                summaries.append(f"【图片回复：{image_count}张】")
            if videos:
                summaries.append("【视频回复】")
            message = "\n".join([text for text in [message, *summaries] if text])[:2000]
            if not message:
                continue
            comment_id = str(post.get("id") or "")
            key = (
                (comment_id,)
                if comment_id
                else (floor, str(post.get("author_id")), message)
            )
            if key in seen:
                continue
            seen.add(key)
            user = cls._user(post, users)
            username = (
                _text(user.get("name_show") or user.get("name")).strip() or "贴吧用户"
            )
            uid = str(user.get("id") or post.get("author_id") or "")
            replies.append(
                {
                    "username": username,
                    "author": username,
                    "uid": uid,
                    "user_id": uid,
                    "avatar_url": cls._avatar(user),
                    "message": message,
                    "likes": _number(_object(post.get("agree")).get("agree_num")),
                    "time": _timestamp(post.get("time")),
                    "position_text": f"第 {floor} 楼",
                    "floor": floor,
                    "comment_kind": "floor",
                    "comment_id": comment_id,
                }
            )
            if len(replies) >= count:
                break
        return replies

    async def parse(
        self, session: aiohttp.ClientSession, url: str
    ) -> MediaMetadata | None:
        thread_id = self._thread_id(url)
        if not thread_id:
            raise ValueError("不是支持的贴吧帖子链接")
        async with self.semaphore:
            response = await self._fetch_json(session, thread_id)
            # Also validate mocked/cached responses before considering content.
            if (
                not isinstance(response, dict)
                or _number(response.get("error_code")) != 0
            ):
                raise ValueError(self._api_error(_object(response)))
            thread = _object(response.get("thread"))
            if str(thread.get("id") or "") != thread_id:
                raise ValueError("贴吧接口未返回请求的帖子")
            posts = response.get("post_list")
            posts = posts if isinstance(posts, list) else []
            mains = [
                item
                for item in posts[:500]
                if isinstance(item, dict) and _number(item.get("floor")) == 1
            ]
            post_id = str(thread.get("post_id") or "")
            main = next(
                (
                    item
                    for item in mains
                    if not post_id or str(item.get("id")) == post_id
                ),
                {},
            )
            if not main:
                raise ValueError("贴吧未返回主楼，帖子可能已删除或访问受限")
            if self._is_ad(main) or self._is_ad(thread):
                raise ValueError("贴吧返回了广告内容，未作为帖子解析")
            if main.get("is_deleted") in (True, 1, "1") or thread.get("is_deleted") in (
                True,
                1,
                "1",
            ):
                raise ValueError("贴吧帖子不存在或已被删除")
            users_list = response.get("user_list")
            users = (
                {
                    str(user.get("id")): user
                    for user in users_list[:500]
                    if isinstance(user, dict) and user.get("id") is not None
                }
                if isinstance(users_list, list)
                else {}
            )
            user = self._user(main, users) or _object(thread.get("author"))
            body, images, video_items, _ = self._content(main.get("content"))
            thread_video = _object(thread.get("video_info"))
            if thread_video:
                video_items.insert(0, thread_video)
            video_urls: list[list[str]] = []
            video_covers: list[list[str]] = []
            seen_videos: set[str] = set()
            duration = None
            for item in video_items:
                candidates = [item.get("video_url")]
                descriptions = item.get("video_desc")
                if isinstance(descriptions, list):
                    candidates.extend(
                        _object(desc).get("video_url") for desc in descriptions[:20]
                    )
                candidates = list(
                    dict.fromkeys(
                        value
                        for candidate in candidates
                        if (value := self._media_url(candidate, video=True))
                    )
                )
                if not candidates or candidates[0] in seen_videos:
                    continue
                seen_videos.add(candidates[0])
                video_urls.append(candidates)
                covers = list(
                    dict.fromkeys(
                        value
                        for key in (
                            "thumbnail_url",
                            "first_frame_thumbnail",
                            "small_thumbnail_url",
                        )
                        if (value := self._media_url(item.get(key)))
                    )
                )
                video_covers.append(covers)
                duration = duration or _number(item.get("video_duration"))
            if not (body or images or video_urls):
                raise ValueError(
                    "贴吧主楼未返回可访问的正文或媒体，可能需要登录或安全验证"
                )
            forum = _object(response.get("forum"))
            result: MediaMetadata = {
                "url": f"{TIEBA_ORIGIN}/p/{thread_id}",
                "platform": "tieba",
                "title": _text(thread.get("title") or main.get("title")).strip(),
                "author": _text(user.get("name_show") or user.get("name")).strip(),
                "avatar_url": self._avatar(user),
                "desc": body,
                "timestamp": _timestamp(main.get("time") or thread.get("create_time")),
                "image_urls": images if self.include_media else [],
                "video_urls": video_urls if self.include_media else [],
                "video_cover_urls": video_covers if self.include_media else [],
                "image_headers": dict(self.headers),
                "video_headers": dict(self.headers),
                "forum_name": _text(forum.get("name")).strip(),
                "forum_id": str(forum.get("id") or ""),
            }
            if duration is not None:
                result["timelength_ms"] = duration * 1000
            if images:
                result["cover_url"] = images[0][0]
            elif video_covers and video_covers[0]:
                result["cover_url"] = video_covers[0][0]
            stats = {}
            for key, value in (
                ("likes", _object(thread.get("agree")).get("agree_num")),
                ("comments", thread.get("reply_num")),
            ):
                if (number := _number(value)) is not None:
                    stats[key] = number
            if stats:
                result["stats"] = stats
                result["stats_line"] = " ".join(
                    f"{label} {stats[key]}"
                    for key, label in (("likes", "👍"), ("comments", "💬"))
                    if key in stats
                )
            if self.proxy_url:
                result.update(
                    use_image_proxy=True, use_video_proxy=True, proxy_url=self.proxy_url
                )
            if self.hot_comment_count:
                try:
                    replies = self._replies(posts, users, self.hot_comment_count)
                    if replies:
                        result.update(
                            hot_comments=replies, comment_section_title="楼层回复"
                        )
                except asyncio.CancelledError:
                    raise
                except (
                    TypeError,
                    ValueError,
                    KeyError,
                    AttributeError,
                    IndexError,
                    RuntimeError,
                ) as exc:
                    self.logger.debug(
                        f"贴吧回复提取失败，保留主楼（{type(exc).__name__}）"
                    )
            return result
