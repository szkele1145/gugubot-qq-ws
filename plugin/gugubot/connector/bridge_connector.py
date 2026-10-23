"""Bridge connector implementation for cross-server communication between Minecraft servers."""

import json
import time
import threading
import traceback
from typing import Any, Dict, Optional

from gugubot.config import BotConfig
from gugubot.connector.basic_connector import BasicConnector
from gugubot.parser.mc_parser import MCParser
from gugubot.utils.types import BroadcastInfo, ProcessedInfo, Source
from gugubot.ws import WebSocketFactory


class BridgeConnector(BasicConnector):
    """Bridge connector supporting both server and client modes.

    The operating mode is determined by the ``is_main_server`` config flag:

    * ``True`` -- start a WebSocket server and wait for other servers to
      connect.
    * ``False`` -- connect to the main server as a client.
    """

    def __init__(self, server, config: Optional[BotConfig] = None):
        super().__init__(
            source="minecraft_bridge", parser=MCParser, server=server, config=config
        )
        self.source = source_name = config.get_keys(
            ["connector", "minecraft_bridge", "source_name"], "Bridge"
        )

        connector_basic_name = self.server.tr("gugubot.connector.name")
        self.log_prefix = f"[{connector_basic_name}{self.source}]"

        self._connect_count = 0
        self._client_id = f"{source_name}_{int(time.time() * 1000)}"
        self._is_reconnecting = False  # guards against concurrent reconnect threads

        self.is_main_server = config.get_keys(
            ["connector", "minecraft_bridge", "is_main_server"], True
        )

        self.reconnect = config.get_keys(
            ["connector", "minecraft_bridge", "connection", "reconnect"], 5
        )
        self.ping_interval = config.get_keys(
            ["connector", "minecraft_bridge", "connection", "ping_interval"], 5
        )
        self.ping_timeout = config.get_keys(
            ["connector", "minecraft_bridge", "connection", "ping_timeout"], 5
        )
        self.use_ssl = config.get_keys(
            ["connector", "minecraft_bridge", "connection", "use_ssl"], False
        )
        self.verify = config.get_keys(
            ["connector", "minecraft_bridge", "connection", "verify"], True
        )
        self.ca_certs = config.get_keys(
            ["connector", "minecraft_bridge", "connection", "ca_certs"], None
        )
        self.extra_sslopt = config.get_keys(
            ["connector", "minecraft_bridge", "connection", "sslopt"], {}
        )

        self.show_in_game = config.get_keys(
            ["connector", "minecraft_bridge", "show_in_game"], True
        )

        self.ws_server = None
        self.ws_client = None

    async def connect(self) -> None:
        """Establish the bridge connection (server or client, per config)."""
        if self.is_main_server and self.enable:
            self._start_server()
        elif not self.is_main_server and self.enable:
            self._connect_to_server()

    def _start_server(self) -> None:
        """Start the WebSocket bridge server in a daemon thread."""
        self.logger.info(f"{self.log_prefix} 正在启动桥接服务器")

        try:
            self.ws_server = WebSocketFactory.create_bridge_server(
                self.config,
                on_message=self._handle_server_message,
                on_client_connect=self._on_client_connect,
                on_client_disconnect=self._on_client_disconnect,
                logger=self.logger,
            )
        except Exception as e:
            # 依赖库缺失（websocket-server 未安装）等 → 桥接不可用，但
            # QQ ↔ MC 主链路必须继续工作，不能因此中断插件加载。
            self.ws_server = None
            self.logger.error(
                f"{self.log_prefix} 无法创建桥接服务器: {e}\n"
                f"  mod 端将无法连接；QQ ↔ MC 主链路不受影响。"
            )
            return

        try:
            # start() 现在会**如实抛出**启动失败（端口占用 / 权限不足），
            # 不再打印虚假的「已启动」。这里必须兜住：桥接不可用是降级，
            # 不能让它冒泡出去中断整个插件加载。
            self.ws_server.start(daemon=True)
        except Exception as e:
            self.logger.error(
                f"{self.log_prefix} 桥接服务器启动失败: {e}\n"
                f"  mod 端将无法连接。请检查端口是否被其他程序占用，"
                f"或在配置里换一个端口（connector.minecraft_bridge.port）。"
            )
            return

        self.logger.info(f"{self.log_prefix} 桥接服务器就绪 ~")

    def _connect_to_server(self) -> None:
        """Connect to the main bridge server as a client."""
        if self.ws_client:
            try:
                if self.ws_client.is_connected():
                    self.ws_client.disconnect(timeout=1)
            except Exception:
                pass

        self._connect_count += 1

        if self._connect_count > 1:
            self.logger.info(
                f"{self.log_prefix} 正在重连桥接服务器 (第 {self._connect_count} 次连接)"
            )
        else:
            self.logger.info(f"{self.log_prefix} 正在连接到桥接服务器")

        self.ws_client = None
        try:
            self.ws_client = WebSocketFactory.create_bridge_client(
                self.config,
                on_message=self._handle_client_message,
                on_open=self._on_client_open,
                on_error=self._on_client_error,
                on_close=self._on_client_close,
                logger=self.logger,
            )
        except Exception as e:
            # 依赖库缺失 / 配置非法 → 桥接不可用。**必须兜住**：异常会经
            # ConnectorManager.register_connector 重新抛出 → on_load →
            # 整个插件加载中断（连 QQ ↔ MC 主链路都起不来），
            # 而本来只是「桥接」这一个功能不可用。
            self.logger.error(
                f"{self.log_prefix} 无法创建桥接客户端: {e}\n"
                f"  跨服桥接不可用；QQ ↔ MC 主链路不受影响。"
            )
            return

        try:
            self.ws_client.connect(
                reconnect=self.reconnect,
                ping_interval=self.ping_interval,
                ping_timeout=self.ping_timeout,
                use_ssl=self.use_ssl,
                verify=self.verify,
                ca_certs=self.ca_certs,
                extra_sslopt=self.extra_sslopt,
                thread_name=f"[GUGUBot]Bridge_{self._client_id}",
            )
        except Exception as e:
            # 与 _start_server 对称：连接失败是降级，不能中断插件加载。
            # 注意 connect() 是「起后台线程后立即返回」，真正的连接结果由
            # on_open / on_error 回调报告；这里只兜住**同步**阶段的异常
            # （非法 URL、非法 sslopt、线程起不来等）。
            self.logger.error(
                f"{self.log_prefix} 连接桥接服务器失败: {e}\n"
                f"  跨服桥接不可用；QQ ↔ MC 主链路不受影响。"
                f"请检查 connector.minecraft_bridge.connection 下的 "
                f"address / use_ssl / sslopt 配置。"
            )
            return

    def _on_client_connect(self, client: Dict, _: Any) -> None:
        """Handle a new client connecting to the bridge server."""
        client_address = client.get("address") if client else "unknown"
        client_count = self.ws_server.get_client_count() if self.ws_server else 0
        self.logger.info(
            f"{self.log_prefix} 新客户端连接: {client_address} (总数: {client_count})"
        )

    def _on_client_disconnect(self, client: Dict, _: Any) -> None:
        """Handle a client disconnecting from the bridge server."""
        client_address = client.get("address") if client else "unknown"
        client_count = self.ws_server.get_client_count() if self.ws_server else 0
        self.logger.info(
            f"{self.log_prefix} 客户端断开: {client_address} (剩余: {client_count})"
        )

    def _on_client_open(self, _: Any) -> None:
        """Handle the client WebSocket connection being established."""
        self.logger.info(f"{self.log_prefix} 连接成功 ~")

    def _on_client_error(self, _: Any, error: Exception) -> None:
        """Handle a client WebSocket connection error."""
        self.logger.error(
            f"{self.log_prefix} 连接错误: {type(error).__name__} - {error}"
        )

    def _on_client_close(self, ws, _: int, reason: str) -> None:
        """Handle the client WebSocket connection closing; schedule reconnect."""

        # Ignore stale close events from previous connections
        if self.ws_client and self.ws_client.ws and self.ws_client.ws != ws:
            return

        reason_text = f"({reason})" if reason else ""
        self.logger.info(f"{self.log_prefix} 连接已断开 {reason_text}")

        # Schedule a persistent reconnect loop in a background thread
        if self.reconnect > 0 and self.enable and not self._is_reconnecting:
            self._is_reconnecting = True
            self.logger.info(f"{self.log_prefix} 将在 {self.reconnect} 秒后重连...")

            def delayed_reconnect():
                attempt = 0

                try:
                    while self.enable:
                        attempt += 1
                        time.sleep(self.reconnect)

                        if not self.enable or (
                            self.ws_client and self.ws_client.is_connected()
                        ):
                            return

                        if attempt > 1:
                            self.logger.info(
                                f"{self.log_prefix} 尝试重连 (第 {attempt} 次)..."
                            )
                        else:
                            self.logger.info(f"{self.log_prefix} 开始重连...")

                        try:
                            self._connect_to_server()
                            return
                        except Exception as e:
                            self.logger.error(f"{self.log_prefix} 重连失败: {e}")
                finally:
                    self._is_reconnecting = False

            threading.Thread(
                target=delayed_reconnect,
                name=f"[GUGUBot]Reconnect_{self._client_id}",
                daemon=True,
            ).start()

    def _submit_to_mcdr_loop(self, coro_factory) -> None:
        """把协程提交到 MCDR 的事件循环上执行，**不等待结果**。

        这是消息处理路径的**首选**入口：收包线程只负责投递，立刻返回
        → 不会因为插件逻辑里的网络请求（``is_admin`` 要拉群成员列表）
        而阻塞收包、导致桥接消息堆积。

        为什么不能用 ``asyncio.run()``
        ----------------------------
        本连接器的消息回调跑在 **websocket 库的收包线程**里（不是
        asyncio 线程）。此前用 ``asyncio.run(...)` 在该线程里**临时造
        一个事件循环**来跑插件逻辑，有两个致命问题：

        1. ``asyncio.run()`` 在协程返回后**关闭**这个临时循环，关闭时
           会**强制取消**其中所有未完成的后台任务。本插件里
           ``logic/system/vote.py`` 的 ``asyncio.create_task(
           self._monitor_vote(...))`` 与
           ``logic/plugins/player_notice.py`` 的 ``create_task(
           _delayed_leave())`` 都是 fire-and-forget 形态 → 在桥接通道
           下这些任务**刚创建就被取消、从未真正执行** → **投票监控
           静默失效（投票永不超时结算）**、延迟离场提醒永远不触发。
        2. 临时循环与 MCDR 主循环不是同一个 → 插件里创建的
           ``asyncio.Lock`` / ``Queue`` 等跨协程原语无法与主循环共享，
           定时任务（``inactive_check`` / ``unbound_check``）也拿不到
           正确的 loop。

        MCDR 提供的正规做法是 ``server.schedule_task(...)``：它把协程
        提交到 **MCDR 常驻的 async executor 事件循环**上执行（见
        ``mcdreforged/executor/task_executor_async.py`` 的
        ``submit()`` → ``event_loop.create_task(coro)``），因为循环
        长驻，``create_task`` 派生的子任务**不会被取消**。

        **传参必须是「协程对象」或「async def 函数」**
        ----------------------------------------
        MCDR 的 ``schedule_task`` 按下面的顺序判定（见
        ``mcdreforged/plugin/si/server_interface.py``）：

            if inspect.iscoroutine(callable_):           # 协程对象
                → async executor ✓
            elif inspect.iscoroutinefunction(callable_):  # async def 函数
                → async executor ✓
            elif callable(callable_):                   # 普通可调用对象
                → **同步 executor** ✗

        传 ``lambda: self._process_bridge_message(...)`` 这种**普通
        lambda** 会落到第三条分支 → 被丢进**同步** executor → 同步
        executor 调它得到一个**协程对象**却**从不 await** → 协程体从未
        执行（还会报 ``RuntimeWarning: coroutine ... was never
        awaited``）。所以这里统一传**协程对象**（``iscoroutine`` 为
        True）。

        协程对象本身**不会**提前绑定到错误的循环：asyncio 的协程对象
        本身不绑定循环，绑定发生在 ``loop.create_task(coro)`` 时 ——
        而那正是 MCDR async executor 提交时做的事。

        Parameters
        ----------
        coro_factory : Callable[[], Awaitable]
            返回协程的**工厂**。
        """
        try:
            coro = coro_factory()
        except Exception as e:
            self.logger.error(
                f"{self.log_prefix} 创建桥接处理协程失败: {e}\n{traceback.format_exc()}"
            )
            return

        try:
            self.server.schedule_task(coro)
        except Exception as e:
            # 提交失败 → 这个协程对象没人会 await，必须显式 close，
            # 否则报 "coroutine ... was never awaited" 且协程帧不会释放。
            close = getattr(coro, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            self.logger.error(
                f"{self.log_prefix} 调度到 MCDR 事件循环失败: {e}\n{traceback.format_exc()}"
            )

    def _handle_server_message(self, client: Dict, _: Any, message: str) -> None:
        """Process a message received on the server side and relay it."""
        try:
            message_data = json.loads(message) if isinstance(message, str) else message

            # !!qq 等跨平台强制广播命令仅用于单独发送到 QQ，不应原样转发给其它子服务器
            # （否则其它子服的游戏里会看到带有命令前缀的原始消息）
            skip_relay = self._is_qq_broadcast_command(message_data)

            # 整条处理链（算 is_admin → 转发给其它子服 → 本服处理）都放到
            # MCDR 事件循环里跑，**收包线程只负责投递、不等待**。
            #
            # 为什么不阻塞收包线程：is_admin 内部要
            # `await qq_connector.bot.get_group_member_list(...)`（网络请求，
            # 可能好几秒）→ 若在收包线程里同步等结果，这段时间该连接**收
            # 不到任何消息** → 桥接延迟堆积、严重时像掉线。
            self._submit_to_mcdr_loop(
                lambda: self._handle_server_message_async(
                    client, message_data, skip_relay
                )
            )

        except Exception as e:
            self.logger.error(f"{self.log_prefix} 消息处理失败: {e}")

    async def _handle_server_message_async(
        self, client: Dict, message_data: Dict, skip_relay: bool
    ) -> None:
        """在 MCDR 事件循环上完成服务端的桥接消息处理。

        Parameters
        ----------
        client : Dict
            发送方客户端描述。
        message_data : Dict
            已解析的消息体。
        skip_relay : bool
            是否跳过「转发给其它子服」。
        """
        try:
            # Relay to other connected clients
            if not skip_relay and self.ws_server and self.ws_server.get_client_count() > 1:
                message_data["bridge_source"] = client.get("address", ["unknown", 0])[0]
                sender_id = message_data.get("sender_id", None)
                message_data["is_admin"] = await self._is_admin(sender_id)

                for other_client in self.ws_server.get_clients():
                    if other_client["id"] != client["id"]:
                        self.ws_server.send_message(other_client, message_data)

            await self._process_bridge_message(message_data)

        except Exception as e:
            self.logger.error(
                f"{self.log_prefix} 处理桥接消息失败: {e}\n{traceback.format_exc()}"
            )

    def _is_qq_broadcast_command(self, message_data: Dict) -> bool:
        """判断桥接消息是否是子服发来的 !!qq 跨平台广播命令。

        Parameters
        ----------
        message_data : Dict
            桥接消息的原始数据。

        Returns
        -------
        bool
            消息第一个文本段是否以配置的 qq_command 前缀开头。
        """
        if not self.config.get_keys(["system", "cross_broadcast", "enable"], True):
            return False

        qq_cmd = self.config.get_keys(
            ["system", "cross_broadcast", "qq_command"], "!!qq"
        )

        for seg in message_data.get("processed_message", []) or []:
            if isinstance(seg, dict) and seg.get("type") == "text":
                text = (seg.get("data") or {}).get("text", "").strip()
                return text.startswith(qq_cmd)

        return False

    def _handle_client_message(self, _: Any, message: str) -> None:
        """Process a message received on the client side."""
        try:
            message_data = json.loads(message) if isinstance(message, str) else message

            if (
                isinstance(message_data, dict)
                and message_data.get("type") == "server_shutdown"
            ):
                return

            self._submit_to_mcdr_loop(
                lambda: self._process_bridge_message(message_data)
            )

        except Exception as e:
            self.logger.error(f"{self.log_prefix} 消息处理失败: {e}")

    async def _process_bridge_message(self, message_data: Dict) -> None:
        """Reconstruct a ``BroadcastInfo`` from bridge data and dispatch it."""
        try:
            target = message_data.get("target", {}) or {}
            if target and self.source not in target and len(target) == 1:
                return

            source_data = message_data.get("source")
            source = Source.from_any(source_data)
            source.add(self.source)

            processed_info = BroadcastInfo(
                event_type="message",
                event_sub_type=message_data.get("event_sub_type", "group"),
                message=message_data.get("processed_message", []),
                sender=message_data.get("sender", "System"),
                sender_id=message_data.get("sender_id", None),
                _source=source,
                source_id=message_data.get("source_id", ""),
                raw=message_data.get("raw", message_data),
                server=self.server,
                logger=self.logger,
                is_admin=(
                    message_data.get("is_admin")
                    if message_data.get("is_admin") is not None
                    else await self._is_admin(message_data.get("sender_id"))
                ),
                target=target,
            )

            if self.parser is not None:
                await self.parser.system_manager.broadcast_command(processed_info)

        except Exception as e:
            self.logger.error(
                f"{self.log_prefix} 处理桥接消息失败: {e}\n{traceback.format_exc()}"
            )

    async def send_message(self, processed_info: ProcessedInfo) -> None:
        """Serialize and send a message over the bridge."""
        if not self.enable:
            return

        # Serialize Source as a list for WebSocket transport
        source_list = processed_info.source.to_list() if processed_info.source else []

        message_data = {
            "sender": processed_info.sender
            or self.config.get_keys(
                ["connector", "minecraft_bridge", "source_name"], "System"
            ),
            "sender_id": processed_info.sender_id,
            "event_sub_type": processed_info.event_sub_type,
            "receiver": processed_info.receiver,
            "source": source_list,
            "source_id": processed_info.source_id,
            "raw": processed_info.raw,
            "processed_message": processed_info.processed_message,
            "target": processed_info.target,
            "is_admin": (
                await self._is_admin(processed_info.sender_id)
                if self.is_main_server
                else None
            ),
        }

        if self.is_main_server:
            # Server mode: broadcast to all connected clients
            if self.ws_server and self.ws_server.is_running():
                count = self.ws_server.broadcast(message_data)
                self.logger.debug(f"{self.log_prefix} 广播消息给 {count} 个客户端")
        else:
            # Client mode: send to the server
            if self.ws_client and self.ws_client.is_connected():
                if self.ws_client.send(message_data):
                    self.logger.debug(f"{self.log_prefix} 发送消息到服务器")
                else:
                    self.logger.warning(f"{self.log_prefix} 发送消息失败")

    async def disconnect(self) -> None:
        """Disconnect the bridge (server or client)."""
        try:
            self.enable = False

            if self.is_main_server:
                if self.ws_server and self.ws_server.is_running():
                    self.ws_server.stop()
            else:
                if self.ws_client:
                    self.ws_client.disconnect()

            self.logger.info(f"{self.log_prefix} 已断开 ~")
        except Exception as e:
            self.logger.warning(f"{self.log_prefix} 断开连接时出错: {e}")

    async def on_message(self, raw: Any) -> None:
        """Handle an incoming raw message via the parser."""
        if not self.enable:
            return None

        if self.parser is not None:
            return await self.parser.parse(raw)
        return None

    async def _is_admin(self, sender_id) -> bool:
        """Return whether *sender_id* is an admin."""
        bound_system = self.connector_manager.system_manager.get_system("bound")

        if not bound_system:
            return False

        player_manager = bound_system.player_manager
        return await player_manager.is_admin(sender_id)
