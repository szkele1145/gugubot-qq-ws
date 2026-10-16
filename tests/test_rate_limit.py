# -*- coding: utf-8 -*-
"""v2.2.9 回归：REST 客户端 429 语义 + 可配置超时。

不联网：用桩 requests 拦截 requests.request，断言
1) 429 不刷新 token、不重试（恰好 1 次请求）
2) 429 抛出的错误 status=429，能被上层 _is_rate_limited 识别，
   且不被 _is_stale_reply_ref 误判为「失效额度」
3) 401/403 仍会刷新 token 并重试一次（回归保护）
4) timeout 构造参数生效，默认 15 秒
"""
import importlib.abc
import importlib.machinery
import sys
import types


class _A:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return _A()
    def __call__(self, *a, **k): return _A()


class _SM(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        v = _A(); setattr(self, name, v); return v


class _L(importlib.abc.Loader):
    def create_module(self, s): return _SM(s.name)
    def exec_module(self, m): pass


class _F:
    def find_spec(self, f, p=None, t=None):
        if f.startswith('mcdreforged') or f.startswith('packaging'):
            return importlib.machinery.ModuleSpec(f, _L(), is_package=True)
        return None


sys.meta_path.insert(0, _F())
sys.path.insert(0, r'F:\vibe coding工程\gugubot-qq-ws\plugin')

from gugubot.connector.qq_official import api_client as A  # noqa: E402
from gugubot.connector.qq_official.api_client import (  # noqa: E402
    QQOfficialAPIError,
    QQOfficialClient,
)


class FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = str(self._body)
        # 非空响应体（模拟真实 requests.Response.content 语义）
        self.content = b'{}' if self._body else b''

    def json(self):
        return self._body


class FakeToken:
    """与 QQTokenManager 的公开接口一致（auth_header/appid/get_token/invalidate）。"""

    def __init__(self):
        self.invalidated = 0

    @property
    def appid(self):
        return 'APPID'

    def get_token(self):
        return 'TOKEN'

    def auth_header(self):
        return {'Authorization': 'QQBot TOKEN'}

    def invalidate(self):
        self.invalidated += 1


class FakePool:
    def take(self, g): return ('MID', 0)

    def take_event(self, g): return None


def install_fake_requests(seq):
    """seq: list of (status, body) 依次返回；记录调用次数与超时参数。"""
    calls = {'n': 0, 'timeouts': []}

    def fake_request(method, url, **kw):
        calls['timeouts'].append(kw.get('timeout'))
        i = min(calls['n'], len(seq) - 1)
        calls['n'] += 1
        st, body = seq[i]
        return FakeResp(st, body, {'X-Tps-Trace-Id': 'TRACE'})

    A.requests.request = fake_request  # 直接替换模块内引用
    return calls


def main():
    ok = True

    # ---- 1. 429：不刷新 token、恰好一次请求 ----
    token = FakeToken()
    cli = QQOfficialClient(token, FakePool())
    calls = install_fake_requests([(429, {'code': 11244, 'message': 'rate limited'})])
    err = None
    try:
        cli.send_group_message('G', 'hi', msg_id='MID', msg_seq=0)
    except QQOfficialAPIError as e:
        err = e
    assert err is not None, '429 应抛出 QQOfficialAPIError'
    assert err.status == 429, err.status
    assert calls['n'] == 1, f'429 不得重试，实际请求 {calls["n"]} 次'
    assert token.invalidated == 0, '429 不得刷新 token（限流≠鉴权失效）'
    print('PASS: 429 不重试、不刷新 token（恰 1 次请求）')

    # ---- 2. 上层语义判定：429 属限频，不算失效额度 ----
    from gugubot.connector.qq_official.connector import QQOfficialConnector
    assert QQOfficialConnector._is_rate_limited(err) is True, '429 必须判为限频'
    assert QQOfficialConnector._is_stale_reply_ref(err) is False, \
        '429 不得判为失效额度（否则会误丢被动回复额度）'
    print('PASS: 429 → 限频 True / 失效额度 False（额度不会被误丢）')

    # ---- 3. 回归：401 仍刷新 token 并重试一次 ----
    token2 = FakeToken()
    cli2 = QQOfficialClient(token2, FakePool())
    calls2 = install_fake_requests([
        (401, {'code': 11244, 'message': 'token expired'}),
        (200, {'id': 'MSG_OK'}),
    ])
    out = cli2.send_group_message('G', 'hi', msg_id='MID', msg_seq=0)
    assert out.get('id') == 'MSG_OK', out
    assert calls2['n'] == 2, f'401 应重试一次，实际 {calls2["n"]} 次'
    assert token2.invalidated == 1, '401 应刷新一次 token'
    print('PASS: 401 仍刷新 token 并重试一次（未回归）')

    # ---- 4. 超时可配置：默认 15，可覆盖 ----
    token3 = FakeToken()
    d = QQOfficialClient(token3, FakePool())
    c3 = install_fake_requests([(200, {})])
    d._request('GET', '/test')
    assert c3['timeouts'] == [15], c3['timeouts']
    assert QQOfficialClient.DEFAULT_TIMEOUT == 15
    custom = QQOfficialClient(token3, FakePool(), timeout=30)
    c4 = install_fake_requests([(200, {})])
    custom._request('GET', '/test')
    assert c4['timeouts'] == [30], c4['timeouts']
    print('PASS: 超时可配置（默认 15s，可覆盖为 30s）')

    print('\nRATE LIMIT / TIMEOUT TEST PASSED')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
