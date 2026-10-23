# -*- coding: utf-8 -*-
"""WS 重连退避只在「真正连上过」后才重置（v2.2.10 续，101）。

问题
----
``_run_forever`` 旧实现无条件 ``backoff = 3``（注释写「连接成功过则重置
退避」），但 ``websocket-client`` 的 ``run_forever()`` 在 **TCP 连不上时
不抛异常**——它内部捕获错误、回调 ``on_error`` 后**正常返回**。

结果：网络中断（或官方网关不可达）期间，每次循环都被判定为「连接成功过」
→ backoff 永远重置回 3s → **每 3 秒一次重连风暴**，并且每轮重连前还会
请求一次 ``/gateway`` REST 接口刷新地址 → 该接口本有频率限制，跟着被打。

修复
----
新增 ``_got_hello`` 标记：收到 op:10 Hello 才置位；``run_forever()``
返回后仅在 ``_got_hello`` 为真时重置 backoff。真正连上过（哪怕随后
断开）→ 按原逻辑重置；从未连上 → 指数退避 3→6→12→24→48→60（封顶）。

测试
----
用桩 ``run_forever``（不抛异常、不触发 Hello）模拟「TCP 连不上」，
断言 backoff 逐轮翻倍；再模拟「连上后断开」（``_on_message`` 注入
Hello），断言 backoff 重置。
"""
import sys
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

from gugubot.connector.qq_official import ws_client as wc


class FL:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


class _StubAPI:
    def get_wss_url(self):
        return {"url": "wss://example.invalid"}


class _StubTM:
    def get_token(self):
        return "TOKEN"


class _StubPool:
    def cleanup(self):
        pass


def _mk_client():
    return wc.QQOfficialWSClient(
        api_client=_StubAPI(),
        token_manager=_StubTM(),
        intents=1 << 25,
        logger=FL(),
        on_dispatch=lambda *a, **k: None,
        on_ready=None,
        reply_pool=_StubPool(),
    )


# ================================================================ 1. 连不上 → 退避翻倍
def test_backoff_doubles_when_never_connected(monkeypatch):
    """TCP 连不上（run_forever 正常返回、无 Hello）→ backoff 指数退避。

    旧实现：backoff 每轮重置为 3 → 3s 重连风暴。
    新实现：3→6→12→24→48→60（封顶）。
    """
    client = _mk_client()
    waits = []

    class _FakeEvent:
        """stop.wait 秒数记录；第 7 轮后置停止。"""
        def __init__(self, outer):
            self._outer = outer
            self._set = False

        def is_set(self):
            return self._set

        def set(self):
            self._set = True

        def clear(self):
            self._set = False

        def wait(self, timeout=None):
            waits.append(timeout)
            if len(waits) >= 7:
                self._set = True
            return self._set

    fake_stop = _FakeEvent(client)
    monkeypatch.setattr(client, "_stop_event", fake_stop, raising=False)
    monkeypatch.setattr(client, "_got_hello", False, raising=False)

    class _NeverWS:
        """模拟 TCP 连不上：run_forever 不抛异常直接返回。"""
        def run_forever(self, *a, **k):
            return None  # 关键：不抛异常（websocket-client 真实行为）

        def close(self):
            pass

    client.ws = _NeverWS()
    # 首次 run（first_run=True 不会刷 URL，避免触发 REST）
    client._run_forever()

    # 7 轮 wait：3, 6, 12, 24, 48, 60, 60（封顶）
    assert waits[:6] == [3, 6, 12, 24, 48, 60], f"退避序列错误: {waits}"
    assert waits[6] == 60, "封顶应保持 60"


# ================================================================ 2. 连上后断开 → 重置
def test_backoff_resets_after_hello(monkeypatch):
    """真正收到 Hello 后断开 → backoff 重置为 3（原语义保留）。"""
    client = _mk_client()
    waits = []

    class _FakeEvent:
        def __init__(self):
            self._set = False
        def is_set(self):
            return self._set
        def set(self):
            self._set = True
        def clear(self):
            self._set = False
        def wait(self, timeout=None):
            waits.append(timeout)
            if len(waits) >= 3:
                self._set = True
            return self._set

    fake_stop = _FakeEvent()
    monkeypatch.setattr(client, "_stop_event", fake_stop, raising=False)

    class _HelloThenCloseWS:
        """首轮：模拟收到 Hello 后断开；后续：模拟连不上。"""
        def __init__(self, outer):
            self._outer = outer
            self._round = 0

        def run_forever(self, *a, **k):
            self._round += 1
            if self._round == 1:
                # 模拟服务端下发 Hello（_got_hello 置位）
                self._outer._on_message(None, '{"op":10,"d":{"heartbeat_interval":30000}}')
            return None

        def close(self):
            pass

    client.ws = _HelloThenCloseWS(client)
    client._run_forever()

    # 首轮收到 Hello → backoff 重置 3；之后连不上 → 6, 12
    assert waits[0] == 3, f"连上过应重置为 3，实际 {waits}"
    assert waits[1] == 6, f"随后连不上应翻倍，实际 {waits}"
    assert waits[2] == 12, f"继续翻倍，实际 {waits}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
