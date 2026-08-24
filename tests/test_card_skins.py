import tempfile
import unittest
from pathlib import Path

from PIL import Image

from nova_core.rika_render.adapter import _split_author_label
from nova_core.rika_render.data import Author, ParseResult, Platform
from nova_core.rika_render.render import ShareCardRenderer


class CardSkinRenderTests(unittest.TestCase):
    def test_author_label_splits_name_and_identifier(self):
        self.assertEqual(
            _split_author_label("Pekachow (Amber) (@Pekachow1)"),
            ("Pekachow (Amber)", "@Pekachow1"),
        )
        self.assertEqual(
            _split_author_label("Alice(uid:10001)"),
            ("Alice", "uid:10001"),
        )

    def test_advanced_skins_render_distinct_non_blank_images(self):
        result = ParseResult(
            platform=Platform(name="twitter", display_name="推特"),
            author=Author(
                name="Pekachow (Amber) with a deliberately long display name",
                description="@Pekachow1_long_public_identifier",
            ),
            title="A translated title with enough words to verify responsive wrapping",
            text=(
                "This is a body paragraph used to verify layout and text rendering. "
                "It should remain readable without overlapping the author block."
            ),
            timestamp=1_756_000_000,
            url=(
                "https://x.com/i/status/2090756349701554395"
                "?s=46&t=full-public-link-verification"
            ),
            extra={
                "content_type": "图文",
                "stats_line": "点赞 128 评论 16 转发 8",
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

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            avatar_path = root / "avatar.png"
            hero_path = root / "hero.png"
            grid_path = root / "grid.png"
            Image.new("RGB", (180, 180), (70, 190, 240)).save(avatar_path)
            Image.new("RGB", (960, 640), (34, 112, 164)).save(hero_path)
            Image.new("RGB", (320, 320), (242, 126, 164)).save(grid_path)
            skins = ("editorial", "signal", "poster", "neon", "bilibili")

            for width in (520, 720, 800):
                outputs = []
                sizes = []
                for skin in skins:
                    renderer = ShareCardRenderer(
                        cache_dir=root,
                        skin=skin,
                        width=width,
                        watermark="Alice解析",
                    )
                    path = root / f"{skin}-{width}.png"
                    renderer._render_sync(
                        result,
                        {
                            "avatar": avatar_path,
                            "hero": hero_path,
                            "grid": [grid_path],
                        },
                        path,
                    )
                    self.assertTrue(path.is_file())
                    with Image.open(path) as image:
                        self.assertEqual(image.width, width)
                        self.assertGreater(image.height, 700)
                        bbox = image.getbbox()
                        self.assertIsNotNone(bbox)
                        self.assertLess(bbox[3], image.height)
                        extrema = image.convert("RGB").getextrema()
                        self.assertTrue(any(low != high for low, high in extrema))
                        sizes.append(image.size)
                    outputs.append(path.read_bytes())

                self.assertEqual(len(set(outputs)), len(skins))
                self.assertGreaterEqual(len(set(sizes)), 3)

            for skin in skins:
                renderer = ShareCardRenderer(
                    cache_dir=root,
                    skin=skin,
                    width=720,
                    watermark="Alice解析",
                )
                with_avatar = root / f"{skin}-avatar.png"
                without_avatar = root / f"{skin}-placeholder.png"
                renderer._render_sync(
                    result,
                    {"avatar": avatar_path, "hero": hero_path, "grid": []},
                    with_avatar,
                )
                renderer._render_sync(
                    result,
                    {"avatar": None, "hero": hero_path, "grid": []},
                    without_avatar,
                )
                self.assertNotEqual(with_avatar.read_bytes(), without_avatar.read_bytes())

    def test_full_url_layout_preserves_protocol_query_and_fragment(self):
        url = (
            "https://www.bilibili.com/video/BV1Example123"
            "?spm_id_from=333.1007.top_right_bar_window_history.content.click"
            "#reply-10001"
        )
        result = ParseResult(
            platform=Platform(name="bilibili", display_name="哔哩哔哩"),
            title="完整链接测试",
            url=url,
        )
        renderer = ShareCardRenderer(cache_dir=Path("."), width=520)
        lines, _, _ = renderer._url_layout(
            result,
            180,
            initial_size=14,
            min_size=11,
            target_lines=3,
        )

        self.assertEqual("".join(lines), url)

    def test_native_layouts_draw_the_complete_url(self):
        url = (
            "https://www.bilibili.com/video/BV1Example123"
            "?spm_id_from=333.1007.top_right_bar_window_history.content.click"
            "#reply-10001"
        )
        result = ParseResult(
            platform=Platform(name="bilibili", display_name="哔哩哔哩"),
            author=Author(name="Alice Nova", description="uid:10001"),
            title="原生布局完整链接测试",
            text="四种 Nova 原生布局都必须绘制完整协议、查询参数和片段。",
            url=url,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hero_path = root / "hero.png"
            Image.new("RGB", (960, 640), (34, 112, 164)).save(hero_path)

            for layout in ("standard", "magazine", "immersive", "feed"):
                renderer = ShareCardRenderer(
                    cache_dir=root,
                    layout=layout,
                    width=520,
                    watermark="Alice解析",
                )
                expected_lines = renderer._footer_metrics(
                    result,
                    renderer.width - 88,
                )[1]
                drawn_text: list[str] = []
                original_draw_text = renderer._draw_text

                def capture_draw_text(draw, xy, text, size, fill, bold=False):
                    drawn_text.append(str(text))
                    return original_draw_text(draw, xy, text, size, fill, bold)

                renderer._draw_text = capture_draw_text
                renderer._render_sync(
                    result,
                    {"avatar": None, "hero": hero_path, "grid": []},
                    root / f"native-{layout}.png",
                )

                self.assertTrue(expected_lines)
                self.assertTrue(
                    any(
                        drawn_text[index : index + len(expected_lines)]
                        == expected_lines
                        for index in range(len(drawn_text) - len(expected_lines) + 1)
                    ),
                    f"{layout} layout did not draw the complete URL",
                )


if __name__ == "__main__":
    unittest.main()
