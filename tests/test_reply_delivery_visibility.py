# -*- coding: utf-8 -*-
"""回复投递失败必须留下明确警告（v2.2.10 续，83）。

背景
----
所有系统回复用户都走 ``BasicSystem.reply()`` / ``reply_to_source()``：

.. code-block:: python

    await self.system_manager.connector_manager.broadcast_processed_info(
        respond, include=[receiver_source]
    )

``broadcast_processed_info`` 的返回值是 ``{连接器 source: 异常}`` ——
**专门用来判断「谁没收到」**。两处都直接把它丢掉，于是：

* 用户执行 ``#help`` / ``#绑定`` 等命令，回复**没送到**（QQ 限频、
  连接器临时故障、回复目标过滤后为空）；
* 日志里只有 ``_safe_send`` 打的一条「发送消息到 X 失败: <traceback>」，
  **看不出这是在回哪条命令**，而且和大量其他日志混在一起很容易漏掉；
* 调用方（各 ``_handle_*``）返回 ``True`` 表示「命令已处理」——
  这本身没错（命令确实被处理了），但**投递失败这件事完全消失了**。

这是 #82 的同一类问题（失败信息存在、却被丢掉），只是没有 #82 那么
严重 —— #82 会**主动输出错误结论**（"已广播"），这里只是**沉默**。

修复
----
两处都接住返回值：有失败时打一条 **warning**，点名失败连接器和
事件类型（群聊/私聊）。不改变返回类型，不触碰任何调用方。

测试
----
用 ``__new__`` 造一个只提供该方法所需属性的 ``BasicSystem``
（避免拉进整套配置/连接器依赖），把 ``broadcast_processed_info`` 换成
返回失败字典的假实现。
"""
import importlib.abc
import importlib.machinery
import pathlib
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


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
sys.path.insert(0, str(_ROOT / "plugin"))

_MOD = "gugubot.logic.system.basic_system"
sys.modules.pop(_MOD, None)
from gugubot.logic.system import basic_system  # noqa: E402


class _Logger:
    def __init__(self):
        self.records = []

    def _add(self, level, msg, *a):
        try:
            text = msg % a if a else str(msg)
        except Exception:
            text = f"{msg} {a}"
        self.records.append((level, text))

    def debug(self, m, *a): self._add("debug", m, *a)
    def info(self, m, *a): self._add("info", m, *a)
    def warning(self, m, *a): self._add("warning", m, *a)
    def error(self, m, *a): self._add("error", m, *a)

    def levels(self):
        return [lvl for lvl, _ in self.records]

    def text(self):
        return "\n".join(t for _, t in self.records)


class _Source:
    def __init__(self, origin="QQ", current="QQ"):
        self.origin = origin
        self.current = current

    def is_from(self, name):
        return self.origin == name

    def to_list(self):
        return [self.origin, self.current]


class _BroadcastInfo:
    def __init__(self, origin="QQ", current="QQ", source_id="Group_1"):
        self.source = _Source(origin, current)
        self.source_id = source_id
        self.receiver_source = current
        self.event_sub_type = "group"
        self.raw = None
        self.server = types.SimpleNamespace(tr=lambda k, **kw: f"<{k}>")
        self.logger = _Logger()
        self.message = [{"type": "text", "data": {"text": "hi"}}]


class _ConnectorManager:
    def __init__(self, failures):
        self._failures = failures
        self.calls = []

    async def broadcast_processed_info(self, processed_info, **kwargs):
        self.calls.append(kwargs)
        return dict(self._failures)


def _make_system(failures):
    """用 __new__ 绕过 __init__，只装 reply() 需要的属性。"""
    system = basic_system.BasicSystem.__new__(basic_system.BasicSystem)
    system.name = "stub"
    system.logger = _Logger()
    cm = _ConnectorManager(failures)
    system.system_manager = types.SimpleNamespace(
        server=types.SimpleNamespace(tr=lambda k, **kw: f"<{k}>"),
        connector_manager=cm,
    )
    system.config = types.SimpleNamespace(
        # 桥接名取默认值 "Bridge"
        get_keys=lambda path, default=None: default,
    )
    return system, cm


def _run(coro):
    import asyncio
    return asyncio.run(coro)


# ==================================================== 1. reply 失败要告警
def test_reply_warns_when_delivery_fails():
    system, cm = _make_system({"QQ": RuntimeError("限频")})
    info = _BroadcastInfo()

    _run(system.reply(info, info.message))

    assert cm.calls, "没有真正调用 broadcast_processed_info"
    assert "warning" in system.logger.levels(), (
        "回复投递失败却没有任何 warning —— 用户执行命令后什么也没收到，"
        "日志里也看不到「这是在回哪条命令」"
    )
    assert "QQ" in system.logger.text(), "警告里没有点名失败的连接器"


def test_reply_silent_when_delivery_ok():
    """正常路径不能被弄出噪音。"""
    system, _ = _make_system({})
    info = _BroadcastInfo()

    _run(system.reply(info, info.message))

    assert "warning" not in system.logger.levels(), (
        "投递全部成功却打了 warning —— 会淹没真正的问题"
    )


# ==================================================== 2. reply_to_source 同样
def test_reply_to_source_warns_when_delivery_fails():
    system, cm = _make_system({"QQ": RuntimeError("连接已断")})
    info = _BroadcastInfo()

    _run(system.reply_to_source(info, info.message))

    assert "warning" in system.logger.levels(), (
        "reply_to_source 投递失败没有 warning"
    )
    assert "QQ" in system.logger.text()


def test_reply_to_source_silent_when_ok():
    system, _ = _make_system({})
    info = _BroadcastInfo()

    _run(system.reply_to_source(info, info.message))
    assert "warning" not in system.logger.levels()


# ==================================================== 3. 静态：返回值被使用
def test_reply_result_is_used():
    """两个回复方法都必须把返回值赋给变量。"""
    import ast

    src = (_ROOT / "plugin" / "gugubot" / "logic" / "system" / "basic_system.py"
           ).read_text(encoding="utf-8")
    tree = ast.parse(src)

    for fn_name in ("reply", "reply_to_source"):
        fn = None
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == fn_name
            ):
                fn = node
                break
        assert fn is not None, f"找不到 {fn_name}"

        uses_return = any(
            isinstance(node, ast.Assign) and isinstance(node.value, ast.Await)
            for node in ast.walk(fn)
        )
        assert uses_return, (
            f"{fn_name} 丢弃了 broadcast_processed_info 的返回值 —— "
            "投递失败会完全沉默"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
