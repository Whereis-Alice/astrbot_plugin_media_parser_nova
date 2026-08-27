"""卡片区块：可测量、可组合的最小排版单元。

每个区块实现两个方法：

* measure(ctx, width) -> int  返回在给定宽度下需要的精确高度
* draw(ctx, layer, x, y, width) -> None  在给定位置绘制

引擎先把所有区块 measure 一遍算出画布高度，再统一绘制，
因此不存在任何硬编码的卡片高度，也不会出现"先渲主卡再贴板"的接缝。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from . import surface
from .model import CommentItem, MediaItem, QuoteItem
from .palette import RGB, RGBA, alpha, darken, mix, readable_ink

# ============================ 基类 ============================


class Block:
    """区块协议。子类只需覆写 _measure / draw。"""

    #: 同一实例可被 measure 多次（分栏试排），按宽度缓存
    _cache: dict[int, int]

    def __init__(self) -> None:
        self._cache = {}

    # -- 对外 --

    def measure(self, ctx: Any, width: int) -> int:
        width = max(1, int(width))
        if not hasattr(self, "_cache") or self._cache is None:
            self._cache = {}
        cached = self._cache.get(width)
        if cached is None:
            cached = max(0, int(self._measure(ctx, width)))
            self._cache[width] = cached
        return cached

    # -- 子类实现 --

    def _measure(self, ctx: Any, width: int) -> int:
        return 0

    def draw(self, ctx: Any, layer: Any, x: int, y: int, width: int) -> None:
        return None


@dataclass
class SpacerBlock(Block):
    """纯间距。"""

    height: int = 0

    def __post_init__(self) -> None:
        self._cache = {}

    def _measure(self, ctx: Any, width: int) -> int:
        return max(0, self.height)


@dataclass
class RuleBlock(Block):
    """分隔线。style: hair / dash / accent / double / heavy"""

    style: str = "hair"
    pad_before: int = 0
    pad_after: int = 0
    width_ratio: float = 1.0

    def __post_init__(self) -> None:
        self._cache = {}

    def _line_height(self, ctx: Any) -> int:
        if self.style == "double":
            return max(4, ctx.m.unit + 3)
        if self.style in ("accent", "heavy"):
            return max(2, int(round(ctx.m.unit * 0.75)))
        return 1

    def _measure(self, ctx: Any, width: int) -> int:
        return self.pad_before + self._line_height(ctx) + self.pad_after

    def draw(self, ctx: Any, layer: Any, x: int, y: int, width: int) -> None:
        top = y + self.pad_before
        span = max(8, int(width * max(0.05, min(1.0, self.width_ratio))))
        if self.style == "dash":
            surface.hairline(layer, x, top, x + span, ctx.hair, dash=max(3, ctx.m.unit))
        elif self.style == "double":
            surface.hairline(layer, x, top, x + span, ctx.hair)
            surface.hairline(layer, x, top + max(3, ctx.m.unit), x + span, ctx.hair)
        elif self.style == "heavy":
            surface.hairline(layer, x, top, x + span, alpha(ctx.ink, 210), width=self._line_height(ctx))
        elif self.style == "accent":
            surface.hairline(layer, x, top, x + span, alpha(ctx.accent, 235), width=self._line_height(ctx))
        else:
            surface.hairline(layer, x, top, x + span, ctx.hair)


# ============================ 小工具 ============================


def _chip(
    ctx: Any,
    layer: Any,
    x: int,
    y: int,
    text: str,
    *,
    fill: RGBA | None,
    ink: RGB,
    border: RGBA | None = None,
    font: Any = None,
    tracking: float = 0.0,
    bold: bool = False,
    radius: int | None = None,
    pad_x: int | None = None,
    height: int | None = None,
) -> int:
    """绘制一个胶囊标签，返回占用宽度。"""
    font = font or ctx.font(ctx.m.f_eyebrow, bold=bold)
    pad_x = ctx.m.gap_sm if pad_x is None else pad_x
    h = height or ctx.m.chip_h
    text_w = ctx.ts.tracked_width(text, font, tracking)
    w = text_w + pad_x * 2
    r = h // 2 if radius is None else min(radius, h // 2)
    surface.panel(layer, (x, y, x + w, y + h), r, fill=fill, border=border, border_width=ctx.m.hairline)
    baseline = y + (h - ctx.ts.line_height(font, 1.0)) // 2
    ctx.text(layer, (x + pad_x, baseline), text, font, ink, tracking=tracking, bold=bold)
    return w


#: 统计项标签 -> 图标种类（哔哩哔哩风格的图标统计行）
_GLYPH_BY_LABEL: tuple[tuple[tuple[str, ...], str], ...] = (
    (("播放", "观看", "浏览", "阅读", "view", "play"), "play"),
    (("弹幕",), "danmaku"),
    (("评论", "回复", "comment", "reply"), "comment"),
    (("点赞", "赞", "喜欢", "like", "推荐"), "like"),
    (("投币", "硬币", "打赏", "coin"), "coin"),
    (("收藏", "star", "favorite", "mark"), "star"),
    (("转发", "分享", "share", "repost"), "share"),
    (("在线", "同时在看"), "eye"),
    (("时长", "时间", "duration"), "clock"),
)


def _glyph_kind(label: str) -> str:
    text = str(label or "").strip().lower()
    for keys, kind in _GLYPH_BY_LABEL:
        for key in keys:
            if key in text:
                return kind
    return "dot"


def _stat_value(ctx: Any, kinds: Sequence[str]) -> str:
    """按图标种类取第一个有值的统计项。"""
    for label, value in ctx.model.stats:
        if value and _glyph_kind(label) in kinds:
            return value
    return ""


def _stat_pairs(ctx: Any, kinds: Sequence[str]) -> list[tuple[str, str]]:
    """批量取统计项，保持模型里的原始顺序，返回 (标签, 值)。"""
    picked: list[tuple[str, str]] = []
    for label, value in ctx.model.stats:
        if value and _glyph_kind(label) in kinds:
            picked.append((label, value))
    return picked


#: 正文里要走强调色的行内元素：@提及 与裸链接（话题 #...# 单独处理）
_MENTION_RE = re.compile(r"@[^\s#@，。！？、；：,.!?;:]{1,24}|https?://[^\s]+")


def _rich_runs(line: str, inside: bool) -> tuple[list[tuple[str, bool]], bool]:
    """把一行正文切成 (片段, 是否高亮) 序列，并返回话题是否仍未闭合。

    井号话题连同两侧井号整段高亮；inside 用于跨行携带未闭合状态，
    因此折行后的长话题依然保持连续着色。非高亮片段里再挑出 @提及与裸链接。
    """
    runs: list[tuple[str, bool]] = []
    buf = ""
    for ch in line:
        if ch == "#":
            if inside:
                runs.append((buf + ch, True))
                buf = ""
                inside = False
            else:
                if buf:
                    runs.append((buf, False))
                buf = ch
                inside = True
            continue
        buf += ch
    if buf:
        runs.append((buf, inside))

    out: list[tuple[str, bool]] = []
    for text, hot in runs:
        if hot:
            out.append((text, True))
            continue
        pos = 0
        for match in _MENTION_RE.finditer(text):
            if match.start() > pos:
                out.append((text[pos : match.start()], False))
            out.append((match.group(0), True))
            pos = match.end()
        if pos < len(text):
            out.append((text[pos:], False))
    return [run for run in out if run[0]], inside


def _draw_rich_para(
    ctx: Any,
    layer: Any,
    x: int,
    y: int,
    lines: Sequence[str],
    font: Any,
    fill: RGB,
    accent_fill: RGB,
    *,
    leading: float = 1.5,
    bold: bool = False,
) -> int:
    """逐行绘制富文本正文，话题 / 提及 / 链接走 accent_fill，其余走 fill。

    只含单个片段的行只会触发一次 ctx.text，同一段文字只画一次的约定不变。
    """
    step = ctx.ts.line_height(font, leading)
    inside = False
    for index, line in enumerate(lines):
        runs, inside = _rich_runs(line, inside)
        top = y + index * step
        if not runs:
            continue
        if len(runs) == 1:
            text, hot = runs[0]
            ctx.text(layer, (x, top), text, font, accent_fill if hot else fill, bold=bold)
            continue
        cursor = x
        for text, hot in runs:
            cursor += ctx.text(layer, (cursor, top), text, font, accent_fill if hot else fill, bold=bold)
    return step * len(lines)


def _cover_meta(ctx: Any) -> list[tuple[str, str]]:
    """封面左下角浮层要显示的两项（哔哩哔哩：播放量 + 弹幕数）。"""
    picked: list[tuple[str, str]] = []
    for label, value in ctx.model.stats:
        kind = _glyph_kind(label)
        if kind in ("play", "danmaku") and value:
            picked.append((kind, value))
        if len(picked) >= 2:
            break
    return picked


def _meta_parts(ctx: Any) -> list[str]:
    model = ctx.model
    parts: list[str] = []
    if model.content_type:
        parts.append(model.content_type)
    if model.time_text:
        parts.append(model.time_text)
    if model.online:
        parts.append(f"在线 {model.online}")
    return parts


# ============================ 眉标 ============================


@dataclass
class EyebrowBlock(Block):
    """平台 / 类型 / 时间。variant: chip / rule / bracket / plate / bili_top"""

    variant: str = "chip"

    def __post_init__(self) -> None:
        self._cache = {}

    def _measure(self, ctx: Any, width: int) -> int:
        m = ctx.m
        if self.variant == "bili_top":
            brand_f = ctx.font(int(m.f_subtitle * 1.02), bold=True)
            row = max(m.chip_h, ctx.ts.line_height(brand_f, 1.0))
            return row + m.gap_md + 1
        if self.variant == "chip":
            return m.chip_h
        if self.variant == "plate":
            return m.chip_h + m.gap_xs + 1
        if self.variant == "bracket":
            f = ctx.font(m.f_eyebrow, bold=True)
            return ctx.ts.line_height(f, 1.0) + m.gap_xs + 1
        # rule
        f = ctx.font(m.f_eyebrow, bold=True)
        return max(m.unit * 2, ctx.ts.line_height(f, 1.0)) + m.gap_xs + 1

    def _draw_bili_top(self, ctx: Any, layer: Any, x: int, y: int, width: int) -> None:
        """仿 B 站手机端动态详情页顶栏：返回箭头 + 小电视字标 + 分区标签 + 右侧更多。"""
        m, model = ctx.m, ctx.model
        brand_f = ctx.font(int(m.f_subtitle * 1.02), bold=True)
        meta_f = ctx.font(m.f_meta)
        row = max(m.chip_h, ctx.ts.line_height(brand_f, 1.0))
        mid = y + row // 2

        nav = max(10, int(row * 0.56))
        back_w = max(7, nav // 2)
        surface.glyph(
            layer,
            (x, mid - nav // 2, x + back_w, mid + nav // 2),
            "back",
            alpha(ctx.ink, 235),
        )

        more_w = max(5, int(row * 0.20))
        more_h = max(12, int(row * 0.58))
        surface.glyph(
            layer,
            (x + width - more_w, mid - more_h // 2, x + width, mid + more_h // 2),
            "more",
            ctx.ink_muted,
        )
        right_limit = x + width - more_w - m.gap_md

        cursor = x + back_w + m.gap_md
        logo_h = int(row * 0.66)
        logo_w = int(logo_h * 1.18)
        surface.bili_logo(
            layer,
            (cursor, mid - logo_h // 2, cursor + logo_w, mid - logo_h // 2 + logo_h),
            alpha(ctx.accent, 255),
        )
        cursor += logo_w + m.gap_2xs * 2
        cursor += ctx.text(
            layer,
            (cursor, y + (row - ctx.ts.line_height(brand_f, 1.0)) // 2),
            "bilibili",
            brand_f,
            ctx.accent,
            bold=True,
        )

        if model.content_type:
            chip_h = max(14, int(row * 0.62))
            chip_f = ctx.font(max(9, int(m.f_eyebrow * 0.94)), bold=True)
            cursor += m.gap_sm
            cursor += _chip(
                ctx,
                layer,
                cursor,
                mid - chip_h // 2,
                model.content_type,
                fill=alpha(ctx.accent, 255),
                ink=ctx.accent_ink,
                font=chip_f,
                bold=True,
                radius=max(2, m.radius_chip),
                pad_x=m.gap_sm,
                height=chip_h,
            )

        # 右侧仅保留"真实来源站点"，时间已下移到作者行
        source = model.platform_name or ""
        if source and "哔哩" not in source and source.lower() != "bilibili":
            tw = ctx.ts.width(source, meta_f)
            if cursor + m.gap_md + tw <= right_limit:
                ctx.text(
                    layer,
                    (right_limit - tw, y + (row - ctx.ts.line_height(meta_f, 1.0)) // 2),
                    source,
                    meta_f,
                    ctx.ink_muted,
                )

        surface.hairline(layer, x, y + row + m.gap_md, x + width, ctx.hair)

    def draw(self, ctx: Any, layer: Any, x: int, y: int, width: int) -> None:
        m, model, th = ctx.m, ctx.model, ctx.theme
        label = model.platform_name or "网页"
        if th.uppercase_eyebrow:
            label = label.upper()
        tracking = th.tracking_eyebrow
        meta = " · ".join(_meta_parts(ctx))
        meta_font = ctx.font(m.f_meta)

        if self.variant == "bili_top":
            self._draw_bili_top(ctx, layer, x, y, width)
            return

        if self.variant == "chip":
            cursor = x
            cursor += _chip(
                ctx,
                layer,
                cursor,
                y,
                label,
                fill=alpha(ctx.accent, 255),
                ink=ctx.accent_ink,
                font=ctx.font(m.f_eyebrow, bold=True),
                tracking=tracking,
                bold=True,
            )
            if model.content_type:
                cursor += m.gap_xs
                cursor += _chip(
                    ctx,
                    layer,
                    cursor,
                    y,
                    model.content_type,
                    fill=alpha(ctx.accent, ctx.pal.accent_wash),
                    ink=ctx.accent_text,
                    border=ctx.hair,
                    font=ctx.font(m.f_eyebrow),
                )
            tail = " · ".join(p for p in (model.time_text, f"在线 {model.online}" if model.online else "") if p)
            if tail:
                tw = ctx.ts.width(tail, meta_font)
                if cursor + m.gap_sm + tw <= x + width:
                    ty = y + (m.chip_h - ctx.ts.line_height(meta_font, 1.0)) // 2
                    ctx.text(layer, (x + width - tw, ty), tail, meta_font, ctx.ink_muted)
            return

        if self.variant == "plate":
            font = ctx.font(m.f_eyebrow, bold=True)
            bar_w = max(3, m.unit)
            surface.panel(layer, (x, y, x + bar_w, y + m.chip_h), 0, fill=alpha(ctx.accent, 255))
            ty = y + (m.chip_h - ctx.ts.line_height(font, 1.0)) // 2
            ctx.text(layer, (x + bar_w + m.gap_sm, ty), label, font, ctx.ink, tracking=tracking, bold=True)
            if meta:
                mw = ctx.ts.width(meta, meta_font)
                ctx.text(
                    layer,
                    (x + width - mw, y + (m.chip_h - ctx.ts.line_height(meta_font, 1.0)) // 2),
                    meta,
                    meta_font,
                    ctx.ink_muted,
                )
            surface.hairline(layer, x, y + m.chip_h + m.gap_xs, x + width, ctx.hair)
            return

        if self.variant == "bracket":
            font = ctx.font(m.f_eyebrow, bold=True)
            lh = ctx.ts.line_height(font, 1.0)
            cursor = x
            cursor += ctx.text(layer, (cursor, y), "[", font, alpha(ctx.accent, 235)) + m.gap_2xs * 2
            cursor += ctx.text(layer, (cursor, y), label, font, ctx.accent_text, tracking=tracking, bold=True)
            cursor += m.gap_2xs * 2
            cursor += ctx.text(layer, (cursor, y), "]", font, alpha(ctx.accent, 235))
            if model.content_type:
                cursor += m.gap_sm
                cursor += ctx.text(layer, (cursor, y), "/ " + model.content_type, font, ctx.ink_dim, tracking=tracking * 0.5)
            tail = model.time_text
            if tail:
                tw = ctx.ts.width(tail, meta_font)
                ctx.text(layer, (x + width - tw, y), tail, meta_font, ctx.ink_muted)
            surface.hairline(layer, x, y + lh + m.gap_xs, x + width, ctx.hair, dash=max(3, m.unit))
            return

        # rule：报章式细规则线 + 极宽字距
        font = ctx.font(m.f_eyebrow, bold=True)
        lh = ctx.ts.line_height(font, 1.0)
        row_h = max(m.unit * 2, lh)
        square = max(4, int(m.unit * 1.6))
        surface.panel(layer, (x, y + (row_h - square) // 2, x + square, y + (row_h + square) // 2), 0, fill=alpha(ctx.accent, 255))
        cursor = x + square + m.gap_sm
        cursor += ctx.text(layer, (cursor, y + (row_h - lh) // 2), label, font, ctx.ink, tracking=tracking, bold=True)
        if model.content_type:
            cursor += m.gap_sm
            ctx.text(
                layer,
                (cursor, y + (row_h - lh) // 2),
                model.content_type,
                ctx.font(m.f_eyebrow),
                ctx.ink_muted,
                tracking=tracking * 0.6,
            )
        if model.time_text:
            tw = ctx.ts.width(model.time_text, meta_font)
            ctx.text(
                layer,
                (x + width - tw, y + (row_h - ctx.ts.line_height(meta_font, 1.0)) // 2),
                model.time_text,
                meta_font,
                ctx.ink_muted,
            )
        surface.hairline(layer, x, y + row_h + m.gap_xs, x + width, alpha(ctx.ink, 150))


# ============================ 作者 ============================


def _avatar(ctx: Any, layer: Any, x: int, y: int, size: int, *, square: bool = False) -> None:
    model = ctx.model
    img = None
    if model.avatar is not None and surface.Image is not None:
        try:
            img = surface.open_image(model.avatar)
        except Exception:
            img = None
    if img is not None:
        if square:
            tile = surface.cover_fit(img, size, size)
            tile = surface.round_image(tile, max(2, ctx.m.radius_media // 2))
        else:
            tile = surface.circle_image(img, size)
        layer.alpha_composite(tile, (x, y))
    else:
        radius = max(2, ctx.m.radius_media // 2) if square else size // 2
        surface.panel(
            layer,
            (x, y, x + size, y + size),
            radius,
            fill=alpha(mix(ctx.accent, ctx.pal.surface, 0.55), 210),
            border=alpha(ctx.accent, 120),
            border_width=ctx.m.hairline,
        )
        initial = (model.author_name or model.platform_name or "?")[:1]
        font = ctx.font(max(10, int(size * 0.46)), bold=True)
        ctx.text(
            layer,
            (x + size // 2, y + size // 2),
            initial,
            font,
            ctx.accent_ink if ctx.pal.is_dark else darken(ctx.accent, 0.45),
            bold=True,
            anchor="mm",
        )
    if not square:
        surface.panel(layer, (x, y, x + size, y + size), size // 2, border=alpha(ctx.pal.surface_border, 70), border_width=ctx.m.hairline)


@dataclass
class IdentityBlock(Block):
    """作者身份。variant: avatar_left / stacked / minimal / plate / bili"""

    variant: str = "avatar_left"

    def __post_init__(self) -> None:
        self._cache = {}

    def _bili_name_font(self, ctx: Any) -> Any:
        # 参考手机端动态详情页：UP 主名是较大号的深色粗体，不是粉色
        return ctx.font(int(ctx.m.f_subtitle * 1.04), bold=True)

    def _bili_badge(self, ctx: Any, layer: Any, x: int, y: int, size: int) -> None:
        """头像右下角的蓝色认证角标（白/底色描边 + 闪电）。"""
        m = ctx.m
        bs = max(11, int(size * 0.30))
        bx1, by1 = x + size, y + size
        box = (bx1 - bs, by1 - bs, bx1, by1)
        surface.panel(
            layer,
            box,
            bs // 2,
            fill=alpha(ctx.accent_alt or ctx.accent, 255),
            border=alpha(ctx.panel_bg, 255),
            border_width=max(1, int(m.hairline * 2)),
        )
        pad = max(2, bs // 4)
        surface.glyph(
            layer,
            (box[0] + pad, box[1] + pad, box[2] - pad, box[3] - pad),
            "bolt",
            (255, 255, 255),
        )

    def _draw_bili(self, ctx: Any, layer: Any, x: int, y: int, width: int, total: int) -> None:
        """哔哩哔哩作者行：大圆头像 + 蓝色认证角标 + 深色昵称 + 灰色时间 + 粉色 UP主 药丸。"""
        m, model = ctx.m, ctx.model
        name = model.author_name or model.platform_name
        name_f = self._bili_name_font(ctx)
        meta_f = ctx.font(m.f_meta)
        name_lh = ctx.ts.line_height(name_f, 1.15)
        sub = model.time_text or model.author_handle or ""
        sub_lh = ctx.ts.line_height(meta_f, 1.25) if sub else 0

        size = m.avatar
        av_top = y + (total - size) // 2
        _avatar(ctx, layer, x, av_top, size)
        self._bili_badge(ctx, layer, x, av_top, size)

        pill_f = ctx.font(max(9, int(m.f_eyebrow * 0.92)), bold=True)
        pill_h = max(15, ctx.ts.line_height(pill_f, 1.0) + m.gap_2xs * 2)
        pill_text = "UP主"
        pill_w = ctx.ts.width(pill_text, pill_f) + m.gap_sm * 2
        right = x + width
        show_pill = width > size + pill_w + m.gap_lg * 2
        if show_pill:
            top = y + (total - pill_h) // 2
            surface.panel(
                layer,
                (right - pill_w, top, right, top + pill_h),
                pill_h // 2,
                fill=alpha(ctx.accent, 255),
            )
            ctx.text(
                layer,
                (right - pill_w // 2, top + pill_h // 2),
                pill_text,
                pill_f,
                ctx.accent_ink,
                bold=True,
                anchor="mm",
            )
            right -= pill_w + m.gap_sm

        tx = x + size + m.gap_sm
        block_h = name_lh + sub_lh
        ty = y + max(0, (total - block_h) // 2)
        avail = max(24, right - tx)
        ctx.text(layer, (tx, ty), ctx.ts.ellipsize(name, name_f, avail), name_f, ctx.ink, bold=True)
        if sub:
            ctx.text(
                layer,
                (tx, ty + name_lh),
                ctx.ts.ellipsize(sub, meta_f, avail),
                meta_f,
                ctx.ink_muted,
            )
    def _measure(self, ctx: Any, width: int) -> int:
        m = ctx.m
        name_f = ctx.font(m.f_subtitle, bold=True)
        meta_f = ctx.font(m.f_meta)
        if self.variant == "bili":
            bili_f = self._bili_name_font(ctx)
            text_h = ctx.ts.line_height(bili_f, 1.15)
            if ctx.model.time_text or ctx.model.author_handle:
                text_h += ctx.ts.line_height(meta_f, 1.25)
            return max(m.avatar, text_h)
        if self.variant == "minimal":
            lh = ctx.ts.line_height(ctx.font(m.f_meta, bold=True), 1.15)
            return max(lh, int(m.avatar_sm * 0.78))
        if self.variant == "plate":
            return max(m.avatar_sm, ctx.ts.line_height(name_f, 1.0) + ctx.ts.line_height(meta_f, 1.0)) + m.gap_sm * 2
        if self.variant == "stacked":
            h = ctx.ts.line_height(ctx.font(m.f_eyebrow, bold=True), 1.0) + m.gap_2xs
            h += ctx.ts.line_height(name_f, 1.1)
            if ctx.model.author_handle:
                h += ctx.ts.line_height(meta_f, 1.15)
            return max(h, m.avatar_sm)
        text_h = ctx.ts.line_height(name_f, 1.1)
        if ctx.model.author_handle:
            text_h += ctx.ts.line_height(meta_f, 1.2)
        return max(m.avatar, text_h)

    def draw(self, ctx: Any, layer: Any, x: int, y: int, width: int) -> None:
        m, model = ctx.m, ctx.model
        name = model.author_name or model.platform_name
        handle = model.author_handle
        name_f = ctx.font(m.f_subtitle, bold=True)
        meta_f = ctx.font(m.f_meta)
        total = self.measure(ctx, width)

        if self.variant == "bili":
            self._draw_bili(ctx, layer, x, y, width, total)
            return

        if self.variant == "minimal":
            # 报章式署名：小圆头像 + 强调色竖条 + 署名，头像让"作者"在窄栏里也立得住
            font = ctx.font(m.f_meta, bold=True)
            bar = max(3, int(m.unit * 0.8))
            lh = ctx.ts.line_height(font, 1.0)
            size = int(m.avatar_sm * 0.78)
            _avatar(ctx, layer, x, y + (total - size) // 2, size)
            bar_x = x + size + m.gap_sm
            surface.panel(
                layer,
                (bar_x, y + (total - lh) // 2, bar_x + bar, y + (total + lh) // 2),
                0,
                fill=alpha(ctx.accent, 255),
            )
            cursor = bar_x + bar + m.gap_sm
            cursor += ctx.text(layer, (cursor, y + (total - lh) // 2), name, font, ctx.ink, bold=True)
            if handle:
                text = ctx.ts.ellipsize(" / " + handle, meta_f, max(0, x + width - cursor))
                ctx.text(layer, (cursor, y + (total - ctx.ts.line_height(meta_f, 1.0)) // 2), text, meta_f, ctx.ink_muted)
            return

        if self.variant == "plate":
            surface.panel(
                layer,
                (x, y, x + width, y + total),
                m.radius_media,
                fill=alpha(ctx.pal.surface, max(10, ctx.pal.surface_alpha // 2) if ctx.pal.is_dark else 130),
                border=ctx.hair,
                border_width=m.hairline,
            )
            size = m.avatar_sm
            ax = x + m.gap_sm
            _avatar(ctx, layer, ax, y + (total - size) // 2, size, square=True)
            tx = ax + size + m.gap_sm
            avail = max(20, x + width - tx - m.gap_sm)
            label_f = ctx.font(m.f_eyebrow, bold=True)
            ty = y + m.gap_sm
            ctx.text(layer, (tx, ty), ctx.ts.ellipsize(name, name_f, avail), name_f, ctx.ink, bold=True)
            ty += ctx.ts.line_height(name_f, 1.0)
            sub = handle or (model.platform_name + " · " + model.content_type if model.content_type else model.platform_name)
            ctx.text(layer, (tx, ty), ctx.ts.ellipsize(sub, meta_f, avail), meta_f, ctx.ink_muted)
            del label_f
            return

        if self.variant == "stacked":
            label_f = ctx.font(m.f_eyebrow, bold=True)
            size = m.avatar_sm
            _avatar(ctx, layer, x + width - size, y, size)
            avail = max(20, width - size - m.gap_md)
            ty = y
            ctx.text(layer, (x, ty), "作者", label_f, ctx.accent_text, tracking=ctx.theme.tracking_eyebrow, bold=True)
            ty += ctx.ts.line_height(label_f, 1.0) + m.gap_2xs
            ctx.text(layer, (x, ty), ctx.ts.ellipsize(name, name_f, avail), name_f, ctx.ink, bold=True)
            ty += ctx.ts.line_height(name_f, 1.1)
            if handle:
                ctx.text(layer, (x, ty), ctx.ts.ellipsize(handle, meta_f, avail), meta_f, ctx.ink_muted)
            return

        # avatar_left
        size = m.avatar
        _avatar(ctx, layer, x, y + (total - size) // 2, size)
        tx = x + size + m.gap_md
        avail = max(20, x + width - tx)
        block_h = ctx.ts.line_height(name_f, 1.1) + (ctx.ts.line_height(meta_f, 1.2) if handle else 0)
        ty = y + max(0, (total - block_h) // 2)
        ctx.text(layer, (tx, ty), ctx.ts.ellipsize(name, name_f, avail), name_f, ctx.ink, bold=True)
        ty += ctx.ts.line_height(name_f, 1.1)
        if handle:
            ctx.text(layer, (tx, ty), ctx.ts.ellipsize(handle, meta_f, avail), meta_f, ctx.ink_muted)


# ============================ 标题 ============================


@dataclass
class HeadlineBlock(Block):
    """主标题。variant: display / tight / upper / bili_post"""

    variant: str = "display"
    max_lines: int = 4
    scale: float = 1.0
    overlay: bool = False

    def __post_init__(self) -> None:
        self._cache = {}
        self._lines: dict[int, tuple[list[str], Any, float, float]] = {}

    def _plan(self, ctx: Any, width: int) -> tuple[list[str], Any, float, float]:
        cached = self._lines.get(width)
        if cached is not None:
            return cached
        m, th = ctx.m, ctx.theme
        text = ctx.model.title
        base = m.f_title
        leading = m.lh_snug
        tracking = th.tracking_headline
        if self.variant == "bili_post":
            # B 站动态正文：字号只比正文略大、行距宽松，标题与首段共用同一套排版
            size = max(m.f_body, int(round(m.f_body * 1.18 * self.scale * th.headline_scale)))
            bold = ctx.model.has_real_title
            font = ctx.font(size, bold=bold)
            plan = (ctx.ts.fit(text, font, width, self.max_lines), font, m.lh_loose, 0.0)
            self._lines[width] = plan
            return plan
        if self.variant == "tight":
            leading = m.lh_tight
        elif self.variant == "upper":
            base = int(round(m.f_subtitle * 1.12))
            tracking = max(tracking, 0.6)
            leading = m.lh_snug
        size = max(m.f_body + 1, int(round(base * self.scale * th.headline_scale)))
        font = ctx.font(size, bold=True)
        lines = ctx.ts.fit(text, font, width, self.max_lines)
        # 单行很短时略微放大，保证视觉重量
        if len(lines) == 1 and self.variant != "upper":
            grown = int(round(size * 1.12))
            gf = ctx.font(grown, bold=True)
            if ctx.ts.tracked_width(lines[0], gf, tracking) <= width:
                font, size = gf, grown
        # 行数被裁掉太多时缩字号再试一次
        elif len(ctx.ts.wrap(text, font, width)) > self.max_lines and size > m.f_body + 2:
            shrunk = max(m.f_body + 1, int(round(size * 0.88)))
            font = ctx.font(shrunk, bold=True)
            lines = ctx.ts.fit(text, font, width, self.max_lines + 1)
        plan = (lines, font, leading, tracking)
        self._lines[width] = plan
        return plan

    def _measure(self, ctx: Any, width: int) -> int:
        lines, font, leading, _ = self._plan(ctx, width)
        if not lines:
            return 0
        return ctx.ts.paragraph_height(lines, font, leading)

    def draw(self, ctx: Any, layer: Any, x: int, y: int, width: int) -> None:
        lines, font, leading, tracking = self._plan(ctx, width)
        if not lines:
            return
        if self.variant == "bili_post":
            _draw_rich_para(
                ctx,
                layer,
                x,
                y,
                lines,
                font,
                ctx.ink,
                ctx.accent_alt_text,
                leading=leading,
                bold=ctx.model.has_real_title,
            )
            return
        ctx.para(layer, (x, y), lines, font, ctx.ink, leading=leading, tracking=tracking, bold=True)


# ============================ 正文 ============================


@dataclass
class BodyBlock(Block):
    """正文。variant: plain / dropcap / indent / bili"""

    variant: str = "plain"
    max_lines: int = 7

    def __post_init__(self) -> None:
        self._cache = {}
        self._plans: dict[int, dict[str, Any]] = {}

    def _plan(self, ctx: Any, width: int) -> dict[str, Any]:
        cached = self._plans.get(width)
        if cached is not None:
            return cached
        m = ctx.m
        font = ctx.font(m.f_body)
        leading = m.lh_loose if self.variant == "bili" else m.lh_normal
        text = ctx.model.body
        plan: dict[str, Any] = {"font": font, "leading": leading, "variant": self.variant}
        if self.variant == "dropcap" and len(text) > 24:
            cap = text[0]
            rest = text[1:].lstrip()
            cap_font = ctx.font(int(round(m.f_body * 3.0)), bold=True)
            cap_w = ctx.ts.width(cap, cap_font) + m.gap_sm
            step = ctx.ts.line_height(font, leading)
            cap_h = ctx.ts.line_height(cap_font, 1.0)
            indent_rows = max(2, min(3, int(math.ceil(cap_h / max(1, step)))))
            narrow = max(40, width - cap_w)
            head = ctx.ts.wrap(rest, font, narrow)[:indent_rows]
            consumed = 0
            for line in head:
                consumed += len(line)
            tail_source = rest[consumed:].lstrip()
            tail = ctx.ts.fit(tail_source, font, width, max(0, self.max_lines - len(head)))
            plan.update({"cap": cap, "cap_font": cap_font, "cap_w": cap_w, "head": head, "tail": tail, "step": step})
            plan["height"] = max(cap_h, step * len(head)) + step * len(tail)
            self._plans[width] = plan
            return plan
        indent = 0
        if self.variant == "indent":
            indent = m.gap_md
        lines = ctx.ts.fit(text, font, max(40, width - indent), self.max_lines)
        plan.update({"lines": lines, "indent": indent})
        plan["height"] = ctx.ts.paragraph_height(lines, font, leading)
        self._plans[width] = plan
        return plan

    def _measure(self, ctx: Any, width: int) -> int:
        return int(self._plan(ctx, width)["height"])

    def draw(self, ctx: Any, layer: Any, x: int, y: int, width: int) -> None:
        plan = self._plan(ctx, width)
        font, leading = plan["font"], plan["leading"]
        if plan.get("cap"):
            cap_font, cap_w, step = plan["cap_font"], plan["cap_w"], plan["step"]
            ctx.text(layer, (x, y - int(step * 0.18)), plan["cap"], cap_font, ctx.accent_text, bold=True)
            ctx.para(layer, (x + cap_w, y), plan["head"], font, ctx.ink_dim, leading=leading)
            ty = y + max(ctx.ts.line_height(cap_font, 1.0), step * len(plan["head"]))
            ctx.para(layer, (x, ty), plan["tail"], font, ctx.ink_dim, leading=leading)
            return
        lines = plan["lines"]
        indent = plan["indent"]
        if self.variant == "bili":
            _draw_rich_para(ctx, layer, x, y, lines, font, ctx.ink, ctx.accent_alt_text, leading=leading)
            return
        if indent:
            height = max(1, plan["height"])
            surface.vline(layer, x + max(1, ctx.m.unit // 2), y + 1, y + height - 1, alpha(ctx.accent, 170), width=max(2, ctx.m.unit // 2))
        ctx.para(layer, (x + indent, y), lines, font, ctx.ink_dim, leading=leading)


# ============================ 媒体 ============================


def _pick_mode(count: int, items: Sequence[MediaItem], forced: str | None) -> str:
    if count <= 0:
        return "none"
    if forced == "mosaic":
        if count == 1:
            return "hero"
        if count == 2:
            return "duo"
        if count == 4:
            return "quad"
        return "grid3" if count >= 3 else "duo"
    if count == 1:
        return "hero"
    if items and items[0].is_video:
        return "filmstrip"
    if count == 2:
        return "duo"
    if count == 3:
        return "trio_feature" if items[0].is_wide else "trio_row"
    if count == 4:
        return "quad"
    return "grid3"


def _row_plan(count: int, max_cols: int) -> list[int]:
    """把 count 张图切成每行不超过 max_cols 的若干行，行数取最少、各行张数尽量均衡。

    少的一行放在前面（单元更大），形成"先大后小"的视觉层次，且末行永不留空洞。
    """
    if count <= 0:
        return []
    if count <= max_cols:
        return [count]
    rows = int(math.ceil(count / max_cols))
    base, extra = divmod(count, rows)
    return [base + (1 if index >= rows - extra else 0) for index in range(rows)]


def _cell_aspect(items: Sequence[MediaItem], low: float, high: float) -> float:
    """按素材真实宽高比的中位数推导网格单元宽高比，避免死板方格造成大片衬底。"""
    values = [float(getattr(item, "aspect", 0.0) or 0.0) for item in items]
    values = [v for v in values if v > 0.05]
    if not values:
        return max(low, min(high, 1.0))
    values.sort()
    half = len(values) // 2
    mid = values[half] if len(values) % 2 else (values[half - 1] + values[half]) / 2.0
    return max(low, min(high, mid))


def _boxes(
    mode: str,
    count: int,
    width: int,
    gap: int,
    items: Sequence[MediaItem],
    scale: float,
    *,
    force_aspect: float | None = None,
) -> tuple[list[tuple[int, int, int, int]], int]:
    """返回相对 (0,0) 的贴片框与总高度。

    force_aspect 不为空时，网格单元强制使用该宽高比（哔哩哔哩九宫格用 1.0 的正方格），
    而不是按素材真实比例推导。此时 scale 不再参与高度计算——scale 是"整体缩放"旋钮，
    只压高度会把指定的宽高比拉扁（信息流布局 scale=0.88 会让正方格变成 227x200）。
    """
    boxes: list[tuple[int, int, int, int]] = []
    if mode == "none" or count <= 0 or width <= 0:
        return boxes, 0
    if mode == "hero":
        aspect = items[0].aspect if items else 1.0
        raw = width / max(0.2, aspect)
        h = int(round(max(width * 0.46, min(width * 1.12, raw)) * scale))
        boxes.append((0, 0, width, h))
        return boxes, h
    if mode == "duo":
        w = (width - gap) // 2
        if force_aspect:
            h = max(1, int(round(w / force_aspect)))
        else:
            avg = sum(i.aspect for i in items[:2]) / max(1, len(items[:2]))
            h = int(round((w / max(0.5, min(1.9, avg))) * scale))
            h = max(int(w * 0.62), min(int(w * 1.34), h))
        boxes.append((0, 0, w, h))
        boxes.append((w + gap, 0, w + gap + (width - gap - w), h))
        return boxes, h
    if mode == "trio_feature":
        big_w = int(round((width - gap) * 0.62))
        small_w = width - gap - big_w
        h = int(round(width * 0.52 * scale))
        small_h = (h - gap) // 2
        boxes.append((0, 0, big_w, h))
        boxes.append((big_w + gap, 0, big_w + gap + small_w, small_h))
        boxes.append((big_w + gap, small_h + gap, big_w + gap + small_w, h))
        return boxes, h
    if mode == "trio_row":
        w = (width - gap * 2) // 3
        if force_aspect:
            h = max(1, int(round(w / force_aspect)))
        else:
            h = int(round((w / _cell_aspect(items[:3], 0.82, 1.52)) * scale))
        for i in range(3):
            left = i * (w + gap)
            right = width if i == 2 else left + w
            boxes.append((left, 0, right, h))
        return boxes, h
    if mode == "quad":
        w = (width - gap) // 2
        if force_aspect:
            h = max(1, int(round(w / force_aspect)))
        else:
            h = int(round((w / _cell_aspect(items[:4], 0.88, 1.62)) * scale))
        for i in range(4):
            col, row = i % 2, i // 2
            left = col * (w + gap)
            right = width if col == 1 else left + w
            top = row * (h + gap)
            boxes.append((left, top, right, top + h))
        return boxes, h * 2 + gap
    if mode == "filmstrip":
        hero_aspect = items[0].aspect if items else 1.6
        hero_h = int(round(max(width * 0.44, min(width * 0.72, width / max(0.5, hero_aspect))) * scale))
        boxes.append((0, 0, width, hero_h))
        rest = count - 1
        top = hero_h + gap if rest else hero_h
        for cells in _row_plan(rest, 4):
            tw = (width - gap * (cells - 1)) // cells
            th = max(1, int(round(tw * 0.72)))
            for col in range(cells):
                left = col * (tw + gap)
                right = width if col == cells - 1 else left + tw
                boxes.append((left, top, right, top + th))
            top += th + gap
        return boxes, max(hero_h, top - gap if rest else hero_h)
    # grid3：以 3 列为基准的齐边网格。末行不足 3 张时把该行单元拉宽填满整幅宽度，
    # 绝不留空洞格子；所有单元共用同一宽高比，因此行高随该行张数变化，形成层次。
    if force_aspect:
        # 固定 3 列等比方格（哔哩哔哩九宫格）：单元尺寸恒定，末行不拉伸
        cell_w = (width - gap * 2) // 3
        row_h = max(1, int(round(cell_w / force_aspect)))
        for index in range(count):
            col, row = index % 3, index // 3
            left = col * (cell_w + gap)
            right = width if col == 2 else left + cell_w
            top = row * (row_h + gap)
            boxes.append((left, top, right, top + row_h))
        rows = (count + 2) // 3
        return boxes, rows * row_h + gap * max(0, rows - 1)
    row_plan = _row_plan(count, 3)
    aspect = _cell_aspect(items[:count], 0.84, 1.46)
    top = 0
    for cells in row_plan:
        cell_w = (width - gap * (cells - 1)) // cells
        row_h = max(1, int(round((cell_w / aspect) * scale)))
        for col in range(cells):
            left = col * (cell_w + gap)
            right = width if col == cells - 1 else left + cell_w
            boxes.append((left, top, right, top + row_h))
        top += row_h + gap
    return boxes, max(0, top - gap)


def _tile_image(ctx: Any, item: MediaItem, w: int, h: int, *, letterbox: bool = False) -> Any:
    """把素材填进给定方框。

    只有 letterbox=True 才允许"毛玻璃衬底 + 等比内缩"的完整展示，且只留给主视觉格子；
    网格里的次要缩略图一律裁切填满——小格子做内缩只会露出一条突兀的分层带。
    """
    if surface.Image is None or w <= 0 or h <= 0:
        return None
    try:
        img = surface.open_image(item.path)
    except Exception:
        return None
    box_aspect = w / max(1, h)
    try:
        real = img.width / max(1, img.height)
    except Exception:
        real = box_aspect
    # 配置要求"完整显示封面"时永不裁切
    if getattr(ctx, "cover_full_size", False):
        return surface.blur_backdrop_fit(img, w, h, blur=max(8, ctx.m.gap_lg), dim=70 if ctx.pal.is_dark else 40)
    if letterbox and box_aspect > 0 and abs(real - box_aspect) / box_aspect > 0.52:
        return surface.blur_backdrop_fit(img, w, h, blur=max(8, ctx.m.gap_lg), dim=70 if ctx.pal.is_dark else 40)
    return surface.cover_fit(img, w, h)


def _play_badge(ctx: Any, layer: Any, box: tuple[int, int, int, int]) -> None:
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    r = max(14, int(min(x1 - x0, y1 - y0) * 0.11))
    surface.panel(layer, (cx - r, cy - r, cx + r, cy + r), r, fill=(255, 255, 255, 40))
    surface.glow_ring(layer, (cx, cy), r, (255, 255, 255), 110, width=max(1, ctx.m.hairline + 1))
    d = ctx.draw_for(layer)
    s = int(r * 0.52)
    d.polygon(
        [(cx - s // 2, cy - s), (cx - s // 2, cy + s), (cx + int(s * 0.92), cy)],
        fill=(255, 255, 255, 235),
    )


def _corner_tag(ctx: Any, layer: Any, box: tuple[int, int, int, int], text: str, *, right: bool = True, strong: bool = False) -> None:
    if not text:
        return
    m = ctx.m
    font = ctx.font(m.f_caption, bold=True)
    lh = ctx.ts.line_height(font, 1.0)
    pad = max(4, m.gap_2xs * 2)
    w = ctx.ts.width(text, font) + pad * 2
    h = lh + pad
    x1 = box[2] - m.gap_xs
    y1 = box[3] - m.gap_xs
    x0 = x1 - w if right else box[0] + m.gap_xs
    if not right:
        x1 = x0 + w
    surface.panel(
        layer,
        (x0, y1 - h, x1, y1),
        max(3, m.radius_media // 3),
        fill=alpha(ctx.accent, 235) if strong else (10, 12, 18, 190),
    )
    ctx.text(layer, (x0 + pad, y1 - h + pad // 2), text, font, ctx.accent_ink if strong else (255, 255, 255), bold=True)


def _bili_cover_overlay(ctx: Any, layer: Any, box: tuple[int, int, int, int], item: MediaItem, radius: int) -> None:
    """哔哩哔哩封面浮层：底部渐变 + 左下播放/弹幕图标 + 右下时长（纯白字，不用胶囊）。"""
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    if bw <= 0 or bh <= 0:
        return
    m = ctx.m
    font = ctx.font(max(10, int(m.f_caption * 1.04)))
    lh = ctx.ts.line_height(font, 1.0)
    band = min(bh, max(lh + m.gap_sm * 2, int(bh * 0.3)))
    strip = surface.scrim((bw, band), (0, 0, 0), top_alpha=0, bottom_alpha=158, curve=1.4)
    if radius > 0:
        strip = surface.round_image(strip, radius, corners=(False, False, True, True))
    layer.alpha_composite(strip, (x0, y1 - band))
    white: RGB = (255, 255, 255)
    ty = y1 - m.gap_xs - lh
    cursor = x0 + m.gap_xs
    icon = max(9, int(lh * 0.88))
    for kind, value in _cover_meta(ctx):
        gy = ty + max(0, (lh - icon) // 2)
        surface.glyph(layer, (cursor, gy, cursor + icon, gy + icon), kind, (255, 255, 255, 232))
        cursor += icon + m.gap_2xs
        cursor += ctx.text(layer, (cursor, ty), value, font, white)
        cursor += m.gap_sm
    if item.duration:
        dw = ctx.ts.width(item.duration, font)
        dx = x1 - m.gap_xs - dw
        if dx > cursor:
            ctx.text(layer, (dx, ty), item.duration, font, white)


@dataclass
class MediaBlock(Block):
    """媒体网格。variant: editorial / framed / window / feed / mosaic / bili"""

    variant: str = "editorial"

    def __post_init__(self) -> None:
        self._cache = {}
        self._plans: dict[int, dict[str, Any]] = {}

    def _plan(self, ctx: Any, width: int) -> dict[str, Any]:
        cached = self._plans.get(width)
        if cached is not None:
            return cached
        m, model = ctx.m, ctx.model
        items = model.media
        gap = m.media_gap
        mat = 0
        caption_h = 0
        if self.variant == "framed":
            mat = max(m.gap_md, int(m.gap_lg * 0.9))
            caption_font = ctx.font(m.f_caption)
            caption_h = ctx.ts.line_height(caption_font, 1.0) + m.gap_xs
            gap = max(gap, m.gap_sm)
        elif self.variant == "window":
            gap = max(gap, m.gap_sm)
        elif self.variant in ("feed", "bili"):
            gap = max(3, m.media_gap - 2)
        inner = max(40, width - mat * 2)
        mode = _pick_mode(len(items), items, ctx.layout.media_mode)
        boxes, height = _boxes(
            mode,
            len(items),
            inner,
            gap,
            items,
            ctx.layout.media_scale,
            force_aspect=1.0 if self.variant == "bili" else None,
        )
        if self.variant == "bili" and boxes and model.hero_is_video and mode in ("hero", "filmstrip"):
            # 哔哩哔哩的视频封面永远是 16:9，强制首图比例并整体下移后续贴片
            bx0, by0, bx1, by1 = boxes[0]
            target_h = max(40, int(round((bx1 - bx0) * 9.0 / 16.0 * ctx.layout.media_scale)))
            delta = target_h - (by1 - by0)
            if delta:
                boxes = [(bx0, by0, bx1, by0 + target_h)] + [
                    (b[0], b[1] + delta, b[2], b[3] + delta) for b in boxes[1:]
                ]
                height = max(target_h, height + delta)
        total = height + mat * 2 + caption_h
        plan = {"mode": mode, "boxes": boxes, "inner": inner, "mat": mat, "height": total, "caption_h": caption_h, "grid_h": height}
        self._plans[width] = plan
        return plan

    def _measure(self, ctx: Any, width: int) -> int:
        if not ctx.model.media:
            return 0
        return int(self._plan(ctx, width)["height"])

    def draw(self, ctx: Any, layer: Any, x: int, y: int, width: int) -> None:
        model = ctx.model
        if not model.media:
            return
        plan = self._plan(ctx, width)
        m, pal = ctx.m, ctx.pal
        mat = plan["mat"]
        boxes = plan["boxes"]
        radius = m.radius_media
        if self.variant == "window":
            radius = max(2, m.radius_media // 4)
        elif self.variant == "framed":
            radius = 0
        elif self.variant == "feed":
            radius = max(4, int(m.radius_media * 0.7))
        elif self.variant == "bili":
            radius = max(3, m.radius_media)

        if self.variant == "framed":
            frame_box = (x, y, x + width, y + plan["grid_h"] + mat * 2)
            surface.panel(
                layer,
                frame_box,
                max(2, m.radius_media // 4),
                fill=alpha(pal.media_mat, 255),
                border=alpha(pal.media_edge, pal.media_edge_alpha),
                border_width=m.border,
                shadow_alpha=int(pal.shadow_alpha * 0.7),
                shadow_blur=m.shadow_blur,
                shadow_offset=(0, max(2, m.unit)),
            )

        ox, oy = x + mat, y + mat
        shown = len(boxes)
        for index, box in enumerate(boxes):
            if index >= len(model.media):
                break
            item = model.media[index]
            bx0, by0, bx1, by1 = box[0] + ox, box[1] + oy, box[2] + ox, box[3] + oy
            bw, bh = bx1 - bx0, by1 - by0
            if bw <= 0 or bh <= 0:
                continue
            if self.variant in ("editorial", "feed", "mosaic", "bili"):
                surface.panel(
                    layer,
                    (bx0, by0, bx1, by1),
                    radius,
                    fill=alpha(pal.media_mat, 255),
                    shadow_alpha=int(pal.shadow_alpha * 0.5) if self.variant == "editorial" else 0,
                    shadow_blur=max(4, m.shadow_blur // 2),
                    shadow_offset=(0, max(2, m.unit)),
                )
            else:
                surface.panel(layer, (bx0, by0, bx1, by1), radius, fill=alpha(pal.media_mat, 255))
            # 只有主视觉格子（单图 / 视频封面 / 三图特写的首图）才允许等比完整展示
            feature = index == 0 and plan["mode"] in ("hero", "filmstrip", "trio_feature")
            tile = _tile_image(ctx, item, bw, bh, letterbox=feature)
            if tile is not None:
                if radius > 0:
                    tile = surface.round_image(tile, radius)
                layer.alpha_composite(tile, (bx0, by0))
            else:
                font = ctx.font(m.f_caption)
                ctx.text(layer, ((bx0 + bx1) // 2, (by0 + by1) // 2), "图片不可用", font, ctx.ink_muted, anchor="mm")
            # 贴片边线
            surface.panel(
                layer,
                (bx0, by0, bx1, by1),
                radius,
                border=alpha(pal.media_edge, pal.media_edge_alpha),
                border_width=m.hairline,
            )
            if self.variant == "window":
                surface.corner_marks(
                    layer,
                    (bx0, by0, bx1 - 1, by1 - 1),
                    alpha(ctx.accent, 210),
                    max(8, m.gap_md),
                    width=max(1, m.border),
                )
            # hero 上的播放按钮 / 时长
            if index == 0 and item.is_video:
                if self.variant == "bili":
                    _bili_cover_overlay(ctx, layer, (bx0, by0, bx1, by1), item, radius)
                if ctx.show_play_button:
                    _play_badge(ctx, layer, (bx0, by0, bx1, by1))
                if item.duration and self.variant != "bili":
                    _corner_tag(ctx, layer, (bx0, by0, bx1, by1), item.duration)
            if ctx.theme.caption_numbering and shown > 1:
                _corner_tag(
                    ctx,
                    layer,
                    (bx0, by0, bx1, by1),
                    f"{index + 1:02d}/{model.total_media:02d}",
                    right=False,
                )
            # 溢出角标
            if index == shown - 1 and model.total_media > len(model.media):
                extra = model.total_media - len(model.media)
                layer.alpha_composite(
                    surface.round_image(surface.scrim((bw, bh), (0, 0, 0), top_alpha=110, bottom_alpha=170, curve=1.0), radius),
                    (bx0, by0),
                )
                font = ctx.font(max(m.f_subtitle, int(bh * 0.22)), bold=True)
                ctx.text(layer, ((bx0 + bx1) // 2, (by0 + by1) // 2), f"+{extra}", font, (255, 255, 255), bold=True, anchor="mm")

        if plan["caption_h"] and self.variant == "framed":
            font = ctx.font(m.f_caption)
            label = model.content_type or "图像"
            caption = f"{label} · 共 {model.total_media} 项" if model.total_media > 1 else label
            cy = y + plan["grid_h"] + mat * 2 + ctx.m.gap_xs // 2
            ctx.text(layer, (x, cy), caption, font, ctx.ink_muted, tracking=1.4)
            if model.duration_text:
                tw = ctx.ts.width(model.duration_text, font)
                ctx.text(layer, (x + width - tw, cy), model.duration_text, font, ctx.ink_muted)


# ============================ 数据 ============================


def _stat_value_number(value: str) -> float:
    text = str(value or "").strip()
    mult = 1.0
    if text.endswith("万"):
        mult, text = 10000.0, text[:-1]
    elif text.endswith("亿"):
        mult, text = 100000000.0, text[:-1]
    elif text.lower().endswith("k"):
        mult, text = 1000.0, text[:-1]
    elif text.lower().endswith("m"):
        mult, text = 1000000.0, text[:-1]
    try:
        return float(text) * mult
    except ValueError:
        return 0.0


@dataclass
class StatsBlock(Block):
    """互动数据。variant: chips / ledger / bars / inline / bili"""

    variant: str = "chips"

    def __post_init__(self) -> None:
        self._cache = {}
        self._plans: dict[int, dict[str, Any]] = {}

    def _plan(self, ctx: Any, width: int) -> dict[str, Any]:
        cached = self._plans.get(width)
        if cached is not None:
            return cached
        m = ctx.m
        stats = ctx.model.stats
        label_f = ctx.font(m.f_label)
        value_f = ctx.font(m.f_value, bold=True)
        plan: dict[str, Any] = {"label_f": label_f, "value_f": value_f}
        if self.variant == "bili":
            font = ctx.font(m.f_meta)
            icon = max(10, int(m.f_meta * 1.15))
            row_h = max(ctx.ts.line_height(font, 1.0), icon)
            gap = m.gap_lg
            rows: list[list[tuple[str, str, int]]] = [[]]
            used = 0
            for label, value in stats:
                text = value or label
                w = icon + m.gap_2xs * 2 + ctx.ts.width(text, font)
                if used and used + gap + w > width:
                    rows.append([])
                    used = 0
                rows[-1].append((_glyph_kind(label), text, w))
                used += (gap if used else 0) + w
            plan.update({"font": font, "icon": icon, "row_h": row_h, "rows": rows, "gap": gap})
            plan["height"] = len(rows) * row_h + max(0, len(rows) - 1) * m.gap_xs
        elif self.variant == "inline":
            label_i = ctx.font(m.f_meta)
            value_i = ctx.font(m.f_meta, bold=True)
            sep = "  ·  "
            sep_w = ctx.ts.width(sep, label_i)
            row_h = max(ctx.ts.line_height(label_i, 1.0), ctx.ts.line_height(value_i, 1.0))
            irows: list[list[tuple[str, str, int, int]]] = [[]]
            used = 0
            for label, value in stats:
                lw = ctx.ts.width(label, label_i)
                vw = ctx.ts.width(value, value_i) if value else 0
                w = lw + (m.gap_2xs * 2 + vw if value else 0)
                if used and used + sep_w + w > width:
                    irows.append([])
                    used = 0
                irows[-1].append((label, value, lw, vw))
                used += (sep_w if used else 0) + w
            plan.update(
                {
                    "label_i": label_i,
                    "value_i": value_i,
                    "sep": sep,
                    "sep_w": sep_w,
                    "row_h": row_h,
                    "rows": irows,
                }
            )
            plan["height"] = len(irows) * row_h + max(0, len(irows) - 1) * m.gap_xs
        elif self.variant == "bars":
            row_h = max(ctx.ts.line_height(value_f, 1.0), ctx.ts.line_height(label_f, 1.0)) + max(2, m.unit)
            plan["row_h"] = row_h
            plan["height"] = row_h * len(stats) + m.gap_2xs * max(0, len(stats) - 1)
        elif self.variant == "ledger":
            cell_h = ctx.ts.line_height(value_f, 1.0) + ctx.ts.line_height(label_f, 1.2) + m.gap_sm * 2
            # 分栏账本：窄栏（如杂志布局的侧栏）里 7 个统计项挤成一行时，
            # 每格只剩几十像素，数值会被 ellipsize 成空串。先按最宽内容
            # 算出能放几列，放不下就换行，保证每个数值都完整可读。
            need = m.gap_sm * 2
            for label, value in stats:
                need = max(
                    need,
                    max(ctx.ts.width(value, value_f), ctx.ts.width(label, label_f)) + m.gap_sm * 2,
                )
            cols = max(1, min(len(stats) or 1, max(1, width // max(1, need))))
            rows_count = max(1, math.ceil((len(stats) or 1) / cols))
            plan["cell_h"] = cell_h
            plan["cols"] = cols
            plan["rows_count"] = rows_count
            plan["height"] = rows_count * cell_h + max(0, rows_count - 1) * m.gap_sm + 2
        else:  # chips
            plan["chip_h"] = max(m.chip_h, ctx.ts.line_height(value_f, 1.0) + m.gap_sm)
            # 换行排布
            font_l, font_v = label_f, value_f
            gap = ctx.m.gap_xs
            pad = ctx.m.gap_sm
            rows: list[list[tuple[str, str, int]]] = [[]]
            used = 0
            for label, value in stats:
                w = pad * 2 + ctx.ts.width(label, font_l) + ctx.m.gap_2xs * 2 + ctx.ts.width(value, font_v)
                if used and used + gap + w > width:
                    rows.append([])
                    used = 0
                rows[-1].append((label, value, w))
                used += (gap if used else 0) + w
            plan["rows"] = rows
            plan["height"] = len(rows) * plan["chip_h"] + max(0, len(rows) - 1) * gap
        self._plans[width] = plan
        return plan

    def _measure(self, ctx: Any, width: int) -> int:
        if not ctx.model.stats:
            return 0
        return int(self._plan(ctx, width)["height"])

    def draw(self, ctx: Any, layer: Any, x: int, y: int, width: int) -> None:
        stats = ctx.model.stats
        if not stats:
            return
        m, pal = ctx.m, ctx.pal
        plan = self._plan(ctx, width)
        label_f, value_f = plan["label_f"], plan["value_f"]

        if self.variant == "bili":
            font, icon = plan["font"], plan["icon"]
            row_h, gap = plan["row_h"], plan["gap"]
            lh = ctx.ts.line_height(font, 1.0)
            for row_index, row in enumerate(plan["rows"]):
                top = y + row_index * (row_h + m.gap_xs)
                cursor = x
                for kind, text, _w in row:
                    gy = top + max(0, (row_h - icon) // 2)
                    surface.glyph(layer, (cursor, gy, cursor + icon, gy + icon), kind, alpha(ctx.ink_muted, 255))
                    cursor += icon + m.gap_2xs * 2
                    ctx.text(layer, (cursor, top + max(0, (row_h - lh) // 2)), text, font, ctx.ink_dim)
                    cursor += ctx.ts.width(text, font) + gap
            return

        if self.variant == "inline":
            label_i, value_i = plan["label_i"], plan["value_i"]
            sep, sep_w, row_h = plan["sep"], plan["sep_w"], plan["row_h"]
            for row_index, row in enumerate(plan["rows"]):
                top = y + row_index * (row_h + m.gap_xs)
                cursor = x
                for index, (label, value, lw, vw) in enumerate(row):
                    if index:
                        ctx.text(layer, (cursor, top), sep, label_i, alpha(ctx.ink_muted, 140))
                        cursor += sep_w
                    ctx.text(layer, (cursor, top), label, label_i, ctx.ink_muted)
                    cursor += lw
                    if value:
                        cursor += m.gap_2xs * 2
                        ctx.text(layer, (cursor, top), value, value_i, ctx.ink, bold=True)
                        cursor += vw
            return

        if self.variant == "bars":
            peak = max(_stat_value_number(v) for _, v in stats) or 1.0
            row_h = plan["row_h"]
            label_w = max(ctx.ts.width(label, label_f) for label, _ in stats) + m.gap_sm
            value_w = max(ctx.ts.width(value, value_f) for _, value in stats) + m.gap_sm
            bar_x = x + label_w
            bar_w = max(20, width - label_w - value_w)
            for index, (label, value) in enumerate(stats):
                top = y + index * (row_h + m.gap_2xs)
                ctx.text(layer, (x, top + (row_h - ctx.ts.line_height(label_f, 1.0)) // 2), label, label_f, ctx.ink_muted)
                raw = (_stat_value_number(value) / peak) if peak else 0.0
                ratio = min(1.0, max(0.15, math.sqrt(max(0.0, raw))))
                track_h = max(3, int(m.unit * 1.2))
                ty = top + (row_h - track_h) // 2
                surface.panel(layer, (bar_x, ty, bar_x + bar_w, ty + track_h), track_h // 2, fill=alpha(ctx.ink, 34))
                surface.panel(
                    layer,
                    (bar_x, ty, bar_x + max(track_h, int(bar_w * ratio)), ty + track_h),
                    track_h // 2,
                    fill=alpha(ctx.accent, 240),
                )
                vw = ctx.ts.width(value, value_f)
                ctx.text(
                    layer,
                    (x + width - vw, top + (row_h - ctx.ts.line_height(value_f, 1.0)) // 2),
                    value,
                    value_f,
                    ctx.ink,
                    bold=True,
                )
            return

        if self.variant == "ledger":
            cell_h = plan["cell_h"]
            cols = int(plan["cols"])
            step = cell_h + m.gap_sm
            surface.hairline(layer, x, y, x + width, alpha(ctx.ink, 150))
            cell_w = max(1, width // cols)
            for index, (label, value) in enumerate(stats):
                row_index, col_index = divmod(index, cols)
                top = y + row_index * step
                cx = x + col_index * cell_w
                if col_index:
                    surface.vline(layer, cx, top + m.gap_xs, top + cell_h - m.gap_xs, ctx.hair)
                inner = cell_w - m.gap_sm
                vy = top + m.gap_sm
                ctx.text(layer, (cx + m.gap_sm, vy), ctx.ts.ellipsize(value, value_f, inner), value_f, ctx.ink, bold=True)
                vy += ctx.ts.line_height(value_f, 1.0)
                ctx.text(layer, (cx + m.gap_sm, vy), ctx.ts.ellipsize(label, label_f, inner), label_f, ctx.ink_muted, tracking=1.2)
            for row_index in range(int(plan["rows_count"])):
                surface.hairline(layer, x, y + row_index * step + cell_h + 1, x + width, ctx.hair)
            return

        # chips
        chip_h = plan["chip_h"]
        gap = m.gap_xs
        pad = m.gap_sm
        for row_index, row in enumerate(plan["rows"]):
            top = y + row_index * (chip_h + gap)
            cursor = x
            for label, value, w in row:
                surface.panel(
                    layer,
                    (cursor, top, cursor + w, top + chip_h),
                    chip_h // 2,
                    fill=alpha(pal.surface, 22 if pal.is_dark else 150),
                    border=ctx.hair,
                    border_width=m.hairline,
                )
                lx = cursor + pad
                ly = top + (chip_h - ctx.ts.line_height(label_f, 1.0)) // 2
                ctx.text(layer, (lx, ly), label, label_f, ctx.ink_muted)
                vx = lx + ctx.ts.width(label, label_f) + m.gap_2xs * 2
                vy = top + (chip_h - ctx.ts.line_height(value_f, 1.0)) // 2
                ctx.text(layer, (vx, vy), value, value_f, ctx.ink, bold=True)
                cursor += w + gap


# ============================ IP 属地 / 页签 ============================


@dataclass
class IpNoteBlock(Block):
    """哔哩哔哩式的补充说明行：把播放、弹幕、投币等次要数据压成一行灰色小字。

    没有可显示内容时高度为 0 且完全不绘制，避免版面出现空洞。
    """

    variant: str = "bili"
    #: 只放"消费类"指标；点赞/投币/收藏/转发/评论 留给底部操作栏，避免同一数字出现两次。
    kinds: tuple[str, ...] = ("play", "danmaku", "eye", "clock")

    def __post_init__(self) -> None:
        self._cache = {}
        self._plans: dict[int, dict[str, Any]] = {}

    def _plan(self, ctx: Any, width: int) -> dict[str, Any]:
        cached = self._plans.get(width)
        if cached is not None:
            return cached
        m = ctx.m
        font = ctx.font(m.f_caption)
        parts = [f"{label} {value}".strip() for label, value in _stat_pairs(ctx, self.kinds)]
        text = ctx.ts.ellipsize(" · ".join(parts), font, max(20, width)) if parts else ""
        plan = {
            "font": font,
            "text": text,
            "height": (ctx.ts.line_height(font, 1.0) + m.gap_xs) if text else 0,
        }
        self._plans[width] = plan
        return plan

    def _measure(self, ctx: Any, width: int) -> int:
        return int(self._plan(ctx, width)["height"])

    def draw(self, ctx: Any, layer: Any, x: int, y: int, width: int) -> None:
        plan = self._plan(ctx, width)
        if not plan["text"]:
            return
        ctx.text(layer, (x, y), plan["text"], plan["font"], ctx.ink_muted)


@dataclass
class TabBarBlock(Block):
    """哔哩哔哩动态详情页的页签条：选中的"评论 N"带粉色下划线，右邻"赞和转发 M"。"""

    variant: str = "bili"

    def __post_init__(self) -> None:
        self._cache = {}
        self._plans: dict[int, dict[str, Any]] = {}

    def _labels(self, ctx: Any) -> tuple[str, str]:
        model = ctx.model
        count = _stat_value(ctx, ("comment",))
        if not count and model.comments:
            count = str(len(model.comments))
        active = f"评论 {count}".strip() if count else "评论"
        likes = _stat_value(ctx, ("like",)) or _stat_value(ctx, ("share",))
        rest = f"赞和转发 {likes}".strip() if likes else "赞和转发"
        return active, rest

    def _plan(self, ctx: Any, width: int) -> dict[str, Any]:
        cached = self._plans.get(width)
        if cached is not None:
            return cached
        m = ctx.m
        active_f = ctx.font(int(m.f_meta * 1.06), bold=True)
        rest_f = ctx.font(int(m.f_meta * 1.06))
        row = max(ctx.ts.line_height(active_f, 1.0), ctx.ts.line_height(rest_f, 1.0))
        rule = max(2, int(m.unit * 0.6))
        plan = {
            "active_f": active_f,
            "rest_f": rest_f,
            "row": row,
            "rule": rule,
            "height": row + m.gap_2xs + rule + m.gap_sm + 1,
        }
        self._plans[width] = plan
        return plan

    def _measure(self, ctx: Any, width: int) -> int:
        return int(self._plan(ctx, width)["height"])

    def draw(self, ctx: Any, layer: Any, x: int, y: int, width: int) -> None:
        m = ctx.m
        plan = self._plan(ctx, width)
        active, rest = self._labels(ctx)
        row, rule = plan["row"], plan["rule"]
        aw = ctx.text(layer, (x, y), active, plan["active_f"], ctx.accent_text, bold=True)
        under_y = y + row + m.gap_2xs
        surface.panel(
            layer,
            (x, under_y, x + max(8, aw), under_y + rule),
            rule // 2,
            fill=alpha(ctx.accent, 255),
        )
        cursor = x + aw + m.gap_xl
        if cursor + ctx.ts.width(rest, plan["rest_f"]) <= x + width:
            ctx.text(layer, (cursor, y), rest, plan["rest_f"], ctx.ink_muted)
        surface.hairline(layer, x, under_y + rule + m.gap_sm, x + width, ctx.hair)


# ============================ 转发引用 ============================


@dataclass
class QuoteBlock(Block):
    """转发 / 引用原文。"""

    variant: str = "panel"

    def __post_init__(self) -> None:
        self._cache = {}
        self._plans: dict[int, dict[str, Any]] = {}

    def _plan(self, ctx: Any, width: int) -> dict[str, Any]:
        cached = self._plans.get(width)
        if cached is not None:
            return cached
        m = ctx.m
        quote: QuoteItem | None = ctx.model.quote
        plan: dict[str, Any] = {"height": 0}
        if quote is None:
            self._plans[width] = plan
            return plan
        pad = m.gap_md
        inner = max(40, width - pad * 2 - m.gap_sm)
        label_f = ctx.font(m.f_eyebrow, bold=True)
        author_f = ctx.font(m.f_meta, bold=True)
        text_f = ctx.font(m.f_quote)
        h = pad
        h += ctx.ts.line_height(label_f, 1.0) + m.gap_2xs
        if quote.author:
            h += ctx.ts.line_height(author_f, 1.15)
        body = " ".join(p for p in (quote.title, quote.text) if p).strip()
        lines = ctx.ts.fit(body, text_f, inner, 3) if body else []
        h += ctx.ts.paragraph_height(lines, text_f, m.lh_normal)
        h += pad
        plan.update({"pad": pad, "label_f": label_f, "author_f": author_f, "text_f": text_f, "lines": lines, "height": h})
        self._plans[width] = plan
        return plan

    def _measure(self, ctx: Any, width: int) -> int:
        return int(self._plan(ctx, width)["height"])

    def draw(self, ctx: Any, layer: Any, x: int, y: int, width: int) -> None:
        quote = ctx.model.quote
        if quote is None:
            return
        plan = self._plan(ctx, width)
        m, pal = ctx.m, ctx.pal
        pad = plan["pad"]
        height = plan["height"]
        if self.variant == "quote":
            surface.vline(layer, x + max(1, m.unit // 2), y, y + height, alpha(ctx.accent, 190), width=max(2, m.unit // 2))
        else:
            surface.panel(
                layer,
                (x, y, x + width, y + height),
                m.radius_media,
                fill=alpha(pal.surface, 20 if pal.is_dark else 140),
                border=ctx.hair,
                border_width=m.hairline,
            )
            surface.panel(layer, (x, y + m.gap_sm, x + max(2, m.unit), y + height - m.gap_sm), 0, fill=alpha(ctx.accent, 220))
        tx = x + pad + m.gap_sm
        ty = y + pad
        ctx.text(layer, (tx, ty), "转发原文", plan["label_f"], ctx.accent_text, tracking=ctx.theme.tracking_eyebrow, bold=True)
        ty += ctx.ts.line_height(plan["label_f"], 1.0) + m.gap_2xs
        avail = max(20, x + width - tx - pad)
        if quote.author:
            ctx.text(layer, (tx, ty), ctx.ts.ellipsize("@" + quote.author, plan["author_f"], avail), plan["author_f"], ctx.ink, bold=True)
            ty += ctx.ts.line_height(plan["author_f"], 1.15)
        ctx.para(layer, (tx, ty), plan["lines"], plan["text_f"], ctx.ink_dim, leading=m.lh_normal)


# ============================ 提示 ============================


@dataclass
class WarningBlock(Block):
    """限制 / 降级提示。"""

    variant: str = "panel"

    def __post_init__(self) -> None:
        self._cache = {}
        self._plans: dict[int, dict[str, Any]] = {}

    def _plan(self, ctx: Any, width: int) -> dict[str, Any]:
        cached = self._plans.get(width)
        if cached is not None:
            return cached
        m = ctx.m
        warnings = ctx.model.warnings
        plan: dict[str, Any] = {"height": 0}
        if not warnings:
            self._plans[width] = plan
            return plan
        pad = m.gap_sm
        font = ctx.font(m.f_meta)
        label_f = ctx.font(m.f_caption, bold=True)
        bullet_w = ctx.ts.width("·", font) + m.gap_xs
        inner = max(40, width - pad * 2 - bullet_w - m.gap_sm)
        rows = [ctx.ts.fit(text, font, inner, 2) for text in warnings[:3]]
        h = pad + ctx.ts.line_height(label_f, 1.0) + m.gap_2xs
        for lines in rows:
            h += ctx.ts.paragraph_height(lines, font, m.lh_snug) + m.gap_2xs
        h += pad
        plan.update({"pad": pad, "font": font, "label_f": label_f, "rows": rows, "bullet_w": bullet_w, "height": h})
        self._plans[width] = plan
        return plan

    def _measure(self, ctx: Any, width: int) -> int:
        return int(self._plan(ctx, width)["height"])

    def draw(self, ctx: Any, layer: Any, x: int, y: int, width: int) -> None:
        if not ctx.model.warnings:
            return
        plan = self._plan(ctx, width)
        m = ctx.m
        tone = ctx.warn
        surface.panel(
            layer,
            (x, y, x + width, y + plan["height"]),
            max(3, m.radius_media // 2),
            fill=alpha(tone, 28),
            border=alpha(tone, 120),
            border_width=m.hairline,
        )
        pad = plan["pad"]
        ty = y + pad
        ctx.text(layer, (x + pad + m.gap_2xs, ty), "解析提示", plan["label_f"], tone, tracking=1.6, bold=True)
        ty += ctx.ts.line_height(plan["label_f"], 1.0) + m.gap_2xs
        for lines in plan["rows"]:
            ctx.text(layer, (x + pad + m.gap_2xs, ty), "·", plan["font"], alpha(tone, 220))
            ctx.para(
                layer,
                (x + pad + m.gap_2xs + plan["bullet_w"], ty),
                lines,
                plan["font"],
                ctx.ink_dim,
                leading=m.lh_snug,
            )
            ty += ctx.ts.paragraph_height(lines, plan["font"], m.lh_snug) + m.gap_2xs


# ============================ 热评 ============================


def _comment_avatar(
    ctx: Any,
    layer: Any,
    x: int,
    y: int,
    size: int,
    name: str,
    path: Any = None,
) -> None:
    """评论者头像：优先真实头像，缺失时用按昵称派生色相的首字占位。"""
    if path is not None and surface.Image is not None:
        try:
            img = surface.open_image(path)
        except Exception:
            img = None
        if img is not None:
            layer.alpha_composite(surface.circle_image(img, size), (x, y))
            surface.panel(
                layer,
                (x, y, x + size, y + size),
                size // 2,
                border=alpha(ctx.pal.surface_border, 70),
                border_width=ctx.m.hairline,
            )
            return
    text = name or "?"
    seed = sum(ord(ch) for ch in text)
    tint = mix(ctx.accent, ctx.pal.surface, 0.10 + (seed % 6) * 0.07)
    surface.panel(
        layer,
        (x, y, x + size, y + size),
        size // 2,
        fill=alpha(tint, 255),
    )
    font = ctx.font(max(9, int(size * 0.48)), bold=True)
    ctx.text(
        layer,
        (x + size // 2, y + size // 2),
        text[:1],
        font,
        readable_ink(tint),
        bold=True,
        anchor="mm",
    )


@dataclass
class CommentsBlock(Block):
    """热门评论。variant: cards / thread / quote / bili"""

    variant: str = "cards"
    limit: int = 3

    def __post_init__(self) -> None:
        self._cache = {}
        self._plans: dict[int, dict[str, Any]] = {}

    def _items(self, ctx: Any) -> list[CommentItem]:
        return ctx.model.comments[: max(1, self.limit)]

    def _plan(self, ctx: Any, width: int) -> dict[str, Any]:
        cached = self._plans.get(width)
        if cached is not None:
            return cached
        m = ctx.m
        items = self._items(ctx)
        plan: dict[str, Any] = {"height": 0, "rows": []}
        if not items:
            self._plans[width] = plan
            return plan
        head_f = ctx.font(m.f_eyebrow, bold=True)
        name_f = ctx.font(m.f_meta, bold=True)
        meta_f = ctx.font(m.f_caption)
        text_f = ctx.font(m.f_quote)
        head_h = ctx.ts.line_height(head_f, 1.0) + m.gap_xs + 1 + m.gap_sm

        # 所有皮肤的评论区都画头像（真实头像优先，缺失时首字占位），
        # avatar_offset 是头像左侧要预留的装饰宽度（时间线轴 / 引号）。
        avatar_size = max(20, int(m.avatar_sm * 0.72))
        if self.variant == "cards":
            pad = m.gap_sm
            avatar_offset = 0
        elif self.variant == "thread":
            pad = 0
            avatar_offset = m.gap_md
        elif self.variant == "bili":
            # 页签条已经写了"评论 N"，这里退成次级灰标题，且不再画一道重复的分割线
            head_f = ctx.font(m.f_meta)
            head_h = ctx.ts.line_height(head_f, 1.0) + m.gap_md
            name_f = ctx.font(m.f_meta)
            pad = 0
            avatar_offset = 0
            avatar_size = max(22, int(m.avatar_sm * 0.86))
        else:
            pad = 0
            # 引号装饰按真实字形宽度让位，否则大号引号会压到头像上
            mark_f = ctx.font(int(m.f_quote * 1.9), bold=True)
            plan["mark_f"] = mark_f
            avatar_offset = ctx.ts.width("“", mark_f) + m.gap_2xs
        indent = avatar_offset + avatar_size + m.gap_sm

        row_gap = m.gap_md if self.variant == "bili" else m.gap_sm
        rows: list[dict[str, Any]] = []
        total = head_h
        for item in items:
            inner = max(40, width - pad * 2 - indent)
            lines = ctx.ts.fit(item.message, text_f, inner, 3)
            h = pad
            h += ctx.ts.line_height(name_f, 1.15)
            h += ctx.ts.paragraph_height(lines, text_f, m.lh_snug)
            meta_h = 0
            if self.variant == "bili" and item.time:
                meta_h = ctx.ts.line_height(meta_f, 1.35)
                h += meta_h
            h += pad
            if avatar_size:
                h = max(h, avatar_size)
            rows.append({"item": item, "lines": lines, "h": h, "meta_h": meta_h})
            total += h + row_gap
        total -= row_gap
        plan.update(
            {
                "rows": rows,
                "height": total,
                "head_f": head_f,
                "name_f": name_f,
                "meta_f": meta_f,
                "text_f": text_f,
                "head_h": head_h,
                "pad": pad,
                "indent": indent,
                "avatar_size": avatar_size,
                "avatar_offset": avatar_offset,
                "row_gap": row_gap,
            }
        )
        self._plans[width] = plan
        return plan

    def _measure(self, ctx: Any, width: int) -> int:
        return int(self._plan(ctx, width)["height"])

    def draw(self, ctx: Any, layer: Any, x: int, y: int, width: int) -> None:
        plan = self._plan(ctx, width)
        rows = plan["rows"]
        if not rows:
            return
        m, pal = ctx.m, ctx.pal
        head_f = plan["head_f"]
        row_gap = int(plan.get("row_gap") or m.gap_sm)
        if self.variant == "bili":
            head_lh = ctx.ts.line_height(head_f, 1.0)
            ctx.text(layer, (x, y), "热门评论", head_f, ctx.ink_muted)
            sort_f = ctx.font(m.f_caption)
            sort_text = "按热度"
            sw = ctx.ts.width(sort_text, sort_f)
            icon = max(8, int(m.f_caption * 0.95))
            gx = x + width - sw - icon - m.gap_2xs * 2
            gy = y + max(0, (head_lh - icon) // 2)
            surface.glyph(layer, (gx, gy, gx + icon, gy + icon), "sort", ctx.ink_muted)
            ctx.text(
                layer,
                (x + width - sw, y + max(0, (head_lh - ctx.ts.line_height(sort_f, 1.0)) // 2)),
                sort_text,
                sort_f,
                ctx.ink_muted,
            )
            cursor = y + int(plan["head_h"])
        else:
            title = f"热门评论 · {len(rows)}"
            ctx.text(layer, (x, y), title, head_f, ctx.accent_text, tracking=ctx.theme.tracking_eyebrow, bold=True)
            line_y = y + ctx.ts.line_height(head_f, 1.0) + m.gap_xs
            surface.hairline(layer, x, line_y, x + width, ctx.hair)
            cursor = line_y + 1 + m.gap_sm
        pad, indent = plan["pad"], plan["indent"]
        avatar_size = int(plan.get("avatar_size") or 0)
        name_f, meta_f, text_f = plan["name_f"], plan["meta_f"], plan["text_f"]

        for index, row in enumerate(rows):
            item: CommentItem = row["item"]
            h = row["h"]
            if self.variant == "bili":
                if avatar_size:
                    _comment_avatar(
                        ctx,
                        layer,
                        x,
                        cursor,
                        avatar_size,
                        item.username,
                        item.avatar,
                    )
                tx = x + indent
                ty = cursor
                avail = max(20, x + width - tx)
                likes = str(item.likes) if item.likes and item.likes != "0" else ""
                icon_w = max(9, int(m.f_meta * 0.95)) if likes else 0
                tail_w = (icon_w + m.gap_2xs + ctx.ts.width(likes, meta_f)) if likes else 0
                ctx.text(
                    layer,
                    (tx, ty),
                    ctx.ts.ellipsize(item.username, name_f, max(20, avail - tail_w - m.gap_sm)),
                    name_f,
                    ctx.ink_muted,
                )
                if likes:
                    tail_x = x + width - tail_w
                    gy = ty + max(0, (ctx.ts.line_height(name_f, 1.0) - icon_w) // 2)
                    surface.glyph(layer, (tail_x, gy, tail_x + icon_w, gy + icon_w), "like", alpha(ctx.ink_muted, 255))
                    ctx.text(layer, (tail_x + icon_w + m.gap_2xs, ty), likes, name_f, ctx.ink_muted)
                ty += ctx.ts.line_height(name_f, 1.15)
                ty += ctx.para(layer, (tx, ty), row["lines"], text_f, ctx.ink, leading=m.lh_snug)
                if row.get("meta_h"):
                    ctx.text(layer, (tx, ty + m.gap_2xs), f"{item.time} 回复", meta_f, ctx.ink_muted)
                cursor += h + row_gap
                continue
            if self.variant == "cards":
                surface.panel(
                    layer,
                    (x, cursor, x + width, cursor + h),
                    max(3, int(m.radius_media * 0.75)),
                    fill=alpha(pal.surface, 18 if pal.is_dark else 130),
                    border=ctx.hair,
                    border_width=m.hairline,
                )
            elif self.variant == "thread":
                rail_x = x + max(1, m.unit // 2)
                is_last = index == len(rows) - 1
                surface.vline(layer, rail_x, cursor, cursor + (h if not is_last else max(1, h // 2)), ctx.hair)
                dot = max(3, m.unit)
                surface.panel(
                    layer,
                    (rail_x - dot // 2, cursor + m.gap_2xs, rail_x + dot // 2 + 1, cursor + m.gap_2xs + dot + 1),
                    dot,
                    fill=alpha(ctx.accent, 240),
                )
            else:
                mark_f = plan.get("mark_f") or ctx.font(int(m.f_quote * 1.9), bold=True)
                ctx.text(layer, (x, cursor - int(m.unit * 1.2)), "“", mark_f, alpha(ctx.accent, 150), bold=True)

            tx = x + pad + indent
            ty = cursor + pad
            if avatar_size:
                _comment_avatar(
                    ctx,
                    layer,
                    x + pad + int(plan.get("avatar_offset") or 0),
                    ty,
                    avatar_size,
                    item.username,
                    item.avatar,
                )
            avail = max(20, x + width - tx - pad)
            likes = str(item.likes) if item.likes and item.likes != "0" else ""
            heart_w = int(m.f_meta * 0.82) if likes else 0
            tail_w = (ctx.ts.width(likes, meta_f) + heart_w + m.gap_2xs) if likes else 0
            ctx.text(
                layer,
                (tx, ty),
                ctx.ts.ellipsize(item.username, name_f, max(20, avail - tail_w - m.gap_sm)),
                name_f,
                ctx.ink,
                bold=True,
            )
            if likes:
                tail_x = x + width - pad - tail_w
                heart_h = max(4, int(heart_w * 0.9))
                heart_y = ty + max(0, (ctx.ts.line_height(meta_f, 1.0) - heart_h) // 2)
                surface.heart(
                    layer,
                    (tail_x, heart_y, tail_x + heart_w, heart_y + heart_h),
                    alpha(ctx.accent, 235),
                )
                ctx.text(
                    layer,
                    (tail_x + heart_w + m.gap_2xs, ty),
                    likes,
                    meta_f,
                    ctx.accent_text,
                )
            ty += ctx.ts.line_height(name_f, 1.15)
            ctx.para(layer, (tx, ty), row["lines"], text_f, ctx.ink_dim, leading=m.lh_snug)
            cursor += h + row_gap


# ============================ 页脚 ============================


def _url_flow(
    ctx: Any,
    url: str,
    font: Any,
    first_width: int,
    rest_width: int,
    max_lines: int = 3,
) -> list[str]:
    """把链接按可用宽度逐字符折行，保证协议 / 路径 / 查询串 / 片段都不丢。

    首行可用宽度通常比后续行窄（要给右侧水印让位），因此单独传入。
    只有超过 max_lines 行时才在末行补省略号。
    """
    text = str(url or "")
    if not text:
        return []
    lines: list[str] = []
    idx, total = 0, len(text)
    while idx < total and len(lines) < max_lines:
        avail = max(24, first_width if not lines else rest_width)
        take = 0
        while idx + take < total:
            if ctx.ts.width(text[idx : idx + take + 1], font) > avail:
                break
            take += 1
        if take == 0:
            take = 1
        lines.append(text[idx : idx + take])
        idx += take
    if idx < total and lines:
        avail = max(24, first_width if len(lines) == 1 else rest_width)
        last = lines[-1]
        while last and ctx.ts.width(last + "…", font) > avail:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


@dataclass
class FooterBlock(Block):
    """页脚：链接 + 水印。variant: rule / plate / minimal / ledger / bili

    链接过长时折成最多三行（而不是截断），与 card_footer_url 的"不省略"约定保持一致。
    """

    variant: str = "rule"
    max_url_lines: int = 3

    def __post_init__(self) -> None:
        self._cache = {}
        self._plans: dict[int, dict[str, Any]] = {}

    def _rows(self, ctx: Any) -> tuple[str, str]:
        return ctx.model.url, ctx.model.watermark

    def _plan(self, ctx: Any, width: int) -> dict[str, Any]:
        cached = self._plans.get(width)
        if cached is not None:
            return cached
        m = ctx.m
        url, mark = self._rows(ctx)
        font = ctx.font(m.f_footer)
        mark_f = ctx.font(m.f_footer, bold=True)
        row = ctx.ts.line_height(font, 1.2)
        mark_tracking = 0.0 if self.variant == "bili" else 1.2
        mark_w = ctx.ts.tracked_width(mark, mark_f, mark_tracking) if mark else 0

        if self.variant == "bili":
            # 底部操作栏：水印 + 转发/评论/收藏/点赞 四组数据，链接收进一条通栏灰色药丸里
            icon = max(11, int(m.f_footer * 1.3))
            action_row = max(ctx.ts.line_height(mark_f, 1.2), icon)
            actions = [
                (kinds[0], _stat_value(ctx, kinds))
                for kinds in (("share",), ("comment",), ("star",), ("like",))
            ]
            actions = [pair for pair in actions if pair[1]]
            pill_pad = max(m.gap_xs, m.unit)
            pill_inner = max(40, width - pill_pad * 2)
            url_lines = _url_flow(ctx, url, font, pill_inner, pill_inner, self.max_url_lines)
            pill_h = pill_pad * 2 + row * max(1, len(url_lines))
            head = m.gap_sm + 1 + m.gap_md
            plan = {
                "font": font,
                "mark_f": mark_f,
                "mark": mark,
                "mark_w": mark_w,
                "mark_tracking": mark_tracking,
                "url_lines": url_lines,
                "row": row,
                "inner_w": pill_inner,
                "offset_x": pill_pad,
                "offset_y": head + action_row + m.gap_sm + pill_pad,
                "icon": icon,
                "action_row": action_row,
                "actions": actions,
                "pill_pad": pill_pad,
                "pill_h": pill_h,
                "head": head,
                "height": head + action_row + m.gap_sm + pill_h,
            }
            self._plans[width] = plan
            return plan

        if self.variant == "plate":
            inner_w = max(40, width - m.gap_sm * 2)
            offset_x, offset_y = m.gap_sm, m.gap_sm
            chrome = m.gap_sm * 2
        elif self.variant == "ledger":
            inner_w = width
            offset_x, offset_y = 0, m.gap_sm + 4 + m.gap_sm
            chrome = m.gap_sm + 4 + m.gap_sm
        elif self.variant == "minimal":
            inner_w = width
            offset_x, offset_y = 0, 0
            chrome = 0
        else:
            inner_w = width
            offset_x, offset_y = 0, m.gap_sm + 1 + m.gap_sm
            chrome = m.gap_sm + 1 + m.gap_sm

        first_w = max(24, inner_w - (mark_w + m.gap_md if mark_w else 0))
        url_lines = _url_flow(ctx, url, font, first_w, inner_w, self.max_url_lines)
        rows = max(1, len(url_lines))
        plan = {
            "font": font,
            "mark_f": mark_f,
            "mark": mark,
            "mark_w": mark_w,
            "mark_tracking": mark_tracking,
            "url_lines": url_lines,
            "row": row,
            "inner_w": inner_w,
            "offset_x": offset_x,
            "offset_y": offset_y,
            "height": chrome + row * rows,
        }
        self._plans[width] = plan
        return plan

    def _measure(self, ctx: Any, width: int) -> int:
        return int(self._plan(ctx, width)["height"])

    def _draw_bili(self, ctx: Any, layer: Any, x: int, y: int, width: int, plan: dict[str, Any]) -> None:
        """哔哩哔哩底部操作栏：一道分割线 + 水印与四组数据 + 通栏链接药丸。"""
        m, pal = ctx.m, ctx.pal
        row = plan["row"]
        surface.hairline(layer, x, y + m.gap_sm, x + width, ctx.hair)
        top = y + int(plan["head"])
        action_row = int(plan["action_row"])
        mark_f = plan["mark_f"]
        if plan["mark"]:
            ctx.text(
                layer,
                (x, top + max(0, (action_row - ctx.ts.line_height(mark_f, 1.2)) // 2)),
                plan["mark"],
                mark_f,
                ctx.accent_text,
                tracking=plan["mark_tracking"],
                bold=True,
            )

        icon = int(plan["icon"])
        font = plan["font"]
        gap = m.gap_lg
        actions = plan["actions"]
        widths = [icon + m.gap_2xs + ctx.ts.width(value, font) for _, value in actions]
        total_w = sum(widths) + gap * max(0, len(widths) - 1)
        cursor = x + width - total_w
        if actions and cursor >= x + plan["mark_w"] + m.gap_md:
            gy = top + max(0, (action_row - icon) // 2)
            tyy = top + max(0, (action_row - row) // 2)
            for (kind, value), w in zip(actions, widths, strict=False):
                surface.glyph(layer, (cursor, gy, cursor + icon, gy + icon), kind, alpha(ctx.ink_muted, 255))
                ctx.text(layer, (cursor + icon + m.gap_2xs, tyy), value, font, ctx.ink_muted)
                cursor += w + gap

        pill_pad = int(plan["pill_pad"])
        pill_h = int(plan["pill_h"])
        pill_top = top + action_row + m.gap_sm
        surface.panel(
            layer,
            (x, pill_top, x + width, pill_top + pill_h),
            max(3, min(pill_h // 2, m.radius_panel)),
            fill=alpha(pal.surface, 34 if pal.is_dark else 150),
            border=ctx.hair,
            border_width=m.hairline,
        )
        for index, line in enumerate(plan["url_lines"]):
            ctx.text(layer, (x + pill_pad, pill_top + pill_pad + index * row), line, font, ctx.accent_alt_text)

    def draw(self, ctx: Any, layer: Any, x: int, y: int, width: int) -> None:
        m = ctx.m
        plan = self._plan(ctx, width)
        total = self.measure(ctx, width)
        row = plan["row"]
        inner_x = x + plan["offset_x"]
        inner_w = plan["inner_w"]
        ty = y + plan["offset_y"]

        if self.variant == "bili":
            self._draw_bili(ctx, layer, x, y, width, plan)
            return

        if self.variant == "plate":
            surface.panel(
                layer,
                (x, y, x + width, y + total),
                max(3, m.radius_media // 2),
                fill=alpha(ctx.accent, ctx.pal.accent_wash + 8),
                border=alpha(ctx.accent, 90),
                border_width=m.hairline,
            )
        elif self.variant == "ledger":
            surface.hairline(layer, x, y + m.gap_sm, x + width, alpha(ctx.ink, 140))
            surface.hairline(layer, x, y + m.gap_sm + 3, x + width, ctx.hair)
        elif self.variant != "minimal":
            surface.hairline(layer, x, y + m.gap_sm, x + width, ctx.hair)

        url_ink = ctx.ink_muted
        for index, line in enumerate(plan["url_lines"]):
            ctx.text(layer, (inner_x, ty + index * row), line, plan["font"], url_ink)
        if plan["mark"]:
            ctx.text(
                layer,
                (inner_x + inner_w - plan["mark_w"], ty),
                plan["mark"],
                plan["mark_f"],
                ctx.accent_text,
                tracking=plan["mark_tracking"],
                bold=True,
            )


# ============================ 组合区块 ============================


@dataclass
class RowBlock(Block):
    """两栏并排（杂志布局）。"""

    main: list[Block] = field(default_factory=list)
    side: list[Block] = field(default_factory=list)
    side_span: int = 5
    gap: int = 0
    divider: bool = True
    side_first: bool = False

    def __post_init__(self) -> None:
        self._cache = {}
        self._plans: dict[int, dict[str, Any]] = {}

    def _plan(self, ctx: Any, width: int) -> dict[str, Any]:
        cached = self._plans.get(width)
        if cached is not None:
            return cached
        m = ctx.m
        gap = self.gap or m.gutter
        side_w = max(80, m.col(width - gap, self.side_span, 12)) if self.side else 0
        if self.side:
            side_w = int(round((width - gap) * (self.side_span / 12.0)))
            side_w = max(90, min(width - gap - 120, side_w))
        main_w = width - (side_w + gap if self.side else 0)
        main_h = _stack_height(ctx, self.main, main_w, m.gap_md)
        side_h = _stack_height(ctx, self.side, side_w, m.gap_md) if self.side else 0
        plan = {"gap": gap, "side_w": side_w, "main_w": main_w, "height": max(main_h, side_h)}
        self._plans[width] = plan
        return plan

    def _measure(self, ctx: Any, width: int) -> int:
        return int(self._plan(ctx, width)["height"])

    def draw(self, ctx: Any, layer: Any, x: int, y: int, width: int) -> None:
        plan = self._plan(ctx, width)
        gap, side_w, main_w = plan["gap"], plan["side_w"], plan["main_w"]
        if self.side_first:
            side_x = x
            main_x = x + side_w + gap
        else:
            main_x = x
            side_x = x + main_w + gap
        _stack_draw(ctx, layer, self.main, main_x, y, main_w, ctx.m.gap_md)
        if self.side:
            if self.divider:
                rail = side_x - gap // 2 if not self.side_first else main_x - gap // 2
                surface.vline(layer, rail, y, y + plan["height"], ctx.hair)
            _stack_draw(ctx, layer, self.side, side_x, y, side_w, ctx.m.gap_md)


@dataclass
class ImmersiveHeroBlock(Block):
    """全幅 hero + 底部渐隐压字（沉浸布局）。"""

    bleed: int = 0
    corner_top: bool = True

    def __post_init__(self) -> None:
        self._cache = {}
        self._plans: dict[int, dict[str, Any]] = {}

    def _plan(self, ctx: Any, width: int) -> dict[str, Any]:
        cached = self._plans.get(width)
        if cached is not None:
            return cached
        m, model = ctx.m, ctx.model
        hero = model.hero
        inner = max(40, width - m.pad * 2)
        title_font_size = max(m.f_subtitle, int(round(m.f_display * 0.72 * ctx.layout.headline_scale)))
        title_f = ctx.font(title_font_size, bold=True)
        lines = ctx.ts.fit(model.title, title_f, inner, 3) if model.title else []
        meta_f = ctx.font(m.f_meta)
        overlay_h = m.gap_lg
        overlay_h += m.chip_h + m.gap_sm
        overlay_h += ctx.ts.paragraph_height(lines, title_f, m.lh_tight)
        overlay_h += m.gap_sm + ctx.ts.line_height(meta_f, 1.2)
        overlay_h += m.gap_lg
        aspect = hero.aspect if hero else 1.6
        image_h = int(round(max(width * 0.62, min(width * 1.05, width / max(0.5, aspect))) * ctx.layout.media_scale))
        height = max(image_h, overlay_h + int(width * 0.28))
        plan = {"title_f": title_f, "lines": lines, "meta_f": meta_f, "overlay_h": overlay_h, "height": height}
        self._plans[width] = plan
        return plan

    def _measure(self, ctx: Any, width: int) -> int:
        return int(self._plan(ctx, width)["height"])

    def draw(self, ctx: Any, layer: Any, x: int, y: int, width: int) -> None:
        plan = self._plan(ctx, width)
        m, pal, model = ctx.m, ctx.pal, ctx.model
        height = plan["height"]
        hero = model.hero
        surface.panel(layer, (x, y, x + width, y + height), 0, fill=alpha(pal.media_mat, 255))
        if hero is not None:
            tile = _tile_image(ctx, hero, width, height, letterbox=True)
            if tile is not None:
                layer.alpha_composite(tile, (x, y))
        # 上下遮罩：上轻下重，保证压字可读
        layer.alpha_composite(
            surface.scrim((width, height), (0, 0, 0), top_alpha=120, bottom_alpha=0, curve=0.7), (x, y)
        )
        layer.alpha_composite(
            surface.scrim((width, height), darken(pal.backdrop_b, 0.25), top_alpha=0, bottom_alpha=245, curve=2.1), (x, y)
        )
        if hero is not None and hero.is_video and ctx.show_play_button:
            _play_badge(ctx, layer, (x, y, x + width, y + height - plan["overlay_h"] // 2))
        if model.duration_text:
            _corner_tag(ctx, layer, (x, y + m.gap_lg + m.chip_h, x + width - m.pad + m.gap_xs, y + m.gap_lg + m.chip_h * 2), model.duration_text)

        inner_x = x + m.pad
        inner_w = width - m.pad * 2
        base = y + height - m.gap_lg
        meta_f = plan["meta_f"]
        meta_parts = [p for p in (model.author_name, model.time_text) if p]
        if model.total_media > 1:
            meta_parts.append(f"{model.total_media} 项媒体")
        meta = "  ·  ".join(meta_parts)
        base -= ctx.ts.line_height(meta_f, 1.2)
        if meta:
            ctx.text(layer, (inner_x, base), ctx.ts.ellipsize(meta, meta_f, inner_w), meta_f, (226, 230, 240))
        lines = plan["lines"]
        if lines:
            title_f = plan["title_f"]
            h = ctx.ts.paragraph_height(lines, title_f, m.lh_tight)
            base -= m.gap_sm + h
            ctx.para(layer, (inner_x, base), lines, title_f, (255, 255, 255), leading=m.lh_tight, bold=True)
        label = model.platform_name or "网页"
        if ctx.theme.uppercase_eyebrow:
            label = label.upper()
        base -= m.gap_sm + m.chip_h
        cursor = inner_x
        cursor += _chip(
            ctx,
            layer,
            cursor,
            base,
            label,
            fill=alpha(ctx.accent, 250),
            ink=ctx.accent_ink,
            font=ctx.font(m.f_eyebrow, bold=True),
            tracking=ctx.theme.tracking_eyebrow,
            bold=True,
        )
        if model.content_type:
            cursor += m.gap_xs
            _chip(
                ctx,
                layer,
                cursor,
                base,
                model.content_type,
                fill=(255, 255, 255, 40),
                ink=(255, 255, 255),
                border=(255, 255, 255, 90),
                font=ctx.font(m.f_eyebrow),
            )


# ============================ 堆叠工具 ============================


def _stack_height(ctx: Any, blocks: Sequence[Block], width: int, gap: int) -> int:
    total = 0
    visible = 0
    for block in blocks:
        h = block.measure(ctx, width)
        if h <= 0:
            continue
        if visible:
            total += gap
        total += h
        visible += 1
    return total


def _stack_draw(ctx: Any, layer: Any, blocks: Sequence[Block], x: int, y: int, width: int, gap: int) -> int:
    cursor = y
    first = True
    for block in blocks:
        h = block.measure(ctx, width)
        if h <= 0:
            continue
        if not first:
            cursor += gap
        block.draw(ctx, layer, x, cursor, width)
        cursor += h
        first = False
    return cursor - y


__all__ = [
    "Block",
    "BodyBlock",
    "CommentsBlock",
    "EyebrowBlock",
    "FooterBlock",
    "HeadlineBlock",
    "IdentityBlock",
    "ImmersiveHeroBlock",
    "IpNoteBlock",
    "MediaBlock",
    "QuoteBlock",
    "RowBlock",
    "RuleBlock",
    "SpacerBlock",
    "StatsBlock",
    "TabBarBlock",
    "WarningBlock",
    "_stack_draw",
    "_stack_height",
]
