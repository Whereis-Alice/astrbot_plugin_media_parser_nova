"""普通视频直链下载处理器。"""

from typing import Any, Dict, Optional

import aiohttp

from ...logger import logger

from ..utils import generate_cache_file_path
from .base import download_media_from_url


async def download_video_to_cache(
    session: aiohttp.ClientSession,
    video_url: str,
    cache_dir: str,
    media_id: str,
    index: int = 0,
    headers: dict = None,
    proxy: str = None,
    max_bytes: Optional[int] = None,
    budget=None,
) -> Optional[Dict[str, Any]]:
    """下载视频到缓存目录

    Args:
        session: aiohttp会话
        video_url: 视频URL
        cache_dir: 缓存目录路径
        media_id: 媒体ID
        index: 索引
        headers: 请求头字典
        proxy: 代理地址（可选）

    Returns:
        包含file_path和size_mb的字典，失败时为None
    """
    if not cache_dir:
        return None

    logger.debug(f"开始下载视频: {video_url}, media_id={media_id}, index={index}")

    def file_path_generator(content_type: str, url: str) -> str:
        return generate_cache_file_path(
            cache_dir=cache_dir,
            media_id=media_id,
            media_type="video",
            index=index,
            content_type=content_type,
            url=url,
        )

    file_path, size_mb, status_code, error = await download_media_from_url(
        session=session,
        media_url=video_url,
        file_path_generator=file_path_generator,
        is_video=True,
        headers=headers,
        proxy=proxy,
        max_bytes=max_bytes,
        budget=budget,
    )

    if file_path:
        logger.debug(f"视频下载完成: {video_url} -> {file_path}, {size_mb}MB")
        return {"file_path": file_path, "size_mb": size_mb, "status_code": status_code}
    logger.debug(f"视频下载失败: {video_url}")
    return {
        "file_path": None,
        "size_mb": None,
        "status_code": status_code,
        "error": error or "下载失败",
    }
