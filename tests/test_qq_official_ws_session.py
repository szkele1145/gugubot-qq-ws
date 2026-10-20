# -*- coding: utf-8 -*-
"""官方 WS 会话失效与回调签名探测的边界修复（v2.2.10）。

背景
----
1. **op:9 的 d 值判定过窄**
   官方 op:9（INVALID_SESSION）的 ``d`` 字段用于区分「必须重新 Identify」
   （d=false）与「可重试 Resume」（d=true）。旧实现写的是
   ``if d_val is False`` —— 只认**恰好是布尔 False** 的对象。
   若官方下发的是字符串 ``"false"`` 或数字 ``0``，判定落空 → 走
   「保留 session 等下个 Hello 重试 Resume」分支 → 拿**已被官方判死**
   的 session 去 Resume → 再次被拒 → 死循环。

2. **op:9 后不主动重连 → 死连接**
   官方发 op:9 只说明「本 session 作废」，并不保证随后关闭连接。
   旧实现只清字段、**不重连** → Identify 永远发不出去（Identify 只在
   新连接的 Hello 之后发）→ 连接看似还在、实则已死，只能等心跳失活
   兜底（心跳间隔官方最长 300s → 最坏离线近 10 分钟）。

3. **回调签名探测靠「试调用 + 捕获 TypeError」**
   旧实现先试 4 参调用，捕获 TypeError 后若异常文案含
   ``argument``/``positional`` 就判定「旧签名」→ 改用 3 参**再处理
   一次同一事件**。而回调内部的业务 TypeError 文案里完全可能出现
   这些词（如 ``got an unexpected keyword argument``）→ **事件重复
   处理**。现改用 ``inspect.signature`` 静态求值，不依赖任何异常。

本文件覆盖上述三点。
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


class FakeWS:
    def __init__(self):
        self.closed = False
        self.sock = None

    def close(self):
        self.closed = True


def _mk_client(on_dispatch=None):
    client = wc.QQOfficialWSClient.__new__(wc.QQOfficialWSClient)
    client._logger = FL()
    client._on_dispatch = on_dispatch
    client._on_ready = None
    client._pool = None
    client.session_id = "sess-1"
    client.last_seq = 42
    client._acked_seq = 42
    client._heartbeat_interval = 30.0
    client._ready = True
    client._hb_stop = type("E", (), {"set": lambda self: None})()
    client._hb_ack_miss = 0
    client._hb_generation = 0
    client._last_close_code = None
    client._dispatch_arg_count = None
    client.ws = FakeWS()
    client.closed_count = 0

    def _schedule_reconnect():
        client.closed_count += 1
        client.ws.close()

    client._schedule_reconnect = _schedule_reconnect
    return client


# ================================================== 1. op:9 的 d 值判定
@pytest.mark.parametrize(
    "d_value",
    [False, 0, "false", "False", "FALSE", "0", " false "],
)
def test_invalid_session_d_false_variants(d_value):
    """回归：d 的各种「假值」形态都要判定为「必须重新 Identify」。"""
    client = _mk_client()
    client._on_message(None, '{"op":9,"d":%s}' % _json_dump(d_value))
    assert client.session_id is None, f"d={d_value!r} 应清除 session_id"
    assert client.last_seq is None
    assert client._acked_seq is None


def _json_dump(v):
    import json
    return json.dumps(v)


@pytest.mark.parametrize("d_value", [True, "true", 1, None])
def test_invalid_session_d_true_keeps_session(d_value):
    """d 为真值 → 保留 session（等下个 Hello 重试 Resume）。"""
    client = _mk_client()
    client._on_message(None, '{"op":9,"d":%s}' % _json_dump(d_value))
    assert client.session_id == "sess-1", f"d={d_value!r} 应保留 session_id"


# ================================================ 2. op:9 后主动重连
def test_invalid_session_triggers_reconnect():
    """回归：op:9 后必须主动重连（否则死连接，等心跳兜底最长 10 分钟）。"""
    client = _mk_client()
    client._on_message(None, '{"op":9,"d":false}')
    assert client.closed_count >= 1, "op:9 应触发重连"
    assert client.ws.closed is True


def test_invalid_session_d_true_also_triggers_reconnect():
    """d=true 同样要重连（否则旧 session 会被反复 Resume 直到被拒）。"""
    client = _mk_client()
    client._on_message(None, '{"op":9,"d":true}')
    assert client.closed_count >= 1


# ============================================ 3. 回调签名探测不靠异常
def test_dispatch_4arg_callback_gets_event_id():
    """4 参回调应收到 event_id（第 4 参）。"""
    got = []

    def cb4(event_type, seq, payload, event_id):
        got.append((event_type, seq, event_id))

    client = _mk_client(cb4)
    client._on_message(
        None,
        '{"op":0,"s":7,"t":"GROUP_AT_MESSAGE_CREATE","id":"EV-1",'
        '"d":{"content":"hi"}}',
    )
    assert got == [("GROUP_AT_MESSAGE_CREATE", 7, "EV-1")]


def test_dispatch_3arg_callback_works():
    """3 参（旧签名）回调正常工作，不多传参数。"""
    got = []

    def cb3(event_type, seq, payload):
        got.append((event_type, seq, payload))

    client = _mk_client(cb3)
    client._on_message(
        None,
        '{"op":0,"s":8,"t":"GROUP_AT_MESSAGE_CREATE","id":"EV-2",'
        '"d":{"content":"hi"}}',
    )
    assert len(got) == 1
    assert got[0][0] == "GROUP_AT_MESSAGE_CREATE"


def test_dispatch_business_typeerror_is_not_treated_as_old_signature():
    """回归：回调内部的业务 TypeError 不得被误判为「旧签名」。

    旧实现：先试 4 参 → 回调内部抛 TypeError（文案含 "argument"）
    → 判定为旧签名 → 改用 3 参**再处理一次同一事件**。
    本用例的回调**同时接受 3 参和 4 参**（默认参数），因此旧实现下
    第二次 3 参调用会**成功执行** → 同一事件被处理两次。
    """
    calls = []

    def cb(event_type, seq, payload, event_id=None):
        calls.append(event_id)
        raise TypeError("got an unexpected keyword argument 'x'")

    client = _mk_client(cb)
    # _on_message 会吞掉 dispatch 异常（记录日志），但位点不得推进
    client._on_message(
        None,
        '{"op":0,"s":9,"t":"GROUP_AT_MESSAGE_CREATE","id":"EV-3",'
        '"d":{"content":"hi"}}',
    )
    assert len(calls) == 1, f"同一事件不得被处理两次，实际 {calls}"


def test_dispatch_business_typeerror_keeps_acked_seq():
    """回归：处理失败不得推进 Resume 位点（事件要能被补发）。"""
    def cb4(event_type, seq, payload, event_id):
        raise TypeError("boom")

    client = _mk_client(cb4)
    client._acked_seq = 5
    client._on_message(
        None,
        '{"op":0,"s":10,"t":"GROUP_AT_MESSAGE_CREATE","d":{}}',
    )
    assert client._acked_seq == 5, "处理失败时 _acked_seq 不得推进"
    assert client.last_seq == 10, "收到的 seq 仍要记录（心跳包 d 需要）"


def test_probe_arity_variants():
    """参数个数探测：3 参 / 4 参 / *args / 不可读签名。"""
    client = _mk_client()
    assert client._probe_dispatch_arity() == 3  # on_dispatch=None → 保守 3 参
    client._on_dispatch = lambda a, b, c: None
    assert client._probe_dispatch_arity() == 3
    client._on_dispatch = lambda a, b, c, d: None
    assert client._probe_dispatch_arity() == 4
    client._on_dispatch = lambda *args: None
    assert client._probe_dispatch_arity() == 4
    # 带默认值的 4 参
    client._on_dispatch = lambda a, b, c, d=None: None
    assert client._probe_dispatch_arity() == 4
    # 只有 2 参 → 保守按 3 参处理
    client._on_dispatch = lambda a, b: None
    assert client._probe_dispatch_arity() == 3
    # 不可读签名的可调用对象 → 保守 3 参
    class Weird:
        def __call__(self, *a, **k): pass
        @property
        def __signature__(self):  # noqa: D401
            raise ValueError("无法读取签名")
    client._on_dispatch = Weird()
    assert client._probe_dispatch_arity() == 3


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
