"""YouTube 登录态运行时：Cookie 轮换吸收、持久化与保鲜。

YouTube / Google 的登录 Cookie 不是一张长期不变的令牌，而是一组会被服
务端持续轮换的凭据：`__Secure-1PSIDTS` / `__Secure-3PSIDTS` / `SIDCC`
这几项每隔一段时间就会通过 `Set-Cookie` 下发新值，浏览器静默跟进，所以
用户自己刷 YouTube 永远不用重新登录。

插件如果只把配置里那份静态字符串一直原样发出去，就等于一个永远不更新
凭据的浏览器，服务端迟早判定会话过期——这才是「YouTube Cookie 很容易失
效」的真正原因，而不是 Cookie 本身写了个短过期时间。

本运行时因此做三件事，让一份手工导出的 Cookie 可以长期不用再管：

1. 吸收：把每次 YouTube 响应里的 `Set-Cookie` 合并回内存 Cookie 罐；
2. 持久化：合并结果原子落盘，插件重载/重启后接着用轮换后的新值；
3. 保鲜：定期向 youtube.com 发一次带登录态的轻量请求主动触发轮换，
   让长期没有解析请求的实例也不会把 Cookie 放到腐烂。

安全约定：运行时文件权限收敛到 0600，只存 Cookie 名值，不写任何日志明
文；日志里一律只出现 Cookie 名，绝不输出取值。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
from http.cookies import CookieError, SimpleCookie
from typing import Any, Dict, Iterable, List, Optional, Tuple

import aiohttp

from ....logger import logger


__all__ = [
    "IDENTITY_COOKIE_NAMES",
    "ROTATING_COOKIE_NAMES",
    "SAPISID_COOKIE_NAMES",
    "YOUTUBE_ORIGIN",
    "YouTubeCookieRuntime",
    "build_sapisid_authorization",
    "collect_set_cookie_headers",
    "normalize_cookie_input",
    "parse_cookie_header",
]


YOUTUBE_ORIGIN = "https://www.youtube.com"

_KEEPALIVE_URL = "https://www.youtube.com/account"
_KEEPALIVE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# SAPISIDHASH 鉴权用到的 cookie 名，按优先级排列。
SAPISID_COOKIE_NAMES: Tuple[str, ...] = (
    "SAPISID",
    "__Secure-3PAPISID",
    "__Secure-1PAPISID",
)

# 账号身份类 Cookie：决定「你是谁」，正常情况下极少变动，但 Google 偶尔
# 也会轮换，跟进比死守更安全。
IDENTITY_COOKIE_NAMES: Tuple[str, ...] = (
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "__Secure-1PAPISID",
    "__Secure-3PAPISID",
    "LOGIN_INFO",
)

# 高频轮换类 Cookie：这些才是 Cookie 腐烂的主因，必须跟着服务端走。
ROTATING_COOKIE_NAMES: Tuple[str, ...] = (
    "SIDCC",
    "__Secure-1PSIDCC",
    "__Secure-3PSIDCC",
    "__Secure-1PSIDTS",
    "__Secure-3PSIDTS",
    "VISITOR_INFO1_LIVE",
    "VISITOR_PRIVACY_METADATA",
    "YSC",
    "PREF",
    "SOCS",
    "CONSENT",
    "__Secure-YEC",
    "__Secure-ROLLOUT_TOKEN",
)

_ACCEPTED_COOKIE_NAMES = frozenset(IDENTITY_COOKIE_NAMES + ROTATING_COOKIE_NAMES)

# 服务端删除 Cookie 时惯用的占位值；照抄进罐子等于自己把登录态清掉。
_HTTPONLY_PREFIX = "#HttpOnly_"

_DELETION_VALUES = frozenset(
    ("", "EXPIRED", "DELETED", "expired", "deleted", "null", "undefined")
)

_LOGGED_IN_PATTERNS = (
    re.compile(r'"LOGGED_IN"\s*:\s*(true|false)'),
    re.compile(r'"logged_in"\s*:\s*"?(1|0|true|false)"?'),
    re.compile(r'"loggedIn"\s*:\s*(true|false)'),
)
_LOGGED_IN_TRUE = frozenset(("true", "1"))


# ── Cookie 基础操作 ──────────────────────────────────────

def parse_cookie_header(cookie: str) -> Dict[str, str]:
    """把 "a=1; b=2" 形式的 Cookie 头切成字典（保留大小写与顺序）。"""
    jar: Dict[str, str] = {}
    for chunk in (cookie or "").split(";"):
        name, sep, value = chunk.partition("=")
        name = name.strip()
        if not name or not sep:
            continue
        jar[name] = value.strip()
    return jar


def _cookies_from_netscape(text: str) -> Dict[str, str]:
    """解析 cookies.txt（Netscape 格式）文本，取出 name/value。"""
    jar: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            # "#HttpOnly_" 是合法数据行的前缀，其余 # 开头的都是注释。
            if not line.startswith(_HTTPONLY_PREFIX):
                continue
            line = line[len(_HTTPONLY_PREFIX):]
        fields = line.split("\t")
        if len(fields) < 6:
            # 有些编辑器会把制表符换成空格，退回按空白切分。
            fields = line.split()
        if len(fields) < 6:
            continue
        name = fields[5].strip()
        if not name:
            continue
        jar[name] = fields[6].strip() if len(fields) > 6 else ""
    return jar


def _cookies_from_json(text: str) -> Dict[str, str]:
    """解析 Cookie-Editor / EditThisCookie 这类扩展导出的 JSON。"""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {}
    if isinstance(data, dict):
        nested = data.get("cookies")
        if isinstance(nested, (list, dict)):
            data = nested
    if isinstance(data, dict):
        jar: Dict[str, str] = {}
        for key, value in data.items():
            name = str(key).strip()
            if name and not isinstance(value, (list, dict)):
                jar[name] = str(value if value is not None else "")
        return jar
    if not isinstance(data, list):
        return {}
    jar = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "").strip()
        if not name:
            continue
        value = item.get("value", "")
        jar[name] = str(value if value is not None else "")
    return jar


def normalize_cookie_input(raw: str) -> str:
    """把用户可能填进配置的各种 Cookie 形态统一成 Cookie 请求头。

    浏览器扩展导出的东西五花八门：`Get cookies.txt LOCALLY` 给的是
    Netscape 格式的 cookies.txt，`Cookie-Editor` 默认给 JSON 数组，只有少
    数扩展直接给 "a=1; b=2" 的请求头。此前插件只认最后一种，粘错格式会
    静默退回匿名请求（日志里只有一句找不到 SAPISID），排查成本很高。

    现在三种格式都收，统一转成请求头字符串；已经是请求头的原样返回（只
    折叠掉粘贴时带进来的换行与多余空白），保证不会破坏既有配置。
    """
    text = (raw or "").strip().lstrip("\ufeff").strip()
    if not text:
        return ""
    jar: Dict[str, str] = {}
    if text[:1] in ("[", "{"):
        jar = _cookies_from_json(text)
    if not jar and ("\t" in text or _HTTPONLY_PREFIX in text or "# Netscape" in text):
        jar = _cookies_from_netscape(text)
    if not jar and "\n" in text and "=" not in text.split("\n", 1)[0]:
        jar = _cookies_from_netscape(text)
    if jar:
        return "; ".join(f"{name}={value}" for name, value in jar.items())
    return " ".join(text.split())


def build_sapisid_authorization(
    cookie: str,
    origin: str = YOUTUBE_ORIGIN,
    timestamp: Optional[int] = None,
) -> str:
    """
    由 cookie 里的 SAPISID 算出 Innertube 需要的 Authorization 头。

    只把 Cookie 头丢给 Innertube 是**无效**的（服务端会当匿名请求处理），
    必须额外带 Authorization: SAPISIDHASH <ts>_<sha1(ts SAPISID origin)>
    才算真正登录。取不到 SAPISID 时返回空串，调用方据此退回匿名请求。
    """
    jar = parse_cookie_header(cookie)
    sapisid = ""
    for name in SAPISID_COOKIE_NAMES:
        if jar.get(name):
            sapisid = jar[name]
            break
    if not sapisid:
        return ""
    stamp = int(timestamp if timestamp is not None else time.time())
    digest = hashlib.sha1(
        f"{stamp} {sapisid} {origin}".encode("utf-8")
    ).hexdigest()
    return f"SAPISIDHASH {stamp}_{digest}"


def collect_set_cookie_headers(response: Any) -> List[str]:
    """把响应里的所有 Set-Cookie 原始行取出来（兼容单值 headers 实现）。"""
    headers = getattr(response, "headers", None)
    if headers is None:
        return []
    getall = getattr(headers, "getall", None)
    if callable(getall):
        try:
            return [str(item) for item in getall("Set-Cookie", [])]
        except (KeyError, TypeError):
            return []
    try:
        single = headers.get("Set-Cookie", "")
    except (AttributeError, TypeError):
        return []
    return [str(single)] if single else []


def _is_deletion(morsel: Any) -> bool:
    """判断一条 Set-Cookie 是否在删除该 Cookie 而不是给出新值。"""
    value = str(getattr(morsel, "value", "") or "").strip()
    if value in _DELETION_VALUES:
        return True
    max_age = str(morsel.get("max-age", "") or "").strip()
    if max_age:
        try:
            if int(max_age) <= 0:
                return True
        except ValueError:
            pass
    return False


def _detect_logged_in(html: str) -> Optional[bool]:
    """从 YouTube 页面里读出服务端认定的登录态；读不出返回 None。"""
    for pattern in _LOGGED_IN_PATTERNS:
        match = pattern.search(html or "")
        if match:
            return match.group(1).lower() in _LOGGED_IN_TRUE
    return None


class YouTubeCookieRuntime:
    """管理一份 YouTube Cookie 的生命周期：吸收轮换、落盘、定期保鲜。"""

    def __init__(
        self,
        configured_cookie: str = "",
        state_path: str = "",
        auto_refresh: bool = True,
    ):
        """用配置里的 Cookie 初始化，并尽量接续上次落盘的轮换结果。"""
        self._configured = (configured_cookie or "").strip()
        self._fingerprint = self._make_fingerprint(self._configured)
        self.state_path = (state_path or "").strip()
        self.auto_refresh = bool(auto_refresh)

        self._jar: Dict[str, str] = parse_cookie_header(self._configured)
        # 未发生任何轮换前原样回放配置字符串，避免重新序列化改变字节形态。
        self._mutated = False
        self._dirty = False
        self._lock = asyncio.Lock()
        self._revision = 0
        self._last_rotation_at: float = 0.0
        self._last_keepalive_at: float = 0.0
        self._last_keepalive_ok: Optional[bool] = None

        if self._configured and self.state_path:
            self._load_state()

    # ── 只读视图 ─────────────────────────────────────────

    @property
    def configured_cookie(self) -> str:
        """返回配置里原始的 Cookie 字符串。"""
        return self._configured

    @property
    def revision(self) -> int:
        """每吸收到一次有效轮换就自增，便于测试与日志定位。"""
        return self._revision

    @property
    def authenticated(self) -> bool:
        """当前 Cookie 是否足以生成 SAPISIDHASH（即是否算真登录）。"""
        return bool(build_sapisid_authorization(self.header()))

    def header(self) -> str:
        """返回本次请求应当发送的 Cookie 头。"""
        if not self._mutated:
            return self._configured
        return "; ".join(f"{name}={value}" for name, value in self._jar.items())

    def names(self) -> Tuple[str, ...]:
        """返回当前罐子里的 Cookie 名（只回名字，绝不回取值）。"""
        return tuple(self._jar)

    def status_line(self) -> str:
        """给日志用的一行状态摘要，不含任何 Cookie 取值。"""
        if not self._configured:
            return "未配置"
        parts = [f"{len(self._jar)} 项", "已鉴权" if self.authenticated else "缺少 SAPISID"]
        if self._revision:
            parts.append(f"已吸收轮换 {self._revision} 次")
        if self._last_rotation_at:
            age = max(0, int(time.time() - self._last_rotation_at))
            parts.append(f"上次轮换 {age // 60} 分钟前")
        if self._last_keepalive_ok is not None:
            parts.append("保鲜正常" if self._last_keepalive_ok else "保鲜未通过")
        return "，".join(parts)

    # ── 轮换吸收 ─────────────────────────────────────────

    @staticmethod
    def _make_fingerprint(cookie: str) -> str:
        """对配置里的 Cookie 取指纹，用于判断用户是否换了新 Cookie。"""
        if not cookie:
            return ""
        return hashlib.sha256(cookie.encode("utf-8")).hexdigest()

    def absorb_response(self, response: Any) -> bool:
        """从一个响应对象里吸收 Set-Cookie；返回是否真的发生了变更。"""
        return self.absorb(collect_set_cookie_headers(response))

    def absorb(self, set_cookie_headers: Iterable[str]) -> bool:
        """
        合并服务端下发的 Set-Cookie。

        只接受身份类与轮换类白名单里的 Cookie 名：一是避免罐子被埋点 Cookie
        撑大，二是防止服务端下发的删除指令把登录态就地清空。
        """
        if not self._configured or not self.auto_refresh:
            return False
        changed = False
        for raw in set_cookie_headers or ():
            jar = SimpleCookie()
            try:
                jar.load(str(raw))
            except (CookieError, ValueError):
                continue
            for name, morsel in jar.items():
                if name not in _ACCEPTED_COOKIE_NAMES:
                    continue
                if _is_deletion(morsel):
                    logger.debug(
                        f"[youtube] 忽略服务端删除 Cookie 的指令: {name}"
                    )
                    continue
                value = str(morsel.value or "").strip()
                if self._jar.get(name) == value:
                    continue
                self._jar[name] = value
                changed = True
                logger.debug(f"[youtube] 已吸收 Cookie 轮换: {name}")
        if not changed:
            return False
        self._mutated = True
        self._dirty = True
        self._revision += 1
        self._last_rotation_at = time.time()
        return True

    # ── 持久化 ───────────────────────────────────────────

    def _load_state(self) -> None:
        """读取上次落盘的轮换结果；配置换了新 Cookie 时直接丢弃旧状态。"""
        path = self.state_path
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as file_obj:
                data = json.load(file_obj)
        except Exception as exc:
            logger.warning(f"[youtube] 读取 Cookie 运行时文件失败: {exc}")
            return
        if not isinstance(data, dict):
            return
        if str(data.get("fingerprint") or "") != self._fingerprint:
            logger.info(
                "[youtube] 配置里的 Cookie 已更换，丢弃旧的运行时轮换状态"
            )
            return
        stored = data.get("cookies")
        if not isinstance(stored, dict):
            return
        merged = 0
        for name, value in stored.items():
            name_text = str(name or "").strip()
            value_text = str(value or "").strip()
            if not name_text or value_text in _DELETION_VALUES:
                continue
            if self._jar.get(name_text) == value_text:
                continue
            self._jar[name_text] = value_text
            merged += 1
        if not merged:
            return
        self._mutated = True
        try:
            self._last_rotation_at = float(data.get("updated_at") or 0.0)
        except (TypeError, ValueError):
            self._last_rotation_at = 0.0
        logger.info(
            f"[youtube] 已接续运行时 Cookie 轮换状态（{merged} 项较配置更新）"
        )

    def _write_state(self) -> None:
        """把当前罐子原子写入运行时文件，权限收敛到 0600。"""
        path = self.state_path
        if not path:
            return
        temp_path = ""
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            handle, temp_path = tempfile.mkstemp(
                prefix=os.path.basename(path) + ".",
                suffix=".tmp",
                dir=parent or ".",
            )
            payload = {
                "fingerprint": self._fingerprint,
                "cookies": dict(self._jar),
                "updated_at": time.time(),
                "revision": self._revision,
            }
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file_obj:
                json.dump(payload, file_obj, ensure_ascii=False, indent=2)
                file_obj.flush()
                os.fsync(file_obj.fileno())
            try:
                os.chmod(temp_path, 0o600)
            except OSError:
                pass
            os.replace(temp_path, path)
            temp_path = ""
        except Exception as exc:
            logger.warning(f"[youtube] 保存 Cookie 运行时文件失败: {exc}")
        finally:
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    async def flush(self) -> bool:
        """有未落盘的轮换时写一次文件；返回本次是否真的写了盘。"""
        if not self.state_path:
            self._dirty = False
            return False
        async with self._lock:
            if not self._dirty:
                return False
            self._dirty = False
            await asyncio.to_thread(self._write_state)
            return True

    async def absorb_and_flush(self, response: Any) -> bool:
        """吸收一次响应里的轮换并立即落盘（供解析链顺手调用）。"""
        if not self.absorb_response(response):
            return False
        await self.flush()
        return True

    # ── 保鲜 ─────────────────────────────────────────────

    async def keepalive(
        self,
        session: aiohttp.ClientSession,
        proxy: Optional[str] = None,
        timeout_seconds: float = 20.0,
    ) -> Tuple[Optional[bool], str]:
        """
        主动跑一次带登录态的轻量请求，触发并吸收服务端的 Cookie 轮换。

        Returns:
            Tuple[Optional[bool], str]: (服务端是否认为已登录, 可读摘要)。
            登录态读不出来时第一项为 None，此时不能据此判定 Cookie 失效。
        """
        cookie = self.header()
        if not cookie:
            return None, "未配置 Cookie，跳过保鲜"
        headers = {
            "User-Agent": _KEEPALIVE_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": cookie,
        }
        authorization = build_sapisid_authorization(cookie)
        if authorization:
            headers["Authorization"] = authorization
            headers["X-Origin"] = YOUTUBE_ORIGIN
            headers["X-Goog-AuthUser"] = "0"
        try:
            async with session.get(
                _KEEPALIVE_URL,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=max(5.0, timeout_seconds)),
                proxy=proxy,
                allow_redirects=True,
            ) as response:
                rotated = self.absorb_response(response)
                status = response.status
                html = await response.text()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_keepalive_at = time.time()
            return None, f"保鲜请求失败: {type(exc).__name__}: {exc}"

        await self.flush()
        self._last_keepalive_at = time.time()
        logged_in = _detect_logged_in(html)
        self._last_keepalive_ok = logged_in
        detail = [f"HTTP {status}"]
        detail.append("已吸收轮换" if rotated else "无新轮换")
        if logged_in is True:
            detail.append("服务端确认已登录")
        elif logged_in is False:
            detail.append("服务端判定未登录（Cookie 可能已失效）")
        else:
            detail.append("未读出登录态")
        return logged_in, "，".join(detail)
