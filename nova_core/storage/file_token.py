"""文件 Token 服务集成，将已下载媒体注册为可回调的临时 URL。"""
import asyncio
import os
from typing import Any, Dict, List, Optional

from ..logger import logger


async def register_files_with_token_service(
    metadata: Dict[str, Any],
    callback_api_base: str,
    file_token_ttl: int,
) -> None:
    """将已下载的媒体文件注册到 AstrBot 文件 Token 服务。

    Token 服务只增强已经缓存到本地文件的媒体。注册失败不会改变解析结果，
    节点构建时会回退为本地文件发送。
    """
    metadata['use_file_token_service'] = False
    metadata['file_token_urls'] = []

    file_paths = metadata.get('file_paths', [])
    if not file_paths or metadata.get('error'):
        return

    local_modes = list(metadata.get('video_modes') or []) + list(
        metadata.get('image_modes') or []
    )
    if not any(
        fp and os.path.exists(fp) and idx < len(local_modes)
        and local_modes[idx] == "local"
        for idx, fp in enumerate(file_paths)
    ):
        return

    try:
        from astrbot.core import file_token_service, astrbot_config
    except ImportError:
        logger.warning(
            "无法导入astrbot.core的file_token_service，"
            "文件Token服务不可用，将回退为本地文件发送"
        )
        return

    if not callback_api_base:
        callback_api_base = str(
            astrbot_config.get("callback_api_base") or ""
        ).strip().rstrip("/")
    if not callback_api_base:
        logger.warning(
            "文件Token服务模式已启用，但未配置回调地址"
            "（插件配置 callback_api_base 或 AstrBot 全局 callback_api_base 均为空），"
            "将回退为本地文件发送"
        )
        return

    async def register_one(file_path: str) -> Optional[str]:
        """注册单个文件，失败时返回 None 以便回退为本地文件发送。"""
        try:
            token = await file_token_service.register_file(
                file_path, timeout=file_token_ttl
            )
            logger.debug(f"已注册文件到Token服务: {file_path}")
            return f"{callback_api_base}/api/file/{token}"
        except Exception as e:
            logger.warning(f"注册文件到Token服务失败: {file_path}, 错误: {e}")
            return None

    # 并发注册，避免逐个 await 串行等待远端服务。
    registrable_indexes = [
        idx
        for idx, fp in enumerate(file_paths)
        if idx < len(local_modes)
        and local_modes[idx] == "local"
        and fp
        and os.path.exists(fp)
    ]
    registered_urls: Dict[int, Optional[str]] = {}
    if registrable_indexes:
        gathered = await asyncio.gather(
            *(register_one(file_paths[idx]) for idx in registrable_indexes)
        )
        registered_urls = dict(zip(registrable_indexes, gathered))

    file_token_urls: List[Optional[str]] = [
        registered_urls.get(idx) for idx in range(len(file_paths))
    ]

    metadata['file_token_urls'] = file_token_urls
    metadata['use_file_token_service'] = any(
        url is not None for url in file_token_urls
    )
