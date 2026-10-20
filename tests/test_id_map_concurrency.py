# -*- coding: utf-8 -*-
"""QQIDMapper 并发正确性测试。

核心不变量：同一个 openid 在任何线程、任何时刻解析出的 legacy 号必须
**始终一致**。一旦出现「同群两个号」，下游的群号→目标映射就会错乱，
消息可能发到错误的群。

说明：这些测试守护的是**不变量**，不是某个已被证实的竞态回归用例。
``to_legacy_group`` 原先在释放锁后才调 ``group_legacy_id()``，中间存在
TOCTOU 窗口；但由于 RLock 紧邻释放、GIL 又让窗口极窄，把它还原成旧写法
后这些测试**依然通过**——即无法稳定复现。修复（整段持锁 + RLock 重入）
是消除窗口的防御性改动，不是靠测试驱动出来的修复。
"""
import sys, types, importlib.abc, importlib.machinery, threading, tempfile
from pathlib import Path


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
sys.path.insert(0, r'F:\vibe coding工程\gugubot-qq-ws\plugin')

from gugubot.connector.qq_official.id_map import QQIDMapper


class FL:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


def test_concurrent_same_openid_same_result():
    """多线程并发解析同一 openid → 结果必须完全一致。"""
    m = QQIDMapper(None, FL())
    results = []
    lock = threading.Lock()

    def worker():
        r = m.to_legacy_group("openidSAME")
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(results)) == 1, f"同一 openid 出现了多个 legacy 号: {set(results)}"
    print(f"PASS: 32 线程并发解析同一 openid → 唯一结果 {results[0]} ✅")


def test_concurrent_bind_and_resolve_consistent():
    """绑定与解析并发时，解析结果不能出现「先真实号后回退虚拟号」。

    注意：受 GIL 与窄窗口影响，这个用例**不能保证复现**原竞态；它保证的是
    「一旦绑定生效，后续解析不会再退回虚拟号」这条不变量成立。
    """
    m = QQIDMapper(None, FL())
    m.register_legacy_groups(["111111"])

    seen = []
    lock = threading.Lock()

    def resolver():
        for _ in range(200):
            r = m.to_legacy_group("openidX")
            with lock:
                seen.append(r)

    def binder():
        # 先让 resolver 跑起来（可能已分配虚拟号），再绑定
        for _ in range(50):
            if len(seen) > 10:
                break
        m.bind_group_openid("openidX")

    t1 = threading.Thread(target=resolver)
    t2 = threading.Thread(target=binder)
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert seen, "应有解析结果"
    first_real = None
    for i, v in enumerate(seen):
        if v == "111111":
            first_real = i
            break
    if first_real is not None:
        after = seen[first_real:]
        assert all(v == "111111" for v in after), (
            f"绑定到真实群号后又退回虚拟号: {after}"
        )
    print(f"PASS: 绑定/解析并发无回退（{len(seen)} 次解析）✅")


def test_concurrent_new_openids_unique():
    """并发处理大量新 openid → 每个都拿到唯一虚拟号，不重复。"""
    m = QQIDMapper(None, FL())
    results = {}
    lock = threading.Lock()

    def worker(idx):
        oid = f"openid_{idx}"
        r = m.to_legacy_group(oid)
        with lock:
            results[oid] = r

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(64)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 64
    vals = list(results.values())
    assert len(set(vals)) == 64, f"虚拟号重复: {len(vals) - len(set(vals))} 个冲突"
    # 反向解析必须一一对应
    for oid, legacy in results.items():
        assert m.to_official_group(legacy) == oid, (oid, legacy)
    print("PASS: 64 个并发新 openid 全部拿到唯一虚拟号 ✅")


def test_user_and_group_namespaces_independent():
    """用户号与群号使用不同起始段，互不干扰。"""
    m = QQIDMapper(None, FL())
    u = m.to_legacy_user("useropenid")
    g = m.to_legacy_group("groupopenid")
    assert u != g, (u, g)
    assert u.startswith("1"), u
    assert g.startswith("2"), g
    # 反向解析各自正确
    assert m.to_official_user(u) == "useropenid"
    assert m.to_official_group(g) == "groupopenid"
    print(f"PASS: 用户号 {u} 与群号 {g} 命名空间独立 ✅")
