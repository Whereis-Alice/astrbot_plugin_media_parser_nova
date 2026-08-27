"""卡片设计系统：排版度量（流式网格）。

旧渲染器把高度、间距、字号写成散落各处的魔法数字，导致：
- 卡片高度靠硬编码累加，内容多一点就溢出、少一点就留白；
- 不同宽度下字号不缩放，520 宽的卡片标题挤成一团。

本模块把所有尺寸统一由「卡片宽度 + 密度」推导：
- 字号走一条模块化比例阶（modular scale），保证层级清晰；
- 间距走 4pt 栅格，保证节奏统一；
- 圆角、描边、头像、栅栏间距全部同源缩放。
"""

from __future__ import annotations

from dataclasses import dataclass

# 三种密度：舒展（海报/展厅）、常规、紧凑（信息流）
DENSITIES = ("airy", "normal", "compact")

# 基准宽度：所有尺寸以 800 宽为 1.0 缩放
BASE_WIDTH = 800.0


def _snap(value: float, step: int = 1, minimum: int = 1) -> int:
    """把浮点尺寸吸附到整数栅格，避免半像素造成的模糊描边。"""
    if step <= 1:
        return max(minimum, int(round(value)))
    return max(minimum, int(round(value / step)) * step)


@dataclass(frozen=True, slots=True)
class Metrics:
    """一张卡片的全部尺寸令牌。"""

    width: int
    density: str
    scale: float

    # --- 栅格 ---
    unit: int          # 4pt 栅格基本单位
    pad: int           # 卡片左右内边距
    pad_top: int       # 卡片上内边距
    pad_bottom: int    # 卡片下内边距
    gutter: int        # 分栏间距
    gap_2xs: int
    gap_xs: int
    gap_sm: int
    gap_md: int
    gap_lg: int
    gap_xl: int
    gap_2xl: int

    # --- 圆角 / 描边 ---
    radius_card: int
    radius_panel: int
    radius_media: int
    radius_chip: int
    hairline: int
    border: int

    # --- 字号阶 ---
    f_eyebrow: int     # 眉标（平台 / 类型）
    f_meta: int        # 元信息（时间 / 句柄）
    f_display: int     # 巨型标题（沉浸式）
    f_title: int       # 主标题
    f_subtitle: int    # 副标题
    f_body: int        # 正文
    f_quote: int       # 引用 / 评论正文
    f_label: int       # 数据标签
    f_value: int       # 数据数值
    f_caption: int     # 图注 / 编号
    f_footer: int      # 页脚链接

    # --- 行距倍率 ---
    lh_tight: float
    lh_snug: float
    lh_normal: float
    lh_loose: float

    # --- 元件 ---
    avatar: int
    avatar_sm: int
    chip_h: int
    shadow_blur: int
    shadow_inset: int
    media_gap: int

    @property
    def content_width(self) -> int:
        """去掉左右内边距后的可用宽度。"""
        return max(120, self.width - self.pad * 2)

    def col(self, total: int, span: int, columns: int = 12) -> int:
        """12 栏网格取列宽（含栏间距）。"""
        columns = max(1, columns)
        span = max(1, min(columns, span))
        gaps = self.gutter * (columns - 1)
        unit = (total - gaps) / columns
        return max(1, int(round(unit * span + self.gutter * (span - 1))))


def _font_ladder(scale: float, density: str) -> dict[str, int]:
    """模块化比例阶：ratio 越大层级对比越强。"""
    ratio = 1.24 if density == "airy" else (1.18 if density == "normal" else 1.14)
    base = 17.0 * scale
    if density == "compact":
        base *= 0.97
    elif density == "airy":
        base *= 1.02

    def step(n: float) -> int:
        return _snap(base * (ratio ** n), 1, 9)

    return {
        "f_eyebrow": step(-1.6),
        "f_meta": step(-1.2),
        "f_caption": step(-1.5),
        "f_label": step(-1.4),
        "f_value": step(0.2),
        "f_body": step(0.0),
        "f_quote": step(-0.3),
        "f_subtitle": step(1.0),
        "f_title": step(2.6),
        "f_display": step(4.0),
        "f_footer": step(-1.1),
    }


def build_metrics(width: int, density: str = "normal") -> Metrics:
    """由宽度与密度推导全部尺寸。"""
    width = max(520, min(1080, int(width)))
    density = density if density in DENSITIES else "normal"
    scale = width / BASE_WIDTH
    # 小卡片不要把字号压得太小，大卡片不要无限放大
    type_scale = max(0.86, min(1.16, scale))

    unit = _snap(4 * scale, 1, 3)
    if density == "airy":
        pad_mult, gap_mult = 1.16, 1.18
    elif density == "compact":
        pad_mult, gap_mult = 0.82, 0.86
    else:
        pad_mult, gap_mult = 1.0, 1.0

    pad = _snap(44 * scale * pad_mult, unit, unit * 4)
    fonts = _font_ladder(type_scale, density)

    return Metrics(
        width=width,
        density=density,
        scale=scale,
        unit=unit,
        pad=pad,
        pad_top=_snap(pad * 0.86, unit, unit * 3),
        pad_bottom=_snap(pad * 0.78, unit, unit * 3),
        gutter=_snap(20 * scale * gap_mult, unit, unit),
        gap_2xs=_snap(unit * 1.0, 1, 2),
        gap_xs=_snap(unit * 2.0 * gap_mult, 1, 4),
        gap_sm=_snap(unit * 3.0 * gap_mult, 1, 6),
        gap_md=_snap(unit * 4.5 * gap_mult, 1, 8),
        gap_lg=_snap(unit * 6.5 * gap_mult, 1, 12),
        gap_xl=_snap(unit * 9.0 * gap_mult, 1, 16),
        gap_2xl=_snap(unit * 12.0 * gap_mult, 1, 20),
        radius_card=_snap(28 * scale, 1, 16),
        radius_panel=_snap(20 * scale, 1, 12),
        radius_media=_snap(16 * scale, 1, 8),
        radius_chip=_snap(999, 1, 1),
        hairline=1,
        border=_snap(1.5 * scale, 1, 1),
        avatar=_snap(56 * scale, 2, 34),
        avatar_sm=_snap(34 * scale, 2, 22),
        chip_h=_snap(30 * scale, 2, 22),
        shadow_blur=_snap(18 * scale, 1, 8),
        shadow_inset=_snap(10 * scale, 1, 4),
        media_gap=_snap(10 * scale, 1, 5),
        lh_tight=1.14 if density == "compact" else 1.16,
        lh_snug=1.28 if density == "compact" else 1.32,
        lh_normal=1.44 if density == "compact" else (1.52 if density == "airy" else 1.48),
        lh_loose=1.62 if density == "compact" else 1.72,
        **fonts,
    )
