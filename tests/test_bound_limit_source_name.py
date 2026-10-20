# -*- coding: utf-8 -*-
"""绑定上限与不活跃检查对自定义 source_name 的适配（v2.2.10）。

背景
----
``player.accounts`` 的平台键是 ``connector.QQ.source_name``（用户可自定义，
默认 "QQ"），历史数据里可能存在默认 "QQ" 键。两处只查当前单键：

1. ``bound.py`` 绑定上限检查：``len(player.accounts.get(source, []))``
   → 改名前的旧绑定不计入上限 → **可借改名绕过绑定上限**；
2. ``inactive_check.py``：``player.accounts.get(QQ_connector_name, [])``
   → 历史键下的玩家**被漏检**（不活跃却不被清理/通知）。

现两处都遍历「当前 source_name + 历史 "QQ" 键」。
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
from gugubot.logic.plugins.inactive_check import InactiveCheckSystem


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


# --------------------------------------------------------------- bound 上限
class _Player:
    def __init__(self, name, accounts):
        self.name = name
        self.java_name = [name]
        self.bedrock_name = []
        self.accounts = accounts
        self.properties = {}


def _make_bound(source_name="我的QQ", accounts=None, max_platform_bound=1):
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
    bs.config["system"] = {"bound": {"max_platform_bound": max_platform_bound}}
    bs._qq_platform_keys = BoundSystem._qq_platform_keys.__get__(bs)

    class PM:
        def __init__(self):
            self._player = _Player("Steve", accounts or {})

        def get_player(self, identifier, platform=None, name_only=False):
            if platform and platform in ("QQ", source_name):
                for pk in ("QQ", source_name):
                    if identifier in self._player.accounts.get(pk, []):
                        return self._player
                return None
            if identifier == self._player.name:
                return self._player
            return None

        def save(self): pass

    bs.player_manager = PM()
    return bs


def _count_platform_accounts(bs, source):
    """调用 BoundSystem 的真实实现。"""
    return bs._platform_account_count(bs.player_manager._player, source)


def test_bound_limit_counts_legacy_key():
    """历史 "QQ" 键 + 当前键的绑定数都要计入上限。"""
    bs = _make_bound(
        "我的QQ", {"QQ": ["1001"], "我的QQ": ["1002"]}, max_platform_bound=2
    )
    assert _count_platform_accounts(bs, "我的QQ") == 2


def test_bound_limit_legacy_only_still_counts():
    """只有历史键有绑定时，计数仍为 1（不被当成「没绑过」）。"""
    bs = _make_bound("我的QQ", {"QQ": ["1001"]}, max_platform_bound=1)
    assert _count_platform_accounts(bs, "我的QQ") == 1


def test_bound_limit_other_platform_unchanged():
    """非 QQ 平台仍只查该平台键。"""
    bs = _make_bound("我的QQ", {"Discord": ["dc1"]}, max_platform_bound=1)
    assert _count_platform_accounts(bs, "Discord") == 1


# --------------------------------------------------------- inactive_check
def _make_inactive(source_name="我的QQ", accounts=None):
    ic = InactiveCheckSystem.__new__(InactiveCheckSystem)
    ic.logger = FL()
    ic.config = FC()
    ic.config["connector"] = {
        "QQ": {"source_name": source_name, "permissions": {"admin_ids": []}}
    }
    ic.active_whitelist_system = None
    ic._player = _Player("Steve", accounts or {})
    return ic


def _inactive_qq_accounts(ic, source_name):
    """调用 InactiveCheckSystem 的真实实现。"""
    return ic._get_player_qq_accounts(ic._player, source_name)


def test_inactive_check_sees_legacy_key_players():
    """历史键下的玩家必须被纳入检查（不能漏检）。"""
    ic = _make_inactive("我的QQ", {"QQ": ["1001"]})
    assert _inactive_qq_accounts(ic, "我的QQ") == ["1001"]


def test_inactive_check_sees_custom_key_players():
    ic = _make_inactive("我的QQ", {"我的QQ": ["1002"]})
    assert _inactive_qq_accounts(ic, "我的QQ") == ["1002"]


def test_inactive_check_merges_both_keys():
    ic = _make_inactive("我的QQ", {"QQ": ["1001"], "我的QQ": ["1002"]})
    assert sorted(_inactive_qq_accounts(ic, "我的QQ")) == ["1001", "1002"]


def test_inactive_check_default_name_single_key():
    """默认 source_name="QQ" 时不应重复计数。"""
    ic = _make_inactive("QQ", {"QQ": ["1001"]})
    assert _inactive_qq_accounts(ic, "QQ") == ["1001"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
