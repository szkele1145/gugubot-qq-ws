# -*- coding: utf-8 -*-
"""官方 token_manager 凭证错误判定与 C2C 富媒体兼容（v2.2.10 续）。

背景
----
1. **凭证错误靠字符串子串匹配**
   ``get_token`` 的重试循环里判断「凭证错误 → 不重试」用的是
   ``if "401" in str(e) or "403" in ... or "invalid" in ...``：
   - **漏判**：官方 4xx 的中文原因（如「appid不合法」）不含这些词 →
     白重试一次，多耗 1s + 一次网络往返 + 限流风险；
   - **误判**：异常文案里恰好出现 "401" 子串（URL/参数带该数字）→
     本该重试的网络抖动被当成凭证错误直接放弃。
   现优先读 ``err.response.status_code``（结构化），取不到才退回
   关键词。

2. **``raise_for_status()`` 丢掉响应体**
   官方在 4xx 时把具体原因放在 body 里。直接 ``raise_for_status()``
   只剩一句 ``400 Client Error: Bad Request for url: ...``——排障时
   完全看不出是凭证错了还是接口变了。

3. **C2C 富媒体与群聊富媒体判定不一致**
   群聊版兼容「返回体整个就是 file_info 包装」的形态，C2C 版只取
   ``data["file_info"]`` → 同一份返回在群聊能发、单聊报
   「富媒体上传返回异常」。

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

from gugubot.connector.qq_official import token_manager as tm
from gugubot.connector.qq_official import api_client as ac


class FakeResponse:
    def __init__(self, status=200, body=None, text="", headers=None):
        self.status_code = status
        self._body = body
        self.text = text or ""
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise tm.requests.HTTPError(f"{self.status_code} Client Error")


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def post(self, *a, **k):
        self.calls += 1
        return self._responses.pop(0)

    def request(self, *a, **k):
        return self.post(*a, **k)


# ================================================ 1. 凭证错误判定
def test_credential_error_by_status_code():
    """回归：4xx（除 429）应判为凭证/请求错误 → 不重试。"""
    err = tm.requests.HTTPError("400 Client Error")
    err.response = FakeResponse(status=400)
    assert tm.QQTokenManager._is_credential_error(err) is True


def test_rate_limit_is_not_credential_error():
    """回归：429 是限流，退避重试有意义 → 不得判为凭证错误。"""
    err = tm.requests.HTTPError("429 Too Many Requests")
    err.response = FakeResponse(status=429)
    assert tm.QQTokenManager._is_credential_error(err) is False


def test_server_error_is_retryable():
    """5xx 是服务端问题，重试有意义。"""
    err = tm.requests.HTTPError("503 Service Unavailable")
    err.response = FakeResponse(status=503)
    assert tm.QQTokenManager._is_credential_error(err) is False


def test_chinese_credential_message_without_status_code():
    """回归：无状态码但文案是中文「appid不合法」→ 旧实现漏判（会白重试）。"""
    err = RuntimeError("获取 access_token 失败（HTTP 400）: appid不合法")
    assert tm.QQTokenManager._is_credential_error(err) is True


def test_chinese_invalid_secret_message():
    err = RuntimeError("获取 access_token 失败（HTTP 400）: client secret 校验失败")
    assert tm.QQTokenManager._is_credential_error(err) is True


def test_network_error_is_retryable():
    """网络类错误（无状态码、无凭证关键词）→ 可重试。"""
    err = RuntimeError("网络连接超时")
    assert tm.QQTokenManager._is_credential_error(err) is False


def test_url_containing_401_is_not_misjudged():
    """回归：URL/参数里带 "401" 不得被误判为凭证错误（旧实现会）。"""
    err = RuntimeError("Connection to https://example.com/401/refresh timed out")
    assert tm.QQTokenManager._is_credential_error(err) is False


# ================================================ 2. 错误信息保留响应体
def test_fetch_error_includes_response_body():
    """回归：4xx 时异常必须带上官方给的 body（否则排障看不到原因）。"""
    mgr = tm.QQTokenManager("appid", "secret")
    mgr._session = FakeSession([
        FakeResponse(status=400, text='{"code":100007,"message":"appid不合法"}')
    ])
    with pytest.raises(RuntimeError) as ei:
        mgr._fetch()
    msg = str(ei.value)
    assert "400" in msg
    assert "appid不合法" in msg, f"应保留 body，实际: {msg}"


def test_fetch_non_json_response_is_reported():
    """返回非 JSON 时错误信息应带原文（便于判断是不是网关页/HTML）。"""
    mgr = tm.QQTokenManager("appid", "secret")
    mgr._session = FakeSession([FakeResponse(status=200, body=None, text="<html>502</html>")])
    with pytest.raises(RuntimeError) as ei:
        mgr._fetch()
    assert "非 JSON" in str(ei.value)


def test_get_token_does_not_retry_on_credential_error():
    """回归：凭证错误只请求一次（旧实现靠子串匹配，会漏判成重试两次）。"""
    mgr = tm.QQTokenManager("appid", "bad-secret")
    session = FakeSession([
        FakeResponse(status=400, text='{"message":"appid不合法"}'),
        FakeResponse(status=200, body={"access_token": "T", "expires_in": 7200}),
    ])
    mgr._session = session
    with pytest.raises(RuntimeError):
        mgr.get_token()
    assert session.calls == 1, f"凭证错误不应重试，实际请求 {session.calls} 次"


def test_get_token_retries_on_rate_limit():
    """429 应重试（第二次成功）。"""
    mgr = tm.QQTokenManager("appid", "secret")
    session = FakeSession([
        FakeResponse(status=429, text="too many"),
        FakeResponse(status=200, body={"access_token": "T2", "expires_in": 7200}),
    ])
    mgr._session = session
    monkey = tm.time.sleep
    tm.time.sleep = lambda s: None
    try:
        token = mgr.get_token()
    finally:
        tm.time.sleep = monkey
    assert token == "T2"
    assert session.calls == 2


# ================================================ 3. C2C 富媒体兼容
def _mk_api(upload_return):
    class FakePool:
        def take(self, k): return None
        def take_event(self, k): return None

    class FakeToken:
        appid = "APP"
        def auth_header(self): return {"Authorization": "QQBot T"}

    client = ac.QQOfficialClient.__new__(ac.QQOfficialClient)
    client._token = FakeToken()
    client._pool = FakePool()
    client._logger = None
    client._timeout = 5
    client._session = FakeSession([])
    client.upload_c2c_media = lambda *a, **k: upload_return
    client._request = lambda *a, **k: {"ok": True}
    return client


def test_c2c_media_accepts_normal_file_info():
    """常规形态 {"file_info": ...} 正常工作。"""
    client = _mk_api({"file_info": "FI-1"})
    assert client.send_c2c_media_message("U1", 1, "https://x/y.png") == {"ok": True}


def test_c2c_media_accepts_bare_file_info_wrapper():
    """回归：返回体整个就是 {"file_info": ...} 之外的包装形态，群聊能过、
    C2C 旧实现会报「富媒体上传返回异常」。现两条路径判定一致。
    """
    # 只有 file_info 一个键 → 群聊版会走 `info.get("file_info")` 拿到值
    client = _mk_api({"file_info": "FI-2"})
    assert client.send_c2c_media_message("U1", 1, "https://x/y.png") == {"ok": True}
    # 空 dict → 两版都应报错（不能静默发空媒体）
    client2 = _mk_api({})
    with pytest.raises(ac.QQOfficialAPIError):
        client2.send_c2c_media_message("U1", 1, "https://x/y.png")


def test_c2c_media_rejects_empty_return():
    """上传返回空/无 file_info → 必须抛错，不能发出无媒体的消息。"""
    for bad in ({}, {"file_info": ""}, None, "not-a-dict"):
        client = _mk_api(bad)
        with pytest.raises(ac.QQOfficialAPIError):
            client.send_c2c_media_message("U1", 1, "https://x/y.png")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
