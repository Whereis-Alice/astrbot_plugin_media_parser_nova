"""Nitter 页面解析。

X/Twitter 已停止对未登录访问输出带 JSON-LD 的服务端渲染页（爬虫 UA 只能拿到
403，或拿到 200 但页面里既没有 application/ld+json 也没有 "Comment" 节点），
因此原先的"公开帖子页"路线已经永久拿不到回复。

Nitter 是 X 的开源前端，自建实例可以稳定给出：

* 主推的完整原文与统计数字（评论 / 转发 / 喜欢 / 阅读）
* 回复区每条回复的作者、头像、时间、正文与点赞数

本模块只做纯函数式的 HTML -> 结构化数据转换，不涉及网络，便于单测覆盖。
"""

import html as html_lib
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

__all__ = [
    "NITTER_STAT_ICONS",
    "normalize_base_urls",
    "thread_url",
    "restore_media_url",
    "build_stats_line",
    "format_time",
    "parse_thread",
]

# Nitter 把图片代理成 /pic/<urlencoded 原始路径>；还原成 pbs.twimg.com 直链后
# 卡片就不必依赖内网可达的 Nitter 地址（也避免把内网地址写进产物）。
_PIC_PREFIX_RE = re.compile(r"^/pic/(?:orig/)?")
_PBS_HOST = "https://pbs.twimg.com/"
_AVATAR_SIZE_RE = re.compile(r"_(?:bigger|mini|normal)(?=\.[A-Za-z0-9]+$)")
_PASSTHROUGH_HOSTS = ("pbs.twimg.com/", "video.twimg.com/", "abs.twimg.com/")

_MAIN_BLOCK_RE = re.compile(
    r'<div[^>]+id="m"[^>]*class="main-tweet"[^>]*>(.*?)(?=<div[^>]+id="r"|\Z)',
    re.S,
)
_REPLIES_BLOCK_RE = re.compile(
    r'<div[^>]+id="r"[^>]*class="replies"[^>]*>(.*)\Z',
    re.S,
)
_ITEM_HEAD_RE = re.compile(
    r'<div class="timeline-item[^"]*"\s+data-username="([^"]*)"\s*>',
)
_TWEET_LINK_RE = re.compile(r'<a class="tweet-link" href="/[^"/]+/status/(\d+)')
_AVATAR_RE = re.compile(r'<img class="avatar[^"]*" src="([^"]+)"')
_FULLNAME_RE = re.compile(r'<a class="fullname"[^>]*title="([^"]*)"')
_DATE_RE = re.compile(r'<span class="tweet-date">\s*<a[^>]*title="([^"]*)"')
_CONTENT_RE = re.compile(r'<div class="tweet-content[^"]*"[^>]*>(.*?)</div>', re.S)
_STATS_BLOCK_RE = re.compile(r'<div class="tweet-stats">(.*?)</div>\s*</div>', re.S)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_BLOCK_END_RE = re.compile(r"</(?:p|div|li)\s*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_HANDLE_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_]")
#: Nitter 对被删除 / 受保护的推文既可能渲染独立的 unavailable-box 元素，
#: 也可能把 unavailable 直接写进 timeline-item 的 class，两种都要跳过。
_UNAVAILABLE_RE = re.compile(r'class="[^"]*\bunavailable[\w-]*\b')

#: Nitter 统计图标 -> 卡片 stats_line 使用的 emoji 前缀。
#: 顺序即卡片展示顺序：喜欢 / 转发 / 评论 / 阅读。
NITTER_STAT_ICONS: Tuple[Tuple[str, str, str], ...] = (
    ("heart", "likes", "\u2764\ufe0f"),
    ("retweet", "retweets", "\u21a9\ufe0f"),
    ("comment", "replies", "\U0001f4ac"),
    ("views", "views", "\U0001f440"),
)


def normalize_base_urls(raw: Any) -> Tuple[str, ...]:
    """把配置里的 Nitter 地址整理成去重后的 base URL 列表。

    支持逗号、分号、空白或换行分隔的多个实例；缺少协议时按 http:// 补全，
    并去掉末尾斜杠，便于直接拼路径。
    """
    if isinstance(raw, (list, tuple, set)):
        candidates: List[str] = [str(item) for item in raw]
    else:
        candidates = re.split(r"[,;\s]+", str(raw or ""))
    bases: List[str] = []
    for candidate in candidates:
        value = candidate.strip().rstrip("/")
        if not value:
            continue
        if "://" not in value:
            value = "http://" + value
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            continue
        normalized = value.rstrip("/")
        if normalized not in bases:
            bases.append(normalized)
    return tuple(bases)


def thread_url(base_url: str, tweet_id: str, screen_name: str = "") -> str:
    """拼出 Nitter 上的帖子页地址。

    已知作者 handle 时用规范路径；未知时退回 /i/status/{id}（Nitter 会自行
    重定向到规范路径）。
    """
    base = str(base_url or "").rstrip("/")
    handle = _HANDLE_UNSAFE_RE.sub("", str(screen_name or ""))[:15] or "i"
    return base + "/" + handle + "/status/" + str(tweet_id)


def restore_media_url(
    src: str,
    base_url: str = "",
    *,
    upgrade_avatar: bool = False,
) -> str:
    """把 Nitter 的 /pic/... 代理路径还原为原始 CDN 直链。

    Args:
        src: Nitter HTML 里的 src / href 值。
        base_url: 还原失败时用于拼绝对地址的 Nitter base。
        upgrade_avatar: 头像是否升级到 _400x400（Nitter 默认给 _bigger，
            73px 放到卡片上偏糊）。
    """
    value = str(src or "").strip()
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if _PIC_PREFIX_RE.match(value):
        path = unquote(_PIC_PREFIX_RE.sub("", value)).lstrip("/")
        if path.startswith(_PASSTHROUGH_HOSTS):
            restored = "https://" + path
        else:
            restored = _PBS_HOST + path
        if upgrade_avatar:
            restored = _AVATAR_SIZE_RE.sub("_400x400", restored)
        return restored
    base = str(base_url or "").rstrip("/")
    if base and value.startswith("/"):
        return base + value
    return value


#: Nitter 的时间标题固定是英文 UTC 格式，转成本地时间与其它平台热评一致。
_NITTER_TIME_FORMATS = (
    "%b %d, %Y · %I:%M:%S %p %Z",
    "%b %d, %Y · %I:%M %p %Z",
    "%b %d, %Y · %H:%M:%S %Z",
    "%b %d, %Y · %H:%M %Z",
)


def format_time(value: Any) -> str:
    """把 Nitter 的英文 UTC 时间转成本地 "YYYY-MM-DD HH:MM:SS"。

    解析失败时原样返回，宁可显示英文也不要丢掉时间信息。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text.replace(" ", " ")
    for fmt in _NITTER_TIME_FORMATS:
        try:
            parsed = datetime.strptime(normalized, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        try:
            return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError):
            return text
    return text


def _html_to_text(fragment: Any) -> str:
    """把正文片段转成纯文本，保留换行与 #话题 文字。"""
    text = _BR_RE.sub("\n", str(fragment or ""))
    text = _BLOCK_END_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    text = text.replace("\u00a0", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


def _stat_value(fragment: str, icon: str) -> str:
    """取某个统计图标后面的数字文本，缺失时返回空串。"""
    match = re.search(
        'icon-' + icon + r'"[^>]*>\s*</span>\s*([0-9][0-9,.]*)',
        fragment,
    )
    return match.group(1).strip().rstrip(".") if match else ""


def _stat_number(value: str) -> int:
    """把带千位分隔符的数字文本转成 int，无数字时返回 0。"""
    digits = re.sub(r"[^0-9]", "", str(value or ""))
    return int(digits) if digits else 0


def _extract_stats(fragment: str) -> Dict[str, str]:
    """解析 tweet-stats 区块，返回 {字段名: 数字文本}。"""
    match = _STATS_BLOCK_RE.search(fragment)
    block = match.group(1) if match else fragment
    stats: Dict[str, str] = {}
    for icon, key, _emoji in NITTER_STAT_ICONS:
        value = _stat_value(block, icon)
        if value:
            stats[key] = value
    return stats


def build_stats_line(stats: Dict[str, str]) -> str:
    """把统计字典拼成卡片可解析的 stats_line。"""
    parts: List[str] = []
    for _icon, key, emoji in NITTER_STAT_ICONS:
        value = str(stats.get(key) or "").strip()
        if value:
            parts.append(emoji + value)
    return " ".join(parts)


def _iter_items(block: str):
    """按 timeline-item 切分区块，逐个产出 (handle, 片段)。

    片段包含 timeline-item 自身的开标签，这样 class 上的 unavailable 标记也能
    被识别（Nitter 既会用独立的 unavailable-box 元素，也会把状态写进 class）。
    """
    heads = list(_ITEM_HEAD_RE.finditer(block))
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(block)
        yield head.group(1), block[head.start() : end]


def _display_name(fragment: str, handle: str) -> str:
    """组合 昵称(@handle)，与其他平台热评展示保持一致。"""
    match = _FULLNAME_RE.search(fragment)
    fullname = html_lib.unescape(match.group(1)).strip() if match else ""
    name = fullname or handle or "未知用户"
    if handle and handle.lower() != name.lower():
        return name + "(@" + handle + ")"
    return name


def _item_comment(
    handle: str,
    fragment: str,
    base_url: str,
) -> Optional[Dict[str, Any]]:
    """把单个 timeline-item 片段转成热评字典。"""
    if _UNAVAILABLE_RE.search(fragment):
        return None
    content_match = _CONTENT_RE.search(fragment)
    if not content_match:
        return None
    message = _html_to_text(content_match.group(1))
    if not message:
        return None
    link_match = _TWEET_LINK_RE.search(fragment)
    avatar_match = _AVATAR_RE.search(fragment)
    date_match = _DATE_RE.search(fragment)
    stats = _extract_stats(fragment)
    return {
        "username": _display_name(fragment, handle),
        "uid": handle,
        "likes": _stat_number(stats.get("likes", "")),
        "time": format_time(
            html_lib.unescape(date_match.group(1)) if date_match else ""
        ),
        "message": message,
        "avatar_url": restore_media_url(
            avatar_match.group(1) if avatar_match else "",
            base_url,
            upgrade_avatar=True,
        ),
        "comment_id": link_match.group(1) if link_match else "",
    }


def parse_thread(html_text: str, limit: int, base_url: str = "") -> Dict[str, Any]:
    """解析 Nitter 帖子页，返回主推信息与按点赞降序排列的回复。

    Args:
        html_text: Nitter 帖子页 HTML。
        limit: 最多保留的回复条数；<= 0 时只解析主推信息。
        base_url: Nitter base URL，用于兜底拼接无法还原的相对地址。

    Returns:
        含 comments / stats / stats_line / author_name / author_avatar /
        text / time 的字典。
    """
    text = str(html_text or "")
    result: Dict[str, Any] = {
        "comments": [],
        "stats": {},
        "stats_line": "",
        "author_avatar": "",
        "author_name": "",
        "text": "",
        "time": "",
    }
    if not text:
        return result

    main_match = _MAIN_BLOCK_RE.search(text)
    if main_match:
        main = main_match.group(1)
        head = _ITEM_HEAD_RE.search(main)
        handle = head.group(1) if head else ""
        stats = _extract_stats(main)
        content_match = _CONTENT_RE.search(main)
        avatar_match = _AVATAR_RE.search(main)
        date_match = _DATE_RE.search(main)
        result["stats"] = stats
        result["stats_line"] = build_stats_line(stats)
        result["author_name"] = _display_name(main, handle)
        result["author_avatar"] = restore_media_url(
            avatar_match.group(1) if avatar_match else "",
            base_url,
            upgrade_avatar=True,
        )
        result["text"] = _html_to_text(content_match.group(1)) if content_match else ""
        result["time"] = format_time(
            html_lib.unescape(date_match.group(1)) if date_match else ""
        )

    try:
        keep = max(0, int(limit))
    except (TypeError, ValueError):
        keep = 0
    if keep <= 0:
        return result

    replies_match = _REPLIES_BLOCK_RE.search(text)
    if not replies_match:
        return result

    comments: List[Dict[str, Any]] = []
    seen: set = set()
    for handle, fragment in _iter_items(replies_match.group(1)):
        comment = _item_comment(handle, fragment, base_url)
        if comment is None:
            continue
        key = comment.get("comment_id") or (comment.get("uid"), comment.get("message"))
        if key in seen:
            continue
        seen.add(key)
        comments.append(comment)
    comments.sort(key=lambda item: int(item.get("likes", 0) or 0), reverse=True)
    result["comments"] = comments[:keep]
    return result
