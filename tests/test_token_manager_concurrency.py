# -*- coding: utf-8 -*-
"""QQTokenManager 并发与 token 作废语义测试。

覆盖三个真实缺陷：

1. **invalidate 误伤新 token**：线程 A 用 T1 发请求（T1 已过期），
   同时线程 B 已把 token 刷新成 T2。A 收到 401 后调 invalidate()，
   若无条件清空 → T2 也被作废 → 下一个请求被迫再刷一次 token
   （多一次网络往返 + 限流风险）。修复后只作废「本次实际使用的那枚」。

2. **持锁做网络请求**：旧实现 get_token() 在 _lock 内发起网络请求，
   最坏情况（10s 超时 ×2 次重试 ≈ 20s）全程阻塞其他 API 请求。
   修复后改为单飞（single-flight）：同一时刻只有一个线程真正刷新。

3. **并发重复刷新**：单飞语义下 N 个并发 get_token 只应触发 1 次网络请求。
"""
import sys
import pathlib, types, threading, time, importlib.abc, importlib.machinery


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


sys.meta_path.insert(0, _F())
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'plugin'))

from gugubot.connector.qq_official.token_manager import QQTokenManager


class FL:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


def test_invalidate_only_affects_named_token():
    """invalidate(token=T1) 不应作废当前缓存的 T2。"""
    tm = QQTokenManager("appid", "secret", logger=FL())
    tm._token = "T2"
    tm._expires_at = time.time() + 3600
    tm.invalidate("T1")          # T1 是旧 token，当前已是 T2
    assert tm._token == "T2", f"不应作废 T2，实际 {tm._token}"
    assert tm.get_token() == "T2", "T2 仍应可用"
    print("PASS: invalidate 只作废指定的那枚 token ✅")


def test_invalidate_named_current_token():
    """invalidate(token=当前那枚) 应当确实作废它。"""
    tm = QQTokenManager("appid", "secret", logger=FL())
    tm._token = "T1"
    tm._expires_at = time.time() + 3600
    tm.invalidate("T1")
    assert tm._token is None, f"T1 应被作废，实际 {tm._token}"
    print("PASS: invalidate 作废当前那枚 token ✅")


def test_invalidate_without_arg_clears_all():
    """不带参数调用（向后兼容）→ 无条件清空。"""
    tm = QQTokenManager("appid", "secret", logger=FL())
    tm._token = "T2"
    tm._expires_at = time.time() + 3600
    tm.invalidate()
    assert tm._token is None
    print("PASS: invalidate() 无条件清空 ✅")


def test_concurrent_refresh_only_one_network_call():
    """并发 get_token 只应触发一次网络请求（单飞语义）。"""
    tm = QQTokenManager("appid", "secret", logger=FL())
    calls = []
    fetch_lock = threading.Lock()

    def fake_fetch():
        with fetch_lock:
            calls.append(1)
        time.sleep(0.2)          # 模拟网络耗时
        return "TOKEN1", 7200

    tm._fetch = fake_fetch

    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        try:
            results.append(tm.get_token())
        except Exception as e:
            results.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(results) == 8, results
    assert all(r == "TOKEN1" for r in results), results
    assert len(calls) == 1, f"应只刷新一次，实际 {len(calls)} 次"
    print(f"PASS: 8 并发只触发 1 次刷新（单飞）✅")


def test_invalidate_not_blocked_by_concurrent_refresh():
    """慢刷新进行中时，invalidate() 不应被阻塞。

    旧实现把网络请求放在 _lock 内 → 刷新期间 invalidate() 会被卡住
    （它只是要清两个字段，却要等整轮网络请求）。
    新实现用独立的 _refresh_lock → invalidate 持 _lock 的时间只有瞬时操作。

    真实场景：REST 请求收到 401 → 调 invalidate() 准备刷新 token，
    此时恰好另一个线程正在刷新 → 不该白等一整轮网络请求。
    """
    tm = QQTokenManager("appid", "secret", logger=FL())

    started = threading.Event()
    release = threading.Event()

    def slow_fetch():
        started.set()
        release.wait(timeout=5)
        return "NEW", 7200

    tm._fetch = slow_fetch

    # 缓存过期 → 刷新线程会进入慢路径并持有 _lock（旧实现）
    tm._token = "OLD"
    tm._expires_at = time.time() - 1

    t = threading.Thread(target=lambda: tm.get_token())
    t.start()
    assert started.wait(timeout=2), "刷新线程未启动"

    # 此刻 invalidate 应当立即完成
    t0 = time.time()
    tm.invalidate()
    elapsed = time.time() - t0
    assert elapsed < 0.5, f"invalidate 被慢刷新阻塞了 {elapsed:.2f}s"

    release.set()
    t.join(timeout=5)
    print("PASS: invalidate 不被并发刷新阻塞 ✅")
