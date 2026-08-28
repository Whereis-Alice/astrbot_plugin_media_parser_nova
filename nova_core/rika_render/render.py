"""解析卡片渲染兼容层。

真正的排版与绘制逻辑已经迁移到 nova_core.card 设计系统，本模块只保留历史
公开 API（ShareCardRenderer 与若干模块级工具函数），负责：

1. 参数归一（主题 / 布局 / 风格别名，含 v1.4 及更早的旧配置值）；
2. 缓存路径计算（样式版本 + 全部影响视觉的参数）；
3. 并发拉取头像 / 封面 / 图集的本地文件；
4. 把 ParseResult 交给 nova_core.card.build_model 与 render_card_image 渲染。

所有绘图仍是 CPU 密集的同步任务，由 ShareCardRenderer.render 通过
asyncio.to_thread 放到后台线程，避免阻塞 AstrBot 事件循环。
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..card import (
    LAYOUT_KEYS,
    PLATFORM_ACCENTS,
    THEME_KEYS,
    TypeSetter,
    build_model,
    clean_text,
    limit_chars,
    parse_stats,
    render_card_image,
    resolve_layout_key,
    resolve_mode,
    resolve_theme_key,
)
from ..logger import logger
from .data import ImageContent, ParseResult
from .task import PathTask

try:
    from PIL import Image
except ImportError:  # pragma: no cover - 缺少 Pillow 时整体禁用渲染
    Image = None  # type: ignore[assignment]

#: 卡片右下角默认署名
DEFAULT_WATERMARK_TAG = "Nova解析"

#: 卡片样式版本：视觉变化时必须 +1，否则用户侧已缓存的旧卡片不会重新渲染。
#: 19 = 全新 nova_core.card 设计系统（主题 / 布局 / 深浅色三者全部生效）。
_CARD_STYLE_VERSION = "24"

#: 布局与风格枚举（直接取自设计系统，避免两处枚举漂移）
LAYOUT_NAMES: tuple[str, ...] = LAYOUT_KEYS
SKIN_NAMES: tuple[str, ...] = THEME_KEYS

#: 封面下载失败时的内置兜底背景图
_FALLBACK_BG_PATH = Path(__file__).resolve().parent / "assets" / "fallback_nova.png"

#: 平台品牌色（历史 API，设计系统内部已改用 PLATFORM_ACCENTS）
PLATFORM_COLORS: dict[str, str] = dict(PLATFORM_ACCENTS)


# ============================ 模块级工具（历史 API） ============================


def strip_emoji(text: Optional[str]) -> str:
    """移除 emoji 并做 NFKC 归一化，避免字体缺字渲染成方块。"""
    return clean_text(text)


def parse_stats_line(stats_line: Optional[str]) -> list[tuple[str, str]]:
    """将类似「👍 1.2万 🪙 8千」的统计行解析为 (标签, 数值) 列表。"""
    return parse_stats(stats_line)


def card_footer_url(result: Any) -> str:
    """返回完整原始链接，协议、路径、查询参数和片段均不省略。"""
    return str(getattr(result, "url", None) or "").strip()


def format_timestamp(ts: Optional[int]) -> Optional[str]:
    """卡片脚注用的短时间戳（月-日 时:分）；无法格式化时返回 None。"""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return None


# ============================ 渲染器 ============================


class ShareCardRenderer:
    """把 ParseResult 渲染为分享卡片 PNG。"""

    def __init__(
        self,
        cache_dir: Path,
        *,
        enabled: bool = True,
        width: int = 800,
        theme: str = "dark",
        font_path: Optional[str] = None,
        layout: str = "standard",
        skin: str = "nova",
        cover_full_size: bool = False,
        show_play_button: bool = False,
        watermark: str = DEFAULT_WATERMARK_TAG,
        show_watermark: bool = True,
        show_url: bool = True,
        hot_comment_max_chars: int = 180,
    ) -> None:
        self.cache_dir = cache_dir
        self.enabled = bool(enabled) and Image is not None
        try:
            width_value = int(width)
        except (TypeError, ValueError):
            width_value = 800
        self.width = max(520, min(1080, width_value))
        self.theme_name = resolve_mode(theme)
        self.layout_name = resolve_layout_key(layout)
        self.skin_name = resolve_theme_key(skin)
        self.font_path = font_path
        self.show_play_button = bool(show_play_button)
        self.cover_full_size = bool(cover_full_size)
        self.watermark = (str(watermark or "").strip() or DEFAULT_WATERMARK_TAG)[:32]
        self.show_watermark = bool(show_watermark)
        self.show_url = bool(show_url)
        try:
            comment_limit = int(hot_comment_max_chars)
        except (TypeError, ValueError):
            comment_limit = 180
        self.hot_comment_max_chars = max(60, min(600, comment_limit or 180))
        self._typesetter = TypeSetter(font_path=font_path)

    # ---------- 对外入口 ----------

    async def render(
        self,
        result: ParseResult,
        cache_key: Optional[str] = None,
        existing: Optional[Path] = None,
    ) -> Optional[Path]:
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

    # ---------- 缓存 ----------

    def _output_path(self, cache_key: Optional[str], result: ParseResult) -> Path:
        """按「样式版本 + 全部视觉参数 + 内容指纹」生成缓存文件名。"""
        extra = getattr(result, "extra", {}) or {}
        warnings_str = "|".join(extra.get("limit_warnings") or [])
        payload = (
            cache_key
            or f"{result.platform.name}|{result.title}|{result.timestamp}|{result.url}"
        )
        signature = "|".join(
            (
                _CARD_STYLE_VERSION,
                self.skin_name,
                self.theme_name,
                self.layout_name,
                str(self.width),
                str(self.cover_full_size),
                str(self.show_play_button),
                self.watermark,
                str(self.show_watermark),
                str(self.show_url),
                str(self.hot_comment_max_chars),
                payload,
                warnings_str,
            )
        )
        digest = hashlib.md5(signature.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"card_{digest}.png"

    # ---------- 素材 ----------

    async def _collect_images(self, result: ParseResult) -> dict[str, Any]:
        """并发获取头像 / 视频封面 / 图集图片的本地路径。"""
        images: dict[str, Any] = {
            "avatar": None,
            "hero": None,
            "grid": [],
            "comment_avatars": {},
        }

        tasks: list[tuple[str, PathTask]] = []
        if result.author and result.author.avatar:
            tasks.append(("avatar", result.author.avatar))

        for index, task in enumerate(result.extra.get("hot_comment_avatars") or []):
            if isinstance(task, PathTask):
                tasks.append((f"comment_avatar:{index}", task))

        video = result.video
        hero_task: Optional[PathTask] = None
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
            if isinstance(path, BaseException):
                if isinstance(path, asyncio.CancelledError):
                    raise path
                logger.debug(f"卡片素材获取失败（{kind}）: {path}")
                continue
            if not path:
                continue
            if kind == "avatar":
                images["avatar"] = path
            elif kind == "hero":
                images["hero"] = path
            elif kind.startswith("comment_avatar:"):
                images["comment_avatars"][int(kind.split(":", 1)[1])] = path
            else:
                images["grid"].append(path)

        # 封面下载失败时使用内置兜底背景图
        if images["hero"] is None and hero_task is not None and _FALLBACK_BG_PATH.is_file():
            images["hero"] = _FALLBACK_BG_PATH
        return images

    # ---------- 同步渲染 ----------

    def _render_sync(
        self,
        result: ParseResult,
        images: dict[str, Any],
        out_path: Path,
    ) -> Path:
        """在后台线程里构建数据模型并绘制卡片。"""
        payload = dict(images)
        hero = payload.get("hero")
        if not hero and not payload.get("grid") and _FALLBACK_BG_PATH.is_file():
            # 完全没有素材时用内置背景兜底，避免纯文字卡片显得空旷
            payload["hero"] = _FALLBACK_BG_PATH

        model = build_model(
            result,
            payload,
            watermark=self.watermark,
            show_watermark=self.show_watermark,
            show_url=self.show_url,
            comment_max_chars=self.hot_comment_max_chars,
        )
        image = render_card_image(
            model,
            width=self.width,
            mode=self.theme_name,
            theme_key=self.skin_name,
            layout_key=self.layout_name,
            font_path=self.font_path,
            show_play_button=self.show_play_button,
            cover_full_size=self.cover_full_size,
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(out_path)
        return out_path

    # ---------- 兼容用文本工具 ----------

    def _font(self, size: int, bold: bool = False) -> Any:
        return self._typesetter.font(size, bold)

    def _text_width(self, text: str, font: Any) -> int:
        return self._typesetter.width(text, font)

    def _wrap(self, text: str, font: Any, max_width: int) -> list[str]:
        return self._typesetter.wrap(text, font, max_width)

    def _fit_lines(
        self, text: str, font: Any, max_width: int, max_lines: int
    ) -> list[str]:
        return self._typesetter.fit(text, font, max_width, max_lines)

    @staticmethod
    def _limit_text(text: str, max_chars: int) -> str:
        return limit_chars(text, max_chars)

    def _draw_text(
        self,
        draw: Any,
        xy: tuple[int, int],
        text: str,
        size: int,
        fill: Any,
        bold: bool = False,
    ) -> None:
        font = self._typesetter.font(size, bold)
        self._typesetter.draw_line(draw, xy, text, font, fill, bold=bold)

    def _normalized_card_comments(self, result: ParseResult) -> list[dict[str, Any]]:
        """把 extra 里的 hot_comments 归一为卡片可直接使用的字段。"""
        extra = getattr(result, "extra", {}) or {}
        comments = extra.get("hot_comments")
        if not isinstance(comments, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in comments[:5]:
            if not isinstance(item, dict):
                continue
            message = limit_chars(
                clean_text(str(item.get("message") or "")),
                self.hot_comment_max_chars,
            )
            if not message:
                continue
            normalized.append(
                {
                    "username": clean_text(str(item.get("username") or "未知用户")),
                    "uid": clean_text(str(item.get("uid") or "")),
                    "likes": item.get("likes", 0),
                    "likes_text": clean_text(str(item.get("likes_text") or "")),
                    "time": clean_text(str(item.get("time") or "")),
                    "message": message,
                }
            )
        return normalized


__all__ = [
    "DEFAULT_WATERMARK_TAG",
    "LAYOUT_NAMES",
    "PLATFORM_COLORS",
    "SKIN_NAMES",
    "ShareCardRenderer",
    "card_footer_url",
    "format_timestamp",
    "parse_stats_line",
    "strip_emoji",
]
