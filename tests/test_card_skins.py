import tempfile
import unittest
from pathlib import Path

from PIL import Image

from nova_core.rika_render.data import Author, ParseResult, Platform
from nova_core.rika_render.render import ShareCardRenderer


class CardSkinRenderTests(unittest.TestCase):
    def test_advanced_skins_render_distinct_non_blank_images(self):
        result = ParseResult(
            platform=Platform(name="twitter", display_name="推特"),
            author=Author(name="Alice", description="Nova media lab"),
            title="A translated title for the public card",
            text="This is a body paragraph used to verify layout and text rendering.",
            timestamp=1_756_000_000,
            url="https://x.com/i/status/2090756349701554395",
            extra={
                "content_type": "图文",
                "stats_line": "点赞 128 评论 16 转发 8",
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
                    self.assertGreater(image.height, 500)
                    self.assertIsNotNone(image.getbbox())
                    sizes.append(image.size)
                outputs.append(path.read_bytes())

            self.assertEqual(len(set(outputs)), 3)
            self.assertGreaterEqual(len(set(sizes)), 2)


if __name__ == "__main__":
    unittest.main()
