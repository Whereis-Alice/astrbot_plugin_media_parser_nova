"""消息发送封装，统一不同会话场景下的发送行为。"""

import os
from pathlib import Path
from typing import Any, List, Optional

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image, Node, Nodes, Plain, Reply

from ..constants import Config
from ..logger import logger
from .node_builder import is_pure_image_gallery

# 平台富媒体通道拒收超大文件时的特征词。QQ 的 Highway 通道会在上传中途返回
# 102902，报错文本里没有"太大"两个字，只能靠这些关键字认出来。
_OVERSIZE_FAILURE_KEYWORDS = (
    "highway",
    "httpupload",
    "102902",
    "too large",
    "file size",
    "rich media",
    "上传失败",
)


class MessageDeliveryError(RuntimeError):
    """预期发送的内容全部失败。"""


class MessageSender:
    """消息发送器，封装统一的私聊/群聊发送接口。"""

    @staticmethod
    def _metadata_for_link(link_metadata: Optional[List[dict]], link_idx: int) -> dict:
        if not link_metadata or link_idx >= len(link_metadata):
            return {}
        meta = link_metadata[link_idx]
        return meta if isinstance(meta, dict) else {}

    @staticmethod
    def _delivery_chains(link_nodes: list, metadata: dict) -> list[list]:
        """把一条链接拆成平台可发送的消息链，卡片模式只在这里生效。"""
        card_node = metadata.get("card_node")
        card_mode = str(metadata.get("card_mode") or "")
        text_nodes = list(metadata.get("display_text_nodes") or [])
        media_nodes = list(metadata.get("media_nodes") or [])

        if card_node is not None and card_mode:
            chains: list[list] = []
            if card_mode == "卡片+文本同条发送":
                first_chain = [card_node]
                if text_nodes:
                    first_chain.append(text_nodes.pop(0))
                chains.append(first_chain)
                chains.extend([[node] for node in text_nodes if node is not None])
            elif card_mode == "卡片+文本分开发":
                chains.append([card_node])
                chains.extend([[node] for node in text_nodes if node is not None])
            elif card_mode == "仅卡片":
                chains.append(
                    [card_node, *[node for node in text_nodes if node is not None]]
                )
            else:
                return [[node] for node in link_nodes if node is not None]

            if media_nodes:
                if all(isinstance(node, Image) for node in media_nodes):
                    chains.append(media_nodes)
                else:
                    chains.extend([[node] for node in media_nodes if node is not None])
            return chains

        if is_pure_image_gallery(link_nodes):
            texts = [node for node in link_nodes if isinstance(node, Plain)]
            images = [node for node in link_nodes if isinstance(node, Image)]
            chains = [[node] for node in texts]
            if images:
                chains.append(images)
            return chains
        return [[node] for node in link_nodes if node is not None]

    @staticmethod
    def _source_urls(metadata_list: Any) -> List[str]:
        """从元数据里取出原始链接，供发送失败提示回显。"""
        urls: List[str] = []
        for metadata in metadata_list or []:
            if not isinstance(metadata, dict):
                continue
            url = str(metadata.get("source_url") or "").strip()
            if url and url not in urls:
                urls.append(url)
        return urls

    @staticmethod
    def _describe_delivery_failure(error: Any) -> str:
        """把平台发送异常翻译成用户看得懂的一句话。"""
        text = str(error or "").strip()
        lowered = text.lower()
        if any(keyword in lowered for keyword in _OVERSIZE_FAILURE_KEYWORDS):
            return "视频上传被聊天平台拒收（体积过大或服务端限制）"
        if not text:
            return "未知发送错误"
        return text if len(text) <= 120 else text[:117] + "..."

    @classmethod
    async def _finish_best_effort_delivery(
        cls,
        event: AstrMessageEvent,
        *,
        label: str,
        expected: int,
        succeeded: int,
        errors: list[Exception],
        failed_urls: Optional[List[str]] = None,
    ) -> None:
        if expected <= 0 or not errors:
            return
        if succeeded <= 0:
            raise MessageDeliveryError(
                f"{label}全部发送失败（{len(errors)}项）"
            ) from errors[0]
        error_preview = "; ".join(str(error) for error in errors[:3])
        logger.warning(
            f"{label}部分发送失败: {len(errors)}/{expected} 项失败，"
            f"其余内容已发送。错误: {error_preview}"
        )
        # 部分失败以前只写日志，群里看不到任何异常，用户只会觉得"视频凭空没了"。
        # 这里补一条简短提示，并尽量附上原链接方便自己点开。
        reasons: List[str] = []
        for error in errors:
            reason = cls._describe_delivery_failure(error)
            if reason not in reasons:
                reasons.append(reason)
        notice = (
            f"⚠️ 有 {len(errors)}/{expected} 项内容未能发出："
            + "；".join(reasons[:2])
        )
        unique_urls: List[str] = []
        for raw in failed_urls or []:
            url = str(raw or "").strip()
            if url and url not in unique_urls:
                unique_urls.append(url)
        if unique_urls:
            notice += "\n原始链接：" + "\n".join(unique_urls[:3])
        try:
            await event.send(event.plain_result(notice))
        except Exception as exc:
            logger.warning(f"{label}发送失败提示也未能送出: {exc}")

    @staticmethod
    def collect_rendered_card_paths(
        metadata_list: Optional[List[dict]],
    ) -> List[str]:
        """收集已渲染卡片路径，实际发送由最终消息链统一完成。"""
        card_paths: List[str] = []
        for metadata in metadata_list or []:
            if not isinstance(metadata, dict):
                continue
            card_path = str(metadata.get("_card_file_path") or "").strip()
            if not card_path or not os.path.isfile(card_path):
                continue
            card_paths.append(card_path)
        return card_paths


    def get_sender_info(self, event: AstrMessageEvent) -> tuple:
        """获取发送者信息

        Args:
            event: 消息事件对象

        Returns:
            包含发送者名称和ID的元组 (sender_name, sender_id)
        """
        sender_name = "Nova解析"
        sender_id = str(event.get_self_id() or "").strip() or "10000"
        return sender_name, sender_id

    async def send_aggregated_results(
        self,
        event: AstrMessageEvent,
        link_metadata: list,
        sender_name: str,
        sender_id: Any,
        large_video_threshold_mb: float = 0.0,
    ):
        """使用 Nodes 合并转发发送结果。

        Args:
            event: 消息事件对象
            link_metadata: 链接元数据列表
            sender_name: 发送者名称
            sender_id: 发送者ID
            large_video_threshold_mb: 大视频阈值(MB)
        """
        normal_metadata = [
            meta for meta in link_metadata if meta.get("is_normal", True)
        ]
        large_media_metadata = [
            meta for meta in link_metadata if meta.get("is_large_media", False)
        ]
        separator = "-------------------------------------"
        node_uin = str(sender_id or "").strip() or "10000"
        expected = 0
        succeeded = 0
        errors: list[Exception] = []
        failed_urls: List[str] = []

        if normal_metadata:
            flat_nodes = []
            for link_idx, metadata in enumerate(normal_metadata):
                for content in self._delivery_chains(
                    metadata.get("link_nodes") or [],
                    metadata,
                ):
                    if content:
                        flat_nodes.append(
                            Node(name=sender_name, uin=node_uin, content=content)
                        )
                if link_idx < len(normal_metadata) - 1:
                    flat_nodes.append(
                        Node(
                            name=sender_name,
                            uin=node_uin,
                            content=[Plain(separator)],
                        )
                    )
            if flat_nodes:
                expected += 1
                try:
                    await event.send(event.chain_result([Nodes(flat_nodes)]))
                    succeeded += 1
                except Exception as exc:
                    errors.append(exc)
                    # 聚合转发是一整条消息，失败即整批链接都没发出去。
                    failed_urls.extend(self._source_urls(normal_metadata))
                    logger.warning(f"发送聚合消息失败: {exc}")

        if large_media_metadata:
            (
                large_expected,
                large_succeeded,
                large_errors,
                large_failed_urls,
            ) = await self.send_large_media_results(
                event,
                large_media_metadata,
                large_video_threshold_mb,
            )
            expected += large_expected
            succeeded += large_succeeded
            errors.extend(large_errors)
            failed_urls.extend(large_failed_urls)

        await self._finish_best_effort_delivery(
            event,
            label="解析结果",
            expected=expected,
            succeeded=succeeded,
            errors=errors,
            failed_urls=failed_urls,
        )

    async def send_large_media_results(
        self,
        event: AstrMessageEvent,
        link_metadata: list,
        large_video_threshold_mb: float = 0.0,
    ) -> tuple[int, int, list[Exception], List[str]]:
        """发送大媒体结果（单独发送）

        Args:
            event: 消息事件对象
            link_metadata: 大媒体链接的构建辅助信息
            large_video_threshold_mb: 大视频阈值(MB)
        """
        separator = "-------------------------------------"
        threshold_mb = int(
            large_video_threshold_mb
            if large_video_threshold_mb > 0
            else Config.DEFAULT_LARGE_VIDEO_THRESHOLD_MB
        )
        notice_text = f"⚠️ 链接中包含超过{threshold_mb}MB的视频时将单独发送所有媒体"
        try:
            await event.send(event.plain_result(notice_text))
        except Exception as exc:
            logger.warning(f"发送大媒体提示失败: {exc}")
        expected = 0
        succeeded = 0
        errors: list[Exception] = []
        failed_urls: List[str] = []
        for link_idx, metadata in enumerate(link_metadata):
            chains = self._delivery_chains(
                metadata.get("link_nodes") or [],
                metadata,
            )
            for content in chains:
                if not content:
                    continue
                expected += 1
                try:
                    await event.send(event.chain_result(content))
                    succeeded += 1
                except Exception as e:
                    errors.append(e)
                    failed_urls.extend(self._source_urls([metadata]))
                    logger.warning(f"发送大媒体消息链失败: {e}")
            if link_idx < len(link_metadata) - 1:
                try:
                    await event.send(event.plain_result(separator))
                except Exception as e:
                    logger.warning(f"发送分隔符失败: {e}")
        return expected, succeeded, errors, failed_urls

    async def send_individual_results(
        self,
        event: AstrMessageEvent,
        all_link_nodes: list,
        link_metadata: Optional[List[dict]] = None,
        *,
        quote_user_message: bool = False,
        quote_message_id: str = "",
    ) -> None:
        """发送非聚合结果（逐项独立发送）。

        Args:
            event: 消息事件对象
            all_link_nodes: 所有链接节点列表
            link_metadata: 每条链接的构建辅助信息
            quote_user_message: 文本元数据是否引用对应的用户消息
            quote_message_id: 被引用的用户消息 ID
        """
        separator = "-------------------------------------"
        quote_message_id = str(quote_message_id or "").strip()
        expected = 0
        succeeded = 0
        errors: list[Exception] = []
        failed_urls: List[str] = []
        for link_idx, link_nodes in enumerate(all_link_nodes):
            meta = self._metadata_for_link(link_metadata, link_idx)
            metadata_text_node = meta.get("metadata_text_node")
            for content in self._delivery_chains(link_nodes, meta):
                if not content:
                    continue
                expected += 1
                chain = []
                if (
                    quote_user_message
                    and quote_message_id
                    and metadata_text_node is not None
                    and any(node is metadata_text_node for node in content)
                ):
                    chain.append(Reply(id=quote_message_id))
                chain.extend(content)
                try:
                    await event.send(event.chain_result(chain))
                    succeeded += 1
                except Exception as exc:
                    errors.append(exc)
                    failed_urls.extend(self._source_urls([meta]))
                    logger.warning(f"发送消息链失败: {exc}")
            if link_idx < len(all_link_nodes) - 1:
                try:
                    await event.send(event.plain_result(separator))
                except Exception as exc:
                    logger.warning(f"发送分隔符失败: {exc}")
        await self._finish_best_effort_delivery(
            event,
            label="解析结果",
            expected=expected,
            succeeded=succeeded,
            errors=errors,
            failed_urls=failed_urls,
        )

    async def send_zip_result(
        self,
        event: AstrMessageEvent,
        archive_path: str,
    ) -> None:
        """发送本地 ZIP 文件。"""
        try:
            from astrbot.api.message_components import File
        except ImportError as exc:
            raise RuntimeError("当前 AstrBot 版本不支持文件消息组件") from exc

        file_component = File(
            name=Path(archive_path).name,
            file=archive_path,
        )
        await event.send(event.chain_result([file_component]))
