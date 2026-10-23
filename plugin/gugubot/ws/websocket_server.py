"""WebSocket server module.

A simple WebSocket server implementation based on the websocket-server library.
"""

import json
import logging
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

try:
    from websocket_server import WebsocketServer
except ImportError:
    WebsocketServer = None


class WebSocketServer:
    """WebSocket server class.

    Accepts incoming WebSocket connections from clients.

    Attributes
    ----------
    host : str
        Listening address.
    port : int
        Listening port.
    server : Optional[WebsocketServer]
        WebSocket server instance.
    server_thread : Optional[threading.Thread]
        Server thread.
    logger : logging.Logger
        Logger instance.
    clients : List[Dict]
        List of connected clients.
    """

    def __init__(
            self,
            host: str = "0.0.0.0",
            port: int = 8787,
            on_message: Optional[Callable] = None,
            on_client_connect: Optional[Callable] = None,
            on_client_disconnect: Optional[Callable] = None,
            logger: Optional[logging.Logger] = None,
    ):
        """Initialize the WebSocket server.

        Parameters
        ----------
        host : str
            Listening address, default ``"0.0.0.0"``.
        port : int
            Listening port, default ``8787``.
        on_message : Callable, optional
            Message received callback, signature: ``(client, server, message)``.
        on_client_connect : Callable, optional
            Client connected callback, signature: ``(client, server)``.
        on_client_disconnect : Callable. optional
            Client disconnected callback, signature: ``(client, server)``.
        logger : logging.Logger, optional
            Logger instance.
        """
        if WebsocketServer is None:
            raise ImportError(
                "websocket-server 库未安装。请运行: pip install websocket-server"
            )

        self.host = host
        self.port = port
        self.logger = logger or logging.getLogger(__name__)
        self.server: Optional[WebsocketServer] = None
        self.server_thread: Optional[threading.Thread] = None
        self.clients: List[Dict] = []
        # clients 会被两个不同线程访问：websocket 库的收包线程（连接 /
        # 断开 → append / remove）与插件主线程（stop / broadcast /
        # get_clients）。没有锁时：
        #   - `if client in self.clients: self.clients.remove(client)` 是
        #     「先查后删」两步操作，另一线程在两步之间 append/remove 会让
        #     remove 删掉**错误的元素**（list.remove 按值删首个匹配）；
        #   - `self.clients.copy()` 撞上并发 append 会抛 RuntimeError，
        #     或**漏掉刚连上的客户端** → stop() 不断开它 → 服务器关不掉、
        #     端口一直占用（插件 reload 时报「端口已被占用」）。
        self._clients_lock = threading.Lock()
        self._is_running = False
        # 运行线程里的启动期异常（端口占用 / 权限不足）。start() 醒来后
        # 据此抛出真实原因，而不是打印虚假的「已启动」。
        self._start_error: Optional[BaseException] = None
        # 是否由 stop() 主动要求停止：用于区分「正常停止」与「意外退出」
        self._stop_requested = False

        # Callbacks
        self._on_message_callback = on_message
        self._on_client_connect_callback = on_client_connect
        self._on_client_disconnect_callback = on_client_disconnect

    def _handle_new_client(self, client: Dict, server: Any) -> None:
        """Handle a new client connection.

        Parameters
        ----------
        client : Dict
            Client information dictionary.
        server : Any
            Server instance.
        """
        with self._clients_lock:
            self.clients.append(client)
        self.logger.info(f"新客户端连接: {client.get('address', 'unknown')}")

        if self._on_client_connect_callback:
            try:
                self._on_client_connect_callback(client, server)
            except Exception as e:
                self.logger.error("客户端连接回调执行失败: %s", e)

    def _handle_client_left(self, client: Dict, server: Any) -> None:
        """Handle a client disconnection.

        Parameters
        ----------
        client : Dict
            Client information dictionary.
        server : Any
            Server instance.
        """
        with self._clients_lock:
            if client in self.clients:
                self.clients.remove(client)
        client_address = client.get("address") if client else "unknown"
        self.logger.info(f"客户端断开: {client_address}")

        if self._on_client_disconnect_callback:
            try:
                self._on_client_disconnect_callback(client, server)
            except Exception as e:
                self.logger.error("客户端断开回调执行失败: %s", e)

    def _handle_message(self, client: Dict, server: Any, message: str) -> None:
        """Handle a received message.

        Parameters
        ----------
        client : Dict
            Client information dictionary.
        server : Any
            Server instance.
        message : str
            Received message content.
        """
        client_address = client.get("address") if client else "unknown"
        self.logger.debug(f"收到来自 {client_address} 的消息: {message}")

        if self._on_message_callback:
            try:
                self._on_message_callback(client, server, message)
            except Exception as e:
                self.logger.error(f"消息处理回调执行失败: {e}")

    def start(self, daemon: bool = True) -> None:
        """Start the WebSocket server.

        Parameters
        ----------
        daemon : bool
            Whether to run as a daemon thread, default ``True``.
        """
        if self._is_running:
            self.logger.warning("服务器已经在运行中")
            return

        try:
            self.logger.info("正在启动WebSocket服务器: %s:%s", self.host, self.port)

            self.server = WebsocketServer(
                host=self.host,
                port=self.port,
                loglevel=logging.WARNING,
            )

            self.server.set_fn_new_client(self._handle_new_client)
            self.server.set_fn_client_left(self._handle_client_left)
            self.server.set_fn_message_received(self._handle_message)

            self._start_error = None
            self._stop_requested = False

            # **置位必须先于起线程**：``_run_server`` 的 finally 会把
            # ``_is_running`` 置回 False。若顺序反过来（旧实现），
            # 端口被占用时 ``run_forever()`` 立刻抛异常 → 子线程走 finally
            # 置 False → 主线程随后又置 True → 「线程已死、状态却说在跑」。
            self._is_running = True

            self.server_thread = threading.Thread(
                target=self._run_server, name="WebSocketServer", daemon=daemon
            )
            self.server_thread.start()

            # 短暂等待，确认线程真的进了 accept 循环。
            # ``websocket-server`` 的 socket 是在 ``run_forever()`` 里才绑定
            # 的，端口占用 / 权限不足都在**线程内**炸，不 join 就发现不了
            # —— 旧实现因此会对着一个没起来的 server 打印「已启动」，
            # 而 ``bridge_connector`` 靠 ``is_running()`` 判断，会以为一切
            # 正常、不再重试启动，用户侧只看到「一直连不上」。
            self.server_thread.join(0.2)
            if not self.server_thread.is_alive():
                self._is_running = False
                reason = self._start_error
                raise RuntimeError(
                    f"WebSocket 服务器未能启动（{self.host}:{self.port}）——"
                    f"端口可能已被占用或权限不足: {reason}"
                )

            self.logger.info("WebSocket服务器已启动在 %s:%s", self.host, self.port)

        except Exception as e:
            self._is_running = False
            error_msg = str(e) + "\n" + traceback.format_exc()
            self.logger.error("启动WebSocket服务器失败: %s", error_msg)
            raise

    def _run_server(self) -> None:
        """Run the server in a thread."""
        try:
            self.server.run_forever()
        except Exception as e:
            self._start_error = e
            error_msg = str(e) + "\n" + traceback.format_exc()
            self.logger.error(f"WebSocket服务器运行出错: {error_msg}")
            if not self._stop_requested:
                # 非主动停止 → 大概率是端口占用 / 权限不足。给出可操作提示，
                # 别让用户对着「已启动」的日志猜。
                self.logger.error(
                    "WebSocket 服务器意外退出：端口 %s 可能已被其他程序占用，"
                    "或当前用户无权监听该端口。mod／桥接端将无法连接。",
                    self.port,
                )
        finally:
            self._is_running = False

    def stop(self, timeout: int = 5) -> None:
        """Stop the WebSocket server.

        Parameters
        ----------
        timeout : int
            Timeout in seconds for waiting the thread to close.
        """
        if not self._is_running:
            self.logger.warning("服务器未运行")
            return

        try:
            # 标记为「主动停止」：_run_server 据此区分正常停止与意外退出
            self._stop_requested = True
            if self.server:
                # 取快照再逐个断开：拿快照时加锁，避免与收包线程的
                # append/remove 撞车（copy 撞上并发修改会抛 RuntimeError，
                # 且会漏掉刚连上的客户端 → 它不会被断开 → 端口占用）。
                with self._clients_lock:
                    client_count = len(self.clients)
                    clients_snapshot = list(self.clients)

                if client_count > 0:
                    self.logger.info("正在断开 %s 个客户端...", client_count)

                    for client in clients_snapshot:
                        try:
                            self.server.send_message(
                                client, json.dumps({"type": "server_shutdown"})
                            )
                        except Exception:
                            pass

                        try:
                            if hasattr(self.server, "disconnect_client"):
                                self.server.disconnect_client(
                                    client, status=1000, reason="Server shutdown"
                                )
                            elif "handler" in client:
                                handler = client["handler"]
                                if hasattr(handler, "request"):
                                    handler.request.close()
                        except Exception:
                            pass

                    time.sleep(0.5)

                if hasattr(self.server, "shutdown_gracefully"):
                    self.server.shutdown_gracefully()
                else:
                    self.server.shutdown_abruptly()

            if self.server_thread and self.server_thread.is_alive():
                self.server_thread.join(timeout=timeout)

            self._is_running = False
            with self._clients_lock:
                self.clients.clear()

        except Exception as e:
            error_msg = str(e) + "\n" + traceback.format_exc()
            self.logger.error("停止WebSocket服务器时出错: %s", error_msg)
            raise

    def send_message(self, client: Dict, message: Any) -> bool:
        """Send a message to the specified client.

        Parameters
        ----------
        client : Dict
            Client information dictionary.
        message : Any
            Message to send (automatically serialized to JSON string).

        Returns
        -------
        bool
            Whether the message was sent successfully.
        """
        if not self._is_running or not self.server:
            self.logger.warning("服务器未运行，无法发送消息")
            return False

        try:
            if isinstance(message, (dict, list)):
                message = json.dumps(message, ensure_ascii=False)

            self.server.send_message(client, message)
            # 注意：日志取地址必须用 .get 兜底 —— client 缺 'address' 键时
            # `client['address']` 抛 KeyError，会被下面的 except 吞掉并
            # 返回 False，而此时**消息其实已经发出去了** → 调用方误判失败
            # 而重试 → 同一条消息重复发送。
            self.logger.debug(
                "向 %s 发送消息: %s", client.get("address", "unknown"), message
            )
            return True

        except Exception as e:
            error_msg = str(e) + "\n" + traceback.format_exc()
            self.logger.error("发送消息失败: %s", error_msg)
            return False

    def broadcast(self, message: Any) -> int:
        """Broadcast a message to all connected clients.

        Parameters
        ----------
        message : Any
            Message to broadcast (automatically serialized to JSON string).

        Returns
        -------
        int
            **真实送达**的客户端数量。
        """
        if not self._is_running or not self.server:
            self.logger.warning("服务器未运行，无法广播消息")
            return 0

        try:
            if isinstance(message, (dict, list)):
                message = json.dumps(message, ensure_ascii=False)

            # **不要**用库的 ``send_message_to_all``：它是逐个客户端写
            # socket 的朴素循环，**没有 per-client 异常保护**。只要有一个
            # 客户端已经断开（对端进程被杀 / 网络掉线但 TCP 还没感知），
            # 对它的写就会抛异常 → 异常冒泡**中断整个循环** → 排在它后面
            # 的客户端**完全收不到这条消息**；而本方法的 except 还会
            # ``return 0``，让调用方以为「一条都没发出去」，与事实相反。
            #
            # 这里改成：取快照 → 逐个发 → 单个失败只跳过它。
            clients = self.get_clients()
            sent = 0
            for client in clients:
                try:
                    self.server.send_message(client, message)
                    sent += 1
                except Exception as e:
                    # 该客户端大概率已断线：跳过，继续给其余客户端发。
                    # 每次广播都打 error 会刷屏，降到 debug；真正需要
                    # 关注的是它没收到消息这件事，而 sent 计数已经反映。
                    self.logger.debug(
                        "广播跳过客户端 %s: %s",
                        client.get("address", "unknown") if isinstance(client, dict) else client,
                        e,
                    )

            if sent < len(clients) and clients:
                self.logger.warning(
                    "广播：%s/%s 个客户端送达（%s 个已断线或写入失败）",
                    sent, len(clients), len(clients) - sent,
                )
            self.logger.debug("向 %s 个客户端广播消息: %s", sent, message)
            return sent

        except Exception as e:
            error_msg = str(e) + "\n" + traceback.format_exc()
            self.logger.error("广播消息失败: %s", error_msg)
            return 0

    def is_running(self) -> bool:
        """Check whether the server is running.

        Returns
        -------
        bool
            Whether the server is running.
        """
        return self._is_running

    def get_clients(self) -> List[Dict]:
        """Get the list of connected clients.

        Returns
        -------
        List[Dict]
            List of client information dictionaries.
        """
        with self._clients_lock:
            return list(self.clients)

    def get_client_count(self) -> int:
        """Get the number of connected clients.

        Returns
        -------
        int
            Number of connected clients.
        """
        with self._clients_lock:
            return len(self.clients)
