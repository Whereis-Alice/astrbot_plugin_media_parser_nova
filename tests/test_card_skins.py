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
            outputs = []
            sizes = []
            for skin in ("editorial", "signal", "poster"):
                renderer = ShareCardRenderer(
                    cache_dir=root,
                    skin=skin,
                    width=720,
                    watermark="Alice解析",
                )
                path = root / f"{skin}.png"
                renderer._render_sync(result, {"avatar": None, "hero": None, "grid": []}, path)
                self.assertTrue(path.is_file())
                with Image.open(path) as image:
                    self.assertEqual(image.width, 720)
                    self.assertGreater(image.height, 750)
                    bbox = image.getbbox()
                    self.assertIsNotNone(bbox)
                    self.assertLess(bbox[3], image.height)
                    sizes.append(image.size)
                outputs.append(path.read_bytes())

            self.assertEqual(len(set(outputs)), 3)
            self.assertGreaterEqual(len(set(sizes)), 2)


if __name__ == "__main__":
    unittest.main()
