"""消息节点构建器，将解析结果转换为可发送消息节点。"""

import os
from typing import Any, Dict, List, Optional, Union

from astrbot.api.message_components import Image, Plain, Video

from ..downloader.utils import strip_media_prefixes
from ..parser.utils import format_duration_ms
from ..logger import logger
from ..message_text import split_message_text
from ..metadata_visibility import text_metadata_field_enabled
from ..types import BuildAllNodesResult, LinkBuildMeta

TEXT_SECTION_SEPARATOR = "-------------------------------------"


def _split_plain_node(node: Optional[Plain]) -> List[Plain]:
    """将文本节点统一拆分为不超过单消息长度上限的节点。"""
    if node is None:
        return []
    return [Plain(chunk) for chunk in split_message_text(node.text)]


def _resolve_output_flag(metadata: Dict[str, Any], key: str, default: bool) -> bool:
    value = metadata.get(key)
    if value is None:
        return bool(default)
    return bool(value)


def _append_media_skip_summary(text_parts: List[str], metadata: Dict[str, Any]) -> None:
    """将媒体跳过统计和逐项原因追加到文本节点。"""
    video_reasons = metadata.get("video_skip_reasons", []) or []
    image_reasons = metadata.get("image_skip_reasons", []) or []
    image_warnings = metadata.get("image_warnings", []) or []
    video_count = metadata.get("video_count", len(metadata.get("video_urls", [])))
    image_count = metadata.get("image_count", len(metadata.get("image_urls", [])))
    skipped_videos = [
        (idx + 1, reason) for idx, reason in enumerate(video_reasons) if reason
    ]
    skipped_images = [
        (idx + 1, reason) for idx, reason in enumerate(image_reasons) if reason
    ]
    warnings = [
        (idx + 1, warning) for idx, warning in enumerate(image_warnings) if warning
    ]
    if not skipped_videos and not skipped_images and not warnings:
        return

    summary_parts = []
    if video_count:
        summary_parts.append(f"视频 {len(skipped_videos)}/{video_count}")
    if image_count:
        summary_parts.append(f"图片 {len(skipped_images)}/{image_count}")
    if summary_parts:
        text_parts.append(f"媒体跳过：{', '.join(summary_parts)}")

    for idx, reason in skipped_videos[:5]:
        text_parts.append(f"  视频[{idx}]：{reason}")
    for idx, reason in skipped_images[:5]:
        text_parts.append(f"  图片[{idx}]：{reason}")
    for idx, warning in warnings[:5]:
        text_parts.append(f"图片处理警告[{idx}]：{warning}")


def _append_media_notices(
    text_parts: List[str],
    metadata: Dict[str, Any],
    max_video_size_mb: float = 0.0,
    *,
    has_text_metadata: bool = True,
) -> None:
    """渲染解析失败、体积超限与逐项跳过原因，供文本节点与仅卡片模式共用。"""
    if metadata.get("error"):
        text_parts.append(f"解析失败：{metadata['error']}")

    if (
        metadata.get("has_valid_media") is False
        and (metadata.get("video_urls") or metadata.get("image_urls"))
        and has_text_metadata
        and not metadata.get("exceeds_max_size")
        and not metadata.get("send_limit_exceeded")
    ):
        if metadata.get("has_access_denied"):
            text_parts.append("解析失败：媒体访问被拒绝(403 Forbidden)")
        else:
            text_parts.append("解析失败：直链内未找到有效媒体")

    if metadata.get("exceeds_max_size"):
        actual_video_size = metadata.get("max_video_size_mb")
        if actual_video_size is not None:
            if max_video_size_mb > 0:
                text_parts.append(
                    "解析失败：视频大小超过管理员设定的限制"
                    f"（{actual_video_size:.1f}MB > {max_video_size_mb:.1f}MB）"
                )
            else:
                text_parts.append(
                    f"解析失败：视频大小超过限制（{actual_video_size:.1f}MB）"
                )

    _append_media_skip_summary(text_parts, metadata)


def build_media_notice_node(
    metadata: Dict[str, Any], max_video_size_mb: float = 0.0
) -> Optional[Plain]:
    """仅构建媒体提示节点。

    仅卡片模式会丢掉完整文本节点，但"视频体积超过可发送上限、只发了封面"这类
    信息必须让用户看到，否则视频凭空消失、群里没有任何解释。
    """
    text_parts: List[str] = []
    _append_media_notices(text_parts, metadata, max_video_size_mb)
    if not text_parts:
        return None
    return Plain("\n".join(text_parts))


def _mark_media_failure(
    metadata: Dict[str, Any], kind: str, index: int, reason: str
) -> None:
    """节点构建失败时回填跳过原因，供文本节点或调试使用。"""
    key = "video_skip_reasons" if kind == "video" else "image_skip_reasons"
    count_key = "failed_video_count" if kind == "video" else "failed_image_count"
    reasons = metadata.setdefault(key, [])
    while len(reasons) <= index:
        reasons.append(None)
    if not reasons[index]:
        reasons[index] = reason
    try:
        metadata[count_key] = int(metadata.get(count_key, 0) or 0) + 1
    except (TypeError, ValueError):
        metadata[count_key] = 1


def build_text_node(
    metadata: Dict[str, Any],
    max_video_size_mb: float = 0.0,
    enable_text_metadata: bool = True,
) -> Optional[Plain]:
    """构建文本节点

    Args:
        metadata: 元数据字典
        max_video_size_mb: 最大允许的视频大小(MB)，用于显示详细的错误信息
        enable_text_metadata: 是否包含视频图文文本信息的附加文本

    Returns:
        Plain文本节点，无内容时为None
    """
    if not enable_text_metadata:
        error = str(metadata.get("error") or "").strip()
        if not error:
            return None
        url = str(metadata.get("url") or metadata.get("source_url") or "").strip()
        text = f"解析失败：{error}"
        if url:
            text += f"\n原始链接：{url}"
        return Plain(text)

    text_parts = []
    desc_text = (
        str(metadata.get("desc") or "").strip()
        if text_metadata_field_enabled(metadata, "description")
        else ""
    )

    if text_metadata_field_enabled(metadata, "title") and metadata.get("title"):
        text_parts.append(f"标题：{metadata['title']}")
    if text_metadata_field_enabled(metadata, "author") and metadata.get("author"):
        text_parts.append(f"作者：{metadata['author']}")
    if text_metadata_field_enabled(metadata, "timestamp") and metadata.get("timestamp"):
        text_parts.append(f"发布时间：{metadata['timestamp']}")

    video_count = metadata.get("video_count", 0)
    if video_count > 0:
        actual_max_video_size_mb = metadata.get("max_video_size_mb")
        total_video_size_mb = metadata.get("total_video_size_mb", 0.0)

        if actual_max_video_size_mb is not None:
            if video_count == 1:
                text_parts.append(f"视频大小：{actual_max_video_size_mb:.1f} MB")
            else:
                text_parts.append(
                    f"视频大小：最大 {actual_max_video_size_mb:.1f} MB "
                    f"(共 {video_count} 个视频, 总计 {total_video_size_mb:.1f} MB)"
                )

    has_text_metadata = bool(
        (text_metadata_field_enabled(metadata, "title") and metadata.get("title"))
        or (text_metadata_field_enabled(metadata, "author") and metadata.get("author"))
        or desc_text
        or (
            text_metadata_field_enabled(metadata, "timestamp")
            and metadata.get("timestamp")
        )
    )

    access_status = metadata.get("access_status")
    access_message = metadata.get("access_message")
    available_length_ms = metadata.get("available_length_ms")
    timelength_ms = metadata.get("timelength_ms")
    is_preview_only = metadata.get("is_preview_only")
    # B站的 access_message 讲的是「可解析时长 / 全长」，YouTube 讲的是「为什么
    # 拿不到流」。后者挂在「时长：」下面读起来完全不通，所以按有没有可解析时长
    # 分流：带时长的仍用「时长」，纯限制说明改用中性的「提示」，并把真实时长
    # 单独补一行，免得这一行把时长的位置占了却什么都没说。
    if access_status and access_status != "full" and access_message:
        if available_length_ms:
            text_parts.append(f"时长：{access_message}")
        else:
            duration_text = format_duration_ms(timelength_ms)
            if duration_text:
                text_parts.append(f"时长：{duration_text}")
            text_parts.append(f"提示：{access_message}")
    elif is_preview_only and available_length_ms:
        try:
            available_seconds = max(0, int(available_length_ms) // 1000)
            full_seconds = (
                max(0, int(timelength_ms) // 1000)
                if timelength_ms is not None
                else None
            )
            available_min, available_sec = divmod(available_seconds, 60)
            if full_seconds is not None:
                full_min, full_sec = divmod(full_seconds, 60)
                text_parts.append(
                    f"时长：当前可解析 {available_min:02d}:{available_sec:02d} / "
                    f"全长 {full_min:02d}:{full_sec:02d}"
                )
            else:
                text_parts.append(
                    f"时长：当前可解析 {available_min:02d}:{available_sec:02d}"
                )
        except (TypeError, ValueError):
            pass

    _append_media_notices(
        text_parts,
        metadata,
        max_video_size_mb,
        has_text_metadata=has_text_metadata,
    )

    if text_metadata_field_enabled(metadata, "original_link") and metadata.get("url"):
        text_parts.append(f"原始链接：{metadata['url']}")

    if desc_text:
        if text_parts:
            text_parts.append(TEXT_SECTION_SEPARATOR)
        text_parts.append("简介/正文：")
        text_parts.append(desc_text)

    if not text_parts:
        return None
    return Plain("\n".join(text_parts))


def build_hot_comments_node(
    metadata: Dict[str, Any], enable_text_metadata: bool = True
) -> Optional[Plain]:
    """构建独立热评节点，避免与基础文本元数据混排。"""
    if not enable_text_metadata:
        return None

    hot_comments = metadata.get("hot_comments", [])
    if not isinstance(hot_comments, list) or not hot_comments:
        return None

    text_parts = [f"热评（{len(hot_comments)}条）："]
    total = len(hot_comments)
    for idx, item in enumerate(hot_comments, start=1):
        if not isinstance(item, dict):
            continue
        username = str(item.get("username", "") or "").strip() or "未知用户"
        uid = str(item.get("uid", "") or "").strip()
        try:
            likes = int(item.get("likes", 0) or 0)
        except (TypeError, ValueError):
            likes = 0
        time_text = str(item.get("time", "") or "").strip() or "-"
        message = str(item.get("message", "") or "").strip() or "（无文本内容）"
        user_label = f"{username}(uid:{uid})" if uid else username
        text_parts.append(f"[{idx}] {user_label}")
        text_parts.append(f"点赞: {likes} | 时间: {time_text}")
        text_parts.append(message)
        if idx < total:
            text_parts.append("")

    if len(text_parts) <= 1:
        return None
    return Plain("\n".join(text_parts))


def build_media_nodes(
    metadata: Dict[str, Any],
    use_local_files: bool = False,
    enable_rich_media: bool = True,
) -> List[Union[Image, Video]]:
    """构建媒体节点

    Args:
        metadata: 元数据字典
        use_local_files: 是否使用本地文件
        enable_rich_media: 是否构建富媒体节点

    Returns:
        媒体节点列表（Image或Video节点）
    """
    nodes = []
    url = metadata.get("url", "")

    if not enable_rich_media:
        logger.debug(f"富媒体输出已关闭，跳过媒体节点: {url}")
        return nodes

    if metadata.get("exceeds_max_size"):
        logger.debug(f"媒体超过大小限制，跳过节点构建: {url}")
        return nodes

    has_valid_media = metadata.get("has_valid_media")
    if has_valid_media is None:
        logger.warning(f"元数据中has_valid_media字段为None，视为False: {url}")
        has_valid_media = False

    if has_valid_media is False:
        logger.debug(f"媒体无效，跳过节点构建: {url}")
        return nodes

    video_urls = metadata.get("video_urls", [])
    image_urls = metadata.get("image_urls", [])
    file_paths = metadata.get("file_paths", [])
    video_modes = metadata.get("video_modes") or []
    image_modes = metadata.get("image_modes") or []
    use_fts = metadata.get("use_file_token_service", False)
    file_token_urls = metadata.get("file_token_urls", [])

    logger.debug(
        f"构建媒体节点: {url}, "
        f"视频: {len(video_urls)}, 图片: {len(image_urls)}, "
        f"文件路径: {len(file_paths)}, 使用本地文件: {use_local_files}, "
        f"文件Token服务: {use_fts}"
    )

    if not video_urls and not image_urls and not file_paths:
        logger.debug(f"无媒体内容，跳过节点构建: {url}")
        return nodes

    file_idx = 0

    for idx, url_list in enumerate(video_urls):
        mode = (
            video_modes[idx]
            if idx < len(video_modes)
            else ("local" if use_local_files else "direct")
        )
        if mode == "skip":
            file_idx += 1
            continue
        if not url_list or not isinstance(url_list, list):
            file_idx += 1
            continue

        video_url = url_list[0] if url_list else None
        if not video_url:
            file_idx += 1
            continue

        token_url = (
            file_token_urls[file_idx]
            if use_fts and file_idx < len(file_token_urls)
            else None
        )
        if token_url:
            try:
                nodes.append(Video.fromURL(token_url))
                file_idx += 1
                continue
            except Exception as e:
                logger.warning(f"使用文件Token构建视频节点失败: {e}")

        if (
            mode == "local"
            and file_idx < len(file_paths)
            and file_paths[file_idx]
            and os.path.exists(file_paths[file_idx])
        ):
            try:
                nodes.append(Video.fromFileSystem(file_paths[file_idx]))
            except Exception as e:
                logger.warning(f"构建视频节点失败: {file_paths[file_idx]}, 错误: {e}")
                _mark_media_failure(
                    metadata, "video", idx, f"构建本地视频节点失败: {e}"
                )
        elif mode == "local":
            _mark_media_failure(metadata, "video", idx, "本地视频文件不存在或不可访问")
        else:
            actual_video_url = strip_media_prefixes(video_url)
            try:
                nodes.append(Video.fromURL(actual_video_url))
            except Exception as e:
                logger.warning(f"构建视频节点失败: {actual_video_url}, 错误: {e}")
                _mark_media_failure(metadata, "video", idx, f"构建视频URL节点失败: {e}")

        file_idx += 1

    for image_idx, url_list in enumerate(image_urls):
        mode = (
            image_modes[image_idx]
            if image_idx < len(image_modes)
            else ("local" if use_local_files else "direct")
        )
        if mode == "skip":
            file_idx += 1
            continue
        if not url_list or not isinstance(url_list, list):
            file_idx += 1
            continue

        image_url = url_list[0] if url_list else None
        if not image_url:
            file_idx += 1
            continue

        token_url = (
            file_token_urls[file_idx]
            if use_fts and file_idx < len(file_token_urls)
            else None
        )
        if token_url:
            try:
                nodes.append(Image.fromURL(token_url))
                file_idx += 1
                continue
            except Exception as e:
                logger.warning(f"使用文件Token构建图片节点失败: {e}")

        if (
            mode == "local"
            and file_idx < len(file_paths)
            and file_paths[file_idx]
            and os.path.exists(file_paths[file_idx])
        ):
            try:
                nodes.append(Image.fromFileSystem(file_paths[file_idx]))
            except Exception as e:
                logger.warning(f"构建图片节点失败: {file_paths[file_idx]}, 错误: {e}")
                _mark_media_failure(
                    metadata, "image", image_idx, f"构建本地图片节点失败: {e}"
                )
        elif mode == "local":
            _mark_media_failure(
                metadata, "image", image_idx, "本地图片文件不存在或不可访问"
            )
        else:
            try:
                nodes.append(Image.fromURL(image_url))
            except Exception as e:
                logger.warning(f"构建图片节点失败: {image_url}, 错误: {e}")
                _mark_media_failure(
                    metadata, "image", image_idx, f"构建图片URL节点失败: {e}"
                )

        file_idx += 1

    logger.debug(f"构建媒体节点完成: {url}, 共 {len(nodes)} 个节点")
    return nodes


def _build_node_parts_for_link(
    metadata: Dict[str, Any],
    use_local_files: bool = False,
    max_video_size_mb: float = 0.0,
    enable_text_metadata: bool = True,
    enable_rich_media: bool = True,
) -> tuple[
    List[Union[Plain, Image, Video]],
    Optional[Plain],
    Dict[str, Any],
]:
    nodes: List[Union[Plain, Image, Video]] = []
    effective_text_metadata = _resolve_output_flag(
        metadata,
        "_enable_text_metadata",
        enable_text_metadata,
    )
    effective_rich_media = _resolve_output_flag(
        metadata,
        "_enable_rich_media",
        enable_rich_media,
    )
    effective_hot_comments_text = _resolve_output_flag(
        metadata,
        "_enable_hot_comments_text",
        True,
    )

    media_nodes = build_media_nodes(
        metadata,
        use_local_files,
        effective_rich_media,
    )
    text_node = build_text_node(
        metadata,
        max_video_size_mb,
        effective_text_metadata,
    )
    hot_comments_node = build_hot_comments_node(
        metadata,
        effective_text_metadata and effective_hot_comments_text,
    )
    text_nodes = _split_plain_node(text_node)
    hot_comments_nodes = _split_plain_node(hot_comments_node)
    display_text_nodes = [*text_nodes, *hot_comments_nodes]
    card_node: Optional[Image] = None
    card_path = str(metadata.get("_card_file_path") or "").strip()
    if card_path and os.path.isfile(card_path):
        try:
            card_node = Image.fromFileSystem(card_path)
        except Exception as exc:
            logger.warning(f"构建卡片图片节点失败: {card_path}, 错误: {exc}")

    card_mode = str(metadata.get("_card_mode") or "").strip()
    if card_node is None:
        # 渲染失败时始终保留普通文本，避免卡片模式吞掉解析信息。
        card_mode = ""
        nodes.extend(display_text_nodes)
    elif card_mode == "仅卡片":
        display_text_nodes = []
        if effective_text_metadata:
            notice_node = build_media_notice_node(metadata, max_video_size_mb)
            display_text_nodes.extend(_split_plain_node(notice_node))
        if (
            text_metadata_field_enabled(metadata, "original_link")
            and metadata.get("url")
        ):
            display_text_nodes.append(Plain(f"原始链接：{metadata['url']}"))
        nodes.append(card_node)
        nodes.extend(display_text_nodes)
    else:
        nodes.append(card_node)
        nodes.extend(display_text_nodes)
    nodes.extend(media_nodes)

    metadata_text_node = text_nodes[0] if text_nodes else None
    delivery = {
        "card_node": card_node,
        "card_mode": card_mode,
        "display_text_nodes": display_text_nodes,
        "media_nodes": media_nodes,
    }
    return nodes, metadata_text_node, delivery


def is_pure_image_gallery(nodes: List[Union[Plain, Image, Video]]) -> bool:
    """判断节点列表是否是纯图片图集

    Args:
        nodes: 节点列表

    Returns:
        是否为纯图片图集
    """
    has_video = False
    has_image = False
    for node in nodes:
        if isinstance(node, Video):
            has_video = True
            break
        elif isinstance(node, Image):
            has_image = True
    return has_image and not has_video


def summarize_node_counts(
    all_link_nodes: List[List[Union[Plain, Image, Video]]],
) -> Dict[str, int]:
    """统计最终可发送节点数量，用于条件聚合判定。"""
    image_count = 0
    video_count = 0
    node_count = 0

    for link_nodes in all_link_nodes:
        for node in link_nodes:
            if node is None:
                continue
            node_count += 1
            if isinstance(node, Image):
                image_count += 1
            elif isinstance(node, Video):
                video_count += 1

    return {
        "image_count": image_count,
        "video_count": video_count,
        "node_count": node_count,
    }


def build_all_nodes(
    metadata_list: List[Dict[str, Any]],
    large_video_threshold_mb: float = 0.0,
    max_video_size_mb: float = 0.0,
    enable_text_metadata: bool = True,
    enable_rich_media: bool = True,
) -> BuildAllNodesResult:
    """构建所有链接的消息节点。

    Args:
        metadata_list: 元数据列表
        large_video_threshold_mb: 大视频阈值(MB)
        max_video_size_mb: 最大允许的视频大小(MB)，用于显示错误信息
        enable_text_metadata: 是否发送图文文本消息
        enable_rich_media: 是否发送图片/视频

    Returns:
        BuildAllNodesResult 命名元组
    """
    all_link_nodes = []
    link_metadata = []
    temp_files = []
    video_files = []

    logger.debug(f"开始构建所有节点，元数据数量: {len(metadata_list)}")

    for idx, metadata in enumerate(metadata_list):
        url = metadata.get("url", "")
        max_video_size = metadata.get("max_video_size_mb")
        exceeds_max_size = metadata.get("exceeds_max_size", False)
        is_large_media = False
        if (
            large_video_threshold_mb > 0
            and max_video_size is not None
            and not exceeds_max_size
        ):
            if max_video_size > large_video_threshold_mb:
                is_large_media = True

        use_local_files = metadata.get("use_local_files", False)

        logger.debug(
            f"构建节点[{idx}]: {url}, "
            f"大媒体: {is_large_media}, 使用本地文件: {use_local_files}"
        )

        link_nodes, metadata_text_node, delivery = _build_node_parts_for_link(
            metadata,
            use_local_files,
            max_video_size_mb,
            enable_text_metadata,
            enable_rich_media,
        )

        logger.debug(f"节点构建完成[{idx}]: {url}, 节点数量: {len(link_nodes)}")

        link_file_paths = metadata.get("file_paths", [])
        link_video_files = []
        link_temp_files = []

        video_urls = metadata.get("video_urls", [])
        video_count = len(video_urls)
        video_modes = metadata.get("video_modes") or []
        image_modes = metadata.get("image_modes") or []

        for fp_idx, file_path in enumerate(link_file_paths):
            if not file_path:
                continue
            if fp_idx < video_count:
                mode = video_modes[fp_idx] if fp_idx < len(video_modes) else ""
                if mode == "local":
                    link_video_files.append(file_path)
                    video_files.append(file_path)
            else:
                img_idx = fp_idx - video_count
                mode = image_modes[img_idx] if img_idx < len(image_modes) else ""
                if mode == "local":
                    link_temp_files.append(file_path)
                    temp_files.append(file_path)

        if link_nodes:
            all_link_nodes.append(link_nodes)
            link_metadata.append(
                LinkBuildMeta(
                    metadata_index=idx,
                    link_nodes=link_nodes,
                    is_large_media=is_large_media,
                    is_normal=not is_large_media,
                    video_files=link_video_files,
                    temp_files=link_temp_files,
                    metadata_text_node=metadata_text_node,
                    card_node=delivery["card_node"],
                    card_mode=delivery["card_mode"],
                    display_text_nodes=delivery["display_text_nodes"],
                    media_nodes=delivery["media_nodes"],
                    source_url=str(
                        metadata.get("url") or metadata.get("source_url") or ""
                    ),
                )
            )
        else:
            logger.debug(f"节点为空，跳过发送队列: {url}")

    logger.debug(
        f"所有节点构建完成: "
        f"链接节点: {len(all_link_nodes)}, "
        f"临时文件: {len(temp_files)}, "
        f"视频文件: {len(video_files)}"
    )

    return BuildAllNodesResult(all_link_nodes, link_metadata, temp_files, video_files)
