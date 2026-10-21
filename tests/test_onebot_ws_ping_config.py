# -*- coding: utf-8 -*-
"""OneBot WS 心跳参数必须真的传下去（v2.2.10 续）。

背景
----
``default_config.yml`` 的 ``connector.QQ.connection`` 段定义了：

    reconnect: 5       # 重连时间
    ping_interval: 5   # 心跳间隔时间（秒）
    ping_timeout: 4    # 心跳超时时间（秒）

``qq_connector.py`` 也确实把后两项读进了 ``self.ping_interval`` /
``self.ping_timeout``（L137-142）—— 但 ``connect()`` 里**硬编码传 0/0**：

    # Disable ping/pong; the OneBot server does not require it.
    self.ws_client.connect(
        reconnect=self.reconnect,
        ping_interval=0,        # ← 配置被忽略
        ping_timeout=0,         # ← 配置被忽略
        ...
    )

→ 用户在配置里写的心跳参数**完全失效**（且读进来就成了死代码）。
对照 ``bridge_connector.py`` L127-128 是**正确**把配置值传下去的。

另一处：``websocket_client.py`` 里 ``ping_timeout`` 被**连带跳过** ——
``ping_interval<=0`` 时整个 ``ping_timeout`` 一起不传 → 用户显式写的
值被静默忽略，run_forever 改用库默认值（10s）。
"""
import sys
import types
import pathlib
import importlib.abc
import importlib.machinery
import threading

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

# websocket-client 库**可能装了也可能没装**（开发机装了 1.9.2），
# 因此不能依赖「库不存在」这一前提 —— 直接把 WebSocketApp 换成
# 记录型 stub，测试只关心「run_forever 收到了什么 kwargs」。
# 注入点必须在被测模块 import 之前；若被测模块已被别人导入过
# （全量运行时就会发生），再补一次「改模块全局变量」的路。
_stub = types.ModuleType("websocket")


class _FakeApp:
    def __init__(self, url, **kwargs):
        self.url = url
        self.kwargs = kwargs
        self.run_kwargs = None
        self.sock = None

    def run_forever(self, **kwargs):
        self.run_kwargs = kwargs

    def close(self): pass


_stub.WebSocketApp = _FakeApp
sys.modules["websocket"] = _stub

from gugubot.ws import websocket_client as _wc_mod
from gugubot.ws.websocket_client import WebSocketClient

# 被测模块若在 stub 注入前已被导入（首次 import 时拿到真库），
# 把它的 websocket 引用换成 stub。
_wc_mod.websocket = _stub


def _capture_connect(**kwargs):
    """跑一次 connect()，返回 run_forever 实际收到的 kwargs。"""
    client = WebSocketClient(url="ws://127.0.0.1:1")
    client.logger = types.SimpleNamespace(
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    client.connect(**kwargs)
    # 线程已经启动 → 直接取 app 上记录的 kwargs
    for _ in range(50):
        if client.ws.run_kwargs is not None:
            break
        threading.Event().wait(0.01)
    return client.ws.run_kwargs


# ================================================ 1. ping_timeout 独立传参
def test_ping_interval_and_timeout_both_passed():
    """正常场景：两个参数都要传下去。"""
    kw = _capture_connect(reconnect=5, ping_interval=5, ping_timeout=4)
    assert kw["ping_interval"] == 5
    assert kw["ping_timeout"] == 4


def test_ping_timeout_not_skipped_when_interval_zero():
    """回归：ping_interval=0 时 ping_timeout 也必须传。

    修复前 ping_timeout 被连带跳过 → 用户显式写的值被忽略，
    run_forever 改用库默认（10s）。
    """
    kw = _capture_connect(reconnect=5, ping_interval=0, ping_timeout=4)
    assert "ping_timeout" in kw
    assert kw["ping_timeout"] == 4
    # ping_interval=0 → 不传（库默认 0 = 禁用协议层 ping）
    assert "ping_interval" not in kw


def test_ping_timeout_zero_not_passed():
    """ping_timeout=0（无意义值）不传，避免覆盖库默认。"""
    kw = _capture_connect(reconnect=5, ping_interval=5, ping_timeout=0)
    assert "ping_timeout" not in kw


# ================================================ 2. reconnect 语义
def test_reconnect_zero_disables():
    """reconnect=0 → 不传（库默认 0 = 禁用自动重连），行为一致。"""
    kw = _capture_connect(reconnect=0, ping_interval=5, ping_timeout=4)
    assert "reconnect" not in kw


def test_reconnect_positive_passed():
    kw = _capture_connect(reconnect=7, ping_interval=5, ping_timeout=4)
    assert kw["reconnect"] == 7


# ================================================ 3. 字符串配置不被吞
def test_string_values_converted_to_int():
    """配置里写成字符串（"5"）也要能工作（不能抛 TypeError）。"""
    kw = _capture_connect(reconnect="5", ping_interval="5", ping_timeout="4")
    assert kw["reconnect"] == 5
    assert kw["ping_interval"] == 5
    assert kw["ping_timeout"] == 4


def test_garbage_values_fall_back_to_default():
    """非法值回退默认，不抛异常。"""
    kw = _capture_connect(reconnect="abc", ping_interval="xyz", ping_timeout=None)
    assert kw["reconnect"] == 5
    assert kw["ping_interval"] == 20
    assert kw["ping_timeout"] == 10


# ================================================ 4. OneBot 连接器确实把配置传下去
def test_qq_connector_passes_configured_ping_values(monkeypatch):
    """回归：OneBot 连接器必须把配置的 ping 值传给 ws_client。

    修复前 qq_connector.connect() 里硬编码 ping_interval=0 /
    ping_timeout=0 → 用户在 default_config.yml 里配的心跳参数被
    完全忽略（读进 self.ping_interval 后成了死代码）。
    """
    from gugubot.connector import qq_connector as qc

    calls = {}

    class _FakeWSClient:
        def connect(self, **kwargs):
            calls.update(kwargs)

    # 替换工厂 → 只关心 connect() 传了什么参数
    monkeypatch.setattr(
        qc.WebSocketFactory, "create_client",
        staticmethod(lambda **kw: _FakeWSClient()),
    )

    conn = qc.QQWebSocketConnector.__new__(qc.QQWebSocketConnector)
    conn.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        debug=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    conn.log_prefix = "[QQ]"
    conn.url = "ws://127.0.0.1:8080"
    conn.token = None
    conn.server = types.SimpleNamespace(
        tr=lambda key, **kw: key
    )
    conn.reconnect = 5
    conn.ping_interval = 5
    conn.ping_timeout = 4
    conn.use_ssl = False
    conn.verify = True
    conn.ca_certs = None
    conn.extra_sslopt = {}

    import asyncio
    asyncio.new_event_loop().run_until_complete(conn.connect())

    assert calls["ping_interval"] == 5, (
        f"OneBot 连接器应传配置的 ping_interval，实际 {calls.get('ping_interval')}"
    )
    assert calls["ping_timeout"] == 4
    assert calls["reconnect"] == 5


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
