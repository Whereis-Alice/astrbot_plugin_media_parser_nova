"""卡片渲染回归测试：覆盖 nova_core.card 设计系统的主题、布局与排版行为。

约定两条：

* 「某段文字有没有画在卡片上」一律通过 ``RenderContext.texts`` 采集后断言，
  绝不去猜坐标或做像素 OCR；
* 几何断言一律基于真实渲染出的 PIL 图像与真实绘制调用，不依赖硬编码尺寸。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from nova_core.card import LAYOUT_KEYS, THEME_KEYS, THEMES, build_model, normalize_comments
from nova_core.card import blocks as card_blocks
from nova_core.card import engine as card_engine
from nova_core.card.typeset import TypeSetter
from nova_core.rika_render.adapter import _split_author_label
from nova_core.rika_render.data import Author, ParseResult, Platform
from nova_core.rika_render.render import (
    DEFAULT_WATERMARK_TAG,
    LAYOUT_NAMES,
    SKIN_NAMES,
    ShareCardRenderer,
)

#: 深浅两种模式
MODES = ("dark", "light")

#: 主题 x 模式矩阵的渲染宽度（够宽以容纳评论与统计，又不至于太慢）
MATRIX_WIDTH = 640

#: 一条协议 + 查询参数 + 片段都齐全的长链接（150 字符，必须折行而非截断）
FULL_URL = (
    "https://www.bilibili.com/video/BV1Ex421c7mQ/"
    "?vd_source=abcdef0123456789abcdef0123456789"
    "&spm_id_from=333.1007.tianma.1-1-1.click&extra=1#reply987654321"
)


# ============================ 采集工具 ============================


@dataclass
class Probe:
    """一次完整渲染的采集结果：成品图 + 实际绘制的文本 + 实际贴图尺寸。"""

    image: Any
    texts: list[str] = field(default_factory=list)
    tiles: list[tuple[str, int, int]] = field(default_factory=list)
    text_bottoms: list[int] = field(default_factory=list)

    @property
    def joined_text(self) -> str:
        return "\n".join(self.texts)

    @property
    def digest(self) -> str:
        return hashlib.md5(self.image.tobytes()).hexdigest()


def render_probe(
    model: Any,
    *,
    width: int = 800,
    mode: str = "dark",
    theme_key: str = "aurora",
    layout_key: str = "standard",
) -> Probe:
    """走一遍 render_card_image 的完整流程，并顺带采集文本、贴图与文字底边。

    engine.build_context 被临时换成间谍版本，好处是能拿到引擎自己创建的
    RenderContext（沉浸布局下正文用的是第二个 ctx，但两者共享同一个 texts 列表）。
    """
    contexts: list[Any] = []
    tiles: list[tuple[str, int, int]] = []
    bottoms: list[int] = []

    real_build_context = card_engine.build_context
    real_tile_image = card_blocks._tile_image
    real_draw_line = TypeSetter.draw_line

    def spy_build_context(*args: Any, **kwargs: Any) -> Any:
        ctx = real_build_context(*args, **kwargs)
        contexts.append(ctx)
        return ctx

    def spy_tile_image(ctx: Any, item: Any, w: int, h: int, **kwargs: Any) -> Any:
        tiles.append((Path(item.path).name, int(w), int(h)))
        return real_tile_image(ctx, item, w, h, **kwargs)

    def spy_draw_line(self: Any, draw: Any, xy: Any, text: str, font: Any, fill: Any, **kwargs: Any) -> Any:
        if text:
            bottoms.append(int(xy[1]) + self.line_height(font, 1.0))
        return real_draw_line(self, draw, xy, text, font, fill, **kwargs)

    card_engine.build_context = spy_build_context
    card_blocks._tile_image = spy_tile_image
    TypeSetter.draw_line = spy_draw_line
    try:
        image = card_engine.render_card_image(
            model,
            width=width,
            mode=mode,
            theme_key=theme_key,
            layout_key=layout_key,
        )
    finally:
        card_engine.build_context = real_build_context
        card_blocks._tile_image = real_tile_image
        TypeSetter.draw_line = real_draw_line

    return Probe(image=image, texts=list(contexts[0].texts), tiles=tiles, text_bottoms=bottoms)


def png(path: Path, size: tuple[int, int], color: tuple[int, int, int]) -> Path:
    """生成一张纯色测试图并返回路径。"""
    Image.new("RGB", size, color).save(path)
    return path


def make_result(
    *,
    title: str | None = "标题",
    text: str | None = "正文",
    url: str = "https://x.com/i/status/2090756349701554395",
    platform: str = "twitter",
    display_name: str = "推特",
    author: tuple[str, str] | None = ("Pekachow (Amber)", "@Pekachow1"),
    timestamp: int | None = 1_756_000_000,
    extra: dict[str, Any] | None = None,
) -> ParseResult:
    """构造一个用于渲染的 ParseResult。"""
    payload: dict[str, Any] = {"content_type": "图文"}
    payload.update(extra or {})
    return ParseResult(
        platform=Platform(name=platform, display_name=display_name),
        author=Author(name=author[0], description=author[1]) if author else None,
        title=title,
        text=text,
        timestamp=timestamp,
        url=url,
        extra=payload,
    )


# ============================ 素材与矩阵夹具 ============================


@pytest.fixture(scope="module")
def assets(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """一组只生成一次的测试素材（头像、竖图、横图、九宫格图集）。"""
    root = tmp_path_factory.mktemp("card-assets")
    return {
        "avatar": png(root / "avatar.png", (180, 180), (70, 190, 240)),
        "tall": png(root / "tall.png", (900, 1100), (242, 126, 164)),
        "wide": png(root / "wide.png", (1200, 700), (37, 83, 211)),
        "gallery": [
            png(
                root / f"gallery-{index}.png",
                (720 + index * 30, 620 + index * 40),
                (40 + index * 20, 90 + index * 12, 200 - index * 18),
            )
            for index in range(9)
        ],
    }


@pytest.fixture(scope="module")
def rich_model(assets: dict[str, Any]) -> Any:
    """含标题、正文、统计、两条评论与两张图的完整卡片模型。"""
    result = make_result(
        title="A translated title with enough words to verify responsive wrapping",
        text=(
            "This is a body paragraph used to verify layout and text rendering. "
            "It should remain readable without overlapping the author block."
        ),
        extra={
            "stats_line": "👍 128 💬 16 🔁 8",
            "hot_comments": [
                {
                    "username": "A reader with a long public display name",
                    "uid": "reader-10001",
                    "likes": 42,
                    "time": "2026-08-24 12:30:00",
                    "message": "A translated public reply rendered inside the same card.",
                },
                {
                    "username": "Second reader",
                    "uid": "reader-10002",
                    "likes": 9,
                    "time": "2026-08-24 12:31:00",
                    "message": "Another reply verifies the comment panel height.",
                },
            ],
        },
    )
    return build_model(
        result,
        {"avatar": assets["avatar"], "hero": None, "grid": [assets["tall"], assets["wide"]]},
        watermark="Alice解析",
    )


@pytest.fixture(scope="module")
def matrix(rich_model: Any) -> dict[tuple[str, str], Probe]:
    """把 6 主题 x 深浅 2 模式各渲染一次，供多个断言复用（只渲染 12 张）。"""
    return {
        (theme, mode): render_probe(rich_model, width=MATRIX_WIDTH, mode=mode, theme_key=theme)
        for theme in THEME_KEYS
        for mode in MODES
    }


# ============================ 兼容层 API ============================


def test_author_label_splits_name_and_identifier() -> None:
    """作者标签里的显示名与账号标识必须被正确拆开。"""
    assert _split_author_label("Pekachow (Amber) (@Pekachow1)") == (
        "Pekachow (Amber)",
        "@Pekachow1",
    )
    assert _split_author_label("Alice(uid:10001)") == ("Alice", "uid:10001")


def test_legacy_shim_reexports_design_system_enums() -> None:
    """兼容 shim 的枚举必须直接来自设计系统，避免两处漂移。"""
    assert LAYOUT_NAMES == LAYOUT_KEYS == ("standard", "magazine", "immersive", "feed")
    assert SKIN_NAMES == THEME_KEYS
    assert SKIN_NAMES == ("aurora", "broadsheet", "telemetry", "gallery", "nocturne", "bilibili")
    assert DEFAULT_WATERMARK_TAG


# ============================ 主题 x 模式矩阵 ============================


@pytest.mark.parametrize("theme", THEME_KEYS)
@pytest.mark.parametrize("mode", MODES)
def test_theme_and_mode_render_a_non_blank_card(
    matrix: dict[tuple[str, str], Probe], theme: str, mode: str
) -> None:
    """每个主题的深浅两版都要画出内容非空白、宽度等于请求宽度的卡片。"""
    probe = matrix[(theme, mode)]
    image = probe.image
    assert image.mode == "RGB"
    assert image.width == MATRIX_WIDTH
    assert image.height > MATRIX_WIDTH // 2
    assert any(low != high for low, high in image.getextrema()), f"{theme}/{mode} 输出是纯色空白"
    assert probe.texts, f"{theme}/{mode} 没有画出任何文字"


def test_theme_and_mode_matrix_is_pairwise_distinct(matrix: dict[tuple[str, str], Probe]) -> None:
    """6 主题 x 深浅 2 模式共 12 张图必须互不相同（主题与模式都真正生效）。"""
    digests = {key: probe.digest for key, probe in matrix.items()}
    assert len(set(digests.values())) == len(digests), f"存在重复输出: {digests}"


@pytest.mark.parametrize("theme", THEME_KEYS)
@pytest.mark.parametrize("mode", MODES)
def test_no_text_is_clipped_at_the_card_bottom(
    matrix: dict[tuple[str, str], Probe], theme: str, mode: str
) -> None:
    """所有文字的底边都必须落在画布内，且卡片底部要留出合理白边。"""
    probe = matrix[(theme, mode)]
    height = probe.image.height
    assert probe.text_bottoms
    lowest = max(probe.text_bottoms)
    assert lowest < height, f"{theme}/{mode} 底部文字被裁掉"
    assert height - lowest >= 16, f"{theme}/{mode} 底部留白只有 {height - lowest}px"


@pytest.mark.parametrize("width", (520, 800, 1080))
def test_output_width_follows_the_request(rich_model: Any, width: int) -> None:
    """成品图宽度必须严格等于请求宽度。"""
    probe = render_probe(rich_model, width=width, theme_key="aurora", layout_key="standard")
    assert probe.image.width == width


# ============================ 主题 / 布局必须真正生效 ============================


def test_theme_and_layout_changes_produce_different_cards_and_cache_paths(
    assets: dict[str, Any], tmp_path: Path
) -> None:
    """深浅 2 x 主题 6 x 布局 4 = 48 种组合必须张张不同，缓存路径也必须张张不同。

    这条用例是旧断言「高级皮肤忽略主题与布局」的反转：新架构下三者都真正生效。
    """
    result = make_result(title="真实标题", text="正文")
    model = build_model(
        result,
        {"avatar": None, "hero": None, "grid": [assets["wide"]]},
        watermark="Alice解析",
    )

    digests: dict[str, tuple[str, str, str]] = {}
    cache_paths: dict[Path, tuple[str, str, str]] = {}
    for mode in MODES:
        for theme in THEME_KEYS:
            for layout in LAYOUT_KEYS:
                combo = (mode, theme, layout)
                probe = render_probe(
                    model, width=520, mode=mode, theme_key=theme, layout_key=layout
                )
                assert probe.digest not in digests, f"{combo} 与 {digests.get(probe.digest)} 输出相同"
                digests[probe.digest] = combo

                renderer = ShareCardRenderer(
                    cache_dir=tmp_path, skin=theme, theme=mode, layout=layout, width=520
                )
                path = renderer._output_path("same-card", result)
                assert path not in cache_paths, f"{combo} 与 {cache_paths.get(path)} 缓存路径相同"
                cache_paths[path] = combo

    assert len(digests) == len(cache_paths) == 48


# ============================ 媒体编排 ============================


@pytest.mark.parametrize("theme", THEME_KEYS)
def test_second_image_gets_substantial_area(assets: dict[str, Any], theme: str) -> None:
    """两图卡片里的第二张图必须拿到实质面积，不能被压成缩略角标。"""
    model = build_model(
        make_result(title=None, text="两张图片都应当是主要内容。"),
        {"avatar": None, "hero": None, "grid": [assets["tall"], assets["wide"]]},
        watermark="Alice解析",
    )
    probe = render_probe(model, width=800, theme_key=theme)

    second = [(w, h) for name, w, h in probe.tiles if name == Path(assets["wide"]).name]
    assert second, f"{theme} 没有绘制第二张图"
    assert max(w * h for w, h in second) >= 60_000, f"{theme} 第二张图面积过小: {second}"


def test_telemetry_numbers_both_media_windows(assets: dict[str, Any]) -> None:
    """telemetry 主题的两图布局要左右并排，并给两个媒体窗都打上 01/02 编号。"""
    model = build_model(
        make_result(title=None, text="遥测双图窗口。"),
        {"avatar": None, "hero": None, "grid": [assets["tall"], assets["wide"]]},
        watermark="Alice解析",
    )
    probe = render_probe(model, width=800, theme_key="telemetry")

    assert THEMES["telemetry"].caption_numbering is True
    assert "01/02" in probe.texts
    assert "02/02" in probe.texts
    tiles = [(w, h) for _, w, h in probe.tiles]
    assert len(tiles) == 2
    assert tiles[0][1] == tiles[1][1], f"两图不等高，说明没走并排布局: {tiles}"
    assert min(w for w, _ in tiles) > 800 // 3


@pytest.mark.parametrize("theme", THEME_KEYS)
def test_many_images_render_on_every_theme_with_immersive_layout(
    assets: dict[str, Any], theme: str
) -> None:
    """9 张图 + 沉浸布局在所有主题下都要正常出图，不崩也不失控地拉长。"""
    model = build_model(
        make_result(title=None, text="多图布局回归测试。"),
        {"avatar": None, "hero": None, "grid": assets["gallery"]},
        watermark="Alice解析",
    )
    probe = render_probe(model, width=520, theme_key=theme, layout_key="immersive")
    image = probe.image

    assert image.width == 520
    assert 600 < image.height < 5000, f"{theme} 高度异常: {image.height}"
    assert any(low != high for low, high in image.getextrema())
    assert len(probe.tiles) >= 9, f"{theme} 只画了 {len(probe.tiles)} 张图"


# ============================ 文本排版 ============================


def test_hot_comment_is_limited_to_sixty_characters(tmp_path: Path) -> None:
    """评论正文按 60 字符硬截断并以省略号收尾，截断后才参与换行。"""
    result = make_result(
        title=None,
        text=None,
        extra={"hot_comments": [{"username": "Reader", "message": "很长的评论内容" * 40}]},
    )

    message = normalize_comments(result, 60)[0].message
    assert len(message) == 60
    assert message.endswith("…")

    renderer = ShareCardRenderer(cache_dir=tmp_path, width=520, hot_comment_max_chars=60)
    shim_message = renderer._normalized_card_comments(result)[0]["message"]
    assert shim_message == message
    assert "".join(renderer._wrap(shim_message, renderer._font(17), 300)) == message


def test_wrap_keeps_short_ascii_words_intact(tmp_path: Path) -> None:
    """换行不能把短 ASCII 单词劈开，但长链接必须逐字符完整保留。"""
    renderer = ShareCardRenderer(cache_dir=tmp_path, width=520)
    font = renderer._font(20)
    prefix = "为什么 "
    max_width = renderer._text_width(prefix + "H", font)

    lines = renderer._wrap(prefix + "HAL 在下一部作品", font, max_width)
    assert lines[0][-1:] != "H"
    assert any(line.startswith("HAL") for line in lines)

    long_url = "https://example.com/" + "a" * 120
    assert "".join(renderer._wrap(long_url, font, max_width)) == long_url


@pytest.mark.parametrize("layout", LAYOUT_KEYS)
def test_footer_draws_protocol_query_and_fragment(assets: dict[str, Any], layout: str) -> None:
    """四种布局的页脚都要画出完整链接：协议、查询参数与片段一个字符都不许丢。"""
    model = build_model(
        make_result(
            title="完整链接测试",
            text="四种布局都必须绘制完整协议、查询参数和片段。",
            url=FULL_URL,
            platform="bilibili",
            display_name="哔哩哔哩",
        ),
        {"avatar": None, "hero": None, "grid": [assets["wide"]]},
        watermark="Alice解析",
    )
    for theme in THEME_KEYS:
        probe = render_probe(model, width=800, theme_key=theme, layout_key=layout)
        drawn = "".join(probe.texts)
        assert not any("http" in text and text.endswith("…") for text in probe.texts), (
            f"{theme}/{layout} 把链接省略号截断了：{probe.texts}"
        )
        assert FULL_URL in drawn, (
            f"{theme}/{layout} 布局丢失了链接片段，实际绘制：{probe.texts}"
        )


def test_bilibili_theme_has_no_fixed_style_watermark(assets: dict[str, Any]) -> None:
    """bilibili 主题只做视觉仿站，不允许出现固定文案水印。"""
    model = build_model(
        make_result(title=None, text="正文先于媒体展示。"),
        {"avatar": None, "hero": None, "grid": [assets["tall"]]},
        watermark="Alice解析",
    )
    probe = render_probe(model, width=720, theme_key="bilibili")

    assert "图文" in probe.texts
    assert "哔哩哔哩风格" not in probe.joined_text
    assert "B站动态" not in probe.joined_text


@pytest.mark.parametrize("theme", THEME_KEYS)
def test_titleless_social_post_promotes_body_once(assets: dict[str, Any], theme: str) -> None:
    """无标题的社交贴文里，正文只被提升为标题绘制一次，且不出现占位标题。"""
    body = "星星眼很可爱"
    model = build_model(
        make_result(title=None, text=body),
        {"avatar": assets["avatar"], "hero": None, "grid": [assets["tall"]]},
        watermark="Alice解析",
    )
    assert model.has_real_title is False
    assert model.title == body and model.body == ""

    probe = render_probe(model, width=520, theme_key=theme)
    assert probe.texts.count(body) == 1, f"{theme} 把正文画了 {probe.texts.count(body)} 次"
    assert not [t for t in probe.texts if "UNTITLED" in t or "未命名" in t]


# ============================ 兼容层落盘 ============================


def test_render_sync_writes_png_and_avatar_changes_the_output(
    assets: dict[str, Any], tmp_path: Path
) -> None:
    """兼容层 _render_sync 要能落盘出图，且有无头像必须产出不同的卡片。"""
    result = make_result(title="落盘测试", text="正文")
    renderer = ShareCardRenderer(
        cache_dir=tmp_path, skin="aurora", theme="dark", width=720, watermark="Alice解析"
    )
    with_avatar = tmp_path / "with-avatar.png"
    without_avatar = tmp_path / "without-avatar.png"

    renderer._render_sync(
        result, {"avatar": assets["avatar"], "hero": None, "grid": [assets["wide"]]}, with_avatar
    )
    renderer._render_sync(
        result, {"avatar": None, "hero": None, "grid": [assets["wide"]]}, without_avatar
    )

    assert with_avatar.is_file() and without_avatar.is_file()
    with Image.open(with_avatar) as image:
        assert image.width == 720
    assert with_avatar.read_bytes() != without_avatar.read_bytes()

# ============================ 哔哩哔哩仿站结构 ============================


@pytest.fixture(scope="module")
def bili_model(assets: dict[str, Any]) -> Any:
    """一条贴近 B 站手机端动态详情页的样本：九图 + IP 属地 + 带头像热评 + emoji 统计行。"""
    result = make_result(
        title=None,
        text="#凡人慕兰之战# 正文用于验证富文本与话题着色 @凡人修仙传官方 https://b23.tv/abcdef",
        url="https://t.bilibili.com/1234567890",
        platform="bilibili",
        display_name="哔哩哔哩",
        author=("凡人修仙传", "@fanrenxiuxianzhuan"),
        extra={
            "stats_line": "\U0001f44d 361 \U0001fa99 1024 \u2b50 876 \u21a9\ufe0f 33 \U0001f4ac 33 \U0001f440 12.3万 \U0001f4ad 4562",
            "ip_location": "北京",
            "hot_comments": [
                {
                    "username": "热评用户甲",
                    "uid": "10001",
                    "likes": 41,
                    "time": "8月22日 江苏",
                    "message": "第一条热门评论内容。",
                },
                {
                    "username": "热评用户乙",
                    "uid": "10002",
                    "likes": 9,
                    "time": "8月22日 北京",
                    "message": "第二条热门评论内容。",
                },
            ],
        },
    )
    return build_model(
        result,
        {
            "avatar": assets["avatar"],
            "hero": None,
            "grid": assets["gallery"],
            "comment_avatars": {0: assets["avatar"], 1: assets["avatar"]},
        },
        watermark="Alice解析",
    )


def test_bilibili_nine_grid_tiles_are_uniform_squares(bili_model: Any) -> None:
    """B 站九宫格必须是等高等宽的正方格，不能因为末行不足而被拉伸。"""
    probe = render_probe(bili_model, width=800, theme_key="bilibili", layout_key="feed")

    assert len(probe.tiles) == 9, f"九图应贴 9 格，实际 {len(probe.tiles)}"
    heights = {h for _, _, h in probe.tiles}
    widths = [w for _, w, _ in probe.tiles]
    assert len(heights) == 1, f"格子高度不一致：{sorted(heights)}"
    # 列宽按整数像素分配，末列允许 1px 的取整补偿。
    assert max(widths) - min(widths) <= 1, f"格子宽度差过大：{sorted(widths)}"
    assert abs(max(widths) - max(heights)) <= 2, "九宫格应接近正方形"


def test_bilibili_tabbar_shows_comment_and_like_tabs(bili_model: Any) -> None:
    """页签条要画出"评论 N"与"赞和转发 M"两个标签。"""
    probe = render_probe(bili_model, width=800, theme_key="bilibili", layout_key="feed")

    assert "评论 33" in probe.texts
    assert "赞和转发 361" in probe.texts


def test_bilibili_comments_use_native_heading_and_reply_meta(bili_model: Any) -> None:
    """B 站评论区要有"热门评论"小标题、排序入口，以及"时间 + 回复"的元信息行。"""
    probe = render_probe(bili_model, width=800, theme_key="bilibili", layout_key="feed")

    assert "热门评论" in probe.texts
    assert "按热度" in probe.texts
    assert "8月22日 江苏 回复" in probe.texts
    assert "热评用户甲" in probe.texts


def test_bilibili_ip_note_only_lists_consumption_metrics(bili_model: Any) -> None:
    """补充说明行只放播放/弹幕这类消费指标，点赞投币收藏留给底部操作栏，避免数字重复。"""
    probe = render_probe(bili_model, width=800, theme_key="bilibili", layout_key="feed")

    note = [t for t in probe.texts if "播放" in t]
    assert note, "IP 说明行应画出播放数"
    line = note[0]
    assert "弹幕" in line
    assert "投币" not in line and "收藏" not in line and "点赞" not in line


def test_ip_note_block_measures_zero_without_stats(assets: dict[str, Any]) -> None:
    """没有可显示的消费指标时，补充说明行必须完全不占高度、不绘制。"""
    model = build_model(
        make_result(
            title=None,
            text="没有统计行的动态。",
            platform="bilibili",
            display_name="哔哩哔哩",
            extra={"stats_line": ""},
        ),
        {"avatar": assets["avatar"], "hero": None, "grid": [assets["tall"]]},
        watermark="Alice解析",
    )
    probe = render_probe(model, width=720, theme_key="bilibili", layout_key="feed")

    assert not [t for t in probe.texts if "播放" in t or "弹幕" in t]


# ============================ 统计区块窄栏回归 ============================


@pytest.mark.parametrize("theme", ("broadsheet", "nocturne"))
@pytest.mark.parametrize("width", (520, 640, 800))
def test_ledger_stats_keep_every_value_readable_in_narrow_columns(
    assets: dict[str, Any], theme: str, width: int
) -> None:
    """账簿式统计在杂志布局的窄侧栏里会被挤成 40px 一格，数值绝不能被省略成空串。"""
    values = ("361", "1024", "876", "33", "12.3万", "4562")
    model = build_model(
        make_result(
            title="窄栏统计回归",
            text="杂志布局的侧栏只有整宽的三分之一左右。",
            extra={
                "stats_line": (
                    "\U0001f44d 361 \U0001fa99 1024 \u2b50 876 "
                    "\u21a9\ufe0f 33 \U0001f440 12.3万 \U0001f4ad 4562"
                )
            },
        ),
        {"avatar": assets["avatar"], "hero": None, "grid": [assets["wide"]]},
        watermark="Alice解析",
    )
    probe = render_probe(model, width=width, theme_key=theme, layout_key="magazine")

    for value in values:
        assert value in probe.texts, f"{theme}@{width} 丢了统计值 {value}：{probe.texts}"
