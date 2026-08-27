"""卡片设计系统：字体与文字排版。

集中处理：
- 系统中文字体探测（带缓存，避免每次渲染都扫目录）；
- 文本度量、CJK + ASCII 混排换行、行数裁剪、单行省略；
- 字距（tracking）绘制与无粗体字体时的伪粗体；
- 段落绘制（返回实际占用高度，供流式布局精确测量）。
"""

from __future__ import annotations

import math
import re
import sys
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..logger import logger

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]


# ============================ 字体探测 ============================

FONT_CANDIDATES: dict[str, tuple[tuple[str, str], ...]] = {
    "win32": (
        ("msyh.ttc", "msyhbd.ttc"),
        ("simhei.ttf", "msyhbd.ttc"),
        ("Deng.ttf", "Dengb.ttf"),
        ("simsun.ttc", "simsun.ttc"),
        ("NotoSansSC-VF.ttf", "NotoSansSC-VF.ttf"),
    ),
    "darwin": (
        ("PingFang.ttc", "PingFang.ttc"),
        ("Hiragino Sans GB.ttc", "Hiragino Sans GB.ttc"),
        ("STHeiti Medium.ttc", "STHeiti Medium.ttc"),
    ),
    "linux": (
        ("NotoSansCJK-Regular.ttc", "NotoSansCJK-Bold.ttc"),
        ("NotoSansCJKsc-Regular.otf", "NotoSansCJKsc-Bold.otf"),
        ("SourceHanSansSC-Regular.otf", "SourceHanSansSC-Bold.otf"),
        ("wqy-zenhei.ttc", "wqy-zenhei.ttc"),
        ("wqy-microhei.ttc", "wqy-microhei.ttc"),
        ("DroidSansFallbackFull.ttf", "DroidSansFallbackFull.ttf"),
        ("arpluminghk-regular.ttf", "arpluminghk-regular.ttf"),
    ),
}

FONT_DIRS = (
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
    "/usr/share/fonts",
)


@lru_cache(maxsize=16)
def discover_fonts(custom_path: str | None = None) -> tuple[str | None, str | None]:
    """查找可用中文字体，返回 (常规, 粗体) 路径；结果按 custom_path 缓存。"""
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

    candidates = FONT_CANDIDATES.get(sys.platform, FONT_CANDIDATES["linux"])
    dirs = [Path(d) for d in FONT_DIRS]

    for regular_name, bold_name in candidates:
        for d in dirs:
            reg, bol = d / regular_name, d / bold_name
            if reg.exists():
                return str(reg), str(bol) if bol.exists() else None

    for d in dirs:
        if not d.is_dir():
            continue
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.suffix.lower() in {".ttf", ".ttc", ".otf"}:
                return str(p), None
    return None, None


# ============================ 文本清洗 ============================

_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001F0FF"
    "\U0001F100-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U0001D400-\U0001D7FF"
    "\U00002600-\U000026FF"
    "\U00002700-\U000027BF"
    "\U0000FE00-\U0000FE0F"
    "\U0001F1E6-\U0001F1FF"
    "\U0001F3FB-\U0001F3FF"
    "\U0000200D"
    "\U000E0020-\U000E007F"
    "]+"
)

_TOKEN_RE = re.compile(r"[\x21-\x7e]+|[ \t]+|.")

#: 中文排版禁则：这些字符不允许落在行首（收束类标点），必要时让上一行轻微溢出
_NO_LINE_START = frozenset(
    "，。、；：？！?!…‥·》〉」』）】〕｝’”\u2019\u201d,.;:%)]}>"
)
#: 这些字符不允许落在行尾（起始类括号与引号），必要时下移到次行
_NO_LINE_END = frozenset("《〈「『（【〔｛‘“\u2018\u201c([{")


#: NFKC 会把全角中文标点压成 ASCII（「，」->「,」），中文排版下非常难看。
#: 这里按字符归一化并跳过这些标点，只把花体 / 数学字母 / 全角字母数字拉回常规字形。
_KEEP_PUNCTUATION = frozenset(
    "，。、；：？！…—～·「」『』（）【】《》〈〉“”‘’〔〕｛｝"
)


def clean_text(text: str | None) -> str:
    """去 emoji + 逐字 NFKC 归一化，避免字体缺字渲染成方块。

    与整串 NFKC 不同：中文全角标点原样保留，避免正文里出现
    「先量后画,彻底移除」这种半角逗号紧贴汉字的排版事故。
    """
    if not text:
        return ""
    cleaned = "".join(
        ch if ch in _KEEP_PUNCTUATION else unicodedata.normalize("NFKC", ch)
        for ch in text
    )
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _EMOJI_RE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def limit_chars(text: str | None, max_chars: int) -> str:
    """按字符数硬限制文本，超长时以省略号收尾。"""
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    return value[: max(1, max_chars - 1)].rstrip() + "…"


# ============================ 排版器 ============================

REGULAR = "regular"
BOLD = "bold"


@dataclass(slots=True)
class TypeSetter:
    """字体缓存 + 度量 + 换行 + 绘制。"""

    font_path: str | None = None
    _regular: str | None = None
    _bold: str | None = None
    _loaded: bool = False
    _cache: dict[tuple[int, bool], Any] = None  # type: ignore[assignment]
    _measure: Any = None
    _wrap_cache: dict[tuple[int, int, bool, str], tuple[str, ...]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._cache = {}
        self._wrap_cache = {}
        if Image is not None:
            self._measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

    # ---------- 字体 ----------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self._regular, self._bold = discover_fonts(self.font_path)
        if not self._regular:
            logger.warning(
                "未找到可用的中文字体，解析卡片文字可能显示为方块，"
                "可在插件配置的自定义字体路径中指定字体文件"
            )

    @property
    def has_real_bold(self) -> bool:
        self._ensure_loaded()
        return bool(self._bold)

    def font(self, size: int, bold: bool = False) -> Any:
        size = max(8, int(size))
        key = (size, bool(bold))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        self._ensure_loaded()
        path = self._bold if bold and self._bold else self._regular
        try:
            font = ImageFont.truetype(str(path), size) if path else ImageFont.load_default()
        except Exception:
            logger.exception(f"加载字体失败: {path}")
            font = ImageFont.load_default()
        self._cache[key] = font
        return font

    def fake_bold_stroke(self, bold: bool) -> int:
        """没有真粗体字体时，用描边模拟粗体。"""
        self._ensure_loaded()
        return 1 if bold and not self._bold and self._regular else 0

    # ---------- 度量 ----------

    def width(self, text: str, font: Any) -> int:
        if not text:
            return 0
        if self._measure is None:
            return len(text) * getattr(font, "size", 12)
        return math.ceil(self._measure.textlength(text, font=font))

    def tracked_width(self, text: str, font: Any, tracking: float = 0.0) -> int:
        base = self.width(text, font)
        if tracking and len(text) > 1:
            base += int(round(tracking * (len(text) - 1)))
        return base

    def line_height(self, font: Any, leading: float = 1.0) -> int:
        try:
            ascent, descent = font.getmetrics()
            base = ascent + descent
        except Exception:
            base = int(getattr(font, "size", 14) * 1.25)
        return max(1, int(round(base * max(0.6, leading))))

    # ---------- 换行 ----------

    def wrap(self, text: str, font: Any, max_width: int) -> list[str]:
        """按实际宽度换行：CJK 可逐字折行，短 ASCII 词尽量保持完整。"""
        key = (id(font), int(max_width), False, text)
        cached = self._wrap_cache.get(key)
        if cached is not None:
            return list(cached)
        source = str(text or "")
        if not source.strip():
            # 空内容必须返回空列表，否则区块会白占一行高度（并把装饰线也画出来）
            self._wrap_cache[key] = []
            return []
        lines: list[str] = []
        for raw in source.strip("\n").split("\n"):
            if not raw:
                lines.append("")
                continue
            current = ""
            for token in _TOKEN_RE.findall(raw):
                if token.isspace():
                    if current and not current.endswith(" "):
                        candidate = current + " "
                        if self.width(candidate, font) <= max_width:
                            current = candidate
                    continue
                if self.width(current + token, font) <= max_width:
                    current += token
                    continue
                # 禁则一：收束标点不落行首，允许上一行溢出一个字身
                if (
                    current
                    and len(token) == 1
                    and token in _NO_LINE_START
                    and current[-1] not in _NO_LINE_START
                ):
                    current += token
                    continue
                if current:
                    # 禁则二：起始括号不落行尾，把它挪到下一行开头
                    head, hang = current, ""
                    while head and head[-1] in _NO_LINE_END:
                        hang = head[-1] + hang
                        head = head[:-1]
                    if head.strip():
                        lines.append(head.rstrip())
                        current = hang
                    else:
                        lines.append(current.rstrip())
                        current = ""
                if self.width(current + token, font) <= max_width:
                    current += token
                    continue
                if not current and self.width(token, font) <= max_width:
                    current = token
                    continue
                for ch in token:
                    if current and self.width(current + ch, font) > max_width:
                        if ch in _NO_LINE_START and current[-1] not in _NO_LINE_START:
                            current += ch
                            continue
                        lines.append(current.rstrip())
                        current = ch
                    else:
                        current += ch
            lines.append(current.rstrip())
        if len(self._wrap_cache) < 4096:
            self._wrap_cache[key] = tuple(lines)
        return lines

    def fit(self, text: str, font: Any, max_width: int, max_lines: int) -> list[str]:
        """换行后裁剪到 max_lines 行，末行补省略号。"""
        lines = self.wrap(text, font, max_width)
        if max_lines <= 0:
            return []
        if len(lines) <= max_lines:
            return lines
        result = lines[: max_lines - 1]
        last = lines[max_lines - 1]
        while last and self.width(last + "…", font) > max_width:
            last = last[:-1]
        result.append(last + "…")
        return result

    def ellipsize(self, text: str, font: Any, max_width: int) -> str:
        value = str(text or "")
        if max_width <= 0:
            return ""
        if self.width(value, font) <= max_width:
            return value
        while value and self.width(value + "…", font) > max_width:
            value = value[:-1]
        return value + "…" if value else ""

    def fit_size(
        self,
        text: str,
        max_width: int,
        sizes: tuple[int, ...],
        *,
        bold: bool = False,
        tracking: float = 0.0,
    ) -> tuple[int, Any]:
        """在候选字号里挑第一个能单行放下的；都放不下则返回最小字号。"""
        chosen = sizes[-1] if sizes else 14
        for size in sizes:
            font = self.font(size, bold)
            if self.tracked_width(text, font, tracking) <= max_width:
                return size, font
        return chosen, self.font(chosen, bold)

    # ---------- 绘制 ----------

    def draw_line(
        self,
        draw: Any,
        xy: tuple[int, int],
        text: str,
        font: Any,
        fill: Any,
        *,
        tracking: float = 0.0,
        bold: bool = False,
        anchor: str | None = None,
    ) -> int:
        """绘制单行文字，返回绘制后的墨迹宽度。"""
        if not text:
            return 0
        stroke = self.fake_bold_stroke(bold)
        if not tracking:
            draw.text(
                xy,
                text,
                font=font,
                fill=fill,
                stroke_width=stroke,
                stroke_fill=fill if stroke else None,
                anchor=anchor,
            )
            return self.width(text, font)
        x, y = xy
        cursor = float(x)
        for ch in text:
            draw.text(
                (int(round(cursor)), y),
                ch,
                font=font,
                fill=fill,
                stroke_width=stroke,
                stroke_fill=fill if stroke else None,
            )
            cursor += self.width(ch, font) + tracking
        return int(round(cursor - tracking - x))

    def draw_paragraph(
        self,
        draw: Any,
        xy: tuple[int, int],
        lines: list[str],
        font: Any,
        fill: Any,
        *,
        leading: float = 1.45,
        tracking: float = 0.0,
        bold: bool = False,
        align: str = "left",
        max_width: int | None = None,
    ) -> int:
        """绘制多行段落，返回占用高度。"""
        if not lines:
            return 0
        step = self.line_height(font, leading)
        x, y = xy
        for index, line in enumerate(lines):
            if line:
                draw_x = x
                if align != "left" and max_width:
                    ink = self.tracked_width(line, font, tracking)
                    if align == "center":
                        draw_x = x + max(0, (max_width - ink) // 2)
                    elif align == "right":
                        draw_x = x + max(0, max_width - ink)
                self.draw_line(
                    draw,
                    (draw_x, y + index * step),
                    line,
                    font,
                    fill,
                    tracking=tracking,
                    bold=bold,
                )
        return step * len(lines)

    def paragraph_height(self, lines: list[str], font: Any, leading: float = 1.45) -> int:
        if not lines:
            return 0
        return self.line_height(font, leading) * len(lines)
