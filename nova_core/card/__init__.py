"""Nova 卡片设计系统。

分层职责：

* palette  颜色令牌与对比度工具
* metrics  由宽度 + 密度推导的排版度量
* typeset  字体发现、换行、绘制
* surface  Pillow 绘制原语（渐变、玻璃、噪点、圆角、阴影）
* model    把解析结果归一为渲染数据模型
* theme    主题配方（8 套 + 跟随平台）与布局预设（4 套）
* blocks   可测量、可组合的排版区块
* engine   装配与渲染入口
"""

from __future__ import annotations

from .engine import RenderContext, build_context, render_card_image
from .metrics import BASE_WIDTH, DENSITIES, Metrics, build_metrics
from .model import (
    CardModel,
    CommentItem,
    MediaItem,
    QuoteItem,
    build_model,
    compact_number,
    format_ts,
    normalize_comments,
    parse_stats,
)
from .palette import (
    PLATFORM_ACCENTS,
    Palette,
    contrast_ratio,
    ensure_contrast,
    get_palette,
    hex_to_rgb,
    platform_accent,
    readable_ink,
)
from .theme import (
    AUTO_THEME_KEY,
    LAYOUT_ALIASES,
    LAYOUT_KEYS,
    LAYOUTS,
    PLATFORM_THEMES,
    THEME_ALIASES,
    THEME_KEYS,
    THEMES,
    LayoutPreset,
    ThemeRecipe,
    get_layout,
    get_theme,
    is_auto_theme,
    resolve_layout_key,
    resolve_mode,
    resolve_theme_key,
    resolve_theme_key_for_platform,
)
from .typeset import TypeSetter, clean_text, discover_fonts, limit_chars

__all__ = [
    "AUTO_THEME_KEY",
    "BASE_WIDTH",
    "CardModel",
    "CommentItem",
    "DENSITIES",
    "LAYOUTS",
    "LAYOUT_ALIASES",
    "LAYOUT_KEYS",
    "LayoutPreset",
    "MediaItem",
    "Metrics",
    "PLATFORM_ACCENTS",
    "PLATFORM_THEMES",
    "Palette",
    "QuoteItem",
    "RenderContext",
    "THEMES",
    "THEME_ALIASES",
    "THEME_KEYS",
    "ThemeRecipe",
    "TypeSetter",
    "build_context",
    "build_metrics",
    "build_model",
    "clean_text",
    "compact_number",
    "contrast_ratio",
    "discover_fonts",
    "ensure_contrast",
    "format_ts",
    "get_layout",
    "get_palette",
    "get_theme",
    "hex_to_rgb",
    "is_auto_theme",
    "limit_chars",
    "normalize_comments",
    "parse_stats",
    "platform_accent",
    "readable_ink",
    "render_card_image",
    "resolve_layout_key",
    "resolve_mode",
    "resolve_theme_key",
    "resolve_theme_key_for_platform",
]
