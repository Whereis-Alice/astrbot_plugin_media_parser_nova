"""nova_core.parser.utils 模块。"""

from __future__ import annotations
import json
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse


class SkipParse(Exception):
    pass


def format_duration_ms(duration_ms) -> str:
    """将毫秒时长格式化为 mm:ss 或 hh:mm:ss。"""
    if duration_ms is None:
        return ""
    try:
        total_seconds = max(0, int(duration_ms) // 1000)
    except (TypeError, ValueError):
        return ""

    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _ensure_url_has_scheme(url: str) -> str:
    """确保URL带有scheme，便于urlparse正确解析hostname。"""
    if not url:
        return url
    u = url.strip()
    if u.startswith("//"):
        return "https:" + u
    if u.startswith(("http://", "https://")):
        return u
    return "https://" + u


def _looks_like_url(value: str) -> bool:
    """判断字符串是否为完整链接，避免把 tracking 参数当成跳转链接。"""
    lowered = str(value or "").strip().lower()
    return lowered.startswith(("http://", "https://", "//"))


def _is_live_url_basic(url: str) -> bool:
    """仅基于hostname标签判断是否为live域名。"""
    parsed = urlparse(_ensure_url_has_scheme(url))
    host = (parsed.hostname or "").strip(".").lower()
    if not host:
        return False
    labels = [x for x in host.split(".") if x]
    return "live" in labels


def is_live_url(url: str) -> bool:
    """判断是否为直播类型链接或“跳转到直播”的重定向链接。

    规则：
    - 直链：hostname 的任意标签为 live，则判定为直播域名链接
    - 重定向：若URL的 query 参数里包含一个可解码出的URL，且该URL为直播域名链接，则也判定为直播

    例：
    - https://live.bilibili.com/ -> True
    - https://api.live.bilibili.com/ -> True
    - https://example.com/redirect?url=https%3A%2F%2Flive.example.com%2Froom -> True
    - https://www.douyin.com/ -> False
    """
    if not url:
        return False
    try:
        if _is_live_url_basic(url):
            return True

        parsed = urlparse(_ensure_url_has_scheme(url))
        qs = parse_qs(parsed.query, keep_blank_values=True)
        for values in qs.values():
            for v in values:
                if not v:
                    continue
                candidate = v.strip()
                for _ in range(3):
                    # 仅当参数本身是一个完整链接时才按跳转链接判定，
                    # 否则 from=live.share 这类 tracking 参数会被误判为直播
                    if _looks_like_url(candidate) and _is_live_url_basic(candidate):
                        return True
                    new_candidate = unquote(candidate)
                    if new_candidate == candidate:
                        break
                    candidate = new_candidate

        return False
    except Exception:
        return False


def _pick_card_url(message_data: dict) -> Optional[str]:
    """从卡片结构体中取出跳转链接（qqdocurl / jumpUrl）。"""
    if not isinstance(message_data, dict):
        return None
    meta = message_data.get("meta") or {}
    if not isinstance(meta, dict):
        return None
    detail_1 = meta.get("detail_1") or {}
    curl_link = detail_1.get("qqdocurl") if isinstance(detail_1, dict) else None
    if not curl_link:
        news = meta.get("news") or {}
        curl_link = news.get("jumpUrl") if isinstance(news, dict) else None
    return curl_link


def extract_url_from_card_data(msg_data) -> Optional[str]:
    """从单个消息段的 data 字段中提取 QQ 结构化卡片 URL。"""
    try:
        curl_link = None
        if isinstance(msg_data, dict) and not msg_data.get('data'):
            curl_link = _pick_card_url(msg_data)

        if not curl_link:
            json_str = (
                msg_data.get('data', '')
                if isinstance(msg_data, dict) else msg_data
            )
            # data 既可能是 JSON 字符串，也可能已被适配器解析成 dict
            if isinstance(json_str, dict):
                curl_link = _pick_card_url(json_str)
            elif json_str and isinstance(json_str, str):
                curl_link = _pick_card_url(json.loads(json_str))
        return curl_link
    except (AttributeError, KeyError, json.JSONDecodeError, TypeError):
        return None


def build_request_headers(
    is_video: bool = False,
    referer: Optional[str] = None,
    default_referer: Optional[str] = None,
    origin: Optional[str] = None,
    user_agent: Optional[str] = None,
    custom_headers: Optional[dict] = None
) -> dict:
    """构建请求头

    Args:
        is_video: 是否为视频（True为视频，False为图片）
        referer: Referer URL，如果提供则使用
        default_referer: 默认Referer URL（如果referer未提供）
        origin: Origin URL（可选）
        user_agent: User-Agent（可选，默认使用桌面端 User-Agent）
        custom_headers: 自定义请求头（如果提供，会与默认请求头合并）

    Returns:
        请求头字典
    """
    if custom_headers and 'Referer' in custom_headers:
        referer_url = custom_headers['Referer']
    else:
        referer_url = referer if referer else (default_referer or '')
    
    if user_agent:
        effective_user_agent = user_agent
    else:
        effective_user_agent = (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/120.0.0.0 Safari/537.36'
        )
    
    default_accept_language = 'zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7'
    
    if is_video:
        headers = {
            'User-Agent': effective_user_agent,
            'Accept': '*/*',
            'Accept-Language': default_accept_language,
            'Accept-Encoding': 'gzip, deflate',
        }
    else:
        headers = {
            'User-Agent': effective_user_agent,
            'Accept': (
                'image/avif,image/webp,image/apng,image/svg+xml,'
                'image/*,*/*;q=0.8'
            ),
            'Accept-Language': default_accept_language,
            'Accept-Encoding': 'gzip, deflate',
        }
    
    if referer_url:
        headers['Referer'] = referer_url
    
    if origin:
        headers['Origin'] = origin
    
    if custom_headers:
        headers.update(custom_headers)
    
    return headers
