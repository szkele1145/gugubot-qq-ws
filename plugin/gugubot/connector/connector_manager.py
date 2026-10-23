"""
Manager for multiple connector instances.
Provides registration, removal, and message broadcasting across connectors.
"""

import asyncio
import logging
import traceback
from typing import Dict, List, Optional

from gugubot.config import BotConfig
from gugubot.connector.basic_connector import BasicConnector
from gugubot.utils.types import ProcessedInfo


class ConnectorManager:
    """Manage multiple connector instances.

    Provides registration, removal, and message broadcasting across
    connectors.

    Attributes
    ----------
    connectors : list[BasicConnector]
        All connector instances currently managed.
    logger : logging.Logger
        Logger instance.
    """

    def __init__(
        self, server, bot_config: BotConfig, logger: Optional[logging.Logger] = None
    ) -> None:
        """Initialize the connector manager.

        Parameters
        ----------
        server : Any
            MCDR server instance.
        bot_config : BotConfig
            Bot configuration object.
        logger : logging.Logger, optional
            Logger instance.  Falls back to ``server.logger`` when omitted.
        """
        self.connectors: List[BasicConnector] = []

        self.server = server
        self.config = bot_config
        self.logger = logger or server.logger

        self.system_manager = None  # gugubot.logic.system.system_manager.SystemManager

    def register_system_manager(self, system_manager) -> None:
        """Register the system manager instance.

        Parameters
        ----------
        system_manager : SystemManager
            The system manager to register.
        """
        self.system_manager = system_manager

    def get_connector(self, source: str) -> Optional[BasicConnector]:
        """Look up a connector by its source identifier.

        Parameters
        ----------
        source : str
            Source identifier of the desired connector.

        Returns
        -------
        BasicConnector or None
            The matching connector, or ``None`` if not found.
        """
        for connector in self.connectors:
            if connector.source == source:
                return connector
        return None

    async def register_connector(self, connector: BasicConnector) -> None:
        """Register and connect a new connector.

        Parameters
        ----------
        connector : BasicConnector
            The connector instance to register.

        Raises
        ------
        ValueError
            If the connector is already registered.
        """
        if connector in self.connectors:
            raise ValueError(f"连接器 {connector.source} 已经存在")

        try:
            connector.connector_manager = self
            connector.logger = self.logger
            connector.config = self.config

            await connector.connect()
            self.connectors.append(connector)

            self.logger.info(f"已添加并连接到连接器: {connector.source}")
        except Exception as e:
            error_msg = str(e) + "\n" + traceback.format_exc()
            self.logger.error(f"连接到 {connector.source} 失败: {error_msg}")
            raise

    async def remove_connector(self, connector: BasicConnector) -> None:
        """Disconnect and remove a connector.

        The connector is always removed from the internal list, even if
        disconnecting raises an exception.

        Parameters
        ----------
        connector : BasicConnector
            The connector instance to remove.

        Raises
        ------
        ValueError
            If the connector is not registered.
        """
        if connector not in self.connectors:
            raise ValueError(f"连接器 {connector.source} 不存在")

        try:
            await connector.disconnect()
            self.connectors.remove(connector)
            self.logger.info(f"已断开并移除连接器: {connector.source}")
        except Exception as e:
            error_msg = str(e) + "\n" + traceback.format_exc()
            self.logger.error(f"断开 {connector.source} 失败: {error_msg}")
            # Still remove from the list even if disconnect failed
            self.connectors.remove(connector)
            raise

    async def broadcast_processed_info(
        self,
        processed_info: ProcessedInfo,
        include: Optional[List[str]] = None,
        exclude: Optional[List[str]] = None,
    ) -> Dict[str, Exception]:
        """Broadcast a message to all (or a filtered subset of) connectors.

        Parameters
        ----------
        processed_info : ProcessedInfo
            The processed message to broadcast.
        include : list[str] or None, optional
            If given, only send to connectors whose source matches one of
            these patterns.
        exclude : list[str] or None, optional
            If given, skip connectors whose source matches any of these
            patterns.

        Returns
        -------
        dict[str, Exception]
            A mapping of connector source to the exception raised during
            sending, for every connector that failed.
        """
        failures: Dict[str, Exception] = {}
        tasks = []
        to_connectors = self.connectors

        # **精确相等**匹配（与 get_connector 一致）。
        #
        # 旧实现是 ``re.match(re.escape(p), c.source)`` —— 注释写的是
        # "literal matching"，但 ``re.match`` 只锚定开头、不锚定结尾，实际
        # 变成**前缀匹配**。连接器名（source_name）由用户自定义，互为前缀
        # 极其常见（QQ / QQ2、Minecraft / MinecraftBeta、群 / 群2）：
        #   * include 侧 → **消息重复**：reply() 用 include=[receiver_source]
        #     回消息，选中 QQ 时 QQ2 也被带上 → 同一条回复发到两个连接器；
        #   * exclude 侧 → **消息丢失**：echo 用 exclude=[source_name, ...]
        #     避免回传自己，排除 QQ 时把 QQ2 一起排掉 → 该转发的消息静默消失。
        if include is not None:
            to_connectors = [
                c for c in to_connectors if any(c.source == p for p in include)
            ]

        if exclude is not None:
            to_connectors = [
                c for c in to_connectors if not any(c.source == p for p in exclude)
            ]

        connector_info = f"广播消息到连接器: {to_connectors}"
        message_info = f"消息内容: {processed_info}"
        debug_msg = connector_info + "\n" + message_info
        self.logger.debug(debug_msg)

        for connector in to_connectors:
            task = asyncio.create_task(self._safe_send(connector, processed_info))
            tasks.append((connector, task))

        for connector, task in tasks:
            try:
                await task
            except Exception as e:
                failures[connector.source] = e

        return failures

    async def _safe_send(
        self, connector: BasicConnector, processed_info: ProcessedInfo
    ) -> None:
        """Send a message to a single connector, logging errors.

        Parameters
        ----------
        connector : BasicConnector
            Target connector.
        processed_info : ProcessedInfo
            The processed message to send.

        Raises
        ------
        Exception
            Re-raised after logging if the send fails.
        """
        try:
            await connector.send_message(processed_info)
        except Exception as e:
            error_msg = str(e) + "\n" + traceback.format_exc()
            self.logger.error(f"发送消息到 {connector.source} 失败: {error_msg}")
            raise

    async def disconnect_all(self) -> Dict[str, Exception]:
        """Disconnect and remove every registered connector.

        Returns
        -------
        dict[str, Exception]
            A mapping of connector source to the exception raised during
            disconnection, for every connector that failed.
        """
        failures: Dict[str, Exception] = {}
        tasks = []

        for connector in self.connectors[:]:  # iterate over a copy to avoid mutating the list
            task = asyncio.create_task(self.remove_connector(connector))
            tasks.append((connector, task))

        for connector, task in tasks:
            try:
                await task
            except Exception as e:
                error_msg = str(e) + "\n" + traceback.format_exc()
                self.logger.error(f"[gugubot]断开 {connector.source} 失败: {error_msg}")
                failures[connector.source] = e

        return failures
