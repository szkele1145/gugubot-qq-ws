# -*- coding: utf-8 -*-
"""官方 Bot 的请求跟踪字段必须是实例属性（v2.2.10 续）。

背景
----
``QQOfficialBot`` 里 ``function_return`` / ``pending_requests`` 原本写成
**类级可变默认值**::

    class QQOfficialBot:
        function_return: dict = {}
        pending_requests: dict = {}

类级可变默认值是**所有实例共享同一个 dict**。任何一次写入都会：

1. **跨实例污染** —— A 实例写进去的 echo，B 实例也能读到；
2. **内存只增不减** —— 官方模式**没有** OneBot 那套
   ``_cleanup_pending_requests()``（它挂在 OneBot 的 ``bot`` 类上），
   所以写入的条目永远不会被回收。

OneBot 模式之所以没出问题，是因为 ``qq_connector.py`` 在 ``__init__``
里赋了实例属性，**遮蔽**了类级默认值。官方模式没有这一步。

修复：类级只保留**只读兜底**（供 ``__new__`` 绕过 ``__init__`` 构造的
实例读取时不报 AttributeError），正常实例一律在 ``__init__`` 里建立
自己的 dict。
"""
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

from gugubot.connector.qq_official import connector as cm


def _mk_bot():
    """构造一个 QQOfficialBot（不碰真实 connector）。"""
    return cm.QQOfficialBot.__new__(cm.QQOfficialBot)


# ================================================ 1. 实例隔离
def test_two_instances_do_not_share_function_return():
    """两个实例的 function_return 必须是**不同的** dict。

    回归：类级可变默认值 → 所有实例共享同一个 dict → 跨实例污染。
    """
    b1 = cm.QQOfficialBot(object())
    b2 = cm.QQOfficialBot(object())
    b1.function_return["echo-1"] = {"msg": "x"}
    assert "echo-1" not in b2.function_return, "实例之间不得共享 function_return"
    assert b1.function_return is not b2.function_return


def test_two_instances_do_not_share_pending_requests():
    """pending_requests 同理，必须实例隔离。"""
    b1 = cm.QQOfficialBot(object())
    b2 = cm.QQOfficialBot(object())
    b1.pending_requests["echo-1"] = 12345.0
    assert "echo-1" not in b2.pending_requests
    assert b1.pending_requests is not b2.pending_requests


def test_write_does_not_leak_to_class_level():
    """实例写入不得污染类级（类级根本不该有这两个字段）。"""
    b = cm.QQOfficialBot(object())
    b.function_return["echo-2"] = 1
    b.pending_requests["echo-2"] = 2
    # 类级没有这两个属性 → 实例写入不可能污染它
    assert "function_return" not in vars(cm.QQOfficialBot)
    assert "pending_requests" not in vars(cm.QQOfficialBot)


# ================================================ 2. 绕过 __init__ 的实例
def test_new_instance_getattr_provides_dicts():
    """__new__ 构造的实例读这两个字段不报 AttributeError（__getattr__ 兜底）。"""
    b = _mk_bot()
    assert isinstance(b.function_return, dict)
    assert isinstance(b.pending_requests, dict)


def test_new_instance_fallback_is_isolated():
    """__new__ 构造的实例之间也必须互不污染（各自建自己的 dict）。

    回归：首版用「类级空 dict 兜底」→ 绕过 __init__ 的实例共享同一份
    → 写入即污染。现改用 __getattr__ 惰性建本实例专属的 dict。
    """
    b1 = _mk_bot()
    b2 = _mk_bot()
    b1.function_return["k"] = 1
    assert "k" not in b2.function_return
    assert b1.function_return is not b2.function_return


def test_new_instance_getattr_is_stable():
    """同一实例多次访问拿到**同一个** dict（不能每次新建）。"""
    b = _mk_bot()
    first = b.function_return
    second = b.function_return
    assert first is second
    first["x"] = 1
    assert b.function_return["x"] == 1


def test_getattr_does_not_mask_other_attributes():
    """__getattr__ 不得掩盖真实的属性缺失（否则报错会被误导）。"""
    b = _mk_bot()
    with pytest.raises(AttributeError):
        b.definitely_not_a_real_attribute


# ================================================ 3. 语义保持
def test_bot_still_exposes_request_tracking_fields():
    """对外接口不变：OneBot 兼容字段必须仍然可读可写。"""
    b = cm.QQOfficialBot(object())
    b.function_return["echo"] = {"ok": True}
    b.pending_requests["echo"] = 1.0
    assert b.function_return["echo"] == {"ok": True}
    assert b.pending_requests["echo"] == 1.0


def test_class_level_fallback_not_shared_with_instances():
    """实例的 dict 不能**就是**类级那份（类级已不持有该字段）。"""
    b = cm.QQOfficialBot(object())
    assert b.function_return is not getattr(cm.QQOfficialBot, "function_return", None)
    assert b.pending_requests is not getattr(cm.QQOfficialBot, "pending_requests", None)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
