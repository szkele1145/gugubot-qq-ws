# -*- coding: utf-8 -*-
"""群类配置里填了「虚拟用户号」必须报错（v2.2.10 续，86）。

问题
----
``logic/system/config_check.py`` 对虚拟号的检查是**单向的**：

* ``_check_virtual_user_ids``（用于 ``admin_ids``）会拒绝 ``200000xxx``
  —— 那是虚拟**群**号段，管理员是「人」，填群号永远匹配不上 → 明确报错；
* 但 ``_check_group_ids``（用于 ``group_ids`` / ``admin_group_ids``）
  把 ``100000xxx`` 也算作合法的「虚拟号」✅ —— 可那是虚拟**用户**号段。

于是用户把 #我是谁 查到的**用户**虚拟号填进 ``group_ids`` 时，配置检验
会给出 **✅ 1 个虚拟号**，而实际效果是：这个号永远匹配不上任何群 →
群监听/转发**完全不工作**，且用户拿着"检验通过"的输出根本查不出问题。

这是同一份代码里两个方向不一致：A 拒绝了 B 段，B 却不拒绝 A 段。

修复
----
``_check_group_ids`` 把虚拟号分成两段分别统计：

* ``200000xxx`` → 虚拟群号，合法 ✅
* ``100000xxx`` → 虚拟**用户**号，**报错** ❌ 并提示改用群号 / 虚拟群号
* 其余纯数字 → 真实群号（走槽位自动绑定）✅
* 24-40 位十六进制 → openid 原文 ✅
"""
import importlib.abc
import importlib.machinery
import pathlib
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


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
sys.path.insert(0, str(_ROOT / "plugin"))

_MOD = "gugubot.logic.system.config_check"
sys.modules.pop(_MOD, None)
from gugubot.logic.system import config_check  # noqa: E402


class _Logger:
    def __getattr__(self, n):
        return lambda *a, **k: None


def _official_connector():
    ws = types.SimpleNamespace(is_connected=lambda: True)
    return types.SimpleNamespace(
        id_mapper=types.SimpleNamespace(), ws_client=ws
    )


def _make_system(overrides):
    """overrides 直接覆盖 ``connector.QQ`` 下的配置子树。"""
    base = {
        "connection": {
            "type": "official",
            "official": {
                "appid": "123456",
                "secret": "s" * 32,
                "intents": {"group_at_messages": True},
            },
        },
        "max_message_length": 2000,
        "permissions": {
            "admin_ids": ["100000000001"],
            "group_ids": ["200000000001"],
            "admin_group_ids": [],
        },
    }

    def _merge(dst, src):
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                _merge(dst[k], v)
            else:
                dst[k] = v

    _merge(base, overrides)

    class _Config:
        def get_keys(self, path, default=None):
            cur = {"connector": {"QQ": base}}
            for p in path:
                if not isinstance(cur, dict) or p not in cur:
                    return default
                cur = cur[p]
            return cur

    inst = config_check.ConfigCheckSystem.__new__(config_check.ConfigCheckSystem)
    inst.name = "config_check"
    inst.logger = _Logger()
    inst.config = _Config()
    inst._connector_manager = types.SimpleNamespace(
        connectors=[_official_connector()]
    )
    return inst


# ==================================================== 1. 用户虚拟号进群配置 → 报错
def test_user_virtual_id_in_group_ids_is_rejected():
    """把虚拟**用户**号填进 ``group_ids`` 必须报错，而不是 ✅。"""
    inst = _make_system(
        {"permissions": {"group_ids": ["100000000001"]}}
    )
    result = inst._check_qq_connector()

    assert result["ok"] is False, (
        "group_ids 里填了虚拟用户号（100000xxx），检验却判为通过 —— "
        "这个号永远匹配不上任何群，群监听/转发完全不工作，"
        "而用户拿着「✅ 1 个虚拟号」根本查不出问题"
    )
    text = "\n".join(result["lines"])
    assert "group_ids" in text
    assert "用户" in text, f"提示里没说明那是虚拟用户号:\n{text}"


def test_user_virtual_id_in_admin_group_ids_is_rejected():
    inst = _make_system(
        {"permissions": {"admin_group_ids": ["100000000002"]}}
    )
    result = inst._check_qq_connector()
    assert result["ok"] is False
    assert "admin_group_ids" in "\n".join(result["lines"])


# ==================================================== 2. 正常用法仍判通过
def test_virtual_group_id_still_ok():
    """虚拟**群**号（200000xxx）仍是合法用法。"""
    inst = _make_system({"permissions": {"group_ids": ["200000000009"]}})
    result = inst._check_qq_connector()
    assert result["ok"] is True, (
        f"虚拟群号被误判为错误:\n" + "\n".join(result["lines"])
    )


def test_real_group_id_still_ok():
    """真实群号（9-10 位）仍是标准用法，不能误报。"""
    inst = _make_system({"permissions": {"group_ids": ["123456789"]}})
    result = inst._check_qq_connector()
    assert result["ok"] is True, (
        f"真实群号被误判为错误:\n" + "\n".join(result["lines"])
    )


def test_openid_literal_still_ok():
    """openid 原文（32 位十六进制）仍是合法用法。"""
    inst = _make_system(
        {"permissions": {"group_ids": ["a" * 32]}}
    )
    result = inst._check_qq_connector()
    assert result["ok"] is True, (
        f"openid 原文被误判为错误:\n" + "\n".join(result["lines"])
    )


# ==================================================== 3. admin_ids 侧不受影响
def test_group_virtual_id_in_admin_ids_still_rejected():
    """原有的对称方向（200000xxx 填进 admin_ids 报错）不能被改坏。"""
    inst = _make_system(
        {"permissions": {"admin_ids": ["200000000003"]}}
    )
    result = inst._check_qq_connector()
    assert result["ok"] is False
    assert "admin_ids" in "\n".join(result["lines"])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
