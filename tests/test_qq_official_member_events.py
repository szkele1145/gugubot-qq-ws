# -*- coding: utf-8 -*-
"""官方「群成员增减」事件的解析与下游退群清理的虚实号适配（v2.2.10 续）。

背景
----
官方开放平台在 ``GROUP_AND_C2C_EVENT (1<<25)`` intent 位下还会下发
``GROUP_MEMBER_ADD`` / ``GROUP_MEMBER_REMOVE``（群成员增加/退出，与
``GROUP_ADD_ROBOT`` 同位）。旧版解析器没有分支 → 落到 ``return None``
→ **静默丢弃** → 官方模式下：

1. ``bound.py`` 的退群处理（认 ``event_sub_type == "group_decrease"``）
   永远收不到事件 → 退群通知与绑定清理完全失效；
2. ``raw.get("group_id", "")`` —— 即使收到事件，官方 payload 里也只有
   ``group_openid``，没有 OneBot 的 ``group_id``，处理函数首行
   ``if not user_id or not group_id: return False`` 就直接返回。

修复
----
- 解析器新增 ``_parse_group_member_change``：官方事件 → OneBot 兼容的
  ``group_decrease`` / ``group_increase`` notice，``raw`` 里补齐
  ``user_id``（虚拟用户号）与 ``group_id``（群显示号）；
- ``bound.py`` 的退群处理改用 id_mapper 统一把「事件里的群号」与
  「配置里的真实群号」翻成 openid 再比对（官方模式下事件里是虚拟
  群号、配置里是真实群号，直接字符串比较永不相等）。
"""
import asyncio
import sys
import types
import pathlib
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

from gugubot.connector.qq_official import parser as pm


def _mk_parser_with_mapper():
    p = pm.QQOfficialParser.__new__(pm.QQOfficialParser)
    p._no_slot_warned = set()
    p._not_listened_warned = set()
    p.logger = None
    p.server = None

    mapper = types.SimpleNamespace(
        to_legacy_group=lambda oid: {
            "grp-openid-1": "200000000001",
            "grp-openid-2": "200000000002",
        }.get(oid, ""),
        to_legacy_user=lambda oid: {
            "usr-openid-a": "100000000001",
            "usr-openid-b": "100000000002",
        }.get(oid, ""),
        to_official_group=lambda lid: {
            "200000000001": "grp-openid-1",
            "200000000002": "grp-openid-2",
        }.get(str(lid), str(lid)),
    )
    p.connector = types.SimpleNamespace(
        source="QQ",
        id_mapper=mapper,
        config=types.SimpleNamespace(
            get_keys=lambda path, default=None: default
        ),
    )
    return p


def _parse(p, event_name, data):
    info = p.parse(
        {"_event_name": event_name, "_data": data, "_event_id": "e1"}
    )
    # parse 是 async 函数
    if asyncio.iscoroutine(info):
        info = asyncio.new_event_loop().run_until_complete(info)
    return info


# ================================================ 1. 事件不再被丢弃
def test_group_member_remove_is_parsed():
    """GROUP_MEMBER_REMOVE 必须产出 group_decrease notice（旧版直接丢弃）。"""
    p = _mk_parser_with_mapper()
    info = _parse(p, "GROUP_MEMBER_REMOVE", {
        "group_openid": "grp-openid-1",
        "op_member_openid": "usr-openid-a",
    })
    assert info is not None, "事件被解析器丢弃了"
    assert info.event_type == "notice"
    assert info.event_sub_type == "group_decrease"


def test_group_member_add_is_parsed():
    """GROUP_MEMBER_ADD → group_increase notice。"""
    p = _mk_parser_with_mapper()
    info = _parse(p, "GROUP_MEMBER_ADD", {
        "group_openid": "grp-openid-1",
        "op_member_openid": "usr-openid-b",
    })
    assert info is not None
    assert info.event_sub_type == "group_increase"


# ================================================ 2. 下游契约（raw 字段补齐）
def test_remove_raw_has_onebot_ids():
    """raw 必须带下游期待的 user_id / group_id（OneBot 形态）。"""
    p = _mk_parser_with_mapper()
    info = _parse(p, "GROUP_MEMBER_REMOVE", {
        "group_openid": "grp-openid-1",
        "op_member_openid": "usr-openid-a",
    })
    assert info.raw.get("user_id") == "100000000001", info.raw
    assert info.raw.get("group_id") == "200000000001", info.raw


def test_remove_source_and_sender_ids():
    """source_id = 群显示号；sender_id = 虚拟用户号。"""
    p = _mk_parser_with_mapper()
    info = _parse(p, "GROUP_MEMBER_REMOVE", {
        "group_openid": "grp-openid-1",
        "op_member_openid": "usr-openid-a",
    })
    assert str(info.source_id) == "200000000001"
    assert str(info.sender_id) == "100000000001"


def test_unknown_openids_yield_none():
    """openid 翻译不出（未知用户/群）→ 返回 None，不产出空 ID 事件。"""
    p = _mk_parser_with_mapper()
    info = _parse(p, "GROUP_MEMBER_REMOVE", {
        "group_openid": "unknown-grp",
        "op_member_openid": "usr-openid-a",
    })
    # 群号翻不出来 → legacy_group 为空串 → 事件无意义，应返回 None
    assert info is None


def test_missing_member_openid_yields_none():
    """缺 op_member_openid → None。"""
    p = _mk_parser_with_mapper()
    info = _parse(p, "GROUP_MEMBER_REMOVE", {"group_openid": "grp-openid-1"})
    assert info is None


def test_missing_group_openid_yields_none():
    """缺 group_openid → None。"""
    p = _mk_parser_with_mapper()
    info = _parse(p, "GROUP_MEMBER_REMOVE", {"op_member_openid": "usr-openid-a"})
    assert info is None


def test_robot_leaves_not_treated_as_member_quit():
    """机器人被踢（op_type=BOT）≠ 普通成员退群 → 不产出 group_decrease。"""
    p = _mk_parser_with_mapper()
    info = _parse(p, "GROUP_MEMBER_REMOVE", {
        "group_openid": "grp-openid-1",
        "op_member_openid": "usr-openid-a",
        "op_type": "BOT",
    })
    # 机器人进退群走 GROUP_ADD_ROBOT / GROUP_DEL_ROBOT 语义
    assert info is None


# ================================================ 3. bound 退群处理的虚实号适配
def _mk_bound_system(mapper, config_data):
    from gugubot.logic.system import bound as bm

    sysm = bm.BoundSystem.__new__(bm.BoundSystem)

    # config：get_keys 只处理 permissions 两键 + source_name
    cfg = types.SimpleNamespace(
        get_keys=lambda path, default=None: _cfg_get(config_data, path, default)
    )
    connector = types.SimpleNamespace(id_mapper=mapper)
    sysm.config = cfg
    sysm.system_manager = types.SimpleNamespace(
        connector_manager=types.SimpleNamespace(
            get_connector=lambda name: connector
        )
    )
    sysm.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    return sysm


def _cfg_get(data, path, default):
    cur = data
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


class _AsyncRecorder:
    def __init__(self):
        self.calls = []

    async def _send_quit_notification_to_admin_groups(self, user_id, group_id, player):
        self.calls.append(("notify", user_id, group_id))

    async def _get_member_in_all_groups(self):
        return None  # 官方模式：无成员列表


def _mk_info(user_id, group_id, source="QQ"):
    from gugubot.utils.types import BroadcastInfo, Source

    return BroadcastInfo(
        event_type="notice",
        event_sub_type="group_decrease",
        message=[{"type": "text", "data": {"text": "quit"}}],
        raw={"user_id": user_id, "group_id": group_id},
        server=None,
        logger=None,
        _source=Source(source),
        source_id=group_id,
        sender="",
        sender_id=user_id,
    )


def test_quit_member_vgroup_matches_real_config():
    """官方模式：事件里是虚拟群号、配置里是真实群号 → 必须能匹配上。

    回归：旧实现直接字符串比较 → 恒不相等 → is_normal_group 恒 False
    → 退群通知在官方模式下永远不发。

    前提：该群已绑定（否则配置里的真实群号与事件里的虚拟群号本就
    指向不同的群，见 id_map.to_official_group 只在已绑定时才把真实
    群号翻成 openid）。绑定后两者指向同一个 group_openid。
    """
    from gugubot.logic.system import bound as bm

    mapper = types.SimpleNamespace(
        # 已绑定：虚拟群号与真实群号指向同一个 openid
        to_official_group=lambda lid: {
            "200000000001": "grp-openid-1",
            "123456789": "grp-openid-1",
        }.get(str(lid), str(lid)),
    )
    sysm = _mk_bound_system(
        mapper,
        {"connector": {"QQ": {
            "source_name": "QQ",
            "permissions": {
                "group_ids": ["123456789"],       # 配置里是真实群号
                "admin_group_ids": [],
            },
        }}},
    )
    rec = _AsyncRecorder()
    sysm._send_quit_notification_to_admin_groups = rec._send_quit_notification_to_admin_groups
    sysm._get_member_in_all_groups = rec._get_member_in_all_groups
    # 成员列表不可用 → 跳过清理但通知已发
    sysm.player_manager = types.SimpleNamespace(
        get_player=lambda uid, platform=None: None
    )

    info = _mk_info("100000000001", "200000000001")
    r = asyncio.new_event_loop().run_until_complete(
        bm.BoundSystem._handle_quit_member(sysm, info)
    )
    assert r is True
    assert rec.calls, f"退群通知应已触发: {rec.calls}"
    assert rec.calls[0][1] == "100000000001"


def test_quit_member_admin_group_skips_notification():
    """退群发生在管理群 → 不发通知。"""
    from gugubot.logic.system import bound as bm

    mapper = types.SimpleNamespace(
        to_official_group=lambda lid: {
            "200000000001": "grp-openid-1",
            "123456789": "grp-openid-1",
            "200000000002": "grp-openid-2",
        }.get(str(lid), str(lid)),
    )
    sysm = _mk_bound_system(
        mapper,
        {"connector": {"QQ": {
            "source_name": "QQ",
            "permissions": {
                "group_ids": ["123456789"],
                "admin_group_ids": ["123456789"],  # 同一个群既是监听群也是管理群
            },
        }}},
    )
    rec = _AsyncRecorder()
    sysm._send_quit_notification_to_admin_groups = rec._send_quit_notification_to_admin_groups
    sysm._get_member_in_all_groups = rec._get_member_in_all_groups
    sysm.player_manager = types.SimpleNamespace(
        get_player=lambda uid, platform=None: None
    )

    info = _mk_info("100000000001", "200000000001")
    r = asyncio.new_event_loop().run_until_complete(
        bm.BoundSystem._handle_quit_member(sysm, info)
    )
    assert r is True
    assert not rec.calls, f"管理群退群不应发通知: {rec.calls}"


def test_quit_member_onebot_mode_unaffected():
    """OneBot 模式（无 id_mapper）：真实群号直接比较，行为不变。"""
    from gugubot.logic.system import bound as bm

    sysm = _mk_bound_system(
        None,  # get_connector 返回的连接器没有 id_mapper
        {"connector": {"QQ": {
            "source_name": "QQ",
            "permissions": {
                "group_ids": ["123456789"],
                "admin_group_ids": [],
            },
        }}},
    )
    # _mk_bound_system 给的 connector 固定带 id_mapper —— 覆盖为 None
    sysm.system_manager.connector_manager.get_connector = lambda name: None
    rec = _AsyncRecorder()
    sysm._send_quit_notification_to_admin_groups = rec._send_quit_notification_to_admin_groups
    sysm._get_member_in_all_groups = rec._get_member_in_all_groups
    sysm.player_manager = types.SimpleNamespace(
        get_player=lambda uid, platform=None: None
    )

    info = _mk_info("12345678", "123456789")
    r = asyncio.new_event_loop().run_until_complete(
        bm.BoundSystem._handle_quit_member(sysm, info)
    )
    assert r is True
    assert rec.calls, f"OneBot 模式退群通知应照常: {rec.calls}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
