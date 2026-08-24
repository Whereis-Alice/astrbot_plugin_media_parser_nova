"""精美解析卡片渲染模块（现代卡片风格 v2）

使用 Pillow 将解析结果渲染为一张现代风格的分享卡片图片，
支持深色 / 浅色两套精修主题，包含：

- 顶部横幅：视频封面或图集首图全宽展示，柔和渐变 scrim + 标题浮层
- 毛玻璃悬浮徽章组（平台圆点徽标 / 类型 / 时间）与毛玻璃播放按钮
- 圆形作者头像、昵称与签名
- 正文简介、毛玻璃数据统计徽章（时长 / 点赞 / 投币 / 收藏 / 播放等）
- 图集网格（超过 6 张显示 +N）、转发内容引用卡片
- 底部链接与可配置署名

所有绘图操作均为 CPU 密集的同步任务，由调用方通过 asyncio.to_thread
放到后台线程执行，避免阻塞 AstrBot 事件循环。
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from ..logger import logger
from .data import ImageContent, ParseResult
from .task import PathTask
from .utils import fmt_duration

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
except ImportError:  # pragma: no cover
    Image = ImageDraw = ImageFilter = ImageFont = None
    ImageOps = None

try:
    _LANCZOS = Image.Resampling.LANCZOS
except AttributeError:  # Pillow < 9.1
    _LANCZOS = Image.LANCZOS  # type: ignore[attr-defined]

DEFAULT_WATERMARK_TAG = "Nova解析"


# ============================ 文本与统计处理 ============================

# 常见 emoji 区域（含 ZWJ 序列、变体选择符、肤色修饰符）
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001F0FF"    # 麻将 / 扑克
    "\U0001F100-\U0001F64F"    # 数字符号 - 表情符号
    "\U0001F680-\U0001F6FF"    # 交通
    "\U0001F700-\U0001F77F"    # 炼金术符号
    "\U0001F780-\U0001F7FF"    # 几何图形扩展
    "\U0001F800-\U0001F8FF"    # 补充箭头
    "\U0001F900-\U0001F9FF"    # 补充符号与图案
    "\U0001FA00-\U0001FA6F"    # 国际象棋符号
    "\U0001FA70-\U0001FAFF"    # 符号扩展
    "\U0001D400-\U0001D7FF"    # 数学字母数字符号（花体昵称）
    "\U00002600-\U000026FF"    # 杂项符号
    "\U00002700-\U000027BF"    # 装饰符号
    "\U0000FE00-\U0000FE0F"    # 变体选择符
    "\U0001F1E6-\U0001F1FF"    # 区域指示符（国旗）
    "\U0001F3FB-\U0001F3FF"    # 肤色修饰符
    "\U0000200D"               # 零宽连接符
    "\U000E0020-\U000E007F"    # 标签字符
    "]+"
)

# 统计行 emoji -> 中文标签
_STAT_LABELS = {
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


# 卡片样式版本：视觉样式变化时 +1，使已缓存的旧卡片失效并重新渲染
_CARD_STYLE_VERSION = "18"


def strip_emoji(text: str | None) -> str:
    """移除字符串中的 emoji，避免字体缺失导致渲染成方块。"""
    if not text:
        return ""
    # 兼容归一化：花体/数学字母昵称（如 𝑹𝒐𝒔𝒂𝒍𝒊𝒏𝒅）转回普通字母，避免渲染成方块
    cleaned = unicodedata.normalize("NFKC", text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _EMOJI_RE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def parse_stats_line(stats_line: str | None) -> list[tuple[str, str]]:
    """将类似『👍 1.2万 🪙 8千』的统计行解析为 (标签, 数值) 列表。"""
    if not stats_line:
        return []
    tokens = stats_line.split()
    stats: list[tuple[str, str]] = []
    i = 0
    icons = sorted(_STAT_LABELS.items(), key=lambda kv: len(kv[0]), reverse=True)
    while i < len(tokens):
        token = tokens[i]
        matched = None
        for icon, label in icons:
            if token.startswith(icon):
                matched = label
                rest = strip_emoji(token[len(icon):])
                break
        if matched is not None:
            value = rest
            if not value and i + 1 < len(tokens):
                i += 1
                value = tokens[i]
            if value:
                stats.append((matched, value))
        else:
            clean = strip_emoji(token)
            if clean:
                stats.append((clean, ""))
        i += 1
    return stats


def card_footer_url(result) -> str:
    """返回完整原始链接，协议、路径、查询参数和片段均不省略。"""
    return str(getattr(result, "url", None) or "").strip()


def format_timestamp(ts: int | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return None


# ============================ 视觉参数表 ============================


class _L:
    """布局 / 字号 / 圆角 / 透明度常量表（重设计后所有魔法数字集中于此）"""

    # --- 画布 ---
    PAD = 44                  # 卡片左右内边距
    RADIUS = 30               # 卡片圆角
    GRID_GAP = 12             # 图集网格间距
    SHADOW_BLUR = 16          # 投影高斯半径
    SHADOW_INSET = 10         # 投影内缩

    # --- 顶部横幅 ---
    HERO_RATIO = 9 / 16       # 横幅宽高比
    HERO_BADGE_TOP = 24       # 悬浮徽章距顶
    HERO_BADGE_H = 42         # 悬浮徽章高度
    HERO_BADGE_GAP = 10       # 徽章间距
    TOP_SCRIM_ALPHA = 90      # 顶部 scrim 最大透明度（徽章可读性）
    TOP_SCRIM_END = 0.30      # 顶部 scrim 结束位置
    PLAY_R = 46               # 播放按钮半径

    # --- 纯文本头部 ---
    HEAD_BAR_W = 64           # accent 短横条宽度
    HEAD_BAR_H = 6            # accent 短横条高度
    HEAD_BAR_TOP = 24         # accent 短横条距顶
    HEAD_PILL_H = 42          # 平台徽章高度

    # --- 作者行 ---
    AVATAR = 72               # 头像直径
    AVATAR_RING_W = 2         # 头像描边环宽
    TITLE_AVATAR = 100       # 标题行头像直径（包含标题+作者两行）

    # --- 统计徽章 ---
    STAT_H = 40               # 统计药丸高度
    STAT_PAD_X = 18           # 统计药丸水平内边距
    STAT_GAP = 10             # 药丸间距
    STAT_ROW_GAP = 12         # 药丸行距
    STAT_LABEL_VALUE_GAP = 6  # 标签与数值间距

    # --- 图集 ---
    GRID_RADIUS = 18          # 图集圆角
    GRID_SINGLE_MAX = 460     # 单图最大边长

    # --- 引用 ---
    QUOTE_RADIUS = 18
    QUOTE_BAR_W = 6

    # --- 页脚 ---
    FOOTER_H = 108
    WM_DOT = 10               # 水印圆点直径
    WM_DOT_GAP = 8            # 水印圆点与文字间距

    # --- 毛玻璃 ---
    GLASS_BLUR = 10           # 毛玻璃背景模糊半径
    HERO_GLASS_TINT = (12, 15, 24)      # 横幅上毛玻璃底色
    HERO_GLASS_TINT_ALPHA = 105
    HERO_GLASS_BORDER_ALPHA = 64

    # --- 字号 ---
    F_PLATFORM = 22
    F_CHIP = 20
    F_TIME = 19
    F_TITLE = 36
    F_TITLE_LINE_H = 52
    F_DESC = 25
    F_DESC_LINE_H = 40
    F_STAT_LABEL = 20
    F_STAT_VALUE = 21
    F_NAME = 26
    F_SIGN = 19
    F_ONLINE = 21
    F_QUOTE = 24
    F_QUOTE_LINE_H = 36
    F_FOOT = 20
    F_PLUS = 36
    F_INITIAL = 26


# ============================ 主题与平台配色 ============================

PLATFORM_COLORS = {
    "bilibili": "#FB7299",
    "douyin": "#2EF2EE",
    "kuaishou": "#FF7E00",
    "weibo": "#FF8200",
    "xiaohongshu": "#FF2442",
    "twitter": "#55ACEE",
    "acfun": "#FD4C5D",
    "nga": "#66C0F4",
    "youtube": "#FF4E45",
    "tiktok": "#2EF2EE",
    "website": "#8B7CF6",
    "default": "#8B7CF6",
}


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _with_alpha(rgb: tuple[int, int, int], alpha: int) -> tuple[int, int, int, int]:
    return (rgb[0], rgb[1], rgb[2], alpha)


def _mix(
    a: tuple[int, int, int], b: tuple[int, int, int], ratio: float
) -> tuple[int, int, int]:
    """按 ratio 将颜色 a 向 b 混合。"""
    return tuple(round(a[i] + (b[i] - a[i]) * ratio) for i in range(3))  # type: ignore[return-value]


class _Theme:
    """一套卡片配色方案"""

    def __init__(
        self,
        *,
        gradient_top: str,
        gradient_bottom: str,
        border: str,
        text_primary: str,
        text_secondary: str,
        text_tertiary: str,
        pill_bg: str,
        quote_bg: str,
        divider: str,
        shadow_alpha: int,
        stat_pill_bg: str,
        glow_alpha: int,
        frost_alpha: int,
        frost_border_alpha: int,
        border_alpha: int,
        placeholder_top: tuple[int, int, int],
        placeholder_bottom: tuple[int, int, int],
    ):
        self.gradient_top = _hex_to_rgb(gradient_top)
        self.gradient_bottom = _hex_to_rgb(gradient_bottom)
        self.border = _hex_to_rgb(border)
        self.text_primary = _hex_to_rgb(text_primary)
        self.text_secondary = _hex_to_rgb(text_secondary)
        self.text_tertiary = _hex_to_rgb(text_tertiary)
        self.pill_bg = _hex_to_rgb(pill_bg)
        self.quote_bg = _hex_to_rgb(quote_bg)
        self.divider = _hex_to_rgb(divider)
        self.shadow_alpha = shadow_alpha
        self.stat_pill_bg = _hex_to_rgb(stat_pill_bg)
        self.glow_alpha = glow_alpha
        self.frost_alpha = frost_alpha
        self.frost_border_alpha = frost_border_alpha
        self.border_alpha = border_alpha
        self.placeholder_top = placeholder_top
        self.placeholder_bottom = placeholder_bottom


_THEMES = {
    # 深色：深海军蓝层次渐变 + 低饱和品牌色渗透光晕
    "dark": _Theme(
        gradient_top="#242B3F",
        gradient_bottom="#12161F",
        border="#FFFFFF",
        text_primary="#F5F7FC",
        text_secondary="#AEB6C8",
        text_tertiary="#7B8598",
        pill_bg="#FFFFFF",
        quote_bg="#FFFFFF",
        divider="#FFFFFF",
        shadow_alpha=130,
        stat_pill_bg="#FFFFFF",
        glow_alpha=30,
        frost_alpha=14,
        frost_border_alpha=26,
        border_alpha=24,
        placeholder_top=(44, 51, 71),
        placeholder_bottom=(20, 24, 35),
    ),
    # 浅色：干净近白底 + 极轻品牌色光晕
    "light": _Theme(
        gradient_top="#FFFFFF",
        gradient_bottom="#F1F4F9",
        border="#1B2233",
        text_primary="#1A2130",
        text_secondary="#55607A",
        text_tertiary="#8C95A9",
        pill_bg="#1B2233",
        quote_bg="#1B2233",
        divider="#1B2233",
        shadow_alpha=55,
        stat_pill_bg="#1B2233",
        glow_alpha=16,
        frost_alpha=10,
        frost_border_alpha=20,
        border_alpha=14,
        placeholder_top=(228, 233, 242),
        placeholder_bottom=(243, 246, 251),
    ),
}


# ============================ 字体探测 ============================

_FONT_CANDIDATES: dict[str, tuple[tuple[str, str], ...]] = {
    "win32": (
        ("msyh.ttc", "msyhbd.ttc"),        # 微软雅黑
        ("simhei.ttf", "msyhbd.ttc"),      # 黑体
        ("Deng.ttf", "Dengb.ttf"),         # 等线
        ("simsun.ttc", "simsun.ttc"),      # 宋体
        ("NotoSansSC-VF.ttf", "NotoSansSC-VF.ttf"),
    ),
    "darwin": (
        ("PingFang.ttc", "PingFang.ttc"),  # 苹方
        ("Hiragino Sans GB.ttc", "Hiragino Sans GB.ttc"),
        ("STHeiti Medium.ttc", "STHeiti Medium.ttc"),
    ),
    "linux": (
        ("NotoSansCJK-Regular.ttc", "NotoSansCJK-Bold.ttc"),
        ("SourceHanSansSC-Regular.otf", "SourceHanSansSC-Bold.otf"),
        ("wqy-zenhei.ttc", "wqy-zenhei.ttc"),
        ("wqy-microhei.ttc", "wqy-microhei.ttc"),
        ("DroidSansFallbackFull.ttf", "DroidSansFallbackFull.ttf"),
        ("arpluminghk-regular.ttf", "arpluminghk-regular.ttf"),
    ),
}

_FONT_DIRS = [
    "C:/Windows/Fonts",
    "/System/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "/usr/share/fonts/opentype/noto",
    "/usr/share/fonts/opentype/noto-cjk",
    "/usr/share/fonts/truetype/noto-cjk",
    "/usr/share/fonts/noto-cjk",
    "/usr/share/fonts/truetype/wqy",
    "/usr/share/fonts/truetype/droid",
    "/usr/share/fonts/truetype/arphic",
    "/usr/share/fonts/opentype/source-han-sans",
    "/usr/local/share/fonts",
]

# 内置兜底背景图：原创无文字 Nova 抽象纹理，封面/横幅加载失败时使用。
_FALLBACK_BG_PATH = Path(__file__).resolve().parent / "assets" / "fallback_nova.png"


def _discover_fonts(custom_path: str | None = None) -> tuple[str | None, str | None]:
    """查找可用的中文字体，返回 (常规字体, 粗体字体) 路径。"""
    if custom_path:
        p = Path(custom_path).expanduser()
        if p.is_dir():
            for ext in ("*.ttf", "*.ttc", "*.otf"):
                found = sorted(p.glob(ext))
                if found:
                    return str(found[0]), str(found[-1] if len(found) > 1 else found[0])
        elif p.is_file():
            return str(p), str(p)
        logger.warning(f"渲染字体路径无效，将自动探测系统字体: {custom_path}")

    platform = sys.platform
    candidates = _FONT_CANDIDATES.get(platform, _FONT_CANDIDATES["linux"])
    dirs = [Path(d) for d in _FONT_DIRS]

    for regular_name, bold_name in candidates:
        for d in dirs:
            reg = d / regular_name
            bol = d / bold_name
            if reg.exists():
                if bol.exists():
                    return str(reg), str(bol)
                return str(reg), None

    # 兜底：任意目录下存在的中文字体
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.suffix.lower() in {".ttf", ".ttc", ".otf"}:
                return str(p), None
    return None, None


# ============================ 渲染器 ============================


# 可选卡片布局：standard 标准横幅 / magazine 双栏杂志 /
# immersive 沉浸全屏 / feed 社交动态流
LAYOUT_NAMES = ("standard", "magazine", "immersive", "feed")
SKIN_NAMES = ("nova", "editorial", "signal", "poster", "neon", "bilibili")


class ShareCardRenderer:
    """将 ParseResult 渲染为精美卡片图片"""

    def __init__(
        self,
        cache_dir: Path,
        *,
        enabled: bool = True,
        width: int = 800,
        theme: str = "dark",
        font_path: str | None = None,
        layout: str = "standard",
        skin: str = "nova",
        cover_full_size: bool = False,
        show_play_button: bool = False,
        watermark: str = DEFAULT_WATERMARK_TAG,
        hot_comment_max_chars: int = 180,
    ):
        self.cache_dir = cache_dir
        self.enabled = enabled and Image is not None
        self.width = max(520, min(1080, int(width)))
        self.theme_name = theme if theme in _THEMES else "dark"
        self.layout_name = layout if layout in LAYOUT_NAMES else "standard"
        skin_aliases = {
            "Nova 原生": "nova",
            "原生": "nova",
            "编辑室": "editorial",
            "信号终端": "signal",
            "海报档案": "poster",
            "霓虹夜景": "neon",
            "霓虹": "neon",
            "哔哩哔哩风格": "bilibili",
            "哔哩哔哩": "bilibili",
            "B站动态": "bilibili",
            "B站风格": "bilibili",
        }
        skin = skin_aliases.get(str(skin or "").strip(), str(skin or "").strip().lower())
        self.skin_name = skin if skin in SKIN_NAMES else "nova"
        self.font_path = font_path
        self.show_play_button = bool(show_play_button)
        self.cover_full_size = cover_full_size
        self.watermark = (str(watermark or "").strip() or DEFAULT_WATERMARK_TAG)[:32]
        try:
            comment_limit = int(hot_comment_max_chars)
        except (TypeError, ValueError):
            comment_limit = 180
        self.hot_comment_max_chars = max(60, min(600, comment_limit))
        self._regular_font: str | None = None
        self._bold_font: str | None = None
        self._font_cache: dict[tuple[int, bool], Any] = {}
        self._measure = ImageDraw.Draw(Image.new("RGBA", (1, 1))) if Image else None

    # ---------- 字体 ----------

    def _load_fonts(self) -> None:
        self._regular_font, self._bold_font = _discover_fonts(self.font_path)
        if not self._regular_font:
            logger.warning(
                "未找到可用的中文字体，解析卡片文字可能显示为方块，"
                "可在插件配置 RENDER_FONT_PATH 中指定字体文件"
            )

    def _font(self, size: int, bold: bool = False) -> Any:
        key = (size, bold)
        if key in self._font_cache:
            return self._font_cache[key]
        if self._regular_font is None:
            self._load_fonts()
        path = self._bold_font if bold and self._bold_font else self._regular_font
        try:
            if path:
                font = ImageFont.truetype(str(path), size)
            else:
                font = ImageFont.load_default()
        except Exception:
            logger.exception(f"加载字体失败: {path}")
            font = ImageFont.load_default()
        self._font_cache[key] = font
        return font

    def _bold_stroke(self, bold: bool) -> int:
        """使用常规字体模拟粗体时的描边宽度。"""
        return 2 if bold and not self._bold_font and self._regular_font else 0

    # ---------- 文本工具 ----------

    def _text_width(self, text: str, font: Any) -> int:
        return math.ceil(self._measure.textlength(text, font=font))

    def _wrap(self, text: str, font: Any, max_width: int) -> list[str]:
        """按实际宽度换行；CJK 可逐字折行，短 ASCII 词尽量保持完整。"""
        lines: list[str] = []
        for raw in text.split("\n"):
            if not raw:
                lines.append("")
                continue
            current = ""
            tokens = re.findall(r"[\x21-\x7e]+|[ \t]+|.", raw)
            for token in tokens:
                if token.isspace():
                    if current and not current.endswith(" "):
                        candidate = current + " "
                        if self._text_width(candidate, font) <= max_width:
                            current = candidate
                    continue

                if self._text_width(current + token, font) <= max_width:
                    current += token
                    continue

                if current:
                    lines.append(current.rstrip())
                    current = ""

                if self._text_width(token, font) <= max_width:
                    current = token
                    continue

                for ch in token:
                    if current and self._text_width(current + ch, font) > max_width:
                        lines.append(current.rstrip())
                        current = ch
                    else:
                        current += ch
            lines.append(current.rstrip())
        return lines

    def _fit_lines(
        self, text: str, font: Any, max_width: int, max_lines: int
    ) -> list[str]:
        lines = self._wrap(text, font, max_width)
        if len(lines) <= max_lines:
            return lines
        result = lines[: max_lines - 1]
        last = lines[max_lines - 1]
        ellipsis = "…"
        while last and self._text_width(last + ellipsis, font) > max_width:
            last = last[:-1]
        result.append(last + ellipsis)
        return result

    @staticmethod
    def _limit_text(text: str, max_chars: int) -> str:
        """按字符数限制文本，并让后续排版完整显示限制后的内容。"""
        value = str(text or "").strip()
        if len(value) <= max_chars:
            return value
        return value[: max(1, max_chars - 1)].rstrip() + "…"

    def _line_height(self, font: Any) -> int:
        ascent, descent = font.getmetrics()
        return ascent + descent

    def _ellipsize(self, text: str, font: Any, max_width: int) -> str:
        """把单行文本截断到指定宽度，并保留省略号。"""
        value = str(text or "")
        if max_width <= 0:
            return ""
        if self._text_width(value, font) <= max_width:
            return value
        ellipsis = "…"
        while value and self._text_width(value + ellipsis, font) > max_width:
            value = value[:-1]
        return value + ellipsis if value else ""

    def _url_layout(
        self,
        result: ParseResult,
        max_width: int,
        *,
        initial_size: int = 16,
        min_size: int = 11,
        target_lines: int = 5,
    ) -> tuple[list[str], int, int]:
        """完整保留链接并按字符换行，只通过缩小字号控制行数。"""
        text = card_footer_url(result)
        size = max(min_size, initial_size)
        if not text:
            return [], size, size + 6
        lines = self._wrap(text, self._font(size), max(1, max_width))
        while size > min_size and len(lines) > target_lines:
            size -= 1
            lines = self._wrap(text, self._font(size), max(1, max_width))
        return lines, size, size + 6

    def _footer_metrics(
        self,
        result: ParseResult,
        inner_w: int,
    ) -> tuple[int, list[str], int, int]:
        """计算原生布局完整链接页脚的动态高度。"""
        lines, size, line_h = self._url_layout(
            result,
            inner_w,
            initial_size=16 if self.width >= 680 else 14,
            target_lines=5,
        )
        url_h = len(lines) * line_h
        footer_h = 42 + url_h + (12 if lines else 0) + 38
        return footer_h, lines, size, line_h

    def _draw_text(
        self,
        draw: Any,
        xy: tuple[int, int],
        text: str,
        size: int,
        fill: str | tuple[int, int, int],
        bold: bool = False,
    ) -> None:
        font = self._font(size, bold)
        stroke = self._bold_stroke(bold)
        draw.text(
            xy,
            text,
            font=font,
            fill=fill,
            stroke_width=stroke,
            stroke_fill=fill,
        )

    @staticmethod
    def _text_ink_left(text: str, font) -> int:
        """返回文本首字符的实际墨迹左偏移（用于多行文字视觉左对齐）。"""
        if not text:
            return 0
        try:
            probe = Image.new("RGBA", (256, 64), (0, 0, 0, 0))
            pd = ImageDraw.Draw(probe)
            pd.text((0, 0), text[0], font=font, fill=(255, 255, 255, 255))
            bbox = probe.getbbox()
            return bbox[0] if bbox else 0
        except Exception:
            return 0

    def _fit_title_single(self, text: str, max_w: int):
        """标题单行显示：优先自动缩字号放完整标题，实在放不下再截断。

        返回 (字号, 字体, 行列表)。
        """
        for size in (_L.F_TITLE, 33, 30, 27, 24):
            font = self._font(size, bold=True)
            if self._text_width(text, font) <= max_w:
                return size, font, [text]
        font = self._font(24, bold=True)
        lines = self._fit_lines(text, font, max_w, 1)
        return 24, font, lines

    # ---------- 图片工具 ----------

    @staticmethod
    def _open_image(path: Path) -> Image.Image:
        with Image.open(path) as im:
            im.load()
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA")
            return im.copy()

    @staticmethod
    def _cover_fit(image: Image.Image, box_w: int, box_h: int) -> Image.Image:
        """等比缩放并居中裁剪填满目标区域。"""
        img = image.convert("RGB")
        iw, ih = img.size
        if iw <= 0 or ih <= 0:
            raise ValueError("invalid image size")
        scale = max(box_w / iw, box_h / ih)
        nw, nh = math.ceil(iw * scale), math.ceil(ih * scale)
        img = img.resize((nw, nh), _LANCZOS)
        x = (nw - box_w) // 2
        y = (nh - box_h) // 2
        return img.crop((x, y, x + box_w, y + box_h))

    @staticmethod
    def _contain_fit(
        image: Image.Image,
        box_w: int,
        box_h: int,
        background: tuple[int, int, int],
    ) -> Image.Image:
        """等比缩放并完整放入目标区域，空白处使用指定背景填充。"""
        img = ImageOps.contain(
            image.convert("RGB"),
            (box_w, box_h),
            method=_LANCZOS,
        )
        canvas = Image.new("RGB", (box_w, box_h), background)
        canvas.paste(
            img,
            ((box_w - img.width) // 2, (box_h - img.height) // 2),
        )
        return canvas

    @staticmethod
    def _rounded_image(image: Image.Image, radius: int) -> Image.Image:
        img = image.convert("RGBA")
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, img.size[0] - 1, img.size[1] - 1), radius=radius, fill=255
        )
        img.putalpha(mask)
        return img

    @staticmethod
    def _circle_avatar(image: Image.Image, size: int) -> Image.Image:
        img = image.convert("RGBA").resize((size, size), _LANCZOS)
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
        img.putalpha(mask)
        return img

    @staticmethod
    def _gradient(size: tuple[int, int], top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
        w, h = size
        grad = Image.new("RGB", (1, max(h, 1)))
        for y in range(max(h, 1)):
            ratio = y / max(h - 1, 1)
            color = tuple(round(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3))
            grad.putpixel((0, y), color)
        return grad.resize((w, h))

    # ---------- 毛玻璃与光影 ----------

    @staticmethod
    def _glass(
        canvas: Image.Image,
        box: tuple[int, int, int, int],
        radius: int,
        tint_rgb: tuple[int, int, int],
        tint_alpha: int,
        border_rgb: tuple[int, int, int],
        border_alpha: int,
        blur: int = _L.GLASS_BLUR,
    ) -> None:
        """在画布指定区域绘制毛玻璃圆角块（真实背景模糊 +  tint + 细描边）。"""
        x0, y0, x1, y1 = box
        if x1 <= x0 or y1 <= y0:
            return
        region = canvas.crop(box).filter(ImageFilter.GaussianBlur(blur))
        region.alpha_composite(
            Image.new("RGBA", region.size, (*tint_rgb, tint_alpha))
        )
        mask = Image.new("L", region.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, region.size[0] - 1, region.size[1] - 1), radius=radius, fill=255
        )
        canvas.paste(region, (x0, y0), mask)
        if border_alpha > 0:
            # 描边先画到透明层再混合，避免直接 Draw 替换像素产生半透明空洞
            border_layer = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
            ImageDraw.Draw(border_layer).rounded_rectangle(
                (0, 0, x1 - x0 - 1, y1 - y0 - 1), radius=radius,
                outline=(*border_rgb, border_alpha), width=1,
            )
            canvas.alpha_composite(border_layer, (x0, y0))

    @staticmethod
    def _radial_glow(
        w: int, h: int, rgb: tuple[int, int, int], alpha: int
    ) -> Image.Image:
        """生成一团柔和的径向光晕（品牌色渗透渐变用）。"""
        base = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(base).ellipse(
            (-w // 3, -h // 2, w // 2, h // 2), fill=(*rgb, alpha)
        )
        return base.filter(ImageFilter.GaussianBlur(max(w, h) // 5))

    @staticmethod
    def _scrim(
        w: int,
        h: int,
        *,
        start: float,
        max_alpha: int,
        power: float = 1.6,
        invert: bool = False,
    ) -> Image.Image:
        """生成垂直渐变黑色遮罩。invert=True 时从顶部开始衰减。"""
        mask = Image.new("L", (1, max(h, 1)))
        for yy in range(max(h, 1)):
            t = yy / max(h - 1, 1)
            if invert:
                ratio = max(0.0, 1.0 - t / max(start, 1e-6))
            else:
                ratio = max(0.0, (t - start) / max(1.0 - start, 1e-6))
            alpha = int(max_alpha * (ratio ** power))
            mask.putpixel((0, yy), min(255, alpha))
        mask = mask.resize((w, h))
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 255))
        layer.putalpha(mask)
        return layer

    # ---------- 主流程 ----------

    async def render(
        self,
        result: ParseResult,
        cache_key: str | None = None,
        existing: Path | None = None,
    ) -> Path | None:
        """异步渲染卡片，失败时返回 None（由调用方回退到文本输出）。"""
        if not self.enabled:
            return None
        try:
            if existing is not None and existing.exists():
                return existing
            out_path = self._output_path(cache_key, result)
            if out_path.exists():
                return out_path
            images = await self._collect_images(result)
            return await asyncio.to_thread(self._render_sync, result, images, out_path)
        except Exception:
            logger.exception("解析卡片渲染失败，已回退到文本输出")
            return None

    def _output_path(self, cache_key: str | None, result: ParseResult) -> Path:
        warnings_str = "|".join(result.extra.get("limit_warnings") or [])
        payload = (
            cache_key
            or f"{result.platform.name}|{result.title}|{result.timestamp}|{result.url}"
        )
        theme_key = self.theme_name if self.skin_name == "nova" else "skin-fixed"
        layout_key = self.layout_name if self.skin_name == "nova" else "skin-independent"
        digest = hashlib.md5(
            f"{_CARD_STYLE_VERSION}|{self.skin_name}|{theme_key}|{self.width}|{layout_key}|{self.cover_full_size}|{self.show_play_button}|{self.watermark}|{self.hot_comment_max_chars}|{payload}|{warnings_str}".encode("utf-8")
        ).hexdigest()[:16]
        return self.cache_dir / f"card_{digest}.png"

    async def _collect_images(self, result: ParseResult) -> dict[str, Any]:
        """并发获取头像 / 视频封面 / 图集图片的本地路径。"""
        images: dict[str, Any] = {"avatar": None, "hero": None, "grid": []}

        tasks: list[tuple[str, PathTask]] = []
        if result.author and result.author.avatar:
            tasks.append(("avatar", result.author.avatar))

        video = result.video
        hero_task: PathTask | None = None
        if video is not None and video.cover is not None:
            hero_task = video.cover
            tasks.append(("hero", hero_task))

        grid_tasks: list[PathTask] = []
        seen: set[int] = set()
        for t in result.all_grid_images:
            if id(t) not in seen:
                seen.add(id(t))
                grid_tasks.append(t)
        for g in result.graphics:
            if isinstance(g, ImageContent) and id(g.path_task) not in seen:
                seen.add(id(g.path_task))
                grid_tasks.append(g.path_task)

        # 图集中可能已包含视频封面，去重后单独取封面
        hero_id = id(hero_task) if hero_task else None
        for t in grid_tasks:
            if id(t) == hero_id:
                continue
            tasks.append(("grid", t))

        if not tasks:
            return images

        results = await asyncio.gather(
            *[t.safe_get() for _, t in tasks], return_exceptions=True
        )
        for (kind, _), path in zip(tasks, results):
            if not path:
                continue
            if kind == "avatar":
                images["avatar"] = path
            elif kind == "hero":
                images["hero"] = path
            else:
                images["grid"].append(path)
        # 封面下载失败时使用内置兜底背景图
        if images["hero"] is None and hero_task is not None and _FALLBACK_BG_PATH.is_file():
            images["hero"] = _FALLBACK_BG_PATH
        return images

    # ---------- 布局辅助 ----------

    @staticmethod
    def _rounded_image_top(image: Image.Image, radius: int) -> Image.Image:
        """仅保留顶部圆角的图片（用于全宽横幅）。"""
        img = image.convert("RGBA")
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, img.size[0] - 1, img.size[1] - 1), radius=radius,
            corners=(True, True, False, False), fill=255,
        )
        img.putalpha(mask)
        return img

    def _fallback_cover(
        self,
        w: int,
        h: int,
        theme,
        accent_rgb,
        radius: int,
        top_only: bool = False,
    ) -> Image.Image:
        """封面/横幅加载失败时使用内置兜底背景图；图片不可用时回退渐变占位。"""
        try:
            if _FALLBACK_BG_PATH.is_file():
                bg = self._open_image(_FALLBACK_BG_PATH)
                bg = self._cover_fit(bg, w, h).convert("RGBA")
                if top_only:
                    return self._rounded_image_top(bg, radius)
                return self._rounded_image(bg, radius)
        except Exception:
            logger.warning("兜底背景图渲染失败，回退渐变占位", exc_info=True)
        ph = self._gradient(
            (w, h), theme.placeholder_top, theme.placeholder_bottom
        ).convert("RGBA")
        ph.alpha_composite(Image.new("RGBA", (w, h), (*accent_rgb, 40)))
        if top_only:
            return self._rounded_image_top(ph, radius)
        return self._rounded_image(ph, radius)

    def _grid_metrics(
        self, n: int, inner_w: int, gap: int
    ) -> tuple[int, int, int, int]:
        """计算图集网格的 (总高度, 列数, 行数, 单元格边长)。"""
        if n <= 0:
            return 0, 0, 0, 0
        if n == 1:
            cols, rows, cell_h = 1, 1, min(inner_w, _L.GRID_SINGLE_MAX)
        elif n == 2:
            cols, rows, cell_h = 2, 1, (inner_w - gap) // 2
        elif n == 3:
            cols, rows, cell_h = 3, 1, (inner_w - gap * 2) // 3
        elif n == 4:
            cols, rows, cell_h = 2, 2, (inner_w - gap) // 2
        else:
            cols, rows, cell_h = 3, 2, (inner_w - gap * 2) // 3
        grid_h = rows * cell_h + (rows - 1) * gap
        return grid_h, cols, rows, cell_h

    def _stat_pill_width(self, label: str, value: str) -> int:
        """统计药丸宽度：水平内边距 + 标签 + 间距 + 数值。"""
        w = _L.STAT_PAD_X * 2
        w += self._text_width(label, self._font(_L.F_STAT_LABEL))
        if value:
            w += _L.STAT_LABEL_VALUE_GAP
            w += self._text_width(value, self._font(_L.F_STAT_VALUE, bold=True))
        return w

    def _build_stat_rows(
        self, stats: list[tuple[str, str]], inner_w: int
    ) -> list[list[tuple[str, str]]]:
        """将统计项按宽度拆分为多行徽章。"""
        rows: list[list[tuple[str, str]]] = []
        if not stats:
            return rows
        pills: list[tuple[str, str]] = []
        row_w = 0
        for label, value in stats:
            w = self._stat_pill_width(label, value)
            if pills and row_w + w + _L.STAT_GAP > inner_w:
                rows.append(pills)
                pills = []
                row_w = 0
            pills.append((label, value))
            row_w += w + _L.STAT_GAP
        if pills:
            rows.append(pills)
        return rows

    def _platform_pill_width(self, text: str) -> int:
        """平台徽标（accent 圆点 + 平台名）宽度。"""
        font = self._font(_L.F_PLATFORM, bold=True)
        return 18 + 12 + 8 + self._text_width(text, font) + 18

    def _normalized_card_comments(self, result: ParseResult) -> list[dict[str, Any]]:
        comments = result.extra.get("hot_comments")
        if not isinstance(comments, list):
            return []
        normalized = []
        for item in comments[:5]:
            if not isinstance(item, dict):
                continue
            message = self._limit_text(
                strip_emoji(str(item.get("message") or "")),
                self.hot_comment_max_chars,
            )
            if not message:
                continue
            normalized.append(
                {
                    "username": strip_emoji(
                        str(item.get("username") or "未知用户")
                    ),
                    "uid": strip_emoji(str(item.get("uid") or "")),
                    "likes": item.get("likes", 0),
                    "time": strip_emoji(str(item.get("time") or "")),
                    "message": message,
                }
            )
        return normalized

    def _append_hot_comments(
        self,
        path: Path,
        result: ParseResult,
    ) -> Path:
        """在已完成的卡片下方追加与当前皮肤匹配的热评版块。"""
        comments = self._normalized_card_comments(result)
        if not comments:
            return path

        base = self._open_image(path).convert("RGBA")
        accent_rgb = _hex_to_rgb(
            PLATFORM_COLORS.get(result.platform.name, PLATFORM_COLORS["default"])
        )
        pad = 30 if self.width >= 680 else 22
        inner_w = self.width - pad * 2
        body_size = 19 if self.width >= 680 else 17
        author_size = 17 if self.width >= 680 else 15
        body_font = self._font(body_size)
        bili_comment_layout = self.skin_name == "bilibili"
        comment_avatar_size = 42 if bili_comment_layout else 0
        text_offset = comment_avatar_size + 14 if bili_comment_layout else 0
        text_w = max(120, inner_w - text_offset)
        prepared = []
        for item in comments:
            lines = self._wrap(item["message"], body_font, text_w)
            user_label = item["username"]
            uid = item["uid"]
            if uid and uid not in user_label:
                user_label = f"{user_label}  ·  {uid}"
            user_lines = self._fit_lines(
                user_label,
                self._font(author_size, bold=True),
                max(80, text_w - 170),
                2,
            )
            content_h = (
                len(user_lines) * (author_size + 6)
                + 7
                + len(lines) * (body_size + 9)
            )
            item_h = max(comment_avatar_size, content_h)
            prepared.append((item, lines, user_lines, item_h))
        panel_h = (
            72
            + sum(item_h for _, _, _, item_h in prepared)
            + max(0, len(prepared) - 1) * 17
            + 18
        )

        if self.skin_name == "editorial":
            bg_top = bg_bottom = (247, 242, 233)
            ink = (34, 32, 29)
            muted = (103, 96, 87)
            divider = (154, 143, 128)
            header = "读者摘录 / HOT COMMENTS"
            radius = 8
        elif self.skin_name == "signal":
            bg_top = (4, 24, 16)
            bg_bottom = (2, 12, 9)
            accent_rgb = (72, 255, 157)
            ink = (218, 255, 234)
            muted = (116, 199, 153)
            divider = (58, 161, 105)
            header = "公开评论 · PUBLIC COMMENTS"
            radius = 3
        elif self.skin_name == "poster":
            bg_top = bg_bottom = (238, 232, 218)
            accent_rgb = (191, 58, 50)
            ink = (37, 34, 31)
            muted = (105, 98, 88)
            divider = (165, 153, 136)
            header = "档案附注 · AUDIENCE NOTES"
            radius = 6
        elif self.skin_name == "neon":
            bg_top = (20, 12, 39)
            bg_bottom = (5, 8, 22)
            accent_rgb = (255, 63, 188)
            ink = (241, 249, 255)
            muted = (158, 190, 212)
            divider = (71, 202, 224)
            header = "NEON REPLIES · 公开热评"
            radius = 5
        elif self.skin_name == "bilibili":
            bg_top = bg_bottom = (255, 255, 255)
            accent_rgb = (251, 114, 153)
            ink = (36, 38, 43)
            muted = (117, 120, 128)
            divider = (225, 228, 232)
            header = "热门评论"
            radius = 8
        else:
            theme = _THEMES[self.theme_name]
            bg_top = theme.gradient_top
            bg_bottom = theme.gradient_bottom
            ink = theme.text_primary
            muted = theme.text_secondary
            divider = theme.divider
            header = "热门评论"
            radius = _L.RADIUS

        panel = self._gradient((self.width, panel_h), bg_top, bg_bottom).convert(
            "RGBA"
        )
        draw = ImageDraw.Draw(panel)
        if self.skin_name == "signal":
            grid = Image.new("RGBA", panel.size, (0, 0, 0, 0))
            grid_draw = ImageDraw.Draw(grid)
            for xx in range(0, self.width, 32):
                grid_draw.line(
                    (xx, 0, xx, panel_h),
                    fill=(72, 255, 157, 24),
                    width=1,
                )
            for yy in range(0, panel_h, 32):
                grid_draw.line(
                    (0, yy, self.width, yy),
                    fill=(72, 255, 157, 24),
                    width=1,
                )
            panel.alpha_composite(grid)
            draw = ImageDraw.Draw(panel)
        elif self.skin_name == "poster":
            draw.rectangle((0, 0, 10, panel_h), fill=(*accent_rgb, 255))
            for hole_y in range(92, panel_h - 10, 54):
                draw.ellipse(
                    (18, hole_y - 4, 26, hole_y + 4),
                    fill=(83, 78, 70),
                )
            draw.rectangle(
                (self.width - 152, 18, self.width - 24, 48),
                outline=(*accent_rgb, 210),
                width=2,
            )
            self._draw_text(
                draw,
                (self.width - 140, 25),
                "ARCHIVE NOTE",
                11,
                accent_rgb,
                bold=True,
            )
        elif self.skin_name == "neon":
            neon_layer = Image.new("RGBA", panel.size, (0, 0, 0, 0))
            neon_draw = ImageDraw.Draw(neon_layer)
            neon_draw.line((18, 0, 18, panel_h), fill=(255, 63, 188, 220), width=5)
            neon_draw.line(
                (self.width - 20, 0, self.width - 20, panel_h),
                fill=(55, 236, 255, 190),
                width=3,
            )
            for yy in range(110, panel_h, 74):
                neon_draw.line(
                    (32, yy, self.width - 34, yy - 28),
                    fill=(55, 236, 255, 28),
                    width=1,
                )
            panel.alpha_composite(neon_layer.filter(ImageFilter.GaussianBlur(5)))
            panel.alpha_composite(neon_layer)
            draw = ImageDraw.Draw(panel)
        elif self.skin_name == "bilibili":
            draw.rectangle((0, 0, self.width, 4), fill=(*accent_rgb, 255))

        draw.line((pad, 28, pad + 70, 28), fill=(*accent_rgb, 255), width=4)
        self._draw_text(draw, (pad, 38), header, 15, ink, bold=True)
        y = 72
        for index, (item, lines, user_lines, item_h) in enumerate(prepared, start=1):
            item_top = y
            text_x = pad + text_offset
            if bili_comment_layout:
                initial = (item["username"][:1] or "?").upper()
                draw.ellipse(
                    (
                        pad,
                        item_top,
                        pad + comment_avatar_size,
                        item_top + comment_avatar_size,
                    ),
                    fill=_mix((255, 255, 255), accent_rgb, 0.18),
                    outline=(*accent_rgb, 150),
                    width=1,
                )
                initial_font = self._font(17, bold=True)
                initial_w = self._text_width(initial, initial_font)
                self._draw_text(
                    draw,
                    (
                        pad + (comment_avatar_size - initial_w) // 2,
                        item_top + 10,
                    ),
                    initial,
                    17,
                    accent_rgb,
                    bold=True,
                )
            author_y = item_top
            first_user_line = user_lines[0] if user_lines else "未知用户"
            self._draw_text(
                draw,
                (text_x, author_y),
                first_user_line,
                author_size,
                accent_rgb if bili_comment_layout else ink,
                bold=True,
            )
            meta_parts = []
            try:
                likes = int(item["likes"] or 0)
            except (TypeError, ValueError):
                likes = 0
            if likes:
                meta_parts.append(f"赞 {likes}")
            if item["time"]:
                meta_parts.append(item["time"])
            meta_text = "  ·  ".join(meta_parts) or f"评论 {index:02d}"
            meta_font = self._font(13)
            meta_text = self._ellipsize(meta_text, meta_font, 160)
            self._draw_text(
                draw,
                (
                    self.width - pad - self._text_width(meta_text, meta_font),
                    item_top + 2,
                ),
                meta_text,
                13,
                muted,
            )
            author_y += author_size + 6
            for extra_line in user_lines[1:]:
                self._draw_text(draw, (text_x, author_y), extra_line, 13, muted)
                author_y += author_size + 6
            body_y = author_y + 7
            for line in lines:
                self._draw_text(
                    draw,
                    (text_x, body_y),
                    line,
                    body_size,
                    ink if bili_comment_layout else muted,
                )
                body_y += body_size + 9
            y = item_top + item_h
            if index < len(prepared):
                draw.line(
                    (text_x, y + 8, self.width - pad, y + 8),
                    fill=(*divider, 90),
                    width=1,
                )
                y += 17

        mask = Image.new("L", panel.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, self.width - 1, panel_h - 1),
            radius=radius,
            fill=255,
        )
        panel.putalpha(mask)
        bottom_margin = {
            "editorial": 18,
            "signal": 16,
            "poster": 18,
            "neon": 16,
            "bilibili": 16,
        }.get(self.skin_name, 14)
        visible_h = max(1, base.height - bottom_margin)
        panel_y = max(0, visible_h - 6)
        canvas = Image.new(
            "RGBA",
            (self.width, panel_y + panel_h + bottom_margin),
            (0, 0, 0, 0),
        )
        canvas.alpha_composite(panel, (0, panel_y))
        canvas.alpha_composite(base, (0, 0))
        canvas.save(path, "PNG", optimize=True)
        return path

    # ---------- 同步绘制 ----------

    def _render_sync(
        self,
        result: ParseResult,
        images: dict[str, Any],
        out_path: Path,
    ) -> Path:
        """按 self.layout_name 分发到具体布局实现。"""
        if self.skin_name == "editorial":
            rendered = self._render_editorial(result, images, out_path)
        elif self.skin_name == "signal":
            rendered = self._render_signal(result, images, out_path)
        elif self.skin_name == "poster":
            rendered = self._render_poster_archive(result, images, out_path)
        elif self.skin_name == "neon":
            rendered = self._render_neon(result, images, out_path)
        elif self.skin_name == "bilibili":
            rendered = self._render_bilibili(result, images, out_path)
        elif self.layout_name == "magazine":
            rendered = self._render_magazine(result, images, out_path)
        elif self.layout_name == "immersive":
            rendered = self._render_immersive(result, images, out_path)
        elif self.layout_name == "feed":
            rendered = self._render_feed(result, images, out_path)
        else:
            rendered = self._render_standard(result, images, out_path)
        return self._append_hot_comments(rendered, result)

    # ==================== 高级皮肤：编辑室 ====================

    def _render_editorial(
        self,
        result: ParseResult,
        images: dict[str, Any],
        out_path: Path,
    ) -> Path:
        """编辑室：纸张底、非对称双栏和信息编排，独立于 Nova 原生布局。"""
        d = self._prep(result, images)
        accent = PLATFORM_COLORS.get(result.platform.name, PLATFORM_COLORS["default"])
        accent_rgb = _hex_to_rgb(accent)
        ink = (34, 32, 29)
        muted = (103, 96, 87)
        soft = (157, 147, 134)
        paper = (247, 242, 233)
        paper_deep = (235, 227, 214)

        pad = 30
        inner_w = self.width - pad * 2
        gap = 26
        left_w = max(220, round(inner_w * 0.42))
        right_w = max(180, inner_w - left_w - gap)
        body_y = 96

        headline_text, body_text, has_real_title = self._advanced_copy(d)
        title_size = 36 if has_real_title else 29
        title_lines = (
            self._wrap(
                headline_text,
                self._font(title_size, bold=True),
                right_w,
            )
            if headline_text
            else []
        )
        max_title_lines = 4 if has_real_title else 6
        while title_size > 22 and len(title_lines) > max_title_lines:
            title_size -= 2
            title_lines = self._wrap(
                headline_text,
                self._font(title_size, bold=True),
                right_w,
            )
        desc_lines = (
            self._fit_lines(body_text, self._font(22), right_w, 5)
            if body_text
            else []
        )
        author_name_size = 20
        author_name_lines: list[str] = []
        author_desc_size = 14
        author_desc_lines: list[str] = []
        if d["author"]:
            while author_name_size > 14:
                candidate = self._wrap(
                    d["name"],
                    self._font(author_name_size, bold=True),
                    right_w - 62,
                )
                if len(candidate) <= 2:
                    break
                author_name_size -= 1
            author_name_lines = self._fit_lines(
                d["name"],
                self._font(author_name_size, bold=True),
                right_w - 62,
                3,
            )
            if d["author_desc"]:
                author_desc_lines = self._fit_lines(
                    d["author_desc"],
                    self._font(author_desc_size),
                    right_w - 62,
                    2,
                )
        author_text_h = (
            len(author_name_lines) * (author_name_size + 6)
            + len(author_desc_lines) * (author_desc_size + 5)
        )
        author_block_h = max(48, author_text_h) + 22 if d["author"] else 0
        (
            editorial_url_lines,
            editorial_url_size,
            editorial_url_line_h,
        ) = self._url_layout(
            result,
            inner_w,
            initial_size=15,
            min_size=11,
            target_lines=5,
        )
        footer_h = 96 + len(editorial_url_lines) * editorial_url_line_h
        stat_count = min(len(d["stats"]), 6)
        gallery_paths = d["grid"][:6]
        gallery_count = len(gallery_paths)
        gallery_gap = 10
        if gallery_count == 1:
            gallery_cols = 1
            gallery_cell_w = inner_w
            gallery_cell_h = min(260, max(190, round(inner_w * 0.32)))
        elif gallery_count == 2:
            gallery_cols = 2
            gallery_cell_w = max(1, (inner_w - gallery_gap) // 2)
            gallery_cell_h = min(240, max(170, round(gallery_cell_w * 0.7)))
        elif gallery_count:
            gallery_cols = 3
            gallery_cell_w = max(1, (inner_w - gallery_gap * 2) // 3)
            gallery_cell_h = min(190, gallery_cell_w)
        else:
            gallery_cols = 0
            gallery_cell_w = 0
            gallery_cell_h = 0
        gallery_rows = (
            math.ceil(gallery_count / gallery_cols) if gallery_cols else 0
        )
        gallery_h = (
            58
            + gallery_rows * gallery_cell_h
            + max(0, gallery_rows - 1) * gallery_gap
            + 18
            if gallery_count
            else 0
        )
        media_h = max(440, min(680, round(left_w * 1.34)))
        title_line_h = title_size + 12
        right_h = 56 + len(title_lines) * title_line_h
        if d["author"]:
            right_h += author_block_h
        if desc_lines:
            right_h += 26 + len(desc_lines) * 34
        if stat_count:
            right_h += 26 + ((stat_count + 1) // 2) * 34
        body_h = max(media_h, right_h + 8)
        card_h = body_y + body_h + gallery_h + footer_h
        total_h = card_h + 18

        canvas = Image.new("RGBA", (self.width, total_h), (0, 0, 0, 0))
        shadow = Image.new("RGBA", (self.width, total_h), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (10, 10, self.width - 10, card_h + 3), radius=8,
            fill=(42, 34, 25, 70),
        )
        canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(12)))
        card = Image.new("RGBA", (self.width, card_h), (*paper, 255))
        card_draw = ImageDraw.Draw(card)

        # 纸张纹理和版心标记，避免仅依赖颜色表达皮肤。
        for yy in range(18, card_h, 28):
            card_draw.line((pad, yy, self.width - pad, yy), fill=(*paper_deep, 80), width=1)
        card_draw.line((pad, 22, pad + 78, 22), fill=(*accent_rgb, 255), width=4)
        card_draw.line((pad, 28, self.width - pad, 28), fill=(*ink, 35), width=1)
        self._draw_text(
            card_draw,
            (pad, 42),
            d["ts"] or "MEDIA NOTE",
            15,
            ink,
            bold=True,
        )
        meta_head = f"{d['platform_text']}  /  {d['content_type']}"
        meta_font = self._font(16, bold=True)
        self._draw_text(
            card_draw,
            (self.width - pad - self._text_width(meta_head, meta_font), 42),
            meta_head,
            16,
            muted,
            bold=True,
        )

        # 左栏：封面作为编辑版心，附带编号和索引条。
        hero = d["hero"]
        try:
            if hero:
                hero_img = self._cover_fit(self._open_image(hero), left_w, body_h)
                hero_img = self._rounded_image(hero_img, 6)
            else:
                hero_img = self._fallback_cover(
                    left_w, body_h, _THEMES["light"], accent_rgb, 6
                )
            card.alpha_composite(hero_img, (pad, body_y))
        except Exception:
            card.alpha_composite(
                self._fallback_cover(left_w, body_h, _THEMES["light"], accent_rgb, 6),
                (pad, body_y),
            )
        card_draw = ImageDraw.Draw(card)
        card_draw.rectangle(
            (pad, body_y, pad + left_w - 1, body_y + body_h - 1),
            outline=(*ink, 110), width=1,
        )
        self._draw_text(card_draw, (pad + 18, body_y + 12), "01", 92, (255, 255, 255, 160), bold=True)
        card_draw.rectangle(
            (pad + 18, body_y + body_h - 58, pad + 112, body_y + body_h - 48),
            fill=(*accent_rgb, 245),
        )
        left_label = f"{d['platform_text']}  ·  {d['content_type']}"
        self._draw_text(
            card_draw, (pad + 18, body_y + body_h - 40), left_label[:30], 16,
            (255, 255, 255, 235), bold=True,
        )

        # 右栏：标题、作者、正文和统计采用杂志式垂直节奏。
        rx = pad + left_w + gap
        y = body_y
        card_draw.line((rx, y, rx + 62, y), fill=(*accent_rgb, 255), width=4)
        y += 16
        section_label = "FEATURE / 解析内容"
        self._draw_text(card_draw, (rx, y), section_label, 15, muted, bold=True)
        y += 30
        for line in title_lines:
            self._draw_text(card_draw, (rx, y), line, title_size, ink, bold=True)
            y += title_line_h
        y += 10

        if d["author"]:
            avatar_size = 48
            avatar_path = images.get("avatar")
            avatar = None
            if avatar_path:
                try:
                    avatar = self._cover_fit(self._open_image(avatar_path), avatar_size, avatar_size)
                except Exception:
                    avatar = None
            if avatar is None:
                avatar = Image.new("RGBA", (avatar_size, avatar_size), (*accent_rgb, 255))
                first = (d["name"][:1] or "?").upper()
                first_font = self._font(22, bold=True)
                first_draw = ImageDraw.Draw(avatar)
                first_draw.text(
                    ((avatar_size - self._text_width(first, first_font)) // 2, 10),
                    first,
                    font=first_font,
                    fill=(255, 255, 255, 255),
                )
            card.alpha_composite(avatar.convert("RGBA"), (rx, y))
            card_draw = ImageDraw.Draw(card)
            text_y = y
            for line in author_name_lines:
                self._draw_text(
                    card_draw,
                    (rx + 62, text_y),
                    line,
                    author_name_size,
                    ink,
                    bold=True,
                )
                text_y += author_name_size + 6
            for line in author_desc_lines:
                self._draw_text(
                    card_draw,
                    (rx + 62, text_y),
                    line,
                    author_desc_size,
                    muted,
                )
                text_y += author_desc_size + 5
            y += author_block_h

        if desc_lines:
            card_draw.line((rx, y, rx + right_w, y), fill=(*ink, 35), width=1)
            y += 14
            for line in desc_lines:
                self._draw_text(card_draw, (rx, y), line, 22, muted)
                y += 34
            y += 12

        if d["stats"]:
            stats = d["stats"][:6]
            card_draw.line((rx, y, rx + right_w, y), fill=(*ink, 35), width=1)
            y += 12
            stat_w = max(1, right_w // 2)
            for idx, (label, value) in enumerate(stats):
                col = idx % 2
                row = idx // 2
                sx = rx + col * stat_w
                sy = y + row * 34
                self._draw_text(card_draw, (sx, sy), label.upper(), 14, soft, bold=True)
                value_text = value or "—"
                self._draw_text(card_draw, (sx, sy + 16), value_text, 18, ink, bold=True)
            y += ((len(stats) + 1) // 2) * 34 + 14

        if gallery_count:
            gallery_y = body_y + body_h + 20
            card_draw.line(
                (pad, gallery_y, self.width - pad, gallery_y),
                fill=(*ink, 55),
                width=1,
            )
            self._draw_text(
                card_draw,
                (pad, gallery_y + 14),
                "CONTACT SHEET / 补充画面",
                13,
                muted,
                bold=True,
            )
            cells_y = gallery_y + 40
            for idx, path in enumerate(gallery_paths):
                row, col = divmod(idx, gallery_cols)
                cell_x = pad + col * (gallery_cell_w + gallery_gap)
                cell_y = cells_y + row * (gallery_cell_h + gallery_gap)
                card_draw.rectangle(
                    (
                        cell_x,
                        cell_y,
                        cell_x + gallery_cell_w,
                        cell_y + gallery_cell_h,
                    ),
                    fill=paper_deep,
                    outline=(*soft, 120),
                    width=1,
                )
                try:
                    source = self._open_image(path)
                    if gallery_count <= 2:
                        thumb = self._contain_fit(
                            source,
                            gallery_cell_w - 8,
                            gallery_cell_h - 8,
                            paper_deep,
                        )
                    else:
                        thumb = self._cover_fit(
                            source,
                            gallery_cell_w - 8,
                            gallery_cell_h - 8,
                        )
                    card.alpha_composite(
                        thumb.convert("RGBA"),
                        (cell_x + 4, cell_y + 4),
                    )
                except Exception:
                    pass
                card_draw = ImageDraw.Draw(card)
                self._draw_text(
                    card_draw,
                    (cell_x + 10, cell_y + 9),
                    f"{idx + 2:02d}",
                    12,
                    (255, 255, 255),
                    bold=True,
                )
            if len(d["grid"]) > gallery_count:
                self._draw_text(
                    card_draw,
                    (self.width - pad - 74, gallery_y + 14),
                    f"+{len(d['grid']) - gallery_count}",
                    13,
                    accent,
                    bold=True,
                )

        # 独立页脚：编辑号、原链和可配置署名。
        footer_y = body_y + body_h + gallery_h + 26
        card_draw.line((pad, footer_y, self.width - pad, footer_y), fill=(*ink, 70), width=1)
        wm_font = self._font(16, bold=True)
        wm_text = self.watermark
        wm_width = self._text_width(wm_text, wm_font)
        wm_x = self.width - pad - wm_width
        url_y = footer_y + 16
        for line in editorial_url_lines:
            self._draw_text(card_draw, (pad, url_y), line, editorial_url_size, muted)
            url_y += editorial_url_line_h
        bottom_y = max(footer_y + 45, url_y + 5)
        self._draw_text(card_draw, (wm_x, bottom_y), wm_text, 16, accent, bold=True)
        self._draw_text(
            card_draw, (pad, bottom_y + 3), "A CURATED MEDIA NOTE", 12, soft, bold=True
        )

        mask = Image.new("L", (self.width, card_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, self.width - 1, card_h - 1), radius=8, fill=255
        )
        flattened = Image.new("RGBA", card.size, (*paper, 255))
        flattened.alpha_composite(card)
        card = flattened
        card.putalpha(mask)
        canvas.alpha_composite(card, (0, 0))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, "PNG", optimize=True)
        return out_path

    # ==================== 高级皮肤：信号终端 ====================

    def _render_signal(
        self,
        result: ParseResult,
        images: dict[str, Any],
        out_path: Path,
    ) -> Path:
        """信号终端：网格背景、状态栏和遥测面板，独立于常规卡片布局。"""
        d = self._prep(result, images)
        accent = (72, 255, 157)
        accent_rgb = accent
        cyan = (76, 224, 255)
        bg_top = (5, 31, 20)
        bg_bottom = (2, 11, 8)
        panel = (6, 35, 22)
        ink = (221, 255, 235)
        muted = (127, 204, 160)
        dim = (63, 139, 100)
        warning = (255, 210, 98)
        pad = 24
        inner_w = self.width - pad * 2
        gap = 18
        hero_w = max(260, round(inner_w * 0.58))
        side_w = inner_w - hero_w - gap
        top_y = 96
        telemetry_value_w = max(80, side_w - 32)
        profile_text_w = max(62, side_w - 98)
        telemetry_value_size = 18 if side_w >= 220 else 16
        author_value_size = telemetry_value_size
        author_name_lines = self._wrap(
            d["name"] or "UNKNOWN",
            self._font(author_value_size, bold=True),
            profile_text_w,
        )
        while author_value_size > 13 and len(author_name_lines) > 4:
            author_value_size -= 1
            author_name_lines = self._wrap(
                d["name"] or "UNKNOWN",
                self._font(author_value_size, bold=True),
                profile_text_w,
            )
        author_desc_size = 12
        author_desc_lines = (
            self._wrap(
                d["author_desc"],
                self._font(author_desc_size),
                profile_text_w,
            )
            if d["author_desc"]
            else []
        )
        simple_fields = [
            ("SOURCE", d["platform_text"]),
            ("TYPE", d["content_type"]),
            ("STAMP", d["ts"] or "N/A"),
        ]
        simple_field_lines = [
            (
                label,
                self._wrap(
                    value,
                    self._font(telemetry_value_size, bold=True),
                    telemetry_value_w,
                ),
            )
            for label, value in simple_fields
        ]
        profile_h = max(
            58,
            len(author_name_lines) * (author_value_size + 5)
            + len(author_desc_lines) * (author_desc_size + 4),
        )
        telemetry_fields_h = 22 + profile_h + sum(
            17 + len(lines) * (telemetry_value_size + 5) + 8
            for _, lines in simple_field_lines
        )
        hero_h = max(
            340,
            min(480, round(hero_w * 0.72)),
            57 + telemetry_fields_h + 68,
        )
        headline_text, body_text, has_real_title = self._advanced_copy(d)
        title_size = 34 if has_real_title else 28
        title_font = self._font(title_size, bold=True)
        title_lines = (
            self._fit_lines(
                headline_text,
                title_font,
                inner_w,
                2 if has_real_title else 4,
            )
            if headline_text
            else []
        )
        desc_lines = (
            self._fit_lines(body_text, self._font(21), inner_w, 3)
            if body_text
            else []
        )
        title_line_h = title_size + 12
        secondary_paths = d["grid"][:4]
        secondary_count = len(secondary_paths)
        multi_image_post = not d["is_video_hero"] and len(d["media"]) > 1
        secondary_gap = 12
        if secondary_count == 1:
            secondary_cols = 1
            secondary_cell_w = inner_w
            secondary_cell_h = max(180, min(290, round(inner_w * 0.34)))
        elif secondary_count:
            secondary_cols = 2
            secondary_cell_w = max(1, (inner_w - secondary_gap) // 2)
            secondary_cell_h = max(
                150,
                min(230, round(secondary_cell_w * 0.68)),
            )
        else:
            secondary_cols = 0
            secondary_cell_w = 0
            secondary_cell_h = 0
        secondary_rows = (
            math.ceil(secondary_count / secondary_cols) if secondary_cols else 0
        )
        secondary_gallery_h = (
            secondary_rows * secondary_cell_h
            + max(0, secondary_rows - 1) * secondary_gap
        )
        signal_url_lines, signal_url_size, signal_url_line_h = self._url_layout(
            result,
            inner_w,
            initial_size=14 if self.width >= 680 else 13,
            min_size=11,
            target_lines=5,
        )
        signal_footer_h = 78 + len(signal_url_lines) * signal_url_line_h
        lower_h = 34 + len(title_lines) * title_line_h + (len(desc_lines) * 31 + 18 if desc_lines else 0)
        if d["stats"]:
            lower_h += 62
        if secondary_count:
            lower_h += secondary_gallery_h + 26
        card_h = max(
            560,
            top_y + hero_h + lower_h + signal_footer_h + 36,
        )
        total_h = card_h + 16

        canvas = Image.new("RGBA", (self.width, total_h), (0, 0, 0, 0))
        shadow = Image.new("RGBA", (self.width, total_h), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rectangle(
            (10, 10, self.width - 10, card_h + 2), fill=(0, 0, 0, 125)
        )
        canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(13)))
        card = self._gradient(
            (self.width, card_h),
            bg_top,
            bg_bottom,
        ).convert("RGBA")

        # 荧光网格直接铺在渐变底上，半透明面板会保留其穿透感。
        grid_layer = Image.new("RGBA", (self.width, card_h), (0, 0, 0, 0))
        grid_draw = ImageDraw.Draw(grid_layer)
        for xx in range(0, self.width, 32):
            major = xx % 128 == 0
            grid_draw.line(
                (xx, 0, xx, card_h),
                fill=(72, 255, 157, 54 if major else 25),
                width=1,
            )
        for yy in range(0, card_h, 32):
            major = yy % 128 == 0
            grid_draw.line(
                (0, yy, self.width, yy),
                fill=(72, 255, 157, 54 if major else 25),
                width=1,
            )
        for yy in range(8, card_h, 10):
            grid_draw.line(
                (0, yy, self.width, yy),
                fill=(210, 255, 226, 7),
                width=1,
            )
        sweep = Image.new("RGBA", (self.width, card_h), (0, 0, 0, 0))
        sweep_draw = ImageDraw.Draw(sweep)
        sweep_draw.polygon(
            (
                (round(self.width * 0.66), 0),
                (round(self.width * 0.82), 0),
                (round(self.width * 0.46), card_h),
                (round(self.width * 0.32), card_h),
            ),
            fill=(*cyan, 12),
        )
        grid_layer.alpha_composite(sweep)
        card.alpha_composite(grid_layer)
        draw = ImageDraw.Draw(card)
        draw.rectangle(
            (0, 0, self.width - 1, card_h - 1),
            outline=(*accent_rgb, 190),
            width=2,
        )

        # 顶部状态栏。
        header_layer = Image.new("RGBA", card.size, (0, 0, 0, 0))
        header_draw = ImageDraw.Draw(header_layer)
        header_draw.rectangle((0, 0, self.width, 66), fill=(*panel, 188))
        header_draw.line(
            (0, 65, self.width, 65),
            fill=(*accent_rgb, 150),
            width=1,
        )
        card.alpha_composite(header_layer)
        draw = ImageDraw.Draw(card)
        for idx, height in enumerate((12, 22, 34, 18)):
            x = 24 + idx * 13
            draw.rectangle(
                (x, 45 - height, x + 5, 45),
                fill=(*accent_rgb, 125 + idx * 28),
            )
        self._draw_text(draw, (92, 14), "SIGNAL TERMINAL", 18, ink, bold=True)
        self._draw_text(draw, (92, 39), "LIVE MEDIA CHANNEL", 12, cyan, bold=True)
        status = "ONLINE  01"
        status_font = self._font(15, bold=True)
        self._draw_text(
            draw, (self.width - pad - self._text_width(status, status_font), 24),
            status, 15, accent, bold=True,
        )

        # 媒体视窗。
        hero = d["hero"]
        hero_box = (pad, top_y, pad + hero_w, top_y + hero_h)
        try:
            if hero:
                source_image = self._open_image(hero)
                if multi_image_post:
                    hero_img = self._contain_fit(
                        source_image,
                        hero_w,
                        hero_h,
                        panel,
                    ).convert("RGBA")
                else:
                    hero_img = self._cover_fit(
                        source_image,
                        hero_w,
                        hero_h,
                    ).convert("RGBA")
            else:
                hero_img = self._fallback_cover(
                    hero_w, hero_h, _THEMES["dark"], accent_rgb, 0
                )
            hero_img.alpha_composite(
                Image.new("RGBA", hero_img.size, (*accent_rgb, 22))
            )
            card.alpha_composite(hero_img, (pad, top_y))
        except Exception:
            card.alpha_composite(
                self._fallback_cover(hero_w, hero_h, _THEMES["dark"], accent_rgb, 0),
                (pad, top_y),
            )
        draw = ImageDraw.Draw(card)
        draw.rectangle(hero_box, outline=(*accent_rgb, 220), width=2)
        scan_layer = Image.new("RGBA", (hero_w, hero_h), (0, 0, 0, 0))
        scan_draw = ImageDraw.Draw(scan_layer)
        for yy in range(8, hero_h, 9):
            scan_draw.line((2, yy, hero_w - 2, yy), fill=(255, 255, 255, 10), width=1)
        card.alpha_composite(scan_layer, (pad, top_y))
        draw = ImageDraw.Draw(card)
        self._draw_text(draw, (pad + 14, top_y + 14), "MEDIA WINDOW 01", 13, (255, 255, 255, 230), bold=True)
        draw.line(
            (pad + 14, top_y + 36, pad + 116, top_y + 36),
            fill=(*cyan, 220),
            width=2,
        )
        if d["is_video_hero"] and self.show_play_button:
            cx, cy = pad + hero_w // 2, top_y + hero_h // 2
            draw.ellipse((cx - 34, cy - 34, cx + 34, cy + 34), outline=(255, 255, 255, 220), width=2)
            draw.polygon(((cx - 8, cy - 14), (cx - 8, cy + 14), (cx + 15, cy)), fill=(255, 255, 255, 235))

        # 右侧遥测字段。
        sx = pad + hero_w + gap
        sy = top_y
        telemetry_layer = Image.new("RGBA", card.size, (0, 0, 0, 0))
        telemetry_draw = ImageDraw.Draw(telemetry_layer)
        telemetry_draw.rounded_rectangle(
            (sx, sy, sx + side_w, sy + hero_h),
            radius=4,
            fill=(*panel, 178),
            outline=(*accent_rgb, 105),
            width=1,
        )
        card.alpha_composite(telemetry_layer)
        draw = ImageDraw.Draw(card)
        self._draw_text(draw, (sx + 16, sy + 14), "LIVE PROFILE", 14, cyan, bold=True)
        draw.line((sx + 16, sy + 39, sx + side_w - 16, sy + 39), fill=(*dim, 150), width=1)
        avatar_size = 54
        avatar_y = sy + 54
        self._avatar_block(
            card,
            draw,
            sx + 16,
            avatar_y,
            avatar_size,
            images,
            d,
            cyan,
        )
        draw = ImageDraw.Draw(card)
        name_x = sx + 82
        name_y = avatar_y
        for line in author_name_lines:
            self._draw_text(
                draw,
                (name_x, name_y),
                line,
                author_value_size,
                ink,
                bold=True,
            )
            name_y += author_value_size + 5
        for line in author_desc_lines:
            self._draw_text(draw, (name_x, name_y), line, author_desc_size, cyan)
            name_y += author_desc_size + 4
        fy = avatar_y + profile_h + 16
        for label, lines in simple_field_lines:
            self._draw_text(draw, (sx + 16, fy), label, 12, dim, bold=True)
            fy += 17
            for line in lines:
                self._draw_text(
                    draw,
                    (sx + 16, fy),
                    line,
                    telemetry_value_size,
                    ink,
                    bold=True,
                )
                fy += telemetry_value_size + 5
            fy += 8
        draw.line(
            (sx + 16, sy + hero_h - 56, sx + side_w - 16, sy + hero_h - 56),
            fill=_mix(panel, dim, 0.7),
            width=1,
        )
        pulse_y = sy + hero_h - 34
        draw.line(
            (sx + 16, pulse_y, sx + side_w - 16, pulse_y),
            fill=_mix(panel, cyan, 0.42),
            width=1,
        )
        points = []
        for idx in range(12):
            px = sx + 18 + idx * max(1, (side_w - 40) // 11)
            py = pulse_y - (6 if idx % 3 == 0 else (14 if idx % 4 == 0 else 2))
            points.append((px, py))
        if len(points) > 1:
            glow_line = Image.new("RGBA", card.size, (0, 0, 0, 0))
            glow_draw = ImageDraw.Draw(glow_line)
            glow_draw.line(points, fill=(*cyan, 190), width=5)
            card.alpha_composite(glow_line.filter(ImageFilter.GaussianBlur(5)))
            draw = ImageDraw.Draw(card)
            draw.line(points, fill=(*accent_rgb, 245), width=2)

        # 标题、正文和数据行。
        lower_top = top_y + hero_h + 16
        lower_layer = Image.new("RGBA", card.size, (0, 0, 0, 0))
        lower_draw = ImageDraw.Draw(lower_layer)
        lower_draw.rounded_rectangle(
            (
                pad - 8,
                lower_top,
                self.width - pad + 8,
                card_h - signal_footer_h - 10,
            ),
            radius=5,
            fill=(*panel, 132),
            outline=(*accent_rgb, 72),
            width=1,
        )
        card.alpha_composite(lower_layer)
        draw = ImageDraw.Draw(card)
        y = top_y + hero_h + 28
        self._draw_text(draw, (pad, y), "CONTENT SIGNAL", 13, cyan, bold=True)
        y += 23
        for line in title_lines:
            self._draw_text(draw, (pad, y), line, title_size, ink, bold=True)
            y += title_line_h
        if desc_lines:
            y += 3
            for line in desc_lines:
                self._draw_text(draw, (pad, y), line, 21, muted)
                y += 31
            y += 8
        if d["stats"]:
            draw.line((pad, y, self.width - pad, y), fill=(*dim, 150), width=1)
            y += 12
            stats_text = "   ".join(
                f"[{label}] {value or '—'}" for label, value in d["stats"][:6]
            )
            stats_font = self._font(15, bold=True)
            stats_lines = self._fit_lines(stats_text, stats_font, inner_w, 2)
            for line in stats_lines:
                self._draw_text(draw, (pad, y), line, 15, ink, bold=True)
                y += 24
            y += 5
        if secondary_count:
            media_y = y + 4
            for idx, path in enumerate(secondary_paths):
                row, col = divmod(idx, secondary_cols)
                x = pad + col * (secondary_cell_w + secondary_gap)
                yy = media_y + row * (secondary_cell_h + secondary_gap)
                try:
                    secondary = self._contain_fit(
                        self._open_image(path),
                        secondary_cell_w,
                        secondary_cell_h,
                        panel,
                    ).convert("RGBA")
                    secondary.alpha_composite(
                        Image.new("RGBA", secondary.size, (*cyan, 10))
                    )
                    card.alpha_composite(secondary, (x, yy))
                except Exception:
                    draw.rectangle(
                        (
                            x,
                            yy,
                            x + secondary_cell_w,
                            yy + secondary_cell_h,
                        ),
                        fill=(*panel, 230),
                    )
                draw = ImageDraw.Draw(card)
                draw.rectangle(
                    (
                        x,
                        yy,
                        x + secondary_cell_w - 1,
                        yy + secondary_cell_h - 1,
                    ),
                    outline=(*cyan, 190),
                    width=2,
                )
                window_label = f"MEDIA WINDOW {idx + 2:02d}"
                self._draw_text(
                    draw,
                    (x + 12, yy + 10),
                    window_label,
                    11,
                    (255, 255, 255, 230),
                    bold=True,
                )
                draw.line(
                    (x + 12, yy + 30, x + min(112, secondary_cell_w - 12), yy + 30),
                    fill=(*accent_rgb, 220),
                    width=2,
                )
                if idx == secondary_count - 1 and len(d["grid"]) > secondary_count:
                    over_text = f"+{len(d['grid']) - secondary_count}"
                    over_font = self._font(28, bold=True)
                    over_w = self._text_width(over_text, over_font)
                    self._draw_text(
                        draw,
                        (
                            x + (secondary_cell_w - over_w) // 2,
                            yy + secondary_cell_h // 2 - 18,
                        ),
                        over_text,
                        28,
                        ink,
                        bold=True,
                    )
            y = media_y + secondary_gallery_h + 10

        # 独立终端页脚。
        footer_y = card_h - signal_footer_h
        draw.line((pad, footer_y, self.width - pad, footer_y), fill=(*accent_rgb, 150), width=1)
        wm_font = self._font(15, bold=True)
        wm_text = self.watermark
        wm_width = self._text_width(wm_text, wm_font)
        self._draw_text(draw, (pad, footer_y + 13), "SOURCE LINK", 11, cyan, bold=True)
        url_y = footer_y + 32
        for line in signal_url_lines:
            self._draw_text(draw, (pad, url_y), line, signal_url_size, muted)
            url_y += signal_url_line_h
        footer_bottom_y = card_h - 28
        self._draw_text(
            draw,
            (self.width - pad - wm_width, footer_bottom_y),
            wm_text,
            15,
            accent,
            bold=True,
        )
        footer_status = "MEDIA READY  ·  CRC OK"
        self._draw_text(draw, (pad, footer_bottom_y + 3), footer_status, 11, dim, bold=True)
        if d["warnings"]:
            warning_text = self._fit_lines(strip_emoji(d["warnings"][0]), self._font(13), inner_w, 1)[0]
            self._draw_text(draw, (pad, footer_y - 22), f"NOTICE  {warning_text}", 13, warning, bold=True)

        canvas.alpha_composite(card, (0, 0))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, "PNG", optimize=True)
        return out_path

    # ==================== 高级皮肤：海报档案 ====================

    def _render_poster(
        self,
        result: ParseResult,
        images: dict[str, Any],
        out_path: Path,
    ) -> Path:
        """海报档案：整幅媒体背景、强标题层级和色块信息带。"""
        d = self._prep(result, images)
        accent = PLATFORM_COLORS.get(result.platform.name, PLATFORM_COLORS["default"])
        accent_rgb = _hex_to_rgb(accent)
        ink = (8, 12, 18)
        white = (250, 252, 251)
        muted = (216, 225, 226)
        pad = 34
        inner_w = self.width - pad * 2
        title_size = 52 if self.width >= 760 else 44
        title_font = self._font(title_size, bold=True)
        title_lines = self._fit_lines(d["title"] or "未命名内容", title_font, inner_w, 3)
        desc_lines = self._fit_lines(d["text"], self._font(21), inner_w, 3) if d["text"] else []
        author_name_size = 22
        author_name_lines = (
            self._wrap(
                d["name"],
                self._font(author_name_size, bold=True),
                inner_w - 56,
            )
            if d["author"]
            else []
        )
        while author_name_size > 16 and len(author_name_lines) > 3:
            author_name_size -= 1
            author_name_lines = self._wrap(
                d["name"],
                self._font(author_name_size, bold=True),
                inner_w - 56,
            )
        author_desc_size = 14
        author_desc_lines = (
            self._wrap(
                d["author_desc"],
                self._font(author_desc_size),
                inner_w - 56,
            )
            if d["author_desc"]
            else []
        )
        poster_url_lines, poster_url_size, poster_url_line_h = self._url_layout(
            result,
            inner_w,
            initial_size=14 if self.width >= 680 else 13,
            min_size=11,
            target_lines=5,
        )
        bottom_band_h = 106 + len(poster_url_lines) * poster_url_line_h
        title_h = len(title_lines) * (title_size + 10)
        desc_h = len(desc_lines) * 31
        author_text_h = (
            len(author_name_lines) * (author_name_size + 6)
            + len(author_desc_lines) * (author_desc_size + 5)
        )
        author_h = max(50, author_text_h + 4) if d["author"] else 0
        content_stack_h = (
            title_h
            + 24
            + author_h
            + (18 if desc_lines else 0)
            + desc_h
        )
        card_h = max(760, 190 + content_stack_h + bottom_band_h + 40)
        total_h = card_h + 18

        canvas = Image.new("RGBA", (self.width, total_h), (0, 0, 0, 0))
        shadow = Image.new("RGBA", (self.width, total_h), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (9, 9, self.width - 9, card_h + 3), radius=18, fill=(0, 0, 0, 110)
        )
        canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(15)))
        card = Image.new("RGBA", (self.width, card_h), (*ink, 255))

        hero = d["hero"]
        try:
            if hero:
                bg_img = self._cover_fit(self._open_image(hero), self.width, card_h).convert("RGBA")
            else:
                bg_img = self._fallback_cover(
                    self.width, card_h, _THEMES["dark"], accent_rgb, 0
                )
            card.alpha_composite(bg_img, (0, 0))
        except Exception:
            card.alpha_composite(
                self._fallback_cover(self.width, card_h, _THEMES["dark"], accent_rgb, 0),
                (0, 0),
            )

        # 海报的层次由遮罩、几何色块和排版共同完成。
        card.alpha_composite(Image.new("RGBA", (self.width, card_h), (ink[0], ink[1], ink[2], 76)))
        card.alpha_composite(
            self._scrim(self.width, card_h, start=0.26, max_alpha=215, power=1.35),
            (0, 0),
        )
        poster_overlay = Image.new("RGBA", (self.width, card_h), (0, 0, 0, 0))
        poster_draw = ImageDraw.Draw(poster_overlay)
        poster_draw.polygon(
            ((0, 0), (round(self.width * 0.38), 0), (round(self.width * 0.22), card_h), (0, card_h)),
            fill=(*accent_rgb, 34),
        )
        poster_draw.text(
            (self.width - 178, 96),
            "01",
            font=self._font(156, bold=True),
            fill=(255, 255, 255, 24),
            stroke_width=self._bold_stroke(True),
            stroke_fill=(255, 255, 255, 24),
        )
        card.alpha_composite(poster_overlay)
        draw = ImageDraw.Draw(card)
        draw.rectangle((0, 0, self.width, 10), fill=(*accent_rgb, 240))
        draw.rectangle((pad, 78, pad + 88, 84), fill=(*accent_rgb, 255))

        # 顶部档案标识。
        self._draw_text(draw, (pad, 28), "MEDIA ARCHIVE / 01", 16, white, bold=True)
        top_meta = f"{d['platform_text']}  ·  {d['content_type']}"
        top_font = self._font(16, bold=True)
        top_w = self._text_width(top_meta, top_font)
        self._draw_text(draw, (self.width - pad - top_w, 28), top_meta, 16, white, bold=True)
        if d["ts"]:
            self._draw_text(draw, (pad, 98), d["ts"], 14, muted, bold=True)

        band_y = card_h - bottom_band_h
        content_bottom = band_y - 28
        desc_y = content_bottom - desc_h
        author_y = desc_y - (18 if desc_lines else 0) - author_h
        title_y = max(180, author_y - 24 - title_h)
        # 轻微阴影保证标题在明亮封面上仍清晰。
        for idx, line in enumerate(title_lines):
            ly = title_y + idx * (title_size + 10)
            self._draw_text(draw, (pad + 2, ly + 3), line, title_size, (0, 0, 0), bold=True)
            self._draw_text(draw, (pad, ly), line, title_size, white, bold=True)

        if d["author"]:
            avatar_size = 42
            avatar_path = images.get("avatar")
            avatar = None
            if avatar_path:
                try:
                    avatar = self._circle_avatar(self._open_image(avatar_path), avatar_size)
                except Exception:
                    avatar = None
            if avatar is not None:
                card.alpha_composite(avatar, (pad, author_y))
            else:
                draw.ellipse((pad, author_y, pad + avatar_size, author_y + avatar_size), fill=accent_rgb)
                first = (d["name"][:1] or "?").upper()
                self._draw_text(draw, (pad + 12, author_y + 8), first, 22, white, bold=True)
            draw = ImageDraw.Draw(card)
            author_text_y = author_y + 2
            for line in author_name_lines:
                self._draw_text(
                    draw,
                    (pad + 56, author_text_y),
                    line,
                    author_name_size,
                    white,
                    bold=True,
                )
                author_text_y += author_name_size + 6
            for line in author_desc_lines:
                self._draw_text(
                    draw,
                    (pad + 56, author_text_y),
                    line,
                    author_desc_size,
                    muted,
                )
                author_text_y += author_desc_size + 5

        if desc_lines:
            for line in desc_lines:
                self._draw_text(draw, (pad, desc_y), line, 21, muted)
                desc_y += 31

        if d["is_video_hero"] and self.show_play_button:
            cx = self.width // 2
            cy = max(150, min(280, title_y - 76))
            draw.ellipse((cx - 42, cy - 42, cx + 42, cy + 42), outline=white, width=2)
            draw.polygon(((cx - 10, cy - 19), (cx - 10, cy + 19), (cx + 22, cy)), fill=white)

        # 底部色块信息带，独立承担 URL、统计和水印。
        draw.rectangle((0, band_y, self.width, card_h), fill=ink)
        draw.rectangle((0, band_y, 14, card_h), fill=(*accent_rgb, 255))
        self._draw_text(draw, (pad, band_y + 18), "MEDIA / INDEX", 13, (*accent_rgb, 255), bold=True)
        if d["stats"]:
            stats_text = "  ·  ".join(f"{label} {value}" for label, value in d["stats"][:4])
            stats_text = self._fit_lines(stats_text, self._font(16), inner_w, 1)[0]
            self._draw_text(draw, (pad, band_y + 42), stats_text, 16, white, bold=True)
        url_y = band_y + 72
        for line in poster_url_lines:
            self._draw_text(draw, (pad, url_y), line, poster_url_size, muted)
            url_y += poster_url_line_h
        wm_font = self._font(17, bold=True)
        wm_w = self._text_width(self.watermark, wm_font)
        self._draw_text(
            draw,
            (self.width - pad - wm_w, card_h - 32),
            self.watermark,
            17,
            white,
            bold=True,
        )

        if d["grid"]:
            thumb_size = 48
            start_x = self.width - pad - wm_w - 18 - thumb_size * min(3, len(d["grid"])) - 8 * (min(3, len(d["grid"])) - 1)
            for idx, path in enumerate(d["grid"][:3]):
                tx = start_x + idx * (thumb_size + 8)
                try:
                    thumb = self._cover_fit(self._open_image(path), thumb_size, thumb_size).convert("RGBA")
                    card.alpha_composite(thumb, (tx, band_y + 18))
                except Exception:
                    draw.rectangle((tx, band_y + 18, tx + thumb_size, band_y + 18 + thumb_size), outline=muted, width=1)
                draw = ImageDraw.Draw(card)
                draw.rectangle((tx, band_y + 18, tx + thumb_size - 1, band_y + 18 + thumb_size - 1), outline=muted, width=1)

        if d["warnings"]:
            warning_text = self._fit_lines(strip_emoji(d["warnings"][0]), self._font(13), inner_w - 20, 1)[0]
            self._draw_text(draw, (pad, band_y - 24), warning_text, 13, (255, 213, 103), bold=True)

        mask = Image.new("L", (self.width, card_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, self.width - 1, card_h - 1), radius=18, fill=255
        )
        card.putalpha(mask)
        canvas.alpha_composite(card, (0, 0))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, "PNG", optimize=True)
        return out_path

    def _render_poster_archive(
        self,
        result: ParseResult,
        images: dict[str, Any],
        out_path: Path,
    ) -> Path:
        """海报档案：装裱海报、目录轨、裁切标与档案章组成的独立结构。"""
        d = self._prep(result, images)
        accent = PLATFORM_COLORS.get(result.platform.name, PLATFORM_COLORS["default"])
        accent_rgb = _hex_to_rgb(accent)
        board = (20, 21, 24)
        board_deep = (10, 11, 13)
        paper = (244, 239, 227)
        paper_soft = (226, 218, 202)
        ink = (31, 30, 29)
        muted = (111, 105, 96)
        archive_red = (191, 58, 50)
        white = (248, 248, 244)

        pad = 28 if self.width >= 680 else 20
        inner_w = self.width - pad * 2
        rail_w = 42 if self.width >= 680 else 34
        print_gap = 14
        print_x = pad + rail_w + print_gap
        print_w = self.width - print_x - pad
        print_y = 122
        hero_h = max(280, min(500, round(print_w * 0.66)))
        gallery_paths = d["grid"][:4]
        gallery_count = len(gallery_paths)
        gallery_gap = 10
        if gallery_count == 1:
            gallery_cols = 1
            gallery_cell_w = print_w - 24
            gallery_cell_h = min(230, max(170, round(print_w * 0.3)))
        elif gallery_count:
            gallery_cols = 2
            gallery_cell_w = max(1, (print_w - 24 - gallery_gap) // 2)
            gallery_cell_h = min(190, max(140, round(gallery_cell_w * 0.72)))
        else:
            gallery_cols = 0
            gallery_cell_w = 0
            gallery_cell_h = 0
        gallery_rows = (
            math.ceil(gallery_count / gallery_cols) if gallery_cols else 0
        )
        gallery_h = (
            52
            + gallery_rows * gallery_cell_h
            + max(0, gallery_rows - 1) * gallery_gap
            + 14
            if gallery_count
            else 0
        )

        headline_text, body_text, has_real_title = self._advanced_copy(d)
        if has_real_title:
            title_size = 38 if self.width >= 680 else 31
        else:
            title_size = 30 if self.width >= 680 else 26
        title_lines = (
            self._fit_lines(
                headline_text,
                self._font(title_size, bold=True),
                inner_w - 42,
                4 if has_real_title else 6,
            )
            if headline_text
            else []
        )
        desc_size = 19 if self.width >= 680 else 17
        desc_lines = (
            self._fit_lines(
                body_text,
                self._font(desc_size),
                inner_w - 42,
                5,
            )
            if body_text
            else []
        )
        author_name_size = 20 if self.width >= 680 else 17
        author_name_lines = (
            self._wrap(
                d["name"],
                self._font(author_name_size, bold=True),
                inner_w - 118,
            )
            if d["author"]
            else []
        )
        author_desc_lines = (
            self._wrap(
                d["author_desc"],
                self._font(13),
                inner_w - 118,
            )
            if d["author_desc"]
            else []
        )
        author_text_h = (
            len(author_name_lines) * (author_name_size + 5)
            + len(author_desc_lines) * 17
        )
        author_h = max(54, author_text_h) + 16 if d["author"] else 0
        stats = d["stats"][:4]
        stat_rows = (len(stats) + 1) // 2
        stats_h = stat_rows * 48 + (16 if stats else 0)
        content_h = (
            34
            + len(title_lines) * (title_size + 9)
            + 18
            + author_h
            + (len(desc_lines) * (desc_size + 10) + 20 if desc_lines else 0)
            + stats_h
            + 28
        )
        url_lines, url_size, url_line_h = self._url_layout(
            result,
            inner_w - 36,
            initial_size=14,
            min_size=11,
            target_lines=6,
        )
        source_h = 88 + len(url_lines) * url_line_h
        content_y = print_y + hero_h + 28 + gallery_h
        source_y = content_y + content_h + 22
        card_h = source_y + source_h + 20
        total_h = card_h + 18
        archive_id = hashlib.md5(
            f"{result.platform.name}|{result.url}|{result.title}".encode("utf-8")
        ).hexdigest()[:8].upper()

        canvas = Image.new("RGBA", (self.width, total_h), (0, 0, 0, 0))
        shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (10, 10, self.width - 10, card_h + 3),
            radius=8,
            fill=(0, 0, 0, 125),
        )
        canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14)))
        card = self._gradient((self.width, card_h), board, board_deep).convert("RGBA")
        draw = ImageDraw.Draw(card)
        draw.rectangle((0, 0, self.width, 8), fill=(*accent_rgb, 255))

        # 顶部目录栏。
        self._draw_text(draw, (pad, 26), "POSTER ARCHIVE", 23, white, bold=True)
        self._draw_text(draw, (pad, 57), "PUBLIC MEDIA COLLECTION", 12, paper_soft, bold=True)
        catalog = f"CATALOG  {archive_id}"
        catalog_font = self._font(14, bold=True)
        self._draw_text(
            draw,
            (self.width - pad - self._text_width(catalog, catalog_font), 29),
            catalog,
            14,
            paper,
            bold=True,
        )
        meta = f"{d['platform_text']}  ·  {d['content_type']}  ·  {d['ts'] or 'NO DATE'}"
        meta_font = self._font(12)
        meta = self._ellipsize(meta, meta_font, inner_w)
        self._draw_text(draw, (pad, 82), meta, 12, paper_soft)

        # 左侧目录轨与装订孔。
        draw.rectangle(
            (pad, print_y, pad + rail_w, print_y + hero_h),
            fill=(31, 32, 35),
            outline=(*paper_soft, 80),
            width=1,
        )
        for index, ch in enumerate("ARCHIVE"):
            self._draw_text(
                draw,
                (pad + 11, print_y + 22 + index * 25),
                ch,
                13,
                paper_soft,
                bold=True,
            )
        for hole_y in (print_y + hero_h - 78, print_y + hero_h - 48, print_y + hero_h - 18):
            draw.ellipse(
                (pad + rail_w // 2 - 4, hole_y - 4, pad + rail_w // 2 + 4, hole_y + 4),
                fill=board_deep,
                outline=(*paper_soft, 110),
                width=1,
            )

        # 海报装裱纸、裁切标和媒体图。
        frame_box = (print_x, print_y, print_x + print_w, print_y + hero_h)
        frame_shadow = Image.new("RGBA", card.size, (0, 0, 0, 0))
        ImageDraw.Draw(frame_shadow).rectangle(
            (print_x + 7, print_y + 8, print_x + print_w + 7, print_y + hero_h + 8),
            fill=(0, 0, 0, 115),
        )
        card.alpha_composite(frame_shadow.filter(ImageFilter.GaussianBlur(9)))
        draw = ImageDraw.Draw(card)
        draw.rectangle(frame_box, fill=paper)
        image_box = (
            print_x + 12,
            print_y + 12,
            print_x + print_w - 12,
            print_y + hero_h - 12,
        )
        image_w = image_box[2] - image_box[0]
        image_h = image_box[3] - image_box[1]
        try:
            if d["hero"]:
                source_image = self._open_image(d["hero"])
                if gallery_count and not d["is_video_hero"]:
                    hero_img = self._contain_fit(
                        source_image,
                        image_w,
                        image_h,
                        board_deep,
                    )
                else:
                    hero_img = self._cover_fit(source_image, image_w, image_h)
            else:
                hero_img = self._fallback_cover(
                    image_w,
                    image_h,
                    _THEMES["light"],
                    accent_rgb,
                    0,
                )
            card.alpha_composite(hero_img.convert("RGBA"), (image_box[0], image_box[1]))
        except Exception:
            card.alpha_composite(
                self._fallback_cover(
                    image_w,
                    image_h,
                    _THEMES["light"],
                    accent_rgb,
                    0,
                ),
                (image_box[0], image_box[1]),
            )
        draw = ImageDraw.Draw(card)
        mark = 18
        for x, y, dx, dy in (
            (print_x - 8, print_y - 8, mark, 0),
            (print_x - 8, print_y - 8, 0, mark),
            (print_x + print_w + 8, print_y - 8, -mark, 0),
            (print_x + print_w + 8, print_y - 8, 0, mark),
            (print_x - 8, print_y + hero_h + 8, mark, 0),
            (print_x - 8, print_y + hero_h + 8, 0, -mark),
            (print_x + print_w + 8, print_y + hero_h + 8, -mark, 0),
            (print_x + print_w + 8, print_y + hero_h + 8, 0, -mark),
        ):
            draw.line((x, y, x + dx, y + dy), fill=(*paper_soft, 180), width=1)

        if gallery_count:
            sheet_y = print_y + hero_h + 24
            draw.rectangle(
                (print_x, sheet_y, print_x + print_w, sheet_y + gallery_h - 10),
                fill=paper,
                outline=paper_soft,
                width=1,
            )
            self._draw_text(
                draw,
                (print_x + 14, sheet_y + 12),
                "CONTACT SHEET",
                12,
                archive_red,
                bold=True,
            )
            self._draw_text(
                draw,
                (print_x + print_w - 92, sheet_y + 12),
                f"{gallery_count:02d} FILES",
                11,
                muted,
                bold=True,
            )
            cells_y = sheet_y + 38
            for idx, path in enumerate(gallery_paths):
                row, col = divmod(idx, gallery_cols)
                cell_x = print_x + 12 + col * (gallery_cell_w + gallery_gap)
                cell_y = cells_y + row * (gallery_cell_h + gallery_gap)
                draw.rectangle(
                    (
                        cell_x,
                        cell_y,
                        cell_x + gallery_cell_w,
                        cell_y + gallery_cell_h,
                    ),
                    fill=paper_soft,
                    outline=(*muted, 100),
                    width=1,
                )
                try:
                    contact = self._contain_fit(
                        self._open_image(path),
                        gallery_cell_w - 8,
                        gallery_cell_h - 8,
                        paper_soft,
                    )
                    card.alpha_composite(
                        contact.convert("RGBA"),
                        (cell_x + 4, cell_y + 4),
                    )
                except Exception:
                    pass
                draw = ImageDraw.Draw(card)
                self._draw_text(
                    draw,
                    (cell_x + 9, cell_y + 8),
                    f"{idx + 2:02d}",
                    11,
                    archive_red,
                    bold=True,
                )
            if len(d["grid"]) > gallery_count:
                self._draw_text(
                    draw,
                    (print_x + print_w - 62, sheet_y + gallery_h - 28),
                    f"+{len(d['grid']) - gallery_count}",
                    12,
                    archive_red,
                    bold=True,
                )

        stamp_w = min(186, max(130, print_w // 2))
        stamp_x = print_x + print_w - stamp_w - 20
        stamp_y = print_y + 20
        draw.rectangle(
            (stamp_x, stamp_y, stamp_x + stamp_w, stamp_y + 48),
            outline=(*archive_red, 230),
            width=3,
        )
        self._draw_text(
            draw,
            (stamp_x + 12, stamp_y + 11),
            "ARCHIVE COPY",
            17,
            archive_red,
            bold=True,
        )

        # 纸质说明卡：标题、作者头像、正文与索引数据。
        content_box = (pad, content_y, self.width - pad, content_y + content_h)
        draw.rectangle(content_box, fill=paper, outline=paper_soft, width=1)
        draw.rectangle((pad, content_y, pad + 10, content_y + content_h), fill=archive_red)
        y = content_y + 22
        self._draw_text(draw, (pad + 28, y), "CATALOG ENTRY", 12, archive_red, bold=True)
        self._draw_text(
            draw,
            (self.width - pad - 118, y),
            "FILE 01",
            12,
            muted,
            bold=True,
        )
        y += 25
        for line in title_lines:
            self._draw_text(draw, (pad + 28, y), line, title_size, ink, bold=True)
            y += title_size + 9
        y += 8
        if d["author"]:
            avatar_size = 54
            self._avatar_block(
                card,
                draw,
                pad + 28,
                y,
                avatar_size,
                images,
                d,
                accent_rgb,
            )
            draw = ImageDraw.Draw(card)
            author_y = y + 2
            for line in author_name_lines:
                self._draw_text(
                    draw,
                    (pad + 96, author_y),
                    line,
                    author_name_size,
                    ink,
                    bold=True,
                )
                author_y += author_name_size + 5
            for line in author_desc_lines:
                self._draw_text(draw, (pad + 96, author_y), line, 13, muted)
                author_y += 17
            y += author_h
        if desc_lines:
            draw.line((pad + 28, y, self.width - pad - 28, y), fill=paper_soft, width=1)
            y += 14
            for line in desc_lines:
                self._draw_text(draw, (pad + 28, y), line, desc_size, muted)
                y += desc_size + 10
            y += 6
        if stats:
            stat_w = max(1, (inner_w - 64) // 2)
            for idx, (label, value) in enumerate(stats):
                col = idx % 2
                row = idx // 2
                sx = pad + 28 + col * stat_w
                sy = y + row * 48
                self._draw_text(draw, (sx, sy), label, 12, archive_red, bold=True)
                self._draw_text(draw, (sx, sy + 18), value or "—", 17, ink, bold=True)

        # 来源记录带：完整 URL 与署名分层展示。
        draw.rectangle((pad, source_y, self.width - pad, source_y + source_h), fill=board_deep)
        draw.rectangle((pad, source_y, pad + 10, source_y + source_h), fill=(*accent_rgb, 255))
        self._draw_text(draw, (pad + 26, source_y + 18), "SOURCE RECORD", 12, accent, bold=True)
        url_y = source_y + 42
        for line in url_lines:
            self._draw_text(draw, (pad + 26, url_y), line, url_size, paper_soft)
            url_y += url_line_h
        wm_font = self._font(16, bold=True)
        wm_w = self._text_width(self.watermark, wm_font)
        self._draw_text(
            draw,
            (self.width - pad - 22 - wm_w, source_y + source_h - 28),
            self.watermark,
            16,
            white,
            bold=True,
        )
        self._draw_text(
            draw,
            (pad + 26, source_y + source_h - 26),
            f"ARCHIVE ID  {archive_id}",
            11,
            muted,
            bold=True,
        )

        mask = Image.new("L", (self.width, card_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, self.width - 1, card_h - 1),
            radius=8,
            fill=255,
        )
        card.putalpha(mask)
        canvas.alpha_composite(card, (0, 0))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, "PNG", optimize=True)
        return out_path

    def _render_neon(
        self,
        result: ParseResult,
        images: dict[str, Any],
        out_path: Path,
    ) -> Path:
        """霓虹夜景：斜切媒体窗、纵向身份轨与双色光带的独立布局。"""
        d = self._prep(result, images)
        cyan = (55, 236, 255)
        magenta = (255, 63, 188)
        yellow = (255, 225, 92)
        ink = (241, 249, 255)
        muted = (164, 183, 207)
        bg_top = (20, 12, 39)
        bg_bottom = (5, 8, 22)
        panel = (18, 18, 42)
        pad = 24 if self.width >= 680 else 18
        inner_w = self.width - pad * 2
        gap = 16
        rail_w = max(138, round(inner_w * 0.26))
        media_w = inner_w - rail_w - gap
        top_y = 92
        media_h = max(300, min(500, round(media_w * 0.82)))
        avatar_size = min(82, rail_w - 36)

        name_size = 19 if self.width >= 680 else 16
        name_lines = (
            self._wrap(
                d["name"],
                self._font(name_size, bold=True),
                rail_w - 28,
            )
            if d["author"]
            else []
        )
        while name_size > 13 and len(name_lines) > 5:
            name_size -= 1
            name_lines = self._wrap(
                d["name"],
                self._font(name_size, bold=True),
                rail_w - 28,
            )
        author_desc_lines = (
            self._wrap(
                d["author_desc"],
                self._font(12),
                rail_w - 28,
            )
            if d["author_desc"]
            else []
        )
        rail_field_h = 0
        for value in (d["platform_text"], d["content_type"], d["ts"] or "NO DATE"):
            value_lines = self._wrap(value, self._font(14, bold=True), rail_w - 32)
            rail_field_h += 15 + len(value_lines) * 20 + 9
        required_rail_h = (
            34
            + avatar_size
            + 18
            + len(name_lines) * (name_size + 6)
            + len(author_desc_lines) * 17
            + 18
            + rail_field_h
            + 18
        )
        media_h = max(media_h, required_rail_h)
        headline_text, body_text, has_real_title = self._advanced_copy(d)
        if has_real_title:
            title_size = 38 if self.width >= 680 else 31
        else:
            title_size = 31 if self.width >= 680 else 27
        title_lines = (
            self._fit_lines(
                headline_text,
                self._font(title_size, bold=True),
                inner_w,
                4 if has_real_title else 5,
            )
            if headline_text
            else []
        )
        desc_size = 20 if self.width >= 680 else 17
        desc_lines = (
            self._fit_lines(body_text, self._font(desc_size), inner_w, 5)
            if body_text
            else []
        )
        stats = d["stats"][:6]
        stat_cols = 3 if self.width >= 680 else 2
        stat_rows = math.ceil(len(stats) / stat_cols) if stats else 0
        stats_h = stat_rows * 56 + (18 if stats else 0)
        gallery_paths = d["grid"][:4]
        gallery_count = len(gallery_paths)
        gallery_gap = 12
        if gallery_count == 1:
            gallery_cols = 1
            gallery_cell_w = inner_w
            gallery_cell_h = max(190, min(280, round(inner_w * 0.32)))
        elif gallery_count:
            gallery_cols = 2
            gallery_cell_w = max(1, (inner_w - gallery_gap) // 2)
            gallery_cell_h = max(
                150,
                min(220, round(gallery_cell_w * 0.66)),
            )
        else:
            gallery_cols = 0
            gallery_cell_w = 0
            gallery_cell_h = 0
        gallery_rows = (
            math.ceil(gallery_count / gallery_cols) if gallery_cols else 0
        )
        gallery_visual_h = (
            gallery_rows * gallery_cell_h
            + max(0, gallery_rows - 1) * gallery_gap
        )
        gallery_section_h = gallery_visual_h + 48 if gallery_count else 0
        url_lines, url_size, url_line_h = self._url_layout(
            result,
            inner_w,
            initial_size=14,
            min_size=11,
            target_lines=6,
        )
        lower_h = (
            42
            + len(title_lines) * (title_size + 10)
            + (20 + len(desc_lines) * (desc_size + 11) if desc_lines else 0)
            + stats_h
        )
        footer_h = 78 + len(url_lines) * url_line_h
        card_h = top_y + media_h + 20 + gallery_section_h + lower_h + footer_h
        total_h = card_h + 18

        canvas = Image.new("RGBA", (self.width, total_h), (0, 0, 0, 0))
        shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (9, 9, self.width - 9, card_h + 3),
            radius=6,
            fill=(0, 0, 0, 145),
        )
        canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(15)))
        card = self._gradient((self.width, card_h), bg_top, bg_bottom).convert("RGBA")

        # 霓虹城市式透视线，不复用终端网格。
        perspective = Image.new("RGBA", card.size, (0, 0, 0, 0))
        pdraw = ImageDraw.Draw(perspective)
        horizon = round(card_h * 0.58)
        for x in range(-self.width, self.width * 2, 72):
            pdraw.line(
                (self.width // 2, horizon, x, card_h),
                fill=(*cyan, 22),
                width=1,
            )
        for index, y in enumerate(range(horizon, card_h, 34)):
            alpha = min(70, 16 + index * 5)
            pdraw.line((0, y, self.width, y), fill=(*magenta, alpha), width=1)
        card.alpha_composite(perspective)
        draw = ImageDraw.Draw(card)
        draw.rectangle((0, 0, self.width, 7), fill=(*magenta, 255))
        draw.rectangle((round(self.width * 0.46), 0, self.width, 7), fill=(*cyan, 255))

        glow = Image.new("RGBA", card.size, (0, 0, 0, 0))
        gdraw = ImageDraw.Draw(glow)
        gdraw.line((pad, 70, self.width - pad, 70), fill=(*cyan, 150), width=5)
        gdraw.line((pad, card_h - 16, self.width - pad, card_h - 16), fill=(*magenta, 150), width=5)
        card.alpha_composite(glow.filter(ImageFilter.GaussianBlur(8)))
        draw = ImageDraw.Draw(card)
        self._draw_text(draw, (pad, 22), "NEON MEDIA", 22, ink, bold=True)
        self._draw_text(draw, (pad, 51), "NIGHT CHANNEL", 11, cyan, bold=True)
        header_meta = f"{d['platform_text']}  ·  {d['content_type']}"
        header_font = self._font(13, bold=True)
        self._draw_text(
            draw,
            (self.width - pad - self._text_width(header_meta, header_font), 31),
            header_meta,
            13,
            yellow,
            bold=True,
        )

        # 左侧身份轨。
        rail_box = (pad, top_y, pad + rail_w, top_y + media_h)
        rail_layer = Image.new("RGBA", card.size, (0, 0, 0, 0))
        rail_draw = ImageDraw.Draw(rail_layer)
        rail_draw.polygon(
            (
                (rail_box[0], rail_box[1] + 24),
                (rail_box[0] + 24, rail_box[1]),
                (rail_box[2], rail_box[1]),
                (rail_box[2], rail_box[3]),
                (rail_box[0], rail_box[3]),
            ),
            fill=(*panel, 222),
            outline=(*magenta, 165),
        )
        rail_draw.rectangle(
            (rail_box[0], rail_box[1] + 72, rail_box[0] + 5, rail_box[3] - 18),
            fill=(*magenta, 255),
        )
        card.alpha_composite(rail_layer)
        draw = ImageDraw.Draw(card)
        avatar_x = pad + (rail_w - avatar_size) // 2
        avatar_y = top_y + 34
        self._avatar_block(card, draw, avatar_x, avatar_y, avatar_size, images, d, cyan)
        draw = ImageDraw.Draw(card)
        y = avatar_y + avatar_size + 18
        for line in name_lines:
            line_w = self._text_width(line, self._font(name_size, bold=True))
            self._draw_text(
                draw,
                (pad + (rail_w - line_w) // 2, y),
                line,
                name_size,
                ink,
                bold=True,
            )
            y += name_size + 6
        for line in author_desc_lines:
            line_w = self._text_width(line, self._font(12))
            self._draw_text(
                draw,
                (pad + (rail_w - line_w) // 2, y),
                line,
                12,
                cyan,
            )
            y += 17
        y += 18
        for label, value, color in (
            ("PLATFORM", d["platform_text"], cyan),
            ("TYPE", d["content_type"], magenta),
            ("TIME", d["ts"] or "NO DATE", yellow),
        ):
            self._draw_text(draw, (pad + 16, y), label, 10, muted, bold=True)
            y += 15
            for line in self._wrap(value, self._font(14, bold=True), rail_w - 32):
                self._draw_text(draw, (pad + 16, y), line, 14, color, bold=True)
                y += 20
            y += 9

        # 右侧斜切媒体窗。
        media_x = pad + rail_w + gap
        hero = d["hero"]
        try:
            if hero:
                source_image = self._open_image(hero)
                if gallery_count and not d["is_video_hero"]:
                    hero_img = self._contain_fit(
                        source_image,
                        media_w,
                        media_h,
                        panel,
                    ).convert("RGBA")
                else:
                    hero_img = self._cover_fit(
                        source_image,
                        media_w,
                        media_h,
                    ).convert("RGBA")
            else:
                hero_img = self._fallback_cover(
                    media_w,
                    media_h,
                    _THEMES["dark"],
                    cyan,
                    0,
                )
            hero_mask = Image.new("L", (media_w, media_h), 0)
            ImageDraw.Draw(hero_mask).polygon(
                ((34, 0), (media_w, 0), (media_w, media_h), (0, media_h), (0, 34)),
                fill=255,
            )
            hero_img.putalpha(hero_mask)
            card.alpha_composite(hero_img, (media_x, top_y))
        except Exception:
            card.alpha_composite(
                self._fallback_cover(media_w, media_h, _THEMES["dark"], cyan, 0),
                (media_x, top_y),
            )
        draw = ImageDraw.Draw(card)
        media_polygon = (
            (media_x + 34, top_y),
            (media_x + media_w, top_y),
            (media_x + media_w, top_y + media_h),
            (media_x, top_y + media_h),
            (media_x, top_y + 34),
        )
        glow_frame = Image.new("RGBA", card.size, (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow_frame)
        glow_draw.line(media_polygon + (media_polygon[0],), fill=(*cyan, 170), width=7)
        card.alpha_composite(glow_frame.filter(ImageFilter.GaussianBlur(7)))
        draw = ImageDraw.Draw(card)
        draw.line(media_polygon + (media_polygon[0],), fill=(*cyan, 235), width=2)
        draw.line(
            (media_x + 18, top_y + media_h - 18, media_x + 154, top_y + media_h - 18),
            fill=(*magenta, 255),
            width=5,
        )
        self._draw_text(draw, (media_x + 18, top_y + media_h - 48), "VISUAL 01", 12, ink, bold=True)

        if gallery_count:
            gallery_y = top_y + media_h + 20
            self._draw_text(
                draw,
                (pad, gallery_y),
                "SECONDARY VISUALS",
                11,
                yellow,
                bold=True,
            )
            cells_y = gallery_y + 28
            for idx, path in enumerate(gallery_paths):
                row, col = divmod(idx, gallery_cols)
                cell_x = pad + col * (gallery_cell_w + gallery_gap)
                cell_y = cells_y + row * (gallery_cell_h + gallery_gap)
                try:
                    visual = self._contain_fit(
                        self._open_image(path),
                        gallery_cell_w,
                        gallery_cell_h,
                        panel,
                    ).convert("RGBA")
                    card.alpha_composite(visual, (cell_x, cell_y))
                except Exception:
                    draw.rectangle(
                        (
                            cell_x,
                            cell_y,
                            cell_x + gallery_cell_w,
                            cell_y + gallery_cell_h,
                        ),
                        fill=panel,
                    )
                cell_color = magenta if idx % 2 == 0 else cyan
                frame = Image.new("RGBA", card.size, (0, 0, 0, 0))
                frame_draw = ImageDraw.Draw(frame)
                frame_draw.rectangle(
                    (
                        cell_x,
                        cell_y,
                        cell_x + gallery_cell_w - 1,
                        cell_y + gallery_cell_h - 1,
                    ),
                    outline=(*cell_color, 210),
                    width=5,
                )
                card.alpha_composite(frame.filter(ImageFilter.GaussianBlur(5)))
                draw = ImageDraw.Draw(card)
                draw.rectangle(
                    (
                        cell_x,
                        cell_y,
                        cell_x + gallery_cell_w - 1,
                        cell_y + gallery_cell_h - 1,
                    ),
                    outline=(*cell_color, 235),
                    width=2,
                )
                self._draw_text(
                    draw,
                    (cell_x + 12, cell_y + 10),
                    f"VISUAL {idx + 2:02d}",
                    11,
                    ink,
                    bold=True,
                )
            if len(d["grid"]) > gallery_count:
                self._draw_text(
                    draw,
                    (self.width - pad - 62, gallery_y),
                    f"+{len(d['grid']) - gallery_count}",
                    11,
                    magenta,
                    bold=True,
                )

        # 标题与正文采用横向光带，不复用终端或海报结构。
        lower_y = top_y + media_h + 20 + gallery_section_h
        draw.rectangle((pad, lower_y, self.width - pad, lower_y + 5), fill=(*cyan, 220))
        draw.rectangle((pad, lower_y, pad + inner_w // 3, lower_y + 5), fill=(*magenta, 255))
        y = lower_y + 25
        self._draw_text(draw, (pad, y), "FEATURED SIGNAL", 11, yellow, bold=True)
        y += 24
        for line in title_lines:
            self._draw_text(draw, (pad - 1, y), line, title_size, magenta, bold=True)
            self._draw_text(draw, (pad + 2, y), line, title_size, cyan, bold=True)
            self._draw_text(draw, (pad, y), line, title_size, ink, bold=True)
            y += title_size + 10
        if desc_lines:
            y += 8
            for line in desc_lines:
                self._draw_text(draw, (pad, y), line, desc_size, muted)
                y += desc_size + 11
        if stats:
            y += 12
            cell_gap = 8
            cell_w = max(1, (inner_w - cell_gap * (stat_cols - 1)) // stat_cols)
            for idx, (label, value) in enumerate(stats):
                col = idx % stat_cols
                row = idx // stat_cols
                sx = pad + col * (cell_w + cell_gap)
                sy = y + row * 56
                cell_color = cyan if idx % 2 == 0 else magenta
                draw.rectangle((sx, sy, sx + cell_w, sy + 46), outline=(*cell_color, 130), width=1)
                draw.rectangle((sx, sy, sx + 5, sy + 46), fill=(*cell_color, 230))
                self._draw_text(draw, (sx + 13, sy + 6), label, 10, muted, bold=True)
                self._draw_text(draw, (sx + 13, sy + 22), value or "—", 15, ink, bold=True)
            y += stat_rows * 56

        footer_y = card_h - footer_h
        draw.line((pad, footer_y, self.width - pad, footer_y), fill=(*magenta, 160), width=1)
        self._draw_text(draw, (pad, footer_y + 15), "SOURCE LINK", 11, cyan, bold=True)
        url_y = footer_y + 36
        for line in url_lines:
            self._draw_text(draw, (pad, url_y), line, url_size, muted)
            url_y += url_line_h
        wm_font = self._font(16, bold=True)
        wm_w = self._text_width(self.watermark, wm_font)
        self._draw_text(
            draw,
            (self.width - pad - wm_w, card_h - 31),
            self.watermark,
            16,
            magenta,
            bold=True,
        )
        self._draw_text(draw, (pad, card_h - 29), "NEON CHANNEL ACTIVE", 10, yellow, bold=True)

        mask = Image.new("L", (self.width, card_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, self.width - 1, card_h - 1),
            radius=6,
            fill=255,
        )
        card.putalpha(mask)
        canvas.alpha_composite(card, (0, 0))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, "PNG", optimize=True)
        return out_path

    def _render_bilibili(
        self,
        result: ParseResult,
        images: dict[str, Any],
        out_path: Path,
    ) -> Path:
        """哔哩哔哩动态流：作者、正文、媒体、互动和评论按阅读顺序展开。"""
        d = self._prep(result, images)
        pink = (251, 114, 153)
        cyan = (0, 174, 236)
        paper = (255, 255, 255)
        media_bg = (246, 247, 249)
        ink = (36, 38, 43)
        muted = (117, 120, 128)
        divider = (229, 231, 235)
        pad = 28 if self.width >= 680 else 20
        inner_w = self.width - pad * 2
        gap = 10

        avatar_size = 64 if self.width >= 680 else 56
        name_size = 22 if self.width >= 680 else 19
        author_text_w = max(140, inner_w - avatar_size - 24)
        name_lines = (
            self._fit_lines(
                d["name"],
                self._font(name_size, bold=True),
                author_text_w,
                2,
            )
            if d["author"]
            else []
        )
        author_desc_lines = (
            self._fit_lines(
                d["author_desc"],
                self._font(13),
                author_text_w,
                2,
            )
            if d["author_desc"]
            else []
        )
        author_text_h = (
            len(name_lines) * (name_size + 5)
            + len(author_desc_lines) * 17
            + 24
        )
        header_h = 24 + max(avatar_size, author_text_h) + 20

        headline_text, body_text, has_real_title = self._advanced_copy(d)
        title_size = 30 if self.width >= 680 else 26
        body_size = 23 if self.width >= 680 else 20
        title_lines = (
            self._fit_lines(
                headline_text,
                self._font(title_size, bold=True),
                inner_w,
                3,
            )
            if has_real_title and headline_text
            else []
        )
        dynamic_text = body_text if has_real_title else headline_text
        dynamic_lines = (
            self._fit_lines(
                dynamic_text,
                self._font(body_size),
                inner_w,
                8,
            )
            if dynamic_text
            else []
        )
        copy_h = 10
        if title_lines:
            copy_h += len(title_lines) * (title_size + 10) + 8
        if dynamic_lines:
            copy_h += len(dynamic_lines) * (body_size + 11) + 18

        media = list(d["media"])
        shown_media = media[:9]
        over_count = max(0, len(media) - len(shown_media))
        gallery_boxes: list[tuple[Path, int, int, int, int, bool]] = []
        gallery_h = 0
        if shown_media:
            if d["is_video_hero"]:
                primary_h = max(250, min(460, round(inner_w * 9 / 16)))
                gallery_boxes.append(
                    (shown_media[0], 0, 0, inner_w, primary_h, False)
                )
                extras = shown_media[1:]
                if extras:
                    cols = min(3, len(extras))
                    cell_w = max(1, (inner_w - gap * (cols - 1)) // cols)
                    rows = math.ceil(len(extras) / cols)
                    cell_h = cell_w
                    extra_y = primary_h + gap
                    for idx, path in enumerate(extras):
                        row, col = divmod(idx, cols)
                        gallery_boxes.append(
                            (
                                path,
                                col * (cell_w + gap),
                                extra_y + row * (cell_h + gap),
                                cell_w,
                                cell_h,
                                False,
                            )
                        )
                    gallery_h = extra_y + rows * cell_h + (rows - 1) * gap
                else:
                    gallery_h = primary_h
            elif len(shown_media) == 1:
                single_h = max(300, min(540, round(inner_w * 0.66)))
                if self.cover_full_size:
                    single_h = max(
                        260,
                        min(
                            720,
                            self._hero_aspect_height(
                                shown_media[0],
                                inner_w,
                                single_h,
                            ),
                        ),
                    )
                gallery_boxes.append(
                    (shown_media[0], 0, 0, inner_w, single_h, True)
                )
                gallery_h = single_h
            elif len(shown_media) == 2:
                cell_w = max(1, (inner_w - gap) // 2)
                cell_h = max(260, min(390, round(cell_w * 1.02)))
                for idx, path in enumerate(shown_media):
                    gallery_boxes.append(
                        (path, idx * (cell_w + gap), 0, cell_w, cell_h, True)
                    )
                gallery_h = cell_h
            else:
                cols = 3
                cell_w = max(1, (inner_w - gap * 2) // cols)
                cell_h = cell_w
                rows = math.ceil(len(shown_media) / cols)
                for idx, path in enumerate(shown_media):
                    row, col = divmod(idx, cols)
                    gallery_boxes.append(
                        (
                            path,
                            col * (cell_w + gap),
                            row * (cell_h + gap),
                            cell_w,
                            cell_h,
                            False,
                        )
                    )
                gallery_h = rows * cell_h + (rows - 1) * gap

        stats = d["stats"][:6]
        stat_font = self._font(15)
        stat_rows: list[list[str]] = []
        current_row: list[str] = []
        current_width = 0
        for label, value in stats:
            segment = f"{label} {value or '—'}"
            segment_w = self._text_width(segment, stat_font) + 28
            if current_row and current_width + segment_w > inner_w:
                stat_rows.append(current_row)
                current_row = []
                current_width = 0
            current_row.append(segment)
            current_width += segment_w
        if current_row:
            stat_rows.append(current_row)
        interaction_h = 20 + max(1, len(stat_rows)) * 28

        url_lines, url_size, url_line_h = self._url_layout(
            result,
            inner_w,
            initial_size=14,
            min_size=11,
            target_lines=6,
        )
        source_h = 60 + len(url_lines) * url_line_h
        card_h = (
            header_h
            + copy_h
            + (gallery_h + 22 if gallery_h else 0)
            + interaction_h
            + source_h
        )
        total_h = card_h + 16

        canvas = Image.new("RGBA", (self.width, total_h), (0, 0, 0, 0))
        shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (8, 8, self.width - 8, card_h + 2),
            radius=8,
            fill=(28, 38, 52, 42),
        )
        canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(12)))
        card = Image.new("RGBA", (self.width, card_h), (*paper, 255))
        draw = ImageDraw.Draw(card)
        draw.rectangle((0, 0, self.width, 5), fill=(*pink, 255))

        header_y = 24
        if d["author"]:
            self._avatar_block(
                card,
                draw,
                pad,
                header_y,
                avatar_size,
                images,
                d,
                pink,
            )
            draw = ImageDraw.Draw(card)
            text_x = pad + avatar_size + 16
            text_y = header_y
            for line in name_lines:
                self._draw_text(
                    draw,
                    (text_x, text_y),
                    line,
                    name_size,
                    pink,
                    bold=True,
                )
                text_y += name_size + 5
            for line in author_desc_lines:
                self._draw_text(draw, (text_x, text_y), line, 13, muted)
                text_y += 17
            meta_y = max(text_y + 2, header_y + avatar_size - 20)
        else:
            text_x = pad
            meta_y = header_y + 8

        chip_font = self._font(12, bold=True)
        chip_text = d["content_type"]
        chip_w = self._text_width(chip_text, chip_font) + 22
        draw.rounded_rectangle(
            (text_x, meta_y, text_x + chip_w, meta_y + 24),
            radius=4,
            fill=(*pink, 255),
        )
        self._draw_text(
            draw,
            (text_x + 11, meta_y + 5),
            chip_text,
            12,
            (255, 255, 255),
            bold=True,
        )
        platform_x = text_x + chip_w + 12
        self._draw_text(
            draw,
            (platform_x, meta_y + 5),
            d["platform_text"],
            12,
            cyan,
            bold=True,
        )
        if d["ts"]:
            ts_font = self._font(12)
            ts_w = self._text_width(d["ts"], ts_font)
            self._draw_text(
                draw,
                (self.width - pad - ts_w, meta_y + 5),
                d["ts"],
                12,
                muted,
            )

        y = header_h
        for line in title_lines:
            self._draw_text(draw, (pad, y), line, title_size, ink, bold=True)
            y += title_size + 10
        if title_lines:
            y += 8
        for line in dynamic_lines:
            self._draw_text(draw, (pad, y), line, body_size, ink)
            y += body_size + 11
        if dynamic_lines:
            y += 18

        gallery_y = y
        for index, (path, bx, by, bw, bh, contain) in enumerate(gallery_boxes):
            x = pad + bx
            yy = gallery_y + by
            try:
                source = self._open_image(path)
                if contain:
                    media_img = self._contain_fit(source, bw, bh, media_bg)
                else:
                    media_img = self._cover_fit(source, bw, bh)
                media_img = self._rounded_image(media_img, 6)
                card.alpha_composite(media_img, (x, yy))
            except Exception:
                draw.rounded_rectangle(
                    (x, yy, x + bw, yy + bh),
                    radius=6,
                    fill=media_bg,
                    outline=divider,
                    width=1,
                )
            draw = ImageDraw.Draw(card)
            draw.rounded_rectangle(
                (x, yy, x + bw - 1, yy + bh - 1),
                radius=6,
                outline=divider,
                width=1,
            )
            if index == len(gallery_boxes) - 1 and over_count:
                overlay = Image.new("RGBA", (bw, bh), (0, 0, 0, 118))
                overlay = self._rounded_image(overlay, 6)
                card.alpha_composite(overlay, (x, yy))
                plus_text = f"+{over_count}"
                plus_font = self._font(34, bold=True)
                plus_w = self._text_width(plus_text, plus_font)
                self._draw_text(
                    ImageDraw.Draw(card),
                    (x + (bw - plus_w) // 2, yy + bh // 2 - 20),
                    plus_text,
                    34,
                    (255, 255, 255),
                    bold=True,
                )
        if d["is_video_hero"] and gallery_boxes and self.show_play_button:
            _, bx, by, bw, bh, _ = gallery_boxes[0]
            cx = pad + bx + bw // 2
            cy = gallery_y + by + bh // 2
            draw.ellipse(
                (cx - 34, cy - 34, cx + 34, cy + 34),
                fill=(0, 0, 0, 118),
            )
            draw.polygon(
                ((cx - 8, cy - 15), (cx - 8, cy + 15), (cx + 17, cy)),
                fill=(255, 255, 255),
            )
        if gallery_h:
            y = gallery_y + gallery_h + 22

        draw.line((pad, y, self.width - pad, y), fill=divider, width=1)
        y += 18
        if stat_rows:
            for row_index, row in enumerate(stat_rows):
                x = pad
                for segment_index, segment in enumerate(row):
                    color = pink if row_index == 0 and segment_index == 0 else muted
                    self._draw_text(draw, (x, y), segment, 15, color, bold=color == pink)
                    x += self._text_width(segment, stat_font) + 28
                y += 28
        else:
            self._draw_text(
                draw,
                (pad, y),
                f"来自 {d['platform_text']} 的公开动态",
                14,
                muted,
            )
            y += 28
        y += 2

        source_y = card_h - source_h
        draw.line((pad, source_y, self.width - pad, source_y), fill=divider, width=1)
        self._draw_text(draw, (pad, source_y + 15), "原始链接", 12, pink, bold=True)
        url_y = source_y + 36
        for line in url_lines:
            self._draw_text(draw, (pad, url_y), line, url_size, muted)
            url_y += url_line_h
        wm_font = self._font(15, bold=True)
        wm_w = self._text_width(self.watermark, wm_font)
        self._draw_text(
            draw,
            (self.width - pad - wm_w, card_h - 27),
            self.watermark,
            15,
            pink,
            bold=True,
        )
        draw.ellipse(
            (pad, card_h - 22, pad + 7, card_h - 15),
            fill=(*pink, 255),
        )
        draw.ellipse(
            (pad + 11, card_h - 22, pad + 18, card_h - 15),
            fill=(*cyan, 255),
        )

        mask = Image.new("L", (self.width, card_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, self.width - 1, card_h - 1),
            radius=8,
            fill=255,
        )
        card.putalpha(mask)
        canvas.alpha_composite(card, (0, 0))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, "PNG", optimize=True)
        return out_path

    def _render_standard(
        self,
        result: ParseResult,
        images: dict[str, Any],
        out_path: Path,
    ) -> Path:
        """标准布局：顶部全宽横幅 + 纵向信息流。"""
        theme = _THEMES[self.theme_name]
        accent = PLATFORM_COLORS.get(result.platform.name, PLATFORM_COLORS["default"])
        accent_rgb = _hex_to_rgb(accent)

        pad = _L.PAD
        inner_w = self.width - pad * 2
        gap = _L.GRID_GAP

        # ================= 数据准备 =================
        is_video_hero = images.get("hero") is not None
        hero = images.get("hero")
        grid = list(images.get("grid") or [])
        if hero is None and grid:
            # 没有视频封面时，用图集首图作为顶部横幅
            hero = grid.pop(0)
        hero_h = round(self.width * _L.HERO_RATIO) if hero else 0
        if hero and self.cover_full_size:
            hero_h = self._hero_aspect_height(hero, self.width, hero_h)

        grid_h, cols, rows, cell_h = self._grid_metrics(len(grid), inner_w, gap)

        # 头部文字
        platform_text = result.platform.display_name
        platform_pill_w = self._platform_pill_width(platform_text)

        content_type = result.content_type or "动态"
        chip_font = self._font(_L.F_CHIP, bold=True)
        type_pill_w = self._text_width(content_type, chip_font) + 36

        ts = format_timestamp(result.timestamp)
        ts_font = self._font(_L.F_TIME)

        # 作者
        author = result.author
        avatar_size = _L.AVATAR
        name = strip_emoji(author.name) or "未知作者" if author else ""
        author_desc = strip_emoji(author.description or "")[:40] if author else ""
        name_font = self._font(_L.F_NAME, bold=True)
        name_lh = self._line_height(name_font)
        sign_font = self._font(_L.F_SIGN)
        sign_lh = self._line_height(sign_font)

        # 标题（独立一行，可换行）
        title_font = self._font(_L.F_TITLE, bold=True)
        title = strip_emoji(result.title)
        title_lines: list[str] = []
        if title:
            title_lines = self._fit_lines(
                title, title_font, inner_w, 2 if hero else 3
            )

        # 简介
        desc_font = self._font(_L.F_DESC)
        text = strip_emoji(result.text)
        desc_lines: list[str] = []
        if text:
            desc_lines = self._fit_lines(text, desc_font, inner_w, 6)

        # 统计（时长并入统计徽章）
        stats = parse_stats_line(result.extra.get("stats_line"))
        if dur := result.extra.get("duration"):
            stats.insert(0, ("时长：", fmt_duration(dur)))
        online_text = strip_emoji(result.extra.get("online") or "")
        limit_warnings = result.extra.get("limit_warnings") or []
        warnings_h = self._warning_block_height(limit_warnings, inner_w)
        stat_rows = self._build_stat_rows(stats, inner_w)

        quote_h = self._measure_quote(result.repost, inner_w) if result.repost else 0
        footer_h = self._footer_metrics(result, inner_w)[0]

        # ================= 高度计算 =================
        if hero:
            y = hero_h + 20
        else:
            y = _L.HEAD_BAR_TOP + _L.HEAD_BAR_H + 18 + _L.HEAD_PILL_H + 18
        if author:
            y += avatar_size + 20
        else:
            y += 12
        if title_lines:
            y += len(title_lines) * _L.F_TITLE_LINE_H + 14
        if desc_lines:
            y += len(desc_lines) * _L.F_DESC_LINE_H + 16
        if stat_rows:
            y += len(stat_rows) * (_L.STAT_H + _L.STAT_ROW_GAP) - _L.STAT_ROW_GAP + 18
        if online_text:
            y += 38
        if warnings_h:
            y += warnings_h + 20
        if grid:
            y += grid_h + 20
        if quote_h:
            y += quote_h + 20
        y += footer_h
        card_h = y
        total_h = card_h + 14

        # ================= 绘制底层 =================
        canvas = Image.new("RGBA", (self.width, total_h), (0, 0, 0, 0))

        # 阴影
        shadow = Image.new("RGBA", (self.width, total_h), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (_L.SHADOW_INSET, _L.SHADOW_INSET, self.width - _L.SHADOW_INSET, total_h - 2),
            radius=_L.RADIUS + 2,
            fill=(0, 0, 0, theme.shadow_alpha),
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(_L.SHADOW_BLUR))
        canvas.alpha_composite(shadow)

        # 卡片主体（垂直渐变 + 品牌色渗透光晕 + 圆角）
        grad = self._gradient(
            (self.width, card_h), theme.gradient_top, theme.gradient_bottom
        )
        glow = self._radial_glow(self.width, round(self.width * 0.9), accent_rgb, theme.glow_alpha)
        grad_rgba = grad.convert("RGBA")
        # 光晕与卡片同宽并从 (0,0) 覆盖，避免图层左缘产生内部接缝
        grad_rgba.alpha_composite(glow, (0, 0))
        mask = Image.new("L", (self.width, card_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, self.width - 1, card_h - 1), radius=_L.RADIUS, fill=255
        )
        card = grad_rgba
        card.putalpha(mask)
        canvas.alpha_composite(card, (0, 0))

        draw = ImageDraw.Draw(canvas)
        # 卡片描边同样先画到透明层再混合，保证输出像素不透明
        border_layer = Image.new("RGBA", (self.width, card_h), (0, 0, 0, 0))
        ImageDraw.Draw(border_layer).rounded_rectangle(
            (0, 0, self.width - 1, card_h - 1), radius=_L.RADIUS,
            outline=_with_alpha(theme.border, theme.border_alpha), width=1,
        )
        canvas.alpha_composite(border_layer, (0, 0))

        y = 0
        if hero:
            # ============ 顶部横幅 ============
            hero_img = None
            try:
                hero_img = self._cover_fit(self._open_image(hero), self.width, hero_h)
                hero_img = self._rounded_image_top(hero_img, _L.RADIUS)
            except Exception:
                hero_img = None
                logger.warning("横幅图片渲染失败，使用占位背景", exc_info=True)
            if hero_img is not None:
                canvas.alpha_composite(hero_img, (0, 0))
            else:
                ph = self._fallback_cover(
                    self.width, hero_h, theme, accent_rgb,
                    _L.RADIUS, top_only=True,
                )
                canvas.alpha_composite(ph, (0, 0))

            # 柔和渐变 scrim：顶部保证徽章可读
            canvas.alpha_composite(
                self._scrim(
                    self.width, hero_h,
                    start=_L.TOP_SCRIM_END, max_alpha=_L.TOP_SCRIM_ALPHA,
                    power=1.4, invert=True,
                ),
                (0, 0),
            )

            # 悬浮徽章组：平台徽标 + 类型 chip（毛玻璃）
            badge_y = _L.HERO_BADGE_TOP
            badge_h = _L.HERO_BADGE_H
            self._draw_hero_badge(
                canvas, pad, badge_y, badge_h, platform_pill_w,
                text=platform_text, accent_rgb=accent_rgb, dot=True,
                font_size=_L.F_PLATFORM,
            )
            chip_x = pad + platform_pill_w + _L.HERO_BADGE_GAP
            self._draw_hero_badge(
                canvas, chip_x, badge_y, badge_h, type_pill_w,
                text=content_type, accent_rgb=accent_rgb, dot=False,
                font_size=_L.F_CHIP,
            )
            if ts:
                ts_w = self._text_width(ts, ts_font) + 32
                ts_x = self.width - pad - ts_w
                self._draw_hero_badge(
                    canvas, ts_x, badge_y, badge_h, ts_w,
                    text=ts, accent_rgb=accent_rgb, dot=False,
                    font_size=_L.F_TIME, bold=False,
                )

            # 视频播放按钮（毛玻璃圆环）
            if is_video_hero and self.show_play_button:
                play_r = _L.PLAY_R
                cx, cy = self.width // 2, hero_h // 2
                box = (cx - play_r, cy - play_r, cx + play_r, cy + play_r)
                self._glass(
                    canvas, box, play_r,
                    tint_rgb=(255, 255, 255), tint_alpha=34,
                    border_rgb=(255, 255, 255), border_alpha=110,
                    blur=8,
                )
                pd = ImageDraw.Draw(canvas)
                pd.polygon(
                    [
                        (cx - 12, cy - 18),
                        (cx - 12, cy + 18),
                        (cx + 20, cy),
                    ],
                    fill=(255, 255, 255, 245),
                )

            y = hero_h + 20
        else:
            # ============ 纯文本卡片头部 ============
            # accent 短横条（品牌色渐变淡出）
            bar = Image.new("RGBA", (_L.HEAD_BAR_W, _L.HEAD_BAR_H), (0, 0, 0, 0))
            for xx in range(_L.HEAD_BAR_W):
                a = int(230 * (1 - xx / max(_L.HEAD_BAR_W - 1, 1)) ** 1.3)
                ImageDraw.Draw(bar).line(
                    [(xx, 0), (xx, _L.HEAD_BAR_H)], fill=(*accent_rgb, a)
                )
            bar = self._rounded_image(bar, _L.HEAD_BAR_H // 2)
            canvas.alpha_composite(bar, (pad, _L.HEAD_BAR_TOP))

            y = _L.HEAD_BAR_TOP + _L.HEAD_BAR_H + 18
            # 平台徽标（毛玻璃 + accent 圆点）
            self._draw_flat_badge(
                canvas, theme, pad, y, _L.HEAD_PILL_H, platform_pill_w,
                text=platform_text, accent_rgb=accent_rgb, dot=True,
                font_size=_L.F_PLATFORM, bold=True, text_rgb=theme.text_primary,
            )
            # 类型与时间弱化为辅助文字
            type_x = pad + platform_pill_w + 16
            type_lh = self._line_height(chip_font)
            self._draw_text(
                draw,
                (type_x, y + (_L.HEAD_PILL_H - type_lh) // 2),
                content_type, _L.F_CHIP, theme.text_tertiary, bold=True,
            )
            if ts:
                ts_w = self._text_width(ts, ts_font)
                ts_lh = self._line_height(ts_font)
                self._draw_text(
                    draw,
                    (self.width - pad - ts_w, y + (_L.HEAD_PILL_H - ts_lh) // 2),
                    ts, _L.F_TIME, theme.text_tertiary,
                )
            y += _L.HEAD_PILL_H + 18

        # ============ 作者行（小头像） ============
        if author:
            avatar_path = images.get("avatar")
            avatar = None
            if avatar_path:
                try:
                    avatar = self._circle_avatar(
                        self._open_image(avatar_path), avatar_size
                    )
                except Exception:
                    avatar = None
            if avatar is not None:
                canvas.alpha_composite(avatar, (pad, y))
                ring = Image.new("RGBA", (avatar_size, avatar_size), (0, 0, 0, 0))
                ImageDraw.Draw(ring).ellipse(
                    (1, 1, avatar_size - 2, avatar_size - 2),
                    outline=_with_alpha(accent_rgb, 170), width=_L.AVATAR_RING_W,
                )
                canvas.alpha_composite(ring, (pad, y))
            else:
                # 无头像时绘制 accent 渐变首字母占位圆
                placeholder = self._gradient(
                    (avatar_size, avatar_size),
                    accent_rgb, _mix(accent_rgb, (0, 0, 0), 0.35),
                ).convert("RGBA")
                pmask = Image.new("L", (avatar_size, avatar_size), 0)
                ImageDraw.Draw(pmask).ellipse(
                    (0, 0, avatar_size - 1, avatar_size - 1), fill=255
                )
                placeholder.putalpha(pmask)
                canvas.alpha_composite(placeholder, (pad, y))
                first = name[:1].upper()
                f_font = self._font(_L.F_INITIAL, bold=True)
                fw = self._text_width(first, f_font)
                self._draw_text(
                    draw,
                    (pad + (avatar_size - fw) // 2, y + (avatar_size - self._line_height(f_font)) // 2),
                    first, _L.F_INITIAL, "#FFFFFF", bold=True,
                )
            name_x = pad + avatar_size + 20
            name_block_h = name_lh + (sign_lh + 4 if author_desc else 0)
            name_y = y + (avatar_size - name_block_h) // 2
            self._draw_text(
                draw, (name_x, name_y), name, _L.F_NAME, theme.text_primary, bold=True
            )
            if author_desc:
                self._draw_text(
                    draw, (name_x, name_y + name_lh + 4),
                    author_desc, _L.F_SIGN, theme.text_tertiary,
                )
            y += avatar_size + 20
        else:
            y += 12

        # ============ 标题 ============
        if title_lines:
            for line in title_lines:
                self._draw_text(
                    draw, (pad, y), line, _L.F_TITLE, theme.text_primary, bold=True
                )
                y += _L.F_TITLE_LINE_H
            y += 14

        # ============ 简介 ============
        if desc_lines:
            for line in desc_lines:
                self._draw_text(draw, (pad, y), line, _L.F_DESC, theme.text_secondary)
                y += _L.F_DESC_LINE_H
            y += 16

        # ============ 统计徽章（毛玻璃药丸：标签弱化 + 数值强调） ============
        if stat_rows:
            label_font = self._font(_L.F_STAT_LABEL)
            value_font = self._font(_L.F_STAT_VALUE, bold=True)
            for row in stat_rows:
                x = pad
                for label, value in row:
                    w = self._stat_pill_width(label, value)
                    self._glass(
                        canvas, (x, y, x + w, y + _L.STAT_H), _L.STAT_H // 2,
                        tint_rgb=theme.stat_pill_bg, tint_alpha=theme.frost_alpha,
                        border_rgb=theme.stat_pill_bg,
                        border_alpha=theme.frost_border_alpha,
                        blur=6,
                    )
                    label_w = self._text_width(label, label_font)
                    tx = x + _L.STAT_PAD_X
                    if value:
                        self._draw_text(
                            draw,
                            (tx, y + (_L.STAT_H - self._line_height(label_font)) // 2),
                            label, _L.F_STAT_LABEL, theme.text_tertiary,
                        )
                        self._draw_text(
                            draw,
                            (tx + label_w + _L.STAT_LABEL_VALUE_GAP,
                             y + (_L.STAT_H - self._line_height(value_font)) // 2),
                            value, _L.F_STAT_VALUE, theme.text_primary, bold=True,
                        )
                    else:
                        self._draw_text(
                            draw,
                            (tx, y + (_L.STAT_H - self._line_height(label_font)) // 2),
                            label, _L.F_STAT_LABEL, theme.text_secondary,
                        )
                    x += w + _L.STAT_GAP
                y += _L.STAT_H + _L.STAT_ROW_GAP
            y += 18 - _L.STAT_ROW_GAP

        # ============ 在线人数（accent 圆点 + 文字） ============
        if online_text:
            online_font = self._font(_L.F_ONLINE)
            if self._text_width(online_text, online_font) <= inner_w - 18:
                dot_r = 4
                dot_cy = y + self._line_height(online_font) // 2
                draw.ellipse(
                    (pad, dot_cy - dot_r, pad + dot_r * 2, dot_cy + dot_r),
                    fill=accent,
                )
                self._draw_text(
                    draw, (pad + 18, y), online_text, _L.F_ONLINE, accent
                )
            y += 38

        # ============ 警告提示块 ============
        if limit_warnings:
            y = self._draw_warning_block(canvas, draw, theme, limit_warnings, y, inner_w)
            draw = ImageDraw.Draw(canvas)

        # ============ 图集网格 ============
        if grid:
            try:
                show = cols * rows
                over = len(grid) - show if len(grid) > show else 0
                for idx, path in enumerate(grid[:show]):
                    r, c = divmod(idx, cols)
                    x = pad + c * (cell_h + gap)
                    yy = y + r * (cell_h + gap)
                    try:
                        img = self._cover_fit(self._open_image(path), cell_h, cell_h)
                        img = self._rounded_image(img, _L.GRID_RADIUS)
                        canvas.alpha_composite(img, (x, yy))
                    except Exception:
                        ph_layer = Image.new(
                            "RGBA", (cell_h, cell_h), (0, 0, 0, 0)
                        )
                        ImageDraw.Draw(ph_layer).rounded_rectangle(
                            (0, 0, cell_h - 1, cell_h - 1), radius=_L.GRID_RADIUS,
                            fill=_with_alpha(theme.pill_bg, 14),
                        )
                        canvas.alpha_composite(ph_layer, (x, yy))
                    if idx == show - 1 and over > 0:
                        canvas.alpha_composite(
                            self._scrim(
                                cell_h, cell_h, start=0.0, max_alpha=150, power=1.2
                            ),
                            (x, yy),
                        )
                        plus_font = self._font(_L.F_PLUS, bold=True)
                        plus_text = f"+{over}"
                        pw = self._text_width(plus_text, plus_font)
                        self._draw_text(
                            draw,
                            (x + (cell_h - pw) // 2, yy + (cell_h - self._line_height(plus_font)) // 2),
                            plus_text, _L.F_PLUS, "#FFFFFF", bold=True,
                        )
                y += grid_h + 20
            except Exception:
                logger.warning("图集渲染失败，已跳过", exc_info=True)
                y -= grid_h + 20

        # ============ 转发引用（毛玻璃容器 + accent 竖条） ============
        if result.repost and quote_h:
            qy = y
            self._glass(
                canvas, (pad, qy, pad + inner_w, qy + quote_h), _L.QUOTE_RADIUS,
                tint_rgb=theme.quote_bg, tint_alpha=theme.frost_alpha,
                border_rgb=theme.quote_bg, border_alpha=theme.frost_border_alpha,
                blur=6,
            )
            bar_layer = Image.new(
                "RGBA", (_L.QUOTE_BAR_W + 2, quote_h - 32), (0, 0, 0, 0)
            )
            ImageDraw.Draw(bar_layer).rounded_rectangle(
                (0, 0, _L.QUOTE_BAR_W + 1, quote_h - 33),
                radius=_L.QUOTE_BAR_W // 2,
                fill=_with_alpha(accent_rgb, 230),
            )
            canvas.alpha_composite(bar_layer, (pad + 18, qy + 16))
            self._draw_quote_text(
                draw, result.repost, pad + 18 + _L.QUOTE_BAR_W + 16, qy + 16,
                inner_w - 18 * 2 - _L.QUOTE_BAR_W - 16, theme,
            )
            y += quote_h + 20

        # ============ 页脚（完整链接 + 可配置署名） ============
        self._footer_block(
            canvas,
            draw,
            theme,
            accent,
            accent_rgb,
            result,
            y,
            inner_w,
        )

        # ---------- 保存 ----------
        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, "PNG", optimize=True)
        return out_path

    # ---------- 徽章组件 ----------

    def _draw_hero_badge(
        self,
        canvas: Image.Image,
        x: int,
        y: int,
        h: int,
        w: int,
        *,
        text: str,
        accent_rgb: tuple[int, int, int],
        dot: bool,
        font_size: int,
        bold: bool = True,
    ) -> None:
        """横幅上的毛玻璃徽章（可选 accent 圆点），文字恒为白色。"""
        self._glass(
            canvas, (x, y, x + w, y + h), h // 2,
            tint_rgb=_L.HERO_GLASS_TINT, tint_alpha=_L.HERO_GLASS_TINT_ALPHA,
            border_rgb=(255, 255, 255), border_alpha=_L.HERO_GLASS_BORDER_ALPHA,
        )
        draw = ImageDraw.Draw(canvas)
        font = self._font(font_size, bold)
        tx = x + 18
        if dot:
            dot_r = 6
            dot_cy = y + h // 2
            draw.ellipse(
                (tx, dot_cy - dot_r, tx + dot_r * 2, dot_cy + dot_r),
                fill=(*accent_rgb, 255),
            )
            tx += dot_r * 2 + 8
        self._draw_text(
            draw, (tx, y + (h - self._line_height(font)) // 2),
            text, font_size, "#FFFFFF", bold=bold,
        )

    def _draw_flat_badge(
        self,
        canvas: Image.Image,
        theme: _Theme,
        x: int,
        y: int,
        h: int,
        w: int,
        *,
        text: str,
        accent_rgb: tuple[int, int, int],
        dot: bool,
        font_size: int,
        bold: bool,
        text_rgb: tuple[int, int, int],
    ) -> None:
        """卡片主体上的毛玻璃徽章（主题感知配色）。"""
        self._glass(
            canvas, (x, y, x + w, y + h), h // 2,
            tint_rgb=theme.pill_bg, tint_alpha=theme.frost_alpha,
            border_rgb=theme.pill_bg, border_alpha=theme.frost_border_alpha,
            blur=6,
        )
        draw = ImageDraw.Draw(canvas)
        font = self._font(font_size, bold)
        tx = x + 18
        if dot:
            dot_r = 6
            dot_cy = y + h // 2
            draw.ellipse(
                (tx, dot_cy - dot_r, tx + dot_r * 2, dot_cy + dot_r),
                fill=(*accent_rgb, 255),
            )
            tx += dot_r * 2 + 8
        self._draw_text(
            draw, (tx, y + (h - self._line_height(font)) // 2),
            text, font_size, text_rgb, bold=bold,
        )

    # ---------- 转发引用 ----------

    def _measure_quote(self, repost: ParseResult, inner_w: int) -> int:
        q_font = self._font(_L.F_QUOTE)
        author = strip_emoji(repost.author.name) if repost.author else "原帖"
        text = strip_emoji(repost.title or repost.text or "")
        body = f"@{author}"
        if text:
            body += f"：{text}"
        max_w = inner_w - 18 * 2 - _L.QUOTE_BAR_W - 16
        lines = self._fit_lines(body, q_font, max_w, 4)
        return max(80, len(lines) * _L.F_QUOTE_LINE_H + 32)

    def _draw_quote_text(
        self,
        draw: Any,
        repost: ParseResult,
        x: int,
        y: int,
        max_width: int,
        theme: _Theme,
    ) -> None:
        q_font = self._font(_L.F_QUOTE)
        author = strip_emoji(repost.author.name) if repost.author else "原帖"
        text = strip_emoji(repost.title or repost.text or "")
        body = f"@{author}"
        if text:
            body += f"：{text}"
        lines = self._fit_lines(body, q_font, max_width, 4)
        for line in lines:
            self._draw_text(draw, (x, y), line, _L.F_QUOTE, theme.text_secondary)
            y += _L.F_QUOTE_LINE_H

    # ==================== 备选布局共享组件 ====================

    @staticmethod
    def _advanced_copy(d: dict[str, Any]) -> tuple[str, str, bool]:
        """高级皮肤没有真实标题时提升正文，且不重复绘制同一段内容。"""
        title = str(d.get("title") or "").strip()
        text = str(d.get("text") or "").strip()
        return title or text, text if title else "", bool(title)

    def _prep(self, result: ParseResult, images: dict[str, Any]) -> dict[str, Any]:
        """备选布局的公共数据准备。"""
        hero = images.get("hero")
        grid = list(images.get("grid") or [])
        if hero is None and grid:
            # 没有视频封面时，用图集首图作为视觉图
            hero = grid.pop(0)
        author = result.author
        stats = parse_stats_line(result.extra.get("stats_line"))
        if dur := result.extra.get("duration"):
            stats.insert(0, ("时长：", fmt_duration(dur)))
        media = []
        seen_media: set[str] = set()
        for path in (hero, *grid):
            if not path:
                continue
            key = str(path)
            if key in seen_media:
                continue
            seen_media.add(key)
            media.append(path)
        return {
            "is_video_hero": images.get("hero") is not None,
            "hero": hero,
            "grid": grid,
            "media": media,
            "platform_text": result.platform.display_name,
            "content_type": result.content_type or "动态",
            "ts": format_timestamp(result.timestamp),
            "title": strip_emoji(result.title),
            "text": strip_emoji(result.text),
            "author": author,
            "name": (strip_emoji(author.name) or "未知作者") if author else "",
            "author_desc": strip_emoji(author.description or "") if author else "",
            "stats": stats,
            "online_text": strip_emoji(result.extra.get("online") or ""),
            "warnings": result.extra.get("limit_warnings") or [],
        }

    def _base_canvas(self, theme: _Theme, accent_rgb: tuple[int, int, int], card_h: int):
        """阴影 + 渐变底 + 光晕 + 圆角 + 描边，返回 (canvas, draw)。"""
        total_h = card_h + 14
        canvas = Image.new("RGBA", (self.width, total_h), (0, 0, 0, 0))
        shadow = Image.new("RGBA", (self.width, total_h), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (_L.SHADOW_INSET, _L.SHADOW_INSET, self.width - _L.SHADOW_INSET, total_h - 2),
            radius=_L.RADIUS + 2, fill=(0, 0, 0, theme.shadow_alpha),
        )
        canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(_L.SHADOW_BLUR)))
        grad = self._gradient((self.width, card_h), theme.gradient_top, theme.gradient_bottom)
        grad_rgba = grad.convert("RGBA")
        glow = self._radial_glow(self.width, round(self.width * 0.9), accent_rgb, theme.glow_alpha)
        grad_rgba.alpha_composite(glow, (0, 0))
        mask = Image.new("L", (self.width, card_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, self.width - 1, card_h - 1), radius=_L.RADIUS, fill=255
        )
        grad_rgba.putalpha(mask)
        canvas.alpha_composite(grad_rgba, (0, 0))
        border_layer = Image.new("RGBA", (self.width, card_h), (0, 0, 0, 0))
        ImageDraw.Draw(border_layer).rounded_rectangle(
            (0, 0, self.width - 1, card_h - 1), radius=_L.RADIUS,
            outline=_with_alpha(theme.border, theme.border_alpha), width=1,
        )
        canvas.alpha_composite(border_layer, (0, 0))
        return canvas, ImageDraw.Draw(canvas)

    def _header_badges(self, canvas, theme: _Theme, accent_rgb, d: dict) -> int:
        """纯文本头部：accent 条 + 平台药丸 + 类型 + 时间，返回结束 y。"""
        pad = _L.PAD
        bar = Image.new("RGBA", (_L.HEAD_BAR_W, _L.HEAD_BAR_H), (0, 0, 0, 0))
        for xx in range(_L.HEAD_BAR_W):
            a = int(230 * (1 - xx / max(_L.HEAD_BAR_W - 1, 1)) ** 1.3)
            ImageDraw.Draw(bar).line([(xx, 0), (xx, _L.HEAD_BAR_H)], fill=(*accent_rgb, a))
        canvas.alpha_composite(self._rounded_image(bar, _L.HEAD_BAR_H // 2), (pad, _L.HEAD_BAR_TOP))
        y = _L.HEAD_BAR_TOP + _L.HEAD_BAR_H + 18
        pw = self._platform_pill_width(d["platform_text"])
        self._draw_flat_badge(
            canvas, theme, pad, y, _L.HEAD_PILL_H, pw,
            text=d["platform_text"], accent_rgb=accent_rgb, dot=True,
            font_size=_L.F_PLATFORM, bold=True, text_rgb=theme.text_primary,
        )
        chip_font = self._font(_L.F_CHIP, bold=True)
        self._draw_text(
            ImageDraw.Draw(canvas),
            (pad + pw + 16, y + (_L.HEAD_PILL_H - self._line_height(chip_font)) // 2),
            d["content_type"], _L.F_CHIP, theme.text_tertiary, bold=True,
        )
        if d["ts"]:
            ts_font = self._font(_L.F_TIME)
            ts_w = self._text_width(d["ts"], ts_font)
            self._draw_text(
                ImageDraw.Draw(canvas),
                (self.width - pad - ts_w, y + (_L.HEAD_PILL_H - self._line_height(ts_font)) // 2),
                d["ts"], _L.F_TIME, theme.text_tertiary,
            )
        return y + _L.HEAD_PILL_H + 18

    def _avatar_block(self, canvas, draw, x: int, y: int, size: int,
                      images: dict, d: dict, accent_rgb) -> None:
        """绘制头像（含 accent 描边环或渐变首字母占位）。"""
        avatar_path = images.get("avatar")
        avatar = None
        if avatar_path:
            try:
                avatar = self._circle_avatar(self._open_image(avatar_path), size)
            except Exception:
                avatar = None
        if avatar is not None:
            canvas.alpha_composite(avatar, (x, y))
            ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            ImageDraw.Draw(ring).ellipse(
                (1, 1, size - 2, size - 2),
                outline=_with_alpha(accent_rgb, 170), width=_L.AVATAR_RING_W,
            )
            canvas.alpha_composite(ring, (x, y))
        else:
            placeholder = self._gradient(
                (size, size), accent_rgb, _mix(accent_rgb, (0, 0, 0), 0.35)
            ).convert("RGBA")
            pmask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(pmask).ellipse((0, 0, size - 1, size - 1), fill=255)
            placeholder.putalpha(pmask)
            canvas.alpha_composite(placeholder, (x, y))
            first = d["name"][:1].upper()
            f_font = self._font(_L.F_INITIAL, bold=True)
            fw = self._text_width(first, f_font)
            self._draw_text(
                ImageDraw.Draw(canvas),
                (x + (size - fw) // 2, y + (size - self._line_height(f_font)) // 2),
                first, _L.F_INITIAL, "#FFFFFF", bold=True,
            )

    def _stat_rows_height(self, d: dict, inner_w: int):
        rows = self._build_stat_rows(d["stats"], inner_w)
        if not rows:
            return 0, []
        return len(rows) * (_L.STAT_H + _L.STAT_ROW_GAP) - _L.STAT_ROW_GAP, rows

    def _draw_stat_rows(self, canvas, theme: _Theme, rows, x0: int, y: int) -> int:
        """主题感知的统计药丸行，返回结束 y。"""
        label_font = self._font(_L.F_STAT_LABEL)
        value_font = self._font(_L.F_STAT_VALUE, bold=True)
        for row in rows:
            x = x0
            for label, value in row:
                w = self._stat_pill_width(label, value)
                self._glass(
                    canvas, (x, y, x + w, y + _L.STAT_H), _L.STAT_H // 2,
                    tint_rgb=theme.stat_pill_bg, tint_alpha=theme.frost_alpha,
                    border_rgb=theme.stat_pill_bg, border_alpha=theme.frost_border_alpha,
                    blur=6,
                )
                draw = ImageDraw.Draw(canvas)
                label_w = self._text_width(label, label_font)
                tx = x + _L.STAT_PAD_X
                if value:
                    self._draw_text(draw, (tx, y + (_L.STAT_H - self._line_height(label_font)) // 2),
                                    label, _L.F_STAT_LABEL, theme.text_tertiary)
                    self._draw_text(draw, (tx + label_w + _L.STAT_LABEL_VALUE_GAP,
                                           y + (_L.STAT_H - self._line_height(value_font)) // 2),
                                    value, _L.F_STAT_VALUE, theme.text_primary, bold=True)
                else:
                    self._draw_text(draw, (tx, y + (_L.STAT_H - self._line_height(label_font)) // 2),
                                    label, _L.F_STAT_LABEL, theme.text_secondary)
                x += w + _L.STAT_GAP
            y += _L.STAT_H + _L.STAT_ROW_GAP
        return y - _L.STAT_ROW_GAP

    def _draw_online(self, draw, d: dict, y: int, accent: str) -> int:
        """在线人数（accent 圆点 + 文字），返回结束 y。"""
        online_font = self._font(_L.F_ONLINE)
        dot_cy = y + self._line_height(online_font) // 2
        draw.ellipse((_L.PAD, dot_cy - 4, _L.PAD + 8, dot_cy + 4), fill=accent)
        self._draw_text(draw, (_L.PAD + 18, y), d["online_text"], _L.F_ONLINE, accent)
        return y + 38

    def _warning_block_height(self, warnings: list[str], inner_w: int) -> int:
        """计算警告提示块的总高度。"""
        if not warnings:
            return 0
        font = self._font(20)
        line_h = self._line_height(font) + 4
        avail_w = inner_w - 46
        total_h = 0
        for raw_msg in warnings:
            msg = strip_emoji(raw_msg)
            lines = self._wrap(msg, font, avail_w)
            box_h = max(len(lines), 1) * line_h + 24
            total_h += box_h + 12
        return total_h - 12 if total_h > 0 else 0

    def _draw_warning_block(
        self, canvas: Image.Image, draw: ImageDraw.ImageDraw, theme: _Theme,
        warnings: list[str], y: int, inner_w: int, on_image: bool = False
    ) -> int:
        """渲染精致的时长超出限制警告提示块，返回结束 y。"""
        if not warnings:
            return y
        pad = _L.PAD
        font = self._font(20)
        line_h = self._line_height(font) + 4
        avail_w = inner_w - 46

        # 主题色彩适配：精致不张扬的琥珀警告色调
        if on_image:
            bg_rgb = (20, 24, 36)
            bg_alpha = 160
            border_rgb = (245, 158, 11)
            border_alpha = 75
            bar_rgb = (245, 158, 11, 230)
            text_color = (253, 230, 138)  # #FDE68A 柔金黄
        elif self.theme_name == "dark":
            bg_rgb = (245, 158, 11)
            bg_alpha = 22
            border_rgb = (245, 158, 11)
            border_alpha = 50
            bar_rgb = (245, 158, 11, 220)
            text_color = (252, 211, 77)   # #FCD34D 亮琥珀
        else:
            bg_rgb = (254, 243, 199)
            bg_alpha = 150
            border_rgb = (217, 119, 6)
            border_alpha = 60
            bar_rgb = (217, 119, 6, 220)
            text_color = (180, 83, 9)     # #B45309 深琥珀

        for raw_msg in warnings:
            msg = strip_emoji(raw_msg)
            lines = self._wrap(msg, font, avail_w)
            box_h = max(len(lines), 1) * line_h + 24

            # 毛玻璃圆角卡片底
            self._glass(
                canvas, (pad, y, pad + inner_w, y + box_h), 14,
                tint_rgb=bg_rgb, tint_alpha=bg_alpha,
                border_rgb=border_rgb, border_alpha=border_alpha, blur=6,
            )

            # 左侧 Warning Accent 竖条
            bar_layer = Image.new("RGBA", (4, max(box_h - 16, 8)), (0, 0, 0, 0))
            ImageDraw.Draw(bar_layer).rounded_rectangle(
                (0, 0, 3, max(box_h - 17, 7)), radius=2, fill=bar_rgb,
            )
            canvas.alpha_composite(bar_layer, (pad + 14, y + 8))

            # 警告文本
            draw = ImageDraw.Draw(canvas)
            ty = y + 12
            for line in lines:
                self._draw_text(draw, (pad + 28, ty), line, 20, text_color)
                ty += line_h

            y += box_h + 12

        return y + 8

    def _hero_aspect_height(self, hero_path: Path | None, box_w: int, default_h: int) -> int:
        """若开启了封面全尺寸模式 (cover_full_size)，按原图宽高比计算高度，否则使用 default_h。"""
        if not hero_path or not self.cover_full_size:
            return default_h
        try:
            with Image.open(hero_path) as im:
                w, h = im.size
                if w > 0 and h > 0:
                    return max(100, round(box_w * h / w))
        except Exception:
            pass
        return default_h

    def _footer_block(self, canvas, draw, theme: _Theme, accent: str, accent_rgb,
                      result: ParseResult, y: int, inner_w: int,
                      on_image: bool = False) -> None:
        """页脚：完整链接独占多行，署名单独放在底行。"""
        pad = _L.PAD
        _, url_lines, url_size, url_line_h = self._footer_metrics(result, inner_w)
        divider_layer = Image.new("RGBA", (inner_w, 1), (0, 0, 0, 0))
        ImageDraw.Draw(divider_layer).line(
            (0, 0, inner_w - 1, 0),
            fill=((255, 255, 255, 60) if on_image
                  else _with_alpha(theme.divider, 14 if self.theme_name == "dark" else 12)),
            width=1,
        )
        canvas.alpha_composite(divider_layer, (pad, y + 12))
        color = (255, 255, 255, 176) if on_image else theme.text_tertiary
        url_y = y + 28
        for line in url_lines:
            self._draw_text(draw, (pad, url_y), line, url_size, color)
            url_y += url_line_h
        foot_y = url_y + (10 if url_lines else 0)
        wm_font = self._font(_L.F_FOOT, bold=True)
        wm_text_w = self._text_width(self.watermark, wm_font)
        wm_group_w = _L.WM_DOT + _L.WM_DOT_GAP + wm_text_w
        wm_x = self.width - pad - wm_group_w
        wm_lh = self._line_height(wm_font)
        dot_cy = foot_y + wm_lh // 2
        draw.ellipse(
            (wm_x, dot_cy - _L.WM_DOT // 2, wm_x + _L.WM_DOT, dot_cy + _L.WM_DOT // 2),
            fill=(*accent_rgb, 255),
        )
        self._draw_text(draw, (wm_x + _L.WM_DOT + _L.WM_DOT_GAP, foot_y),
                        self.watermark, _L.F_FOOT, accent, bold=True)
        source_label = "原始链接"
        self._draw_text(
            draw,
            (pad, foot_y + 2),
            source_label,
            14,
            color,
            bold=True,
        )

    def _draw_grid_block(self, canvas, draw, theme: _Theme, grid: list,
                         y: int, inner_w: int, gap: int) -> int:
        """图集网格，返回结束 y（自带异常兜底）。"""
        pad = _L.PAD
        grid_h, cols, rows, cell_h = self._grid_metrics(len(grid), inner_w, gap)
        if not grid_h:
            return y
        try:
            show = cols * rows
            over = len(grid) - show if len(grid) > show else 0
            for idx, path in enumerate(grid[:show]):
                r, c = divmod(idx, cols)
                x = pad + c * (cell_h + gap)
                yy = y + r * (cell_h + gap)
                try:
                    img = self._cover_fit(self._open_image(path), cell_h, cell_h)
                    img = self._rounded_image(img, _L.GRID_RADIUS)
                    canvas.alpha_composite(img, (x, yy))
                except Exception:
                    ph = Image.new("RGBA", (cell_h, cell_h), (0, 0, 0, 0))
                    ImageDraw.Draw(ph).rounded_rectangle(
                        (0, 0, cell_h - 1, cell_h - 1), radius=_L.GRID_RADIUS,
                        fill=_with_alpha(theme.pill_bg, 14),
                    )
                    canvas.alpha_composite(ph, (x, yy))
                if idx == show - 1 and over > 0:
                    canvas.alpha_composite(
                        self._scrim(cell_h, cell_h, start=0.0, max_alpha=150, power=1.2),
                        (x, yy),
                    )
                    plus_font = self._font(_L.F_PLUS, bold=True)
                    pw = self._text_width(f"+{over}", plus_font)
                    self._draw_text(
                        ImageDraw.Draw(canvas),
                        (x + (cell_h - pw) // 2, yy + (cell_h - self._line_height(plus_font)) // 2),
                        f"+{over}", _L.F_PLUS, "#FFFFFF", bold=True,
                    )
            return y + grid_h + 20
        except Exception:
            logger.warning("图集渲染失败，已跳过", exc_info=True)
            return y

    def _draw_quote_block(self, canvas, draw, theme: _Theme, accent_rgb,
                          result: ParseResult, y: int, inner_w: int) -> int:
        """转发引用（毛玻璃容器 + accent 竖条），返回结束 y。"""
        if not result.repost:
            return y
        pad = _L.PAD
        quote_h = self._measure_quote(result.repost, inner_w)
        self._glass(
            canvas, (pad, y, pad + inner_w, y + quote_h), _L.QUOTE_RADIUS,
            tint_rgb=theme.quote_bg, tint_alpha=theme.frost_alpha,
            border_rgb=theme.quote_bg, border_alpha=theme.frost_border_alpha, blur=6,
        )
        bar = Image.new("RGBA", (_L.QUOTE_BAR_W, quote_h - 32), (0, 0, 0, 0))
        ImageDraw.Draw(bar).rounded_rectangle(
            (0, 0, _L.QUOTE_BAR_W - 1, quote_h - 33), radius=_L.QUOTE_BAR_W // 2,
            fill=(*accent_rgb, 230),
        )
        canvas.alpha_composite(bar, (pad + 18, y + 16))
        self._draw_quote_text(
            ImageDraw.Draw(canvas), result.repost,
            pad + 18 + _L.QUOTE_BAR_W + 16, y + 16,
            inner_w - 18 * 2 - _L.QUOTE_BAR_W - 16, theme,
        )
        return y + quote_h + 20

    # ==================== 布局：双栏杂志 ====================

    def _render_magazine(self, result, images, out_path) -> Path:
        """双栏杂志：封面缩为左侧方块，标题/作者在右侧栏。"""
        theme = _THEMES[self.theme_name]
        accent = PLATFORM_COLORS.get(result.platform.name, PLATFORM_COLORS["default"])
        accent_rgb = _hex_to_rgb(accent)
        pad = _L.PAD
        inner_w = self.width - pad * 2
        gap = _L.GRID_GAP
        d = self._prep(result, images)

        cover_w = round(inner_w * 0.42) if d["hero"] else 0
        cover_h = self._hero_aspect_height(d["hero"], cover_w, cover_w) if d["hero"] else 0
        right_x = pad + cover_w + 26
        right_w = inner_w - cover_w - 26 if d["hero"] else inner_w

        title_font = self._font(33, bold=True)
        title_lines = (
            self._fit_lines(d["title"], title_font, right_w, 4 if d["hero"] else 3)
            if d["title"] else []
        )
        desc_font = self._font(_L.F_DESC)
        desc_lines = self._fit_lines(d["text"], desc_font, inner_w, 4) if d["text"] else []
        stats_h, stat_rows = self._stat_rows_height(d, inner_w)
        warnings_h = self._warning_block_height(d["warnings"], inner_w)
        grid_h = self._grid_metrics(len(d["grid"]), inner_w, gap)[0]
        quote_h = self._measure_quote(result.repost, inner_w) if result.repost else 0
        footer_h = self._footer_metrics(result, inner_w)[0]

        # ---- 高度 ----
        y = _L.HEAD_BAR_TOP + _L.HEAD_BAR_H + 18 + _L.HEAD_PILL_H + 20
        if d["hero"]:
            right_h = len(title_lines) * 46 + (18 + 56 if d["author"] else 0)
            y += max(cover_h, right_h) + 22
        else:
            y += len(title_lines) * _L.F_TITLE_LINE_H + 14
            if d["author"]:
                y += _L.AVATAR + 20
        if desc_lines:
            y += len(desc_lines) * _L.F_DESC_LINE_H + 16
        if stats_h:
            y += stats_h + 18
        if d["online_text"]:
            y += 38
        if warnings_h:
            y += warnings_h + 20
        if grid_h:
            y += grid_h + 20
        if quote_h:
            y += quote_h + 20
        y += footer_h
        card_h = y

        canvas, draw = self._base_canvas(theme, accent_rgb, card_h)
        y = self._header_badges(canvas, theme, accent_rgb, d) + 2

        if d["hero"]:
            # 左侧封面方块
            try:
                img = self._cover_fit(self._open_image(d["hero"]), cover_w, cover_h)
                img = self._rounded_image(img, 20)
                canvas.alpha_composite(img, (pad, y))
            except Exception:
                ph = self._fallback_cover(cover_w, cover_h, theme, accent_rgb, 20)
                canvas.alpha_composite(ph, (pad, y))
            if d["is_video_hero"]:
                r_ = 38
                cx, cy = pad + cover_w // 2, y + cover_h // 2
                self._glass(canvas, (cx - r_, cy - r_, cx + r_, cy + r_), r_,
                            (255, 255, 255), 34, (255, 255, 255), 110, blur=8)
                ImageDraw.Draw(canvas).polygon(
                    [(cx - 10, cy - 15), (cx - 10, cy + 15), (cx + 17, cy)],
                    fill=(255, 255, 255, 245),
                )
            # 右栏：标题 + 作者
            ry = y + 4
            draw = ImageDraw.Draw(canvas)
            for line in title_lines:
                self._draw_text(draw, (right_x, ry), line, 33, theme.text_primary, bold=True)
                ry += 46
            if d["author"]:
                ry += 18
                self._avatar_block(canvas, draw, right_x, ry, 56, images, d, accent_rgb)
                draw = ImageDraw.Draw(canvas)
                self._draw_text(draw, (right_x + 72, ry + 2), d["name"], 24, theme.text_primary, bold=True)
                if d["author_desc"]:
                    self._draw_text(draw, (right_x + 72, ry + 56 - 22), d["author_desc"], 18, theme.text_tertiary)
            y += max(cover_h, len(title_lines) * 46 + (18 + 56 if d["author"] else 0)) + 22
        else:
            for line in title_lines:
                self._draw_text(draw, (pad, y), line, _L.F_TITLE, theme.text_primary, bold=True)
                y += _L.F_TITLE_LINE_H
            y += 14
            if d["author"]:
                self._avatar_block(canvas, draw, pad, y, _L.AVATAR, images, d, accent_rgb)
                draw = ImageDraw.Draw(canvas)
                name_x = pad + _L.AVATAR + 20
                self._draw_text(draw, (name_x, y + 6), d["name"], _L.F_NAME, theme.text_primary, bold=True)
                if d["author_desc"]:
                    self._draw_text(draw, (name_x, y + _L.AVATAR - 26), d["author_desc"], _L.F_SIGN, theme.text_tertiary)
                y += _L.AVATAR + 20

        if desc_lines:
            for line in desc_lines:
                self._draw_text(draw, (pad, y), line, _L.F_DESC, theme.text_secondary)
                y += _L.F_DESC_LINE_H
            y += 16
        if stat_rows:
            y = self._draw_stat_rows(canvas, theme, stat_rows, pad, y) + 18
            draw = ImageDraw.Draw(canvas)
        if d["online_text"]:
            y = self._draw_online(draw, d, y, accent)
        if d["warnings"]:
            y = self._draw_warning_block(canvas, draw, theme, d["warnings"], y, inner_w)
            draw = ImageDraw.Draw(canvas)
        y = self._draw_grid_block(canvas, draw, theme, d["grid"], y, inner_w, gap)
        y = self._draw_quote_block(canvas, ImageDraw.Draw(canvas), theme, accent_rgb, result, y, inner_w)
        self._footer_block(canvas, ImageDraw.Draw(canvas), theme, accent, accent_rgb, result, y, inner_w)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, "PNG", optimize=True)
        return out_path

    # ==================== 布局：沉浸全屏 ====================

    def _render_immersive(self, result, images, out_path) -> Path:
        """沉浸全屏：封面占上部整宽，标题/作者/统计在封面下方内容区（无图回退标准布局）。"""
        if images.get("hero") is None and not images.get("grid"):
            return self._render_standard(result, images, out_path)

        theme = _THEMES[self.theme_name]
        accent = PLATFORM_COLORS.get(result.platform.name, PLATFORM_COLORS["default"])
        accent_rgb = _hex_to_rgb(accent)
        pad = _L.PAD
        inner_w = self.width - pad * 2
        gap = _L.GRID_GAP
        d = self._prep(result, images)

        title_font = self._font(_L.F_TITLE, bold=True)
        if d["title"]:
            title_lines = self._fit_lines(d["title"], title_font, inner_w, 2)
        else:
            title_lines = []
        stats_h, stat_rows = self._stat_rows_height(d, inner_w)
        name_lh = self._line_height(self._font(_L.F_NAME, bold=True))
        sign_lh = self._line_height(self._font(_L.F_SIGN))
        title_lh = _L.F_TITLE_LINE_H
        title_block_h = len(title_lines) * title_lh if title_lines else 0
        warnings_h = self._warning_block_height(d["warnings"], inner_w)
        grid_h = self._grid_metrics(len(d["grid"]), inner_w, gap)[0]
        footer_h = self._footer_metrics(result, inner_w)[0]

        # 封面（上部整宽）
        hero_h = self._hero_aspect_height(d["hero"], self.width, round(self.width * 0.8))

        # 封面下方内容栈高度
        stack = 20
        if d["author"]:
            stack += 72 + 16
        else:
            stack += 12
        if title_lines:
            stack += title_block_h + 18
        if stats_h:
            stack += stats_h + 16
        if d["online_text"]:
            stack += 36
        if warnings_h:
            stack += warnings_h + 16
        if grid_h:
            stack += grid_h + 20
        stack += footer_h
        card_h = hero_h + stack

        canvas, draw = self._base_canvas(theme, accent_rgb, card_h)

        # 封面铺满上部
        try:
            img = self._cover_fit(self._open_image(d["hero"]), self.width, hero_h)
            img = self._rounded_image_top(img, _L.RADIUS)
            canvas.alpha_composite(img, (0, 0))
        except Exception:
            ph = self._fallback_cover(
                self.width, hero_h, theme, accent_rgb,
                _L.RADIUS, top_only=True,
            )
            canvas.alpha_composite(ph, (0, 0))

        # 顶部 scrim：保证徽章可读
        canvas.alpha_composite(
            self._scrim(
                self.width, hero_h,
                start=_L.TOP_SCRIM_END, max_alpha=_L.TOP_SCRIM_ALPHA,
                power=1.4, invert=True,
            ),
            (0, 0),
        )

        # 卡片描边（覆盖在封面上方，保持轮廓清晰）
        border_layer = Image.new("RGBA", (self.width, card_h), (0, 0, 0, 0))
        ImageDraw.Draw(border_layer).rounded_rectangle(
            (0, 0, self.width - 1, card_h - 1), radius=_L.RADIUS,
            outline=_with_alpha(theme.border, theme.border_alpha), width=1,
        )
        canvas.alpha_composite(border_layer, (0, 0))

        # 顶部徽章
        pw = self._platform_pill_width(d["platform_text"])
        badge_y = _L.HERO_BADGE_TOP
        self._draw_hero_badge(canvas, pad, badge_y, _L.HERO_BADGE_H, pw,
                              text=d["platform_text"], accent_rgb=accent_rgb, dot=True,
                              font_size=_L.F_PLATFORM)
        chip_font = self._font(_L.F_CHIP, bold=True)
        type_w = self._text_width(d["content_type"], chip_font) + 36
        self._draw_hero_badge(canvas, pad + pw + _L.HERO_BADGE_GAP, badge_y, _L.HERO_BADGE_H, type_w,
                              text=d["content_type"], accent_rgb=accent_rgb, dot=False,
                              font_size=_L.F_CHIP)
        if d["ts"]:
            ts_font = self._font(_L.F_TIME)
            ts_w = self._text_width(d["ts"], ts_font) + 32
            self._draw_hero_badge(canvas, self.width - pad - ts_w, badge_y, _L.HERO_BADGE_H, ts_w,
                                  text=d["ts"], accent_rgb=accent_rgb, dot=False,
                                  font_size=_L.F_TIME, bold=False)

        # 播放按钮（封面中央）
        if d["is_video_hero"] and self.show_play_button:
            r_ = _L.PLAY_R
            cx = self.width // 2
            cy = hero_h // 2
            self._glass(canvas, (cx - r_, cy - r_, cx + r_, cy + r_), r_,
                        (255, 255, 255), 34, (255, 255, 255), 110, blur=8)
            ImageDraw.Draw(canvas).polygon(
                [(cx - 12, cy - 18), (cx - 12, cy + 18), (cx + 20, cy)],
                fill=(255, 255, 255, 245),
            )

        # ---- 封面下方内容区：作者行（小头像）+ 大标题独立一行 ----
        y = hero_h + 20
        draw = ImageDraw.Draw(canvas)
        if d["author"]:
            self._avatar_block(canvas, draw, pad, y, 72, images, d, accent_rgb)
            draw = ImageDraw.Draw(canvas)
            name_x = pad + 72 + 20
            name_block_h = name_lh + (sign_lh + 4 if d["author_desc"] else 0)
            name_y = y + (72 - name_block_h) // 2
            self._draw_text(draw, (name_x, name_y), d["name"], _L.F_NAME, theme.text_primary, bold=True)
            if d["author_desc"]:
                self._draw_text(draw, (name_x, name_y + name_lh + 4), d["author_desc"], _L.F_SIGN, theme.text_tertiary)
            y += 72 + 16
        else:
            y += 12
        if title_lines:
            for line in title_lines:
                self._draw_text(draw, (pad, y), line, _L.F_TITLE, theme.text_primary, bold=True)
                y += _L.F_TITLE_LINE_H
            y += 18
        if stat_rows:
            y = self._draw_stat_rows(canvas, theme, stat_rows, pad, y) + 18
            draw = ImageDraw.Draw(canvas)
        if d["online_text"]:
            y = self._draw_online(draw, d, y, accent)
        if d["warnings"]:
            y = self._draw_warning_block(canvas, draw, theme, d["warnings"], y, inner_w)
            draw = ImageDraw.Draw(canvas)
        y = self._draw_grid_block(
            canvas,
            draw,
            theme,
            d["grid"],
            y,
            inner_w,
            gap,
        )
        self._footer_block(canvas, draw, theme, accent, accent_rgb, result, y, inner_w)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, "PNG", optimize=True)
        return out_path

    # ==================== 布局：社交动态流 ====================

    def _render_feed(self, result, images, out_path) -> Path:
        """社交动态：作者行最前，媒体为内嵌圆角块。"""
        theme = _THEMES[self.theme_name]
        accent = PLATFORM_COLORS.get(result.platform.name, PLATFORM_COLORS["default"])
        accent_rgb = _hex_to_rgb(accent)
        pad = _L.PAD
        inner_w = self.width - pad * 2
        gap = _L.GRID_GAP
        d = self._prep(result, images)

        title_font = self._font(34, bold=True)
        title_lines = self._fit_lines(d["title"], title_font, inner_w, 3) if d["title"] else []
        desc_font = self._font(_L.F_DESC)
        desc_lines = self._fit_lines(d["text"], desc_font, inner_w, 5) if d["text"] else []
        media_h = round(inner_w * 9 / 16) if d["hero"] else 0
        if d["hero"] and self.cover_full_size:
            media_h = self._hero_aspect_height(d["hero"], inner_w, media_h)
        stats_h, stat_rows = self._stat_rows_height(d, inner_w)
        warnings_h = self._warning_block_height(d["warnings"], inner_w)
        grid_h = self._grid_metrics(len(d["grid"]), inner_w, gap)[0]
        quote_h = self._measure_quote(result.repost, inner_w) if result.repost else 0
        footer_h = self._footer_metrics(result, inner_w)[0]

        # ---- 高度 ----
        y = 38
        if d["author"]:
            y += 68 + 20
        y += len(title_lines) * 48 + (12 if title_lines else 0)
        if desc_lines:
            y += len(desc_lines) * _L.F_DESC_LINE_H + 16
        if media_h:
            y += media_h + 20
        if stats_h:
            y += stats_h + 18
        if d["online_text"]:
            y += 38
        if warnings_h:
            y += warnings_h + 20
        if grid_h:
            y += grid_h + 20
        if quote_h:
            y += quote_h + 20
        y += footer_h
        card_h = y

        canvas, draw = self._base_canvas(theme, accent_rgb, card_h)

        # 作者行最前（无顶部 accent 条/头部徽章）
        y = 38
        if d["author"]:
            self._avatar_block(canvas, draw, pad, y, 68, images, d, accent_rgb)
            draw = ImageDraw.Draw(canvas)
            name_x = pad + 68 + 20
            name_font = self._font(26, bold=True)
            name_w = self._text_width(d["name"], name_font)
            self._draw_text(draw, (name_x, y + 8), d["name"], 26, theme.text_primary, bold=True)
            # 平台小药丸跟在昵称后
            pill_font = self._font(17, bold=True)
            pill_w = 12 + 8 + 6 + self._text_width(d["platform_text"], pill_font) + 12
            pill_h = 30
            pill_y = y + 8 + (self._line_height(name_font) - pill_h) // 2
            self._glass(canvas, (name_x + name_w + 12, pill_y,
                                 name_x + name_w + 12 + pill_w, pill_y + pill_h),
                        pill_h // 2, theme.pill_bg, theme.frost_alpha,
                        theme.pill_bg, theme.frost_border_alpha, blur=6)
            draw = ImageDraw.Draw(canvas)
            dot_cy = pill_y + pill_h // 2
            draw.ellipse((name_x + name_w + 24, dot_cy - 4, name_x + name_w + 32, dot_cy + 4),
                         fill=(*accent_rgb, 255))
            self._draw_text(draw, (name_x + name_w + 24 + 14,
                                   pill_y + (pill_h - self._line_height(pill_font)) // 2),
                            d["platform_text"], 17, theme.text_secondary, bold=True)
            if d["author_desc"]:
                self._draw_text(draw, (name_x, y + 68 - 26), d["author_desc"], _L.F_SIGN,
                                theme.text_tertiary)
            if d["ts"]:
                ts_font = self._font(_L.F_TIME)
                ts_w = self._text_width(d["ts"], ts_font)
                self._draw_text(draw, (self.width - pad - ts_w, y + 12), d["ts"], _L.F_TIME,
                                theme.text_tertiary)
            y += 68 + 20

        for line in title_lines:
            self._draw_text(draw, (pad, y), line, 34, theme.text_primary, bold=True)
            y += 48
        if title_lines:
            y += 12
        for line in desc_lines:
            self._draw_text(draw, (pad, y), line, _L.F_DESC, theme.text_secondary)
            y += _L.F_DESC_LINE_H
        if desc_lines:
            y += 16

        # 媒体内嵌圆角块（类型 chip 浮在媒体角上）
        if d["hero"]:
            try:
                img = self._cover_fit(self._open_image(d["hero"]), inner_w, media_h)
                img = self._rounded_image(img, 20)
                canvas.alpha_composite(img, (pad, y))
            except Exception:
                ph = self._fallback_cover(inner_w, media_h, theme, accent_rgb, 20)
                canvas.alpha_composite(ph, (pad, y))
            chip_font = self._font(18, bold=True)
            chip_w = self._text_width(d["content_type"], chip_font) + 28
            self._draw_hero_badge(canvas, pad + 14, y + 14, 34, chip_w,
                                  text=d["content_type"], accent_rgb=accent_rgb, dot=False,
                                  font_size=18)
            if d["is_video_hero"] and self.show_play_button:
                r_ = _L.PLAY_R
                cx, cy = self.width // 2, y + media_h // 2
                self._glass(canvas, (cx - r_, cy - r_, cx + r_, cy + r_), r_,
                            (255, 255, 255), 34, (255, 255, 255), 110, blur=8)
                ImageDraw.Draw(canvas).polygon(
                    [(cx - 12, cy - 18), (cx - 12, cy + 18), (cx + 20, cy)],
                    fill=(255, 255, 255, 245),
                )
            y += media_h + 20
            draw = ImageDraw.Draw(canvas)

        if stat_rows:
            y = self._draw_stat_rows(canvas, theme, stat_rows, pad, y) + 18
            draw = ImageDraw.Draw(canvas)
        if d["online_text"]:
            y = self._draw_online(draw, d, y, accent)
        if d["warnings"]:
            y = self._draw_warning_block(canvas, draw, theme, d["warnings"], y, inner_w)
            draw = ImageDraw.Draw(canvas)
        y = self._draw_grid_block(canvas, draw, theme, d["grid"], y, inner_w, gap)
        y = self._draw_quote_block(canvas, ImageDraw.Draw(canvas), theme, accent_rgb, result, y, inner_w)
        self._footer_block(canvas, ImageDraw.Draw(canvas), theme, accent, accent_rgb, result, y, inner_w)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, "PNG", optimize=True)
        return out_path
