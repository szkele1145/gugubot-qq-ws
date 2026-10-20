# -*- coding: utf-8 -*-
"""bound.py 在 QQ 官方直连（@机器人）消息形态下的回归测试（v2.2.10）。

背景
----
QQ 官方直连里 @机器人 时，消息段是 ``[at 段, text 段]``（长度 2），
OneBot 的 @机器人 才是单段：

    QQ 官方：@机器人            → message = [at, text]   长度 2
    OneBot：@机器人 绑定 Steve   → message = [text]        长度 1

``bound.py`` 里有两处用 ``len(broadcast_info.message) == 1`` 判定
「空命令」：

- ``_handle_command``：空命令 → 回绑定系统帮助；
- ``_handle_bind``：空内容 → 回绑定用法提示（bind_instruction）。

在官方形态下这两个条件都不成立，命令会**继续往下走**，最终由
``_handle_bind`` 里的 ``if not player_name`` 分支回同一条
``bind_instruction``——**用户可见行为一致**（都拿到用法提示），
只是绕了一圈。因此这里不改动判定口径，改为加回归测试锁住
「官方 at 段形态下命令行为与 OneBot 单段形态一致」这一事实，
防止后续重构把提示语弄丢。

本文件覆盖：空命令的提示内容、带玩家名的绑定路径、非命令消息被忽略。
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


TR = {
    "name": "绑定",
    "bind": "绑定",
    "unbind": "解绑",
    "list": "列表",
    "search": "查询",
    "bind_instruction": "请提供要绑定的玩家名称",
    "bind_success": "绑定成功",
    "no_bindings": "您还没有绑定任何账号",
    "bind_existed": "该玩家已存在",
    "max_bound_reached": "绑定数量已达上限",
    "user_help_msg": "绑定系统帮助",
    "help_msg": "绑定系统帮助（管理员）",
    "gugubot.enable": "开启",
    "gugubot.disable": "关闭",
}


def _make_bound(source_name="QQ", bind_result=True):
    bs = BoundSystem.__new__(BoundSystem)
    bs.logger = FL()
    bs.enable = True
    bs.name = "bound"
    bs.config = FC()
    bs.config["GUGUBot"] = {"command_prefix": "#", "group_admin": False}
    bs.config["connector"] = {
        "QQ": {
            "source_name": source_name,
            "permissions": {"admin_group_ids": [], "group_ids": []},
            "others": {"change_group_card": True},
        }
    }
    bs.config["system"] = {"bound": {"max_platform_bound": 1}}
    bs._qq_platform_keys = BoundSystem._qq_platform_keys.__get__(bs)
    bs.replies = []
    bs.bound_calls = []

    class PM:
        def __init__(self):
            self.players = []
            self.saved = 0

        def get_player(self, identifier, platform=None, name_only=False):
            return None

        def get_all_players(self):
            return []

        def add_player_account(self, identifier, platform=None, account_id=None,
                              is_bedrock=False):
            bs.bound_calls.append((identifier, platform, account_id))
            return True

        def remove_player(self, name): pass

        def save(self):
            self.saved += 1

    bs.player_manager = PM()

    async def _reply(broadcast_info, message):
        bs.replies.append(message)

    bs.reply = _reply
    bs.get_tr = lambda key, **kw: TR.get(key, key)

    async def _bind_player(broadcast_info, player_name, platform, is_bedrock,
                           is_offline, is_online, target_id):
        bs.bound_calls.append((player_name, platform, target_id))
        return bind_result

    bs._bind_player = _bind_player
    return bs


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _info(message, source="QQ", sender_id="100000001"):
    return BroadcastInfo(
        raw=None,
        message=message,
        _source=Source(source),
        source_id="200000002",
        sender="某人",
        sender_id=sender_id,
        event_type="message",
        event_sub_type="group",
        is_admin=False,
    )


AT_SEG = {"type": "at", "data": {"qq": "999999"}}


def _text(t):
    return {"type": "text", "data": {"text": t}}


def _reply_text(bs):
    if not bs.replies:
        return ""
    return bs.replies[0][0]["data"]["text"]


# ---------------------------------------------------- 空命令：提示一致
def test_official_at_empty_command_shows_help():
    """官方形态空命令 → 回绑定系统帮助（与 OneBot 一致）。"""
    bs = _make_bound()
    bi = _info([AT_SEG, _text("#绑定")])
    assert _run(bs.process_broadcast_info(bi)) is True
    assert _reply_text(bs) == TR["user_help_msg"]


def test_onebot_empty_command_shows_help():
    """OneBot 形态空命令 → 同样回绑定系统帮助。"""
    bs = _make_bound()
    bi = _info([_text("#绑定")])
    assert _run(bs.process_broadcast_info(bi)) is True
    assert _reply_text(bs) == TR["user_help_msg"]


def test_official_and_onebot_empty_command_same_reply():
    """回归：两种平台形态的空命令必须给**同一条**提示。"""
    bs_official = _make_bound()
    _run(bs_official.process_broadcast_info(_info([AT_SEG, _text("#绑定")])))

    bs_onebot = _make_bound()
    _run(bs_onebot.process_broadcast_info(_info([_text("#绑定")])))

    assert _reply_text(bs_official) == _reply_text(bs_onebot)


def test_official_at_empty_command_does_not_bind():
    """官方形态空命令不得进入绑定流程（不产生脏数据）。"""
    bs = _make_bound()
    bi = _info([AT_SEG, _text("#绑定")])
    _run(bs.process_broadcast_info(bi))
    assert not bs.bound_calls, "空命令不应触发绑定"


# ------------------------------------------------ 带玩家名：正常绑定
def test_official_at_with_player_name_binds():
    """官方形态带玩家名 → 正常走绑定流程。"""
    bs = _make_bound()
    bi = _info([AT_SEG, _text("#绑定 Steve")])
    assert _run(bs.process_broadcast_info(bi)) is True
    assert bs.bound_calls, "应进入绑定流程"
    assert bs.bound_calls[0][0] == "Steve"
    assert _reply_text(bs) == TR["bind_success"]


def test_onebot_with_player_name_binds():
    """OneBot 形态带玩家名 → 正常走绑定流程（行为一致）。"""
    bs = _make_bound()
    bi = _info([_text("#绑定 Steve")])
    assert _run(bs.process_broadcast_info(bi)) is True
    assert bs.bound_calls
    assert bs.bound_calls[0][0] == "Steve"


def test_official_at_at_segment_targets_other_user():
    """@了别人 → 绑定到被 @ 的人（at 段的 qq），而不是发送者。"""
    bs = _make_bound()
    bi = _info([AT_SEG, _text("#绑定 Steve")], sender_id="100000001")
    _run(bs.process_broadcast_info(bi))
    assert bs.bound_calls
    # target_id 来自 at 段
    assert bs.bound_calls[0][2] == "999999"


# -------------------------------------------------------- 非法消息
def test_only_at_segment_is_ignored():
    """只有 at 段没有文本 → 不是命令，静默忽略（不抛异常）。"""
    bs = _make_bound()
    bi = _info([AT_SEG])
    assert _run(bs.process_broadcast_info(bi)) is False
    assert not bs.replies
    assert not bs.bound_calls


def test_non_command_message_is_ignored():
    """普通聊天（非命令）→ 忽略。"""
    bs = _make_bound()
    bi = _info([_text("随便说点什么")])
    assert _run(bs.process_broadcast_info(bi)) is False
    assert not bs.bound_calls


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
