# -*- coding: utf-8 -*-
"""主动消息遇「读超时」不得重试（v2.2.10 续，98）。

问题
----
``qq_official/api_client.py`` 的 ``_request`` 在网络异常时**无条件重试
一次**：

.. code-block:: python

    except requests.RequestException as e:
        if _net_attempt < 1:
            time.sleep(1)
            return self._request(..., _net_attempt=_net_attempt + 1)
        raise QQOfficialAPIError(0, 0, f"网络异常: {e}") from e

注释给的理由是「幂等的读操作与消息发送均安全——官方按 msg_id+msg_seq
去重」。**但那只对「被动回复」成立**：主动消息（payload 里没有
``msg_id`` / ``event_id``）根本没有去重键。

而 ``requests.RequestException`` 包含 **``ReadTimeout``** —— 读超时的
语义是「请求**可能已经被服务端处理**，只是响应没等到」。此时重试：

* 被动回复 → 官方用 msg_id+msg_seq 去重，安全；
* **主动消息 → 群里出现两条一模一样的消息**（且用户在客户端看不出
  是哪边的问题）。

真正安全的判据是**幂等性 + 失败阶段**：

* ``ConnectTimeout`` / ``ConnectionError``：连接都没建立 → 请求**未送达**
  → 任何请求都可以安全重试；
* ``ReadTimeout``：请求可能已被处理 → **只有幂等/带去重键的请求**才可重试。

修复
----
按上述判据决定是否重试。
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

# 出现两份类身份，其他测试文件的 except QQOfficialAPIError 就抓不到
# 本模块抛出的异常（实测会打挂 test_send_quota.py）。
from gugubot.connector.qq_official import api_client as ac  # noqa: E402


class _Logger:
    def __getattr__(self, n):
        return lambda *a, **k: None


def _make_client(monkeypatch, exc):
    """造一个 API 客户端；session.request 永远抛 exc，并记录调用次数。"""
    calls = []

    def _boom(*a, **k):
        calls.append(1)
        raise exc

    inst = ac.QQOfficialClient.__new__(ac.QQOfficialClient)
    inst._logger = _Logger()
    inst._timeout = 5
    inst._token = types.SimpleNamespace(
        auth_header=lambda: {"Authorization": "QQBot T"},
        appid="123",
    )
    inst._pool = types.SimpleNamespace(
        take=lambda key: None,
        take_event=lambda key: None,
        drop=lambda *a: None,
        drop_event=lambda *a: None,
    )
    inst._session = types.SimpleNamespace(request=_boom)
    # 把 sleep 掉，避免测试慢
    monkeypatch.setattr(ac.time, "sleep", lambda *_: None)
    return inst, calls


def _post(inst, payload):
    return inst._request("POST", "/v2/groups/G/messages", payload)


# ==================================================== 1. 主动消息：读超时不重试
def test_active_message_read_timeout_is_not_retried(monkeypatch):
    """主动消息（无 msg_id/event_id）遇读超时**不得**重试 —— 否则发两遍。"""
    exc = ac.requests.exceptions.ReadTimeout("read timed out")
    inst, calls = _make_client(monkeypatch, exc)

    with pytest.raises(ac.QQOfficialAPIError):
        _post(inst, {"msg_type": 0, "content": "hi"})

    assert len(calls) == 1, (
        f"主动消息在读超时后被重试了 {len(calls)} 次 —— "
        f"读超时的语义是「请求可能已被服务端处理」，而没有 msg_id 的主动"
        f"消息**没有去重键** → 群里会出现两条一模一样的消息"
    )


# ==================================================== 2. 主动消息：连接失败可重试
def test_active_message_connect_error_is_retried(monkeypatch):
    """连接失败 = 请求**未送达** → 主动消息也可以安全重试。"""
    exc = ac.requests.exceptions.ConnectTimeout("connect timed out")
    inst, calls = _make_client(monkeypatch, exc)

    with pytest.raises(ac.QQOfficialAPIError):
        _post(inst, {"msg_type": 0, "content": "hi"})

    assert len(calls) == 2, (
        f"连接阶段失败只发了 {len(calls)} 次 —— 请求未送达，应当退避重试一次"
    )


def test_active_message_connection_error_is_retried(monkeypatch):
    exc = ac.requests.exceptions.ConnectionError("refused")
    inst, calls = _make_client(monkeypatch, exc)
    with pytest.raises(ac.QQOfficialAPIError):
        _post(inst, {"msg_type": 0, "content": "hi"})
    assert len(calls) == 2


# ==================================================== 3. 被动回复：读超时可重试
def test_passive_reply_read_timeout_is_retried(monkeypatch):
    """被动回复带 msg_id → 官方按 msg_id+msg_seq 去重 → 读超时可重试。"""
    exc = ac.requests.exceptions.ReadTimeout("read timed out")
    inst, calls = _make_client(monkeypatch, exc)

    with pytest.raises(ac.QQOfficialAPIError):
        _post(inst, {"msg_type": 0, "content": "hi",
                     "msg_id": "M1", "msg_seq": 0})

    assert len(calls) == 2, (
        f"带 msg_id 的被动回复没有重试（{len(calls)} 次）—— "
        f"官方按 msg_id+msg_seq 去重，重试是安全的"
    )


def test_event_reply_read_timeout_is_retried(monkeypatch):
    exc = ac.requests.exceptions.ReadTimeout("read timed out")
    inst, calls = _make_client(monkeypatch, exc)
    with pytest.raises(ac.QQOfficialAPIError):
        _post(inst, {"msg_type": 0, "content": "hi", "event_id": "E1"})
    assert len(calls) == 2


# ==================================================== 4. GET 天然幂等
def test_get_is_retried(monkeypatch):
    exc = ac.requests.exceptions.ReadTimeout("read timed out")
    inst, calls = _make_client(monkeypatch, exc)
    with pytest.raises(ac.QQOfficialAPIError):
        inst._request("GET", "/gateway")
    assert len(calls) == 2, "GET 是幂等的，读超时应当重试"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
