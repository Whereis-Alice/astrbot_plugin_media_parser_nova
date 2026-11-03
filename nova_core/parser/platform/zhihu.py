"""知乎公开问题、回答、专栏文章和视频解析。"""

import asyncio
import html
import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

from ...constants import Config
from ...logger import logger
from ...types import MediaMetadata
from ..utils import trim_url_tail
from .base import BaseVideoParser

ZH_HOSTS = frozenset(
    {"zhihu.com", "www.zhihu.com", "m.zhihu.com", "zhuanlan.zhihu.com"}
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
ACCESS_ERROR = (
    "知乎访问受限（登录、验证或内容权限），可在插件设置中配置知乎 Cookie 后重试"
)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_url(value: Any) -> str:
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


def _media_url(value: Any) -> str:
    candidate = _safe_url(value)
    if not candidate:
        return ""
    host = (urlparse(candidate).hostname or "").lower()
    return (
        candidate
        if any(
            host == domain or host.endswith("." + domain)
            for domain in ("zhimg.com", "zhihu.com", "vzuu.com")
        )
        else ""
    )


class _ContentParser(HTMLParser):
    """Read public rich text without script content or tracking GIFs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.images: list[str] = []
        self.video_urls: list[str] = []
        self.video_ids: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag in {"script", "style"}:
            self._hidden += 1
            return
        if self._hidden:
            return
        if tag in {"p", "div", "li", "br", "h1", "h2", "h3", "blockquote"}:
            self.parts.append("\n")
        if tag == "img":
            for key in ("data-original", "data-actualsrc", "data-src", "src"):
                candidate = _media_url(attributes.get(key))
                if candidate and not urlparse(candidate).path.lower().endswith(".svg"):
                    self.images.append(candidate)
                    break
        if tag in {"video", "source"} and (
            candidate := _media_url(attributes.get("src"))
        ):
            self.video_urls.append(candidate)
        if (video_id := attributes.get("data-video-id")) and re.fullmatch(
            r"\d{1,24}", video_id
        ):
            self.video_ids.append(video_id)
        if tag == "a":
            candidate = _safe_url(attributes.get("href"))
            if candidate and urlparse(candidate).hostname in ZH_HOSTS:
                match = re.fullmatch(
                    r"/(?:video|zvideo)/(\d{1,24})/?", urlparse(candidate).path
                )
                if match:
                    self.video_ids.append(match.group(1))

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._hidden = max(0, self._hidden - 1)
        elif not self._hidden and tag in {"p", "div", "li", "blockquote"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.parts.append(data)

    def text(self) -> str:
        lines = [
            re.sub(r"[ \t\r\f\v]+", " ", line).strip()
            for line in "".join(self.parts).split("\n")
        ]
        return "\n".join(line for line in lines if line)


def _plain(value: Any) -> str:
    parser = _ContentParser()
    parser.feed(value if isinstance(value, str) else "")
    return parser.text()


class ZhihuParser(BaseVideoParser):
    """Use official JSON first, then the page's embedded public content."""

    def __init__(
        self,
        cookie: str = "",
        use_proxy: bool = False,
        proxy_url: str | None = None,
        include_media: bool = True,
    ) -> None:
        super().__init__("zhihu")
        self.cookie = str(cookie or "").strip()
        if "\r" in self.cookie or "\n" in self.cookie:
            logger.warning("知乎 Cookie 含换行，已忽略；请填写单行 Cookie")
            self.cookie = ""
        self.use_proxy = bool(use_proxy)
        self.proxy_url = proxy_url
        self.include_media = bool(include_media)
        self.semaphore = asyncio.Semaphore(Config.PARSER_MAX_CONCURRENT)

    @staticmethod
    def _identity(url: str) -> tuple[str, str]:
        candidate = _safe_url(url)
        if not candidate:
            return "", ""
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
        if host not in ZH_HOSTS:
            return "", ""
        path = parsed.path
        if host == "zhuanlan.zhihu.com":
            match = re.fullmatch(r"/p/(\d{1,24})/?", path)
            return ("article", match.group(1)) if match else ("", "")
        match = re.fullmatch(r"/question/\d{1,24}/answer/(\d{1,24})/?", path)
        if match:
            return "answer", match.group(1)
        match = re.fullmatch(r"/(answer|question|zvideo)/(\d{1,24})/?", path)
        if match:
            return match.group(1), match.group(2)
        return "", ""

    def can_parse(self, url: str) -> bool:
        return bool(self._identity(url)[0])

    def extract_links(self, text: str) -> list[str]:
        result: list[str] = []
        seen: set[tuple[str, str]] = set()
        for match in re.finditer(r"https?://[^\s<>\"']+", text or "", re.IGNORECASE):
            link = trim_url_tail(match.group(0))
            identity = self._identity(link)
            if identity[0] and identity not in seen:
                result.append(link)
                seen.add(identity)
        return result

    def _get_proxy(self) -> str | None:
        return self.proxy_url if self.use_proxy and self.proxy_url else None

    def _headers(self, use_cookie: bool = True) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.zhihu.com/",
            "Accept": "application/json,text/html,*/*",
        }
        if use_cookie and self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    @staticmethod
    async def _read_response(response: aiohttp.ClientResponse) -> bytes:
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.content.iter_chunked(64 * 1024):
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise RuntimeError("知乎响应超过大小限制")
            chunks.append(chunk)
        return b"".join(chunks)

    async def _fetch_json(
        self, session: aiohttp.ClientSession, url: str, use_cookie: bool = True
    ) -> dict[str, Any]:
        async with session.get(
            url,
            headers=self._headers(use_cookie),
            proxy=self._get_proxy(),
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=Config.DEFAULT_TIMEOUT),
        ) as response:
            if response.status in {401, 403}:
                raise RuntimeError(ACCESS_ERROR)
            if response.status == 429:
                raise RuntimeError("知乎请求过于频繁，请稍后重试")
            if response.status != 200:
                raise RuntimeError(f"知乎内容请求失败（HTTP {response.status}）")
            try:
                data = json.loads(await self._read_response(response))
            except (ValueError, UnicodeDecodeError) as exc:
                raise RuntimeError("知乎返回了无效 JSON") from exc
        if not isinstance(data, dict) or data.get("error"):
            raise RuntimeError(ACCESS_ERROR)
        return data

    async def _fetch_page(self, session: aiohttp.ClientSession, url: str) -> str:
        current_url = url
        for redirect_count in range(5):
            candidate = _safe_url(current_url)
            parsed = urlparse(candidate)
            if not candidate or parsed.hostname not in ZH_HOSTS:
                raise RuntimeError("知乎页面跳转到了不受信任的地址")
            if parsed.scheme != "https":
                raise RuntimeError("知乎页面跳转到了不安全的地址")
            if parsed.path.startswith(("/signin", "/account", "/captcha")):
                raise RuntimeError(ACCESS_ERROR)
            async with session.get(
                current_url,
                headers=self._headers(),
                proxy=self._get_proxy(),
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=Config.DEFAULT_TIMEOUT),
            ) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    if not response.headers.get("Location"):
                        raise RuntimeError("知乎页面重定向缺少目标地址")
                    current_url = urljoin(current_url, response.headers["Location"])
                    continue
                if response.status in {401, 403}:
                    raise RuntimeError(ACCESS_ERROR)
                if response.status != 200:
                    raise RuntimeError(f"知乎页面请求失败（HTTP {response.status}）")
                raw = await self._read_response(response)
                return raw.decode(response.charset or "utf-8", errors="replace")
        raise RuntimeError("知乎页面重定向次数过多")

    @staticmethod
    def _entity_from_page(page: str, kind: str, content_id: str) -> dict[str, Any]:
        for match in re.finditer(
            r"<script\b([^>]*)>(.*?)</script\s*>", page, re.IGNORECASE | re.DOTALL
        ):
            attributes, raw = match.groups()
            if not re.search(
                r"\bid\s*=\s*['\"]js-initialData['\"]", attributes, re.IGNORECASE
            ):
                continue
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                continue
            initial = _dict(_dict(payload).get("initialState")) or _dict(payload)
            entities = _dict(initial.get("entities"))
            bucket = _dict(
                entities.get(
                    {
                        "answer": "answers",
                        "article": "articles",
                        "question": "questions",
                        "zvideo": "zvideos",
                    }[kind]
                )
            )
            entity = _dict(bucket.get(content_id))
            if entity:
                entity = dict(entity)
                author = entity.get("author")
                if isinstance(author, str):
                    entity["author"] = _dict(_dict(entities.get("users")).get(author))
                question = entity.get("question")
                if isinstance(question, (str, int)):
                    entity["question"] = _dict(
                        _dict(entities.get("questions")).get(str(question))
                    )
                return entity
        return {}

    @staticmethod
    def _jsonld_from_page(page: str, kind: str, content_id: str) -> dict[str, Any]:
        def visit(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [node for item in value for node in visit(item)]
            if isinstance(value, dict):
                return [value] + visit(value.get("@graph", []))
            return []

        for match in re.finditer(
            r"<script\b([^>]*)>(.*?)</script\s*>", page, re.IGNORECASE | re.DOTALL
        ):
            attrs, raw = match.groups()
            if not re.search(
                r"\btype\s*=\s*['\"]application/ld\+json['\"]", attrs, re.IGNORECASE
            ):
                continue
            try:
                nodes = visit(json.loads(raw))
            except (ValueError, TypeError):
                continue
            for node in nodes:
                if node.get("@type") == "QAPage":
                    node = _dict(node.get("mainEntity"))
                node_type = node.get("@type")
                if isinstance(node_type, list):
                    node_type = next(
                        (
                            value
                            for value in node_type
                            if isinstance(value, str)
                            and value
                            in {
                                "Article",
                                "BlogPosting",
                                "NewsArticle",
                                "Question",
                                "VideoObject",
                            }
                        ),
                        "",
                    )
                if not isinstance(node_type, str):
                    continue
                if kind == "question" and node_type == "Question":
                    return {
                        "title": node.get("name"),
                        "detail": node.get("text") or node.get("description"),
                        "created": node.get("dateCreated"),
                        "author": node.get("author"),
                    }
                if kind == "article" and node_type in {
                    "Article",
                    "BlogPosting",
                    "NewsArticle",
                }:
                    return {
                        "title": node.get("headline") or node.get("name"),
                        "content": node.get("articleBody") or node.get("text"),
                        "excerpt": node.get("description"),
                        "created": node.get("datePublished"),
                        "author": node.get("author"),
                        "image": node.get("image"),
                    }
                if kind == "answer" and node_type == "Question":
                    answers = (
                        node.get("acceptedAnswer") or node.get("suggestedAnswer") or []
                    )
                    if isinstance(answers, dict):
                        answers = [answers]
                    for answer in answers if isinstance(answers, list) else []:
                        answer = _dict(answer)
                        answer_url = answer.get("url") or answer.get("@id") or ""
                        if ZhihuParser._identity(answer_url) == ("answer", content_id):
                            return {
                                "content": answer.get("text"),
                                "created": answer.get("dateCreated"),
                                "author": answer.get("author"),
                                "question": {"title": node.get("name")},
                            }
                if kind == "zvideo" and node_type == "VideoObject":
                    return {
                        "title": node.get("name"),
                        "description": node.get("description"),
                        "created": node.get("uploadDate"),
                        "contentUrl": node.get("contentUrl"),
                        "thumbnailUrl": node.get("thumbnailUrl"),
                        "author": node.get("author"),
                    }
        return {}

    @staticmethod
    def _timestamp(value: Any) -> str:
        if isinstance(value, str) and not value.isdecimal():
            return value
        try:
            return (
                datetime.fromtimestamp(float(value), UTC).strftime("%Y-%m-%d %H:%M:%S")
                if value
                else ""
            )
        except (TypeError, ValueError, OverflowError, OSError):
            return ""

    def _build_metadata(
        self, url: str, kind: str, data: dict[str, Any]
    ) -> MediaMetadata:
        author = _dict(data.get("author"))
        question = _dict(data.get("question"))
        title = (
            data.get("title")
            or question.get("title")
            or ("知乎回答" if kind == "answer" else "知乎内容")
        )
        full_content = data.get("detail") if kind == "question" else data.get("content")
        if kind == "zvideo":
            full_content = data.get("description")
        preview = not full_content and bool(data.get("excerpt"))
        content = full_content or data.get("excerpt") or ""
        parsed = _ContentParser()
        parsed.feed(content if isinstance(content, str) else "")
        image_values = list(parsed.images)
        for key in ("title_image", "titleImage", "image", "thumbnailUrl"):
            value = data.get(key)
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, dict):
                    item = item.get("url")
                if candidate := _media_url(item):
                    image_values.insert(0, candidate)
        image_values = list(dict.fromkeys(image_values))
        video_urls = list(parsed.video_urls)
        if direct := _media_url(data.get("contentUrl")):
            video_urls.insert(0, direct)
        playlist = _dict(_dict(data.get("video")).get("playlist")) or _dict(
            data.get("playlist")
        )
        candidates = self._playlist_urls(playlist)
        video_groups = [[value] for value in dict.fromkeys(video_urls)]
        if candidates:
            video_groups.append(candidates)
        for group in video_groups:
            for index, value in enumerate(group):
                if urlparse(value).path.lower().endswith(".m3u8"):
                    group[index] = "m3u8:" + value
        media_headers = self._headers(use_cookie=False)
        media_headers["Referer"] = url
        media_headers["Accept"] = "*/*"
        result: MediaMetadata = {
            "url": url,
            "title": _plain(title),
            "author": _plain(author.get("name")),
            "avatar_url": _media_url(
                author.get("avatar_url") or author.get("avatarUrl")
            ),
            "desc": parsed.text(),
            "timestamp": self._timestamp(
                data.get("created_time")
                or data.get("createdTime")
                or data.get("created")
                or data.get("publishedTime")
            ),
            "platform": self.name,
            "image_urls": [[value] for value in image_values],
            "video_urls": video_groups,
            "image_headers": media_headers.copy(),
            "video_headers": media_headers.copy(),
            "video_force_download": bool(video_groups),
            "use_image_proxy": bool(self._get_proxy()),
            "use_video_proxy": bool(self._get_proxy()),
            "proxy_url": self.proxy_url or "",
        }
        if kind == "zvideo" and image_values:
            result["video_cover_urls"] = [[image_values[0]] for _ in video_groups]
        paid = _dict(data.get("paid_info") or data.get("paidInfo"))
        purchased = bool(
            data.get("is_purchased")
            or data.get("is_bought")
            or paid.get("is_purchased")
            or paid.get("is_bought")
        )
        if (
            preview
            or data.get("is_locked")
            or data.get("isLocked")
            or (
                not purchased
                and (
                    data.get("is_paid")
                    or data.get("isPaid")
                    or paid.get("is_paid")
                    or paid.get("isPaid")
                )
            )
        ):
            result.update(
                {
                    "access_status": "restricted",
                    "restriction_type": "login_or_paid",
                    "restriction_label": "仅公开预览",
                    "is_preview_only": True,
                    "can_access_full_video": False,
                    "access_message": "该知乎内容仅返回公开预览，完整内容可能需要登录或购买权限",
                }
            )
        return result

    @staticmethod
    def _playlist_urls(playlist: dict[str, Any]) -> list[str]:
        candidates: list[str] = []
        for key in ("FHD", "HD", "SD", "LD", "fhd", "hd", "sd", "ld"):
            item = _dict(playlist.get(key))
            if value := _media_url(
                item.get("play_url") or item.get("playUrl") or item.get("url")
            ):
                candidates.append(value)
        return list(dict.fromkeys(candidates))

    async def parse(self, session: aiohttp.ClientSession, url: str) -> MediaMetadata:
        kind, content_id = self._identity(url)
        if not kind:
            raise ValueError("不支持的知乎链接")
        if kind == "article":
            canonical = f"https://zhuanlan.zhihu.com/p/{content_id}"
        elif kind == "answer" and re.search(r"/question/\d+/answer/", url):
            canonical = "https://www.zhihu.com" + urlparse(url).path.rstrip("/")
        else:
            canonical = f"https://www.zhihu.com/{kind}/{content_id}"
        endpoint = {
            "answer": "answers",
            "question": "questions",
            "article": "articles",
            "zvideo": "zvideos",
        }[kind]
        api_url = f"https://www.zhihu.com/api/v4/{endpoint}/{content_id}"
        if kind == "article":
            api_url = f"https://zhuanlan.zhihu.com/api/articles/{content_id}"
        elif kind == "answer":
            api_url += "?include=content,created_time,updated_time,author,question"
        elif kind == "question":
            api_url += "?include=detail,created,author,answer_count"
        async with self.semaphore:
            try:
                data = await self._fetch_json(session, api_url)
                if data.get("id") is not None and str(data["id"]) != content_id:
                    raise RuntimeError("知乎接口返回了其他内容")
                if not data.get("id") and not (
                    data.get("title") or data.get("content")
                ):
                    raise RuntimeError("知乎接口未返回内容")
            except (TimeoutError, aiohttp.ClientError, RuntimeError):
                page = await self._fetch_page(session, canonical)
                data = self._entity_from_page(
                    page, kind, content_id
                ) or self._jsonld_from_page(page, kind, content_id)
                if not data:
                    raise RuntimeError(ACCESS_ERROR)
            result = self._build_metadata(canonical, kind, data)
            # Resolve only official embedded video IDs. Cookie never goes to the video CDN.
            video_ids: list[str] = []
            if self.include_media:
                rich_text = _ContentParser()
                rich_text.feed(str(data.get("content") or ""))
                video_ids = list(dict.fromkeys(rich_text.video_ids))[:3]
            for video_id in video_ids:
                try:
                    video = await self._fetch_json(
                        session,
                        f"https://lens.zhihu.com/api/v4/videos/{video_id}",
                        use_cookie=False,
                    )
                    sources = self._playlist_urls(_dict(video.get("playlist")))
                    if sources:
                        result["video_urls"].append(
                            [
                                "m3u8:" + value
                                if urlparse(value).path.lower().endswith(".m3u8")
                                else value
                                for value in sources
                            ]
                        )
                        result["video_force_download"] = True
                except (TimeoutError, aiohttp.ClientError, RuntimeError):
                    continue
        if (
            not result.get("desc")
            and not result.get("image_urls")
            and not result.get("video_urls")
            and kind != "question"
        ):
            raise RuntimeError(
                "知乎未返回可访问的正文或媒体，完整内容可能需要登录或权限"
            )
        return result
