# -*- coding: utf-8 -*-
"""Gensokyo 式 msg_id 回复池 —— 绕开官方主动消息限额。

QQ 官方机器人「主动消息」有严格限额（群聊默认每月 4 条/群），但
**被动回复**不受限：每条收到的消息带一个 msg_id，回复时携带
``msg_id + msg_seq``（同一 msg_id 可回复 5 次，msg_seq 0~4）即可无限回复。

Gensokyo 的做法是把收到消息的 msg_id 存入池子，之后所有「主动」
推送（如 MC 聊天转发、玩家进出通知）都挂到**目标群池子里最新**的
msg_id 上，变成被动回复，从而绕开主动消息限额。

本实现：
- 按群（group_openid）维护 msg_id 池（有序、去重、FIFO 上限）；
- ``take()`` 弹出池中最新的 msg_id 及下一个可用 msg_seq；
- 线程安全；仅在收到 GROUP_AT_MESSAGE / C2C 事件时入池。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Optional, Tuple

# 每个群池子最多保留的 msg_id 数（防止内存膨胀）
_POOL_LIMIT = 64
# 单个 msg_id 官方最多允许回复 5 次（msg_seq 0-4）
_MAX_SEQ = 5
# msg_id 有效期（官方约 5 分钟，提前 30s 弃用）
_MSG_TTL = 4 * 60 + 30


class QQReplyPool:
    """按群维护的 msg_id 被动回复池。"""

    def __init__(self, logger=None) -> None:
        self._logger = logger
        self._lock = threading.RLock()
        # group_openid -> OrderedDict[msg_id, {"seq": int, "ts": float}]
        self._pools: "OrderedDict[str, OrderedDict]" = {}

    # ------------------------------------------------------------------ #

    def add(self, group_openid: str, msg_id: str, event_id: str = "") -> None:
        """收到消息时入池（可附带事件外层 id 作为备用被动回复通道）。"""
        if not group_openid or not msg_id:
            return
        with self._lock:
            pool = self._pools.setdefault(group_openid, OrderedDict())
            if msg_id in pool:
                # 已存在：补充 event_id（可能事件先到、后来才有外层id）
                if event_id:
                    pool[msg_id]["event_id"] = event_id
                return
            while len(pool) >= _POOL_LIMIT:
                pool.popitem(last=False)
            info = {"seq": 0, "ts": time.time()}
            if event_id:
                info["event_id"] = event_id
            pool[msg_id] = info

    def take(self, group_openid: str) -> Optional[Tuple[str, int]]:
        """取池中最新的 msg_id 和下一个 msg_seq。

        优先取最新的（最新的一定还在被动回复窗口内），其 msg_seq 用尽
        后回退到次新的，依此类推；池空或全部过期返回 None。
        """
        now = time.time()
        with self._lock:
            pool = self._pools.get(group_openid)
            if not pool:
                return None

            keys = list(pool.keys())
            # 从最新往旧找
            for msg_id in reversed(keys):
                info = pool[msg_id]
                if now - info["ts"] > _MSG_TTL:
                    # 过期：删掉继续找更旧的没有意义，直接清空并退出
                    del pool[msg_id]
                    continue
                if info["seq"] >= _MAX_SEQ:
                    del pool[msg_id]
                    continue
                seq = info["seq"]
                info["seq"] = seq + 1
                return msg_id, seq

            return None

    def take_event(self, group_openid: str) -> Optional[str]:
        """取最新的 event_id（外层事件 id 被动回复备用通道）。

        msg_id 通道用尽/过期时使用；event_id 每个事件也受 5 次限制，
        这里不做次数追踪（超限时官方报错，drop 即可）。
        """
        now = time.time()
        with self._lock:
            pool = self._pools.get(group_openid)
            if not pool:
                return None
            for msg_id in reversed(list(pool.keys())):
                info = pool[msg_id]
                eid = info.get("event_id", "")
                if eid and now - info["ts"] <= _MSG_TTL:
                    return eid
            return None

    def drop(self, group_openid: str, msg_id: str) -> None:
        """某 msg_id 回复报错（如序列号错误）时丢弃。"""
        with self._lock:
            pool = self._pools.get(group_openid)
            if pool and msg_id in pool:
                del pool[msg_id]

    def clear(self) -> None:
        with self._lock:
            self._pools.clear()

    def cleanup(self) -> int:
        """主动清理全部过期/用尽的 msg_id（心跳线程定期调用）。

        Returns
        -------
        int
            清理掉的条目数。
        """
        now = time.time()
        removed = 0
        with self._lock:
            for group_openid in list(self._pools.keys()):
                pool = self._pools[group_openid]
                stale = [
                    mid
                    for mid, info in pool.items()
                    if now - info["ts"] > _MSG_TTL or info["seq"] >= _MAX_SEQ
                ]
                for mid in stale:
                    del pool[mid]
                removed += len(stale)
                # 空池子整个移除，防小对象累积
                if not pool:
                    del self._pools[group_openid]
        return removed

    def size(self, group_openid: str) -> int:
        with self._lock:
            pool = self._pools.get(group_openid)
            return len(pool) if pool else 0
