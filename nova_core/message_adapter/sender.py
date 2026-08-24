"""消息发送封装，统一不同会话场景下的发送行为。"""

import os
from pathlib import Path
from typing import Any, List, Optional

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image, Node, Nodes, Plain, Reply

from ..logger import logger
from .node_builder import is_pure_image_gallery


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
    async def _finish_best_effort_delivery(
        event: AstrMessageEvent,
        *,
        label: str,
        expected: int,
        succeeded: int,
        errors: list[Exception],
    ) -> None:
        if expected <= 0 or not errors:
            return
        if succeeded <= 0:
            raise MessageDeliveryError(
                f"{label}全部发送失败（{len(errors)}项）"
            ) from errors[0]
        try:
            await event.send(
                event.plain_result(
                    f"{label}有 {len(errors)} 项发送失败，其余内容已发送。"
                )
            )
        except Exception as exc:
            logger.warning(f"发送部分失败提示失败: {exc}")

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
                    logger.warning(f"发送聚合消息失败: {exc}")

        if large_media_metadata:
            (
                large_expected,
                large_succeeded,
                large_errors,
            ) = await self.send_large_media_results(
                event,
                large_media_metadata,
                large_video_threshold_mb,
            )
            expected += large_expected
            succeeded += large_succeeded
            errors.extend(large_errors)

        await self._finish_best_effort_delivery(
            event,
            label="解析结果",
            expected=expected,
            succeeded=succeeded,
            errors=errors,
        )

    async def send_large_media_results(
        self,
        event: AstrMessageEvent,
        link_metadata: list,
        large_video_threshold_mb: float = 0.0,
    ) -> tuple[int, int, list[Exception]]:
        """发送大媒体结果（单独发送）

        Args:
            event: 消息事件对象
            link_metadata: 大媒体链接的构建辅助信息
            large_video_threshold_mb: 大视频阈值(MB)
        """
        separator = "-------------------------------------"
        threshold_mb = (
            int(large_video_threshold_mb) if large_video_threshold_mb > 0 else 50
        )
        notice_text = f"⚠️ 链接中包含超过{threshold_mb}MB的视频时将单独发送所有媒体"
        try:
            await event.send(event.plain_result(notice_text))
        except Exception as exc:
            logger.warning(f"发送大媒体提示失败: {exc}")
        expected = 0
        succeeded = 0
        errors: list[Exception] = []
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
                    logger.warning(f"发送大媒体消息链失败: {e}")
            if link_idx < len(link_metadata) - 1:
                try:
                    await event.send(event.plain_result(separator))
                except Exception as e:
                    logger.warning(f"发送分隔符失败: {e}")
        return expected, succeeded, errors

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
