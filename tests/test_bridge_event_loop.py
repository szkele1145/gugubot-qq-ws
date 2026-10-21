# -*- coding: utf-8 -*-
"""桥接连接器必须在 MCDR 的事件循环上跑插件逻辑（v2.2.10 续）。

背景
----
``connector/bridge_connector.py`` 的消息回调跑在 **websocket 库的收包
线程**里。此前用 ``asyncio.run(...)`` 在该线程里**临时造一个事件循环**
来跑插件逻辑。

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

测试策略
--------
1. **静态检查**：桥接连接器源码里不得再出现 ``asyncio.run``；
2. **行为检查**：``_run_on_mcdr_loop`` 必须把协程提交给
   ``server.schedule_task``，并且**同步**取回结果；
3. **机制检查**：在**常驻**事件循环上创建的 fire-and-forget 任务能真正
   跑完（对照：在 ``asyncio.run`` 的临时循环上会被取消）—— 用它证明
   修复方向是对的。
"""
import asyncio
import sys
import types
import pathlib
import importlib.abc
import importlib.machinery
import threading
import time

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
        # 只看真正的调用节点（文档字符串里的文字不算）
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


# ==================================================== 2. 行为检查
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


def test_run_on_mcdr_loop_uses_schedule_task():
    """_run_on_mcdr_loop 必须把协程交给 server.schedule_task。"""
    submitted = []

    def schedule_task(coro_factory):
        submitted.append(coro_factory)
        fut = concurrent_future()
        fut.set_result("ok")
        return fut

    conn = _mk_connector(schedule_task)
    result = conn._run_on_mcdr_loop(lambda: _dummy_coro("hello"))

    assert len(submitted) == 1, "协程没有被提交给 server.schedule_task"
    assert result == "ok"


def test_run_on_mcdr_loop_returns_coroutine_result():
    """_run_on_mcdr_loop 必须同步取回协程的返回值。"""
    def schedule_task(coro_factory):
        # 真实地跑一遍协程并把结果塞进 future
        fut = concurrent_future()

        def _runner():
            try:
                loop = asyncio.new_event_loop()
                try:
                    fut.set_result(loop.run_until_complete(coro_factory()))
                finally:
                    loop.close()
            except Exception as e:  # pragma: no cover
                fut.set_exception(e)

        t = threading.Thread(target=_runner)
        t.start()
        t.join(timeout=5)
        return fut

    conn = _mk_connector(schedule_task)
    assert conn._run_on_mcdr_loop(lambda: _dummy_coro(42)) == 42


def test_run_on_mcdr_loop_handles_schedule_failure():
    """schedule_task 抛异常时不得让整个消息处理崩掉。"""
    def schedule_task(_):
        raise RuntimeError("MCDR 未运行")

    conn = _mk_connector(schedule_task)
    assert conn._run_on_mcdr_loop(lambda: _dummy_coro(1)) is None


def test_run_on_mcdr_loop_handles_timeout():
    """协程卡住超时 → 返回 None，不无限期阻塞收包线程。"""
    def schedule_task(coro_factory):
        fut = concurrent_future()
        # 故意不设结果 → 永远 pending
        return fut

    conn = _mk_connector(schedule_task)
    assert conn._run_on_mcdr_loop(lambda: _dummy_coro(1), timeout=0.1) is None


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
        # 模拟 MCDR 的 task_awaiter：让循环跑一会儿
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

    模拟 MCDR 的常驻循环 + 桥接连接器的 ``_run_on_mcdr_loop`` →
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
        def schedule_task(coro_factory):
            fut = concurrent_future()
            task_holder = {}

            def _runner():
                try:
                    task_holder["t"] = loop.create_task(coro_factory())
                    fut.set_result(None)
                except Exception as e:  # pragma: no cover
                    fut.set_exception(e)

            loop.call_soon_threadsafe(_runner)
            return fut

        conn = _mk_connector(schedule_task)
        # 从**另一个线程**调用（模拟 websocket 收包线程）
        t = threading.Thread(
            target=lambda: conn._run_on_mcdr_loop(
                lambda: process_bridge_message(), timeout=5
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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
