# -*- coding: utf-8 -*-
"""玩家进出播报投递失败必须可见（v2.2.10 续，87）。

问题
----
``logic/plugins/player_notice.py`` 的 ``_broadcast_notice``：

.. code-block:: python

    await connector_manager.broadcast_processed_info(
        processed_info, exclude=exclude_sources
    )
    server.logger.debug(message)          # 成功：仅 debug

``broadcast_processed_info`` 返回 ``{连接器 source: 异常}`` —— 用来判断
「谁没收到」。这里**丢弃返回值**，成功只记 debug，失败**一条日志都没有**。

后果：QQ 掉线 / 桥接断开时，**玩家进出播报一条都没发出去**，而日志里：

* 成功时是 ``debug`` —— 默认日志级别下**根本看不到**；
* 失败时**什么都不记**（`_safe_send` 会打 traceback，但那是"发消息到 X
  失败"，看不出这是玩家进出播报，也看不出是播报这条路的问题）。

用户侧表现：「进出播报坏了？」—— 无法判断是没触发、还是发出去了没送达。

这是 #82 / #83 的同一类（失败信息存在却被丢弃）。播报是本项目的招牌功能
之一，所以补上可见性。

修复
----
接住返回值：有失败时打一条 **warning** 并点名失败的连接器。
"""
import asyncio
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

_MOD = "gugubot.logic.plugins.player_notice"
sys.modules.pop(_MOD, None)
from gugubot.logic.plugins import player_notice  # noqa: E402


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


class _Server:
    def __init__(self):
        self.logger = _Logger()

    def tr(self, key, **kwargs):
        return f"<{key}>"


class _ConnectorManager:
    def __init__(self, failures):
        self._failures = failures
        self.calls = []

    async def broadcast_processed_info(self, processed_info, **kwargs):
        self.calls.append(kwargs)
        return dict(self._failures)


def _run(coro):
    return asyncio.run(coro)


def _call(server, mgr):
    return player_notice._broadcast_notice(
        server,
        mgr,
        "Minecraft",
        ["Minecraft"],
        None,
        "Steve joined the game",
    )


# ==================================================== 1. 失败必须可见
def test_notice_failure_is_visible():
    server = _Server()
    mgr = _ConnectorManager({"QQ": RuntimeError("连接已断")})

    _run(_call(server, mgr))

    assert mgr.calls, "没有真正调用 broadcast_processed_info"
    assert "warning" in server.logger.levels(), (
        "玩家进出播报投递失败却没有任何 warning —— 日志里成功只记 debug、"
        "失败什么都不记，用户无法判断是没触发还是没送达"
    )
    assert "QQ" in server.logger.text(), "警告里没有点名失败的连接器"


# ==================================================== 2. 成功时保持安静
def test_notice_success_does_not_warn():
    """成功时不能打 warning（会淹没真问题）。"""
    server = _Server()
    mgr = _ConnectorManager({})

    _run(_call(server, mgr))

    assert "warning" not in server.logger.levels()
    assert "error" not in server.logger.levels()


# ==================================================== 3. 静态：返回值被使用
def test_notice_result_is_used():
    import ast

    src = (
        _ROOT / "plugin" / "gugubot" / "logic" / "plugins" / "player_notice.py"
    ).read_text(encoding="utf-8-sig")
    tree = ast.parse(src)

    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_broadcast_notice":
            fn = node
            break
    assert fn is not None, "找不到 _broadcast_notice"

    uses_return = any(
        isinstance(node, ast.Assign) and isinstance(node.value, ast.Await)
        for node in ast.walk(fn)
    )
    assert uses_return, (
        "_broadcast_notice 丢弃了 broadcast_processed_info 的返回值 —— "
        "投递失败完全不可见"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
