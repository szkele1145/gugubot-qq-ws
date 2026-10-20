# -*- coding: utf-8 -*-
"""PlayerManager / inactive_check 对自定义 source_name 的适配（v2.2.10）。

背景
----
``PlayerManager.get_player(identifier, platform=...)`` 按平台键精确匹配
``player.accounts`` 的键。而绑定数据的平台键是 ``connector.QQ.source_name``
（用户可自定义，默认 "QQ"）。用户改过 source_name 后：

1. **查询自己改名前的绑定查不到**（旧绑定在 "QQ" 键下，查询用
   platform="我的QQ" 精确匹配 → 无结果）→ 系统误判「没绑过」→
   允许重复绑定、绑定上限算错；
2. **管理员判定失效**：``is_admin()`` 遍历 ``connectors.items()``，其键
   是**配置节名**（恒为 "QQ"），旧实现却拿它去和 source_name 比较 →
   改名后恒为 False → QQ 管理员分支整个被跳过；
3. ``inactive_check`` 只查当前键 → 历史键下的玩家漏检不活跃。

修复后：QQ 平台的查询/判定都遍历「source_name 当前键 + 历史 "QQ" 键」。
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

from gugubot.utils.player_manager import PlayerManager


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


def _mk_manager(source_name="我的QQ"):
    """构造最小可用的 PlayerManager（__new__ 绕过 MCDR 依赖）。"""
    pm = PlayerManager.__new__(PlayerManager)
    pm._players = {}
    pm._admin_member_cache = None
    pm.logger = FL()

    class FakeBS:
        pass

    fb = FakeBS()
    fb.config = FC()
    fb.config["connector"] = {"QQ": {"source_name": source_name}}
    pm.bound_system = fb
    return pm


def _player(name, accounts):
    class P:
        pass

    p = P()
    p.name = name
    p.java_name = [name]
    p.bedrock_name = []
    p.accounts = accounts
    p.properties = {}
    return p


# ------------------------------------------------------------ 候选键生成
def test_platform_candidates_qq_custom_name():
    """platform="QQ"（配置节名）→ 候选键 = {QQ, source_name}。"""
    pm = _mk_manager("我的QQ")
    keys = pm._platform_candidates("QQ")
    assert "QQ" in keys and "我的QQ" in keys


def test_platform_candidates_custom_name():
    """platform=source_name → 候选键同样包含两者。"""
    pm = _mk_manager("我的QQ")
    keys = pm._platform_candidates("我的QQ")
    assert "QQ" in keys and "我的QQ" in keys


def test_platform_candidates_other_platform_single():
    """其他平台（如 Discord）→ 只有单一候选键。"""
    pm = _mk_manager()
    keys = pm._platform_candidates("Discord")
    assert keys == {"Discord"}


def test_platform_candidates_none_is_unrestricted():
    """platform=None → 空候选集（get_player 里语义为不限平台）。"""
    pm = _mk_manager()
    assert pm._platform_candidates(None) == set()


# ------------------------------------------------------------ 查询行为
def test_get_player_finds_legacy_key_binding():
    """改名前的旧绑定（历史 "QQ" 键）用 platform=source_name 也能查到。"""
    pm = _mk_manager("我的QQ")
    pm._players["Steve"] = _player("Steve", {"QQ": ["1001"]})
    found = pm.get_player("1001", platform="我的QQ")
    assert found is not None, "历史 QQ 键下的绑定必须能按新 source_name 查到"
    assert found.name == "Steve"


def test_get_player_finds_custom_key_binding():
    """新绑定（source_name 键）按 platform="QQ"（节名）也能查到。"""
    pm = _mk_manager("我的QQ")
    pm._players["Alex"] = _player("Alex", {"我的QQ": ["2002"]})
    found = pm.get_player("2002", platform="QQ")
    assert found is not None


def test_get_player_does_not_cross_platform():
    """非 QQ 平台的精确语义不变：Discord 绑定不能被 QQ 平台查到。"""
    pm = _mk_manager()
    pm._players["Bob"] = _player("Bob", {"Discord": ["dc1"]})
    assert pm.get_player("dc1", platform="QQ") is None
    assert pm.get_player("dc1", platform="Discord") is not None


def test_get_player_no_platform_still_finds_all():
    """platform=None 的全平台查找行为不变。"""
    pm = _mk_manager()
    pm._players["Cat"] = _player("Cat", {"Discord": ["dc2"]})
    assert pm.get_player("dc2") is not None


# ------------------------------------------------------------ is_admin
def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _mk_admin_manager(source_name, accounts):
    """构造 is_admin 测试环境：绑定 + 配置 + 连接器桩。"""
    pm = _mk_manager(source_name)
    pm._players["Steve"] = _player("Steve", accounts)

    class FakeConfig(FC):
        pass

    cfg = FakeConfig()
    cfg["connector"] = {
        "QQ": {
            "source_name": source_name,
            "permissions": {"admin_ids": ["999"], "admin_group_ids": []},
        }
    }

    class BS:
        config = cfg
        class system_manager:
            class connector_manager:
                @staticmethod
                def get_connector(name):
                    return None

    pm.bound_system = BS()
    return pm


def test_is_admin_recognizes_admin_with_custom_key():
    """管理员绑定在自定义键下（admin_ids=999）→ 应判定为管理员。

    旧实现的 QQ 分支判定（source == source_name）在改名后恒 False →
    管理员被当成普通玩家。
    """
    pm = _mk_admin_manager("我的QQ", {"我的QQ": ["999"]})
    assert _run(pm.is_admin("Steve")) is True


def test_is_admin_recognizes_admin_with_legacy_key():
    """管理员绑定在历史 "QQ" 键下 → 同样应判定为管理员。"""
    pm = _mk_admin_manager("我的QQ", {"QQ": ["999"]})
    assert _run(pm.is_admin("Steve")) is True


def test_is_admin_rejects_non_admin():
    """对照组：普通玩家绑定 → 不是管理员。"""
    pm = _mk_admin_manager("我的QQ", {"我的QQ": ["777"]})
    assert _run(pm.is_admin("Steve")) is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
