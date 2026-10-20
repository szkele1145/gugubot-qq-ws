# -*- coding: utf-8 -*-
"""v2.2.9 回归：REST 客户端 429 语义 + 可配置超时。

**v2.2.10 重大修正**：这个文件此前**从未真正执行过**——

1. 它没有模块级 ``def test_*`` 函数（只有 ``main()``）→ **pytest 收集不到**，
   全量 ``pytest tests/`` 里没有它；
2. 它把 ``A.requests.request`` 换掉，但 ``api_client`` 实际走的是
   ``self._session.request(...)``（复用的 Session 连接池）→ **桩根本没生效**，
   于是走到了真实网络路径；
3. 桩 ``FakeToken.invalidate()`` 不接受 ``used_token`` 参数，与真实
   ``QQTokenManager.invalidate(token)`` 签名不符 → 直接运行会 TypeError。

结果是「429 语义」这条关键回归线**一直是裸奔的**。本文件重写为标准
pytest 用例，桩改为拦截 Session 的 ``request`` 方法。

不联网：全部走桩。
"""
import importlib.abc
import importlib.machinery
import os
import sys
import types

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "plugin"))


# --- mcdreforged 桩（插件包 import 时会用到） ---------------------------
class _A:
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, n):
        return _A()

    def __call__(self, *a, **k):
        return _A()


class _SM(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        v = _A()
        setattr(self, name, v)
        return v


class _L(importlib.abc.Loader):
    def create_module(self, s):
        return _SM(s.name)

    def exec_module(self, m):
        pass


class _F:
    def find_spec(self, f, p=None, t=None):
        if f.startswith("mcdreforged") or f.startswith("packaging"):
            return importlib.machinery.ModuleSpec(f, _L(), is_package=True)
        return None


sys.meta_path.insert(0, _F())

from gugubot.connector.qq_official import api_client as A  # noqa: E402
from gugubot.connector.qq_official.api_client import (  # noqa: E402
    QQOfficialAPIError,
    QQOfficialClient,
)


class FakeResp:
    """模拟 requests.Response 的最小接口。"""

    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = str(self._body)
        self.content = b"x" if self._body else b""

    def json(self):
        return self._body


class FakeToken:
    """与 QQTokenManager 的公开接口一致。

    注意 ``invalidate(token=None)`` 的签名——api_client 会把**本次实际
    使用的那枚 token** 传进来，避免误伤其他线程刚刷新的新 token。
    """

    def __init__(self):
        self.invalidated = []
        self.token = "TOKEN"

    @property
    def appid(self):
        return "APPID"

    def get_token(self):
        return self.token

    def auth_header(self):
        return {"Authorization": "QQBot " + self.token}

    def invalidate(self, token=None):
        self.invalidated.append(token)


class FakePool:
    def take(self, g):
        return ("MID", 0)

    def take_event(self, g):
        return None


def install_fake_requests(monkeypatch, seq):
    """把 ``client._session.request`` 换成桩；seq 为 (status, body) 序列。

    注意：``Session.request`` 是**实例方法**，patch 到类上后调用时会把
    ``self`` 作为第一个实参传入 —— 桩必须写成 ``(self, method, url, **kw)``。

    Returns
    -------
    dict
        ``{"n": 调用次数, "timeouts": [每次的 timeout]}``
    """
    calls = {"n": 0, "timeouts": []}

    def fake_request(self, method, url, **kw):
        calls["timeouts"].append(kw.get("timeout"))
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        st, body = seq[i]
        return FakeResp(st, body, {"X-Tps-Trace-Id": "TRACE"})

    monkeypatch.setattr(A.requests.Session, "request", fake_request)
    return calls


def test_429_no_retry_no_token_refresh(monkeypatch):
    """429 不刷新 token、不重试（恰好 1 次请求）。"""
    token = FakeToken()
    cli = QQOfficialClient(token, FakePool())
    calls = install_fake_requests(
        monkeypatch, [(429, {"err_code": 40034100, "message": "rate limited"})]
    )
    with pytest.raises(QQOfficialAPIError) as exc:
        cli.send_group_message("G", "hi", msg_id="MID", msg_seq=0)
    assert exc.value.status == 429
    assert calls["n"] == 1, f"429 不得重试，实际请求 {calls['n']} 次"
    assert token.invalidated == [], "429 不得刷新 token（限流≠鉴权失效）"


def test_429_semantics_not_stale_ref():
    """上层语义判定：429 属限频，不算失效额度（否则会误丢被动回复额度）。"""
    from gugubot.connector.qq_official.connector import QQOfficialConnector

    err = QQOfficialAPIError(429, 40034100, "触发流量限制（429）")
    assert QQOfficialConnector._is_rate_limited(err) is True
    assert QQOfficialConnector._is_stale_reply_ref(err) is False


def test_401_refreshes_token_and_retries_once(monkeypatch):
    """401 仍刷新 token 并重试一次（回归保护）。"""
    token = FakeToken()
    cli = QQOfficialClient(token, FakePool())
    calls = install_fake_requests(
        monkeypatch,
        [
            (401, {"err_code": 11241, "message": "token expired"}),
            (200, {"id": "MSG_OK"}),
        ],
    )
    out = cli.send_group_message("G", "hi", msg_id="MID", msg_seq=0)
    assert out.get("id") == "MSG_OK"
    assert calls["n"] == 2, f"401 应重试一次，实际 {calls['n']} 次"
    assert token.invalidated == ["TOKEN"], "401 应作废本次使用的那枚 token"


def test_timeout_default_and_override(monkeypatch):
    """超时可配置：默认 15，可覆盖。"""
    token = FakeToken()
    default_client = QQOfficialClient(token, FakePool())
    calls = install_fake_requests(monkeypatch, [(200, {})])
    default_client._request("GET", "/test")
    assert calls["timeouts"] == [15]
    assert QQOfficialClient.DEFAULT_TIMEOUT == 15

    custom = QQOfficialClient(token, FakePool(), timeout=30)
    calls2 = install_fake_requests(monkeypatch, [(200, {})])
    custom._request("GET", "/test")
    assert calls2["timeouts"] == [30]


def test_business_error_in_http_200_raises(monkeypatch):
    """**核心回归**：业务错误藏在 HTTP 200 里也必须抛异常。

    官方《API 调用指南》明确：失败时响应体含 ``err_code``/``message``，
    HTTP 状态码可能仍是 200；且字段名是 ``err_code`` 而非 ``code``。
    此前实现只读 ``code`` 且只在 HTTP>=400 时解析 → 所有业务错误
    （如 msg_id 过期 40034005、频控 40034100）都被当成**发送成功**。
    """
    token = FakeToken()
    cli = QQOfficialClient(token, FakePool())
    install_fake_requests(
        monkeypatch,
        [(200, {"err_code": 40034005, "message": "回复消息msg_id已过期"})],
    )
    with pytest.raises(QQOfficialAPIError) as exc:
        cli.send_group_message("G", "hi", msg_id="MID", msg_seq=0)
    assert exc.value.code == 40034005
    assert "过期" in exc.value.message


def test_err_code_zero_is_success(monkeypatch):
    """err_code 为 0 视为成功，不抛异常。"""
    token = FakeToken()
    cli = QQOfficialClient(token, FakePool())
    install_fake_requests(monkeypatch, [(200, {"err_code": 0, "id": "MSG_OK"})])
    out = cli.send_group_message("G", "hi", msg_id="MID", msg_seq=0)
    assert out.get("id") == "MSG_OK"


def test_stale_ref_codes_recognized():
    """官方文档列出的被动回复失效码必须被判为「额度失效」。"""
    from gugubot.connector.qq_official.connector import QQOfficialConnector

    for code in (304103, 40034005, 40034024, 40034025, 40034026, 40034128):
        err = QQOfficialAPIError(400, code, "回复消息msg_id已过期")
        assert QQOfficialConnector._is_stale_reply_ref(err) is True, code
        assert QQOfficialConnector._is_rate_limited(err) is False, code


def test_rate_limit_code_recognized():
    """官方频控码必须被判为「限频」（退避重试而非丢额度）。"""
    from gugubot.connector.qq_official.connector import QQOfficialConnector

    err = QQOfficialAPIError(200, 40034100, "主动消息发送超过频控限制")
    assert QQOfficialConnector._is_rate_limited(err) is True
    assert QQOfficialConnector._is_stale_reply_ref(err) is False


def test_auth_err_code_in_http_200_refreshes_token(monkeypatch):
    """**回归**：access_token 过期以 HTTP 200 + err_code 11243 返回时，
    同样必须刷新 token 并重试一次。

    此前只在 HTTP 401/403 时刷新 → token 过期后所有请求静默失败。
    """
    token = FakeToken()
    cli = QQOfficialClient(token, FakePool())
    calls = install_fake_requests(
        monkeypatch,
        [
            (200, {"err_code": 11243, "message": "ErrorCheckTokenNotPass"}),
            (200, {"id": "MSG_OK"}),
        ],
    )
    out = cli.send_group_message("G", "hi", msg_id="MID", msg_seq=0)
    assert out.get("id") == "MSG_OK"
    assert calls["n"] == 2, f"token 失效应重试一次，实际 {calls['n']} 次"
    assert token.invalidated == ["TOKEN"], "应作废本次使用的那枚 token"


def test_unknown_stale_ref_code_with_http_200_still_detected():
    """未收录的失效码只要 message 命中关键词，也必须判为失效额度。

    官方业务错误是 HTTP 200 —— 此前兜底逻辑限定 status in (400,404)，
    导致 HTTP 200 的失效引用漏判，每次发送都撞同一个废 id。
    """
    from gugubot.connector.qq_official.connector import QQOfficialConnector

    err = QQOfficialAPIError(200, 99999999, "msg_id 已过期，不能回复")
    assert QQOfficialConnector._is_stale_reply_ref(err) is True


def test_plain_success_body_returned_directly(monkeypatch):
    """无 err_code 的成功响应体应原样返回（不包成 {"raw": ...}）。"""
    token = FakeToken()
    cli = QQOfficialClient(token, FakePool())
    install_fake_requests(monkeypatch, [(200, {"id": "MSG_OK", "timestamp": "T"})])
    out = cli.send_group_message("G", "hi", msg_id="MID", msg_seq=0)
    assert out.get("id") == "MSG_OK"
    assert out.get("timestamp") == "T"


def test_429_with_legacy_code_field_still_exposes_code(monkeypatch):
    """429 响应体带 code 时，抛出的错误应携带该码（便于上层判限频）。"""
    token = FakeToken()
    cli = QQOfficialClient(token, FakePool())
    install_fake_requests(
        monkeypatch, [(429, {"code": 40034100, "message": "rate limited"})]
    )
    with pytest.raises(QQOfficialAPIError) as exc:
        cli.send_group_message("G", "hi", msg_id="MID", msg_seq=0)
    assert exc.value.status == 429
    assert exc.value.code == 40034100


if __name__ == "__main__":
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-v"]))
