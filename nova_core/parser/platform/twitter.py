"""Twitter/X 解析器实现。"""

import asyncio
import html as html_lib
import json
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import aiohttp

from ...constants import Config
from ...logger import logger
from ..utils import build_request_headers
from . import nitter as nitter_source
from .base import BaseVideoParser


class FxTwitterServiceUnavailableError(RuntimeError):
    """FxTwitter 服务不可达、超时或服务端错误。"""


class FxTwitterTweetUnavailableError(RuntimeError):
    """FxTwitter 可访问，但目标推文不可用或响应不是目标内容。"""


def json_dumps_compact(value: Any) -> str:
    """生成无多余空白的 JSON 查询参数。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


_LOGGED_OUT_COMMENT_RE = re.compile(
    r'"@id":"https://x\.com/[^"/]+/status/(?P<id>\d+)",'
    r'"@type":"Comment",author:\$R\[\d+\]=\{(?P<author>.*?)\},'
    r'commentCount:\d+,datePublished:"(?P<date>(?:\\.|[^"\\])*)",'
    r'identifier:"(?P=id)",interactionStatistic:(?P<stats>.*?)\],'
    r'text:"(?P<text>(?:\\.|[^"\\])*)"\}',
    re.S,
)
_JS_STRING_FIELD_TEMPLATE = r'{field}:"((?:\\.|[^"\\])*)"'
_JSONLD_SCRIPT_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)
# X 仅对爬虫 UA 返回内含 JSON-LD 的服务端渲染页；浏览器 UA 只会拿到
# 不含任何回复内容的 JS 外壳。同一个 UA 被风控拒绝(403)时换另一个爬虫
# UA 再试一次，因为 X 对不同爬虫的放行策略并不一致。
_CRAWLER_USER_AGENTS: Tuple[str, ...] = (
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
)
_UA_LABEL_RE = re.compile(r"compatible;\s*([A-Za-z]+)")
# 只保留 x.com。twitter.com 会 301 到 x.com，作为"第二来源"没有任何意义，
# 反而让失败日志看起来像请求了两个不同站点（旧日志里两条的 url= 都是 x.com）。
_PUBLIC_PAGE_HOST = "x.com"
_HANDLE_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_]")
_JSONLD_MARKER = "application/ld+json"
_COMMENT_NODE_MARKER = '"Comment"'
# 未登录抓取 X 回复随时可能被平台关掉：401/403/404/429 属于"平台不允许"，
# 不是插件异常。这类结果按冷却期提示一次即可，避免每条推文刷一行 WARN。
_BLOCKED_STATUSES = frozenset({401, 403, 404, 429})
_BLOCKED_NOTICE_INTERVAL = 900.0
# 热评是附加信息，不能拖慢正文解析：单次请求与整体抓取都设上限。
_PUBLIC_PAGE_TIMEOUT = 12.0
_HOT_COMMENT_BUDGET = 26.0
# Nitter 实例通常自建/同机，响应很快；给一个更短的超时避免拖慢正文。
_NITTER_TIMEOUT = 10.0
_NITTER_BUDGET = 18.0
_NITTER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_NITTER_HINT_INTERVAL = 3600.0


class TwitterParser(BaseVideoParser):
    """Twitter/X 解析器实现。"""

    # 进程内共享：控制"平台拒绝"提示的冷却，避免每条推文刷一行日志。
    _blocked_notice_at: float = 0.0
    # 进程内共享：控制"建议配置 Nitter"提示的冷却。
    _nitter_hint_at: float = 0.0

    def __init__(
        self,
        use_parse_proxy: bool = False,
        use_image_proxy: bool = False,
        use_video_proxy: bool = False,
        proxy_url: str = None,
        hot_comment_count: int = 0,
        nitter_base_url: str = "",
    ):
        """初始化Twitter解析器

        Args:
            use_parse_proxy: 解析时是否使用代理
            use_image_proxy: 图片下载是否使用代理
            use_video_proxy: 视频下载是否使用代理
            proxy_url: 代理地址（格式：http://host:port 或 socks5://host:port）
            hot_comment_count: 热评条数上限，0 表示不抓热评
            nitter_base_url: Nitter 实例地址（可用逗号分隔多个），留空关闭
        """
        super().__init__("twitter")
        self.use_parse_proxy = use_parse_proxy
        self.use_image_proxy = use_image_proxy
        self.use_video_proxy = use_video_proxy
        self.proxy_url = proxy_url
        try:
            self.hot_comment_count = min(20, max(0, int(hot_comment_count)))
        except (TypeError, ValueError):
            self.hot_comment_count = 0
        self.nitter_base_urls = nitter_source.normalize_base_urls(nitter_base_url)
        self.semaphore = asyncio.Semaphore(Config.PARSER_MAX_CONCURRENT)
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
        }

    @staticmethod
    def _decode_js_string(value: Any) -> str:
        raw = str(value or "")
        try:
            decoded = json.loads(f'"{raw}"')
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = raw.replace('\\"', '"').replace("\\\\", "\\")
        return html_lib.unescape(str(decoded or "")).strip()

    @staticmethod
    def _unescape_entities(value: Any) -> str:
        """反转义推文文本中的 HTML 实体（&amp;gt; / &amp;lt; / &amp;amp; 等）。

        Twitter/X 的 full_text 与 FxTwitter 的 text 字段都以 HTML 实体形式
        转义 < > &，直接展示会出现 "&gt;" 之类的字面量。必须在按
        display_text_range 截取之后再反转义，否则下标会错位。
        """
        text = str(value or "")
        if "&" not in text:
            return text
        return html_lib.unescape(text)

    @classmethod
    def _comment_author_field(cls, author_block: str, field: str) -> str:
        pattern = _JS_STRING_FIELD_TEMPLATE.format(field=re.escape(field))
        match = re.search(pattern, author_block)
        return cls._decode_js_string(match.group(1)) if match else ""

    @staticmethod
    def _format_public_comment_time(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError, OverflowError):
            return text

    @classmethod
    def _parse_logged_out_comments_html(
        cls,
        html_text: str,
        limit: int,
    ) -> List[Dict[str, Any]]:
        """解析 X 公开帖子页 JSON-LD 片段中的精选回复。"""
        comments: List[Dict[str, Any]] = []
        seen_ids = set()
        for match in _LOGGED_OUT_COMMENT_RE.finditer(str(html_text or "")):
            comment_id = str(match.group("id") or "").strip()
            if not comment_id or comment_id in seen_ids:
                continue
            seen_ids.add(comment_id)
            author_block = match.group("author") or ""
            screen_name = cls._comment_author_field(
                author_block, "alternateName"
            )
            display_name = cls._comment_author_field(author_block, "name")
            user_id = cls._comment_author_field(author_block, "identifier")
            avatar_url = cls._comment_author_field(author_block, "image")
            message = cls._decode_js_string(match.group("text"))
            if not message:
                continue
            likes_match = re.search(
                r'interactionType:"https://schema\.org/LikeAction".*?'
                r'userInteractionCount:(\d+)',
                match.group("stats") or "",
                re.S,
            )
            likes = int(likes_match.group(1)) if likes_match else 0
            username = display_name or screen_name or "未知用户"
            if screen_name and screen_name.lower() != username.lower():
                username = f"{username}(@{screen_name})"
            comments.append(
                {
                    "username": username,
                    "uid": user_id,
                    "likes": likes,
                    "time": cls._format_public_comment_time(
                        cls._decode_js_string(match.group("date"))
                    ),
                    "message": message,
                    "avatar_url": avatar_url,
                    "comment_id": comment_id,
                }
            )
        comments.sort(key=lambda item: int(item.get("likes", 0) or 0), reverse=True)
        return comments[: max(0, int(limit))]

    @classmethod
    def _jsonld_comment_entries(cls, node: Any, sink: List[Dict[str, Any]]) -> None:
        """递归收集 JSON-LD 结构中所有 @type == Comment 的节点。"""
        if isinstance(node, dict):
            node_type = node.get("@type") or node.get("type")
            types = node_type if isinstance(node_type, list) else [node_type]
            if any(str(item or "").lower() == "comment" for item in types):
                sink.append(node)
            for value in node.values():
                cls._jsonld_comment_entries(value, sink)
        elif isinstance(node, list):
            for value in node:
                cls._jsonld_comment_entries(value, sink)

    @staticmethod
    def _jsonld_like_count(node: Any) -> int:
        """从 interactionStatistic 中取出点赞数。"""
        stats = node.get("interactionStatistic") if isinstance(node, dict) else None
        if isinstance(stats, dict):
            stats = [stats]
        if not isinstance(stats, list):
            return 0
        fallback = 0
        for item in stats:
            if not isinstance(item, dict):
                continue
            try:
                count = int(item.get("userInteractionCount") or 0)
            except (TypeError, ValueError):
                continue
            interaction = item.get("interactionType")
            if isinstance(interaction, dict):
                interaction = interaction.get("@type") or interaction.get("name")
            if "like" in str(interaction or "").lower():
                return count
            fallback = max(fallback, 0)
        return fallback

    @staticmethod
    def _jsonld_text_field(node: Any, *keys: str) -> str:
        """从可能是字符串/对象的 JSON-LD 字段中取出文本。"""
        if isinstance(node, str):
            return html_lib.unescape(node).strip()
        if not isinstance(node, dict):
            return ""
        for key in keys:
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return html_lib.unescape(value).strip()
            if isinstance(value, dict):
                nested = TwitterParser._jsonld_text_field(
                    value, "contentUrl", "url", "name", "@id"
                )
                if nested:
                    return nested
        return ""

    @classmethod
    def _parse_jsonld_comments(
        cls,
        html_text: str,
        limit: int,
    ) -> List[Dict[str, Any]]:
        """解析 X 爬虫页 <script type="application/ld+json"> 中的公开回复。

        这是首选策略：JSON-LD 是结构化数据，比正则匹配内联 JS 更稳定，
        X 前端改版时通常不会影响它。
        """
        comments: List[Dict[str, Any]] = []
        seen_ids = set()
        for match in _JSONLD_SCRIPT_RE.finditer(str(html_text or "")):
            payload = html_lib.unescape(match.group(1) or "").strip()
            if not payload:
                continue
            try:
                document = json.loads(payload)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            nodes: List[Dict[str, Any]] = []
            cls._jsonld_comment_entries(document, nodes)
            for node in nodes:
                message = cls._jsonld_text_field(node, "text", "articleBody")
                if not message:
                    continue
                identifier = str(
                    node.get("identifier")
                    or cls._jsonld_text_field(node, "@id", "url")
                    or ""
                ).strip()
                comment_id = identifier.rsplit("/", 1)[-1] if identifier else ""
                dedupe_key = comment_id or message
                if dedupe_key in seen_ids:
                    continue
                seen_ids.add(dedupe_key)
                author = node.get("author")
                display_name = cls._jsonld_text_field(author, "name", "givenName")
                screen_name = cls._jsonld_text_field(
                    author, "alternateName", "additionalName"
                )
                if not screen_name:
                    author_url = cls._jsonld_text_field(author, "url", "@id")
                    if author_url:
                        screen_name = author_url.rstrip("/").rsplit("/", 1)[-1]
                username = display_name or screen_name or "未知用户"
                if screen_name and screen_name.lower() != username.lower():
                    username = f"{username}(@{screen_name})"
                comments.append(
                    {
                        "username": username,
                        "uid": cls._jsonld_text_field(author, "identifier"),
                        "likes": cls._jsonld_like_count(node),
                        "time": cls._format_public_comment_time(
                            cls._jsonld_text_field(
                                node, "datePublished", "dateCreated"
                            )
                        ),
                        "message": message,
                        "avatar_url": cls._jsonld_text_field(author, "image", "thumbnailUrl"),
                        "comment_id": comment_id,
                    }
                )
        comments.sort(key=lambda item: int(item.get("likes", 0) or 0), reverse=True)
        return comments[: max(0, int(limit))]

    @classmethod
    def _extract_public_comments(
        cls,
        html_text: str,
        limit: int,
    ) -> List[Dict[str, Any]]:
        """按 JSON-LD → 内联 JS 的顺序尝试解析公开回复。"""
        comments = cls._parse_jsonld_comments(html_text, limit)
        if comments:
            return comments
        return cls._parse_logged_out_comments_html(html_text, limit)

    @staticmethod
    def _ua_label(user_agent: str) -> str:
        """从爬虫 UA 里取一个短标签，只用于日志可读性。"""
        match = _UA_LABEL_RE.search(str(user_agent or ""))
        return match.group(1) if match else "bot"

    @staticmethod
    def _status_code_of(exc: BaseException) -> Optional[int]:
        """取 aiohttp 异常携带的 HTTP 状态码，非 HTTP 错误返回 None。"""
        status = getattr(exc, "status", None)
        return status if isinstance(status, int) else None

    @staticmethod
    def _screen_name_from_url(url: str) -> str:
        """从帖子链接里取作者 handle（形如 /{screen_name}/status/{id}）。"""
        match = re.search(
            r"(?:twitter\.com|x\.com)/([A-Za-z0-9_]{1,15})/status/\d",
            str(url or ""),
        )
        return match.group(1) if match else ""

    @classmethod
    def _public_page_urls(cls, tweet_id: str, screen_name: str = "") -> List[str]:
        """按可用性从高到低给出公开帖子页地址。

        X 的规范帖子路径是 /{screen_name}/status/{id}，/i/status/{id} 只是一个
        重定向入口，服务端渲染对它的支持并不稳定。已知作者时优先用规范路径，
        再回退到 /i/ 形式。
        """
        urls: List[str] = []
        handle = _HANDLE_UNSAFE_RE.sub("", str(screen_name or ""))[:15]
        if handle and handle.lower() != "i":
            urls.append(f"https://{_PUBLIC_PAGE_HOST}/{handle}/status/{tweet_id}")
        urls.append(f"https://{_PUBLIC_PAGE_HOST}/i/status/{tweet_id}")
        return urls

    async def _fetch_public_page(
        self,
        session: aiohttp.ClientSession,
        url: str,
        proxy: Optional[str],
        user_agent: str = _CRAWLER_USER_AGENTS[0],
        timeout: float = _PUBLIC_PAGE_TIMEOUT,
    ) -> str:
        """抓取一次 X 公开页 HTML，返回正文（失败抛异常）。"""
        async with session.get(
            url,
            headers={
                **self.headers,
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Cache-Control": "no-cache",
            },
            proxy=proxy,
            timeout=aiohttp.ClientTimeout(total=max(3.0, float(timeout))),
            allow_redirects=True,
        ) as response:
            response.raise_for_status()
            return await response.text(errors="replace")

    async def _fetch_hot_comments(
        self,
        session: aiohttp.ClientSession,
        tweet_id: str,
        screen_name: str = "",
    ) -> List[Dict[str, Any]]:
        """从无需登录的 X 帖子页获取当前公开可见的精选回复。

        X 只对爬虫 UA 返回带 JSON-LD 的服务端渲染页面，普通浏览器 UA 拿到的
        是纯 JS 外壳（里面没有任何回复），因此这里只用爬虫 UA。
        尝试顺序：规范路径 → /i/ 路径，每个路径依次换爬虫 UA；若配置了代理但
        解析不走代理，则整轮直连失败后再用代理跑一遍，避免"既没报错也没评论"
        的静默失败。整体受 _HOT_COMMENT_BUDGET 时间预算约束，绝不拖慢正文解析。
        """
        limit = self.hot_comment_count
        if limit <= 0:
            return []

        primary_proxy = self.proxy_url if self.use_parse_proxy else None
        proxies: List[Optional[str]] = [primary_proxy]
        if self.proxy_url and not self.use_parse_proxy:
            # 直连拿不到时用代理兜底：热评抓的是 x.com 本站，
            # 可达性和 FxTwitter 不一样。
            proxies.append(self.proxy_url)

        attempts: List[Tuple[str, str, Optional[str]]] = []
        for proxy in proxies:
            for url in self._public_page_urls(tweet_id, screen_name):
                for user_agent in _CRAWLER_USER_AGENTS:
                    attempts.append((url, user_agent, proxy))

        started = time.monotonic()
        failures: List[str] = []
        # blocked_only 表示"全部失败都是平台明确拒绝"，用于区分平台限制与真故障。
        blocked_only = bool(attempts)
        dead_proxies: set = set()
        served: set = set()

        for url, user_agent, proxy in attempts:
            if proxy in dead_proxies or (url, proxy) in served:
                continue
            remaining = _HOT_COMMENT_BUDGET - (time.monotonic() - started)
            if remaining <= 1.0:
                blocked_only = False
                failures.append("其余来源已跳过: 超出热评抓取时间预算")
                break
            via = "(经代理)" if proxy else ""
            tag = f"{url}{via}[{self._ua_label(user_agent)}]"
            try:
                html_text = await self._fetch_public_page(
                    session,
                    url,
                    proxy,
                    user_agent,
                    min(_PUBLIC_PAGE_TIMEOUT, remaining),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                status = self._status_code_of(exc)
                failures.append(f"{tag} -> {type(exc).__name__}: {exc}")
                if status is None:
                    # 连不上/超时：同一代理下换 UA 或换路径都是白试。
                    blocked_only = False
                    dead_proxies.add(proxy)
                elif status not in _BLOCKED_STATUSES:
                    blocked_only = False
                continue
            # 页面能取回说明这条链路上 UA 没被拒，换 UA 也不会多出回复。
            served.add((url, proxy))
            comments = self._extract_public_comments(html_text, limit)
            if comments:
                logger.debug(
                    f"[{self.name}] 公开回复抓取成功: tweet_id={tweet_id}, "
                    f"count={len(comments)}, source={tag}"
                )
                return comments
            has_jsonld = _JSONLD_MARKER in html_text
            has_comment_node = _COMMENT_NODE_MARKER in html_text
            if has_jsonld or has_comment_node:
                # 页面里确实带回复数据却没解析出来，说明结构变了，要报警。
                blocked_only = False
            failures.append(
                f"{tag} -> 页面已取回但未解析到回复("
                f"len={len(html_text)}, jsonld={has_jsonld}, "
                f"comment_node={has_comment_node})"
            )

        self._log_hot_comment_failure(tweet_id, failures, blocked_only)
        return []

    @staticmethod
    def _is_local_host(base_url: str) -> bool:
        """判断 Nitter 地址是否指向本机/内网，用于决定是否绕过代理。"""
        host = urlparse(str(base_url or "")).hostname or ""
        host = host.strip("[]").lower()
        if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
            return True
        if host.startswith(("10.", "192.168.", "127.")):
            return True
        if host.startswith("172."):
            parts = host.split(".")
            if len(parts) > 1 and parts[1].isdigit() and 16 <= int(parts[1]) <= 31:
                return True
        return False

    def _nitter_proxy(self, base_url: str) -> Optional[str]:
        """自建/内网 Nitter 一律直连；公共实例才跟随解析代理设置。"""
        if not self.proxy_url or not self.use_parse_proxy:
            return None
        if self._is_local_host(base_url):
            return None
        return self.proxy_url

    @staticmethod
    def _session_is_public_only(session: Any) -> bool:
        """判断会话是否装了"只允许连公网地址"的下载防护。"""
        try:
            from ...downloader.security import session_uses_public_only_connector

            return bool(session_uses_public_only_connector(session))
        except Exception:
            # 安全模块缺失或会话形态异常时按"不受限"处理，交由调用方自建会话。
            return False

    @asynccontextmanager
    async def _nitter_session(
        self,
        session: Optional[aiohttp.ClientSession],
    ) -> AsyncIterator[aiohttp.ClientSession]:
        """提供一个能访问自建 Nitter 的会话。

        插件的下载会话带 SSRF 防护，连接前就会拒绝私网地址，而 Nitter 通常
        是用户自己跑在 127.0.0.1/内网的实例，会被这层防护挡掉。Nitter 地址
        来自用户配置、不受第三方响应影响，属于显式信任，因此这里单独开一个
        默认连接器的临时会话；媒体下载仍然走原来的安全会话，SSRF 防护不变。
        """
        if session is not None and not self._session_is_public_only(session):
            yield session
            return
        own_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=_NITTER_BUDGET)
        )
        try:
            yield own_session
        finally:
            await own_session.close()

    async def _fetch_nitter_page(
        self,
        session: aiohttp.ClientSession,
        url: str,
        proxy: Optional[str],
        timeout: float = _NITTER_TIMEOUT,
    ) -> str:
        """抓取一次 Nitter 帖子页 HTML，失败抛异常。"""
        async with session.get(
            url,
            headers={
                "User-Agent": _NITTER_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            proxy=proxy,
            timeout=aiohttp.ClientTimeout(total=max(3.0, float(timeout))),
            allow_redirects=True,
        ) as response:
            response.raise_for_status()
            return await response.text(errors="replace")

    async def _fetch_nitter_extras(
        self,
        session: aiohttp.ClientSession,
        tweet_id: str,
        screen_name: str = "",
    ) -> Dict[str, Any]:
        """依次尝试已配置的 Nitter 实例，返回回复与统计数据。

        Nitter 是 X 的开源前端，未登录即可拿到回复区与统计数字，是当前唯一
        稳定的热评来源（X 自身的 SSR 页面已不再输出 JSON-LD）。
        """
        limit = self.hot_comment_count
        started = time.monotonic()
        failures: List[str] = []
        async with self._nitter_session(session) as fetch_session:
            for base_url in self.nitter_base_urls:
                remaining = _NITTER_BUDGET - (time.monotonic() - started)
                if remaining <= 1.0:
                    failures.append("其余 Nitter 实例已跳过: 超出时间预算")
                    break
                url = nitter_source.thread_url(base_url, tweet_id, screen_name)
                try:
                    html_text = await self._fetch_nitter_page(
                        fetch_session,
                        url,
                        self._nitter_proxy(base_url),
                        min(_NITTER_TIMEOUT, remaining),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failures.append(f"{url} -> {type(exc).__name__}: {exc}")
                    continue
                parsed = nitter_source.parse_thread(html_text, limit, base_url)
                if parsed.get("comments") or parsed.get("stats_line"):
                    logger.debug(
                        f"[{self.name}] Nitter 抓取成功: tweet_id={tweet_id}, "
                        f"comments={len(parsed.get('comments') or [])}, "
                        f"stats={parsed.get('stats_line') or '-'}, source={url}"
                    )
                    return parsed
                failures.append(
                    f"{url} -> 页面已取回但未解析到内容(len={len(html_text)})"
                )
        if failures:
            logger.warning(
                f"[{self.name}] Nitter 未返回可用内容: tweet_id={tweet_id}; "
                + "; ".join(failures)
            )
        return {}

    def _log_nitter_hint(self) -> None:
        """未配置 Nitter 时，按冷却期提示一次可行方案。"""
        now = time.monotonic()
        if now - TwitterParser._nitter_hint_at < _NITTER_HINT_INTERVAL:
            return
        TwitterParser._nitter_hint_at = now
        logger.info(
            f"[{self.name}] X 已停止对未登录访问输出服务端渲染的回复数据，"
            f"因此热评通常抓不到。可在配置项"
            f"「附加内容：热评 → Nitter 实例地址」填入一个可用的 Nitter "
            f"（例如自建的 http://127.0.0.1:8585），即可恢复热评并补全"
            f"点赞/转发/阅读等统计数字。（1 小时内不再重复提示）"
        )

    async def _collect_thread_extras(
        self,
        session: aiohttp.ClientSession,
        tweet_id: str,
        screen_name: str = "",
    ) -> Dict[str, Any]:
        """汇总热评与统计数字：Nitter 优先，X 公开页兜底。"""
        extras: Dict[str, Any] = {
            "comments": [],
            "stats_line": "",
            "author_avatar": "",
        }
        if self.nitter_base_urls:
            parsed = await self._fetch_nitter_extras(session, tweet_id, screen_name)
            extras["comments"] = list(parsed.get("comments") or [])
            extras["stats_line"] = str(parsed.get("stats_line") or "")
            extras["author_avatar"] = str(parsed.get("author_avatar") or "")
            if parsed:
                # Nitter 已经给出可用结果（哪怕这条推文本来就没有回复）。
                # X 公开页早已不再对未登录访问输出回复数据，再兜底只会白等
                # 满一个 _HOT_COMMENT_BUDGET，把整条解析拖慢十几秒。
                return extras
        if extras["comments"] or self.hot_comment_count <= 0:
            return extras
        if not self.nitter_base_urls:
            self._log_nitter_hint()
        extras["comments"] = await self._fetch_hot_comments(
            session, tweet_id, screen_name
        )
        return extras

    def _log_hot_comment_failure(
        self,
        tweet_id: str,
        failures: List[str],
        blocked_only: bool,
    ) -> None:
        """按失败性质分级输出日志。

        平台明确拒绝(401/403/404/429)、或页面能取回但压根不含回复区，都是
        "X 关掉了未登录访问"，不是插件故障，每条推文都刷 WARN 只会淹没真正的
        问题；这类情况详情降到 DEBUG，另按冷却期给一条 INFO 说明。其余失败
        （网络不通、超时、页面带回复数据却解析不出来）仍然 WARN。
        """
        if not failures:
            return
        detail = f"tweet_id={tweet_id}; " + "; ".join(failures)
        if not blocked_only:
            logger.warning(f"[{self.name}] 未能获取公开热评: {detail}")
            return
        logger.debug(f"[{self.name}] 公开热评被平台拒绝: {detail}")
        now = time.monotonic()
        if now - TwitterParser._blocked_notice_at < _BLOCKED_NOTICE_INTERVAL:
            return
        TwitterParser._blocked_notice_at = now
        logger.info(
            f"[{self.name}] X 未向未登录访问提供回复数据"
            f"(拒绝访问或页面不含回复区)，"
            f"推文将不带热评展示；正文/图片/视频解析不受影响。"
            f"（{int(_BLOCKED_NOTICE_INTERVAL // 60)} 分钟内不再重复提示）"
        )

    def can_parse(self, url: str) -> bool:
        """判断是否可以解析此URL

        Args:
            url: 视频链接

        Returns:
            是否可以解析
        """
        if not url:
            logger.debug(f"[{self.name}] can_parse: URL为空")
            return False
        try:
            parsed = urlparse(url)
        except (TypeError, ValueError):
            return False
        host = (parsed.hostname or "").lower().rstrip(".")
        trusted_host = any(
            host == domain or host.endswith(f".{domain}")
            for domain in ("twitter.com", "x.com")
        )
        if (
            parsed.scheme.lower() in {"http", "https"}
            and trusted_host
            and re.search(r"/status/(\d+)", parsed.path or "")
        ):
            logger.debug(f"[{self.name}] can_parse: 匹配Twitter链接 {url}")
            return True
        logger.debug(f"[{self.name}] can_parse: 无法解析 {url}")
        return False

    def extract_links(self, text: str) -> List[str]:
        """从文本中提取Twitter链接

        Args:
            text: 输入文本

        Returns:
            Twitter链接列表
        """
        result_links = []
        seen_ids = set()
        pattern = (
            r"https?://(?:(?:www|mobile)\.)?(?:twitter\.com|x\.com)/"
            r'[^\s<>"\'(),，。！？；：、]*?status/(\d+)[^\s<>"\'(),，。！？；：、]*'
        )
        matches = re.finditer(pattern, text, re.IGNORECASE)
        for match in matches:
            tweet_id = match.group(1)
            if tweet_id not in seen_ids:
                seen_ids.add(tweet_id)
                result_links.append(match.group(0).rstrip(".,!?;:，。！？；：、"))
        result = result_links
        if result:
            logger.debug(
                f"[{self.name}] extract_links: 提取到 {len(result)} 个链接: {result[:3]}{'...' if len(result) > 3 else ''}"
            )
        else:
            logger.debug(f"[{self.name}] extract_links: 未提取到链接")
        return result

    def _parse_fxtwitter_response(
        self,
        data: Dict[str, Any],
        expected_tweet_id: str = "",
    ) -> Dict[str, Any]:
        """从 FxTwitter 响应中提取统一媒体结构。"""
        if not isinstance(data, dict) or "tweet" not in data:
            raise FxTwitterTweetUnavailableError("FxTwitter响应缺少tweet字段")

        tweet = data.get("tweet") or {}
        if expected_tweet_id:
            actual_id = str(tweet.get("id") or tweet.get("id_str") or "").strip()
            if not actual_id:
                url_match = re.search(r"/status/(\d+)", str(tweet.get("url") or ""))
                actual_id = url_match.group(1) if url_match else ""
            if actual_id != str(expected_tweet_id):
                raise FxTwitterTweetUnavailableError("FxTwitter响应不是请求的目标推文")
        tweet_text = self._twitter_text(tweet)
        author_info = tweet.get("author", {})
        author = self._fxtwitter_author(author_info)
        avatar_url = self._extract_avatar_url(author_info)
        timestamp = self._parse_twitter_date(tweet.get('created_at'))
        quote = self._extract_fxtwitter_quote(tweet.get('quote'))
        desc = self._build_tweet_desc(tweet_text, quote)

        media_urls = {
            'images': [],
            'videos': [],
            # 普通推文没有独立标题，作者信息已经由 author 字段展示。
            'title': "",
            'text': desc,
            'author': self._combine_parenthetical(
                author,
                quote.get("author", "")
            ),
            'avatar_url': avatar_url,
            'timestamp': self._combine_parenthetical(
                timestamp,
                quote.get("timestamp", "")
            ),
        }

        media = tweet.get("media") or {}
        for photo in media.get("photos") or []:
            if isinstance(photo, dict) and photo.get("url"):
                media_urls["images"].append(photo.get("url"))
        for video in media.get("videos") or []:
            if isinstance(video, dict) and video.get("url"):
                media_urls["videos"].append(
                    {
                        "url": video.get("url", ""),
                        "thumbnail": video.get("thumbnail_url", ""),
                        "duration": video.get("duration", 0),
                    }
                )
        return media_urls

    @staticmethod
    def _twitter_text(tweet: Dict[str, Any]) -> str:
        """提取推文文本，优先使用 raw_text。"""
        if not isinstance(tweet, dict):
            return ""
        raw_text = tweet.get("raw_text")
        if isinstance(raw_text, dict):
            text = raw_text.get("text")
            if text:
                return TwitterParser._unescape_entities(
                    TwitterParser._apply_display_text_range(
                        str(text), raw_text.get("display_text_range")
                    )
                )
        return TwitterParser._unescape_entities(tweet.get("text", ""))

    @staticmethod
    def _fxtwitter_author(author_info: Dict[str, Any]) -> str:
        """格式化 FxTwitter 作者信息。"""
        if not isinstance(author_info, dict):
            return ""
        author_name = author_info.get("name", "")
        author_username = author_info.get("screen_name", "")
        if author_name and author_username:
            return f"{author_name}(@{author_username})"
        return author_name or author_username

    @staticmethod
    def _apply_display_text_range(text: str, display_range: Any) -> str:
        """按 Twitter display_text_range 裁剪正文，去掉回复前缀等非正文内容。"""
        if not text or not isinstance(display_range, list) or len(display_range) != 2:
            return text
        try:
            start = max(0, int(display_range[0]))
            end = max(start, int(display_range[1]))
            return text[start:end].strip()
        except (TypeError, ValueError):
            return text

    @staticmethod
    def _parse_twitter_date(created_at: Any) -> str:
        """将 Twitter created_at 转为 YYYY-MM-DD。"""
        if not created_at:
            return ""
        try:
            dt = datetime.strptime(str(created_at), '%a %b %d %H:%M:%S %z %Y')
            return dt.strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            return str(created_at)

    def _extract_fxtwitter_quote(self, quote: Any) -> Dict[str, str]:
        """提取 FxTwitter 引用推文信息，供既有 metadata 字段合并使用。"""
        if not isinstance(quote, dict):
            return {}
        quote_text = self._twitter_text(quote)
        if not quote_text:
            return {}
        return {
            "text": quote_text,
            "author": self._fxtwitter_author(quote.get("author") or {}),
            "timestamp": self._parse_twitter_date(quote.get("created_at")),
            "reply_to": str(quote.get("replying_to") or "").strip(),
        }

    async def _fetch_fxtwitter_info(
        self,
        session: aiohttp.ClientSession,
        tweet_id: str,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ) -> Dict[str, Any]:
        """使用FxTwitter API获取推特媒体直链（带重试机制）

        Args:
            session: aiohttp会话
            tweet_id: 推文ID
            max_retries: 最大重试次数，默认3次
            retry_delay: 重试延迟（秒），默认1秒，使用指数退避

        Returns:
            包含images和videos的字典。
        """
        api_url = f"https://api.fxtwitter.com/status/{tweet_id}"
        last_exception = None

        proxy = self.proxy_url if self.use_parse_proxy else None
        for attempt in range(max_retries + 1):
            try:
                async with session.get(
                    api_url,
                    headers=self.headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                    proxy=proxy,
                ) as response:
                    response.raise_for_status()
                    data = await response.json()
                    return self._parse_fxtwitter_response(data, tweet_id)
            except aiohttp.ClientResponseError as e:
                if e.status < 500:
                    raise FxTwitterTweetUnavailableError(
                        f"HTTP {e.status} {e.message}"
                    ) from e
                last_exception = e
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                aiohttp.ServerTimeoutError,
            ) as e:
                last_exception = e
            except FxTwitterTweetUnavailableError:
                raise
            except Exception as e:
                raise FxTwitterTweetUnavailableError(str(e)) from e

            if attempt < max_retries:
                delay = retry_delay * (2**attempt)
                await asyncio.sleep(delay)
            else:
                error_msg = str(last_exception) if last_exception else "未知错误"
                raise FxTwitterServiceUnavailableError(
                    f"{error_msg}（已重试{max_retries}次）"
                )

    @staticmethod
    def _best_video_variant(media: Dict[str, Any]) -> Optional[str]:
        """按 bitrate 选择最佳 mp4 变体。"""
        video_info = media.get("video_info") or {}
        variants = video_info.get("variants") or []
        candidates = []
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            url = variant.get("url") or ""
            if ".mp4" not in url.lower():
                continue
            try:
                bitrate = int(variant.get("bitrate") or 0)
            except (TypeError, ValueError):
                bitrate = 0
            candidates.append((bitrate, url))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    @staticmethod
    def _build_img_url(media: Dict[str, Any]) -> Optional[str]:
        """构造原图 URL。"""
        media_url = media.get("media_url_https") or media.get("media_url")
        if not media_url:
            return None
        if "?" in media_url:
            return f"{media_url}&name=orig"
        return f"{media_url}?name=orig"

    async def _fetch_guest_token(self, session: aiohttp.ClientSession) -> str:
        """获取 Twitter guest token。"""
        bearer = (
            "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOjj6tT7UeCs"
            "TnIU3U%3D0owR4rQG2v0nE"
        )
        headers = {
            **self.headers,
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        proxy = self.proxy_url if self.use_parse_proxy else None
        async with session.post(
            "https://api.twitter.com/1.1/guest/activate.json",
            headers=headers,
            proxy=proxy,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
        token = str(data.get("guest_token") or "").strip()
        if not token:
            raise RuntimeError("Twitter guest token为空")
        return token

    @staticmethod
    def _walk_dicts(obj: Any):
        """深度遍历 dict/list。"""
        if isinstance(obj, dict):
            yield obj
            for value in obj.values():
                yield from TwitterParser._walk_dicts(value)
        elif isinstance(obj, list):
            for value in obj:
                yield from TwitterParser._walk_dicts(value)

    def _parse_graphql_response(
        self, data: Dict[str, Any], tweet_id: str
    ) -> Dict[str, Any]:
        """从 Guest GraphQL 响应中提取媒体。"""
        tweet = None
        for candidate in self._walk_dicts(data):
            legacy = candidate.get("legacy")
            if not isinstance(legacy, dict):
                continue
            rest_id = str(candidate.get("rest_id") or legacy.get("id_str") or "")
            if rest_id == tweet_id:
                tweet = candidate
                break
        if not tweet:
            raise RuntimeError("Twitter GraphQL响应中未找到tweet")

        legacy = tweet.get("legacy") or {}
        author = self._graphql_author(tweet)
        user_core = tweet.get("core") or {}
        user_result = (
            ((user_core.get("user_results") or {}).get("result") or {})
            if isinstance(user_core, dict) else {}
        )
        avatar_url = self._extract_avatar_url(user_result)

        timestamp = ""
        created_at = legacy.get("created_at")
        if created_at:
            try:
                dt = datetime.strptime(created_at, '%a %b %d %H:%M:%S %z %Y')
                timestamp = dt.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                timestamp = str(created_at)

        text = self._graphql_tweet_text(tweet)
        quote = self._extract_graphql_quote(data, legacy)
        desc = self._build_tweet_desc(text, quote)

        images: List[str] = []
        videos: List[Dict[str, Any]] = []
        media_items = (legacy.get("extended_entities") or {}).get("media") or []
        for media in media_items:
            if not isinstance(media, dict):
                continue
            media_type = media.get("type")
            if media_type == "photo":
                img_url = self._build_img_url(media)
                if img_url:
                    images.append(img_url)
            elif media_type in ("video", "animated_gif"):
                video_url = self._best_video_variant(media)
                if video_url:
                    videos.append({"url": video_url})

        return {
            "images": images,
            "videos": videos,
            "title": "",
            "text": desc,
            "author": self._combine_parenthetical(
                author,
                quote.get("author", "")
            ),
            "avatar_url": avatar_url,
            "timestamp": self._combine_parenthetical(
                timestamp, quote.get("timestamp", "")
            ),
        }

    def _graphql_author(self, tweet: Dict[str, Any]) -> str:
        """从 GraphQL tweet 节点提取作者。"""
        user_core = tweet.get("core") or {}
        user_result = (
            ((user_core.get("user_results") or {}).get("result") or {})
            if isinstance(user_core, dict)
            else {}
        )
        user_legacy = user_result.get("legacy") or {}
        name = user_legacy.get("name") or ""
        screen_name = user_legacy.get("screen_name") or ""
        return (
            f"{name}(@{screen_name})" if name and screen_name else (name or screen_name)
        )

    @staticmethod
    def _graphql_tweet_text(tweet: Dict[str, Any]) -> str:
        """从 GraphQL tweet 节点提取完整文本。"""
        legacy = tweet.get("legacy") or {}
        note_tweet = (
            (tweet.get("note_tweet") or {}).get("note_tweet_results") or {}
        ).get("result") or {}
        if isinstance(note_tweet, dict) and note_tweet.get("text"):
            return TwitterParser._unescape_entities(note_tweet.get("text"))
        text = str(legacy.get("full_text") or "")
        return TwitterParser._unescape_entities(
            TwitterParser._apply_display_text_range(
                text, legacy.get("display_text_range")
            )
        )

    def _extract_graphql_quote(
        self, data: Dict[str, Any], legacy: Dict[str, Any]
    ) -> Dict[str, str]:
        """从 GraphQL 响应中提取引用推文信息。"""
        quote_id = str(
            legacy.get("quoted_status_id_str") or legacy.get("quoted_status_id") or ""
        )
        if not quote_id:
            return {}
        for candidate in self._walk_dicts(data):
            candidate_legacy = candidate.get("legacy")
            if not isinstance(candidate_legacy, dict):
                continue
            rest_id = str(
                candidate.get("rest_id") or candidate_legacy.get("id_str") or ""
            )
            if rest_id != quote_id:
                continue
            quote_text = self._graphql_tweet_text(candidate)
            if not quote_text:
                return {}
            return {
                "text": quote_text,
                "author": self._graphql_author(candidate),
                "timestamp": self._parse_twitter_date(
                    candidate_legacy.get("created_at")
                ),
                "reply_to": str(
                    candidate_legacy.get("in_reply_to_screen_name") or ""
                ).strip(),
            }
        return {}

    @staticmethod
    def _combine_parenthetical(primary: str, secondary: str) -> str:
        """按 B 站转发动态风格合并主/被引用字段。"""
        primary = str(primary or "").strip()
        secondary = str(secondary or "").strip()
        if primary and secondary:
            return f"{primary} ({secondary})"
        return primary or secondary

    @staticmethod
    def _build_tweet_desc(text: str, quote: Dict[str, str]) -> str:
        """将主推文和引用推文合并到 desc，避免新增展示字段。"""
        desc = str(text or "").strip()
        if not isinstance(quote, dict) or not quote.get("text"):
            return desc

        quote_parts = ["引用推文："]
        quote_author = str(quote.get("author") or "").strip()
        quote_reply_to = str(quote.get("reply_to") or "").strip()
        quote_text = str(quote.get("text") or "").strip()
        if quote_author:
            quote_parts.append(quote_author)
        if quote_reply_to:
            quote_parts.append(f"回复 @{quote_reply_to}")
        quote_parts.append(quote_text)

        quote_desc = "\n".join(quote_parts)
        if desc:
            return f"{desc}\n\n{quote_desc}"
        return quote_desc

    async def _fetch_graphql_info(
        self, session: aiohttp.ClientSession, tweet_id: str
    ) -> Dict[str, Any]:
        """使用 Twitter Guest GraphQL 回退解析。"""
        bearer = (
            "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOjj6tT7UeCs"
            "TnIU3U%3D0owR4rQG2v0nE"
        )
        guest_token = await self._fetch_guest_token(session)
        variables = {
            "tweetId": tweet_id,
            "withCommunity": False,
            "includePromotedContent": False,
            "withVoice": False,
        }
        features = {
            "creator_subscriptions_tweet_preview_api_enabled": True,
            "tweetypie_unmention_optimization_enabled": True,
            "responsive_web_edit_tweet_api_enabled": True,
            "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
            "view_counts_everywhere_api_enabled": True,
            "longform_notetweets_consumption_enabled": True,
            "responsive_web_twitter_article_tweet_consumption_enabled": True,
            "tweet_awards_web_tipping_enabled": False,
            "freedom_of_speech_not_reach_fetch_enabled": True,
            "standardized_nudges_misinfo": True,
            "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
            "rweb_video_timestamps_enabled": True,
            "longform_notetweets_rich_text_read_enabled": True,
            "longform_notetweets_inline_media_enabled": True,
            "responsive_web_graphql_exclude_directive_enabled": True,
            "verified_phone_label_enabled": False,
            "responsive_web_media_download_video_enabled": False,
            "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
            "responsive_web_graphql_timeline_navigation_enabled": True,
        }
        endpoint = (
            "https://twitter.com/i/api/graphql/"
            "0hWvDhmW8YQ-S_ib3azIrw/TweetResultByRestId"
        )
        headers = {
            **self.headers,
            "Authorization": f"Bearer {bearer}",
            "x-guest-token": guest_token,
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
            "Referer": "https://twitter.com/",
        }
        proxy = self.proxy_url if self.use_parse_proxy else None
        async with session.get(
            endpoint,
            headers=headers,
            params={
                "variables": json_dumps_compact(variables),
                "features": json_dumps_compact(features),
            },
            proxy=proxy,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
        return self._parse_graphql_response(data, tweet_id)

    async def _fetch_media_info(
        self, session: aiohttp.ClientSession, tweet_id: str
    ) -> Dict[str, Any]:
        """优先 FxTwitter；仅服务不可达/服务端错误时回退 Guest GraphQL。"""
        try:
            return await self._fetch_fxtwitter_info(session, tweet_id)
        except FxTwitterServiceUnavailableError as e:
            logger.warning(f"FxTwitter不可用，尝试GraphQL回退: {e}")
            return await self._fetch_graphql_info(session, tweet_id)

    async def parse(
        self, session: aiohttp.ClientSession, url: str
    ) -> Optional[Dict[str, Any]]:
        """解析单个Twitter链接

        Args:
            session: aiohttp会话
            url: Twitter链接

        Returns:
            解析结果字典，包含标准化的元数据格式

        Raises:
            RuntimeError: 当解析失败时
        """
        async with self.semaphore:
            tweet_id_match = re.search(r"/status/(\d+)", url)
            if not tweet_id_match:
                raise RuntimeError(f"无法解析此URL: {url}")
            tweet_id = tweet_id_match.group(1)
            # 链接里通常已带作者 handle，热评抓取要用它拼规范路径。
            screen_name = self._screen_name_from_url(url)
            # Nitter 即使不取热评也能补全统计数字，因此两个条件任一成立就并发跑。
            extras_task = None
            if session is not None and (
                self.hot_comment_count > 0 or self.nitter_base_urls
            ):
                extras_task = asyncio.create_task(
                    self._collect_thread_extras(session, tweet_id, screen_name)
                )
            try:
                media_info = await self._fetch_media_info(session, tweet_id)
            except (asyncio.CancelledError, Exception):
                if extras_task is not None:
                    extras_task.cancel()
                    await asyncio.gather(extras_task, return_exceptions=True)
                raise

            hot_comments: List[Dict[str, Any]] = []
            stats_line = ""
            nitter_avatar = ""
            if extras_task is not None:
                try:
                    extras = await extras_task
                    hot_comments = list(extras.get("comments") or [])
                    stats_line = str(extras.get("stats_line") or "")
                    nitter_avatar = str(extras.get("author_avatar") or "")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        f"[{self.name}] 获取公开回复失败，已跳过: "
                        f"tweet_id={tweet_id}, 错误: {exc}"
                    )

            images = media_info.get("images", [])
            videos = media_info.get("videos", [])
            text = media_info.get("text", "")
            title = media_info.get("title", "")
            author = media_info.get("author", "")
            timestamp = media_info.get("timestamp", "")

            video_urls = []
            video_cover_urls = []
            image_urls = []

            for video_info in videos:
                if isinstance(video_info, str):
                    video_url = video_info
                    thumbnail = ""
                elif isinstance(video_info, dict):
                    video_url = video_info.get("url")
                    thumbnail = video_info.get("thumbnail")
                else:
                    continue
                if video_url:
                    video_urls.append(video_url)
                    video_cover_urls.append([thumbnail] if thumbnail else [])

            image_urls = [img for img in images if img]

            has_videos = len(video_urls) > 0
            has_images = len(image_urls) > 0
            has_text = bool(str(text or "").strip())

            if not has_videos and not has_images and not has_text:
                raise RuntimeError("推文不包含文本、图片或视频")

            image_headers = build_request_headers(is_video=False)
            video_headers = build_request_headers(is_video=True)

            metadata_base = {
                "url": url,
                "title": str(title or "").strip(),
                "author": author,
                # FxTwitter/GraphQL 拿不到头像时，用 Nitter 还原的 CDN 直链兜底。
                "avatar_url": str(media_info.get("avatar_url") or "") or nitter_avatar,
                "desc": text,
                "timestamp": timestamp,
                "image_headers": image_headers,
                "video_headers": video_headers,
                "use_image_proxy": self.use_image_proxy,
                "use_video_proxy": self.use_video_proxy,
                "proxy_url": self.proxy_url
                if (self.use_image_proxy or self.use_video_proxy)
                else None,
            }
            if hot_comments:
                metadata_base["hot_comments"] = hot_comments
            if stats_line:
                metadata_base["stats_line"] = stats_line

            if has_videos and has_images:
                result_dict = {
                    **metadata_base,
                    "video_urls": self._add_range_prefix_to_video_urls(
                        [[url] for url in video_urls]
                    ),
                    "video_cover_urls": video_cover_urls,
                    "image_urls": [[url] for url in image_urls],
                    "is_twitter_video": True,
                    "video_force_download": True,
                }
                logger.debug(
                    f"[{self.name}] parse: 解析完成(视频+图片) {url}, video_count={len(video_urls)}, image_count={len(image_urls)}"
                )
                return result_dict
            elif has_videos:
                result_dict = {
                    **metadata_base,
                    "video_urls": self._add_range_prefix_to_video_urls(
                        [[url] for url in video_urls]
                    ),
                    "video_cover_urls": video_cover_urls,
                    "image_urls": [],
                    "is_twitter_video": True,
                    "video_force_download": True,
                }
                logger.debug(
                    f"[{self.name}] parse: 解析完成(视频) {url}, video_count={len(video_urls)}"
                )
                return result_dict
            else:
                result_dict = {
                    **metadata_base,
                    "video_urls": [],
                    "video_cover_urls": [],
                    "image_urls": [[url] for url in image_urls],
                    "is_twitter_video": False,
                }
                if image_urls:
                    logger.debug(
                        f"[{self.name}] parse: 解析完成(图片) {url}, image_count={len(image_urls)}"
                    )
                else:
                    logger.debug(f"[{self.name}] parse: 解析完成(纯文本) {url}")
                return result_dict
