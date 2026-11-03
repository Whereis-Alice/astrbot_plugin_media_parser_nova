"""微博解析器实现。"""

import asyncio
import html as html_lib
import json
import re
from datetime import datetime
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse, parse_qs, urljoin

import aiohttp

from ...logger import logger

from .base import BaseVideoParser
from ..utils import SkipParse, build_request_headers, trim_url_tail


class WeiboParser(BaseVideoParser):
    """微博解析器实现。"""

    HTTP_URL_RE = re.compile(r"https?://[^\s<>\"'()]+", re.IGNORECASE)
    WEB_HOSTS = frozenset({"weibo.com", "www.weibo.com"})
    CN_HOSTS = frozenset({"weibo.cn", "www.weibo.cn"})
    MOBILE_HOST = "m.weibo.cn"
    VIDEO_HOST = "video.weibo.com"
    SHORT_HOST = "t.cn"

    def __init__(
        self, hot_comment_count: int = 0, cookie: str = "",
        use_proxy: bool = False, proxy_url: Optional[str] = None,
    ):
        """初始化微博解析器"""
        super().__init__("weibo")
        self.cookie = str(cookie or "").replace("\r", "").replace("\n", "").strip()
        self.proxy_url = proxy_url if use_proxy and proxy_url else None
        self.semaphore = asyncio.Semaphore(10)
        try:
            self.hot_comment_count = max(0, int(hot_comment_count))
        except (TypeError, ValueError):
            self.hot_comment_count = 0

    def can_parse(self, url: str) -> bool:
        """判断是否可以解析此URL

        Args:
            url: 微博链接

        Returns:
            是否可以解析
        """
        result = self._identify_url(url) is not None
        if result:
            logger.debug(f"[{self.name}] can_parse: 匹配微博链接 {url}")
        else:
            logger.debug(f"[{self.name}] can_parse: 无法解析 {url}")
        return result

    def extract_links(self, text: str) -> List[str]:
        """从文本中提取微博链接

        Args:
            text: 输入文本

        Returns:
            提取到的微博链接列表
        """
        links: List[str] = []
        seen = set()
        for match in self.HTTP_URL_RE.finditer(text or ""):
            link = trim_url_tail(match.group(0))
            if not self.can_parse(link):
                continue
            identity = self._identify_url(link)
            key = identity if identity else link
            if key in seen:
                continue
            seen.add(key)
            links.append(link)
        return links

    @classmethod
    def _identify_url(cls, url: str) -> Optional[tuple[str, str]]:
        """返回受支持 URL 的类型与作品 ID。"""
        if not isinstance(url, str) or not url.strip():
            return None
        try:
            parsed = urlparse(url.strip())
            if parsed.scheme.lower() not in {"http", "https"}:
                return None
            if parsed.username or parsed.password:
                return None
            expected_port = 80 if parsed.scheme.lower() == "http" else 443
            if parsed.port not in {None, expected_port}:
                return None
        except (TypeError, ValueError):
            return None

        host = (parsed.hostname or "").lower().strip(".")
        path = parsed.path or ""
        if host in cls.WEB_HOSTS:
            post_match = re.fullmatch(r"/(\d+)/([A-Za-z0-9]+)/?", path)
            if post_match:
                return "weibo_com", post_match.group(2)
            video_match = re.fullmatch(r"/tv/show/(\d+:\d+)/?", path)
            if video_match:
                return "video_weibo", video_match.group(1)
        elif host in cls.CN_HOSTS:
            status_match = re.fullmatch(r"/status/(\d+)/?", path)
            if status_match:
                return "weibo_com", status_match.group(1)
        elif host == cls.MOBILE_HOST:
            detail_match = re.fullmatch(r"/(?:detail|status)/([A-Za-z0-9]+)/?", path)
            if detail_match:
                return "m_weibo_cn", detail_match.group(1)
        elif host == cls.VIDEO_HOST and path.rstrip("/") == "/show":
            video_ids = parse_qs(parsed.query).get("fid") or []
            if video_ids and re.fullmatch(r"\d+:\d+", video_ids[0]):
                return "video_weibo", video_ids[0]
        elif host == cls.SHORT_HOST:
            short_match = re.fullmatch(r"/([A-Za-z0-9]+)/?", path)
            if short_match:
                return "short_weibo", short_match.group(1)
        return None

    async def _resolve_short_url(self, session: aiohttp.ClientSession, url: str) -> str:
        """只展开 t.cn 分享，其他网站的 t.cn 分享交还其他解析器。"""
        current = url
        for _ in range(5):
            identity = self._identify_url(current)
            if not identity:
                raise SkipParse("t.cn 分享目标不是受支持的微博作品")
            if identity[0] != "short_weibo":
                return current
            async with session.get(
                current, allow_redirects=False, proxy=self.proxy_url,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status not in {301, 302, 303, 307, 308}:
                    raise RuntimeError("微博短链未返回可用跳转")
                location = response.headers.get("Location", "")
                if not location:
                    raise RuntimeError("微博短链跳转缺少目标地址")
                current = urljoin(current, location)
        raise RuntimeError("微博短链重定向次数过多")

    async def _read_json(self, response: aiohttp.ClientResponse) -> Dict[str, Any]:
        """微博风控可能返回 HTTP 200 HTML，明确说明需要登录/重试。"""
        if response.status != 200:
            raise RuntimeError(f"微博接口请求失败: HTTP {response.status}")
        try:
            data = await response.json(content_type=None)
        except (json.JSONDecodeError, ValueError, aiohttp.ContentTypeError) as error:
            raise RuntimeError("微博返回登录或风控页面，可配置微博 Cookie 后重试") from error
        if not isinstance(data, dict):
            raise RuntimeError("微博接口返回的数据不是对象")
        if data.get("ok") == 0:
            raise RuntimeError(f"微博作品不可访问: {data.get('msg') or '需要登录或作品已删除'}")
        return data

    def _get_url_type(self, url: str) -> str:
        """根据URL判断微博链接类型

        Args:
            url: 微博链接

        Returns:
            链接类型: 'weibo_com', 'm_weibo_cn', 'video_weibo'

        Raises:
            ValueError: 无法识别的URL类型
        """
        identity = self._identify_url(url)
        if identity:
            return identity[0]
        raise ValueError(f"无法识别的URL类型: {url}")

    def _extract_page_id(self, url: str) -> str:
        """从微博 URL 中提取页面 ID

        支持数字ID和短ID格式：
        - 数字ID: https://weibo.com/1566936885/5232446897127970
        - 短ID: https://weibo.com/1566936885/QdC5HtUjg

        Args:
            url: 微博链接

        Returns:
            页面 ID（数字ID或短ID）

        Raises:
            ValueError: 无法提取页面 ID
        """
        identity = self._identify_url(url)
        if identity and identity[0] == "weibo_com":
            return identity[1]
        raise ValueError(f"无法从 URL 中提取页面 ID: {url}")

    def _extract_blog_id(self, url: str) -> str:
        """从 m.weibo.cn URL 中提取博客 ID

        Args:
            url: m.weibo.cn 链接

        Returns:
            博客 ID

        Raises:
            ValueError: 无法提取博客 ID
        """
        identity = self._identify_url(url)
        if identity and identity[0] == "m_weibo_cn":
            return identity[1]
        raise ValueError(f"无法从 URL 中提取博客 ID: {url}")

    def _extract_video_id(self, url: str) -> str:
        """从视频 URL 中提取视频 ID

        Args:
            url: 视频链接

        Returns:
            视频 ID，格式如 1034:5233218052358208

        Raises:
            ValueError: 无法提取视频 ID
        """
        identity = self._identify_url(url)
        if identity and identity[0] == "video_weibo":
            return identity[1]
        raise ValueError(f"无法从 URL 中提取视频 ID: {url}")

    async def _get_visitor_cookies(self, session: aiohttp.ClientSession) -> str:
        """获取微博访客cookie

        Args:
            session: aiohttp 会话

        Returns:
            完整的cookie字符串

        Raises:
            Exception: 获取失败
        """
        url = "https://visitor.passport.weibo.cn/visitor/genvisitor2"

        headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "content-type": "application/x-www-form-urlencoded",
            "accept-encoding": "gzip, deflate",
        }

        data = {"cb": "visitor_gray_callback"}

        async with session.post(
            url, headers=headers, data=data, proxy=self.proxy_url,
            allow_redirects=False, timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            if response.status != 200:
                raise Exception(f"获取cookie失败，状态码: {response.status}")

            cookies = []
            for cookie in response.cookies.values():
                cookies.append(f"{cookie.key}={cookie.value}")

            if not cookies:
                raise Exception("获取cookie失败：响应中未包含cookie")

            cookie_str = "; ".join(cookies)

            if "XSRF-TOKEN" not in cookie_str:
                async with session.get(
                    "https://weibo.com", headers={"user-agent": headers["user-agent"]},
                    proxy=self.proxy_url, allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as page_response:
                    if page_response.status == 200:
                        for cookie in page_response.cookies.values():
                            if cookie.key == "XSRF-TOKEN":
                                cookies.append(f"{cookie.key}={cookie.value}")
                                cookie_str = "; ".join(cookies)
                                break

            return cookie_str

    def _format_author(self, screen_name: str, user_id: str) -> str:
        """格式化作者字段

        Args:
            screen_name: 用户名
            user_id: 用户ID

        Returns:
            格式化后的作者字符串，格式: {用户名}(uid:{uid})
        """
        if screen_name and user_id:
            return f"{screen_name}(uid:{user_id})"
        return screen_name or ""

    def _normalize_url(self, url: str) -> str:
        """规范化URL，补全协议

        Args:
            url: 原始URL

        Returns:
            规范化后的URL
        """
        if not isinstance(url, str):
            return ""
        normalized = url.strip()
        if normalized.startswith("//"):
            return "https:" + normalized
        return normalized if normalized.startswith(("http://", "https://")) else ""

    def _extract_video_url_from_dict(self, urls: Dict[str, str]) -> Optional[str]:
        """从URL字典中提取视频URL

        Args:
            urls: URL字典，键为清晰度标识，值为URL

        Returns:
            视频URL，不存在时为None
        """
        if not urls or not isinstance(urls, dict):
            return None
        for video_url in urls.values():
            normalized = self._normalize_url(video_url)
            if normalized:
                return normalized
        return None

    def _extract_video_url_from_media_info(
        self, media_info: Dict[str, Any]
    ) -> Optional[str]:
        """从media_info中提取视频URL

        Args:
            media_info: 媒体信息字典

        Returns:
            视频URL，优先返回高清URL，如果不存在则返回普通URL
        """
        if not isinstance(media_info, dict) or not media_info:
            return None
        hd_url = media_info.get("hd_url") or media_info.get("stream_url_hd")
        if isinstance(hd_url, str) and hd_url.strip():
            return self._normalize_url(hd_url)
        stream_url = media_info.get("stream_url")
        if isinstance(stream_url, str) and stream_url.strip():
            return self._normalize_url(stream_url)
        playback_list = media_info.get("playback_list") or []
        choices = []
        for entry in playback_list if isinstance(playback_list, list) else []:
            if not isinstance(entry, dict):
                continue
            info = entry.get("play_info") or {}
            if not isinstance(info, dict):
                continue
            candidate = self._normalize_url(info.get("url"))
            if not candidate:
                continue
            try:
                quality = float(info.get("bitrate") or 0)
            except (TypeError, ValueError):
                quality = 0
            choices.append((quality, candidate))
        if choices:
            return max(choices, key=lambda item: item[0])[1]
        return None

    def _extract_pic_url(self, pic_data: Dict[str, Any]) -> Optional[str]:
        """从图片数据中提取URL，按优先级顺序尝试

        Args:
            pic_data: 图片数据字典，可能包含 largest, original, large 等字段

        Returns:
            图片URL，不存在时为None
        """
        if not isinstance(pic_data, dict):
            return None
        for key in ["largest", "original", "large"]:
            size_info = pic_data.get(key, {})
            if isinstance(size_info, dict):
                url = size_info.get("url")
                if isinstance(url, str) and url.strip():
                    return self._normalize_url(url)
        fallback = pic_data.get("url")
        if isinstance(fallback, str) and fallback.strip():
            return self._normalize_url(fallback)
        return None

    @staticmethod
    def _assert_status_matches_requested(
        status: Dict[str, Any], requested_id: str
    ) -> None:
        """在响应包含可比 ID 时拒绝其他微博作品的数据。"""
        if not isinstance(status, dict):
            raise RuntimeError("微博接口返回的作品数据不是对象")
        requested_id = str(requested_id or "").strip()
        keys = ("id", "idstr", "mid") if requested_id.isdigit() else ("mblogid", "bid")
        candidates = {
            str(status.get(key)).strip()
            for key in keys
            if status.get(key) not in (None, "")
        }
        if candidates and requested_id not in candidates:
            raise RuntimeError("微博接口返回了其他作品的数据")

    @staticmethod
    def _assert_video_matches_requested(
        playinfo: Dict[str, Any], requested_id: str
    ) -> None:
        """校验视频接口显式返回的对象 ID。"""
        if not isinstance(playinfo, dict):
            raise RuntimeError("微博视频接口返回的播放数据不是对象")
        candidates = {
            str(playinfo.get(key)).strip()
            for key in ("oid", "fid", "object_id")
            if playinfo.get(key) not in (None, "")
        }
        if candidates and str(requested_id) not in candidates:
            raise RuntimeError("微博视频接口返回了其他作品的数据")

    def _build_result_dict(
        self,
        url: str,
        author: str,
        desc: str,
        timestamp: str,
        video_urls: List[List[str]],
        image_urls: List[List[str]],
        avatar_url: str = "",
    ) -> Dict[str, Any]:
        """构建解析结果字典

        Args:
            url: 原始URL
            author: 作者
            desc: 描述
            timestamp: 时间戳
            video_urls: 视频URL列表（List[List[str]]）
            image_urls: 图片URL列表（List[List[str]]）

        Returns:
            解析结果字典
        """
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        referer = "https://weibo.com/"
        image_headers = build_request_headers(
            is_video=False, referer=referer, user_agent=user_agent
        )
        video_headers = build_request_headers(
            is_video=True, referer=referer, user_agent=user_agent
        )
        result = {
            'url': url,
            'title': '',
            'author': author,
            'avatar_url': avatar_url,
            'desc': desc,
            'timestamp': timestamp,
            'video_urls': video_urls,
            'image_urls': image_urls,
            'image_headers': image_headers,
            'video_headers': video_headers,
            'platform': 'weibo',
            'use_image_proxy': bool(self.proxy_url),
            'use_video_proxy': bool(self.proxy_url),
            'proxy_url': self.proxy_url,
        }
        if video_urls:
            result["video_force_download"] = True
        return result

    def _separate_media_urls(self, media_urls: List[str]) -> tuple:
        """将媒体URL列表分离为视频和图片URL列表

        Args:
            media_urls: 混合的媒体URL列表

        Returns:
            (video_urls, image_urls) 元组，每个都是 List[List[str]] 格式
        """
        video_urls = []
        image_urls = []

        for url in media_urls:
            if not isinstance(url, str) or not url:
                continue

            url_lower = url.lower()
            is_video = (
                "video" in url_lower
                or ".mp4" in url_lower
                or "stream" in url_lower
                or "playback" in url_lower
            )

            if is_video:
                video_urls.append([url])
            else:
                image_urls.append([url])

        return video_urls, image_urls

    def _build_weibo_headers(self, referer: str, cookies: str) -> Dict[str, str]:
        """构建微博接口请求头。"""
        xsrf_token = ""
        match = re.search(r"(?:^|;\s*)XSRF-TOKEN=([^;]+)", cookies)
        if match:
            xsrf_token = match.group(1)
        headers = {
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0"
            ),
            "referer": referer,
            "cookie": cookies,
            "accept": "application/json, text/plain, */*",
            "x-requested-with": "XMLHttpRequest",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "accept-language": "zh-CN,zh;q=0.9",
        }
        if xsrf_token:
            headers["x-xsrf-token"] = xsrf_token
        return headers

    @staticmethod
    def _format_comment_time(created_at: str) -> str:
        """将微博评论时间格式化为统一展示文本。"""
        if not created_at:
            return ""
        try:
            dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return created_at

    def _normalize_hot_comment_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """将微博热评结构规范化为统一字段。"""
        user = item.get("user") or {}
        try:
            likes = int(item.get("like_counts", 0) or 0)
        except (TypeError, ValueError):
            likes = 0
        message = self._clean_html_text(
            str(item.get("text_raw") or item.get("text") or "")
        )
        time_text = self._format_comment_time(str(item.get("created_at", "") or ""))
        return {
            "username": str(user.get("screen_name", "") or ""),
            "uid": str(user.get("id", "") or ""),
            # 卡片评论区要画真头像；大图优先，退回普通尺寸
            "avatar_url": str(
                user.get("avatar_large")
                or user.get("avatar_hd")
                or user.get("profile_image_url")
                or ""
            ).strip(),
            "likes": likes,
            "message": message,
            "time": time_text,
        }

    async def _fetch_hot_comments(
        self,
        session: aiohttp.ClientSession,
        cookies: str,
        status_id: str,
        uid: str = "",
    ) -> List[Dict[str, Any]]:
        """异步拉取微博热评列表。"""
        if self.hot_comment_count <= 0:
            return []
        status_id = str(status_id or "").strip()
        uid = str(uid or "").strip()
        if not status_id:
            return []

        params = {
            "id": status_id,
            "flow": 0,
            "is_reload": 1,
            "is_show_bulletin": 2,
            "is_mix": 0,
            "count": max(20, self.hot_comment_count),
        }
        if uid:
            params["uid"] = uid

        headers = self._build_weibo_headers(
            referer=f"https://weibo.com/0/{status_id}", cookies=cookies
        )
        async with session.get(
            "https://weibo.com/ajax/statuses/buildComments",
            headers=headers,
            params=params,
            timeout=aiohttp.ClientTimeout(total=15),
            proxy=self.proxy_url,
            allow_redirects=False,
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(
                    f"获取微博热评失败: HTTP {resp.status}, response: {body[:300]}"
                )
            data = await resp.json(content_type=None)
        comment_list = data.get("data")
        if not isinstance(comment_list, list):
            raise RuntimeError(f"微博热评接口返回异常: {data}")
        comments = [
            self._normalize_hot_comment_item(item)
            for item in comment_list
            if isinstance(item, dict)
        ]
        comments = [item for item in comments if item.get("message")]
        comments.sort(key=lambda x: x.get("likes", 0), reverse=True)
        return comments[: self.hot_comment_count]

    async def _attach_hot_comments_to_result(
        self,
        session: aiohttp.ClientSession,
        result: Dict[str, Any],
        cookies: str,
        status_id: str,
        uid: str = "",
    ) -> None:
        """按配置将热评附加到微博解析结果。"""
        if self.hot_comment_count <= 0:
            return
        if not isinstance(result, dict):
            return
        try:
            comments = await self._fetch_hot_comments(
                session=session, cookies=cookies, status_id=status_id, uid=uid
            )
            if comments:
                result["hot_comments"] = comments
        except Exception as e:
            logger.warning(
                f"[{self.name}] 获取热评失败: status_id={status_id}, "
                f"uid={uid}, 错误: {e}"
            )

    async def _status_to_result(
        self, session: aiohttp.ClientSession, url: str, cookies: str,
        status: Dict[str, Any], requested_id: str,
    ) -> Dict[str, Any]:
        """桌面/移动接口统一保留正文、长文、图片和混合媒体。"""
        self._assert_status_matches_requested(status, requested_id)
        status_id = str(status.get("idstr") or status.get("id") or status.get("mid") or requested_id)
        long_text = status.get("longText") or {}
        if not isinstance(long_text, dict):
            long_text = {}
        full_text = status.get("longTextContent") or long_text.get("longTextContent")
        partial = False
        if status.get("isLongText") and not full_text:
            try:
                async with session.get(
                    "https://m.weibo.cn/statuses/extend", params={"id": status_id},
                    headers=self._build_weibo_headers("https://m.weibo.cn/", cookies),
                    proxy=self.proxy_url, allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    extended = await self._read_json(response)
                    payload = extended.get("data") or {}
                    if isinstance(payload, dict):
                        full_text = payload.get("longTextContent")
            except (RuntimeError, aiohttp.ClientError, asyncio.TimeoutError):
                partial = True
            partial = partial or not full_text
        raw_text = full_text or status.get("text_raw") or status.get("text") or ""
        clean_text = self._clean_html_text(str(raw_text))
        media_urls = self._extract_media_urls(status)
        if not media_urls and not clean_text:
            raise RuntimeError("微博未返回可用正文或媒体，作品可能不可访问")
        if partial:
            clean_text += "\n（微博长文未能获取，已显示公开摘要）"
        user = status.get("user") or {}
        if not isinstance(user, dict):
            user = {}
        video_urls, image_urls = self._separate_media_urls(media_urls)
        result = self._build_result_dict(
            url, self._format_author(user.get("screen_name", ""), user.get("id", "")),
            clean_text, self._format_timestamp(status.get("created_at", "")),
            video_urls, image_urls, self._extract_avatar_url(user),
        )
        page_info = status.get("page_info") or {}
        if video_urls and isinstance(page_info, dict):
            page_pic = page_info.get("page_pic")
            if isinstance(page_pic, dict):
                page_pic = page_pic.get("url")
            cover = self._normalize_url(page_pic)
            if cover:
                result["video_cover_urls"] = [[cover]]
        if partial:
            result["is_preview_only"] = True
            result["restriction_label"] = "长文摘要"
            result["access_message"] = "微博长文未能获取，已显示公开摘要"
        await self._attach_hot_comments_to_result(
            session, result, cookies, status_id, str(user.get("id") or ""),
        )
        return result

    async def _parse_weibo_com(
        self, session: aiohttp.ClientSession, url: str, cookies: str
    ) -> Dict[str, Any]:
        """使用桌面公开接口解析正文和混合媒体。"""
        page_id = self._extract_page_id(url)
        async with session.get(
            "https://weibo.com/ajax/statuses/show",
            params={"id": page_id, "locale": "zh-CN", "isGetLongText": "true"},
            headers=self._build_weibo_headers(url, cookies),
            proxy=self.proxy_url, allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            data = await self._read_json(response)
        status = data.get("data") if isinstance(data.get("data"), dict) else data
        return await self._status_to_result(session, url, cookies, status, page_id)

    async def _parse_m_weibo_cn(
        self, session: aiohttp.ClientSession, url: str, cookies: str
    ) -> Dict[str, Any]:
        """优先移动 JSON 接口，兼容仍嵌入 render_data 的旧页面。"""
        blog_id = self._extract_blog_id(url)
        headers = self._build_weibo_headers("https://m.weibo.cn/", cookies)
        try:
            async with session.get(
                "https://m.weibo.cn/statuses/show", params={"id": blog_id},
                headers=headers, proxy=self.proxy_url, allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                data = await self._read_json(response)
            status = data.get("data") if isinstance(data.get("data"), dict) else data
            return await self._status_to_result(session, url, cookies, status, blog_id)
        except (RuntimeError, aiohttp.ClientError, asyncio.TimeoutError) as api_error:
            async with session.get(
                f"https://m.weibo.cn/detail/{blog_id}", headers=headers,
                proxy=self.proxy_url, allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status != 200:
                    raise api_error
                page = await response.text()
            match = re.search(r"var \$render_data\s*=\s*(\[.*?\])\s*\[0\]", page, re.DOTALL)
            if not match:
                raise api_error
            try:
                rendered = json.loads(match.group(1))
            except json.JSONDecodeError:
                raise api_error
            if not isinstance(rendered, list) or not rendered or not isinstance(rendered[0], dict):
                raise api_error
            status = rendered[0].get("status") or {}
            return await self._status_to_result(session, url, cookies, status, blog_id)

    async def _parse_video_weibo(
        self, session: aiohttp.ClientSession, url: str, cookies: str
    ) -> Dict[str, Any]:
        """解析 video.weibo.com 链接

        Args:
            session: aiohttp 会话
            url: 视频链接
            cookies: cookie 字符串

        Returns:
            解析结果字典

        Raises:
            Exception: 解析失败
        """
        video_id = self._extract_video_id(url)
        referer_url = f"https://weibo.com/tv/show/{video_id}?from=old_pc_videoshow"
        api_url = f"https://weibo.com/tv/api/component?page=/tv/show/{video_id}"

        headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0",
            "referer": referer_url,
            "cookie": cookies,
            "content-type": "application/x-www-form-urlencoded",
            "accept-encoding": "gzip, deflate",
        }

        payload = {"data": json.dumps({"Component_Play_Playinfo": {"oid": video_id}})}

        async with session.post(
            api_url, headers=headers, data=payload, proxy=self.proxy_url,
            allow_redirects=False, timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            if response.status == 200:
                json_data = await self._read_json(response)
                if not isinstance(json_data, dict):
                    raise RuntimeError("微博视频接口返回的 JSON 不是对象")
                response_data = json_data.get("data") or {}
                if not isinstance(response_data, dict):
                    raise RuntimeError("微博视频接口返回的 data 不是对象")
                playinfo = response_data.get("Component_Play_Playinfo") or {}
                self._assert_video_matches_requested(playinfo, video_id)
                media_urls = self._extract_media_urls_video(json_data)

                if not media_urls:
                    raise Exception("未找到视频文件")

                desc = playinfo.get("title", "") or playinfo.get("content1", "")
                screen_name = playinfo.get("author", "") or playinfo.get(
                    "author_name", ""
                )
                playinfo_user = playinfo.get("user") or {}
                if not isinstance(playinfo_user, dict):
                    playinfo_user = {}
                user_id = playinfo.get("author_id", "") or playinfo_user.get("id", "")
                author = self._format_author(screen_name, user_id)
                avatar_url = self._extract_avatar_url(playinfo)

                video_urls, image_urls = self._separate_media_urls(media_urls)
                status_id = str(playinfo.get("mid") or "")
                uid = str(user_id or "")
                result = self._build_result_dict(
                    url, author, desc, '', video_urls, image_urls, avatar_url
                )
                await self._attach_hot_comments_to_result(
                    session=session,
                    result=result,
                    cookies=cookies,
                    status_id=status_id,
                    uid=uid,
                )
                return result
            else:
                raise Exception(
                    f"获取微博视频数据失败: HTTP {response.status}"
                )

    def _extract_media_urls(self, json_data: Dict[str, Any]) -> List[str]:
        """从 JSON 数据中提取所有媒体链接（图片和视频）

        Args:
            json_data: 微博 JSON 数据

        Returns:
            媒体链接列表
        """
        media_urls = []

        mix_media_info = json_data.get("mix_media_info") or {}
        items = (
            mix_media_info.get("items") or []
            if isinstance(mix_media_info, dict)
            else []
        )
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type", "")
                data = item.get("data") or {}
                if not isinstance(data, dict):
                    continue

                if item_type == "pic":
                    pic_url = self._extract_pic_url(data)
                    if pic_url:
                        media_urls.append(pic_url)

                elif item_type == "video":
                    media_info = data.get("media_info", {})
                    video_url = self._extract_video_url_from_media_info(media_info)
                    if video_url:
                        media_urls.append(video_url)

        pic_infos = json_data.get("pic_infos") or {}
        if isinstance(pic_infos, dict):
            pic_ids = json_data.get("pic_ids") or []
            ordered_pics = (
                [pic_infos[key] for key in pic_ids if key in pic_infos]
                if isinstance(pic_ids, list) and pic_ids else list(pic_infos.values())
            )
            for pic_info in ordered_pics:
                if not isinstance(pic_info, dict):
                    continue
                pic_type = pic_info.get("type", "")

                if pic_type == "gif" and pic_info.get("video"):
                    video_url = pic_info.get("video", "")
                    if video_url:
                        media_urls.append(video_url)
                        continue

                pic_url = self._extract_pic_url(pic_info)
                if pic_url:
                    media_urls.append(pic_url)

        pics = json_data.get("pics") or []
        if isinstance(pics, list):
            for pic in pics:
                pic_url = self._extract_pic_url(pic)
                if pic_url:
                    media_urls.append(pic_url)

        page_info = json_data.get("page_info") or {}
        if isinstance(page_info, dict):
            urls = page_info.get("urls", {})
            video_url = self._extract_video_url_from_dict(urls)
            if video_url:
                media_urls.append(video_url)

            media_info = page_info.get("media_info", {})
            video_url = self._extract_video_url_from_media_info(media_info)
            if video_url:
                media_urls.append(video_url)

        video_info = json_data.get("video_info") or {}
        if isinstance(video_info, dict):
            video_details = video_info.get("video_details") or {}
            video_url = (
                video_details.get("video_details") or {}
                if isinstance(video_details, dict)
                else {}
            )
            if isinstance(video_url, dict) and video_url:
                max_quality = max(
                    video_url.keys(),
                    key=lambda x: int(x) if str(x).isdigit() else 0,
                    default=None,
                )
                if max_quality:
                    quality_data = video_url[max_quality]
                    if isinstance(quality_data, dict):
                        url = self._normalize_url(quality_data.get("url", ""))
                        if url:
                            media_urls.append(url)

        return list(dict.fromkeys(media_urls))

    def _extract_media_urls_m_weibo(self, json_data: Dict[str, Any]) -> List[str]:
        """从 m.weibo.cn JSON 数据中提取所有媒体链接

        Args:
            json_data: m.weibo.cn JSON 数据

        Returns:
            媒体链接列表
        """
        status = json_data.get("status") or {}
        if not isinstance(status, dict):
            return []
        return self._extract_media_urls(status)

    def _extract_media_urls_video(self, json_data: Dict[str, Any]) -> List[str]:
        """从 video.weibo.com JSON 数据中提取视频链接

        Args:
            json_data: video.weibo.com JSON 数据

        Returns:
            视频链接列表
        """
        media_urls = []
        response_data = json_data.get("data") or {}
        if not isinstance(response_data, dict):
            return media_urls
        playinfo = response_data.get("Component_Play_Playinfo") or {}
        if not isinstance(playinfo, dict):
            return media_urls
        video_url = self._extract_video_url_from_dict(playinfo.get("urls") or {})
        if video_url:
            media_urls.append(video_url)

        return media_urls

    def _clean_html_text(self, html_text: str) -> str:
        """清理HTML标签，提取纯文本

        处理可跳转标签（如话题标签、视频链接等），提取其中的文本内容

        Args:
            html_text: 包含HTML标签的文本

        Returns:
            清理后的纯文本
        """
        if not html_text:
            return ""

        text = html_text

        def replace_surl_text(match):
            """替换函数，提取 surl-text 内容"""
            return match.group(1)

        text = re.sub(
            r'<span\s+class=["\']surl-text["\']>(.*?)</span>',
            replace_surl_text,
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )

        text = re.sub(
            r'<span\s+class=["\']url-icon["\'][^>]*>.*?</span>',
            "",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )

        text = re.sub(r"<img[^>]*>", "", text, flags=re.IGNORECASE)

        text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)

        text = re.sub(r"<[^>]+>", "", text)

        text = html_lib.unescape(text)

        text = re.sub(r"\s+", " ", text)
        text = text.strip()

        return text

    def _format_timestamp(self, created_at: str) -> str:
        """格式化时间为 Y-M-D 格式

        Args:
            created_at: 原始时间字符串，格式如 "Thu Nov 13 21:18:29 +0800 2025"

        Returns:
            格式化后的时间字符串，格式如 "2025-11-13"
        """
        try:
            dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return created_at if created_at else ""

    async def parse(
        self, session: aiohttp.ClientSession, url: str
    ) -> Optional[Dict[str, Any]]:
        """解析微博作品；登录 Cookie 可选，取消不会被吞掉。"""
        async with self.semaphore:
            original_url = url
            url_type = self._get_url_type(url)
            if url_type == "short_weibo":
                url = await self._resolve_short_url(session, url)
                url_type = self._get_url_type(url)
            cookies = self.cookie
            if not cookies:
                try:
                    cookies = await self._get_visitor_cookies(session)
                except Exception as error:
                    # 访客接口偶尔不返回 Cookie，公开作品仍可尝试匿名解析。
                    logger.debug("微博访客会话不可用，尝试公开接口: %s", type(error).__name__)
                    cookies = ""
            if url_type == "weibo_com":
                result = await self._parse_weibo_com(session, url, cookies)
            elif url_type == "m_weibo_cn":
                result = await self._parse_m_weibo_cn(session, url, cookies)
            elif url_type == "video_weibo":
                result = await self._parse_video_weibo(session, url, cookies)
            else:
                raise ValueError(f"不支持的微博 URL 类型: {url_type}")
            if result and original_url != url:
                result["source_url"] = original_url
            return result
