# -*- coding: utf-8 -*-
"""桥接「客户端模式」的启动失败不得中断插件加载（v2.2.10 续，80）。

背景
----
这是 #78 的**对称缺口**。#78 给 ``bridge_connector._start_server()``
（服务器模式）加了 try/except，理由是「桥接不可用是降级，不能让它冒泡
出去中断整个插件加载」。但**客户端模式**（``is_main_server=False``，
从服务器连主服务器）的 ``_connect_to_server()`` 当时**没有**加。

调用链：

.. code-block:: python

    on_load → ConnectorManager.register_connector(connector)
                 └── await connector.connect()          # bridge_connector.connect()
                       └── self._connect_to_server()     # ← 裸调用，无保护
    except Exception:
        logger.error(...)
        raise                                          # ← 重新抛出

``register_connector`` 会**记日志后重新抛出** → 异常继续冒泡到
``on_load`` → **插件加载中断**。也就是说：配置里一个坏 URL、一个非法
的 ``sslopt``、或者 ``websocket-client`` 环境异常，会让**整个插件**
（包括 QQ ↔ MC 主链路）起不来 —— 而本来只是桥接这一个功能不可用。

这个缺口是我在 #78 里留下的：修了一个方向，没顺手检查反方向。

修复
----
``_connect_to_server()`` 与 ``_start_server()`` 对称：把「创建客户端」与
「连接」分别包在 try/except 里，失败时记一条含处置建议的 error 并返回，
**不冒泡**。

测试手法
--------
用一个只提供该方法所需属性的轻量 stub 当 ``self``，把
``WebSocketFactory.create_bridge_client`` 换成会抛异常的假实现 /
返回「connect() 会抛」的假客户端，直接调用真实的
``BridgeConnector._connect_to_server``。
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

import gugubot.connector.bridge_connector as bc  # noqa: E402


class _Logger:
    def __init__(self):
        self.records = []

    def _add(self, level, msg, *a):
        try:
            text = msg % a if a else str(msg)
        except Exception:
            text = f"{msg} {a}"
        self.records.append((level, text))

    def debug(self, m, *a): self._add("debug", m, *a)
    def info(self, m, *a): self._add("info", m, *a)
    def warning(self, m, *a): self._add("warning", m, *a)
    def error(self, m, *a): self._add("error", m, *a)

    def text(self):
        return "\n".join(t for _, t in self.records)


class _Stub(bc.BridgeConnector):
    """绕过 ``__init__``（它要 config / server），只提供本方法需要的属性。"""

    def __init__(self):  # noqa: D107 - 故意不调 super()
        self.ws_client = None
        self._connect_count = 0
        self.logger = _Logger()
        self.log_prefix = "[Bridge]"
        self.config = types.SimpleNamespace(
            get_keys=lambda path, default=None: default
        )
        self.reconnect = 5
        self.ping_interval = 20
        self.ping_timeout = 10
        self.use_ssl = False
        self.verify = True
        self.ca_certs = None
        self.extra_sslopt = {}
        self._client_id = "stub"


# ==================================================== 1. 创建失败不冒泡
def test_connect_to_server_survives_factory_error(monkeypatch):
    """``create_bridge_client`` 抛异常时，``_connect_to_server`` 必须不冒泡。

    否则异常会经 ``ConnectorManager.register_connector`` 重新抛出 →
    ``on_load`` → **整个插件加载中断**，而本来只是桥接功能不可用。
    """
    stub = _Stub()

    def boom(*a, **k):
        raise RuntimeError("websocket-client 未安装")

    monkeypatch.setattr(bc.WebSocketFactory, "create_bridge_client", boom)

    # 不抛 = 通过
    bc.BridgeConnector._connect_to_server(stub)

    assert "error" in [lvl for lvl, _ in stub.logger.records], (
        "创建客户端失败却没有任何 error 日志 —— 用户完全不知道桥接没起来"
    )


# ==================================================== 2. connect() 失败不冒泡
def test_connect_to_server_survives_connect_error(monkeypatch):
    """``ws_client.connect()`` 抛异常时也必须不冒泡。"""
    stub = _Stub()

    class _BadClient:
        def connect(self, **kwargs):
            raise ValueError("非法 URL: ws://:::")

        def is_connected(self):
            return False

        def disconnect(self, timeout=1):
            pass

    monkeypatch.setattr(
        bc.WebSocketFactory, "create_bridge_client",
        lambda *a, **k: _BadClient(),
    )

    bc.BridgeConnector._connect_to_server(stub)
    assert "error" in [lvl for lvl, _ in stub.logger.records]


# ==================================================== 3. 正常路径仍然连接
def test_connect_to_server_still_connects(monkeypatch):
    """加固不能把正常路径弄坏：成功时仍要真的调 ``connect()``。"""
    stub = _Stub()
    calls = {}

    class _GoodClient:
        def connect(self, **kwargs):
            calls["kwargs"] = kwargs

        def is_connected(self):
            return False

        def disconnect(self, timeout=1):
            pass

    monkeypatch.setattr(
        bc.WebSocketFactory, "create_bridge_client",
        lambda *a, **k: _GoodClient(),
    )

    bc.BridgeConnector._connect_to_server(stub)

    assert "kwargs" in calls, "正常路径没有调用 ws_client.connect()"
    assert stub.ws_client is not None, "正常路径没有保存客户端实例"
    assert stub._connect_count == 1, "连接计数没有递增（影响重连日志）"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
