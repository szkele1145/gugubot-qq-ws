# -*- coding: utf-8 -*-
"""ws_client 关闭码语义 + Resume 位点分离的回归测试。

覆盖三条 v2.2.10 修复：
1. 官方关闭码 4006/4007/4900-4913 → 必须清空 session_id/last_seq/_acked_seq
   （此前不清 → 下次重连带废 session 去 Resume → 服务端再次拒绝 → 永久离线）；
2. 关闭码 4914/4915（已下架/封禁）→ 必须停止重连（此前无限重连刷屏）；
3. Resume 的 seq 用**处理完成**的位点 `_acked_seq`，与心跳用的
   `last_seq`（收到的）分离——事件处理失败不得推进位点（否则该事件
   永久丢失）。
"""
import sys
import pathlib
import types

import pytest

# ===== stub mcdreforged =====
class _Any:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return _Any()
    def __call__(self, *a, **k): return _A()


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


import importlib.abc, importlib.machinery


class _L(importlib.abc.Loader):
    def create_module(self, s): return _SM(s.name)
    def exec_module(self, m): pass


class _F:
    def find_spec(self, f, p=None, t=None):
        if f.startswith("mcdreforged") or f.startswith("packaging"):
            return importlib.machinery.ModuleSpec(f, _L(), is_package=True)


sys.meta_path.insert(0, _F())
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "plugin"))

from gugubot.connector.qq_official.ws_client import QQOfficialWSClient


class _StubAPI:
    def get_wss_url(self):
        return {"url": "wss://example.invalid"}


class _StubTM:
    def get_token(self):
        return "TOKEN"


class _StubPool:
    def cleanup(self):
        pass


def _mk():
    return QQOfficialWSClient(
        api_client=_StubAPI(),
        token_manager=_StubTM(),
        intents=1 << 25,
        logger=None,
        reply_pool=_StubPool(),
    )


# ---------------------------------------------------------------- 关闭码语义
@pytest.mark.parametrize("code", [4006, 4007, 4008, 4009, 4900, 4905, 4913])
def test_clear_session_codes_reset_session(code):
    """会话失效类关闭码必须清空 session_id / last_seq / _acked_seq。"""
    ws = _mk()
    ws.session_id = "SESS"
    ws.last_seq = 42
    ws._acked_seq = 42
    ws._on_close(None, code, "test")
    assert ws.session_id is None, f"code={code} 后 session_id 应被清空"
    assert ws.last_seq is None, f"code={code} 后 last_seq 应被清空"
    assert ws._acked_seq is None, f"code={code} 后 _acked_seq 应被清空"
    assert ws._stop_event.is_set() is False, f"code={code} 不应停止重连"


def test_4914_4915_stop_reconnect():
    """已下架(4914)/封禁(4915)：官方明确不可连接 → 必须停止重连。"""
    for code in (4914, 4915):
        ws = _mk()
        ws._on_close(None, code, "banned")
        assert ws._stop_event.is_set(), f"code={code} 必须停止重连循环"


def test_auth_codes_stop_reconnect():
    """4010-4013（shard/version/intent 无效）：重连无意义 → 停止循环。"""
    for code in (4010, 4011, 4012, 4013):
        ws = _mk()
        ws._on_close(None, code, "auth")
        assert ws._stop_event.is_set(), f"code={code} 必须停止重连循环"


def test_4014_keeps_retrying():
    """4014（能力未开通）：用户开通后可恢复 → 不停循环（60s 长退避）。"""
    ws = _mk()
    ws._on_close(None, 4014, "disallowed intents")
    assert ws._stop_event.is_set() is False


def test_on_open_clears_last_close_code():
    """连接成功后必须清 _last_close_code：否则曾遇 4014 的连接每次循环
    都会白等 60s 长退避（尽管连接早已正常）。"""
    ws = _mk()
    ws._last_close_code = 4014
    ws._on_open(None)
    assert ws._last_close_code is None


# ------------------------------------------------------- Resume 位点分离
def test_resume_uses_acked_seq_not_last_seq():
    """Resume 的 seq 必须用处理完成的 _acked_seq，而非收到的 last_seq。"""
    ws = _mk()
    ws.session_id = "SESS"
    # 已收到 s=10 但只处理完 s=7
    ws.last_seq = 10
    ws._acked_seq = 7

    sent = []
    ws._send_json = lambda p: sent.append(p) or True
    ws._send_resume()

    assert len(sent) == 1
    assert sent[0]["op"] == 6
    assert sent[0]["d"]["seq"] == 7, "Resume 必须用 _acked_seq（处理完成位点）"
    assert sent[0]["d"]["seq"] != 10


def test_resume_defaults_to_zero():
    """无任何位点时 Resume seq=0（合法占位）。"""
    ws = _mk()
    ws.session_id = "SESS"
    ws.last_seq = None
    ws._acked_seq = None

    sent = []
    ws._send_json = lambda p: sent.append(p) or True
    ws._send_resume()
    assert sent[0]["d"]["seq"] == 0


def test_failed_dispatch_does_not_advance_acked_seq():
    """事件处理抛异常 → 不得推进 _acked_seq（否则该事件永久丢失）。

    官方文档：「推荐开发者在处理过事件之后记录下 s」——处理失败的事件
    必须由 Resume 补发。
    """
    ws = _mk()
    calls = []

    def bad_dispatch(*args):
        calls.append(args)
        raise RuntimeError("处理失败")

    ws._on_dispatch = bad_dispatch
    with pytest.raises(RuntimeError):
        ws._handle_dispatch({"op": 0, "s": 5, "t": "GROUP_AT_MESSAGE_CREATE", "d": {}})

    assert len(calls) == 1, "回调应被调用一次"
    assert ws.last_seq == 5, "收到的 seq 要记（心跳用）"
    assert ws._acked_seq is None, "处理失败不得推进 Resume 位点"


def test_successful_dispatch_advances_both():
    """处理成功 → last_seq 与 _acked_seq 一致推进。"""
    ws = _mk()
    ok_calls = []

    def ok_dispatch(*args):
        ok_calls.append(args)

    ws._on_dispatch = ok_dispatch
    ws._handle_dispatch({"op": 0, "s": 5, "t": "GROUP_AT_MESSAGE_CREATE", "d": {}})
    assert ws.last_seq == 5
    assert ws._acked_seq == 5

    ws._handle_dispatch({"op": 0, "s": 6, "t": "GROUP_AT_MESSAGE_CREATE", "d": {}})
    assert ws.last_seq == 6
    assert ws._acked_seq == 6


def test_ready_resets_seq():
    """READY（新会话）→ 旧会话位点被 READY 自身的 s 覆盖。

    READY 事件本身带 s（=1），处理完成后位点应指向它；旧会话的位点
    （100）由 READY 覆盖，**不再残留**。旧会话位点的作废由「关闭码
    4006/4007/4900-4913」与「op:9 d=false」负责。
    """
    ws = _mk()
    ws.last_seq = 100
    ws._acked_seq = 100
    ws._handle_dispatch(
        {
            "op": 0,
            "s": 1,
            "t": "READY",
            "d": {"session_id": "NEW_SESS", "user": {"id": "U"}},
        }
    )
    assert ws.session_id == "NEW_SESS"
    assert ws.last_seq == 1
    assert ws._acked_seq == 1


def test_clear_session_also_resets_acked():
    """4006 清空时 _acked_seq 一并清空——否则新会话带旧位点 Resume。"""
    ws = _mk()
    ws.session_id = "SESS"
    ws.last_seq = 50
    ws._acked_seq = 50
    ws._on_close(None, 4006, "invalid session id")
    assert ws._acked_seq is None


def test_callback_exception_does_not_kill_ws_loop():
    """回归保护：dispatch 回调抛异常必须继续向上传播（由 _on_message 的
    try/except 捕获），不得静默吞掉——否则位点推进判断失效。"""
    ws = _mk()
    ws._on_dispatch = lambda *a: (_ for _ in ()).throw(RuntimeError("x"))
    with pytest.raises(RuntimeError):
        ws._handle_dispatch({"op": 0, "s": 1, "t": "GROUP_AT_MESSAGE_CREATE", "d": {}})


if __name__ == "__main__":
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-v"]))
