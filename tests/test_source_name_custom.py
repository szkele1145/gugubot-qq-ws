# -*- coding: utf-8 -*-
"""自定义 ``connector.QQ.source_name`` 时的全链路适配测试。

背景
----
绑定写入 PlayerManager 时用的平台键是 ``broadcast_info.source.origin``，
也就是用户自定义的 ``connector.QQ.source_name``（默认 "QQ"）。早期实现
多处硬编码字符串 ``"QQ"``：

- ``bound.py``：解绑 / 多余绑定检查 / 退群清理 / 群名片设置 / 查询，
  全部直接操作 ``player.accounts["QQ"]`` → 用户改名后**解绑无效、
  查询查不到、群名片功能静默失效**；
- ``echo.py``：``source.is_from("QQ")`` → 改名后 **QQ 私聊会被广播到
  MC 服**（本该只私聊不广播）；
- ``qq_connector.py`` / ``qq_official/connector.py``：
  ``source.is_from("QQ")`` → 改名后**转发到 QQ 的消息会带上多余的
  ``[来源] 发送者:`` 前缀**；
- ``bound.py`` 退群事件分发：``is_from("QQ")`` → 改名后退群清理不触发。

另：``parser._mentions_to_segments`` 把 openid 原文直接塞进 at 段的
``data.qq``（与另一处原位展开用虚拟号不一致）→ 同一用户产生两条不同
标识的绑定。已删除该死代码（无任何调用方）。

本文件覆盖：source_name 改名后，各处仍能正确识别 QQ 来源与平台键。
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

from gugubot.logic.system.bound import BoundSystem
from gugubot.logic.system.echo import EchoSystem
from gugubot.utils.types import BroadcastInfo, Source


class FL:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


class FC(dict):
    """模拟 BotConfig.get_keys 的嵌套取值。"""

    def get_keys(self, key, default=None):
        r = self
        for k in key[:-1]:
            r = r.get(k, {})
            if not isinstance(r, dict):
                return default
        return r.get(key[-1], default)


# ------------------------------------------------------------------ bound
def _make_bound(source_name="我的QQ", qq_accounts=None):
    """构造一个 BoundSystem 实例（不跑 __init__，只填必需字段）。"""
    bs = BoundSystem.__new__(BoundSystem)
    bs.logger = FL()
    bs.enable = True
    bs.name = "bound"
    bs.config = FC()
    bs.config["connector"] = {
        "QQ": {
            "source_name": source_name,
            "permissions": {"admin_group_ids": [], "group_ids": []},
            "others": {"change_group_card": True},
        }
    }
    bs._qq_platform_keys = BoundSystem._qq_platform_keys.__get__(bs)

    class PM:
        def __init__(self):
            self.players = []
            self.removed = []

        def get_player(self, identifier, platform=None, name_only=False):
            for p in self.players:
                if platform is None:
                    if identifier == p.name:
                        return p
                    for accts in p.accounts.values():
                        if identifier in accts:
                            return p
                    continue
                for plat, accts in p.accounts.items():
                    if plat == platform and identifier in accts:
                        return p
            return None

        def get_all_players(self):
            return list(self.players)

        def remove_player(self, name):
            self.removed.append(name)
            self.players = [p for p in self.players if p.name != name]

        def save(self):
            pass

    bs.player_manager = PM()
    return bs


def test_qq_platform_keys_default():
    """默认 source_name="QQ" → 候选键只有 ["QQ"]。"""
    bs = _make_bound(source_name="QQ")
    assert bs._qq_platform_keys() == ["QQ"]


def test_qq_platform_keys_custom_includes_legacy():
    """自定义 source_name → 候选键 = [自定义, "QQ"（历史兼容）]。"""
    bs = _make_bound(source_name="我的QQ")
    keys = bs._qq_platform_keys()
    assert keys[0] == "我的QQ", "当前键必须优先"
    assert "QQ" in keys, "必须兼容历史键 QQ"


def test_qq_platform_keys_empty_fallback():
    """配置为空串 → 回退到 "QQ"，不能返回空列表。"""
    bs = _make_bound(source_name="")
    assert bs._qq_platform_keys() == ["QQ"]


# ------------------------------------------------------- bound 解绑/清理
def test_accounts_query_uses_custom_key():
    """绑定查询：accounts 在自定义键下时，也能查到绑定玩家名。"""
    bs = _make_bound(source_name="我的QQ")

    class P:
        name = "Steve"
        java_name = ["Steve"]
        bedrock_name = []
        accounts = {"我的QQ": ["100000001"], "Discord": ["dc1"]}

    bs.player_manager.players = [P()]

    # 排除 QQ 平台后的玩家名（自定义键 + 历史键都要被排除）
    qq_keys = {k.lower() for k in bs._qq_platform_keys()}
    bound_players = []
    for platform, accounts in P.accounts.items():
        if str(platform).lower() in qq_keys:
            continue
        bound_players.extend(accounts)
    assert bound_players == ["dc1"], "QQ 平台（自定义键）应被排除"


def _info(source, sub_type="private", source_id="100000001"):
    return BroadcastInfo(
        raw=None,
        message=[{"type": "text", "data": {"text": "hi"}}],
        _source=Source(source),
        source_id=source_id,
        sender="某人",
        sender_id="100000001",
        event_type="message",
        event_sub_type=sub_type,
        is_admin=False,
    )


# --------------------------------------- bound 多余绑定：调用真实方法
def _bind_player(bs, accounts):
    class P:
        name = "Steve"
        java_name = ["Steve"]
        bedrock_name = []

        def __init__(self, acc):
            self.accounts = acc

    p = P(accounts)
    bs.player_manager.players = [p]
    return p


def test_get_extra_bound_member_finds_custom_key_account():
    """真实方法：账号存在自定义键下，也应被判定为「不在群里」。

    此前只读 accounts["QQ"] → 自定义键下的绑定**完全查不到**，
    多余绑定检查形同虚设（脏绑定永远清不掉）。
    """
    bs = _make_bound(source_name="我的QQ")
    _bind_player(bs, {"我的QQ": ["100000001"]})

    async def _members():
        return ["999999999"]  # 群里没有这个人

    bs._get_member_in_all_groups = _members

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(bs._get_extra_bound_member())
    finally:
        loop.close()

    assert result is not None, "成员列表可用时不应返回 None"
    assert result == [("100000001", ["Steve"])], (
        f"自定义键下的绑定应被检出为多余绑定，实际: {result}"
    )


def test_get_extra_bound_member_finds_legacy_key_account():
    """历史键 accounts["QQ"] 同样要能检出（改名之前的旧数据）。"""
    bs = _make_bound(source_name="我的QQ")
    _bind_player(bs, {"QQ": ["100000002"]})

    async def _members():
        return []

    bs._get_member_in_all_groups = _members

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(bs._get_extra_bound_member())
    finally:
        loop.close()

    assert result == [("100000002", ["Steve"])]


def test_remove_extra_bound_deletes_custom_key_account():
    """真实方法：移除多余绑定时，自定义键下的账号应被真正删掉。"""
    bs = _make_bound(source_name="我的QQ")
    player = _bind_player(bs, {"我的QQ": ["100000001"]})

    async def _members():
        return []

    bs._get_member_in_all_groups = _members
    bs._remove_from_whitelist = lambda names: None

    replies = []

    async def _reply(bi, msg):
        replies.append(msg)

    bs.reply = _reply

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            bs._handle_remove_extra_bound(_info("我的QQ", "group"))
        )
    finally:
        loop.close()

    assert "100000001" not in player.accounts.get("我的QQ", []), (
        "自定义键下的账号应被删除"
    )
    assert "我的QQ" not in player.accounts or not player.accounts["我的QQ"]


def test_remove_extra_bound_deletes_legacy_key_account():
    """历史键 accounts["QQ"] 也要能被移除。"""
    bs = _make_bound(source_name="我的QQ")
    player = _bind_player(bs, {"QQ": ["100000003"]})

    async def _members():
        return []

    bs._get_member_in_all_groups = _members
    bs._remove_from_whitelist = lambda names: None

    async def _reply(bi, msg):
        pass

    bs.reply = _reply

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            bs._handle_remove_extra_bound(_info("我的QQ", "group"))
        )
    finally:
        loop.close()

    assert "100000003" not in player.accounts.get("QQ", [])


# ------------------------------------------------------------------ echo
def _make_echo(source_name="我的QQ"):
    es = EchoSystem.__new__(EchoSystem)
    es.logger = FL()
    es.enable = True
    es.name = "echo"
    es.config = FC()
    es.config["connector"] = {
        "QQ": {
            "source_name": source_name,
            "permissions": {"admin_group_ids": [], "group_ids": []},
        }
    }
    es.system_manager = None
    return es


def test_echo_private_from_custom_source_not_broadcast():
    """自定义 source_name 后，QQ 私聊**不能**被广播到 MC。

    此前用硬编码 is_from("QQ") → 改名后私聊被当成普通消息广播出去。
    """
    es = _make_echo("我的QQ")
    # handle_enable_disable 需要 enable/disable 命令判定，这里直接打桩
    async def _no_cmd(bi):
        return False

    es.handle_enable_disable = _no_cmd
    sent = []

    class CM:
        def get_connector(self, name):
            return None

        async def broadcast_processed_info(self, info, exclude=None, include=None):
            sent.append(info)

    es.system_manager = type("SM", (), {"connector_manager": CM()})()

    loop = asyncio.new_event_loop()
    try:
        handled = loop.run_until_complete(
            es.process_broadcast_info(_info("我的QQ", "private"))
        )
    finally:
        loop.close()

    assert handled is False, "QQ 私聊应被 echo 直接忽略（不广播）"
    assert sent == [], "QQ 私聊不应被广播到 MC"


def test_echo_group_from_custom_source_still_processed():
    """对照组：自定义 source_name 的群消息仍走正常处理（不被误拦）。"""
    es = _make_echo("我的QQ")
    sent = []

    async def _no_cmd(bi):
        return False

    es.handle_enable_disable = _no_cmd

    class Conn:
        source = "Minecraft"
        enable_receive = True
        enable_send = True

    class CM:
        connectors = [Conn()]

        def get_connector(self, name):
            return None

        async def broadcast_processed_info(self, info, exclude=None, include=None):
            sent.append(info)

    es.system_manager = type("SM", (), {"connector_manager": CM()})()

    loop = asyncio.new_event_loop()
    try:
        handled = loop.run_until_complete(
            es.process_broadcast_info(_info("我的QQ", "group"))
        )
    finally:
        loop.close()

    assert handled is True, "QQ 群消息应被 echo 处理（非私聊路径）"
    assert sent, "QQ 群消息应被广播（非私聊路径）"


# ------------------------------------------------- parser 死代码已删除
def test_mentions_to_segments_removed():
    """``_mentions_to_segments`` 曾把 openid 原文塞进 at 段（与原位展开
    的虚拟号不一致 → 同一用户两条绑定），已删除。"""
    from gugubot.connector.qq_official.parser import QQOfficialParser

    assert not hasattr(QQOfficialParser, "_mentions_to_segments")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
