"""rika 风格卡片渲染器（移植自 astrbot_plugin_rika_share，MIT）。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from ..logger import logger
from .adapter import build_parse_result
from .data import ParseResult
from .render import ShareCardRenderer

__all__ = ["ShareCardRenderer", "ParseResult", "build_parse_result", "render_card_rika"]


async def render_card_rika(
    metadata: Dict[str, Any],
    *,
    save_dir: str,
    custom_font: str = "",
    theme: str = "dark",
    layout: str = "standard",
    skin: str = "nova",
    width: int = 800,
    cover_full_size: bool = False,
    show_play_button: bool = False,
    watermark: str = "Nova解析",
    hot_comment_max_chars: int = 180,
    cache_key: Optional[str] = None,
) -> Optional[Path]:
    """用 rika 渲染器渲染卡片，成功返回 PNG 路径，失败返回 None。"""
    try:
        save_path = Path(save_dir)
        save_path.mkdir(parents=True, exist_ok=True)

        metadata["_render_save_dir"] = str(save_path)
        result = build_parse_result(metadata, save_dir=save_path)
        if not (result.title or result.text or result.author):
            return None

        font_path = custom_font
        if not font_path or not Path(font_path).is_file():
            font_path = None
        renderer = ShareCardRenderer(
            cache_dir=save_path,
            enabled=True,
            width=width,
            theme=theme,
            layout=layout,
            skin=skin,
            font_path=font_path,
            cover_full_size=cover_full_size,
            show_play_button=show_play_button,
            watermark=watermark,
            hot_comment_max_chars=hot_comment_max_chars,
        )
        url_key = str(metadata.get("url") or "")
        parts = [url_key]
        _a = str(metadata.get("avatar_url") or "")
        if _a:
            parts.append("a:" + _a)
        _covers = metadata.get("video_cover_urls") or []
        if _covers:
            parts.append(
                "covers:"
                + json.dumps(
                    _covers,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
            )
        _images = metadata.get("image_urls") or []
        if _images:
            parts.append(
                "images:"
                + json.dumps(
                    _images,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
            )
        parts.append(
            "meta:"
            + json.dumps(
                {
                    "title": metadata.get("title"),
                    "author": metadata.get("author"),
                    "desc": metadata.get("desc"),
                    "timestamp": metadata.get("timestamp"),
                    "language": metadata.get("translation_target_language"),
                    "comments": (
                        metadata.get("hot_comments")
                        if metadata.get("_card_include_hot_comments")
                        else []
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        )
        key = cache_key or "|".join(parts)
        return await renderer.render(result, cache_key=key)
    except Exception as e:
        logger.warning(f"rika 卡片渲染失败: {metadata.get('url', '')}, 错误: {e}")
        return None
