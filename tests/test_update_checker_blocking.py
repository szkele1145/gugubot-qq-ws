# -*- coding: utf-8 -*-
"""版本检查不得阻塞 MCDR 事件循环（v2.2.10 续，75）。

问题
----
``utils/update_checker.py`` 的 ``check_plugin_version`` 返回一个**协程
函数**，其内部直接调用**同步**的 ``requests.get(...)``，而且**没有
timeout 参数**（requests 默认 = 无限等待）：

.. code-block:: python

    def check_plugin_version(server):
        async def check_plugin_version_task():
            response = requests.get(
                "https://api.github.com/repos/.../releases/latest"   # 无 timeout！
            )
            ...

``__init__.py`` 在 ``on_load`` 里 ``server.schedule_task(check_plugin_version(server))``
—— 传的是**协程函数** → MCDR 走 async executor → ``await`` 它
→ **整个函数体跑在 MCDR 事件循环线程上**。

**后果**：

* 一次请求期间 **MCDR 事件循环完全停摆** —— 期间服务器命令、QQ 消息、
  定时任务（不活跃检查、未绑定检查、回复池清理）**全部排队**；
* **没有 timeout** 意味着「连上但服务端不响应」时**永久挂住** →
  事件循环**永久冻结** → 机器人假死、MCDR 控制台命令无响应；
* 国内直连 ``api.github.com`` 常年不可达 → 每次**插件加载**都会卡住
  十几秒（Windows 的 SYN 重试约 21 秒）。

这与 #71（不活跃检查的同步 HTTP）是同一类缺陷：**在 async 函数里调
同步阻塞 IO**。区别是 #71 有 10 秒 timeout 兜底，这里**连 timeout 都
没有**，更糟。

修复
----
1. HTTP 抽成**纯函数** ``_fetch_latest_version()``（只发请求、不碰
   server 对象），丢进线程池执行；
2. 请求**必须带 timeout**（连接 + 读取）；
3. 协程里用 ``run_in_executor`` 拿到结果后再做版本比较与日志输出。

测试手法
--------
1. **运行时**：假 HTTP 睡 0.3 秒 + 心跳协程，断言**检查期间心跳至少
   跳 3 拍**（阻塞版 0 拍）—— 判定用「拍数」而非「耗时」，因为
   Windows 定时器分辨率会让 ``asyncio.sleep(0.02)`` 实际睡约 31ms，
   耗时断言会假失败；
2. **AST 静态**：协程体内不得直接调用 ``requests.get``，且任何
   ``requests.get`` 都必须带 ``timeout`` 关键字。

> 注入方式：只 ``monkeypatch.setattr`` 被测模块上的 ``requests`` 属性，
> **不改 ``sys.modules["requests"]``** —— 后者会污染同进程里的其它测试
> （实测会把 ``test_qq_official_send_resilience.py`` 打挂 5 项）。
"""
import ast
import asyncio
import importlib.abc
import importlib.machinery
import inspect
import pathlib
import sys
import time
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


# ------------------------------------------------ 只为 mcdreforged 造桩
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


class _Finder:
    """只桩 mcdreforged（插件运行时依赖，测试环境里没有）。

    **不桩 requests / packaging** —— 它们是真实依赖，全局替换会污染
    其它测试。
    """

    def find_spec(self, f, p=None, t=None):
        if f == "mcdreforged" or f.startswith("mcdreforged."):
            return importlib.machinery.ModuleSpec(f, _L(), is_package=True)


if not any(isinstance(x, _Finder) for x in sys.meta_path):
    sys.meta_path.insert(0, _Finder())
sys.path.insert(0, str(_ROOT / "plugin"))

from gugubot.utils import update_checker


class _FakeResponse:
    status_code = 200

    def json(self):
        return {"tag_name": "v99.0.0"}


class _FakeRequests:
    """假 requests：``get()`` 阻塞 0.3 秒（模拟慢网络 + 同步 HTTP）。"""

    def __init__(self):
        self.calls = []

    def get(self, url, *a, **kw):
        self.calls.append((url, kw))
        time.sleep(0.3)          # 阻塞 —— 模拟真实同步 HTTP
        return _FakeResponse()


def _make_server():
    logs = []

    class _Logger:
        def info(self, m): logs.append(("info", m))
        def warning(self, m): logs.append(("warning", m))
        def error(self, m): logs.append(("error", m))
        def debug(self, m): logs.append(("debug", m))

    class _Meta:
        version = "1.0.0"

    class _Server:
        logger = _Logger()

        def get_self_metadata(self):
            return _Meta()

    return _Server(), logs


# ==================================================== 1. 关键：不阻塞事件循环
def test_version_check_does_not_block_event_loop(monkeypatch):
    """版本检查期间，事件循环必须还能跑别的协程（心跳 ≥ 3 拍）。

    阻塞实现：``requests.get`` 在协程里同步执行 0.3 秒 → 心跳 **0 拍**。
    """
    fake = _FakeRequests()
    # 只替换被测模块里的名字 —— 不动 sys.modules
    monkeypatch.setattr(update_checker, "requests", fake, raising=True)

    server, _logs = _make_server()
    task_fn = update_checker.check_plugin_version(server)
    assert inspect.iscoroutinefunction(task_fn), (
        "check_plugin_version 应返回协程函数（MCDR 的 schedule_task 会 "
        "await 它）"
    )

    beats = {"n": 0}

    async def heartbeat():
        for _ in range(200):
            await asyncio.sleep(0.02)
            beats["n"] += 1

    async def main():
        hb = asyncio.create_task(heartbeat())
        await task_fn()                     # ← 被测：版本检查
        await asyncio.sleep(0.01)
        hb.cancel()
        try:
            await hb
        except asyncio.CancelledError:
            pass

    asyncio.run(main())

    assert fake.calls, "被测代码没有真正发起请求（桩未被调用）"

    # 0.3 秒阻塞期里，0.02s 一拍的心跳本应跑到 ~15 拍。
    # Windows 定时器分辨率约 15.6ms → 实际每拍约 31ms → 约 9 拍。
    # 取 3 作为下限：阻塞实现是 **0 拍**，非阻塞实现远超 3 拍。
    assert beats["n"] >= 3, (
        f"版本检查期间心跳只跳了 {beats['n']} 拍 —— 同步 requests.get 把 "
        "MCDR 事件循环阻塞了（阻塞实现为 0 拍）。"
        "一次请求期间服务器命令、QQ 消息、所有定时任务全部排队；"
        "而没有 timeout 时还可能永久冻结事件循环。"
    )


# ==================================================== 2. 静态：必须有 timeout
def test_requests_get_has_timeout():
    """任何 ``requests.get(...)`` 都必须带 ``timeout`` 关键字。

    ``requests`` 默认**无超时** —— 服务端「接受连接但永不响应」时
    会永久挂住（线程池线程泄漏 / 事件循环冻结）。
    """
    src_path = _ROOT / "plugin" / "gugubot" / "utils" / "update_checker.py"
    src = src_path.read_text(encoding="utf-8")
    tree = ast.parse(src)

    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "get"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "requests"
    ]
    assert calls, "update_checker.py 里没找到 requests.get 调用"

    for c in calls:
        kw_names = {k.arg for k in c.keywords if k.arg}
        assert "timeout" in kw_names, (
            f"第 {c.lineno} 行的 requests.get 没有 timeout 参数 —— "
            "服务端不响应时会永久挂住，插件加载阶段会冻结 MCDR 事件循环"
        )


# ==================================================== 3. 静态：协程里不直接调
def test_async_body_does_not_call_requests_directly():
    """``async def`` 的函数体内不得**直接**调用 ``requests.*``。

    允许的形式：把 HTTP 放进普通函数（纯函数），协程里用
    ``run_in_executor`` / ``asyncio.to_thread`` 调用它。
    """
    src_path = _ROOT / "plugin" / "gugubot" / "utils" / "update_checker.py"
    src = src_path.read_text(encoding="utf-8")
    tree = ast.parse(src)

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id == "requests"
            ):
                offenders.append((node.name, sub.lineno, sub.func.attr))

    assert not offenders, (
        f"协程里直接调用了同步 HTTP：{offenders} —— 会阻塞 MCDR 事件循环。"
        "应把 HTTP 抽成纯函数并用 run_in_executor / asyncio.to_thread 执行"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
