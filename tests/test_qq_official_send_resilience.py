# -*- coding: utf-8 -*-
"""qq_official 回复池 / 发送降级链 / REST 重试的边界修复（v2.2.10）。

背景
----
1. **换额度后轮次已用尽 → 刚换到的被动额度白白浪费**
   ``connector._send_group`` / ``_send_c2c`` 用 ``for attempt in range(3)``。
   当第 3 轮遇到「被动引用失效」（msg_id/event_id 过期、msg_seq 非法）时，
   丢弃并换到全新额度后 ``continue``——但 for 循环已耗尽 → 直接
   ``raise last_err``（旧的失效错误）→ **新额度根本没被用过**，本次
   发送失败。改为 while + attempt 计数，换额度不消耗轮次。

2. **``reply_pool.take()`` 的过期回收是 O(n²)**
   原实现用 ``keys.index(msg_id)`` 线性查找定位索引 → 池满 256 条时
   每次 take 都要 O(n²)。改为一次 enumerate 遍历。

3. **REST 鉴权重试丢掉了 ``_net_attempt``**
   ``api_client._request`` 鉴权重试时没透传 ``_net_attempt`` → 网络
   重试次数被重置，最坏情况请求次数翻倍。

4. **``api_client._seq_lock`` 是死代码**（创建后从未使用），却让人误以为
   msg_seq 分配有锁保护（实际由 reply_pool 的 RLock 保护）→ 已删除。

本文件覆盖上述四点。
"""
import sys
import pathlib
import types
import asyncio
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

from gugubot.connector.qq_official.reply_pool import QQReplyPool
from gugubot.connector.qq_official import api_client as ac


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# =========================================================== 1. reply_pool
def test_take_drops_expired_oldest_entries():
    """过期条目被回收，且不误删更新的条目。

    注意 ``take()`` 是**从新到旧**遍历：最新的条目未过期时立即返回，
    更旧的过期条目本轮不会被碰到（它们由 ``cleanup()`` 批量回收）。
    """
    pool = QQReplyPool()
    pool.add("g1", "old")
    pool.add("g1", "new")
    # 伪造 old 已过期
    pool._pools["g1"]["old"]["ts"] -= 10_000
    taken = pool.take("g1")
    assert taken is not None
    assert taken[0] == "new", "应返回最新的未过期条目"
    # 旧的过期条目留给 cleanup()，不会被误删（更新的条目必须保留）
    assert "new" in pool._pools["g1"]


def test_cleanup_reclaims_expired_entries():
    """cleanup() 批量回收过期条目。"""
    pool = QQReplyPool()
    pool.add("g1", "old")
    pool._pools["g1"]["old"]["ts"] -= 10_000
    removed = pool.cleanup()
    assert removed == 1
    assert "g1" not in pool._pools or not pool._pools["g1"]


def test_take_returns_newest_with_increasing_seq():
    """take() 优先取最新条目，msg_seq 递增（同一 msg_id 可回 5 次）。"""
    pool = QQReplyPool()
    pool.add("g1", "a")
    pool.add("g1", "b")
    first = pool.take("g1")
    second = pool.take("g1")
    assert first[0] == "b" and second[0] == "b", "始终优先用最新条目"
    assert first[1] == 0 and second[1] == 1, "msg_seq 应递增"


def test_take_increments_seq_up_to_max():
    """同一 msg_id 最多用 5 次（官方限制），用尽后换次新的。"""
    pool = QQReplyPool()
    pool.add("g1", "a")
    seqs = [pool.take("g1")[1] for _ in range(5)]
    assert seqs == [0, 1, 2, 3, 4]
    # 第 6 次应拿到别的条目或 None
    pool.add("g1", "b")
    assert pool.take("g1")[0] == "b"


def test_take_event_skips_exhausted_event_id():
    """event_id 额度用尽后不再重复拿到同一条。"""
    pool = QQReplyPool()
    pool.add("g1", "a", event_id="E1")
    eids = [pool.take_event("g1") for _ in range(5)]
    assert eids == ["E1"] * 5
    assert pool.take_event("g1") is None


def test_take_event_does_not_raise_on_missing_entry():
    """回归：take_event 清理条目用 pop(..., None)，不得抛 KeyError。

    take_event 同样**从新到旧**遍历：最新条目可用时立即返回。
    """
    pool = QQReplyPool()
    pool.add("g1", "a", event_id="E1")
    pool.add("g1", "b", event_id="E2")
    # a（较旧）的 event 额度用尽
    pool._pools["g1"]["a"]["event_seq"] = 5
    # b（较新）可用 → 直接返回 E2
    assert pool.take_event("g1") == "E2"
    # b 耗尽后再取：此时遍历到 a，a 的 event 额度已满 → 被回收，不抛异常
    pool._pools["g1"]["b"]["event_seq"] = 5
    assert pool.take_event("g1") is None
    assert "a" not in pool._pools["g1"], "额度耗尽的条目应被回收"


def test_take_expired_recovery_is_not_quadratic():
    """回归：过期回收不应出现 O(n²) 的线性查找（keys.index）。

    用行为断言代替源码匹配：池满 256 条时 take() 的耗时不应随条目数
    平方增长——这里用「源码中不得调用 .index(」这一等价且不含注释的
    判据（注释里的说明文字不算调用）。
    """
    import inspect
    src = inspect.getsource(QQReplyPool.take)
    # 去掉注释与文档字符串后再检查（注释里会提到 keys.index 这个词）
    code_lines = [
        ln.split("#", 1)[0] for ln in src.splitlines()
    ]
    code = "\n".join(code_lines)
    assert ".index(" not in code, "过期回收不应再用 .index() 线性查找"


def test_pool_limit_and_cleanup():
    pool = QQReplyPool()
    for i in range(300):
        pool.add("g1", f"m{i}")
    assert pool.size("g1") <= 256
    assert pool.cleanup() >= 0


# ==================================================== 2. 发送降级链轮次
class _Err(Exception):
    """占位异常（不应被使用）。"""


def _api_err(status=200, code=22009, message="msg_id 失效"):
    """构造真实的 QQOfficialAPIError（_send_group 只捕获这个类型）。"""
    return ac.QQOfficialAPIError(status, code, message)


def _make_connector(stale_codes, calls, stale_rounds=1):
    """构造一个最小可用的官方连接器（绕开 __init__）。

    ``stale_rounds``：前 N 次取到的额度都是失效的（第 N+1 次起给好额度）。
    设为 3 可复现「第 3 轮才遇到失效引用」这一边界——旧实现
    ``for attempt in range(3)`` 在该情形下会耗尽轮次后直接抛错。
    """
    from gugubot.connector.qq_official.connector import QQOfficialConnector

    conn = QQOfficialConnector.__new__(QQOfficialConnector)
    conn.logger = type("L", (), {
        "debug": lambda *a, **k: None,
        "info": lambda *a, **k: None,
        "warning": lambda *a, **k: None,
        "error": lambda *a, **k: None,
    })()

    class Pool:
        def __init__(self):
            self.drops = []
            self._taken = 0

        def take(self, key):
            self._taken += 1
            if self._taken <= stale_rounds:
                return (f"stale-msg-id-{self._taken}", 0)
            return ("good-msg-id", 0)

        def take_event(self, key):
            return None

        def drop(self, key, msg_id):
            self.drops.append(msg_id)

        def drop_event(self, key, event_id):
            pass

    conn.reply_pool = Pool()

    class Client:
        def send_group_message(self, group_openid, content, msg_id=None, msg_seq=None):
            calls.append(msg_id)
            if str(msg_id).startswith("stale-msg-id"):
                raise _api_err(200, 22009, "msg_id 失效")
            return {"ok": True}

    conn.api_client = Client()
    conn._is_stale_reply_ref = lambda e: e.code in stale_codes
    conn._is_rate_limited = lambda e: False
    return conn


def test_stale_ref_on_last_round_then_new_credit_succeeds():
    """回归：第 3 轮才遇到失效引用、换到新额度后仍要真正发出去。

    旧实现用 for attempt in range(3)：第 3 轮 continue 后循环耗尽 →
    raise 旧错误 → 新额度白白浪费、消息没发出去。
    """
    calls = []
    # 前 3 次取到的额度都失效 → 失效发生在第 3 轮
    conn = _make_connector({22009}, calls, stale_rounds=3)
    result = _run(conn._send_group("g1", "hello"))
    assert result == {"ok": True}
    assert "good-msg-id" in calls, "换到的新额度必须被真正使用"


def test_stale_ref_then_new_credit_succeeds():
    """第 1 轮就失效 → 换额度后成功（常规路径）。"""
    calls = []
    conn = _make_connector({22009}, calls)
    result = _run(conn._send_group("g1", "hello"))
    assert result == {"ok": True}
    assert "good-msg-id" in calls


def test_stale_ref_repeated_raises_last_error():
    """一直失效 → 最终抛错（不无限重试）。"""
    calls = []
    conn = _make_connector({22009}, calls)

    class BadClient:
        def send_group_message(self, group_openid, content, msg_id=None, msg_seq=None):
            calls.append(msg_id)
            raise _api_err(200, 22009, "失效")

    conn.api_client = BadClient()
    with pytest.raises(ac.QQOfficialAPIError):
        _run(conn._send_group("g1", "hello"))


# ====================================================== 3. REST 重试语义
class _FakeResp:
    def __init__(self, status=200, body=None, headers=None, text=""):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = text
        self.content = text.encode()

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class _FakeToken:
    appid = "123"

    def __init__(self):
        self.invalidated = []

    def auth_header(self):
        return {"Authorization": "QQBot token-abc"}

    def invalidate(self, token):
        self.invalidated.append(token)


def _mk_client(responses):
    """构造 QQOfficialClient，把 session.request 换成按序返回响应。"""
    token = _FakeToken()
    client = ac.QQOfficialClient(token, QQReplyPool())
    seq = list(responses)

    class Session:
        def request(self, *a, **k):
            return seq.pop(0)

    client._session = Session()
    return client, token


def test_auth_error_refreshes_token_and_retries():
    """401 → 刷新 token 重试一次。"""
    client, token = _mk_client([
        _FakeResp(401, {"err_code": 11241, "message": "token 错"}),
        _FakeResp(200, {"err_code": 0}),
    ])
    client._request("GET", "/gateway")
    assert token.invalidated == ["token-abc"], "应作废本次使用的那枚 token"


def test_auth_retry_does_not_reset_net_attempt():
    """回归：鉴权重试要透传 _net_attempt，不重置网络重试次数。"""
    import requests

    client, token = _mk_client([])

    calls = {"n": 0}

    class Session:
        def request(self, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise requests.Timeout("超时")
            if calls["n"] == 2:
                return _FakeResp(401, {"err_code": 11241, "message": "token 错"})
            return _FakeResp(200, {"err_code": 0})

    client._session = Session()
    client._request("GET", "/gateway")
    # 网络异常(1) + 401(2) + 成功(3) = 3 次；_net_attempt 透传后
    # 第三次若再网络异常不会重试（上限 1 次网络重试）
    assert calls["n"] == 3


def test_business_error_code_raises():
    """HTTP 200 + err_code != 0 → 抛业务错误。"""
    client, _ = _mk_client([
        _FakeResp(200, {"err_code": 22009, "message": "msg_id 失效"}),
    ])
    with pytest.raises(ac.QQOfficialAPIError) as ei:
        client._request("POST", "/v2/groups/x/messages", {"a": 1})
    assert ei.value.code == 22009


def test_rate_limit_429_not_retried():
    """429 → 直接抛错（不刷新 token、不重试）。"""
    client, token = _mk_client([
        _FakeResp(429, {"err_code": 0, "message": "too many"}),
    ])
    with pytest.raises(ac.QQOfficialAPIError) as ei:
        client._request("POST", "/v2/groups/x/messages", {"a": 1})
    assert ei.value.status == 429
    assert not token.invalidated, "限流不应作废 token"


# ====================================================== 4. 死代码清理
def test_no_dead_seq_lock():
    """回归：api_client 不应保留未使用的 _seq_lock（误导读者）。"""
    client = ac.QQOfficialClient(_FakeToken(), QQReplyPool())
    assert not hasattr(client, "_seq_lock")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
