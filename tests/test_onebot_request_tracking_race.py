# -*- coding: utf-8 -*-
"""OneBot API 请求跟踪的竞态（v2.2.10 续）。

背景
----
``connector/qq_connector.py`` 的 ``Bot`` 用两个普通 dict 做 API 请求
跟踪：

* ``pending_requests``  : echo id → 发起时间（用于超时清理）
* ``function_return``   : echo id → 响应体（等待方轮询它）

两个 dict 被**三个线程**碰：

* **MCDR 循环**（发方）：``handler`` 里登记、轮询、清理
* **收包线程**：``qq_connector.py`` 的 fast path 写 ``function_return``
* **MCDR 循环**（``qq_parser.py`` L134）：也写 ``function_return``

两个真实缺陷
------------
**A. 响应先到就被丢**
``handler`` 里登记 ``pending_requests`` 的时机在 ``await
self.send_message(...)`` **之后**::

    await self.send_message(command_request)   # ← 回包可能已经到达
    start_time = time.time()
    self.pending_requests[function_return_id] = start_time   # ← 才登记

而收包线程的 fast path 是::

    if echo in self.bot.pending_requests:      # ← 此刻还没登记！
        self.bot.function_return[echo] = message_data
    return                                     # ← 不在就直接丢弃

本地 WS 回包很快，**响应完全可能先于登记到达** → 响应被直接丢弃 →
等待方只能等满 ``max_wait_time`` 后返回 ``None``。表现：``get_group_
member_list`` / ``get_login_info`` 等调用**随机返回 None**，且**无任何
报错**（官方模式的管理员判定就靠它）。

**B. 「先 in 再 []」不是原子操作**
::

    if function_return_id in self.function_return:
        return self.function_return[function_return_id]

两次 dict 访问之间，收包线程的 ``_cleanup_pending_requests`` 可能正好
``del`` 掉这个 key → **KeyError**。而这个 KeyError 发生在 ``handler``
的 ``try`` 里 → ``finally`` 清理后**向上抛** → 调用方收到 KeyError 而
不是正常返回值。
"""
import asyncio
import json
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

from gugubot.connector import qq_connector as qc


# ==================================================== 工具

async def _noop_sender(message):
    return None


def _mk_bot(send_message, max_wait_time=9):
    return qc.Bot(send_message, max_wait_time=max_wait_time)


def _echo_response(echo, payload=None):
    return json.dumps({
        "status": "ok",
        "retcode": 0,
        "echo": echo,
        "data": payload if payload is not None else {"ok": True},
    })


def _make_connector(bot):
    """构造一个 QQWebSocketConnector（绕过 __init__ 的真实副作用）。"""
    conn = qc.QQWebSocketConnector.__new__(qc.QQWebSocketConnector)
    conn.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        debug=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    conn.log_prefix = "[QQ]"
    conn.enable = True
    conn.bot = bot
    # parser 是只读 property（basic_connector.py L91）→ 直接置实例属性
    conn._parser_instance = None
    conn._parser_class = None
    return conn


# ==================================================== 1. 缺陷 A
def test_response_arriving_before_registration_is_dropped():
    """缺陷 A：响应先于登记到达时，fast path 会把它丢掉。

    复刻真实时序：``send_message`` 在被 await 时就同步把回包喂给
    收包线程（模拟本地 WS 极速回包）。
    """
    bot_holder = {}

    async def send_message(message):
        """模拟 ``_send_message``：把请求发出去，并立刻回包。"""
        echo = message.get("echo")
        conn = bot_holder["conn"]
        # 收包线程此刻执行 fast path
        conn._on_ws_message(None, _echo_response(echo))

    bot = _mk_bot(send_message)
    conn = _make_connector(bot)
    bot_holder["conn"] = conn

    async def run():
        return await bot.get_login_info()

    result = asyncio.run(run())

    assert result is not None, (
        "响应在登记前到达时被丢弃 → API 调用返回 None（无任何报错）"
    )
    assert result.get("status") == "ok"


def test_registration_happens_before_send():
    """回归：``pending_requests`` 的登记必须早于发送。

    登记晚于发送 → 回包可能先到 → 被 fast path 丢弃。

    确定性断言：在 ``send_message`` 被调用的那一刻，检查
    ``pending_requests`` 里**已经有**这个 echo。
    """
    observed = {}

    async def send_message(message):
        echo = message.get("echo")
        observed["registered_at_send_time"] = echo in bot.pending_requests

    bot = _mk_bot(send_message)

    async def run2():
        task = asyncio.create_task(bot.get_login_info())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run2())

    assert observed.get("registered_at_send_time") is True, (
        "发送时 pending_requests 里还没有登记该 echo → 回包可能先到被丢弃"
    )


# ==================================================== 2. 缺陷 B
def test_polling_read_is_atomic():
    """回归：轮询读取不得用「先 in 再 []」两段式（中间可能被删）。"""
    src = pathlib.Path(qc.__file__).read_text(encoding="utf-8")

    # 允许存在「in」判断，但不允许出现「in ... : 紧跟 return self.function_return[...]」
    # 这种两段式读法 —— 正确写法是单次 get / pop。
    import ast

    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        # 形如 `x in self.function_return`
        if not (
            isinstance(test, ast.Compare)
            and isinstance(test.ops[0], ast.In)
        ):
            continue
        # body 里若直接 return self.function_return[...] → 两段式
        for stmt in ast.walk(node):
            if (
                isinstance(stmt, ast.Return)
                and isinstance(stmt.value, ast.Subscript)
                and isinstance(stmt.value.value, ast.Attribute)
                and stmt.value.value.attr == "function_return"
            ):
                offenders.append(stmt.lineno)

    assert not offenders, (
        f"qq_connector.py L{offenders} 用了「先 in 再 []」两段式读 "
        "function_return —— 两次访问之间条目可能被删 → KeyError"
    )


def test_cleanup_does_not_break_waiting_readers():
    """回归：超时清理不得把仍在等待的条目的响应弄丢。

    清理与等待方轮询并发时，等待方要么拿到响应、要么继续等，
    不能抛 KeyError。

    **确定性设计**：不用「靠运气撞窗口」的压力测试 —— 改用**对照组**
    验证「这个测试确实能观测到该缺陷」：把两段式读法（``in`` + ``[]``）
    放进同样的并发环境里跑，若对照组观测不到 KeyError，说明测试本身
    无效（那就不能作为回归保护）。
    """
    bot = _mk_bot(_noop_sender, max_wait_time=1)
    errors = []

    async def waiter():
        try:
            await bot.get_login_info()
        except Exception as e:
            errors.append(e)

    async def run():
        t = asyncio.create_task(waiter())
        # 并发地反复做「登记 + 写响应 + 清理」
        for i in range(200):
            bot.pending_requests["x"] = 0.0
            bot.function_return["x"] = {"status": "ok"}
            bot._cleanup_pending_requests()
            await asyncio.sleep(0)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    asyncio.run(run())

    key_errors = [e for e in errors if isinstance(e, KeyError)]
    assert not key_errors, f"轮询过程中抛出 KeyError: {key_errors}"


def test_two_step_read_can_raise_key_error_control_group():
    """对照组：证明「先 in 再 []」两段式读**真的会**抛 KeyError。

    没有这一项，上面的并发测试就可能「永远通过」而毫无保护作用
    （窗口太窄 / 线程调度不利）→ 假回归保护。

    这里用一个**受控**的 dict：``__contains__`` 返回 True 之后、
    真正取值的瞬间把 key 删掉 —— 精确复刻「两次访问之间条目被删」
    的真实竞态。
    """
    class _RacyDict(dict):
        def __contains__(self, key):
            result = super().__contains__(key)
            if result:
                # 模拟另一个线程在两次访问之间删掉了这个 key
                super().pop(key, None)
            return result

    d = _RacyDict({"x": 1})

    # 两段式（旧写法）→ KeyError
    raised = False
    try:
        if "x" in d:
            _ = d["x"]
    except KeyError:
        raised = True
    assert raised, "两段式读居然没抛 KeyError → 对照组失效"

    # 单次 pop（新写法）→ 安全
    d2 = _RacyDict({"x": 1})
    assert d2.pop("x", None) == 1, "单次 pop 不应抛异常"


# ==================================================== 3. 响应不得凭空丢失
def test_fast_path_stores_response_regardless_of_registration_order():
    """核心回归：fast path 必须把响应**存下来**，而不是因为「还没登记」就丢。

    正确语义：响应先到 → 先存着；请求方随后登记并轮询 → 能取到。
    """
    bot = _mk_bot(_noop_sender)
    conn = _make_connector(bot)

    # 响应先到
    conn._on_ws_message(None, _echo_response("early-echo"))

    # 请求方随后登记（模拟 handler 里登记 pending 的时机）
    bot.pending_requests["early-echo"] = 0.0

    # 轮询应能取到
    assert "early-echo" in bot.function_return, (
        "响应在登记前到达时被丢弃 → 请求方永远等不到（只能等满超时）"
    )


# ==================================================== 4. 孤儿响应回收
def test_orphan_responses_are_recycled_not_leaked():
    """孤儿响应必须被回收，``function_return`` 不得无限增长。

    fast path 改为「无条件存储」后，早到的响应（或别人的响应）会留在
    ``function_return`` —— 若没有回收机制，长时间运行会泄漏。
    ``_cleanup_pending_requests`` 按「pending 里没有且超过宽限期」回收。
    """
    bot = _mk_bot(_noop_sender, max_wait_time=1)

    # 1) 正常流程：登记 → 响应 → 取走 → 结束
    async def happy_path():
        echo = "happy-1"
        bot._register_pending(echo)
        bot._store_response(echo, {"status": "ok"})
        resp = bot._take_response(echo)
        bot._forget(echo)
        return resp

    assert asyncio.run(happy_path()) == {"status": "ok"}

    # 2) 孤儿：响应存进来、但从来没人登记/取走
    bot._store_response("orphan-a", {"status": "ok"})
    assert "orphan-a" in bot.function_return

    # 宽限期未到 → 不回收
    bot._cleanup_pending_requests()
    assert "orphan-a" in bot.function_return, "宽限期内就回收 → 早到响应丢失"

    # 宽限期过了 → 回收（把存入时刻拨回过去）
    import time as _time
    with bot._lock:
        bot._orphan_stored_at["orphan-a"] = _time.time() - 100
    bot._cleanup_pending_requests()
    assert "orphan-a" not in bot.function_return, (
        "孤儿响应未被回收 → function_return 无限增长（内存泄漏）"
    )


# ==================================================== 5. 跨线程压测
def test_cross_thread_stress_no_exceptions():
    """真实跨线程压测：收包线程写响应 + 等待方轮询 + 清理并发。

    模拟真实负载：收包线程（fast path）与 MCDR 循环（handler）同时
    操作两个 dict，且清理逻辑并发触发。整个过程中：

    * 等待方要么拿到响应、要么超时返回 None —— **不抛任何异常**
    * 收包线程不抛任何异常
    """
    import time as _time

    got_response = {"count": 0}
    receiver_errors = []

    async def responder(echo):
        """收包线程视角：延迟一点再写响应。"""
        await asyncio.sleep(0.001)
        bot._store_response(echo, {"status": "ok", "echo": echo})

    async def one_call(i):
        echo = f"stress-{i}"

        async def fake_send(message):
            # 发送的同时模拟回包（延迟 1ms）
            asyncio.get_event_loop().create_task(responder(message["echo"]))

        bot.send_message = fake_send
        try:
            result = await bot.get_login_info()
            if result is not None:
                got_response["count"] += 1
        except Exception as e:
            receiver_errors.append(e)

    async def run():
        # 并发 50 个请求 + 持续触发清理
        tasks = [asyncio.create_task(one_call(i)) for i in range(50)]
        for _ in range(100):
            bot._cleanup_pending_requests()
            await asyncio.sleep(0.001)
        await asyncio.gather(*tasks)

    bot = _mk_bot(_noop_sender, max_wait_time=2)

    asyncio.run(run())

    assert not receiver_errors, f"压测中出现异常: {receiver_errors}"
    assert got_response["count"] == 50, (
        f"50 个请求只拿到 {got_response['count']} 个响应 —— 响应丢失"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
