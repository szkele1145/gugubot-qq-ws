# -*- coding: utf-8 -*-
"""include/exclude 必须**精确**匹配连接器名（v2.2.10 续，89）。

问题
----
``ConnectorManager.broadcast_processed_info`` 的过滤用**正则前缀匹配**：

.. code-block:: python

    to_connectors = [
        c for c in to_connectors
        if any(re.match(re.escape(p), c.source) for p in include)
    ]
    # exclude 同理

注释写的是 "Use re.escape for literal matching" —— 本意是**字面匹配**，
但 ``re.match`` 只锚定开头、不锚定结尾，于是变成**前缀匹配**。

而连接器名（``source_name``）是**用户可自定义**的，互为前缀极其常见：
``QQ`` / ``QQ2``、``Minecraft`` / ``MinecraftBeta``、``群`` / ``群2``……

后果（两个方向都错）：

* **include 侧 → 消息重复**：``reply()`` 用 ``include=[receiver_source]``
  回消息；若 ``receiver_source`` 是 ``QQ``，则 ``QQ2`` 也会被选中 →
  同一条回复**同时发到两个连接器**（发到别的群/别的服务器）。
* **exclude 侧 → 消息丢失**：``echo`` 用 ``exclude=[source_name, ...]``
  避免把消息回传给自己；排除 ``QQ`` 会把 ``QQ2`` 一起排掉 →
  本该转发给 ``QQ2`` 的消息**静默消失**。

同一份代码里 ``get_connector()`` 用的却是**精确相等**（``==``），
两处口径不一致 —— 属于本项目反复出现的那类缺陷。

修复
----
``include`` / ``exclude`` 改为**精确相等**匹配（与 ``get_connector`` 一致）。
所有调用点传的都是具体连接器名（不是模式），改完语义更明确。
"""
import asyncio
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

_MOD = "gugubot.connector.connector_manager"
sys.modules.pop(_MOD, None)
from gugubot.connector.connector_manager import ConnectorManager  # noqa: E402


class _Logger:
    def __getattr__(self, n):
        return lambda *a, **k: None


class _Conn:
    def __init__(self, source):
        self.source = source
        self.enable_receive = True
        self.enable_send = True
        self.received = 0

    async def send_message(self, processed_info):
        self.received += 1


def _make_manager(connectors):
    server = types.SimpleNamespace(logger=_Logger())
    config = types.SimpleNamespace(get_keys=lambda path, default=None: default)
    mgr = ConnectorManager(server, config, logger=_Logger())
    mgr.connectors = list(connectors)
    return mgr


def _run(coro):
    return asyncio.run(coro)


# ==================================================== 1. include 必须精确
def test_include_does_not_match_prefix_sibling():
    """``include=["QQ"]`` 只能选中 ``QQ``，不能连带选中 ``QQ2``。

    旧实现用 ``re.match(re.escape("QQ"), "QQ2")`` → 匹配成功 →
    同一条回复被发到两个连接器（用户看到消息重复/串群）。
    """
    qq = _Conn("QQ")
    qq2 = _Conn("QQ2")
    mgr = _make_manager([qq, qq2])

    failures = _run(mgr.broadcast_processed_info(object(), include=["QQ"]))

    assert failures == {}
    assert qq.received == 1, "目标 QQ 没收到消息"
    assert qq2.received == 0, (
        "include=['QQ'] 把 'QQ2' 也选中了 —— 前缀匹配导致**同一条回复"
        "被重复发到另一个连接器**（用户会看到消息发到别的群/服务器）"
    )


# ==================================================== 2. exclude 必须精确
def test_exclude_does_not_drop_prefix_sibling():
    """``exclude=["QQ"]`` 不能把 ``QQ2`` 一起排掉。

    旧实现在 ``echo`` 路径上会把本该转发给 ``QQ2`` 的消息静默丢掉。
    """
    qq = _Conn("QQ")
    qq2 = _Conn("QQ2")
    mgr = _make_manager([qq, qq2])

    _run(mgr.broadcast_processed_info(object(), exclude=["QQ"]))

    assert qq.received == 0, "被排除的连接器却收到了消息"
    assert qq2.received == 1, (
        "exclude=['QQ'] 把 'QQ2' 也排掉了 —— 前缀匹配导致**本该转发给 "
        "QQ2 的消息静默丢失**"
    )


# ==================================================== 3. 精确同名仍正常工作
def test_exact_names_still_work():
    qq = _Conn("QQ")
    mc = _Conn("Minecraft")
    mgr = _make_manager([qq, mc])

    _run(mgr.broadcast_processed_info(object(), include=["QQ"]))
    assert qq.received == 1
    assert mc.received == 0

    _run(mgr.broadcast_processed_info(object(), exclude=["QQ"]))
    assert qq.received == 1, "第二次广播不该再发给被排除的 QQ"
    assert mc.received == 1


# ==================================================== 4. 无过滤时发给全部
def test_no_filter_broadcasts_to_all():
    a = _Conn("A")
    b = _Conn("B")
    mgr = _make_manager([a, b])
    _run(mgr.broadcast_processed_info(object()))
    assert a.received == 1 and b.received == 1


# ==================================================== 5. 无正则注入风险
def test_special_chars_in_source_name():
    """名字里带正则元字符（如 ``[1]``）时也必须能精确匹配。"""
    odd = _Conn("群[1]")
    other = _Conn("群[12]")
    mgr = _make_manager([odd, other])

    _run(mgr.broadcast_processed_info(object(), include=["群[1]"]))
    assert odd.received == 1, "名字含正则元字符时精确匹配失败"
    assert other.received == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
