# -*- coding: utf-8 -*-
"""投票资格名单的两处缺陷（v2.2.10 修复）。

背景
----
``VoteSystem._get_eligible_voters()`` 负责在发起投票时生成资格名单：
在线玩家 ∩ 已绑定 QQ 账号。旧实现有两处真缺陷：

1. **一个未绑定的在线玩家 = 全员失去投票资格**
   遍历在线玩家时，只要 ``get_player(name)`` 查不到（玩家没绑定过），
   就直接 ``return set()`` —— 整个资格名单作废。而服务器上完全可能
   有陌生玩家（路人）在线，他们从未绑定 → 任何人发起投票都会得到
   空资格名单 → ``no_eligible_voters`` 直接拒绝发起；即便管理员绕过
   发起成功，后续每个人投票时也因 ``actual_voter_id not in
   vote.eligible_voters`` 被拒。设计意图（docstring 明写「在线 **且**
   已绑定」）是跳过未绑定者，``return set()`` 是实现失误。

2. **只查当前 source_name 单键，历史 "QQ" 键下的绑定查不到**
   与 ``bound.py`` 的 ``_qq_platform_keys()`` 同源：绑定数据的平台键
   是 ``broadcast_info.source.origin``（用户自定义的 source_name），
   历史数据可能存在默认 "QQ" 键下。旧实现只查当前键 → 改名后老玩家
   的绑定在投票资格里消失。

本文件直接调用**真实方法**（不打桩遍历逻辑），并保留「还原旧实现 →
失败 → 恢复 → 通过」闭环验证的对照断言入口。
"""
import sys
import pathlib
import types
import asyncio
import importlib.abc
import importlib.machinery

import pytest


class _A:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return _A()
    def __call__(self, *a, **k): return _A()


class _SM(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        v = _A()
        setattr(self, name, v)
        return v


class _L(importlib.abc.Loader):
    def create_module(self, s): return _SM(s.name)
    def exec_module(self, m): pass


class _F:
    def find_spec(self, f, p=None, t=None):
        if f.startswith("mcdreforged") or f.startswith("packaging"):
            return importlib.machinery.ModuleSpec(f, _L(), is_package=True)


sys.meta_path.insert(0, _F())
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "plugin"))

from gugubot.logic.system.vote import VoteSystem
from gugubot.utils.types import BroadcastInfo, Source


class FL:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


class FC(dict):
    def get_keys(self, key, default=None):
        r = self
        for k in key[:-1]:
            r = r.get(k, {})
            if not isinstance(r, dict):
                return default
        return r.get(key[-1], default)


def _mk_vote(source_name="QQ", players=None, online=None):
    """构造一个最小可用的 VoteSystem（__new__ 绕过 __init__ 的重依赖）。"""
    vs = VoteSystem.__new__(VoteSystem)
    vs.logger = FL()
    vs.enable = True
    vs.name = "vote"
    vs.debug_enabled = False
    vs.config = FC()
    vs.config["connector"] = {"QQ": {"source_name": source_name}}
    vs.config["GUGUBot"] = {}

    class P:
        def __init__(self, name, accounts):
            self.name = name
            self.java_name = [name]
            self.bedrock_name = []
            self.accounts = accounts

    class PM:
        def __init__(self, players):
            self._players = players

        def get_player(self, identifier, platform=None, name_only=False):
            for p in self._players:
                if p.name == identifier:
                    return p
            return None

    vs.player_manager = PM(players or [])
    vs._get_online_players = lambda: online or []
    return vs


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# --------------------------------------------------------------- 资格名单
def test_unbound_online_player_does_not_void_eligible_set():
    """一个未绑定的在线玩家，不能让整个资格名单作废。

    旧实现：遍历到未绑定玩家时 return set() → 全员失去资格。
    """
    vs = _mk_vote(
        players=[
            type("P", (), {"name": "Steve", "java_name": ["Steve"],
                           "bedrock_name": [], "accounts": {"QQ": ["1001"]}})()
        ],
        online=["Steve", "Guest_游客"],
    )
    eligible = _run(vs._get_eligible_voters())
    assert "1001" in eligible, "已绑定玩家 Steve 必须保留投票资格"
    assert eligible, "资格名单不能因未绑定玩家的存在而清空"


def test_fully_unbound_online_players_yield_empty_set():
    """全部在线玩家都未绑定 → 空资格名单（语义正确的对照组）。"""
    vs = _mk_vote(online=["Guest1", "Guest2"])
    eligible = _run(vs._get_eligible_voters())
    assert eligible == set()


def test_eligible_uses_legacy_qq_key():
    """历史键 accounts["QQ"] 下的绑定也要计入资格（自定义 source_name）。"""
    vs = _mk_vote(
        source_name="我的QQ",
        players=[
            type("P", (), {"name": "Alex", "java_name": ["Alex"],
                           "bedrock_name": [], "accounts": {"QQ": ["2002"]}})()
        ],
        online=["Alex"],
    )
    eligible = _run(vs._get_eligible_voters())
    assert "2002" in eligible, "历史 QQ 键下的绑定必须计入资格名单"


def test_eligible_uses_custom_key():
    """当前自定义键下的绑定计入资格名单。"""
    vs = _mk_vote(
        source_name="我的QQ",
        players=[
            type("P", (), {"name": "Alex", "java_name": ["Alex"],
                           "bedrock_name": [], "accounts": {"我的QQ": ["3003"]}})()
        ],
        online=["Alex"],
    )
    eligible = _run(vs._get_eligible_voters())
    assert "3003" in eligible


def test_eligible_merges_both_keys():
    """自定义键与历史键都有绑定时，两个都计入。"""
    vs = _mk_vote(
        source_name="我的QQ",
        players=[
            type("P", (), {"name": "Alex", "java_name": ["Alex"],
                           "bedrock_name": [],
                           "accounts": {"我的QQ": ["3003"], "QQ": ["2002"]}})()
        ],
        online=["Alex"],
    )
    eligible = _run(vs._get_eligible_voters())
    assert "3003" in eligible and "2002" in eligible


# ---------------------------------------------------- MC 侧投票者解析
def test_voter_ids_include_legacy_key():
    """``_get_voter_ids``（MC 侧投票 → 查绑定QQ）也要遍历候选键。"""
    vs = _mk_vote(
        source_name="我的QQ",
        players=[
            type("P", (), {"name": "Alex", "java_name": ["Alex"],
                           "bedrock_name": [], "accounts": {"QQ": ["2002"]}})()
        ],
    )
    ids = _run(vs._get_voter_ids("Alex"))
    assert "2002" in ids, "历史 QQ 键下的绑定必须能被解析为投票者ID"


def test_voter_ids_include_custom_key():
    vs = _mk_vote(
        source_name="我的QQ",
        players=[
            type("P", (), {"name": "Alex", "java_name": ["Alex"],
                           "bedrock_name": [], "accounts": {"我的QQ": ["3003"]}})()
        ],
    )
    ids = _run(vs._get_voter_ids("Alex"))
    assert "3003" in ids


# -------------------------------------------------- QQ 侧投票者解析（回归）
def test_qq_side_voter_id_uses_sender_id_directly():
    """QQ 侧消息：sender_id（虚拟号/真实号）直接当投票者ID（回归）。"""
    vs = _mk_vote()
    info = BroadcastInfo(
        raw=None,
        message=[{"type": "text", "data": {"text": "111"}}],
        _source=Source("QQ"),
        source_id="88888",
        sender="某人",
        sender_id="1001",
        event_type="message",
        event_sub_type="group",
        is_admin=False,
    )
    vid = _run(vs._get_voter_id_from_broadcast(info))
    assert vid == "1001"


def test_mc_side_voter_id_falls_back_to_binding():
    """MC 侧消息：sender_id 是玩家名 → 查绑定QQ（回归）。"""
    vs = _mk_vote(
        players=[
            type("P", (), {"name": "Steve", "java_name": ["Steve"],
                           "bedrock_name": [], "accounts": {"QQ": ["1001"]}})()
        ],
    )
    info = BroadcastInfo(
        raw=None,
        message=[{"type": "text", "data": {"text": "111"}}],
        _source=Source("Minecraft"),
        source_id=None,
        sender="Steve",
        sender_id="Steve",
        event_type="message",
        event_sub_type="public",
        is_admin=False,
    )
    vid = _run(vs._get_voter_id_from_broadcast(info))
    assert vid == "1001"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
