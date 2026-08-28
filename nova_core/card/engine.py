"""卡片渲染引擎：把主题配方 + 布局预设 + 数据模型装配成一张成品图。

渲染分三步，任何一步都不含硬编码高度：

1. 依据 theme + layout 组装区块列表；
2. 先 measure 全部区块得到精确画布高度；
3. 画背景 -> 画面板 -> 画区块。

theme(深/浅) x skin(6) x layout(4) = 48 种组合都会走同一条流水线，
因此每一种组合都真实生效，不存在"高级皮肤忽略主题与布局"的情况。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from . import blocks as blk
from . import surface
from .metrics import Metrics, build_metrics
from .model import CardModel
from .palette import (
    RGB,
    RGBA,
    Palette,
    alpha,
    darken,
    ensure_contrast,
    get_palette,
    lighten,
    mix,
    platform_accent,
    readable_ink,
)
from .theme import (
    THEMES,
    LayoutPreset,
    ThemeRecipe,
    get_layout,
    resolve_mode,
    resolve_theme_key_for_platform,
)
from .typeset import TypeSetter

try:  # pragma: no cover - 环境缺少 Pillow 时优雅降级
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]

#: 警示文案配色（琥珀），深浅模式各一
WARN_DARK: RGB = (255, 186, 84)
WARN_LIGHT: RGB = (176, 96, 12)

#: 这些区块总是横跨整幅，不进杂志布局的侧栏
FULL_WIDTH_TAIL = ("ipnote", "tabbar", "warnings", "comments", "footer")


# ============================ 渲染上下文 ============================


@dataclass(slots=True)
class RenderContext:
    """区块绘制期间共享的一切：度量、配色、字体、数据与画布。"""

    m: Metrics
    pal: Palette
    ts: TypeSetter
    model: CardModel
    theme: ThemeRecipe
    layout: LayoutPreset
    accent: RGB
    accent_alt: RGB | None = None
    show_play_button: bool = False
    cover_full_size: bool = False
    canvas: Any = None
    draw: Any = None
    texts: list[str] = field(default_factory=list)

    # 以下均在 __post_init__ 里按对比度推导
    ink: RGB = (255, 255, 255)
    ink_dim: RGB = (200, 200, 200)
    ink_muted: RGB = (150, 150, 150)
    hair: RGBA = (255, 255, 255, 40)
    accent_ink: RGB = (255, 255, 255)
    accent_text: RGB = (255, 255, 255)
    warn: RGB = WARN_DARK
    accent_alt_text: RGB = (255, 255, 255)
    panel_bg: RGB = (16, 20, 28)
    _draws: dict[int, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        pal = self.pal
        # 文字实际落在哪个底色上：有面板时是面板混合色，无面板时是背景中值
        backdrop_mid = mix(pal.backdrop_a, pal.backdrop_b, 0.5)
        if self.theme.panel in ("glass", "card", "bar") and pal.surface_alpha > 0:
            opacity = 1.0 if self.theme.panel != "glass" else pal.surface_alpha / 255.0
            self.panel_bg = mix(backdrop_mid, pal.surface, min(1.0, opacity))
        else:
            self.panel_bg = backdrop_mid

        base = self.panel_bg
        self.ink = ensure_contrast(pal.ink, base, 7.0)
        self.ink_dim = ensure_contrast(pal.ink_dim, base, 4.5)
        self.ink_muted = ensure_contrast(pal.ink_muted, base, 3.2)
        self.hair = alpha(pal.hairline, pal.hairline_alpha)
        self.accent_ink = readable_ink(self.accent)
        # 强调色若在面板底上读不清，就朝可读方向推移（只用于文字，不用于色块）
        self.accent_text = ensure_contrast(self.accent, base, 4.0)
        self.warn = ensure_contrast(WARN_DARK if pal.is_dark else WARN_LIGHT, base, 4.0)
        if self.accent_alt is None:
            self.accent_alt = self.accent
        self.accent_alt_text = ensure_contrast(self.accent_alt, base, 4.0)

    # ---------- 画布 ----------

    def draw_for(self, layer: Any) -> Any:
        """按图层缓存 ImageDraw，避免每次绘制新建对象。"""
        key = id(layer)
        cached = self._draws.get(key)
        if cached is None:
            cached = ImageDraw.Draw(layer)
            self._draws[key] = cached
        return cached

    # ---------- 排版 ----------

    def font(self, size: int, bold: bool = False) -> Any:
        return self.ts.font(size, bold)

    def text(
        self,
        layer: Any,
        xy: tuple[int, int],
        text: str,
        font: Any,
        fill: RGB | RGBA,
        *,
        tracking: float = 0.0,
        bold: bool = False,
        anchor: str | None = None,
    ) -> int:
        if text:
            self.texts.append(str(text))
        return self.ts.draw_line(
            self.draw_for(layer),
            xy,
            text,
            font,
            fill,
            tracking=tracking,
            bold=bold,
            anchor=anchor,
        )

    def para(
        self,
        layer: Any,
        xy: tuple[int, int],
        lines: list[str],
        font: Any,
        fill: RGB | RGBA,
        *,
        leading: float = 1.45,
        tracking: float = 0.0,
        bold: bool = False,
        align: str = "left",
        max_width: int | None = None,
    ) -> int:
        for line in lines:
            if line:
                self.texts.append(str(line))
        return self.ts.draw_paragraph(
            self.draw_for(layer),
            xy,
            lines,
            font,
            fill,
            leading=leading,
            tracking=tracking,
            bold=bold,
            align=align,
            max_width=max_width,
        )


# ============================ 背景 ============================


def _bloom_spots(ctx: RenderContext, strength: float = 1.0) -> list[tuple[float, float, float, RGB, int]]:
    """按强调色 + 主题光晕色摆三个柔光斑，构成有纵深的背景。"""
    pal = ctx.pal
    base = max(10, int(pal.bloom_alpha * strength))
    warm = mix(ctx.accent, pal.bloom, 0.45)
    return [
        (0.08, 0.02, 0.62, ctx.accent, base),
        (0.96, 0.20, 0.52, pal.bloom, int(base * 0.9)),
        (0.52, 1.04, 0.70, warm, int(base * 0.55)),
    ]


def _paint_backdrop(ctx: RenderContext, size: tuple[int, int]) -> Any:
    """按 theme.backdrop 铺底，再按 theme.ornament 叠装饰通道。"""
    width, height = size
    pal, theme = ctx.pal, ctx.theme
    kind = theme.backdrop

    if kind == "plain":
        base = Image.new("RGB", (width, height), pal.backdrop_a)
    elif kind == "mesh":
        base = surface.linear_gradient((width, height), pal.backdrop_a, pal.backdrop_b, 135)
    elif kind == "midnight":
        base = surface.linear_gradient((width, height), pal.backdrop_a, pal.backdrop_b, 118)
    elif kind == "wall":
        base = surface.linear_gradient((width, height), pal.backdrop_a, pal.backdrop_b, 100)
    else:  # paper / graphite 走稳定的竖向渐变
        base = surface.linear_gradient((width, height), pal.backdrop_a, pal.backdrop_b, 90)

    canvas = base.convert("RGBA")

    # 背景种类自带的结构层
    if kind == "graphite":
        step = max(14, int(28 * ctx.m.scale))
        surface.measure_grid(
            canvas,
            pal.hairline,
            max(8, pal.hairline_alpha // 2),
            step,
            major_every=4,
            major_alpha=max(14, pal.hairline_alpha),
        )
    elif kind == "wall":
        # 展厅墙面：极淡的水平光带，模拟射灯
        surface.bloom(canvas, [(0.5, -0.06, 0.9, lighten(pal.backdrop_a, 0.5), max(18, pal.bloom_alpha))], blur=0)
    elif kind == "paper":
        surface.bloom(canvas, [(0.5, 0.0, 1.1, lighten(pal.backdrop_a, 0.35), 26)], blur=0)

    # 装饰通道
    for channel in theme.ornament:
        if channel == "bloom":
            surface.bloom(canvas, _bloom_spots(ctx))
        elif channel == "grain":
            surface.grain(canvas, pal.grain_alpha)
        elif channel == "fiber":
            surface.paper_fiber(canvas, max(6, pal.grain_alpha))
        elif channel == "halftone":
            surface.halftone(canvas, pal.hairline, max(8, pal.grain_alpha), max(6, int(9 * ctx.m.scale)), 1)
        elif channel == "grid":
            if kind != "graphite":
                surface.measure_grid(canvas, pal.hairline, max(8, pal.hairline_alpha // 2), max(14, int(28 * ctx.m.scale)))
        elif channel == "scan":
            surface.scanlines(canvas, pal.hairline, max(6, pal.hairline_alpha // 3), max(3, int(3 * ctx.m.scale)))
        elif channel == "vignette":
            surface.vignette(canvas, 40 if pal.is_dark else 26)

    return canvas


# ============================ 面板与画框 ============================


def _paint_panel(ctx: RenderContext, canvas: Any, box: tuple[int, int, int, int]) -> None:
    """按 theme.panel 绘制内容承载面。"""
    m, pal, kind = ctx.m, ctx.pal, ctx.theme.panel
    radius = m.radius_card
    if kind == "glass":
        surface.panel(
            canvas,
            box,
            radius,
            fill=alpha(pal.surface, pal.surface_alpha),
            border=alpha(pal.surface_border, pal.surface_border_alpha),
            border_width=m.border,
            shadow_alpha=pal.shadow_alpha,
            shadow_blur=max(10, m.shadow_blur * 2),
            shadow_offset=(0, max(3, m.unit * 3)),
            blur_backdrop=max(6, int(16 * m.scale)),
        )
        # 顶部一道高光，玻璃才立体
        surface.hairline(
            canvas,
            box[0] + radius,
            box[1] + max(1, m.border),
            box[2] - radius,
            alpha(lighten(pal.surface, 0.6), 70 if pal.is_dark else 130),
        )
    elif kind == "card":
        surface.panel(
            canvas,
            box,
            radius,
            fill=alpha(pal.surface, 255),
            border=alpha(pal.surface_border, pal.surface_border_alpha),
            border_width=m.hairline,
            shadow_alpha=pal.shadow_alpha,
            shadow_blur=max(10, m.shadow_blur * 2),
            shadow_offset=(0, max(4, m.unit * 4)),
        )
    elif kind == "outline":
        surface.panel(
            canvas,
            box,
            max(2, radius // 5),
            fill=alpha(pal.surface, max(0, min(255, pal.surface_alpha - 30))) if pal.surface_alpha > 40 else None,
            border=alpha(pal.surface_border, min(255, pal.surface_border_alpha + 40)),
            border_width=m.border,
        )
    elif kind == "bar":
        surface.panel(
            canvas,
            box,
            radius,
            fill=alpha(pal.surface, 255),
            shadow_alpha=pal.shadow_alpha,
            shadow_blur=max(10, m.shadow_blur * 2),
            shadow_offset=(0, max(4, m.unit * 3)),
        )
        bar_h = max(3, int(5 * m.scale))
        surface.panel(canvas, (box[0], box[1], box[2], box[1] + bar_h), 0, fill=alpha(ctx.accent, 255))


def _paint_frame(ctx: RenderContext, canvas: Any, box: tuple[int, int, int, int]) -> None:
    """按 theme.ornament_frame 画装饰边框（在面板之上、内容之下）。"""
    m, kind = ctx.m, ctx.theme.ornament_frame
    if kind == "none":
        return
    inset = max(4, m.gap_sm)
    x0, y0, x1, y1 = box[0] + inset, box[1] + inset, box[2] - inset, box[3] - inset
    if x1 <= x0 or y1 <= y0:
        return
    if kind == "keyline":
        surface.panel(canvas, (x0, y0, x1, y1), 0, border=ctx.hair, border_width=m.hairline)
    elif kind == "corner":
        surface.corner_marks(canvas, (x0, y0, x1, y1), alpha(ctx.accent, 200), max(10, m.gap_lg), width=max(1, m.border))
    elif kind == "bracket":
        length = max(20, m.gap_2xl)
        color = alpha(ctx.accent, 210)
        surface.vline(canvas, x0, y0, y0 + length, color, width=max(1, m.border))
        surface.vline(canvas, x1, y1 - length, y1, color, width=max(1, m.border))


# ============================ 区块装配 ============================


def _make_block(name: str, theme: ThemeRecipe, layout: LayoutPreset, headline_scale: float) -> blk.Block | None:
    if name == "eyebrow":
        return blk.EyebrowBlock(variant=theme.eyebrow)
    if name == "identity":
        return blk.IdentityBlock(variant=theme.identity)
    if name == "headline":
        return blk.HeadlineBlock(
            variant=theme.headline,
            scale=headline_scale,
            max_lines=3 if layout.key == "feed" else 4,
        )
    if name == "body":
        return blk.BodyBlock(variant=theme.body, max_lines=5 if layout.key == "feed" else 7)
    if name == "media":
        return blk.MediaBlock(variant=theme.media)
    if name == "stats":
        return blk.StatsBlock(variant=theme.stats)
    if name == "ipnote":
        return blk.IpNoteBlock()
    if name == "tabbar":
        return blk.TabBarBlock()
    if name == "quote":
        return blk.QuoteBlock(variant="quote" if theme.panel == "none" else "panel")
    if name == "warnings":
        return blk.WarningBlock()
    if name == "comments":
        return blk.CommentsBlock(variant=theme.comments, limit=2 if layout.key == "feed" else 3)
    if name == "footer":
        return blk.FooterBlock(variant=theme.footer)
    return None


def _assemble(ctx: RenderContext, *, drop: tuple[str, ...] = ()) -> list[blk.Block]:
    """按 theme.order + layout 组装区块树。"""
    theme, layout = ctx.theme, ctx.layout
    headline_scale = theme.headline_scale * layout.headline_scale
    skip = set(drop) | set(layout.drop_blocks)

    names = [n for n in theme.order if n not in skip]
    sidebar = tuple(n for n in layout.sidebar_blocks if n in names)

    if layout.sidebar_span > 0 and sidebar:
        full_width = set(FULL_WIDTH_TAIL) | set(layout.full_width_blocks)
        main_names = [n for n in names if n not in sidebar and n not in full_width]
        # 行锚点 = 参与分栏的第一个区块在原顺序里的位置
        row_members = set(main_names) | set(sidebar)
        anchor = min((i for i, n in enumerate(names) if n in row_members), default=0)
        head_names = [n for i, n in enumerate(names) if n in full_width and i < anchor]
        tail_names = [n for i, n in enumerate(names) if n in full_width and i >= anchor]
        main = [b for b in (_make_block(n, theme, layout, headline_scale) for n in main_names) if b is not None]
        side = [b for b in (_make_block(n, theme, layout, headline_scale) for n in sidebar) if b is not None]
        row = blk.RowBlock(
            main=main,
            side=side,
            side_span=layout.sidebar_span,
            divider=theme.panel != "card",
            side_first=False,
        )
        head = [b for b in (_make_block(n, theme, layout, headline_scale) for n in head_names) if b is not None]
        tail = [b for b in (_make_block(n, theme, layout, headline_scale) for n in tail_names) if b is not None]
        return [*head, row, *tail]

    return [b for b in (_make_block(n, theme, layout, headline_scale) for n in names) if b is not None]


def _immersive_rest_model(model: CardModel) -> CardModel:
    """沉浸布局下 hero 已用掉首图，其余媒体交给普通媒体区块。"""
    rest = list(model.media[1:])
    return replace(
        model,
        media=rest,
        total_media=max(0, model.total_media - 1),
        hero_is_video=False,
        duration_text="",
    )


# ============================ 顶层入口 ============================


def build_context(
    model: CardModel,
    *,
    width: int = 800,
    mode: str = "dark",
    theme_key: str = "aurora",
    layout_key: str = "standard",
    font_path: str | None = None,
    show_play_button: bool = False,
    cover_full_size: bool = False,
    texts: list[str] | None = None,
) -> RenderContext:
    """构造渲染上下文（测试可直接用它断言文本与尺寸）。"""
    # theme_key 可能是「跟随平台」哨兵，要结合来源站点才能定下真正的皮肤
    theme = THEMES[resolve_theme_key_for_platform(theme_key, model.platform_key)]
    layout = get_layout(layout_key)
    mode = resolve_mode(mode)
    metrics = build_metrics(width, layout.density or theme.density)
    if theme.radius_scale != 1.0:
        # 仿站点视觉的主题用它还原对方的圆角语言（例如哔哩哔哩的小圆角）
        scale = max(0.05, float(theme.radius_scale))
        metrics = replace(
            metrics,
            radius_card=max(2, int(round(metrics.radius_card * scale))),
            radius_panel=max(2, int(round(metrics.radius_panel * scale))),
            radius_media=max(2, int(round(metrics.radius_media * scale))),
        )
    palette = get_palette(theme.palette_key, mode)

    if theme.accent_source == "fixed":
        accent = theme.accent_fixed
    elif theme.accent_source == "blend":
        accent = mix(platform_accent(model.platform_key), theme.accent_fixed, 0.55)
    else:
        accent = platform_accent(model.platform_key)
    if theme.accent_adjust:
        # 强调色本身也要在背景上站得住：深色模式提亮、浅色模式压暗
        accent = lighten(accent, 0.18) if palette.is_dark else darken(accent, 0.06)

    return RenderContext(
        m=metrics,
        pal=palette.with_accent(accent),
        ts=TypeSetter(font_path=font_path),
        model=model,
        theme=theme,
        layout=layout,
        accent=accent,
        accent_alt=theme.accent_alt,
        show_play_button=show_play_button,
        cover_full_size=cover_full_size,
        texts=texts if texts is not None else [],
    )


def render_card_image(
    model: CardModel,
    *,
    width: int = 800,
    mode: str = "dark",
    theme_key: str = "aurora",
    layout_key: str = "standard",
    font_path: str | None = None,
    show_play_button: bool = False,
    cover_full_size: bool = False,
) -> Any:
    """渲染一张分享卡片，返回 RGB 图像。"""
    if Image is None:
        raise RuntimeError("渲染卡片需要 Pillow，请先安装 pillow")

    texts: list[str] = []
    ctx = build_context(
        model,
        width=width,
        mode=mode,
        theme_key=theme_key,
        layout_key=layout_key,
        font_path=font_path,
        show_play_button=show_play_button,
        cover_full_size=cover_full_size,
        texts=texts,
    )
    m = ctx.m
    canvas_w = m.width

    # --- 几何 ---
    edge = m.gap_md if ctx.theme.panel != "none" else 0
    inner_w = canvas_w - edge * 2
    content_x = edge + m.pad
    content_w = max(160, canvas_w - content_x * 2)
    gap = m.gap_lg

    # --- 沉浸布局：全幅 hero ---
    use_hero = bool(ctx.layout.immersive_hero and model.has_media)
    hero_block: blk.ImmersiveHeroBlock | None = None
    hero_h = 0
    body_ctx = ctx
    if use_hero:
        hero_block = blk.ImmersiveHeroBlock()
        hero_h = hero_block.measure(ctx, inner_w)
        rest = _immersive_rest_model(model)
        drop = ["eyebrow", "headline", "identity"]
        if not rest.media:
            drop.append("media")
        body_ctx = build_context(
            rest,
            width=width,
            mode=mode,
            theme_key=theme_key,
            layout_key=layout_key,
            font_path=font_path,
            show_play_button=show_play_button,
            cover_full_size=cover_full_size,
            texts=texts,
        )
        body_ctx.ts = ctx.ts  # 复用字体缓存
        body_blocks = _assemble(body_ctx, drop=tuple(drop))
    else:
        body_blocks = _assemble(ctx)

    body_h = blk._stack_height(body_ctx, body_blocks, content_w, gap)

    top_pad = 0 if use_hero else m.pad_top
    content_y = edge + top_pad + (hero_h + m.gap_lg if use_hero else 0)
    total_h = content_y + body_h + m.pad_bottom + edge
    total_h = max(int(canvas_w * 0.34), total_h)

    # --- 绘制 ---
    canvas = _paint_backdrop(ctx, (canvas_w, total_h))
    panel_box = (edge, edge, canvas_w - edge, total_h - edge)
    _paint_panel(ctx, canvas, panel_box)

    if use_hero and hero_block is not None:
        # hero 与面板顶部对齐并共享圆角
        hero_layer = Image.new("RGBA", (inner_w, hero_h), (0, 0, 0, 0))
        hero_block.draw(ctx, hero_layer, 0, 0, inner_w)
        if edge > 0:
            hero_layer = surface.round_image(
                hero_layer, m.radius_card, corners=(True, True, False, False)
            )
        canvas.alpha_composite(hero_layer, (edge, edge))

    _paint_frame(ctx, canvas, panel_box)
    blk._stack_draw(body_ctx, canvas, body_blocks, content_x, content_y, content_w, gap)

    return canvas.convert("RGB")


__all__ = [
    "RenderContext",
    "build_context",
    "render_card_image",
]
