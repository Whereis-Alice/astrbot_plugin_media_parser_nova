"""卡片设计系统：绘制原语（背景、材质、装饰、图片处理）。

所有函数都是纯绘制工具，不含业务语义，供 blocks / theme / engine 复用。
"""

from __future__ import annotations

import math
import random
from pathlib import Path
from typing import Any, Sequence

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps
except ImportError:  # pragma: no cover
    Image = ImageChops = ImageDraw = ImageFilter = ImageOps = None  # type: ignore[assignment]

try:
    LANCZOS = Image.Resampling.LANCZOS
except AttributeError:  # Pillow < 9.1  # pragma: no cover
    LANCZOS = Image.LANCZOS  # type: ignore[attr-defined]

RGB = tuple[int, int, int]
RGBA = tuple[int, int, int, int]


# ============================ 背景 ============================


def linear_gradient(size: tuple[int, int], start: RGB, end: RGB, angle: int = 90) -> Any:
    """线性渐变。angle=90 为竖向（上->下），0 为横向（左->右），135 为对角。"""
    w, h = max(1, size[0]), max(1, size[1])
    if angle % 180 == 0:
        strip = Image.new("RGB", (w, 1))
        px = strip.load()
        for x in range(w):
            t = x / max(1, w - 1)
            px[x, 0] = (
                round(start[0] + (end[0] - start[0]) * t),
                round(start[1] + (end[1] - start[1]) * t),
                round(start[2] + (end[2] - start[2]) * t),
            )
        return strip.resize((w, h), LANCZOS)
    if angle % 180 == 90:
        strip = Image.new("RGB", (1, h))
        px = strip.load()
        for y in range(h):
            t = y / max(1, h - 1)
            px[0, y] = (
                round(start[0] + (end[0] - start[0]) * t),
                round(start[1] + (end[1] - start[1]) * t),
                round(start[2] + (end[2] - start[2]) * t),
            )
        return strip.resize((w, h), LANCZOS)
    # 对角渐变：小图上逐点计算再放大，成本可控
    small_w, small_h = 64, max(1, int(64 * h / max(1, w)))
    small = Image.new("RGB", (small_w, small_h))
    px = small.load()
    rad = math.radians(angle)
    dx, dy = math.cos(rad), math.sin(rad)
    norm = abs(dx) + abs(dy)
    for y in range(small_h):
        for x in range(small_w):
            t = ((x / max(1, small_w - 1)) * dx + (y / max(1, small_h - 1)) * dy) / norm
            t = min(1.0, max(0.0, t if norm else 0.0))
            px[x, y] = (
                round(start[0] + (end[0] - start[0]) * t),
                round(start[1] + (end[1] - start[1]) * t),
                round(start[2] + (end[2] - start[2]) * t),
            )
    return small.resize((w, h), LANCZOS)


def bloom(
    target: Any,
    spots: Sequence[tuple[float, float, float, RGB, int]],
    *,
    blur: int = 0,
) -> None:
    """在图上叠加若干柔和光斑。spots 为 (cx比例, cy比例, 半径比例, 颜色, alpha)。"""
    if not spots:
        return
    w, h = target.size
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for cx, cy, radius, color, a in spots:
        if a <= 0:
            continue
        r = max(8, int(radius * max(w, h)))
        # 用小尺寸径向渐变再放大，代价远低于逐像素
        steps = 26
        patch = Image.new("RGBA", (steps * 2, steps * 2), (0, 0, 0, 0))
        pd = ImageDraw.Draw(patch)
        for i in range(steps, 0, -1):
            t = i / steps
            alpha_i = int(a * ((1.0 - t) ** 1.9))
            if alpha_i <= 0:
                continue
            pd.ellipse(
                (steps - i, steps - i, steps + i, steps + i),
                fill=(color[0], color[1], color[2], alpha_i),
            )
        patch = patch.resize((r * 2, r * 2), LANCZOS)
        layer.alpha_composite(patch, (int(cx * w) - r, int(cy * h) - r))
    if blur:
        layer = layer.filter(ImageFilter.GaussianBlur(blur))
    target.alpha_composite(layer)


def grain(target: Any, strength: int, *, seed: int = 7, coarse: int = 2) -> None:
    """胶片颗粒噪点：低成本做法是生成小噪点图再放大。"""
    if strength <= 0:
        return
    w, h = target.size
    coarse = max(1, coarse)
    nw, nh = max(1, w // coarse), max(1, h // coarse)
    noise = Image.new("L", (nw, nh))
    rnd = random.Random(seed)
    noise.putdata([rnd.randrange(0, 256) for _ in range(nw * nh)])
    if coarse > 1:
        noise = noise.resize((w, h), Image.NEAREST)
    layer = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    layer.putalpha(noise.point(lambda v: int(v * strength / 255)))
    target.alpha_composite(layer)


def paper_fiber(target: Any, strength: int, *, seed: int = 11) -> None:
    """纸纤维：横向细密短线，模拟新闻纸/卡纸质感。"""
    if strength <= 0:
        return
    w, h = target.size
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    rnd = random.Random(seed)
    for _ in range(int(w * h / 900)):
        x = rnd.randrange(0, w)
        y = rnd.randrange(0, h)
        length = rnd.randrange(2, 9)
        dark = rnd.random() < 0.5
        tone = (0, 0, 0, strength) if dark else (255, 255, 255, strength)
        d.line((x, y, x + length, y), fill=tone, width=1)
    target.alpha_composite(layer)


def measure_grid(
    target: Any,
    color: RGB,
    alpha: int,
    step: int,
    *,
    major_every: int = 5,
    major_alpha: int | None = None,
) -> None:
    """测量网格：细线 + 每 N 根一条稍亮的主线（telemetry 主题）。"""
    if alpha <= 0 or step <= 1:
        return
    w, h = target.size
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    strong = major_alpha if major_alpha is not None else min(255, alpha * 2)
    index = 0
    x = 0
    while x < w:
        a = strong if (index % major_every == 0) else alpha
        d.line((x, 0, x, h), fill=(color[0], color[1], color[2], a), width=1)
        x += step
        index += 1
    index = 0
    y = 0
    while y < h:
        a = strong if (index % major_every == 0) else alpha
        d.line((0, y, w, y), fill=(color[0], color[1], color[2], a), width=1)
        y += step
        index += 1
    target.alpha_composite(layer)


def halftone(
    target: Any,
    color: RGB,
    alpha: int,
    spacing: int,
    dot: int,
    *,
    box: tuple[int, int, int, int] | None = None,
) -> None:
    """半调网点：报章 / 海报的印刷感。"""
    if alpha <= 0 or spacing <= 1:
        return
    w, h = target.size
    x0, y0, x1, y1 = box or (0, 0, w, h)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    r = max(1, dot)
    row = 0
    y = y0
    while y < y1:
        offset = (spacing // 2) if row % 2 else 0
        x = x0 + offset
        while x < x1:
            d.ellipse((x, y, x + r, y + r), fill=(color[0], color[1], color[2], alpha))
            x += spacing
        y += spacing
        row += 1
    target.alpha_composite(layer)


def scanlines(target: Any, color: RGB, alpha: int, spacing: int = 3) -> None:
    """扫描线：夜曲 / 信号主题的极细横纹。"""
    if alpha <= 0:
        return
    w, h = target.size
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for y in range(0, h, max(2, spacing)):
        d.line((0, y, w, y), fill=(color[0], color[1], color[2], alpha), width=1)
    target.alpha_composite(layer)


def vignette(target: Any, alpha: int) -> None:
    """四角暗角，把视觉焦点收回卡片中心。"""
    if alpha <= 0:
        return
    w, h = target.size
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    steps = 18
    for i in range(steps):
        t = i / steps
        inset = int(-min(w, h) * 0.28 * (1 - t))
        d.rectangle((inset, inset, w - inset, h - inset), outline=int(alpha * (t ** 2.2)), width=max(1, int(min(w, h) * 0.02)))
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    layer.putalpha(mask.filter(ImageFilter.GaussianBlur(max(6, min(w, h) // 22))))
    dark = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    dark.putalpha(layer.getchannel("A"))
    target.alpha_composite(dark)


# ============================ 面板 / 形状 ============================


def rounded_mask(size: tuple[int, int], radius: int, *, corners: tuple[bool, bool, bool, bool] = (True, True, True, True)) -> Any:
    """生成圆角遮罩，corners 顺序为 (左上, 右上, 右下, 左下)。"""
    w, h = max(1, size[0]), max(1, size[1])
    radius = max(0, min(radius, min(w, h) // 2))
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    if radius <= 0 or all(corners):
        d.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
        return mask
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    tl, tr, br, bl = corners
    if not tl:
        d.rectangle((0, 0, radius, radius), fill=255)
    if not tr:
        d.rectangle((w - 1 - radius, 0, w - 1, radius), fill=255)
    if not br:
        d.rectangle((w - 1 - radius, h - 1 - radius, w - 1, h - 1), fill=255)
    if not bl:
        d.rectangle((0, h - 1 - radius, radius, h - 1), fill=255)
    return mask


def round_image(image: Any, radius: int, *, corners: tuple[bool, bool, bool, bool] = (True, True, True, True)) -> Any:
    """给图片切圆角。已有的半透明通道会与圆角遮罩相乘，而不是被整体覆盖。"""
    img = image.convert("RGBA")
    mask = rounded_mask(img.size, radius, corners=corners)
    existing = img.getchannel("A")
    if existing.getextrema()[0] < 255:
        mask = ImageChops.multiply(existing, mask)
    img.putalpha(mask)
    return img


def drop_shadow(
    size: tuple[int, int],
    box: tuple[int, int, int, int],
    radius: int,
    blur: int,
    alpha: int,
    *,
    offset: tuple[int, int] = (0, 4),
    color: RGB = (0, 0, 0),
) -> Any:
    """返回一张只含阴影的 RGBA 层。"""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    if alpha <= 0:
        return layer
    d = ImageDraw.Draw(layer)
    x0, y0, x1, y1 = box
    d.rounded_rectangle(
        (x0 + offset[0], y0 + offset[1], x1 + offset[0], y1 + offset[1]),
        radius=radius,
        fill=(color[0], color[1], color[2], max(0, min(255, alpha))),
    )
    return layer.filter(ImageFilter.GaussianBlur(max(1, blur)))


def _composite_clipped(target: Any, patch: Any, x: int, y: int) -> None:
    """把小图层合成到目标图上，自动裁掉越界部分。"""
    tw, th = target.size
    px, py = 0, 0
    if x < 0:
        px, x = -x, 0
    if y < 0:
        py, y = -y, 0
    cw = min(patch.width - px, tw - x)
    ch = min(patch.height - py, th - y)
    if cw <= 0 or ch <= 0:
        return
    if (px, py, cw, ch) != (0, 0, patch.width, patch.height):
        patch = patch.crop((px, py, px + cw, py + ch))
    target.alpha_composite(patch, (x, y))


def panel(
    target: Any,
    box: tuple[int, int, int, int],
    radius: int,
    *,
    fill: RGBA | None = None,
    border: RGBA | None = None,
    border_width: int = 1,
    shadow_alpha: int = 0,
    shadow_blur: int = 12,
    shadow_offset: tuple[int, int] = (0, 6),
    blur_backdrop: int = 0,
) -> None:
    """在目标图上绘制（可选毛玻璃的）面板。"""
    x0, y0, x1, y1 = (int(v) for v in box)
    if x1 <= x0 or y1 <= y0:
        return
    # 圆角上限为最短边的一半：让调用方可以放心传 999 表示"胶囊形"
    radius = max(0, min(int(radius), min(x1 - x0, y1 - y0) // 2))
    if shadow_alpha > 0:
        target.alpha_composite(
            drop_shadow(target.size, (x0, y0, x1, y1), radius, shadow_blur, shadow_alpha, offset=shadow_offset)
        )
    if blur_backdrop > 0:
        region = target.crop((x0, y0, x1, y1)).filter(ImageFilter.GaussianBlur(blur_backdrop))
        region.putalpha(rounded_mask(region.size, radius))
        target.alpha_composite(region, (x0, y0))
    if fill or border:
        # 圆角与描边在 4 倍尺寸上画完再缩回来。1 倍直接画 rounded_rectangle 会
        # 在每一个圆角与每一条描边上留下阶梯状锯齿，卡片上到处都是圆角面板，
        # 这层锯齿正是整体"发糙、不够精致"的来源。顺带只申请面板大小的画布，
        # 不再每次都开一张整卡尺寸的图层。
        pw, ph = x1 - x0 + 1, y1 - y0 + 1
        scale = 4 if radius > 0 else 1
        if pw * ph * scale * scale > 24_000_000:
            scale = 1
        patch = Image.new("RGBA", (pw * scale, ph * scale), (0, 0, 0, 0))
        ImageDraw.Draw(patch).rounded_rectangle(
            (0, 0, pw * scale - 1, ph * scale - 1),
            radius=radius * scale,
            fill=fill,
            outline=border,
            width=max(1, border_width * scale) if border else 0,
        )
        if scale > 1:
            patch = patch.resize((pw, ph), LANCZOS)
        _composite_clipped(target, patch, x0, y0)


def hairline(target: Any, x0: int, y: int, x1: int, color: RGBA, *, width: int = 1, dash: int = 0) -> None:
    """发丝分隔线，支持虚线。"""
    layer = Image.new("RGBA", target.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if dash <= 0:
        d.line((x0, y, x1, y), fill=color, width=width)
    else:
        x = x0
        while x < x1:
            d.line((x, y, min(x1, x + dash), y), fill=color, width=width)
            x += dash * 2
    target.alpha_composite(layer)


def vline(target: Any, x: int, y0: int, y1: int, color: RGBA, *, width: int = 1) -> None:
    layer = Image.new("RGBA", target.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).line((x, y0, x, y1), fill=color, width=width)
    target.alpha_composite(layer)


def corner_marks(
    target: Any,
    box: tuple[int, int, int, int],
    color: RGBA,
    length: int,
    *,
    width: int = 1,
) -> None:
    """四角角标（telemetry / gallery 用的技术感边框）。"""
    x0, y0, x1, y1 = box
    layer = Image.new("RGBA", target.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for (cx, cy, sx, sy) in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
        d.line((cx, cy, cx + length * sx, cy), fill=color, width=width)
        d.line((cx, cy, cx, cy + length * sy), fill=color, width=width)
    target.alpha_composite(layer)


def scrim(
    size: tuple[int, int],
    color: RGB,
    *,
    top_alpha: int = 0,
    bottom_alpha: int = 220,
    curve: float = 1.7,
) -> Any:
    """竖向遮罩层，用于沉浸式 hero 上压字。"""
    w, h = max(1, size[0]), max(1, size[1])
    mask = Image.new("L", (1, h))
    px = mask.load()
    for y in range(h):
        t = y / max(1, h - 1)
        value = top_alpha + (bottom_alpha - top_alpha) * (t ** curve)
        px[0, y] = max(0, min(255, int(value)))
    layer = Image.new("RGBA", (w, h), (color[0], color[1], color[2], 255))
    layer.putalpha(mask.resize((w, h)))
    return layer


def glow_ring(target: Any, center: tuple[int, int], radius: int, color: RGB, alpha: int, *, width: int = 2) -> None:
    """柔光圆环（nocturne 的单点 bloom 装饰）。"""
    if alpha <= 0:
        return
    layer = Image.new("RGBA", target.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = center
    d.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        outline=(color[0], color[1], color[2], alpha),
        width=max(1, width),
    )
    target.alpha_composite(layer.filter(ImageFilter.GaussianBlur(max(2, radius // 12))))


# ============================ 图片处理 ============================


def heart(target: Any, box: tuple[int, int, int, int], color: RGBA) -> None:
    """在 box 内绘制一颗实心心形。

    点赞图标不依赖 emoji 字体，避免宿主缺字时渲染成方块（豆腐块）。
    先以 4 倍尺寸绘制再缩小，得到平滑边缘。
    """
    x0, y0, x1, y1 = (int(v) for v in box)
    w, h = x1 - x0, y1 - y0
    if w <= 1 or h <= 1:
        return
    scale = 4
    bw, bh = w * scale, h * scale
    big = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    r = bw / 4.0
    lobe = bh * 0.62
    d.ellipse((0, 0, 2 * r, lobe), fill=color)
    d.ellipse((bw - 2 * r, 0, bw, lobe), fill=color)
    d.polygon([(0, lobe * 0.45), (bw, lobe * 0.45), (bw / 2.0, bh)], fill=color)
    target.alpha_composite(big.resize((w, h), LANCZOS), (x0, y0))


def _star_points(cx: float, cy: float, outer: float, inner: float) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for index in range(10):
        radius = outer if index % 2 == 0 else inner
        angle = -math.pi / 2 + index * math.pi / 5
        points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return points


def glyph(target: Any, box: tuple[int, int, int, int], kind: str, color: RGBA) -> None:
    """绘制一枚极简线性图标，用于统计行 / 封面浮层。

    kind: play / danmaku / like / heart / dislike / coin / star / share / repost /
          export / bookmark / comment / eye / clock / back / more / sort / check /
          bolt / person / dot
    全部为几何绘制（超采样 3x），不依赖 emoji 字体。
    """
    x0, y0, x1, y1 = (int(v) for v in box)
    w, h = x1 - x0, y1 - y0
    if w <= 3 or h <= 3:
        return
    s = 3
    big_w, big_h = w * s, h * s
    big = Image.new("RGBA", (big_w, big_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    line = max(2, int(round(min(big_w, big_h) * 0.11)))

    def px(fx: float, fy: float) -> tuple[int, int]:
        return (int(round(big_w * fx)), int(round(big_h * fy)))

    if kind == "play":
        d.rounded_rectangle(
            (line // 2, int(big_h * 0.12), big_w - 1 - line // 2, int(big_h * 0.88)),
            radius=int(big_w * 0.22),
            outline=color,
            width=line,
        )
        d.polygon([px(0.40, 0.30), px(0.40, 0.70), px(0.70, 0.50)], fill=color)
    elif kind == "danmaku":
        d.rounded_rectangle(
            (line // 2, int(big_h * 0.14), big_w - 1 - line // 2, int(big_h * 0.72)),
            radius=int(big_w * 0.18),
            outline=color,
            width=line,
        )
        d.polygon([px(0.26, 0.72), px(0.50, 0.72), px(0.30, 0.94)], fill=color)
        for fy in (0.34, 0.52):
            d.line([px(0.26, fy), px(0.74, fy)], fill=color, width=line)
    elif kind == "comment":
        d.rounded_rectangle(
            (line // 2, int(big_h * 0.14), big_w - 1 - line // 2, int(big_h * 0.74)),
            radius=int(big_w * 0.26),
            outline=color,
            width=line,
        )
        d.polygon([px(0.28, 0.74), px(0.52, 0.74), px(0.32, 0.96)], fill=color)
    elif kind == "like":
        d.polygon(
            [
                px(0.34, 0.98),
                px(0.34, 0.44),
                px(0.52, 0.44),
                px(0.60, 0.06),
                px(0.74, 0.06),
                px(0.78, 0.18),
                px(0.70, 0.40),
                px(0.96, 0.40),
                px(1.00, 0.50),
                px(0.90, 0.98),
            ],
            fill=color,
        )
        d.rounded_rectangle(
            (0, int(big_h * 0.46), int(big_w * 0.24), big_h - 1),
            radius=int(big_w * 0.06),
            fill=color,
        )
    elif kind == "heart":
        # X「喜欢」：实心心形（两个圆瓣 + 下收的三角）。like 是拇指，两者不能混用，
        # X 的操作栏与回复迷你行都要心形，否则一眼就出戏。
        d.ellipse((int(big_w * 0.02), int(big_h * 0.08), int(big_w * 0.52), int(big_h * 0.60)), fill=color)
        d.ellipse((int(big_w * 0.48), int(big_h * 0.08), int(big_w * 0.98), int(big_h * 0.60)), fill=color)
        d.polygon([px(0.04, 0.40), px(0.96, 0.40), px(0.50, 0.98)], fill=color)
    elif kind == "coin":
        d.ellipse((line // 2, line // 2, big_w - 1 - line // 2, big_h - 1 - line // 2), outline=color, width=line)
        d.line([px(0.50, 0.24), px(0.50, 0.76)], fill=color, width=line)
        d.line([px(0.32, 0.42), px(0.68, 0.42)], fill=color, width=line)
        d.line([px(0.32, 0.58), px(0.68, 0.58)], fill=color, width=line)
    elif kind == "star":
        d.polygon(_star_points(big_w / 2.0, big_h * 0.54, big_w * 0.50, big_w * 0.21), fill=color)
    elif kind == "share":
        # B 站「转发」：一条自左下上扬的曲线尾 + 右侧实心箭头。
        # 早先版本用 arc + 两条斜线拼箭头，小尺寸下箭头会与弧线脱开、看着像坏图，
        # 这里改成折线近似曲线 + 实心三角，15px 也能一眼认出是转发。
        d.line(
            [
                px(0.04, 0.90),
                px(0.11, 0.72),
                px(0.21, 0.59),
                px(0.35, 0.50),
                px(0.50, 0.45),
                px(0.66, 0.43),
            ],
            fill=color,
            width=line,
            joint="curve",
        )
        d.polygon([px(0.62, 0.16), px(0.99, 0.43), px(0.62, 0.70)], fill=color)
    elif kind == "repost":
        # X「转帖」：上下两支反向箭头。原先画成「带拐角的回环 + 三角」，两条折线
        # 的拐角与对面的箭头底边在 20px 下会糊成一个方块，完全认不出是转发；
        # 改成互不相交的双向箭头后，12px 也一眼可辨。share 仍留给 B 站转发。
        d.line([px(0.06, 0.32), px(0.74, 0.32)], fill=color, width=line)
        d.polygon([px(0.70, 0.12), px(0.70, 0.52), px(0.98, 0.32)], fill=color)
        d.line([px(0.94, 0.70), px(0.26, 0.70)], fill=color, width=line)
        d.polygon([px(0.30, 0.50), px(0.30, 0.90), px(0.02, 0.70)], fill=color)
    elif kind == "export":
        # X「分享」：托盘 + 向上飞出的箭头（原版是 ↗ 出框的观感）
        d.line(
            [px(0.14, 0.46), px(0.14, 0.93), px(0.86, 0.93), px(0.86, 0.46)],
            fill=color,
            width=line,
            joint="curve",
        )
        d.line([px(0.50, 0.68), px(0.50, 0.22)], fill=color, width=line)
        d.polygon([px(0.33, 0.32), px(0.67, 0.32), px(0.50, 0.06)], fill=color)
    elif kind == "bookmark":
        # X「书签」：下缘开 V 的书签带
        pts = [px(0.24, 0.08), px(0.76, 0.08), px(0.76, 0.94), px(0.50, 0.70), px(0.24, 0.94)]
        d.line([*pts, pts[0]], fill=color, width=line, joint="curve")
    elif kind == "eye":
        d.ellipse((0, int(big_h * 0.20), big_w - 1, int(big_h * 0.80)), outline=color, width=line)
        d.ellipse((int(big_w * 0.36), int(big_h * 0.38), int(big_w * 0.64), int(big_h * 0.62)), fill=color)
    elif kind == "clock":
        d.ellipse((line // 2, line // 2, big_w - 1 - line // 2, big_h - 1 - line // 2), outline=color, width=line)
        d.line([px(0.50, 0.28), px(0.50, 0.52)], fill=color, width=line)
        d.line([px(0.50, 0.52), px(0.72, 0.60)], fill=color, width=line)
    elif kind == "dislike":
        # 与 like 完全上下镜像，构成 B 站的「踩」图标
        d.polygon(
            [
                px(0.34, 0.02),
                px(0.34, 0.56),
                px(0.52, 0.56),
                px(0.60, 0.94),
                px(0.74, 0.94),
                px(0.78, 0.82),
                px(0.70, 0.60),
                px(0.96, 0.60),
                px(1.00, 0.50),
                px(0.90, 0.02),
            ],
            fill=color,
        )
        d.rounded_rectangle(
            (0, 0, int(big_w * 0.24), int(big_h * 0.54)),
            radius=int(big_w * 0.06),
            fill=color,
        )
    elif kind == "back":
        thick = max(2, int(round(line * 1.15)))
        d.line([px(0.68, 0.10), px(0.34, 0.50)], fill=color, width=thick)
        d.line([px(0.34, 0.50), px(0.68, 0.90)], fill=color, width=thick)
    elif kind == "more":
        dot_r = max(1, int(round(big_w * 0.11)))
        for fy in (0.17, 0.50, 0.83):
            cx, cy = px(0.50, fy)
            d.ellipse((cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r), fill=color)
    elif kind == "sort":
        for fy, fx in ((0.22, 0.92), (0.50, 0.70), (0.78, 0.48)):
            d.line([px(0.08, fy), px(fx, fy)], fill=color, width=line)
    elif kind == "person":
        # 默认头像的人形剪影：头 + 肩，纯几何、任何皮肤下都不会显得跳
        d.ellipse(
            (int(big_w * 0.31), int(big_h * 0.13), int(big_w * 0.69), int(big_h * 0.51)),
            fill=color,
        )
        d.pieslice(
            (int(big_w * 0.13), int(big_h * 0.57), int(big_w * 0.87), int(big_h * 1.32)),
            180,
            360,
            fill=color,
        )
    elif kind == "check":
        # 认证角标里的对勾（X / YouTube 都是勾，不是闪电）
        thick = max(2, int(round(line * 1.35)))
        d.line([px(0.20, 0.52), px(0.42, 0.74)], fill=color, width=thick)
        d.line([px(0.42, 0.74), px(0.80, 0.28)], fill=color, width=thick)
    elif kind == "bolt":
        d.polygon(
            [
                px(0.58, 0.04),
                px(0.20, 0.56),
                px(0.46, 0.56),
                px(0.38, 0.96),
                px(0.80, 0.42),
                px(0.52, 0.42),
            ],
            fill=color,
        )
    else:  # dot
        d.ellipse(
            (int(big_w * 0.30), int(big_h * 0.30), int(big_w * 0.70), int(big_h * 0.70)),
            fill=color,
        )

    target.alpha_composite(big.resize((w, h), LANCZOS), (x0, y0))


def text_shadow_layer(size: tuple[int, int]) -> Any:
    """给浮层文字用的空 RGBA 图层。"""
    return Image.new("RGBA", (max(1, size[0]), max(1, size[1])), (0, 0, 0, 0))


def open_image(path: Path) -> Any:
    img = Image.open(path)
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    return img.convert("RGBA")


def image_aspect(path: Path, default: float = 1.0) -> float:
    """安全读取图片宽高比（宽/高）。"""
    try:
        with Image.open(path) as img:
            w, h = img.size
        return (w / h) if h else default
    except Exception:
        return default


def cover_fit(image: Any, box_w: int, box_h: int) -> Any:
    """等比放大裁切填满目标框（不留黑边、不变形）。"""
    box_w, box_h = max(1, int(box_w)), max(1, int(box_h))
    src = image.convert("RGBA")
    sw, sh = src.size
    if sw <= 0 or sh <= 0:
        return Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    scale = max(box_w / sw, box_h / sh)
    new_size = (max(1, int(math.ceil(sw * scale))), max(1, int(math.ceil(sh * scale))))
    resized = src.resize(new_size, LANCZOS)
    left = max(0, (resized.width - box_w) // 2)
    top = max(0, (resized.height - box_h) // 2)
    return resized.crop((left, top, left + box_w, top + box_h))


def contain_fit(image: Any, box_w: int, box_h: int, background: RGBA) -> Any:
    """等比缩放完整放入目标框，空白处填 background。"""
    box_w, box_h = max(1, int(box_w)), max(1, int(box_h))
    src = image.convert("RGBA")
    sw, sh = src.size
    canvas = Image.new("RGBA", (box_w, box_h), background)
    if sw <= 0 or sh <= 0:
        return canvas
    scale = min(box_w / sw, box_h / sh)
    new_size = (max(1, int(sw * scale)), max(1, int(sh * scale)))
    resized = src.resize(new_size, LANCZOS)
    canvas.alpha_composite(resized, ((box_w - new_size[0]) // 2, (box_h - new_size[1]) // 2))
    return canvas


def blur_backdrop_fit(image: Any, box_w: int, box_h: int, blur: int = 18, dim: int = 60) -> Any:
    """竖图放进横框时的高级做法：模糊放大同图做底，原图完整居中。

    居中图与模糊底之间垫一层柔和投影并收一道极细亮边，
    避免两层直接相接时出现生硬的横向（或纵向）色带接缝。
    """
    box_w, box_h = max(1, int(box_w)), max(1, int(box_h))
    back = cover_fit(image, box_w, box_h).filter(ImageFilter.GaussianBlur(max(1, blur)))
    if dim > 0:
        shade = Image.new("RGBA", (box_w, box_h), (0, 0, 0, max(0, min(255, dim))))
        back.alpha_composite(shade)
    src = image.convert("RGBA")
    sw, sh = src.size
    if sw <= 0 or sh <= 0:
        return back
    scale = min(box_w / sw, box_h / sh)
    new_size = (max(1, int(sw * scale)), max(1, int(sh * scale)))
    resized = src.resize(new_size, LANCZOS)
    off_x, off_y = (box_w - new_size[0]) // 2, (box_h - new_size[1]) // 2
    spread = max(3, int(min(box_w, box_h) * 0.035))
    shadow = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rectangle(
        (
            off_x - spread // 2,
            off_y - spread // 2,
            off_x + new_size[0] + spread // 2,
            off_y + new_size[1] + spread // 2,
        ),
        fill=(0, 0, 0, 148),
    )
    back.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(spread)))
    back.alpha_composite(resized, (off_x, off_y))
    edge = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rectangle(
        (off_x, off_y, off_x + new_size[0] - 1, off_y + new_size[1] - 1),
        outline=(255, 255, 255, 28),
        width=1,
    )
    back.alpha_composite(edge)
    return back


def circle_image(image: Any, size: int) -> Any:
    """裁成圆形头像。

    遮罩在 4 倍尺寸上画好再 LANCZOS 缩回来：直接按 1 倍画椭圆会留下硬锯齿，
    头像越小越明显——那是头像"看着糙"的主因之一。
    """
    size = max(2, int(size))
    src = cover_fit(image, size, size)
    scale = 4
    big = Image.new("L", (size * scale, size * scale), 0)
    ImageDraw.Draw(big).ellipse(
        (0, 0, size * scale - 1, size * scale - 1), fill=255
    )
    src.putalpha(big.resize((size, size), LANCZOS))
    return src


def desaturate(image: Any, amount: float) -> Any:
    """部分去色（0=不变，1=全灰），用于低调装帧。"""
    if amount <= 0:
        return image
    rgba = image.convert("RGBA")
    grey = ImageOps.grayscale(rgba.convert("RGB")).convert("RGBA")
    grey.putalpha(rgba.getchannel("A"))
    return Image.blend(rgba, grey, min(1.0, amount))
