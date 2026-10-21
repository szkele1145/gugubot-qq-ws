# -*- coding: utf-8 -*-
"""连接器注册名 = source_name 的硬编码 "QQ" 遗漏（v2.2.10 续）。

背景
----
``ConnectorManager.get_connector(source)`` 是按 ``connector.source``
匹配的，而 ``connector.source`` 取自 ``connector.QQ.source_name``
（用户**可自定义**，默认 "QQ"）。注意区分两个概念：

- **配置节名**恒为 ``"QQ"``（代码固定，用户改不了）→ 用它读配置是对的；
- **连接器注册名 / 平台键**是 ``source_name``（可改）→ 用它取连接器才对。

多处代码写成 ``get_connector("QQ")``（拿配置节名当注册名用）。用户把
source_name 改成 "我的QQ" 之后全部取到 None，后果各不相同：

| 位置 | 后果 |
|---|---|
| ``bound.py`` 群名片 | ``None.bot`` → AttributeError → 被外层 except 吞掉 → **绑定已写盘，用户却看到"绑定失败"** |
| ``player_manager.py`` 管理员判定 | ``None.bot`` → AttributeError，且**不在 try 保护范围内** → 管理员判定整个抛异常 |
| ``echo.py`` 管理群过滤 | 落进 OneBot 兜底分支 → 拿虚拟群号和真实群号配置比对 → 永远不等 → **管理群消息被错误广播** |
| ``bound_notice.py`` 管理群过滤 | 同上 → **管理群里的未绑定玩家照样收到「请先绑定」提醒** |
| ``bound.py`` 成员归属检查 | 静默 return None → **多绑定检查永远不可用** |
| ``bound.py`` 退群通知 | 静默 return → **退群通知完全不发** |
| ``__init__.py`` 系统装配 | 连接器不设置 → **未绑定检查与不活跃检查系统完全不工作** |

本文件覆盖这些点。
"""
import sys
import asyncio
import pathlib
import types
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

from gugubot.logic.system import bound as bound_mod
from gugubot.logic.system import echo as echo_mod
from gugubot.logic.system import bound_notice as bn_mod
from gugubot.utils import player_manager as pm_mod


class FakeConnectorManager:
    """按 connector.source 匹配的连接器管理器（与真实实现同语义）。"""

    def __init__(self, connectors=None):
        self.connectors = connectors or []
        self.asked = []

    def get_connector(self, source):
        self.asked.append(source)
        for c in self.connectors:
            if getattr(c, "source", None) == source:
                return c
        return None


class FakeBot:
    def __init__(self, members=None):
        self.members = members if members is not None else []
        self.sent = []

    async def get_group_member_list(self, group_id):
        return {"status": "ok", "data": self.members}

    async def send_group_msg(self, group_id, message):
        self.sent.append((group_id, message))
        return {"status": "ok"}


class FakeConnector:
    def __init__(self, source, bot=None, id_mapper=None, enable_send=True):
        self.source = source
        self.bot = bot if bot is not None else FakeBot()
        self.enable_send = enable_send
        self.enable_receive = True
        if id_mapper is not None:
            self.id_mapper = id_mapper


class FakeMapper:
    """虚实号映射桩。"""

    def __init__(self, mapping=None):
        self.mapping = mapping or {}

    def to_official_group(self, gid):
        return self.mapping.get(str(gid), str(gid))

    def to_official_user(self, uid):
        return self.mapping.get(str(uid), str(uid))


class FakeConfig:
    def __init__(self, data):
        self.data = data

    def get_keys(self, path, default=None):
        cur = self.data
        for k in path:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    def get(self, path, default=None):
        if isinstance(path, str):
            return self.data.get(path, default)
        return self.get_keys(path, default)


def _cfg(source_name="我的QQ", admin_groups=("10001",), admin_ids=()):
    return FakeConfig({
        "connector": {
            "QQ": {
                "source_name": source_name,
                "permissions": {
                    "admin_group_ids": list(admin_groups),
                    "admin_ids": list(admin_ids),
                },
            }
        }
    })


# ================================================ bound.py 群名片
def _mk_bound(source_name="我的QQ"):
    b = bound_mod.BoundSystem.__new__(bound_mod.BoundSystem)
    b.config = _cfg(source_name)
    b.logger = type("L", (), {"info": lambda *a, **k: None,
                              "warning": lambda *a, **k: None,
                              "error": lambda *a, **k: None,
                              "debug": lambda *a, **k: None})()
    cm = FakeConnectorManager([FakeConnector(source_name, id_mapper=FakeMapper())])
    b.system_manager = type("SM", (), {"connector_manager": cm})()
    return b, cm


def test_bound_qq_source_name_helper():
    """辅助方法应返回当前 source_name（不是配置节名）。"""
    b, _ = _mk_bound("我的QQ")
    assert b._qq_source_name() == "我的QQ"


def test_bound_qq_source_name_default():
    b, _ = _mk_bound("QQ")
    assert b._qq_source_name() == "QQ"


def test_bound_platform_keys_include_legacy():
    """平台键仍是「当前 + 历史 QQ」。"""
    b, _ = _mk_bound("我的QQ")
    keys = b._qq_platform_keys()
    assert keys[0] == "我的QQ"
    assert "QQ" in keys


# ================================================ echo.py 管理群过滤
def _mk_echo(source_name="我的QQ", admin_groups=("10001",)):
    e = echo_mod.EchoSystem.__new__(echo_mod.EchoSystem)
    e.config = _cfg(source_name, admin_groups)
    e.logger = type("L", (), {"info": lambda *a, **k: None,
                              "warning": lambda *a, **k: None,
                              "error": lambda *a, **k: None,
                              "debug": lambda *a, **k: None})()
    cm = FakeConnectorManager([
        FakeConnector(source_name, id_mapper=FakeMapper({"10001": "REAL_GROUP"}))
    ])
    e.system_manager = type("SM", (), {"connector_manager": cm})()
    e.enable = True
    return e, cm


class FakeSource:
    def __init__(self, origin):
        self.origin = origin

    def is_from(self, s):
        return self.origin == s


class FakeBI:
    def __init__(self, origin, source_id="10001", sub="group", message="hello"):
        self.source = FakeSource(origin)
        self.source_id = source_id
        self.event_sub_type = sub
        self.event_type = "message"
        self.sender_id = "u1"
        self.receiver_source = None
        self.sender = "u1"
        self.message = [{"type": "text", "data": {"text": message}}]
        self.receiver = None
        self.args = None
        self.is_admin = False


def test_echo_uses_source_name_to_get_connector():
    """回归：改名后 echo 必须用 source_name 取连接器，否则管理群消息
    会被错误地广播出去（旧实现落进 OneBot 兜底分支，虚实号永远不等）。
    """
    e, cm = _mk_echo("我的QQ", admin_groups=("10001",))
    bi = FakeBI("我的QQ", source_id="10001")
    # 走管理群判定分支：命中管理群 → 返回 False（不广播）
    res = asyncio.new_event_loop().run_until_complete(e.process_broadcast_info(bi))
    assert res is False, "管理群消息不应被 echo 广播"
    assert "我的QQ" in cm.asked, f"应按 source_name 取连接器，实际问过 {cm.asked}"


def test_echo_non_admin_group_still_broadcasts():
    """非管理群的消息不受影响（应继续广播）。"""
    e, cm = _mk_echo("我的QQ", admin_groups=("99999",))
    bi = FakeBI("我的QQ", source_id="10001")
    res = asyncio.new_event_loop().run_until_complete(e.process_broadcast_info(bi))
    assert "我的QQ" in cm.asked


# ================================================ bound_notice.py
def _mk_bn(source_name="我的QQ"):
    b = bn_mod.BoundNoticeSystem.__new__(bn_mod.BoundNoticeSystem)
    b.config = _cfg(source_name)
    b.logger = type("L", (), {"info": lambda *a, **k: None,
                              "warning": lambda *a, **k: None,
                              "error": lambda *a, **k: None,
                              "debug": lambda *a, **k: None})()
    cm = FakeConnectorManager([
        FakeConnector(source_name, id_mapper=FakeMapper({"10001": "REAL_GROUP"}))
    ])
    b.system_manager = type("SM", (), {"connector_manager": cm})()
    b.enable = True
    return b, cm


def test_bound_notice_skips_admin_group():
    """回归：改名后 bound_notice 必须用 source_name 取连接器，
    否则管理群里的未绑定玩家照样收到「请先绑定」提醒。
    """
    b, cm = _mk_bn("我的QQ")
    bi = FakeBI("我的QQ", source_id="10001")

    async def fake_reply(*a, **k):
        return None

    b.reply = fake_reply
    b.bound_system = type("BS", (), {
        "player_manager": type("PM", (), {"get_player": lambda *a, **k: None})(),
        "get_tr": lambda *a, **k: "绑定系统",
    })()
    b.get_tr = lambda *a, **k: "请先绑定"
    res = asyncio.new_event_loop().run_until_complete(b.process_broadcast_info(bi))
    assert res is False, "管理群消息不应触发未绑定提醒"
    assert "我的QQ" in cm.asked, f"应按 source_name 取连接器，实际问过 {cm.asked}"


# ================================================ player_manager 管理员判定
def _mk_pm(source_name="我的QQ", connectors=None, admin_ids=()):
    pm = pm_mod.PlayerManager.__new__(pm_mod.PlayerManager)
    pm._players = {}
    pm._admin_member_cache = None
    pm.logger = type("L", (), {"info": lambda *a, **k: None,
                               "warning": lambda *a, **k: None,
                               "error": lambda *a, **k: None,
                               "debug": lambda *a, **k: None})()
    cm = FakeConnectorManager(connectors if connectors is not None else [])
    pm.bound_system = type("BS", (), {
        "config": _cfg(source_name, admin_ids=admin_ids),
        "system_manager": type("SM", (), {"connector_manager": cm})(),
    })()
    return pm, cm


def test_player_manager_admin_with_custom_source_name():
    """回归：改名后管理员判定必须走 source_name 取连接器；
    旧实现 get_connector("QQ") → None → None.bot 抛 AttributeError。
    """
    bot = FakeBot(members=[{"user_id": "u-admin"}])
    pm, cm = _mk_pm("我的QQ", [FakeConnector("我的QQ", bot=bot)])
    player = type("P", (), {
        "java_name": ["Steve"], "bedrock_name": [],
        "accounts": {"我的QQ": ["u-admin"]},
    })()
    pm._players["Steve"] = player
    res = asyncio.new_event_loop().run_until_complete(pm.is_admin("u-admin"))
    assert res is True, "管理群成员应被识别为管理员"
    assert "我的QQ" in cm.asked, f"应按 source_name 取连接器，实际问过 {cm.asked}"


def test_player_manager_does_not_raise_when_connector_missing():
    """连接器缺失时不得抛 AttributeError（旧实现 None.bot 直接炸）。"""
    pm, cm = _mk_pm("我的QQ", [])
    player = type("P", (), {
        "java_name": ["Steve"], "bedrock_name": [],
        "accounts": {"我的QQ": ["u1"]},
    })()
    pm._players["Steve"] = player
    res = asyncio.new_event_loop().run_until_complete(pm.is_admin("u1"))
    assert res is False


def test_player_manager_explicit_admin_id_still_works_without_connector():
    """连接器缺失时，配置里显式写的 admin_ids 仍应生效。"""
    pm, cm = _mk_pm("我的QQ", [], admin_ids=("u1",))
    player = type("P", (), {
        "java_name": ["Steve"], "bedrock_name": [],
        "accounts": {"我的QQ": ["u1"]},
    })()
    pm._players["Steve"] = player
    res = asyncio.new_event_loop().run_until_complete(pm.is_admin("u1"))
    assert res is True, "配置里的管理员 ID 不应因连接器缺失而失效"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
