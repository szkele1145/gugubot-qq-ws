# -*- coding: utf-8 -*-
"""桥接连接器必须在 MCDR 的事件循环上跑插件逻辑（v2.2.10 续）。

背景
----
``connector/bridge_connector.py`` 的消息回调跑在 **websocket 库的收包
线程**里（不是 asyncio 线程）。此前用 ``asyncio.run(...)`` 在该线程里
**临时造一个事件循环**来跑插件逻辑。

``asyncio.run()`` 在协程返回后会**关闭**这个临时循环，关闭时**强制
取消**其中所有未完成的后台任务。本插件里：

* ``logic/system/vote.py`` L1274 ``asyncio.create_task(
  self._monitor_vote(vote.vote_id))``
* ``logic/plugins/player_notice.py`` L248 ``create_task(_delayed_leave())``

都是 fire-and-forget 形态 → 在**桥接通道**下这些任务**刚创建就被取消、
从未真正执行** → **投票监控静默失效（投票永不超时结算）**、延迟离场
提醒永远不触发。

而 QQ 通道（``qq_connector.py`` / ``qq_official/connector.py``）用的是
正规做法：``server.schedule_task(...)`` —— 它把协程提交到 **MCDR 常驻
的 async executor 事件循环**（``task_executor_async.py`` 的
``submit()`` → ``event_loop.create_task(coro)``），循环长驻 → 子任务
不会被取消。

两个必须守住的约束
----------------
1. **必须传「协程对象」或「async def 函数」给 ``schedule_task``**。

   MCDR 的判定顺序（见 ``mcdreforged/plugin/si/server_interface.py``）：

       if inspect.iscoroutine(callable_):           # 协程对象
           → async executor ✓
       elif inspect.iscoroutinefunction(callable_):  # async def 函数
           → async executor ✓
       elif callable(callable_):                   # 普通可调用对象
           → **同步 executor** ✗

   传普通 ``lambda`` 会落到第三条分支 → 被丢进**同步** executor → 同步
   executor 调它得到一个**协程对象**却**从不 await** → 协程体从未执行
   （还会报 ``RuntimeWarning: coroutine ... was never awaited``）。

2. **收包线程不得阻塞等待结果**。

   ``is_admin`` 内部要 ``await qq_connector.bot.get_group_member_list()``
   （网络请求，可能好几秒）→ 若在收包线程里同步等结果，这段时间该连接
   **收不到任何消息** → 桥接消息堆积、严重时像掉线。
"""
import asyncio
import inspect
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

from gugubot.connector import bridge_connector as bc


# ==================================================== 1. 静态检查
def test_bridge_connector_has_no_asyncio_run():
    """回归：桥接连接器不得再使用 asyncio.run。

    asyncio.run 造的是**临时**事件循环，结束时强制取消其中所有未完成
    的后台任务 → 投票监控等 fire-and-forget 任务从未真正执行。
    """
    import ast

    src = pathlib.Path(bc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    hits = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "asyncio"
        ):
            hits.append(node.lineno)

    assert not hits, (
        f"bridge_connector 仍在用 asyncio.run（第 {hits} 行）"
        " —— 必须在 MCDR 事件循环上跑"
    )


def test_bridge_connector_does_not_import_asyncio():
    """回归：asyncio 模块本身已无用途，应移除 import。"""
    import ast

    src = pathlib.Path(bc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "asyncio", "asyncio 已不再使用，应移除 import"


# ==================================================== 2. 传参形态
def _mk_connector(schedule_task):
    conn = bc.BridgeConnector.__new__(bc.BridgeConnector)
    conn.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        debug=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    conn.server = types.SimpleNamespace(
        schedule_task=schedule_task,
        tr=lambda key, **kw: key,
    )
    conn.log_prefix = "[Bridge]"
    return conn


def _mcdr_branch(callable_):
    """复刻 MCDR ``schedule_task`` 的分支判定。

    返回 ``"async"`` 表示会走 async executor（协程会被真正 await），
    ``"sync"`` 表示会走同步 executor（协程对象永远不会被 await）。
    """
    if inspect.iscoroutine(callable_):
        return "async"
    if inspect.iscoroutinefunction(callable_):
        return "async"
    return "sync"


def test_submit_passes_coroutine_object_not_lambda():
    """关键回归：传给 schedule_task 的必须是**协程对象**。

    传普通 lambda → MCDR 丢进同步 executor → 得到的协程对象从不被
    await → 协程体从未执行（比原来的 asyncio.run 更糟：原来至少跑过）。
    """
    submitted = []

    def schedule_task(obj):
        submitted.append(obj)
        return None

    conn = _mk_connector(schedule_task)
    conn._submit_to_mcdr_loop(lambda: _dummy_coro(1))

    assert len(submitted) == 1
    obj = submitted[0]
    assert inspect.iscoroutine(obj), (
        "传给 schedule_task 的必须是协程对象（iscoroutine=True），"
        f"实际拿到 {type(obj).__name__}"
    )
    assert _mcdr_branch(obj) == "async", (
        "传参落到了 MCDR 的同步 executor 分支 → 协程永远不会被 await"
    )
    # 收尾：避免 "coroutine was never awaited" 警告
    obj.close()


def test_submit_does_not_block_caller():
    """收包线程不得阻塞等待结果。

    is_admin 要拉群成员列表（网络请求，可能好几秒）→ 若在这里同步等，
    该连接在这段时间收不到任何消息。
    """
    import time

    def schedule_task(obj):
        # 模拟一个「迟迟不返回」的 MCDR future
        fut = concurrent_future()
        obj.close()
        return fut

    conn = _mk_connector(schedule_task)
    start = time.monotonic()
    conn._submit_to_mcdr_loop(lambda: _dummy_coro(1))
    elapsed = time.monotonic() - start

    assert elapsed < 0.5, (
        f"_submit_to_mcdr_loop 阻塞了 {elapsed:.2f}s —— "
        "收包线程会被拖住，桥接消息堆积"
    )


def test_submit_handles_schedule_failure():
    """schedule_task 抛异常时不得让整个消息处理崩掉。"""
    def schedule_task(_):
        raise RuntimeError("MCDR 未运行")

    conn = _mk_connector(schedule_task)
    conn._submit_to_mcdr_loop(lambda: _dummy_coro(1))  # 不应抛异常


def test_submit_closes_coro_when_scheduling_fails():
    """提交失败时必须显式 close 协程对象。

    否则会报 ``RuntimeWarning: coroutine ... was never awaited``，
    协程帧也不会释放。用 ``warnings.catch_warnings`` 捕获这个警告来
    断言（协程对象的 ``close`` 属性是只读的，没法打桩）。
    """
    import warnings

    def schedule_task(_):
        raise RuntimeError("MCDR 未运行")

    conn = _mk_connector(schedule_task)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        conn._submit_to_mcdr_loop(lambda: _dummy_coro(1))
        # 触发 GC → 未 close 的协程会在这里报 never-awaited
        import gc
        gc.collect()

    never_awaited = [
        w for w in caught
        if "never awaited" in str(w.message)
    ]
    assert not never_awaited, (
        "提交失败后协程未被 close，会触发 never-awaited 警告："
        f"{[str(w.message) for w in never_awaited]}"
    )


def test_submit_handles_coro_factory_raising():
    """协程工厂本身抛异常时也不得外溢。"""
    def schedule_task(_):
        return None

    def bad_factory():
        raise ValueError("boom")

    conn = _mk_connector(schedule_task)
    conn._submit_to_mcdr_loop(bad_factory)  # 不应抛异常


async def _dummy_coro(value):
    return value


def concurrent_future():
    from concurrent.futures import Future
    return Future()


# ==================================================== 3. 机制检查
def test_fire_and_forget_task_survives_on_persistent_loop():
    """机制对照：常驻循环上 fire-and-forget 任务能真正跑完。

    这正是 MCDR ``schedule_task`` 的工作方式（event_loop.create_task
    + 长驻循环）。对照组：``asyncio.run`` 的临时循环会取消它。
    """
    state = {}

    async def monitor():
        await asyncio.sleep(0.2)
        state["done"] = True

    # --- 常驻循环（模拟 MCDR 的 async executor）---
    loop = asyncio.new_event_loop()
    try:
        loop.create_task(monitor())
        loop.call_later(0.5, loop.stop)
        loop.run_forever()
    finally:
        loop.close()
    assert state.get("done") is True, "常驻循环上的后台任务应能跑完"

    # --- 对照：asyncio.run 的临时循环 ---
    state2 = {}

    async def monitor2():
        await asyncio.sleep(0.2)
        state2["done"] = True

    async def main():
        asyncio.create_task(monitor2())

    asyncio.run(main())
    assert state2.get("done") is None, (
        "对照：asyncio.run 的临时循环会取消后台任务（这正是原 bug 的机理）"
    )


def test_bridge_message_flow_does_not_cancel_background_tasks():
    """端到端：桥接消息处理链路上，后台任务不会被取消。

    模拟 MCDR 的常驻循环 + 桥接连接器的 ``_submit_to_mcdr_loop`` →
    内部 fire-and-forget 任务（此处用投票监控的形态）必须活下来。
    """
    state = {}

    async def monitor():
        await asyncio.sleep(0.2)
        state["vote_monitor_done"] = True

    async def process_bridge_message():
        # 模拟 vote.py 的 fire-and-forget 监控任务
        asyncio.create_task(monitor())

    loop = asyncio.new_event_loop()
    try:
        def schedule_task(obj):
            fut = concurrent_future()

            def _runner():
                try:
                    loop.create_task(obj)
                    fut.set_result(None)
                except Exception as e:  # pragma: no cover
                    fut.set_exception(e)

            loop.call_soon_threadsafe(_runner)
            return fut

        conn = _mk_connector(schedule_task)
        # 从**另一个线程**调用（模拟 websocket 收包线程）
        t = threading.Thread(
            target=lambda: conn._submit_to_mcdr_loop(
                lambda: process_bridge_message()
            )
        )
        t.start()
        t.join(timeout=10)

        # 让循环把后台任务跑完
        loop.call_later(0.6, loop.stop)
        loop.run_forever()
    finally:
        loop.close()

    assert state.get("vote_monitor_done") is True, (
        "桥接链路上的 fire-and-forget 任务被取消了（投票监控会静默失效）"
    )


def test_server_message_handles_is_admin_without_blocking():
    """服务端消息处理：is_admin 的网络请求不得拖住收包线程。

    模拟一个「慢」的 is_admin（内部要拉群成员列表），断言
    ``_handle_server_message`` 立即返回。
    """
    import time

    state = {}

    async def slow_is_admin(_sender_id):
        await asyncio.sleep(0.5)
        state["is_admin_done"] = True
        return True

    def schedule_task(obj):
        fut = concurrent_future()

        def _runner():
            try:
                loop.create_task(obj)
                fut.set_result(None)
            except Exception as e:  # pragma: no cover
                fut.set_exception(e)

        loop = asyncio.new_event_loop()
        try:
            loop.call_soon_threadsafe(_runner)
            # 让循环跑起来
            threading.Thread(target=loop.run_forever, daemon=True).start()
            time.sleep(0.05)
        finally:
            pass
        return fut

    conn = _mk_connector(schedule_task)
    conn._is_admin = slow_is_admin
    conn.ws_server = None
    conn.config = types.SimpleNamespace(
        get_keys=lambda *a, **k: False
    )

    start = time.monotonic()
    conn._handle_server_message({"id": 1}, None, '{"processed_message": []}')
    elapsed = time.monotonic() - start

    assert elapsed < 0.3, (
        f"_handle_server_message 阻塞了 {elapsed:.2f}s —— "
        "收包线程被 is_admin 的网络请求拖住"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
