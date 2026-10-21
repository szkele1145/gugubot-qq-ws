# -*- coding: utf-8 -*-
"""WS 服务器客户端列表的并发安全测试（v2.2.10 续）。

背景
----
``ws/websocket_server.py`` 的 ``self.clients`` 会被**两个不同线程**访问：

- websocket 库的收包线程：连接 → ``append``、断开 → ``remove``；
- 插件主线程：``stop()`` / ``broadcast()`` / ``get_clients()``。

修复前**没有任何锁**，导致：

1. ``if client in self.clients: self.clients.remove(client)`` 是
   「先查后删」两步操作 —— 另一线程在两步之间 append/remove 会让
   ``remove`` 删掉**错误的元素**（``list.remove`` 按值删首个匹配）；
2. ``self.clients.copy()`` 撞上并发 append 会抛 ``RuntimeError``，
   或**漏掉刚连上的客户端** → ``stop()`` 不断开它 → 服务器关不掉、
   端口一直被占用（插件 reload 报「端口已被占用」）；
3. ``client['address']`` 在 ``send_message`` 成功**之后**才取值 ——
   缺该键时 KeyError 被 except 吞掉并返回 False，而消息**已经发出去了**
   → 调用方误判失败而重试 → 同一条消息重复发送。

测试策略
--------
竞态本身**难以稳定复现**（CPython 的 GIL 让 list 的单步操作基本原子，
「先查后删」窗口极窄）—— 所以本文件不靠运气碰竞态，而是用
**「锁持有状态守卫」**做确定性验证：把 ``self.clients`` 换成一个会记录
「本次访问是否发生在锁内」的 list 子类，跑一遍所有公开操作，断言
**零违规**。

这样：修复版（有锁）→ 全部访问都在锁内 → 零违规；未修复版（无锁）→
源码里根本没有 ``_clients_lock`` → 守卫用「永不持有」的锁 → 每次访问
都记违规 → 稳定失败。
"""
import sys
import types
import pathlib
import importlib.abc
import importlib.machinery
import threading

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

# websocket_server 库未安装时类会拒绝构造 —— 用 stub 顶替。
#
# 注意：必须**在导入被测模块之前**注入。`websocket_server.py` 顶层做的是
#     try: from websocket_server import WebsocketServer
#     except ImportError: WebsocketServer = None
# —— 库没装时会把模块级全局变量固化成 None。若被测模块**已被别的测试
# 先导入**（全量运行时就会发生：别的测试 import gugubot → 连带 import
# 本模块 → WebsocketServer = None 固化），事后往 sys.modules 里塞 stub
# 也救不回来（import 只在首次执行）。所以这里两条路都走：
#   1. 提前往 sys.modules 塞 stub（让首次 import 拿到它）；
#   2. 若被测模块已在 sys.modules 里（已被别人导入过），直接改它的全局
#      变量把 None 换掉。
stub = types.ModuleType("websocket_server")


class _FakeWSS:
    def __init__(self, *a, **k):
        self.sent = []
        self._fns = {}

    def set_fn_new_client(self, fn): self._fns["new"] = fn
    def set_fn_client_left(self, fn): self._fns["left"] = fn
    def set_fn_message_received(self, fn): self._fns["msg"] = fn
    def run_forever(self):
        import time as _t
        while True:
            _t.sleep(0.01)
    def send_message(self, client, msg): self.sent.append((client, msg))
    def send_message_to_all(self, msg): self.sent.append(("ALL", msg))
    def disconnect_client(self, client, status=1000, reason=""): pass
    def shutdown_gracefully(self): pass


stub.WebsocketServer = _FakeWSS
sys.modules["websocket_server"] = stub

from gugubot.ws import websocket_server as _ws_mod
from gugubot.ws.websocket_server import WebSocketServer

# 被测模块若在 stub 注入前就已被导入（首次 import 时库缺失 → 固化为
# None），此时把全局变量换回 stub 实现。
if getattr(_ws_mod, "WebsocketServer", None) is None:
    _ws_mod.WebsocketServer = _FakeWSS


# ==================================================== 并发守卫工具
class _TrackedLock:
    """可重入锁 + 「当前线程是否持有」查询（用于守卫检测）。"""

    def __init__(self, real_lock=None):
        self._lock = real_lock or threading.RLock()
        self._depth = 0
        self._owner = None

    def __enter__(self):
        self._lock.acquire()
        self._depth += 1
        self._owner = threading.get_ident()
        return self

    def __exit__(self, *exc):
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
        self._lock.release()
        return False

    def held(self) -> bool:
        return self._depth > 0 and self._owner == threading.get_ident()


class _NeverHeldLock:
    """「永不持有」锁 —— 用来代表**源码里根本没有锁**的未修复状态。"""

    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def held(self) -> bool: return False
    def acquire(self, *a, **k): return True
    def release(self): return None


class _GuardedList(list):
    """记录「每次访问是否发生在锁内」的 list。"""

    def __init__(self, lock):
        super().__init__()
        self._lock = lock
        self.violations = []

    def _check(self, op):
        if not self._lock.held():
            self.violations.append(op)

    def append(self, x):
        self._check("append")
        super().append(x)

    def remove(self, x):
        self._check("remove")
        return super().remove(x)

    def __contains__(self, x):
        self._check("contains")
        return super().__contains__(x)

    def __len__(self):
        self._check("len")
        return super().__len__()

    def __iter__(self):
        self._check("iter")
        return super().__iter__()

    def copy(self):
        self._check("copy")
        return list(self)

    def clear(self):
        self._check("clear")
        super().clear()


def _mk_server():
    """真实构造 WebSocketServer（不手搓属性 —— 属性来自被测源码）。"""
    s = WebSocketServer(host="127.0.0.1", port=0)
    s.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        debug=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    return s


def _guard(s):
    """给服务器装上守卫：替换 clients 列表与锁观测器。"""
    real = getattr(s, "_clients_lock", None)
    tracker = _TrackedLock(real) if real is not None else _NeverHeldLock()
    s._clients_lock = tracker
    s.clients = _GuardedList(tracker)
    return s, tracker


# ============================================ 1. 客户端缺 address 不误判
def test_send_message_missing_address_still_returns_true():
    """回归：client 缺 'address' 键时，发送成功必须返回 True。

    修复前 `client['address']` 抛 KeyError → except 吞掉 → 返回 False，
    而消息**已经发出去了** → 调用方误判失败而重试 → 重复发送。
    """
    s = _mk_server()
    s._is_running = True
    sent = []
    s.server = types.SimpleNamespace(
        send_message=lambda client, msg: sent.append(msg)
    )
    client = {"id": 1}  # 故意没有 address
    assert s.send_message(client, "hello") is True
    assert sent == ["hello"]


def test_new_client_missing_address_no_crash():
    """回归：连接回调里 client 缺 'address' 不得抛 KeyError。"""
    s = _mk_server()
    s._handle_new_client({"id": 7}, None)
    assert len(s.clients) == 1


# ============================================ 2. 确定性并发守卫
def test_all_client_list_access_under_lock():
    """确定性检查：所有 clients 访问都必须持锁。

    修复前源码里没有 ``_clients_lock`` → 守卫用「永不持有」锁 →
    每次访问都记违规 → 本测试稳定失败。
    """
    s = _mk_server()
    s, tracker = _guard(s)

    s._handle_new_client({"address": "a"}, None)
    s._handle_client_left({"address": "a"}, None)
    s.get_clients()
    s.get_client_count()
    s.broadcast("hi")
    s.send_message({"address": "a"}, "hi")

    s._is_running = True
    s.server = types.SimpleNamespace(
        send_message=lambda c, m: None,
        send_message_to_all=lambda m: None,
        disconnect_client=lambda c, status=1000, reason="": None,
        shutdown_gracefully=lambda: None,
    )
    s.server_thread = None
    s.stop(timeout=1)

    assert s.clients.violations == [], (
        f"以下 clients 访问未持锁: {sorted(set(s.clients.violations))}"
    )


def test_server_has_clients_lock_attribute():
    """修复版必须提供 clients 锁（这是并发安全的基础设施）。"""
    s = _mk_server()
    assert hasattr(s, "_clients_lock"), "WebSocketServer 缺少 _clients_lock"


# ============================================ 3. 并发压力（回归保护）
def test_concurrent_connect_disconnect_no_crash():
    """并发连接/断开 500 轮，不得抛异常、不得丢元素。"""
    s = _mk_server()
    errors = []

    def worker(idx):
        try:
            for i in range(250):
                client = {"address": f"c{idx}-{i}"}
                s._handle_new_client(client, None)
                s._handle_client_left(client, None)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发下抛异常: {errors}"
    assert len(s.clients) == 0


def test_get_clients_returns_copy():
    """get_clients 必须返回副本：外部改动不能影响内部列表。"""
    s = _mk_server()
    s._handle_new_client({"address": "a"}, None)
    snap = s.get_clients()
    snap.append({"address": "fake"})
    assert len(s.clients) == 1
    assert s.get_client_count() == 1


# ============================================ 4. stop() 不漏客户端
def test_stop_snapshot_includes_late_client():
    """stop() 取快照时刚连上的客户端也必须被断开。

    修复前 `self.clients.copy()` 不加锁：与收包线程的 append 撞车时
    会漏掉该客户端 → 它不会被断开 → 端口一直被占用。
    """
    s = _mk_server()
    disconnected = []
    s._is_running = True
    s.server = types.SimpleNamespace(
        send_message=lambda client, msg: None,
        disconnect_client=lambda client, status=1000, reason="": disconnected.append(client),
        shutdown_gracefully=lambda: None,
    )
    s.server_thread = None

    # 模拟：stop 取快照的同时有新客户端连入
    s._handle_new_client({"address": "late"}, None)

    s.stop(timeout=1)
    assert len(disconnected) == 1
    assert disconnected[0]["address"] == "late"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
