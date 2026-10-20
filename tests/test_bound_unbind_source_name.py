# -*- coding: utf-8 -*-
"""bound.py 解绑/重复绑定/退群清理对自定义 source_name 的适配（v2.2.10）。

背景
----
上一版修好了 ``PlayerManager.get_player`` 的查询，但 ``bound.py`` 内部
还有三处**只查当前平台键**的逻辑，改过 source_name 就会出问题：

1. **解绑权限检查**（``_unbind_specific_player``）：拿
   ``broadcast_info.sender_id`` 去比 ``accounts[当前键]`` → 改名前的
   旧绑定查不到 → **绑定者自己解绑被拒**（提示"无绑定"），只有管理员
   能解绑；
2. **重复绑定判重**（``_handle_bind``）：旧代码拿 **MC 玩家名**去和
   **QQ 账号列表**比（两个不同命名空间，几乎恒为 True），且只查当前键
   → 用户改名前绑过的 QQ 号会被**再绑一次**（同一账号在两个键下各存
   一份，解绑时只能删掉一半）；
3. **退群清理**（``_handle_leave``）：判断"是否还有其他平台的绑定"时
   硬编码 ``platform != "QQ"`` → 改名后 QQ 绑定（键为 "我的QQ"）被误判
   成"其他平台的绑定" → 残留**僵尸绑定**（玩家名留着、QQ 号没了）。

本文件覆盖这三处的修复行为。
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


class P:
    """最小玩家对象。"""

    def __init__(self, name="Steve", accounts=None):
        self.name = name
        self.java_name = [name]
        self.bedrock_name = []
        self.accounts = accounts if accounts is not None else {}
        self.properties = {}

    def add_account(self, platform, account_id):
        self.accounts.setdefault(platform, [])
        if account_id not in self.accounts[platform]:
            self.accounts[platform].append(account_id)

    def add_name(self, name, is_bedrock=False):
        pass


def _make_bound(source_name="我的QQ", player=None):
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
            self.players = [player] if player else []
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


# ------------------------------------------------------ 1. 解绑权限检查
def _info(source="我的QQ", sender_id="100000001", is_admin=False):
    bi = BroadcastInfo(
        raw=None,
        message=[{"type": "text", "data": {"text": "hi"}}],
        _source=Source(source),
        source_id=sender_id,
        sender="某人",
        sender_id=sender_id,
        event_type="message",
        is_admin=is_admin,
    )
    return bi


def _collect_bound_accounts(bs, player):
    """调用 BoundSystem 的真实实现。"""
    return bs._get_player_qq_accounts(player)


def test_get_player_qq_accounts_merges_both_keys():
    """两个键下的账号都要返回。"""
    bs = _make_bound("我的QQ")
    p = P(accounts={"QQ": ["100000001"], "我的QQ": ["200000002"]})
    assert sorted(_collect_bound_accounts(bs, p)) == ["100000001", "200000002"]


def test_unbind_permission_sees_legacy_key():
    """旧绑定在历史 "QQ" 键下 → 绑定者自己仍被识别为绑定者（可解绑）。"""
    bs = _make_bound("我的QQ")
    p = P(accounts={"QQ": ["100000001"]})
    assert bs._can_unbind(p, "100000001", is_admin=False) is True


def test_unbind_permission_sees_custom_key():
    bs = _make_bound("我的QQ")
    p = P(accounts={"我的QQ": ["100000001"]})
    assert bs._can_unbind(p, "100000001", is_admin=False) is True


def test_unbind_permission_rejects_stranger():
    """对照组：非绑定者不能解绑。"""
    bs = _make_bound("我的QQ")
    p = P(accounts={"我的QQ": ["100000001"]})
    assert bs._can_unbind(p, "999999999", is_admin=False) is False


def test_unbind_permission_admin_always_allowed():
    """管理员不受绑定关系限制。"""
    bs = _make_bound("我的QQ")
    p = P(accounts={"我的QQ": ["100000001"]})
    assert bs._can_unbind(p, "999999999", is_admin=True) is True


# -------------------------------------------------------- 2. 重复绑定
def test_is_account_bound_legacy_key():
    """旧键下已绑过的账号 → 判为已绑定（不重复绑定）。"""
    bs = _make_bound("我的QQ")
    p = P(accounts={"QQ": ["100000001"]})
    assert bs._is_account_bound(p, "我的QQ", "100000001") is True


def test_is_account_bound_custom_key():
    bs = _make_bound("我的QQ")
    p = P(accounts={"我的QQ": ["100000001"]})
    assert bs._is_account_bound(p, "我的QQ", "100000001") is True


def test_is_account_bound_new_account():
    """没绑过的账号 → 判为未绑定。"""
    bs = _make_bound("我的QQ")
    p = P(accounts={"我的QQ": ["100000001"]})
    assert bs._is_account_bound(p, "我的QQ", "200000002") is False


def test_is_account_bound_other_platform():
    """其他平台的账号不受 QQ 候选键影响。"""
    bs = _make_bound("我的QQ")
    p = P(accounts={"Discord": ["dc1"]})
    assert bs._is_account_bound(p, "Discord", "dc1") is True
    assert bs._is_account_bound(p, "Discord", "dc2") is False


def test_is_account_bound_ignores_player_name():
    """回归：判重比较的是 QQ 账号，不是 MC 玩家名。

    旧实现拿 player_name 去比 accounts 列表（不同命名空间）→ 几乎恒
    为 True，判重形同虚设。
    """
    bs = _make_bound("我的QQ")
    p = P(accounts={"我的QQ": ["100000001"]})
    assert bs._is_account_bound(p, "我的QQ", "Steve") is False


# -------------------------------------------------------- 3. 退群清理
def _has_other_platform_accounts(bs, player):
    """调用 BoundSystem 的真实实现。"""
    return bs._has_other_platform_accounts(player)


def test_leave_cleanup_custom_key_is_not_other_platform():
    """改名后 QQ 键不是 "QQ" → 仍属于 QQ 平台（不算"其他平台"）。"""
    bs = _make_bound("我的QQ")
    p = P(accounts={"我的QQ": ["100000001"]})
    assert _has_other_platform_accounts(bs, p) is False


def test_leave_cleanup_legacy_key_is_not_other_platform():
    bs = _make_bound("我的QQ")
    p = P(accounts={"QQ": ["100000001"]})
    assert _has_other_platform_accounts(bs, p) is False


def test_leave_cleanup_detects_real_other_platform():
    """对照组：真的有其他平台绑定时才为 True。"""
    bs = _make_bound("我的QQ")
    p = P(accounts={"我的QQ": ["100000001"], "Discord": ["dc1"]})
    assert _has_other_platform_accounts(bs, p) is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
