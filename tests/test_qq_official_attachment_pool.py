# -*- coding: utf-8 -*-
"""官方附件下载的线程池隔离与总时长上限（v2.2.10 续）。

背景
----
1. **下载与发送争抢同一个默认线程池**
   官方事件解析是 async 的，附件下载走 ``asyncio.to_thread`` —— 用的是
   **全局默认** ThreadPoolExecutor（约 32 个线程）。而**发送链路**
   （``connector._send_group`` → REST 请求）同样走 ``asyncio.to_thread``，
   用的是**同一个池**。

   群里一次连发几十个大附件、每个下载最坏耗满 20s 超时 → 下载线程把
   默认池占满 → **所有发送请求排队等待** → 机器人彻底卡死，**一条消息
   都发不出去**。

2. **没有总时长上限**
   ``requests`` 的 ``timeout=20`` 是「每次读操作」的超时，**不是总时长**。
   一个每 30 秒吐 1 字节的慢速源可以永远读下去，把线程占死。

修复
----
- 解析器持有**专用**下载线程池（``_DOWNLOAD_WORKERS = 4``），
  ``_build_segments`` 用 ``loop.run_in_executor(pool, ...)`` 提交；
- ``_download_attachment`` 增加**总时长上限**
  （``_ATTACHMENT_TOTAL_TIMEOUT``），按 ``time.monotonic()`` 判定；
- ``shutdown()`` 释放线程池，连接器 ``disconnect()`` 时调用。
"""
import sys
import time
import types
import pathlib
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

from gugubot.connector.qq_official import parser as pm


class FakeResp:
    """模拟 requests 的流式响应。"""

    def __init__(self, chunks, headers=None, status=200):
        self._chunks = list(chunks)
        self.headers = headers or {}
        self.status_code = status
        self.text = ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=65536):
        for c in self._chunks:
            yield c

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mk_parser():
    p = pm.QQOfficialParser.__new__(pm.QQOfficialParser)
    p._no_slot_warned = set()
    p._not_listened_warned = set()
    p.logger = None
    p.connector = types.SimpleNamespace(
        config=types.SimpleNamespace(
            get_keys=lambda path, default=None: default
        )
    )
    return p


# ================================================ 1. 专用线程池
def test_parser_pool_is_lazy():
    """__new__ 构造的实例也必须能下载（惰性建池）。

    回归：首版把建池放在 __init__ → __new__ 构造的实例 pool 为 None
    → _build_segments 直接跳过下载 → e2e 附件缓存失败。
    """
    p = _mk_parser()
    assert getattr(p, "_download_pool", None) is None, "__new__ 后不应有池"
    pool = p._get_download_pool()
    assert pool is not None
    assert p._download_pool is pool
    p.shutdown()


def test_download_workers_are_few():
    """下载并发必须压得低（默认池约 32，发送链路要留够线程）。"""
    assert pm.QQOfficialParser._DOWNLOAD_WORKERS <= 8, (
        f"下载并发 {pm.QQOfficialParser._DOWNLOAD_WORKERS} 过高，"
        f"会挤占发送链路的线程"
    )


def test_shutdown_releases_pool():
    """shutdown 后 _download_pool 置空；再次使用会惰性重建（幂等安全）。"""
    p = _mk_parser()
    pool1 = p._get_download_pool()
    p.shutdown()
    assert p._download_pool is None
    pool2 = p._get_download_pool()  # 重建可用
    assert pool2 is not None and pool2 is not pool1
    p.shutdown()


def test_build_segments_uses_dedicated_pool():
    """_build_segments 必须把下载提交到专用池，而不是默认线程池。"""
    import asyncio
    p = _mk_parser()
    used = {}

    def fake_cache(url, filename, content_type):
        used["pool"] = getattr(p, "_download_pool", None)
        return None

    p._cache_attachment = fake_cache
    segs = asyncio.new_event_loop().run_until_complete(
        p._build_segments("hi", [{"content_type": "image/png",
                                 "url": "https://x/y.png",
                                 "filename": "y.png"}])
    )
    assert used.get("pool") is p._download_pool, "下载应走专用池"
    assert used.get("pool") is not None, "应已惰性建池"
    assert any(s["type"] == "image" for s in segs)
    p.shutdown()


def test_build_segments_tolerates_closed_pool():
    """shutdown 后仍能处理附件（惰性重建池），不得抛异常。"""
    import asyncio
    p = _mk_parser()
    p.shutdown()
    segs = asyncio.new_event_loop().run_until_complete(
        p._build_segments("hi", [{"content_type": "image/png",
                                 "url": "https://x/y.png",
                                 "filename": "y.png"}])
    )
    assert any(s["type"] == "image" for s in segs), "仍应产出 image 段"
    p.shutdown()


# ================================================ 2. 总时长上限
def test_download_aborts_after_total_timeout(tmp_path, monkeypatch):
    """回归：慢速源（每块之间 sleep）必须在总时长上限内中止。

    旧实现只有 requests 的单次读超时 → 慢速源可以永远读下去。
    """
    p = _mk_parser()
    p._ATTACHMENT_TOTAL_TIMEOUT = 0.2

    def slow_iter(chunk_size=65536):
        for _ in range(50):
            time.sleep(0.05)
            yield b"x" * 1024

    class SlowResp(FakeResp):
        def iter_content(self, chunk_size=65536):
            return slow_iter(chunk_size)

    resp = SlowResp([])

    def fake_get(url, timeout=None, stream=None):
        return resp

    monkeypatch.setattr(pm.requests, "get", fake_get)
    local = tmp_path / "a.bin"
    start = time.monotonic()
    ok = p._download_attachment("https://x/a.bin", local)
    elapsed = time.monotonic() - start
    assert ok is False, "超过总时长上限应放弃"
    assert elapsed < 5, f"应在总时长上限附近中止，实际耗时 {elapsed:.1f}s"
    assert not local.exists(), "失败不应留下成品文件"
    p.shutdown()


def test_download_respects_total_timeout_on_fast_source(tmp_path, monkeypatch):
    """正常速度的下载不应被总时长上限误伤。"""
    p = _mk_parser()
    p._ATTACHMENT_TOTAL_TIMEOUT = 30
    resp = FakeResp([b"hello", b"world"])

    monkeypatch.setattr(
        pm.requests, "get",
        lambda url, timeout=None, stream=None: resp,
    )
    local = tmp_path / "b.bin"
    ok = p._download_attachment("https://x/b.bin", local)
    assert ok is True
    assert local.read_bytes() == b"helloworld"
    p.shutdown()


def test_total_timeout_default_is_reasonable():
    """默认总时长上限必须是有限正值（不能是 None/0/无限）。"""
    v = pm.QQOfficialParser._ATTACHMENT_TOTAL_TIMEOUT
    assert isinstance(v, (int, float)) and 0 < v <= 600, f"默认值不合理: {v}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
