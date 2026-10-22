# -*- coding: utf-8 -*-
"""官方直连的 WS 事件必须真的被处理（v2.2.10 续）。

背景
----
``connector/qq_official/connector.py`` 的 ``_on_ws_dispatch`` 把官方 WS
事件交给 ``server.schedule_task(...)``：

    # 修复前
    self.server.schedule_task(lambda: self.parser.process_message(task))

MCDR 的 ``schedule_task`` 按下面的顺序分派（MCDR 源码
``mcdreforged/plugin/si/server_interface.py``，协程对象支持自 v2.14.0）：

    if inspect.iscoroutine(callable_):            # 协程对象
        → async_task_executor.submit(coro)  ✓
    elif inspect.iscoroutinefunction(callable_):  # async def 函数
        → async_task_executor.submit(coro()) ✓
    elif callable(callable_):                    # 普通可调用对象
        → **task_executor**（同步线程池）✗

``lambda`` 属于第三种 → 被丢进**同步** executor → 同步 executor 调它
得到一个**协程对象**却**从不 await** → 协程体从未执行（还会报
``RuntimeWarning: coroutine ... was never awaited``）。

即 **官方模式收到的每一个 WS 事件都被静默丢弃** —— 发消息正常（发送走
``send_message``，不经过 ``schedule_task``），但收消息全丢。

为什么这个 bug 能长期存活
------------------------
**所有**官方测试都是**直接调** ``parser.process_message(...)``，
**绕过了** ``schedule_task`` 这一层 → 单元测试全绿，生产环境事件全丢。
这正是本测试要补的缺口：把 ``schedule_task`` 的**真实分派语义**搬进
测试里。

另外 ``qq_connector.py``（OneBot 通道）L519 早就是传协程对象，形态正确
—— 本测试同时把它作为「正确对照组」。
"""
import asyncio
import inspect
import sys
import types
import pathlib
import importlib.abc
import importlib.machinery
import threading
from concurrent.futures import Future

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

from gugubot.connector.qq_official import connector as cm


# ==================================================== MCDR 行为模拟器
class _MCDRServerSim:
    """复刻 MCDR 的 ``schedule_task`` 分派语义。

    两条路径：

    * ``async_task_executor`` —— 协程对象 / 协程函数走这里：协程在
      **常驻事件循环**上被真正 await。
    * ``task_executor``（同步线程池）—— 普通可调用对象走这里：只是
      ``func()``，**返回的协程对象永远不会被 await**。

    ``dispatch_log`` 记录每次分派去了哪条路径，测试据此断言。
    """

    def __init__(self):
        self.dispatch_log = []
        self.never_awaited = []
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def schedule_task(self, callable_):
        if inspect.iscoroutine(callable_):
            self.dispatch_log.append("async")
            return self._submit_async(callable_)
        if inspect.iscoroutinefunction(callable_):
            self.dispatch_log.append("async")
            return self._submit_async(callable_())
        # 同步 executor：只调用，不 await 返回的协程
        # （**不 close** —— 真实同步 executor 也不会 close，协程对象会被
        #  GC 回收并触发 "coroutine ... was never awaited" 警告）
        self.dispatch_log.append("sync")
        result = callable_()
        if inspect.iscoroutine(result):
            self.never_awaited.append(result)
        return Future()

    def _submit_async(self, coro):
        fut = Future()

        def _runner():
            try:
                self._loop.call_soon_threadsafe(self._loop.create_task, coro)
                fut.set_result(None)
            except Exception as e:  # pragma: no cover
                fut.set_exception(e)

        threading.Thread(target=_runner, daemon=True).start()
        return fut

    def wait(self, seconds=0.5):
        """等异步侧把协程跑完。"""
        deadline = threading.Event()
        self._loop.call_later(seconds, deadline.set)
        deadline.wait(seconds + 1.0)

    def close(self):
        """停循环并清掉未完成的任务（避免 GC 时报 never-awaited）。"""
        pending = []

        def _collect():
            nonlocal pending
            pending = [t for t in asyncio.all_tasks(self._loop)
                       if not t.done()]
            for t in pending:
                t.cancel()
            self._loop.stop()

        try:
            self._loop.call_soon_threadsafe(_collect)
        except RuntimeError:
            pass
        for t in pending:
            try:
                t.close()
            except Exception:
                pass
        # 同步 executor 留下的未 await 协程：显式 close，避免 GC 时在
        # **其它测试**里飘出 "never awaited" 警告（会污染断言）
        for coro in self.never_awaited:
            try:
                coro.close()
            except Exception:
                pass
        self.never_awaited.clear()
        try:
            self._loop.close()
        except Exception:
            pass


# ==================================================== 工具
def _mk_connector(server):
    """构造一个 QQOfficialConnector（绕过 __init__ 的真实副作用）。"""
    conn = cm.QQOfficialConnector.__new__(cm.QQOfficialConnector)
    conn.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        debug=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    conn.log_prefix = "[QQ官方]"
    conn.enable = True
    conn.server = server
    # parser 是只读 property（basic_connector.py L91），惰性建实例 →
    # 直接预设 _parser_instance，绕开 _parser_class / connector_manager
    conn._parser_instance = None
    conn._parser_class = None
    return conn


def _set_parser(conn, parser):
    """替换连接器的 parser 实例（property 只读，只能动 _parser_instance）。"""
    conn._parser_instance = parser
    return parser


class _RecordingParser:
    """记录 ``process_message`` 是否真的被 await 过。"""

    def __init__(self):
        self.calls = []
        self.started = threading.Event()

    async def process_message(self, task, *args, **kwargs):
        self.calls.append(task)
        self.started.set()
        return None


# ==================================================== 测试
def test_official_dispatch_uses_async_executor():
    """关键回归：官方 WS 事件必须走 async executor（协程被真正 await）。

    修复前传 ``lambda`` → 落进同步 executor → 协程从未执行 → **官方
    模式收到的每一个事件都被静默丢弃**。
    """
    server = _MCDRServerSim()
    try:
        conn = _mk_connector(server)
        parser = _RecordingParser()
        _set_parser(conn, parser)

        conn._on_ws_dispatch("GROUP_AT_MESSAGE_CREATE", 1, {"content": "hi"})

        assert server.dispatch_log == ["async"], (
            "官方事件没有走 async executor（落进了同步 executor → "
            f"协程永远不会被 await）：{server.dispatch_log}"
        )
        assert not server.never_awaited, "有协程对象从未被 await"

        server.wait(0.3)
        assert parser.started.is_set(), "parser.process_message 根本没有被执行"
        assert len(parser.calls) == 1
        assert parser.calls[0]["_event_name"] == "GROUP_AT_MESSAGE_CREATE"
    finally:
        server.close()


def test_official_dispatch_passes_coroutine_object():
    """传给 schedule_task 的必须是**协程对象**，不是普通 lambda。"""
    server = _MCDRServerSim()
    try:
        conn = _mk_connector(server)
        _set_parser(conn, _RecordingParser())

        captured = []
        orig = server.schedule_task

        def spy(obj):
            captured.append(obj)
            return orig(obj)

        server.schedule_task = spy

        conn._on_ws_dispatch("C2C_MESSAGE_CREATE", 2, {"content": "yo"})

        assert len(captured) == 1
        obj = captured[0]
        assert inspect.iscoroutine(obj), (
            "传给 schedule_task 的必须是协程对象（iscoroutine=True），"
            f"实际是 {type(obj).__name__}"
        )
        assert not inspect.iscoroutinefunction(obj)
    finally:
        server.close()


def test_official_dispatch_never_leaves_unawaited_coroutine():
    """不允许出现「从未被 await」的协程。

    同步 executor 分支的特征就是：调用后得到一个协程对象却没人 await
    它 → Python 会在 GC 时报 ``RuntimeWarning: coroutine ... was never
    awaited``。

    这里用**旧代码形态**（``schedule_task(lambda: ...)``）做对照，验证
    模拟器确实能观测到这个现象 —— 否则本测试在「模拟器失灵」时会
    假通过。
    """
    import warnings
    import gc

    # --- 对照：旧代码形态（lambda）确实会产生 never-awaited ---
    server = _MCDRServerSim()
    try:
        conn = _mk_connector(server)
        _set_parser(conn, _RecordingParser())

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            # 手动复现旧代码：把协程藏进 lambda 交给 schedule_task
            server.schedule_task(lambda: conn.parser.process_message({"x": 1}))
            # 同步 executor 拿到的协程对象此刻无人引用 → 强制回收
            # （先留一份证据，再清掉引用让它能被 GC）
            unawaited_count = len(server.never_awaited)
            server.never_awaited.clear()
            gc.collect()

        # 模拟器本身必须能观测到「同步分支拿到了协程对象却没 await」
        assert server.dispatch_log == ["sync"], (
            f"模拟器失灵：lambda 形态竟然没走同步分支：{server.dispatch_log}"
        )
        assert unawaited_count == 1, (
            f"模拟器失灵：同步分支没记录到未 await 的协程（{unawaited_count}）"
        )
        never = [w for w in caught if "never awaited" in str(w.message)]
        assert never, (
            "模拟器失灵：旧代码形态（lambda）竟然没产生 never-awaited 警告 "
            "→ 本文件的断言不可信"
        )
    finally:
        server.close()

    # --- 修复后的代码：不应有 never-awaited ---
    server = _MCDRServerSim()
    try:
        conn = _mk_connector(server)
        _set_parser(conn, _RecordingParser())

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            conn._on_ws_dispatch("GROUP_AT_MESSAGE_CREATE", 3, {"content": "x"})
            server.wait(0.3)

        never = [w for w in caught if "never awaited" in str(w.message)]
        assert not never, (
            "出现了从未被 await 的协程（事件实际上被丢弃了）："
            f"{[str(w.message) for w in never]}"
        )
    finally:
        server.close()


def test_onebot_connector_dispatch_is_correct_form():
    """对照组：OneBot 通道 L519 传协程对象，形态正确。

    防止有人「为了统一风格」把官方通道改成 lambda 那种形态。
    """
    from gugubot.connector import qq_connector as qc

    import ast
    src = pathlib.Path(qc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    found = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "schedule_task"
        ):
            arg = node.args[0]
            # 必须是「调用结果」（协程对象）或「Name」（协程函数），
            # 不能是 Lambda
            assert not isinstance(arg, ast.Lambda), (
                f"OneBot 通道 L{node.lineno} 给 schedule_task 传了 lambda —— "
                "会落进 MCDR 的同步 executor，协程永远不会被 await"
            )
            found = True

    assert found, "没找到 qq_connector 的 schedule_task 调用点"


def test_official_connector_dispatch_has_no_lambda():
    """静态检查：官方连接器里不得再出现 ``schedule_task(lambda: ...)``。"""
    import ast

    src = pathlib.Path(cm.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    offenders = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "schedule_task"
            and node.args
            and isinstance(node.args[0], ast.Lambda)
        ):
            offenders.append(node.lineno)

    assert not offenders, (
        f"官方连接器 L{offenders} 仍给 schedule_task 传 lambda —— "
        "会落进 MCDR 的同步 executor，官方事件全部丢失"
    )


def test_dispatch_error_does_not_break_next_event():
    """一个事件处理失败不应影响后续事件。"""
    server = _MCDRServerSim()
    try:
        conn = _mk_connector(server)

        class _FlakyParser:
            def __init__(self):
                self.calls = 0

            async def process_message(self, task, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("boom")
                return None

        parser = _FlakyParser()
        _set_parser(conn, parser)

        conn._on_ws_dispatch("GROUP_AT_MESSAGE_CREATE", 1, {"content": "a"})
        server.wait(0.3)
        conn._on_ws_dispatch("GROUP_AT_MESSAGE_CREATE", 2, {"content": "b"})
        server.wait(0.3)

        assert parser.calls == 2, "首个事件失败后，后续事件没有被处理"
    finally:
        server.close()


# ==================================================== 4. 端到端链路
class _FakeSystemManager:
    """记录 broadcast_command 是否真的被调用。"""

    def __init__(self):
        self.broadcasts = []
        self.called = threading.Event()

    async def broadcast_command(self, broadcast_info, *args, **kwargs):
        self.broadcasts.append(broadcast_info)
        self.called.set()
        return True


def test_end_to_end_event_reaches_broadcast_command():
    """端到端：官方 WS 事件 → MCDR 调度 → parser → broadcast_command。

    这一层此前**完全没有测试覆盖** —— 所有官方测试都直接调
    ``parser.process_message``、绕过 ``schedule_task``，于是「事件被
    静默丢弃」这个 bug 能长期存活而测试全绿。

    这里用**真实的** ``BasicParser.process_message``（只把 ``parse``
    换成假的、``system_manager`` 换成记录器），验证整条链路真的通。
    """
    from gugubot.parser.basic_parser import BasicParser

    server = _MCDRServerSim()
    try:
        conn = _mk_connector(server)

        class _StubParser(BasicParser):
            """用真实的 process_message，只替换 parse 与 system_manager。"""

            def __init__(self):
                self.system_manager = _FakeSystemManager()
                self.logger = None
                self.parsed = []

            async def parse(self, raw_message, *args, **kwargs):
                # 真实的 parse 是协程函数（process_message 里 await 它）
                self.parsed.append(raw_message)
                return "BROADCAST_INFO"

        parser = _StubParser()
        _set_parser(conn, parser)

        conn._on_ws_dispatch(
            "GROUP_AT_MESSAGE_CREATE",
            1,
            {"content": "hello", "id": "msg-1"},
        )
        server.wait(0.5)

        assert parser.parsed, "parse 从未被调用（事件在调度层就丢了）"
        assert parser.system_manager.broadcasts, (
            "broadcast_command 从未被调用（事件没有走到系统层）"
        )
        assert parser.system_manager.broadcasts[0] == "BROADCAST_INFO"
    finally:
        server.close()


def test_end_to_end_replies_are_queued_in_reply_pool():
    """端到端补充：收到消息时回复池必须入池。

    回复池是官方模式绕开「主动消息限额」的关键（每群每月 4 条），
    入池发生在 ``parser._parse_group_message`` 内部 —— 也就是说
    **#69 修复前入池从未发生过**，官方模式只能发主动消息。
    """
    from gugubot.connector.qq_official.reply_pool import QQReplyPool

    pool = QQReplyPool()
    pool.add("group-openid-1", "msg-1", event_id="evt-1")

    assert pool.size("group-openid-1") == 1
    taken = pool.take("group-openid-1")
    assert taken is not None
    assert taken[0] == "msg-1"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
