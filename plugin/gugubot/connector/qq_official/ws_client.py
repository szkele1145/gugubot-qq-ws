# -*- coding: utf-8 -*-
"""QQ 官方开放平台 WebSocket 客户端。

官方 WS 协议（https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/websocket.html）：

连接 wss://api.sgroup.qq.com/websocket 后按 op 码交互：

- op:2  Hello        服务端下发，含 heartbeat_interval
- op:1  Identify     客户端发送鉴权（token, intents, shard）→ 回 op:11 Ready
- op:11 Heartbeat ACK
- op:0  Dispatch     事件推送（t=事件类型，s=序列号，d=事件数据）
- op:10 Reconnect    服务端要求重连（换 wss 地址）
- op:7  Resume       断线恢复会话（session_id + last_seq）
- op:9  Invalid Session
- op:1(客户端)  心跳包：{"op":1,"d":last_seq}，每 heartbeat_interval 一次

intents（订阅位）：
- PUBLIC_GUILD_MESSAGES (1<<30) 频道消息
- DIRECT_MESSAGE     (1<<12)   私信
- GROUP_AT_MESSAGES   (1<<0)    群聊 @ 消息
- C2C_MESSAGE_CREATE  (1<<4)    单聊消息
- INTERACTION         (1<<26)   交互事件

鉴权时 token 必须带 ``QQBot `` 前缀。
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from typing import Any, Callable, Optional

import websocket

from gugubot.connector.qq_official.api_client import QQOfficialClient

# intents 位（官方文档 https://bot.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html）
# 注意：GROUP_AT_MESSAGE_CREATE / C2C_MESSAGE_CREATE / FRIEND_* / GROUP_ADD_ROBOT 等
# 全部属于 GROUP_AND_C2C_EVENT (1<<25)，需在开放平台开通"QQ群/单聊"能力
INTENT_GUILDS = 1 << 0                     # 频道信息（基础默认）
INTENT_GUILD_MEMBERS = 1 << 1               # 频道成员（基础默认）
INTENT_GUILD_MESSAGES = 1 << 9              # 频道消息（私域）
INTENT_DIRECT_MESSAGE = 1 << 12             # 频道私信
INTENT_GROUP_AND_C2C_EVENT = 1 << 25        # ★ 群/单聊事件（群@消息、全量群消息、C2C、好友、机器人进出群）
INTENT_INTERACTION = 1 << 26                # 交互事件
INTENT_MESSAGE_AUDIT = 1 << 27               # 消息审核
INTENT_PUBLIC_GUILD_MESSAGES = 1 << 30      # 频道@消息（公域，基础默认）

# 兼容旧名（历史上误定义，保留引用不破坏导入）
INTENT_GROUP_AT_MESSAGES = INTENT_GROUP_AND_C2C_EVENT
INTENT_C2C_MESSAGE_CREATE = INTENT_GROUP_AND_C2C_EVENT

_DEFAULT_WSS = "wss://api.sgroup.qq.com/websocket/"

# op 码（官方文档 https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/payload.html）
OP_DISPATCH = 0        # 服务端 → 客户端：事件推送（t=事件类型 s=序号）
OP_HEARTBEAT = 1       # 客户端 → 服务端：心跳；服务端回 op:11 ACK
OP_IDENTIFY = 2        # 客户端 → 服务端：鉴权（token, intents, shard）
OP_RESUME = 6          # 客户端 → 服务端：恢复会话（session_id + seq）
OP_RECONNECT = 7       # 服务端 → 客户端：要求断开重连（换 wss 地址）
OP_INVALID_SESSION = 9 # 服务端 → 客户端：会话失效（d=false 时重新 Identify）
OP_HELLO = 10          # 服务端 → 客户端：连接建立后下发（含 heartbeat_interval）
OP_HEARTBEAT_ACK = 11  # 服务端 → 客户端：心跳确认
OP_HTTP_CALLBACK_ACK = 12
OP_RESUME_ACK = 13


class QQOfficialWSClient:
    """官方 WS 客户端：op 状态机 + 心跳线程 + 指数退避重连。

    复用项目已有的 websocket-client 库（WebSocketApp 事件模型与
    OneBot 场景一致），在其上实现官方 op 分发。
    """

    def __init__(
        self,
        api_client: QQOfficialClient,
        token_manager,
        intents: int,
        logger=None,
        on_dispatch: Optional[Callable[[str, int, dict], None]] = None,
        on_ready: Optional[Callable[[dict], None]] = None,
        reply_pool=None,
    ) -> None:
        self._api = api_client
        self._token_manager = token_manager
        self._intents = intents
        self._logger = logger
        self._on_dispatch = on_dispatch
        self._on_ready = on_ready
        self._pool = reply_pool  # 回复池引用（心跳时顺带清理）

        self.ws: Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._send_lock = threading.Lock()
        self._stop_event = threading.Event()

        # 会话状态
        self.session_id: Optional[str] = None
        self.last_seq: Optional[int] = None
        self._heartbeat_interval: float = 30.0
        self._ready = False
        # 心跳线程控制（代数 token 防竞态）
        self._hb_stop = threading.Event()
        self._hb_ack_miss = 0
        self._hb_generation = 0
        # 最近一次关闭码（4014 等鉴权类错误用长退避）
        self._last_close_code: Optional[int] = None

    # ------------------------------------------------------------------ #
    # 连接管理
    # ------------------------------------------------------------------ #

    def connect(self) -> None:
        """启动连接（阻塞获取 wss 地址，非阻塞启动 WS 线程）。

        幂等：若已有旧连接/旧线程，先彻底断开再重连（插件 reload 场景）。
        """
        # 清理旧连接（如有）
        if self.ws is not None or (
            self._ws_thread is not None and self._ws_thread.is_alive()
        ):
            self._stop_event.set()
            self._hb_stop.set()
            try:
                if self.ws:
                    self.ws.close()
            except Exception:
                pass
            if self._ws_thread and self._ws_thread.is_alive():
                self._ws_thread.join(timeout=3)

        self._stop_event.clear()
        self._hb_stop.clear()
        self._ready = False
        wss_url = self._fetch_wss_url()
        self._start_ws(wss_url)

    def _fetch_wss_url(self) -> str:
        try:
            data = self._api.get_wss_url()
            url = data.get("url")
            if url:
                return url
        except Exception as e:
            if self._logger:
                self._logger.warning(f"[QQ官方] 获取 wss 网关失败，使用默认地址: {e}")
        return _DEFAULT_WSS

    def _start_ws(self, url: str) -> None:
        self.ws = websocket.WebSocketApp(
            url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        t = threading.Thread(target=self._run_forever, name="[GUGUBot]QQ_Official_WS", daemon=True)
        self._ws_thread = t
        t.start()

    def _run_forever(self, *args, **kwargs) -> None:
        """带指数退避的 run_forever 包装。"""
        backoff = 3
        while not self._stop_event.is_set():
            try:
                self.ws.run_forever()
                # 4014（能力未开通）时用长退避，其余重置
                if self._last_close_code == 4014:
                    backoff = 60
                else:
                    backoff = 3  # 连接成功过则重置退避
            except Exception as e:
                if self._logger:
                    self._logger.error(f"[QQ官方] WS 运行异常: {e}\n{traceback.format_exc()}")
            if self._stop_event.is_set():
                return
            # on_close 里已打印；这里仅负责退避重连
            if self._logger:
                self._logger.info(f"[QQ官方] {backoff}s 后重新连接")
            self._stop_event.wait(backoff)
            backoff = min(backoff * 2, 60)

    # ------------------------------------------------------------------ #
    # WebSocket 回调
    # ------------------------------------------------------------------ #

    def _on_open(self, _) -> None:
        if self._logger:
            self._logger.info("[QQ官方] WebSocket 已连接，等待 Hello…")
        # 鉴权在收到 op:2 Hello 后进行（需要 heartbeat_interval）

    def _on_error(self, _, error) -> None:
        if self._logger:
            self._logger.warning(f"[QQ官方] WebSocket 错误: {error}")

    def _on_close(self, _, status_code, reason) -> None:
        self._ready = False
        self._last_close_code = status_code
        if status_code == 4014:
            # disallowed intents：机器人未开通对应能力，重连必然再被拒
            if self._logger:
                self._logger.error(
                    "[QQ官方] 连接被拒(4014 disallowed intents)：机器人未开通所订阅的消息能力。\n"
                    "  解决：q.qq.com → 机器人管理 → 开发/权限管理 → 申请并审核通过\n"
                    "  「QQ群」与「QQ单聊」能力；审核通过前可把配置里未开通的\n"
                    "  intents 关掉（connector.QQ.connection.official.intents\n"
                    "  下 group_at_messages / c2c_messages 设为 false）。\n"
                    "  60 秒后重试（等待你开通权限/修改配置）"
                )
        elif self._logger:
            self._logger.info(
                f"[QQ官方] WebSocket 关闭 (code={status_code} reason={reason})"
            )

    def _on_message(self, _, raw: str) -> None:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            if self._logger:
                self._logger.warning(f"[QQ官方] 收到非 JSON 消息: {raw[:200]}")
            return

        op = data.get("op")

        if op == OP_HELLO:  # 服务端 Hello：op:2
            self._heartbeat_interval = (
                (data.get("d") or {}).get("heartbeat_interval", 30000) / 1000.0
            )
            if self._logger:
                self._logger.info(
                    f"[QQ官方] 收到 Hello，心跳间隔 {self._heartbeat_interval}s"
                )
            if self.session_id:
                self._send_resume()
            else:
                self._send_identify()
            self._start_heartbeat()

        elif op == OP_DISPATCH:  # 事件
            self._handle_dispatch(data)

        elif op == OP_HEARTBEAT_ACK:
            self._hb_ack_miss = 0
            if self._logger:
                self._logger.debug("[QQ官方] 心跳 ACK")

        elif op == OP_RECONNECT:
            if self._logger:
                self._logger.warning("[QQ官方] 服务端要求重连（op:7）")
            self._schedule_reconnect()

        elif op == OP_INVALID_SESSION:
            if self._logger:
                self._logger.warning("[QQ官方] 会话失效（op:9），重新鉴权")
            self.session_id = None
            self.last_seq = None

        else:
            if self._logger:
                self._logger.debug(f"[QQ官方] 未知 op:{op} 消息: {str(data)[:200]}")

    def _handle_dispatch(self, data: dict) -> None:
        seq = data.get("s")
        if seq is not None:
            self.last_seq = seq

        event_type = data.get("t", "")
        payload = data.get("d") or {}

        if event_type == "READY":
            self.session_id = payload.get("session_id")
            user = payload.get("user") or {}
            self._ready = True
            if self._logger:
                self._logger.info(
                    f"[QQ官方] 鉴权成功（READY），bot 开放平台 ID: {user.get('id')}；"
                    f"等待群/单聊事件（在群里 @机器人 发消息即可触发绑定）"
                )
            if self._on_ready:
                try:
                    self._on_ready(payload)
                except Exception:
                    pass
            return

        if self._on_dispatch:
            try:
                self._on_dispatch(event_type, seq, payload)
            except Exception as e:
                if self._logger:
                    self._logger.error(f"[QQ官方] 事件处理异常: {e}\n{traceback.format_exc()}")

    # ------------------------------------------------------------------ #
    # 发送控制帧
    # ------------------------------------------------------------------ #

    def _send_json(self, payload: dict) -> bool:
        with self._send_lock:
            if self.ws and self.ws.sock and self.ws.sock.connected:
                try:
                    self.ws.send(json.dumps(payload))
                    return True
                except Exception as e:
                    if self._logger:
                        self._logger.warning(f"[QQ官方] 发送 WS 帧失败: {e}")
                    return False
            return False

    def _send_identify(self) -> None:
        token = f"QQBot {self._token_manager.get_token()}"
        payload = {
            "op": OP_IDENTIFY,
            "d": {
                "token": token,
                "intents": self._intents,
                "shard": [0, 1],
                "properties": {"$os": "linux", "$browser": "gugubot", "$device": "gugubot"},
            },
        }
        if self._logger:
            self._logger.info(
                f"[QQ官方] 发送 Identify（鉴权，intents={self._intents} "
                f"shard=[0,1]）"
            )
        self._send_json(payload)

    def _send_resume(self) -> None:
        token = f"QQBot {self._token_manager.get_token()}"
        payload = {
            "op": OP_RESUME,
            "d": {
                "token": token,
                "session_id": self.session_id,
                "seq": self.last_seq if self.last_seq is not None else 0,
            },
        }
        if self._logger:
            self._logger.info(f"[QQ官方] 发送 Resume（恢复会话 {self.session_id}）")
        self._send_json(payload)

    def _start_heartbeat(self) -> None:
        """（重）启动心跳线程（代数 token 版，无竞态）。

        每次 Hello 都调用。每代心跳线程捕获启动时的 ``_hb_generation``；
        旧的 join 超时未退出也不要紧——它醒来后发现代数已过期会立即
        退出，不会再发送心跳，彻底避免"双心跳并发"。
        连续 ``_HB_MAX_MISS`` 次无 ACK 主动断开，由 run_forever 退避重连。
        """
        # 递增代数：旧线程（无论是否还活着）代数过期自动退出
        self._hb_generation += 1
        gen = self._hb_generation
        self._hb_ack_miss = 0

        def _beat(gen=gen):
            while not self._stop_event.is_set():
                # 代数过期（新连接已建立新心跳）→ 本线程立即退役
                if gen != self._hb_generation:
                    return
                if self._hb_stop.wait(self._heartbeat_interval):
                    return
                if self._stop_event.is_set() or gen != self._hb_generation:
                    return
                # 顺带清理回复池过期条目（零成本防内存增长）
                try:
                    self._pool.cleanup()
                except Exception:
                    pass
                ok = self._send_json({"op": OP_HEARTBEAT, "d": self.last_seq})
                if not ok and self._logger:
                    self._logger.warning("[QQ官方] 心跳发送失败，连接可能已断开")
                    continue
                # 发送成功但连续 2 次无 ACK → 判死链路，主动断开触发重连
                self._hb_ack_miss += 1
                if self._hb_ack_miss >= 2:
                    if self._logger:
                        self._logger.warning("[QQ官方] 心跳连续无 ACK，判定连接失活，重连")
                    self._hb_ack_miss = 0
                    try:
                        if self.ws:
                            self.ws.close()
                    except Exception:
                        pass
                    return

        self._heartbeat_thread = threading.Thread(
            target=_beat, name="[GUGUBot]QQ_Official_HB", daemon=True
        )
        self._heartbeat_thread.start()

    # ------------------------------------------------------------------ #
    # 重连与关闭
    # ------------------------------------------------------------------ #

    def _schedule_reconnect(self) -> None:
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass

    def disconnect(self) -> None:
        self._stop_event.set()
        self._hb_stop.set()
        self._ready = False
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass

    def is_connected(self) -> bool:
        return bool(self._ready and self.ws and self.ws.sock and self.ws.sock.connected)

    @property
    def heartbeat_interval(self) -> float:
        return self._heartbeat_interval
