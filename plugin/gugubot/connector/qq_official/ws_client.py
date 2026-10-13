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

# intents 位
INTENT_PUBLIC_GUILD_MESSAGES = 1 << 30
INTENT_DIRECT_MESSAGE = 1 << 12
INTENT_GROUP_AT_MESSAGES = 1 << 0
INTENT_C2C_MESSAGE_CREATE = 1 << 4
INTENT_INTERACTION = 1 << 26

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
    ) -> None:
        self._api = api_client
        self._token_manager = token_manager
        self._intents = intents
        self._logger = logger
        self._on_dispatch = on_dispatch
        self._on_ready = on_ready

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
        # 心跳线程控制
        self._hb_stop = threading.Event()
        self._hb_ack_miss = 0

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
        if self._logger:
            self._logger.info(f"[QQ官方] WebSocket 关闭 (code={status_code} reason={reason})")

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
                    f"[QQ官方] 鉴权成功（READY），bot 开放平台 ID: {user.get('id')}"
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
            self._logger.info("[QQ官方] 发送 Identify（鉴权）")
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
        """（重）启动心跳线程。

        每次收到 Hello 都调用：先停掉旧线程（连接已更换，旧心跳作废），
        再起新线程。心跳连续 ``_HB_MAX_MISS`` 次未收到 ACK 主动断开，
        由 run_forever 的退避循环接管重连。
        """
        # 停旧线程（等它退出，最多等一个心跳周期的1/3，避免立即被拒收）
        self._hb_stop.set()
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=2)
        self._hb_stop.clear()
        self._hb_ack_miss = 0

        def _beat():
            while not self._stop_event.is_set() and not self._hb_stop.is_set():
                # 等待心跳间隔（可被终止事件提前唤醒）
                if self._hb_stop.wait(self._heartbeat_interval):
                    return
                if self._stop_event.is_set() or self._hb_stop.is_set():
                    return
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
