# -*- coding: utf-8 -*-
"""不活跃检查的同步 HTTP 阻塞事件循环（v2.2.10 续）。

问题
----
``logic/plugins/inactive_check.py`` 的 ``_get_bedrock_player_uuid`` 用
**同步**的 ``requests.get(url, timeout=10)`` 查 mcprofile.io，而它在
``_check_inactive_players`` 里是**串行**调用的::

    for bedrock_name in player.bedrock_name:
        uuid = self._get_bedrock_player_uuid(bedrock_name)   # ← 同步阻塞

整条链都跑在 **MCDR 事件循环**上（``_schedule_check`` → ``await
self._check_inactive_players()``），中间**没有** ``run_in_executor``。

后果
----
* 一次 HTTP 请求最长阻塞事件循环 **10 秒**（timeout）；
* 玩家数 × 基岩名数量 → 阻塞时间**线性累加**；
* 阻塞期间 **MCDR 事件循环完全停摆** → QQ 消息、命令、其他定时任务
  全部排队等待。

这属于典型的「在 async 函数里调同步阻塞 IO」。

修复
----
把 HTTP 请求挪到线程池（``loop.run_in_executor``），并把玩家循环里的
**串行**查询改成**并发**查询（``asyncio.gather``），让总耗时由「最慢的
一个」决定而不是「所有之和」。

测试
----
用「一个假的同步慢函数」验证：修复后事件循环在查询期间**仍能继续跑**
别的协程（即不再被阻塞）。
"""
import asyncio
import pathlib
import sys
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

from gugubot.logic.plugins import inactive_check as ic


# ==================================================== 1. 静态：不得有裸的同步 HTTP
def test_no_blocking_requests_in_event_loop():
    """回归：同步 HTTP 不得从协程里**直接**调用。

    判定规则（按真实故障模式定，不做过度约束）：

    * ``requests.get`` 所在函数可以是**同步** def（HTTP 库本来就是同步的）
    * 但**协程里不得直接调用那个同步函数** —— 必须经
      ``run_in_executor`` 丢到线程池，否则同步阻塞发生在事件循环里

    实现：AST 找出所有「调用了含 ``requests.get`` 的同步函数」的
    **协程**调用点；这些协程内部必须用了 ``run_in_executor``。
    """
    src = pathlib.Path(ic.__file__).read_text(encoding="utf-8")
    import ast

    tree = ast.parse(src)

    # 1) 哪些同步函数里调用了 requests.get
    blocking_fns = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(fn, ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "requests"
            ):
                blocking_fns.add(fn.name)

    assert blocking_fns, "审计失效：没找到任何含 requests.get 的函数"

    # 2) 哪些协程直接调用了它们
    #    调用形态有两种：裸函数 `f(...)` 与方法 `self.f(...)`
    direct_calls = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        has_executor = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("run_in_executor", "to_thread")
            for n in ast.walk(fn)
        )
        if has_executor:
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            called = None
            if isinstance(node.func, ast.Name):
                called = node.func.id
            elif isinstance(node.func, ast.Attribute):
                called = node.func.attr
            if called in blocking_fns:
                direct_calls.append((fn.name, node.lineno, called))

    assert not direct_calls, (
        f"inactive_check.py 里协程直接调用了含 requests.get 的同步函数: "
        f"{direct_calls} → 同步阻塞 HTTP 会卡住整个 MCDR 事件循环"
        "（timeout=10 秒起，且按玩家数线性累加）"
    )


# ==================================================== 2. 运行时：事件循环不被阻塞
def test_event_loop_is_not_blocked_during_lookup():
    """运行时回归：查询基岩版 UUID 期间，事件循环必须还能跑别的协程。

    做法：把 ``requests.get`` 换成「睡 0.3 秒」的假实现，然后：

    * 调 ``_get_bedrock_player_uuid``（经 executor）
    * 同时跑一个心跳协程，每 0.02 秒记一次时间戳
    * 断言心跳在查询期间**继续跳动**（修复前会完全停 0.3 秒）
    """
    import time

    sys_obj = ic.InactiveCheckSystem.__new__(ic.InactiveCheckSystem)
    sys_obj.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        debug=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    # 缓存里没有该玩家 → 会走 HTTP 分支
    sys_obj._data = {"bedrock_cache": {}}
    sys_obj.get = lambda k, d=None: (sys_obj._data.get(k, d))
    sys_obj.__setitem__ = lambda k, v: sys_obj._data.__setitem__(k, v)
    # config 是 BotConfig 的薄封装，这里只需要 get_keys
    sys_obj.config = types.SimpleNamespace(
        get_keys=lambda path, default=None: (
            15552000 if path[-1] == "bedrock_cache_ttl" else default
        )
    )
    sys_obj.save = lambda: None

    def fake_get(url, headers=None, timeout=None):
        time.sleep(0.3)  # 模拟慢速 HTTP
        return types.SimpleNamespace(
            status_code=404,
            json=lambda: {},
        )

    # 替换模块级 requests
    orig_requests = ic.requests
    ic.requests = types.SimpleNamespace(
        get=fake_get,
        exceptions=types.SimpleNamespace(
            Timeout=type("Timeout", (Exception,), {}),
            RequestException=type("RequestException", (Exception,), {}),
        ),
    )
    try:
        async def run():
            beats = []

            async def heartbeat():
                # 持续心跳，直到 lookup 结束
                while not lookup_done.is_set():
                    beats.append(time.time())
                    await asyncio.sleep(0.01)

            lookup_done = asyncio.Event()
            hb = asyncio.create_task(heartbeat())

            # 修复版有 _get_bedrock_player_uuid_async（走线程池）；
            # 修复前只有同步版 → 直接调它（在协程里）→ 正好复现「阻塞」。
            lookup_fn = getattr(
                sys_obj, "_get_bedrock_player_uuid_async", None
            )
            if lookup_fn is None:
                # 旧代码路径：协程里直接调同步方法 → 事件循环被阻塞
                async def lookup_fn(name):
                    return sys_obj._get_bedrock_player_uuid(name)

            lookup = asyncio.create_task(lookup_fn("Steve"))
            await lookup
            lookup_done.set()
            await hb
            return beats

        beats = asyncio.run(run())
    finally:
        ic.requests = orig_requests

    # 判定方式：**看查询期间心跳跳了几拍**。
    # 不看耗时 —— Windows 定时器分辨率会让 asyncio.sleep(0.02) 实际
    # 睡 ~31ms，耗时断言不稳定（假失败）。
    #
    # 阻塞版（同步 HTTP 在事件循环里）：查询期间心跳 **0 拍**。
    # 修复版（HTTP 在线程池）：查询期间心跳应跳 **多拍**。
    assert len(beats) >= 3, (
        f"查询期间心跳只跳了 {len(beats)} 拍 → 事件循环被同步 HTTP 阻塞"
        "（修复前该值为 0）"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
