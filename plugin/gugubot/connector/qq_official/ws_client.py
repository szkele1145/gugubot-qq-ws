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

import inspect
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
# （op:12 HTTP 回调 ACK、op:13 回调地址验证仅 webhook 模式使用，WS 模式无此交互）

# WS 关闭码（官方文档《WebSocket 方式 · WebSocket 错误码》，2026-07-30 版）
#
#   4001 无效的 opcode          → 不可重试
#   4002 无效的 payload         → 不可重试
#   4006 无效的 session id      → 需重新 Identify
#   4007 seq 错误               → 需重新 Identify
#   4008 发送 payload 过快      → 可 Resume / Identify
#   4009 连接过期               → 可 Resume / Identify
#   4010 无效的 shard           → 不可重试
#   4011 需处理的 guild 过多    → 不可重试
#   4012 无效的 version         → 不可重试
#   4013 无效的 intent          → 不可重试
#   4014 intent 无权限          → 不可重试（等用户开通能力）
#   4900~4913 内部错误          → 需重新 Identify
#   4914 机器人已下架（仅沙箱） → 不可连接
#   4915 机器人已封禁           → 不可连接
#
# **必须清空 session_id/last_seq 的码**：带失效 session 去 Resume 会被
# 服务端反复拒绝，导致机器人永久离线（每次重连都拿旧 session 撞墙）。
_CLEAR_SESSION_CODES = frozenset(
    {4006, 4007, 4008, 4009} | set(range(4900, 4914))
)

# **不可重连**（重连必然再被拒，只会刷屏）：需要人工介入修配置/权限/账号
_FATAL_CLOSE_CODES = frozenset({
    4001, 4002, 4010, 4011, 4012, 4013,  # 协议/参数错误：改代码才能修
    4014,                                 # 能力未开通：等审核（单独长退避处理）
    4914, 4915,                           # 已下架 / 已封禁：联系官方解封
})

# 鉴权类：token/appid 错误，重连无意义
_AUTH_CLOSE_CODES = frozenset({4010, 4011, 4012, 4013})


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
        # on_dispatch 回调的参数个数（3 或 4）；None = 尚未探测
        self._dispatch_arg_count: Optional[int] = None

        self.ws: Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._send_lock = threading.Lock()
        self._stop_event = threading.Event()

        # 会话状态
        self.session_id: Optional[str] = None
        # 最新**收到**的 seq —— 用于心跳包 d（官方要求 d=客户端收到的
        # 最新消息 s）
        self.last_seq: Optional[int] = None
        # 最新**处理完成**的 seq —— 用于 Resume（官方文档：「推荐开发者
        # 在处理过事件之后记录下 s」）。与 last_seq 分离的意义：事件处理
        # 抛异常时该事件并未真正处理，若把 seq 记到 last_seq，Resume
        # 会跳过它 → 事件永久丢失。
        self._acked_seq: Optional[int] = None
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
            # 代数递增：即使 join 超时旧心跳线程未死，醒来也会因代数
            # 过期立即退役（_hb_stop 随后会被 clear，不能只靠它）
            self._hb_generation += 1
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
        """带指数退避的 run_forever 包装。

        每次重连前重新获取 wss 网关地址——官方 op:7 Reconnect 要求
        客户端换新地址（负载均衡调度）；固定旧地址可能反复命中故障节点。
        """
        backoff = 3
        stop = self._stop_event
        logger = self._logger
        first_run = True
        while not stop.is_set():
            if self._last_close_code == 4014:
                # 能力未开通：重连必被拒，长退避等用户修配置/权限
                if logger:
                    logger.info("[QQ官方] 60s 后重试（等待开通能力/修改配置）")
                stop.wait(60)
                continue
            try:
                # 重连（非首次）时才刷新网关地址：首次的地址已由 connect()
                # 取好，重复请求会白耗一次 /gateway REST 往返（该接口有频率限制）
                if not first_run:
                    new_url = self._fetch_wss_url()
                    self.ws = websocket.WebSocketApp(
                        new_url,
                        on_open=self._on_open,
                        on_message=self._on_message,
                        on_error=self._on_error,
                        on_close=self._on_close,
                    )
                first_run = False
                self.ws.run_forever()
                backoff = 3  # 连接成功过则重置退避
            except Exception as e:
                if logger:
                    logger.error(f"[QQ官方] WS 运行异常: {e}\n{traceback.format_exc()}")
            if stop.is_set():
                return
            # on_close 里已打印；这里仅负责退避重连
            if logger:
                logger.info(f"[QQ官方] {backoff}s 后重新连接")
            stop.wait(backoff)
            backoff = min(backoff * 2, 60)

    # ------------------------------------------------------------------ #
    # WebSocket 回调
    # ------------------------------------------------------------------ #

    def _on_open(self, _) -> None:
        if self._logger:
            self._logger.info("[QQ官方] WebSocket 已连接，等待 Hello…")
        # 鉴权在收到 op:2 Hello 后进行（需要 heartbeat_interval）
        # 连接成功 → 清除上一次的失败标记：否则若曾遇 4014，后续每次
        # 循环都会白白等 60s 长退避（尽管连接早已正常）。
        self._last_close_code = None

    def _on_error(self, _, error) -> None:
        if self._logger:
            self._logger.warning(f"[QQ官方] WebSocket 错误: {error}")

    def _on_close(self, _, status_code, reason) -> None:
        self._ready = False
        self._last_close_code = status_code
        logger = self._logger

        # ① 会话失效类：必须清空 session_id/last_seq，否则下次重连仍带
        #    旧 session 去 Resume → 服务端再次拒绝 → 机器人永久离线。
        if status_code in _CLEAR_SESSION_CODES:
            self.session_id = None
            self.last_seq = None
            self._acked_seq = None
            if logger:
                logger.warning(
                    f"[QQ官方] 关闭码 {status_code}：会话已失效，"
                    f"已清除 session_id/seq，下次连接将重新 Identify"
                )

        # ② 已下架 / 已封禁：官方明确「不可以连接」→ 停止重连，避免刷屏
        if status_code in (4914, 4915):
            if logger:
                logger.error(
                    f"[QQ官方] 连接被拒(code={status_code} reason={reason})："
                    + (
                        "机器人已下架（只允许连接沙箱环境），请检查当前连接环境。"
                        if status_code == 4914
                        else "机器人已被封禁，请联系官方申请解封后再连接。"
                    )
                    + " 已停止重连。"
                )
            self._stop_event.set()
            return

        if status_code == 4014:
            # disallowed intents：机器人未开通对应能力，重连必然再被拒
            if logger:
                logger.error(
                    "[QQ官方] 连接被拒(4014 disallowed intents)：机器人未开通所订阅的消息能力。\n"
                    "  解决：q.qq.com → 机器人管理 → 开发/权限管理 → 申请并审核通过\n"
                    "  「QQ群」与「QQ单聊」能力；审核通过前可把配置里未开通的\n"
                    "  intents 关掉（connector.QQ.connection.official.intents\n"
                    "  下 group_at_messages / c2c_messages 设为 false）。\n"
                    "  60 秒后重试（等待你开通权限/修改配置）"
                )
        elif status_code in _AUTH_CLOSE_CODES:
            # 鉴权/参数类错误：token 无效或 shard/version/intent 不合法
            # ——重连无意义，终止重试循环
            if logger:
                logger.error(
                    f"[QQ官方] 鉴权/参数失败(code={status_code} reason={reason})："
                    f"请检查 appid/secret 是否正确、token 是否过期、shard/intent 配置是否合法。"
                    f"停止重连。"
                )
            self._stop_event.set()  # 让 _run_forever 循环退出
        elif logger:
            logger.info(
                f"[QQ官方] WebSocket 关闭 (code={status_code} reason={reason})"
            )

    def _on_message(self, _, raw: str) -> None:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            logger = self._logger
            if logger:
                logger.warning(f"[QQ官方] 收到非 JSON 消息: {raw[:200]}")
            return

        op = data.get("op")
        logger = self._logger

        if op == OP_HELLO:  # 服务端 Hello：op:10
            raw_interval = (data.get("d") or {}).get("heartbeat_interval", 30000)
            # 异常数据兜底：0/负值会 wait(0) 空转风暴；超大值饿死心跳
            # 导致断连——夹到 1~300s（下限 1s 兼顾单测快速验证）
            try:
                self._heartbeat_interval = min(max(float(raw_interval) / 1000.0, 1.0), 300.0)
            except (TypeError, ValueError):
                self._heartbeat_interval = 30.0
            if logger:
                logger.info(f"[QQ官方] 收到 Hello，心跳间隔 {self._heartbeat_interval}s")
            if self.session_id:
                self._send_resume()
            else:
                self._send_identify()
            self._start_heartbeat()
        elif op == OP_DISPATCH:  # 事件
            try:
                self._handle_dispatch(data)
            except Exception as e:
                # 鉴权/解析异常不应杀死 WS 读循环；记录后由服务端超时踢触发重连
                if logger:
                    logger.error(f"[QQ官方] Dispatch 处理异常: {e}\n{traceback.format_exc()}")

        elif op == OP_HEARTBEAT_ACK:
            self._hb_ack_miss = 0
            if logger:
                logger.debug("[QQ官方] 心跳 ACK")

        elif op == OP_RECONNECT:
            if logger:
                logger.warning("[QQ官方] 服务端要求重连（op:7）")
            self._schedule_reconnect()

        elif op == OP_INVALID_SESSION:
            # 官方规范：d=false → resume 被拒，需全新 Identify；d=true 可重试 resume
            # d 的取值形态不保证是 JSON 布尔：可能是 false / "false" / 0，
            # 因此不能写 `d_val is False`（只认恰好是布尔 False 的对象）——
            # 漏判会让「已被官方判死的 session」被继续 Resume → 再次被拒 →
            # 死循环。统一按「显式假值」判定。
            d_val = data.get("d")
            is_invalid = d_val is False or d_val == 0 or (
                isinstance(d_val, str) and d_val.strip().lower() in ("false", "0")
            )
            if is_invalid:
                if logger:
                    logger.warning("[QQ官方] 会话失效（op:9 d=false），清除会话重新鉴权")
                self.session_id = None
                self.last_seq = None
                self._acked_seq = None
                # 主动断开重连：官方发 op:9 只说明「本 session 作废」，
                # 不保证随后会关闭连接。若不主动重连，Identify 永远发不出去
                # （Identify 只在新连接的 Hello 之后发）→ 连接看似还在、
                # 实则已死，只能等心跳失活兜底（间隔最长 300s →
                # 最坏情况离线近 10 分钟）。
                self._schedule_reconnect()
            else:
                if logger:
                    logger.warning("[QQ官方] 会话失效（op:9），稍后重试 Resume")
                # 保留 session_id/last_seq，等下一个 Hello 时 _send_resume 重试
                self._schedule_reconnect()

        else:
            if logger:
                logger.debug(f"[QQ官方] 未知 op:{op} 消息: {str(data)[:200]}")

    def _handle_dispatch(self, data: dict) -> None:
        seq = data.get("s")
        # 收到即记录：心跳包 d 要求「客户端收到的最新 s」，与是否处理完无关
        if seq is not None:
            self.last_seq = seq
        try:
            self._handle_dispatch_inner(data, seq)
        except Exception:
            # 处理失败 → **不**推进 _acked_seq：该事件未被真正处理，Resume
            # 时应当由服务端补发（若在这里也记 seq，事件就永久丢了）。
            raise
        else:
            # 处理完成才推进 Resume 位点（官方推荐做法）
            if seq is not None:
                self._acked_seq = seq

    def _probe_dispatch_arity(self) -> int:
        """探测 on_dispatch 回调可接受的参数个数（3 或 4）。

        用 ``inspect.signature`` 静态求值，不通过「试调用 + 捕获 TypeError」
        猜测——后者会把回调**内部**的业务 TypeError 误判成签名不匹配，
        导致同一事件被处理两次。

        探测失败（回调不是普通函数/签名不可读）时按 3 参处理（保守：
        多传参数会让老回调直接 TypeError，少传只是拿不到 event_id）。
        """
        callback = self._on_dispatch
        try:
            sig = inspect.signature(callback)
        except (TypeError, ValueError):
            return 3
        count = 0
        for p in sig.parameters.values():
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
                count += 1
            elif p.kind == p.VAR_POSITIONAL:
                # *args → 可接受任意个数，按 4 处理（多传的 event_id 会被忽略）
                return 4
        return count if count >= 3 else 3

    def _handle_dispatch_inner(self, data: dict, seq) -> None:
        event_type = data.get("t", "")
        payload = data.get("d") or {}

        if event_type == "READY":
            self.session_id = payload.get("session_id")
            user = payload.get("user") or {}
            self._ready = True
            # 注意：这里**不要**清位点——READY 事件本身带 s（=1），外层
            # 会统一记录 last_seq/_acked_seq；旧会话的位点作废已由
            # 「关闭码 4006/4007/4900-4913」与「op:9 d=false」负责清理。
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

        if event_type == "RESUMED":
            # Resume 成功：会话恢复，补发事件随后下发
            self._ready = True
            if self._logger:
                self._logger.info("[QQ官方] 会话恢复成功（RESUMED），开始接收补发事件")
            return

        if event_type in ("AT_MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"):
            # 频道类事件：官方群聊机器人常见场景不会订阅；若误订阅（配置
            # direct_message/guild_messages 开了但事件意外到达），显式忽略
            if self._logger:
                self._logger.debug(f"[QQ官方] 忽略频道事件 {event_type}")
            return

        if self._on_dispatch:
            try:
                # 事件外层 id（event_id）一并传递：备用被动回复通道。
                # 签名探测：早期版本用「先试 4 参、捕获 TypeError 再退回 3 参」
                # 的方式猜签名——但回调**内部**抛出的 TypeError 会被误判成
                # 「签名不对」→ 该事件被再处理一次（且首次调用的异常被吞）。
                # 尤其业务错误文案里含 "argument"/"positional" 时（如
                # "got an unexpected keyword argument"）必然误判。
                # 现改用 inspect.signature 一次性数出回调可接受的参数个数，
                # 判定不依赖任何异常。
                if self._dispatch_arg_count is None:
                    self._dispatch_arg_count = self._probe_dispatch_arity()
                event_id = data.get("id", "")
                if self._dispatch_arg_count >= 4:
                    self._on_dispatch(event_type, seq, payload, event_id)
                else:
                    self._on_dispatch(event_type, seq, payload)
            except Exception as e:
                if self._logger:
                    self._logger.error(f"[QQ官方] 事件处理异常: {e}\n{traceback.format_exc()}")
                # **重新抛出**：让调用方知道本事件未处理成功 → 不推进
                # Resume 位点（_acked_seq）→ 断线重连时服务端补发该事件。
                # 事件处理失败被静默吞掉 = 该事件永久丢失。
                raise

    # ------------------------------------------------------------------ #
    # 发送控制帧
    # ------------------------------------------------------------------ #

    def _send_json(self, payload: dict) -> bool:
        with self._send_lock:
            ws = self.ws
            sock = ws.sock if ws else None
            if sock and sock.connected:
                try:
                    ws.send(json.dumps(payload))
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
        # Resume 的 seq 用**处理完成**的位点（_acked_seq），不是收到的
        # （last_seq）——否则处理失败的事件会被跳过，永久丢失。
        resume_seq = self._acked_seq if self._acked_seq is not None else 0
        payload = {
            "op": OP_RESUME,
            "d": {
                "token": token,
                "session_id": self.session_id,
                "seq": resume_seq,
            },
        }
        if self._logger:
            self._logger.info(
                f"[QQ官方] 发送 Resume（恢复会话 {self.session_id}，seq={resume_seq}）"
            )
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
            # 闭包缓存：心跳为热路径（每隔 interval 秒执行一次），
            # 将稳定对象/方法缓为局部变量，避免每次循环的属性查找。
            stop = self._stop_event
            hb_stop = self._hb_stop
            interval = self._heartbeat_interval
            pool = self._pool
            logger = self._logger
            send_json = self._send_json

            while not stop.is_set():
                # 代数过期（新连接已建立新心跳）→ 本线程立即退役
                if gen != self._hb_generation:
                    return
                if hb_stop.wait(interval):
                    return
                if stop.is_set() or gen != self._hb_generation:
                    return
                # 顺带清理回复池过期条目（零成本防内存增长）
                try:
                    pool.cleanup()
                except Exception:
                    pass
                ok = send_json({"op": OP_HEARTBEAT, "d": self.last_seq})
                if not ok and logger:
                    logger.warning("[QQ官方] 心跳发送失败，连接可能已断开")
                # 发送失败与「发出但无 ACK」统一计入失活计数：两者都说明链路
                # 已死，累计 2 次即主动断开触发重连。（旧实现把 continue 挂在
                # `and logger` 分支里，导致 logger 为 None 时行为反而不一致）
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
        # 代数递增：彻底退役所有心跳线程（与 connect 幂等清理对齐）
        self._hb_generation += 1
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
