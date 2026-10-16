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

# 每个群池子最多保留的 msg_id 数（防内存膨胀；活跃群 5 分钟窗口内
# 64 条不够用——每条可回 5 次，256 条 x 5 次 = 1280 次被动额度足够）
_POOL_LIMIT = 256
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
                if event_id and not pool[msg_id].get("event_id"):
                    pool[msg_id]["event_id"] = event_id
                    pool[msg_id].setdefault("event_seq", 0)
                return
            while len(pool) >= _POOL_LIMIT:
                pool.popitem(last=False)
            info = {"seq": 0, "ts": time.time()}
            if event_id:
                info["event_id"] = event_id
                info["event_seq"] = 0  # event_id 同样受 5 次被动回复限制
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

            # 只读迭代；用尽条目先记后删（避免迭代期变更 OrderedDict）
            to_drop = []
            for msg_id in reversed(pool):
                info = pool[msg_id]
                if now - info["ts"] > _MSG_TTL:
                    # 新到旧遍历，第一条过期则更旧的必然全过期 →
                    # 当前及更旧的条目一并回收（不误删尚未遍历到的更新条目）。
                    # keys 是「旧→新」插入序，当前条目及其之前（更旧）的才是
                    # 过期项，故取 keys[:idx+1]；取 keys[idx:] 会把尚未遍历到的
                    # 更新条目（含仍有 event 额度的）一并误删。
                    keys = list(pool.keys())
                    idx = keys.index(msg_id)
                    to_drop.extend(keys[: idx + 1])
                    break
                if info["seq"] >= _MAX_SEQ:
                    # msg_id 额度用尽：若 event_id 还有额度则保留条目
                    # （take_event 还要用），否则可回收
                    if not info.get("event_id") or info.get("event_seq", 0) >= _MAX_SEQ:
                        to_drop.append(msg_id)
                    continue  # 继续找次新的
                seq = info["seq"]
                info["seq"] = seq + 1
                for m in to_drop:
                    pool.pop(m, None)
                return msg_id, seq

            # 未找到可用条目：仍清理本轮发现的用尽项
            for m in to_drop:
                pool.pop(m, None)
            return None

    def take_event(self, group_openid: str) -> Optional[str]:
        """取最新的 event_id（外层事件 id 被动回复备用通道）。

        msg_id 通道用尽/过期时使用。event_id 与 msg_id 一样每个事件最多
        被动回复 5 次，因此这里同样递增计数，超出即跳过（否则会反复拿到
        同一个已耗尽的 event_id，每次都被官方拒绝）。
        """
        now = time.time()
        with self._lock:
            pool = self._pools.get(group_openid)
            if not pool:
                return None
            to_drop = []
            for msg_id in reversed(pool):
                info = pool[msg_id]
                if now - info["ts"] > _MSG_TTL:
                    break  # 新到旧遍历，第一条过期则更旧的必然过期
                eid = info.get("event_id", "")
                if not eid:
                    continue
                seq = info.get("event_seq", 0)
                if seq >= _MAX_SEQ:
                    to_drop.append(msg_id)
                    continue
                info["event_seq"] = seq + 1
                for m in to_drop:
                    del pool[m]
                return eid
            for m in to_drop:
                pool.pop(m, None)
            return None

    def drop(self, group_openid: str, msg_id: str) -> None:
        """某 msg_id 回复报错（如序列号错误）时丢弃。"""
        with self._lock:
            pool = self._pools.get(group_openid)
            if pool and msg_id in pool:
                del pool[msg_id]
                if not pool:
                    del self._pools[group_openid]

    def drop_event(self, group_openid: str, event_id: str) -> None:
        """event_id 通道被官方拒绝时，摘掉所有引用该 event_id 的条目的
        event_id 字段。

        一个 event 可能对应池中多条 msg_id；只清 event_id 而不删条目，
        可保留这些条目尚未用尽的 msg_id 额度（两条通道相互独立）。
        """
        if not event_id:
            return
        with self._lock:
            pool = self._pools.get(group_openid)
            if not pool:
                return
            for info in pool.values():
                if info.get("event_id") == event_id:
                    info["event_id"] = ""
                    info["event_seq"] = _MAX_SEQ  # 标记该通道已废

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
                    if now - info["ts"] > _MSG_TTL
                    or (
                        info["seq"] >= _MAX_SEQ
                        # 两条通道都耗尽才算废条目：event_id 仍有额度时保留
                        and (
                            not info.get("event_id")
                            or info.get("event_seq", 0) >= _MAX_SEQ
                        )
                    )
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
