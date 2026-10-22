"""QQ WebSocket connector."""

import asyncio
import json
import random
import re
import threading
import time
import traceback
import uuid
from typing import Any, Optional

from gugubot.builder import CQHandler
from gugubot.config import BotConfig
from gugubot.connector.basic_connector import BasicConnector
from gugubot.parser.qq_parser import QQParser
from gugubot.utils.types import ProcessedInfo
from gugubot.ws import WebSocketFactory


def strip_minecraft_color_codes(text: str) -> str:
    """Strip Minecraft colour / formatting codes (e.g. ``§a``, ``§l``).

    Parameters
    ----------
    text : str
        Text that may contain Minecraft colour codes.

    Returns
    -------
    str
        The text with all colour codes removed.
    """
    return re.sub(r"§[0-9a-fk-or]", "", text, flags=re.IGNORECASE)


class Bot:
    """OneBot API client."""

    def __init__(self, send_message, max_wait_time: int = 9) -> None:
        self.send_message = send_message
        # max_wait_time 来自配置，用户可能填成字符串（"5"）→ 比较运算
        # 抛 TypeError → OneBot 请求跟踪全部失效。统一转 int 后再钳制。
        try:
            max_wait_time = int(max_wait_time)
        except (TypeError, ValueError):
            max_wait_time = 9
        self.max_wait_time = max_wait_time if 0 < max_wait_time <= 9 else 9
        self.function_return = {}
        self.pending_requests = {}
        # 这两个 dict 被**三个线程**碰：
        #   - MCDR 循环（发起方）：登记、轮询、清理
        #   - 收包线程（qq_connector 的 fast path）：写 function_return
        #   - MCDR 循环（qq_parser.py）：写 function_return
        # 单个 key 的 dict 读写在 CPython 里靠 GIL 保持原子，但
        # 「先 in 再 []」「检查后删除」这类**复合操作不是原子的**，
        # 中间可能被另一个线程改掉 → KeyError / 响应丢失。
        # 这里用一把锁把「登记 / 存响应 / 取响应 / 清理」都串起来。
        self._lock = threading.Lock()
        # 孤儿响应的存入时刻（echo → time.time()），用于回收宽限期判断
        self._orphan_stored_at: dict = {}
        self.self_id = None  # bot's own QQ ID

    @staticmethod
    def format_request(action: str, params: dict = None):
        """Format a request for the OneBot API."""
        if params is None:
            params = {}
        return {"action": action, "params": params, "echo": params.get("echo", "")}

    def _cleanup_pending_requests(self) -> None:
        """清理超时的请求记录与「孤儿响应」。

        两类要回收的东西：

        1. **超时请求**：``pending_requests`` 里超过
           ``max_wait_time + 5`` 的条目（含其响应）。
        2. **孤儿响应**：``function_return`` 里有、但 ``pending_requests``
           里没有的条目。这类响应来自「响应早于登记到达」的窗口，
           或者同一 WS 上别人的请求。请求方若随后登记了同名 echo，
           它会**立刻**取走（``_take_response``）；取不走的（例如
           请求已超时结束）则由这里按宽限期回收，**不会无限增长**。
        """
        now = time.time()
        grace = self.max_wait_time + 5
        with self._lock:
            stale_keys = [
                k
                for k, t in self.pending_requests.items()
                if now - t > grace
            ]
            for k in stale_keys:
                self.function_return.pop(k, None)
                self.pending_requests.pop(k, None)

            # 孤儿响应：pending 里没有 → 不属于任何在途请求。
            # 刚存进来的（now - 存表时刻）不能立刻删 —— 那正是
            # 「响应先到、请求方随后登记」的场景。给一个宽限期。
            for k in list(self.function_return.keys()):
                if k in self.pending_requests:
                    continue
                if now - self._orphan_stored_at.get(k, now) > grace:
                    self.function_return.pop(k, None)
                    self._orphan_stored_at.pop(k, None)

    def _register_pending(self, echo: str) -> float:
        """登记一个待响应的 echo，返回发起时间。

        **必须在发送之前登记**：若先发送后登记，回包可能在登记完成前
        就到达（本地 WS 回包很快）→ 收包线程的 fast path 查不到这个
        echo → **响应被直接丢弃** → 等待方只能等满 max_wait_time 才
        返回 None，且全程无任何报错。

        Parameters
        ----------
        echo : str
            本次请求的 echo id。

        Returns
        -------
        float
            发起时间（``time.time()``）。
        """
        start_time = time.time()
        with self._lock:
            self.pending_requests[echo] = start_time
        return start_time

    def _store_response(self, echo: str, message_data: dict) -> None:
        """存放一个 API 响应（收包线程调用）。

        **无条件存放**：即便 ``pending_requests`` 里还没有这个 echo
        （响应比登记先到），也必须存下来 —— 否则请求方永远等不到，
        只能等满超时。早到的响应会由 ``_cleanup_pending_requests`` 的
        兜底清理机制回收（见下方「孤儿响应」说明）。

        「孤儿响应」：理论上 echo 是 uuid，不会撞上别的请求；但如果
        收到的是**别的**客户端发来的 echo（同一 WS 上多人共用），它会
        一直留在 ``function_return`` 里。这类条目由
        ``_cleanup_pending_requests`` 按「pending 里没有且超过宽限期」
        回收，不无限增长。
        """
        with self._lock:
            self.function_return[echo] = message_data
            # 记录存入时刻 —— 供「孤儿响应」回收判断宽限期用
            self._orphan_stored_at[echo] = time.time()

    def _take_response(self, echo: str) -> Optional[dict]:
        """取走一个 API 响应（原子：不存在返回 None）。

        用单次 ``pop`` 而非「先 ``in`` 再 ``[]``」—— 后者是两次 dict
        访问，中间收包线程/清理逻辑可能把条目删掉 → **KeyError**。
        """
        with self._lock:
            data = self.function_return.pop(echo, None)
            if data is not None:
                self._orphan_stored_at.pop(echo, None)
            return data

    def _has_response(self, echo: str) -> bool:
        """是否已有该 echo 的响应。"""
        with self._lock:
            return echo in self.function_return

    def _forget(self, echo: str) -> None:
        """清掉一个 echo 的全部记录（请求结束或超时时调用）。"""
        with self._lock:
            self.function_return.pop(echo, None)
            self.pending_requests.pop(echo, None)
            self._orphan_stored_at.pop(echo, None)

    def __getattr__(self, name):
        # 内部属性（下划线开头、且不是 _get* 动态 API）缺失时必须抛
        # AttributeError，而不是静默返回一个 async handler —— 否则
        # 绕过 __init__ 构造的实例访问 self._lock 会拿到一个协程函数，
        # 后面 `with self._lock:` 才炸，错误信息完全指不到根因。
        if name.startswith("_") and not name.startswith("_get"):
            raise AttributeError(
                f"{type(self).__name__} 实例缺少内部属性 {name!r} "
                "（该实例可能绕过了 __init__ 构造）"
            )

        if (
            name.startswith("get_")
            or name.startswith("can_")
            or name.startswith("_get")
        ):

            async def handler(**kwargs):
                # For methods that are expected to return a value,
                # automatically add an echo id to track the response.

                self._cleanup_pending_requests()
                function_return_id = str(uuid.uuid4())
                kwargs["echo"] = function_return_id
                command_request = self.format_request(name, kwargs)

                # **先登记，后发送**：登记若晚于发送，回包可能在登记
                # 完成前就到达 → 收包线程的 fast path 查不到这个 echo
                # → 响应被直接丢弃 → 本次调用只能等满 max_wait_time
                # 后返回 None，且全程无任何报错。
                start_time = self._register_pending(function_return_id)

                try:
                    await self.send_message(command_request)

                    while True:
                        # 原子取走响应：单次 pop，不存在返回 None。
                        # （旧的「先 in 再 []」两段式读，中间条目可能被
                        #   收包线程/清理逻辑删掉 → KeyError 向上抛。）
                        response = self._take_response(function_return_id)
                        if response is not None:
                            return response

                        await asyncio.sleep(0.2)
                        if time.time() - start_time >= self.max_wait_time:
                            return None
                finally:
                    self._forget(function_return_id)

        else:

            async def handler(**kwargs):
                command_request = self.format_request(name, kwargs)
                await self.send_message(command_request)

        return handler


class QQWebSocketConnector(BasicConnector):
    """QQ WebSocket connector."""

    def __init__(self, server, config: Optional[BotConfig] = None):
        source_name = config.get_keys(["connector", "QQ", "source_name"], "QQ")
        super().__init__(
            source=source_name, parser=QQParser, server=server, config=config
        )
        self.ws_client = None

        connector_basic_name = self.server.tr("gugubot.connector.name")
        self.log_prefix = f"[{connector_basic_name}{self.source}]"

        # determine scheme using single parameter 'use_ssl' (boolean)
        self.use_ssl = config.get_keys(
            ["connector", "QQ", "connection", "use_ssl"], False
        )
        self.scheme = "wss" if self.use_ssl else "ws"
        self.url = self._build_url(config)

        self.token = config.get_keys(["connector", "QQ", "connection", "token"], None)
        self.reconnect = config.get_keys(
            ["connector", "QQ", "connection", "reconnect"], 5
        )
        self.ping_interval = config.get_keys(
            ["connector", "QQ", "connection", "ping_interval"], 20
        )
        self.ping_timeout = config.get_keys(
            ["connector", "QQ", "connection", "ping_timeout"], 10
        )
        self.verify = config.get_keys(["connector", "QQ", "connection", "verify"], True)
        self.ca_certs = config.get_keys(
            ["connector", "QQ", "connection", "ca_certs"], None
        )
        self.extra_sslopt = config.get_keys(
            ["connector", "QQ", "connection", "sslopt"], {}
        )

        self.bot = Bot(
            self._send_message,
            max_wait_time=config.get_keys(
                ["connector", "QQ", "connection", "max_wait_time"], 5
            ),
        )

    def _build_url(self, config):
        host = config.get_keys(["connector", "QQ", "connection", "host"], "127.0.0.1")
        port = config.get_keys(["connector", "QQ", "connection", "port"], 8080)
        post_path = config.get_keys(["connector", "QQ", "connection", "post_path"], "")
        url = f"{self.scheme}://{host}:{port}"
        if post_path:
            url += f"/{post_path}"

        return url

    async def connect(self) -> None:
        """Establish the WebSocket connection to the QQ (OneBot) server."""
        self.logger.info(
            f"{self.log_prefix} {self.server.tr('gugubot.connector.QQ.try_connect', url=self.url)}"
        )

        self.ws_client = WebSocketFactory.create_client(
            url=self.url,
            token=self.token,
            on_message=self._on_ws_message,
            on_open=self._on_open,
            on_error=self._on_error,
            on_close=self._on_close,
            logger=self.logger,
        )

        # 心跳参数来自配置（connector.QQ.connection.ping_interval /
        # ping_timeout）。此前这里硬编码 0/0，等于把用户在
        # default_config.yml 里配的 ping_interval: 5 / ping_timeout: 4
        # 完全忽略掉 —— L137-142 把值读进 self.ping_interval /
        # self.ping_timeout 后就再也没用过（死代码）。
        #
        # 与 bridge_connector.py 的做法保持一致：把配置值传下去。
        # 设为 0 仍可显式关闭心跳（websocket-client 的 run_forever
        # 里 ping_interval=0 即禁用）。
        self.ws_client.connect(
            reconnect=self.reconnect,
            ping_interval=self.ping_interval,
            ping_timeout=self.ping_timeout,
            use_ssl=self.use_ssl,
            verify=self.verify,
            ca_certs=self.ca_certs,
            extra_sslopt=self.extra_sslopt,
            thread_name="[GUGUBot]QQ_Connector",
        )

        self.logger.info(
            f"{self.log_prefix} {self.server.tr('gugubot.connector.QQ.start_connect')}"
        )

    def _on_open(self, _):
        self.logger.info(
            f"{self.log_prefix} {self.server.tr('gugubot.connector.QQ.connect_success')}"
        )

    def _on_error(self, _: Any, error: Exception) -> None:
        """Handle a WebSocket error.

        Parameters
        ----------
        _ : Any
            WebSocket instance (unused).
        error : Exception
            The error that occurred.
        """
        self.logger.error(
            f"{self.log_prefix} {self.server.tr('gugubot.connector.QQ.error_connect', error=error)}"
        )

    def _on_close(
        self, _: Any, status_code: Optional[int], reason: Optional[str]
    ) -> None:
        """Handle WebSocket connection close.

        Parameters
        ----------
        _ : Any
            WebSocket instance (unused).
        status_code : int or None
            The close status code.
        reason : str or None
            The close reason.
        """
        close_info = self.server.tr(
            "gugubot.connector.QQ.close_connect", status_code=status_code, reason=reason
        )
        self.logger.debug(f"{self.log_prefix} {close_info}")

    async def _send_message(self, message: Any) -> None:
        """Send a raw message through the WebSocket connection."""
        if self.ws_client and self.ws_client.is_connected():
            if self.ws_client.send(message):
                self.logger.debug(
                    self.server.tr("gugubot.connector.QQ.send_message", message=message)
                )
            else:
                self.logger.error(
                    self.server.tr(
                        "gugubot.connector.QQ.send_message_failed", error="发送失败"
                    )
                )
        else:
            self.logger.warning(self.server.tr("gugubot.connector.QQ.retry_connect"))

    def _strip_color_codes_from_message(self, message: list) -> list:
        """Strip Minecraft colour codes from text segments in a message list.

        Parameters
        ----------
        message : list
            Message segment list; each element is a dict with ``type`` and
            ``data`` keys.

        Returns
        -------
        list
            A new list with colour codes removed from ``text`` segments.
        """
        result = []
        for item in message:
            if isinstance(item, dict) and item.get("type") == "text":
                new_item = item.copy()
                new_data = item.get("data", {}).copy()
                if "text" in new_data:
                    new_data["text"] = strip_minecraft_color_codes(new_data["text"])
                new_item["data"] = new_data
                result.append(new_item)
            else:
                result.append(item)
        return result

    # Estimated lengths for non-text segment types
    _TYPE_LENGTHS = {"image": 100, "default": 20}

    def _get_item_length(self, item: dict) -> int:
        """Return the estimated character length of a single message segment."""
        if item.get("type") == "text":
            return len(item.get("data", {}).get("text", ""))
        return self._TYPE_LENGTHS.get(item.get("type"), self._TYPE_LENGTHS["default"])

    def _split_message(self, message: list, max_length: int = 2000) -> list[list]:
        """Split a message into parts that each fit within *max_length*."""
        # max_length 来自配置（connector.QQ.max_message_length），用户可能填 0
        # 或负数：此时 space=0 → chunk 恒为空串 → text 不缩短 → 死循环，
        # 会直接挂死 MCDR 事件循环（不是抛异常）。此处钳制为正整数。
        try:
            max_length = int(max_length)
        except (TypeError, ValueError):
            max_length = 2000
        if max_length <= 0:
            max_length = 2000

        total = sum(self._get_item_length(m) for m in message if isinstance(m, dict))
        if total <= max_length:
            return [message]

        result, current_part, current_len = [], [], 0

        def flush():
            nonlocal current_part, current_len
            if current_part:
                result.append(current_part)
                current_part, current_len = [], 0

        for item in message:
            if not isinstance(item, dict):
                continue

            if item.get("type") != "text":
                item_len = self._get_item_length(item)
                if current_len + item_len > max_length:
                    flush()
                current_part.append(item)
                current_len += item_len
                continue

            # Text segment — may need splitting across parts
            text = item.get("data", {}).get("text", "")
            while text:
                space = max_length - current_len
                if space <= 0:
                    flush()
                    space = max_length

                if len(text) <= space:
                    current_part.append({"type": "text", "data": {"text": text}})
                    current_len += len(text)
                    break

                # Prefer splitting at a newline boundary
                chunk = text[:space]
                if (pos := chunk.rfind("\n")) > space // 2:
                    chunk = text[: pos + 1]

                current_part.append({"type": "text", "data": {"text": chunk}})
                current_len += len(chunk)
                text = text[len(chunk) :]

        flush()
        return result

    async def send_message(self, processed_info: ProcessedInfo) -> None:
        if not self.enable:
            return

        # Prefer forward_group_ids; fall back to group_ids for backwards compat
        forward_group_ids = self.config.get_keys(
            ["connector", "QQ", "permissions", "forward_group_ids"], []
        )
        if not forward_group_ids or not any(forward_group_ids):
            forward_group_ids = self.config.get_keys(
                ["connector", "QQ", "permissions", "group_ids"], []
            )
        forward_group_target = {
            str(group_id): "group" for group_id in forward_group_ids if group_id
        }
        target = processed_info.target or forward_group_target

        message = processed_info.processed_message
        source = processed_info.source

        message = self._strip_color_codes_from_message(message)

        # Prepend a source prefix when the message originates outside QQ
        # （source_name 用户可自定义，不能硬编码 "QQ"）
        if not source.is_from(self.source) and source.origin and processed_info.sender:
            chat_templates = self.config.get_keys(
                ["connector", "QQ", "chat_templates"], []
            )

            # Pick a chat template (weighted random) or fall back to default
            if chat_templates and isinstance(chat_templates, list):
                if isinstance(chat_templates[0], dict):
                    # Format like: [{"template_string": weight}, ...]
                    templates = []
                    weights = []
                    for item in chat_templates:
                        for template_str, weight in item.items():
                            templates.append(template_str)
                            weights.append(
                                weight if isinstance(weight, (int, float)) else 1
                            )
                    # Weighted random selection
                    template = random.choices(templates, weights=weights, k=1)[0]
                else:
                    # Legacy format: auto-migrate to weighted dict format
                    new_templates = [{tmpl: 1} for tmpl in chat_templates]
                    self.config["connector"]["QQ"]["chat_templates"] = new_templates
                    self.config.save()
                    if self.logger:
                        self.logger.info(
                            "已自动将 chat_templates 从旧格式更新为新格式（默认权重为1）"
                        )
                    # Select from the (pre-migration) plain string list
                    template = random.choice(chat_templates)

                # {display_name} → source origin, {sender} → sender name
                formatted_text = template.format(
                    display_name=source.origin, sender=processed_info.sender
                )
            else:
                # Default format (backwards compatible)
                formatted_text = f"[{source.origin}] {processed_info.sender}: "

            source_message = CQHandler.parse(formatted_text)
            message = source_message + message

        # Join/leave messages have an empty sender — only show the source tag
        elif not source.is_from(self.source) and source.origin and processed_info.sender == "":
            message = CQHandler.parse(f"[{source.origin}] ") + message

        # Max message length before splitting (configurable, default 2000)
        max_message_length = self.config.get_keys(
            ["connector", "QQ", "max_message_length"], 2000
        )

        message_parts = self._split_message(message, max_length=max_message_length)

        for target_id, target_type in target.items():
            if not target_id.isdigit():
                continue

            for part in message_parts:
                if target_type == "group":
                    await self.bot.send_group_msg(group_id=int(target_id), message=part)
                elif target_type == "private":
                    await self.bot.send_private_msg(
                        user_id=int(target_id), message=part
                    )
                else:
                    await self.bot.send_temp_msg(
                        group_id=int(target_type), user_id=int(target_id), message=part
                    )

                # Throttle multi-part sends to avoid rate limiting
                if len(message_parts) > 1:
                    random_time = random.uniform(0.5, 1.5)
                    await asyncio.sleep(random_time)

    async def disconnect(self) -> None:
        """Disconnect from the QQ WebSocket server."""
        try:
            if self.ws_client:
                self.ws_client.disconnect(timeout=5)
            self.logger.info(
                f"{self.log_prefix} {self.server.tr('gugubot.connector.QQ.close_info')}"
            )
        except Exception as e:
            error_msg = str(e) + "\n" + traceback.format_exc()
            error_close_template = self.server.tr(
                "gugubot.connector.QQ.error_close", error=error_msg
            )
            self.logger.warning(f"{self.log_prefix} {error_close_template}")
            raise

    async def on_message(self, raw: Any) -> None:
        """Handle a raw incoming message (base-class interface).

        Delegates to :meth:`_on_ws_message` which contains the actual
        processing logic shared with the WebSocket callback.
        """
        self._on_ws_message(None, raw)

    def _on_ws_message(self, _, raw_message: str) -> None:
        """Dispatch an incoming WebSocket message.

        This method is used as the WebSocket ``on_message`` callback and
        is also called by :meth:`on_message`.

        Processing flow:

        1. Parse the raw JSON string.
        2. If it is an API response (contains ``echo``), store it in
           ``function_return`` for the waiting coroutine.
        3. Otherwise schedule the event for processing on the MCDR event
           loop.

        Parameters
        ----------
        raw_message : str
            JSON-encoded message received from the WebSocket.
        """
        if not self.enable:
            return

        try:

            # Fast path: handle API responses (echo) directly in this thread
            try:
                message_data = json.loads(raw_message)
                echo = message_data.get("echo")
                if echo:
                    # API response — store for the awaiting coroutine.
                    # **无条件存储**：旧实现先查 ``pending_requests``，
                    # 不在就直接丢弃 —— 但响应完全可能**先于**登记到达
                    # （本地 WS 回包很快），于是响应被丢掉、请求方只能
                    # 等满 max_wait_time 后拿到 None，且全程无报错。
                    # 现在「先登记后发送」已把窗口关掉，这里再无条件
                    # 兜一层，双保险。
                    self.bot._store_response(echo, message_data)
                    return
            except Exception:
                pass

            # Event messages are scheduled on the MCDR event loop
            if self.parser is not None:
                self.server.schedule_task(self.parser.process_message(raw_message))

        except Exception as e:
            # Log with translation key and include the full traceback
            error_msg = str(e) + "\n" + traceback.format_exc()
            self.logger.error(
                self.server.tr(
                    "gugubot.connector.QQ.message_handle_failed", error=error_msg
                )
            )
