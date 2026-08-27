"""卡片设计系统：色彩与调色板。

新框架把"配色"与"结构"彻底分开：
- 本模块只负责颜色（调色板 / 平台强调色 / 颜色运算）。
- 结构、排版、装饰分别由 metrics / typeset / surface / blocks 负责。

每套主题都提供深浅两种模式，因此"卡片主题（深色/浅色）"对所有风格都生效，
不再出现"选了高级皮肤后主题被忽略"的情况。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

RGB = tuple[int, int, int]
RGBA = tuple[int, int, int, int]


# ============================ 颜色运算 ============================


def hex_to_rgb(value: str | RGB) -> RGB:
    """支持 "#RRGGBB" / "RRGGBB" / 已是 RGB 元组的输入。"""
    if isinstance(value, (tuple, list)):
        r, g, b = (int(value[0]), int(value[1]), int(value[2]))
        return (r, g, b)
    text = str(value).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def alpha(color: str | RGB, value: int) -> RGBA:
    """给颜色附加 alpha 通道（0-255）。"""
    r, g, b = hex_to_rgb(color)
    return (r, g, b, max(0, min(255, int(value))))


def mix(a: str | RGB, b: str | RGB, ratio: float) -> RGB:
    """把颜色 a 按 ratio(0-1) 向 b 混合。"""
    ca, cb = hex_to_rgb(a), hex_to_rgb(b)
    t = max(0.0, min(1.0, float(ratio)))
    return (
        round(ca[0] + (cb[0] - ca[0]) * t),
        round(ca[1] + (cb[1] - ca[1]) * t),
        round(ca[2] + (cb[2] - ca[2]) * t),
    )


def lighten(color: str | RGB, ratio: float) -> RGB:
    return mix(color, (255, 255, 255), ratio)


def darken(color: str | RGB, ratio: float) -> RGB:
    return mix(color, (0, 0, 0), ratio)


def relative_luminance(color: str | RGB) -> float:
    """sRGB 相对亮度，用于自动选择强调色上的文字颜色。"""
    r, g, b = (channel / 255 for channel in hex_to_rgb(color))

    def _linear(channel: float) -> float:
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4

    return 0.2126 * _linear(r) + 0.7152 * _linear(g) + 0.0722 * _linear(b)


def contrast_ratio(a: str | RGB, b: str | RGB) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def readable_ink(background: str | RGB, dark: str | RGB = "#0B0F16", light: str | RGB = "#FFFFFF") -> RGB:
    """在给定背景上返回对比度更高的一种文字色（无障碍可读性）。"""
    return hex_to_rgb(dark) if contrast_ratio(background, dark) >= contrast_ratio(background, light) else hex_to_rgb(light)


def ensure_contrast(
    ink: str | RGB,
    background: str | RGB,
    minimum: float = 4.5,
    steps: int = 14,
) -> RGB:
    """必要时把文字色向亮或暗推移，直到与背景达到最低对比度。"""
    base = hex_to_rgb(ink)
    if contrast_ratio(base, background) >= minimum:
        return base
    target = (255, 255, 255) if relative_luminance(background) < 0.45 else (0, 0, 0)
    best = base
    for step in range(1, steps + 1):
        candidate = mix(base, target, step / steps)
        best = candidate
        if contrast_ratio(candidate, background) >= minimum:
            return candidate
    return best


# ============================ 平台强调色 ============================

PLATFORM_ACCENTS: dict[str, str] = {
    "bilibili": "#FB7299",
    "douyin": "#22E6DD",
    "kuaishou": "#FF7E00",
    "weibo": "#FF8200",
    "xiaohongshu": "#FF2442",
    "twitter": "#4FA6F0",
    "x": "#4FA6F0",
    "pixiv": "#31A6F2",
    "xianyu": "#FFC300",
    "toutiao": "#F04142",
    "xiaoheihe": "#4E7EF2",
    "tiktok": "#22E6DD",
    "acfun": "#FD4C5D",
    "nga": "#66C0F4",
    "youtube": "#FF4E45",
    "website": "#7C8CF8",
    "default": "#7C8CF8",
}


def platform_accent(name: str | None) -> RGB:
    key = str(name or "").strip().lower()
    return hex_to_rgb(PLATFORM_ACCENTS.get(key, PLATFORM_ACCENTS["default"]))


# ============================ 调色板 ============================


@dataclass(frozen=True, slots=True)
class Palette:
    """一套主题在某个模式（深/浅）下的完整颜色令牌。"""

    mode: str                      # "dark" / "light"
    backdrop_a: RGB                # 背景渐变起点
    backdrop_b: RGB                # 背景渐变终点
    bloom: RGB                     # 背景光斑颜色
    bloom_alpha: int               # 背景光斑强度
    surface: RGB                   # 内容面板底色
    surface_alpha: int             # 内容面板不透明度（0=不画面板）
    surface_border: RGB            # 面板描边
    surface_border_alpha: int
    ink: RGB                       # 主文字
    ink_dim: RGB                   # 次要文字
    ink_muted: RGB                 # 弱化文字 / 标签
    hairline: RGB                  # 分隔线
    hairline_alpha: int
    media_mat: RGB                 # 媒体衬底（画框 / 卡纸）
    media_edge: RGB                # 媒体描边
    media_edge_alpha: int
    grain_alpha: int               # 颗粒噪点强度
    shadow_alpha: int              # 外阴影强度
    accent_ink: RGB = (255, 255, 255)   # 强调色上的文字色（运行时按对比度校正）
    accent_wash: int = 34               # 强调色淡背景的 alpha

    def with_accent(self, accent: RGB) -> "Palette":
        """按平台强调色校正"强调色上的文字色"，保证可读。"""
        return replace(self, accent_ink=readable_ink(accent))

    @property
    def is_dark(self) -> bool:
        return self.mode == "dark"


def _palette(mode: str, **kwargs) -> Palette:
    return Palette(mode=mode, **kwargs)


# --- 极光 aurora：深空网格渐变 + 玻璃面板（默认） ---

AURORA_DARK = _palette(
    "dark",
    backdrop_a=hex_to_rgb("#161C2B"),
    backdrop_b=hex_to_rgb("#0A0D14"),
    bloom=hex_to_rgb("#5B76F7"),
    bloom_alpha=54,
    surface=hex_to_rgb("#FFFFFF"),
    surface_alpha=13,
    surface_border=hex_to_rgb("#FFFFFF"),
    surface_border_alpha=26,
    ink=hex_to_rgb("#F4F7FD"),
    ink_dim=hex_to_rgb("#B4BED2"),
    ink_muted=hex_to_rgb("#7C879C"),
    hairline=hex_to_rgb("#FFFFFF"),
    hairline_alpha=28,
    media_mat=hex_to_rgb("#0D111A"),
    media_edge=hex_to_rgb("#FFFFFF"),
    media_edge_alpha=30,
    grain_alpha=7,
    shadow_alpha=120,
)

AURORA_LIGHT = _palette(
    "light",
    backdrop_a=hex_to_rgb("#FFFFFF"),
    backdrop_b=hex_to_rgb("#E9EEF8"),
    bloom=hex_to_rgb("#7C8CF8"),
    bloom_alpha=40,
    surface=hex_to_rgb("#FFFFFF"),
    surface_alpha=200,
    surface_border=hex_to_rgb("#101725"),
    surface_border_alpha=18,
    ink=hex_to_rgb("#141A26"),
    ink_dim=hex_to_rgb("#4E586E"),
    ink_muted=hex_to_rgb("#828C9F"),
    hairline=hex_to_rgb("#101725"),
    hairline_alpha=24,
    media_mat=hex_to_rgb("#DDE4F0"),
    media_edge=hex_to_rgb("#101725"),
    media_edge_alpha=22,
    grain_alpha=5,
    shadow_alpha=56,
)

# --- 报章 broadsheet：象牙纸 + 细规则线 ---

BROADSHEET_LIGHT = _palette(
    "light",
    backdrop_a=hex_to_rgb("#FBF7EF"),
    backdrop_b=hex_to_rgb("#F1EADC"),
    bloom=hex_to_rgb("#C8B79A"),
    bloom_alpha=26,
    surface=hex_to_rgb("#FFFDF8"),
    surface_alpha=0,
    surface_border=hex_to_rgb("#221F1A"),
    surface_border_alpha=30,
    ink=hex_to_rgb("#1B1915"),
    ink_dim=hex_to_rgb("#4A443A"),
    ink_muted=hex_to_rgb("#857C6C"),
    hairline=hex_to_rgb("#1B1915"),
    hairline_alpha=42,
    media_mat=hex_to_rgb("#E7E0D0"),
    media_edge=hex_to_rgb("#1B1915"),
    media_edge_alpha=52,
    grain_alpha=10,
    shadow_alpha=48,
)

BROADSHEET_DARK = _palette(
    "dark",
    backdrop_a=hex_to_rgb("#22201C"),
    backdrop_b=hex_to_rgb("#151412"),
    bloom=hex_to_rgb("#C8A96A"),
    bloom_alpha=30,
    surface=hex_to_rgb("#FFF6E4"),
    surface_alpha=0,
    surface_border=hex_to_rgb("#F3E9D6"),
    surface_border_alpha=32,
    ink=hex_to_rgb("#F6EFE2"),
    ink_dim=hex_to_rgb("#C3B7A2"),
    ink_muted=hex_to_rgb("#8D8474"),
    hairline=hex_to_rgb("#F3E9D6"),
    hairline_alpha=40,
    media_mat=hex_to_rgb("#1D1B18"),
    media_edge=hex_to_rgb("#F3E9D6"),
    media_edge_alpha=44,
    grain_alpha=11,
    shadow_alpha=110,
)

# --- 遥测 telemetry：石墨底 + 测量网格 ---

TELEMETRY_DARK = _palette(
    "dark",
    backdrop_a=hex_to_rgb("#12161C"),
    backdrop_b=hex_to_rgb("#080A0E"),
    bloom=hex_to_rgb("#2FD2C8"),
    bloom_alpha=30,
    surface=hex_to_rgb("#9FB4C4"),
    surface_alpha=16,
    surface_border=hex_to_rgb("#9FB4C4"),
    surface_border_alpha=40,
    ink=hex_to_rgb("#EAF1F6"),
    ink_dim=hex_to_rgb("#9FB0BE"),
    ink_muted=hex_to_rgb("#6B7A87"),
    hairline=hex_to_rgb("#9FB4C4"),
    hairline_alpha=46,
    media_mat=hex_to_rgb("#0B0E13"),
    media_edge=hex_to_rgb("#9FB4C4"),
    media_edge_alpha=58,
    grain_alpha=6,
    shadow_alpha=126,
)

TELEMETRY_LIGHT = _palette(
    "light",
    backdrop_a=hex_to_rgb("#F7F9FB"),
    backdrop_b=hex_to_rgb("#E8EDF2"),
    bloom=hex_to_rgb("#1FA9A0"),
    bloom_alpha=22,
    surface=hex_to_rgb("#FFFFFF"),
    surface_alpha=190,
    surface_border=hex_to_rgb("#28323C"),
    surface_border_alpha=32,
    ink=hex_to_rgb("#101820"),
    ink_dim=hex_to_rgb("#465462"),
    ink_muted=hex_to_rgb("#7B8895"),
    hairline=hex_to_rgb("#28323C"),
    hairline_alpha=34,
    media_mat=hex_to_rgb("#DFE6EC"),
    media_edge=hex_to_rgb("#28323C"),
    media_edge_alpha=40,
    grain_alpha=4,
    shadow_alpha=52,
)

# --- 展厅 gallery：美术馆墙面 + 卡纸装裱 ---

GALLERY_LIGHT = _palette(
    "light",
    backdrop_a=hex_to_rgb("#F4F2EE"),
    backdrop_b=hex_to_rgb("#E4E0D8"),
    bloom=hex_to_rgb("#FFFFFF"),
    bloom_alpha=64,
    surface=hex_to_rgb("#FCFBF8"),
    surface_alpha=0,
    surface_border=hex_to_rgb("#23211D"),
    surface_border_alpha=20,
    ink=hex_to_rgb("#1F1D1A"),
    ink_dim=hex_to_rgb("#4F4B44"),
    ink_muted=hex_to_rgb("#8A857B"),
    hairline=hex_to_rgb("#23211D"),
    hairline_alpha=30,
    media_mat=hex_to_rgb("#FBFAF7"),
    media_edge=hex_to_rgb("#23211D"),
    media_edge_alpha=46,
    grain_alpha=8,
    shadow_alpha=60,
)

# 深色展厅 = 暖调近黑的展墙 + 略亮一档的展板；卡纸装裱比展板再亮一点，
# 形成"墙 < 板 < 卡纸"三层次。展板必须比 ink 暗，否则字会糊在卡纸色上。
GALLERY_DARK = _palette(
    "dark",
    backdrop_a=hex_to_rgb("#15130F"),
    backdrop_b=hex_to_rgb("#0B0A09"),
    bloom=hex_to_rgb("#C9B489"),
    bloom_alpha=30,
    surface=hex_to_rgb("#201E19"),
    surface_alpha=0,
    surface_border=hex_to_rgb("#EDE7DA"),
    surface_border_alpha=30,
    ink=hex_to_rgb("#F4F1E8"),
    ink_dim=hex_to_rgb("#C6BEAD"),
    ink_muted=hex_to_rgb("#938C7E"),
    hairline=hex_to_rgb("#EDE7DA"),
    hairline_alpha=28,
    media_mat=hex_to_rgb("#2C2924"),
    media_edge=hex_to_rgb("#EDE7DA"),
    media_edge_alpha=38,
    grain_alpha=9,
    shadow_alpha=132,
)

# --- 夜曲 nocturne：午夜双色 + 单点晕光 ---

NOCTURNE_DARK = _palette(
    "dark",
    backdrop_a=hex_to_rgb("#1A1140"),
    backdrop_b=hex_to_rgb("#07060F"),
    bloom=hex_to_rgb("#B14BF0"),
    bloom_alpha=76,
    surface=hex_to_rgb("#C9C4FF"),
    surface_alpha=18,
    surface_border=hex_to_rgb("#D6D2FF"),
    surface_border_alpha=34,
    ink=hex_to_rgb("#F3F0FF"),
    ink_dim=hex_to_rgb("#B9B2DE"),
    ink_muted=hex_to_rgb("#8079AE"),
    hairline=hex_to_rgb("#D6D2FF"),
    hairline_alpha=32,
    media_mat=hex_to_rgb("#0B0918"),
    media_edge=hex_to_rgb("#D6D2FF"),
    media_edge_alpha=40,
    grain_alpha=8,
    shadow_alpha=132,
)

NOCTURNE_LIGHT = _palette(
    "light",
    backdrop_a=hex_to_rgb("#F6F3FF"),
    backdrop_b=hex_to_rgb("#E5DEFA"),
    bloom=hex_to_rgb("#8B5CF6"),
    bloom_alpha=44,
    surface=hex_to_rgb("#FFFFFF"),
    surface_alpha=205,
    surface_border=hex_to_rgb("#2A2145"),
    surface_border_alpha=20,
    ink=hex_to_rgb("#191333"),
    ink_dim=hex_to_rgb("#514874"),
    ink_muted=hex_to_rgb("#8A83A6"),
    hairline=hex_to_rgb("#2A2145"),
    hairline_alpha=24,
    media_mat=hex_to_rgb("#E3DCF7"),
    media_edge=hex_to_rgb("#2A2145"),
    media_edge_alpha=22,
    grain_alpha=5,
    shadow_alpha=58,
)

# --- 哔哩哔哩 bilibili：仿 B 站站点视觉（浅色为主，深色为官方暗色模式） ---
# 取色自 B 站设计规范：粉 #FB7299 / 蓝 #00AEEC / 文字 #18191C-#61666D-#9499A0
# 浅色页底 #F1F2F3、卡片纯白、分割线 #E3E5E7；深色页底 #17181A、卡片 #1F2022、分割线 #2F3134

BILIBILI_LIGHT = _palette(
    "light",
    backdrop_a=hex_to_rgb("#F1F2F3"),
    backdrop_b=hex_to_rgb("#E9EAEC"),
    bloom=hex_to_rgb("#FB7299"),
    bloom_alpha=0,
    surface=hex_to_rgb("#FFFFFF"),
    surface_alpha=255,
    surface_border=hex_to_rgb("#E3E5E7"),
    surface_border_alpha=255,
    ink=hex_to_rgb("#18191C"),
    ink_dim=hex_to_rgb("#61666D"),
    ink_muted=hex_to_rgb("#9499A0"),
    hairline=hex_to_rgb("#E3E5E7"),
    hairline_alpha=255,
    media_mat=hex_to_rgb("#F1F2F3"),
    media_edge=hex_to_rgb("#E3E5E7"),
    media_edge_alpha=255,
    grain_alpha=0,
    shadow_alpha=30,
    accent_ink=(255, 255, 255),
    accent_wash=26,
)

BILIBILI_DARK = _palette(
    "dark",
    backdrop_a=hex_to_rgb("#17181A"),
    backdrop_b=hex_to_rgb("#131416"),
    bloom=hex_to_rgb("#FB7299"),
    bloom_alpha=0,
    surface=hex_to_rgb("#2F3134"),
    surface_alpha=255,
    surface_border=hex_to_rgb("#3B3E43"),
    surface_border_alpha=255,
    ink=hex_to_rgb("#E3E5E7"),
    ink_dim=hex_to_rgb("#A2A7AE"),
    ink_muted=hex_to_rgb("#787D85"),
    hairline=hex_to_rgb("#3B3E43"),
    hairline_alpha=255,
    media_mat=hex_to_rgb("#1B1C1F"),
    media_edge=hex_to_rgb("#3B3E43"),
    media_edge_alpha=255,
    grain_alpha=0,
    shadow_alpha=96,
    accent_ink=(255, 255, 255),
    accent_wash=30,
)

# 哔哩哔哩品牌辅助色：页脚链接 / 次级强调
BILIBILI_BLUE = hex_to_rgb("#00AEEC")
BILIBILI_PINK = hex_to_rgb("#FB7299")


PALETTES: dict[str, dict[str, Palette]] = {
    "aurora": {"dark": AURORA_DARK, "light": AURORA_LIGHT},
    "broadsheet": {"dark": BROADSHEET_DARK, "light": BROADSHEET_LIGHT},
    "telemetry": {"dark": TELEMETRY_DARK, "light": TELEMETRY_LIGHT},
    "gallery": {"dark": GALLERY_DARK, "light": GALLERY_LIGHT},
    "nocturne": {"dark": NOCTURNE_DARK, "light": NOCTURNE_LIGHT},
    "bilibili": {"dark": BILIBILI_DARK, "light": BILIBILI_LIGHT},
    # 兼容旧键：任何配置里残留的 stream 都落到哔哩哔哩配色
    "stream": {"dark": BILIBILI_DARK, "light": BILIBILI_LIGHT},
}


def get_palette(theme: str, mode: str) -> Palette:
    modes = PALETTES.get(theme) or PALETTES["aurora"]
    return modes.get(mode) or next(iter(modes.values()))
