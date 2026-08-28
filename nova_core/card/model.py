"""卡片设计系统：视图模型。

把 ParseResult + 已下载图片路径归一化成渲染层唯一依赖的 CardModel，
让主题 / 区块只关心"要画什么"，不再各自解析 extra 字典。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import surface
from .typeset import clean_text, limit_chars

# 统计行 emoji -> 中文标签
STAT_LABELS: dict[str, str] = {
    "👍": "点赞",
    "❤": "喜欢",
    "❤️": "喜欢",
    "🧡": "喜欢",
    "💗": "喜欢",
    "🪙": "投币",
    "⭐": "收藏",
    "↩": "转发",
    "↩️": "转发",
    "🔁": "转发",
    "📢": "转发",
    "💬": "评论",
    "✉": "回复",
    "✉️": "回复",
    "👀": "播放",
    "▶": "播放",
    "💭": "弹幕",
    "🔗": "链接",
    "📈": "浏览",
    "🔥": "热度",
    "🏄": "在线",
}

_ICONS_BY_LENGTH = sorted(STAT_LABELS.items(), key=lambda kv: len(kv[0]), reverse=True)


def parse_stats(stats_line: str | None) -> list[tuple[str, str]]:
    """将『👍 1.2万 🪙 8千』解析为 [(标签, 数值)]。"""
    if not stats_line:
        return []
    tokens = str(stats_line).split()
    stats: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        matched: str | None = None
        rest = ""
        for icon, label in _ICONS_BY_LENGTH:
            if token.startswith(icon):
                matched = label
                rest = clean_text(token[len(icon):])
                break
        if matched is not None:
            value = rest
            if not value and i + 1 < len(tokens):
                i += 1
                value = tokens[i]
            if value:
                stats.append((matched, value))
        else:
            cleaned = clean_text(token)
            if cleaned:
                stats.append((cleaned, ""))
        i += 1
    return stats


def format_ts(ts: int | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return None


def compact_number(value: Any) -> str:
    """点赞数等大数字压缩为『1.2万』样式。"""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        text = str(value or "").strip()
        return text
    if number < 0:
        return ""
    if number < 10000:
        return str(number)
    if number < 100000000:
        return f"{number / 10000:.1f}".rstrip("0").rstrip(".") + "万"
    return f"{number / 100000000:.1f}".rstrip("0").rstrip(".") + "亿"


@dataclass(slots=True)
class MediaItem:
    """一张待排版的媒体图，携带真实宽高比以便自适应编排。"""

    path: Path
    aspect: float = 1.0
    is_video: bool = False
    duration: str = ""

    @property
    def is_portrait(self) -> bool:
        return self.aspect < 0.86

    @property
    def is_wide(self) -> bool:
        return self.aspect > 1.5


@dataclass(slots=True)
class CommentItem:
    username: str
    uid: str
    likes: str
    time: str
    message: str
    avatar: Path | None = None


@dataclass(slots=True)
class QuoteItem:
    author: str
    title: str
    text: str
    url: str


@dataclass(slots=True)
class CardModel:
    """渲染层唯一数据来源。"""

    platform_key: str = "website"
    platform_name: str = "网页"
    content_type: str = "动态"
    time_text: str = ""
    title: str = ""
    body: str = ""
    has_real_title: bool = True
    author_name: str = ""
    author_handle: str = ""
    avatar: Path | None = None
    stats: list[tuple[str, str]] = field(default_factory=list)
    media: list[MediaItem] = field(default_factory=list)
    hero_is_video: bool = False
    duration_text: str = ""
    online: str = ""
    warnings: list[str] = field(default_factory=list)
    comments: list[CommentItem] = field(default_factory=list)
    quote: QuoteItem | None = None
    url: str = ""
    watermark: str = ""
    total_media: int = 0

    @property
    def has_media(self) -> bool:
        return bool(self.media)

    @property
    def hero(self) -> MediaItem | None:
        return self.media[0] if self.media else None

    @property
    def has_author(self) -> bool:
        return bool(self.author_name or self.avatar)


def _duration_text(seconds: Any) -> str:
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    from ..rika_render.utils import fmt_duration

    return fmt_duration(value)


def normalize_comments(
    result: Any,
    max_chars: int,
    limit: int = 5,
    avatars: dict[int, Any] | None = None,
) -> list[CommentItem]:
    """把 extra['hot_comments'] 归一化为 CommentItem 列表。

    avatars 以 hot_comments 的原始下标为键（下载失败的条目直接缺席），
    因此这里必须按原始下标取头像，不能按过滤后的序号取。
    """
    raw = getattr(result, "extra", {}).get("hot_comments")
    if not isinstance(raw, list):
        return []
    avatar_map = avatars or {}
    items: list[CommentItem] = []
    for index, entry in enumerate(raw[:limit]):
        if not isinstance(entry, dict):
            continue
        message = limit_chars(clean_text(str(entry.get("message") or "")), max_chars)
        if not message:
            continue
        avatar_path = avatar_map.get(index)
        items.append(
            CommentItem(
                username=clean_text(str(entry.get("username") or "未知用户")) or "未知用户",
                uid=clean_text(str(entry.get("uid") or "")),
                likes=compact_number(entry.get("likes", 0)),
                time=clean_text(str(entry.get("time") or "")),
                message=message,
                avatar=Path(str(avatar_path)) if avatar_path else None,
            )
        )
    return items


def build_model(
    result: Any,
    images: dict[str, Any],
    *,
    watermark: str = "",
    show_watermark: bool = True,
    show_url: bool = True,
    comment_max_chars: int = 180,
    max_media: int = 9,
) -> CardModel:
    """由 ParseResult + 图片路径构建 CardModel。

    show_watermark / show_url 为 False 时直接把对应字段置空，让所有皮肤、
    所有布局统一失去该元素——绘制层无需感知开关。
    """
    extra = getattr(result, "extra", {}) or {}
    platform = getattr(result, "platform", None)
    platform_key = str(getattr(platform, "name", "") or "website").lower()
    platform_name = str(getattr(platform, "display_name", "") or platform_key)

    hero_path = images.get("hero")
    grid_paths = [p for p in (images.get("grid") or []) if p]

    ordered: list[Any] = []
    seen: set[str] = set()
    for path in ([hero_path] if hero_path else []) + grid_paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)

    total_media = len(ordered)
    duration = _duration_text(extra.get("duration"))
    media: list[MediaItem] = []
    for index, path in enumerate(ordered[: max(1, max_media)]):
        p = Path(str(path))
        media.append(
            MediaItem(
                path=p,
                aspect=surface.image_aspect(p, 1.0) if surface.Image is not None else 1.0,
                is_video=bool(index == 0 and hero_path is not None),
                duration=duration if index == 0 and hero_path is not None else "",
            )
        )

    stats = parse_stats(extra.get("stats_line"))

    title = clean_text(getattr(result, "title", None))
    body = clean_text(getattr(result, "text", None))
    has_real_title = bool(title)
    if not title and body:
        # 没有真实标题时把正文提为标题，避免同一段文字画两遍
        title, body = body, ""

    author = getattr(result, "author", None)
    author_name = clean_text(getattr(author, "name", "") or "") if author else ""
    author_handle = clean_text(getattr(author, "description", "") or "") if author else ""

    quote: QuoteItem | None = None
    repost = getattr(result, "repost", None)
    if repost is not None:
        quote_author = getattr(repost, "author", None)
        quote = QuoteItem(
            author=clean_text(getattr(quote_author, "name", "") or "") if quote_author else "",
            title=clean_text(getattr(repost, "title", None)),
            text=clean_text(getattr(repost, "text", None)),
            url=str(getattr(repost, "url", "") or "").strip(),
        )
        if not (quote.author or quote.title or quote.text):
            quote = None

    content_type = str(getattr(result, "content_type", "") or extra.get("content_type") or "").strip()
    if not content_type:
        content_type = "视频" if hero_path is not None else ("图文" if media else "动态")

    return CardModel(
        platform_key=platform_key,
        platform_name=platform_name,
        content_type=content_type,
        time_text=format_ts(getattr(result, "timestamp", None)) or "",
        title=title,
        body=body,
        has_real_title=has_real_title,
        author_name=author_name or ("未知作者" if author else ""),
        author_handle=author_handle,
        avatar=Path(str(images["avatar"])) if images.get("avatar") else None,
        stats=stats,
        media=media,
        hero_is_video=hero_path is not None,
        duration_text=duration,
        online=clean_text(str(extra.get("online") or "")),
        warnings=[clean_text(str(w)) for w in (extra.get("limit_warnings") or []) if str(w).strip()],
        comments=normalize_comments(
            result,
            comment_max_chars,
            avatars=images.get("comment_avatars") or {},
        ),
        quote=quote,
        url=str(getattr(result, "url", "") or "").strip() if show_url else "",
        watermark=str(watermark or "").strip() if show_watermark else "",
        total_media=total_media,
    )
