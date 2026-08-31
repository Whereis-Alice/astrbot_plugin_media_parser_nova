"""重新生成 docs/card-skins/ 下的全部文档截图。

用法（在仓库根目录执行）::

    python tools/render_skin_docs.py            # 全部重出
    python tools/render_skin_docs.py aurora x   # 只重出指定文件

脚本刻意走公开的 ShareCardRenderer.render 路径（含皮肤名归一、素材汇集、
build_model 与 render_card_image），所以文档截图与线上输出必然一致，不会再
出现「文档里好看、线上不生效」这种偏差。素材是脚本自己用 Pillow 画的渐变图，
不依赖网络，也不会把任何真实站点内容提交进仓库。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nova_core.rika_render.data import (  # noqa: E402  (需先补 sys.path)
    Author,
    ImageContent,
    ParseResult,
    Platform,
    VideoContent,
)
from nova_core.rika_render.render import ShareCardRenderer  # noqa: E402
from nova_core.rika_render.task import PathTask  # noqa: E402

OUT_DIR = ROOT / "docs" / "card-skins"

#: 文档画布底色（与 README 的深色阅读环境接近）
CANVAS = (13, 13, 18)

#: 单皮肤图：左浅右深两张等宽卡片
PAIR_WIDTH = 540
PAIR_GAP = 32

#: 三联图：三张等宽卡片
TRIO_WIDTH = 760
TRIO_GAP = 48

WATERMARK = "Nova解析"


# ============================ 素材 ============================


def _gradient(path: Path, size: tuple[int, int], corners: Sequence[tuple[int, int, int]]) -> Path:
    """用 2x2 双三次放大画一张四角渐变图（比逐像素快两个数量级）。"""
    seed = Image.new("RGB", (2, 2))
    seed.putpixel((0, 0), tuple(corners[0]))
    seed.putpixel((1, 0), tuple(corners[1]))
    seed.putpixel((0, 1), tuple(corners[2]))
    seed.putpixel((1, 1), tuple(corners[3]))
    seed.resize(size, Image.BICUBIC).save(path)
    return path


def build_assets(root: Path) -> dict[str, Any]:
    """一次性画出文档用的全部素材：封面、图集、作者与评论头像。"""
    root.mkdir(parents=True, exist_ok=True)
    return {
        "hero": _gradient(
            root / "hero.png",
            (1600, 900),
            [(126, 74, 220), (206, 88, 178), (72, 62, 168), (238, 120, 150)],
        ),
        "grid": [
            _gradient(
                root / "grid-1.png",
                (1080, 1350),
                [(38, 150, 196), (66, 208, 208), (26, 96, 150), (110, 224, 190)],
            ),
            _gradient(
                root / "grid-2.png",
                (1200, 1200),
                [(232, 106, 152), (246, 152, 168), (196, 74, 140), (250, 188, 190)],
            ),
            _gradient(
                root / "grid-3.png",
                (1400, 900),
                [(232, 154, 62), (246, 198, 96), (198, 108, 52), (250, 224, 148)],
            ),
        ],
        "avatar": _gradient(
            root / "avatar.png",
            (240, 240),
            [(94, 132, 246), (150, 118, 244), (58, 96, 200), (196, 150, 250)],
        ),
        "comment_avatars": [
            _gradient(
                root / "avatar-c1.png",
                (200, 200),
                [(84, 196, 178), (126, 220, 176), (48, 148, 148), (168, 236, 190)],
            ),
            _gradient(
                root / "avatar-c2.png",
                (200, 200),
                [(244, 150, 116), (250, 190, 132), (208, 108, 96), (252, 216, 168)],
            ),
            _gradient(
                root / "avatar-c3.png",
                (200, 200),
                [(160, 152, 244), (198, 168, 250), (114, 108, 208), (224, 200, 252)],
            ),
        ],
    }


# ============================ 样例内容 ============================


def _ts(text: str) -> int:
    fmt = "%Y-%m-%d %H:%M" if " " in text else "%Y-%m-%d"
    return int(datetime.strptime(text, fmt).timestamp())


SAMPLES: dict[str, dict[str, Any]] = {
    "bilibili": {
        "platform": "bilibili",
        "display_name": "哔哩哔哩",
        "author": "Nova 视觉实验室",
        "handle": "@nova_lab",
        "title": "全新卡片设计系统：主题、布局与深浅色三轴独立生效",
        "body": (
            "这一版把排版拆成可测量的区块，先量后画，彻底移除硬编码高度；"
            "媒体按真实宽高比自适应，热评并入主流程，不再有拼接接缝。"
        ),
        "time": "2025-08-24 09:46",
        "duration": 754,
        "online": "3.1万人在看",
        "stats_line": "👍 12.8万 🪙 8千 ⭐ 3.2万 🔁 1.2万 👀 421万 💬 1024 💭 4562",
        "url": (
            "https://www.bilibili.com/video/BV1Example123/"
            "?spm_id_from=333.788.videopod.sections&vd_source=ab12cd34ef56"
        ),
        "has_video": True,
        "comments": [
            ("一位读者", "uid:10001", 4213, "这一版的排版终于对齐了，标题和媒体的呼吸感很好。"),
            ("第二位读者", "uid:10002", 908, "深色模式的层次比之前干净很多，统计条也不再折行。"),
            ("第三位读者", "uid:10003", 77, "希望浅色模式也能这么好看。"),
        ],
    },
    "youtube": {
        "platform": "youtube",
        "display_name": "YouTube",
        "author": "コズまげch",
        "handle": "@kozumage",
        "title": "如果 Ex-Aid 的主题曲被用在名侦探光之美少女中",
        "body": "把主题曲剪进开场，画面与鼓点几乎逐帧咬合；副歌进来的那一拍连字幕节奏都对上了。",
        "time": "2026-08-22",
        "duration": 226,
        "online": "",
        "stats_line": "👀 128万 👍 4.2万 💬 863",
        "url": "https://www.youtube.com/watch?v=2smExample01",
        "has_video": True,
        "comments": [
            ("Kamen Fan", "uid:20001", 1240, "副歌进来的那一秒鸡皮疙瘩起来了。"),
            ("光之美少女应援团", "uid:20002", 306, "第二段的转场剪得太干净了。"),
        ],
    },
    "twitter": {
        "platform": "twitter",
        "display_name": "推特",
        "author": "プリキュア公式",
        "handle": "@precure_movie",
        "title": None,
        "body": (
            "剧场版《名侦探光之美少女》本预告解禁！事件的钥匙，在那座城市的圣诞夜。"
            "本篇 12 月 5 日全国上映，前售券同步开卖。"
        ),
        "time": "2026-08-27 21:05",
        "duration": 0,
        "online": "",
        "stats_line": "❤️ 3.7万 🔁 1.2万 💬 486 📈 219万",
        "url": "https://x.com/precure_movie/status/2092446937500860496",
        "has_video": False,
        "comments": [
            ("看片的人", "uid:30001", 2180, "这次的预告信息量比上次多太多了。"),
            ("字幕组路人", "uid:30002", 415, "圣诞夜这个设定一出来就知道要哭了。"),
        ],
    },
}

#: 单皮肤图用哪个样例：仿站皮肤配对应平台的内容，通用皮肤统一用 B 站样例
SKIN_SAMPLE = {"x": "twitter", "youtube": "youtube"}


def make_result(key: str, assets: dict[str, Any]) -> ParseResult:
    """构造一份带素材的 ParseResult（PathTask 需要事件循环，必须在协程里调用）。"""
    sample = SAMPLES[key]

    async def ready(path: Any) -> Path:
        return Path(str(path))

    def task(path: Any) -> PathTask:
        return PathTask(ready(path))

    extra: dict[str, Any] = {
        "stats_line": sample["stats_line"],
        "hot_comments": [
            {
                "username": name,
                "uid": uid,
                "likes": likes,
                "time": sample["time"],
                "message": message,
            }
            for name, uid, likes, message in sample["comments"]
        ],
        "hot_comment_avatars": [
            task(p) for p in assets["comment_avatars"][: len(sample["comments"])]
        ],
    }
    if sample["online"]:
        extra["online"] = sample["online"]
    if sample["duration"]:
        extra["duration"] = sample["duration"]

    result = ParseResult(
        platform=Platform(name=sample["platform"], display_name=sample["display_name"]),
        author=Author(
            name=sample["author"],
            avatar=task(assets["avatar"]),
            description=sample["handle"],
        ),
        title=sample["title"],
        text=sample["body"],
        timestamp=_ts(sample["time"]),
        url=sample["url"],
        extra=extra,
    )
    if sample["has_video"]:
        result.contents.append(
            VideoContent(
                path_task=task(assets["hero"]),
                cover=task(assets["hero"]),
                duration=float(sample["duration"]),
            )
        )
    for path in assets["grid"]:
        result.graphics.append(ImageContent(path_task=task(path)))
    return result


# ============================ 渲染与拼版 ============================


async def render_card(
    key: str,
    assets: dict[str, Any],
    cache_dir: Path,
    *,
    skin: str,
    mode: str,
    layout: str,
    width: int,
) -> Image.Image:
    """渲染一张卡片并返回 PIL 图像（完整走 ShareCardRenderer 公开入口）。"""
    renderer = ShareCardRenderer(
        cache_dir,
        width=width,
        theme=mode,
        layout=layout,
        skin=skin,
        watermark=WATERMARK,
        show_play_button=True,
    )
    result = make_result(key, assets)
    path = await renderer.render(result, cache_key=f"{key}|{skin}|{mode}|{layout}|{width}")
    if path is None:
        raise RuntimeError(f"渲染失败: {key}/{skin}/{mode}/{layout}")
    with Image.open(path) as image:
        return image.convert("RGB")


def compose(cards: Sequence[Image.Image], gap: int) -> Image.Image:
    """把若干张卡片横向拼在深色画布上（顶部对齐，无外边距）。"""
    width = sum(card.width for card in cards) + gap * (len(cards) - 1)
    height = max(card.height for card in cards)
    canvas = Image.new("RGB", (width, height), CANVAS)
    x = 0
    for card in cards:
        canvas.paste(card, (x, 0))
        x += card.width + gap
    return canvas


async def build(names: set[str] | None) -> list[str]:
    """按需重出文档截图，返回实际写盘的文件名列表。"""
    from nova_core.card import THEME_KEYS

    written: list[str] = []
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nova-skin-docs-") as tmp:
        tmp_dir = Path(tmp)
        assets = build_assets(tmp_dir / "assets")
        cache_dir = tmp_dir / "cache"

        def wanted(name: str) -> bool:
            return names is None or name in names

        for skin in THEME_KEYS:
            if not wanted(skin):
                continue
            key = SKIN_SAMPLE.get(skin, "bilibili")
            cards = [
                await render_card(
                    key,
                    assets,
                    cache_dir,
                    skin=skin,
                    mode=mode,
                    layout="standard",
                    width=PAIR_WIDTH,
                )
                for mode in ("light", "dark")
            ]
            out = OUT_DIR / f"{skin}.png"
            compose(cards, PAIR_GAP).save(out)
            written.append(out.name)

        trio = ("bilibili", "youtube", "twitter")
        if wanted("platform-skin"):
            cards = [
                await render_card(
                    key,
                    assets,
                    cache_dir,
                    skin="跟随平台",
                    mode="dark",
                    layout="standard",
                    width=TRIO_WIDTH,
                )
                for key in trio
            ]
            out = OUT_DIR / "platform-skin.png"
            compose(cards, TRIO_GAP).save(out)
            written.append(out.name)

        if wanted("platform-chrome"):
            cards = [
                await render_card(
                    key,
                    assets,
                    cache_dir,
                    skin="bilibili",
                    mode="dark",
                    layout="feed",
                    width=TRIO_WIDTH,
                )
                for key in trio
            ]
            out = OUT_DIR / "platform-chrome.png"
            compose(cards, TRIO_GAP).save(out)
            written.append(out.name)
    return written


def main(argv: Sequence[str]) -> int:
    names = {arg.removesuffix(".png") for arg in argv} or None
    written = asyncio.run(build(names))
    if not written:
        print("没有匹配到任何截图名，可用名称：8 套皮肤 + platform-skin + platform-chrome")
        return 1
    for name in written:
        print(f"已重出 docs/card-skins/{name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
