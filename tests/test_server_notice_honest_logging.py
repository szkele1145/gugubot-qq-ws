# -*- coding: utf-8 -*-
"""服务器通知广播失败时不得谎报「已广播」（v2.2.10 续，82）。

问题
----
``logic/plugins/server_notice.py`` 的 ``broadcast_server_start`` /
``broadcast_server_stop``：

.. code-block:: python

    await connector_manager.broadcast_processed_info(
        processed_info, exclude=exclude_sources
    )

    server.logger.info("[服务器通知] 已广播启动消息")

``ConnectorManager.broadcast_processed_info`` 的返回值就是
``{连接器 source: 异常}``，**专门给调用方判断「谁没收到」用的**。这里却
直接丢弃，然后**无条件**打一条成功日志。

后果：QQ 掉线 / 桥接断开时，服务器启动/停止通知**一条都没发出去**，
日志里却写着「已广播启动消息」。用户看着日志以为通知成功了，实际群里
什么都没收到 —— 而这是本项目最显眼的功能之一（开服/关服播报）。

``_safe_send`` 内部确实会打 traceback，所以并非"完全静默"；问题在于
**成功日志与事实相反**，这会直接误导排查方向。

修复
----
接住返回值：
* 没有失败 → 保持原来的成功日志；
* 有失败 → 打 **warning** 并点名失败的连接器，**不再**声称已广播。

同一类「假成功」已经在 #78（WS 启动）修过，这里属于同一家族。
"""
import ast
import importlib.abc
import importlib.machinery
import pathlib
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_NOTICE_MOD = (
    _ROOT / "plugin" / "gugubot" / "logic" / "plugins" / "server_notice.py"
)


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

_NOTICE_MOD_NAME = "gugubot.logic.plugins.server_notice"
sys.modules.pop(_NOTICE_MOD_NAME, None)
from gugubot.logic.plugins import server_notice  # noqa: E402


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


class _Config:
    def get_keys(self, path, default=None):
        # 开关全部打开；其余按默认值
        if path and "source_name" in path[-1:]:
            return default
        return True if default is None else default


class _ConnectorManager:
    def __init__(self, failures):
        self._failures = failures
        self.calls = []

    async def broadcast_processed_info(self, processed_info, **kwargs):
        self.calls.append(kwargs)
        return dict(self._failures)


def _run(coro):
    import asyncio
    return asyncio.run(coro)


# ==================================================== 1. 有失败时不得报成功
def test_start_notice_does_not_claim_success_on_failure():
    server = _Server()
    mgr = _ConnectorManager({"QQ": RuntimeError("连接已断")})

    _run(server_notice.broadcast_server_start(server, mgr, _Config()))

    assert mgr.calls, "没有真正调用 broadcast_processed_info"
    assert "已广播启动消息" not in server.logger.text(), (
        "连接器发送失败，日志却写着「已广播启动消息」—— "
        "用户看着这条日志会以为通知送达了"
    )
    assert "warning" in server.logger.levels(), (
        "发送失败却没有 warning 日志"
    )
    assert "QQ" in server.logger.text(), "失败信息没有点名是哪个连接器"


def test_stop_notice_does_not_claim_success_on_failure():
    server = _Server()
    mgr = _ConnectorManager({"QQ": RuntimeError("连接已断")})

    _run(server_notice.broadcast_server_stop(server, mgr, _Config()))

    assert "已广播停止消息" not in server.logger.text(), (
        "连接器发送失败，日志却写着「已广播停止消息」"
    )
    assert "warning" in server.logger.levels()


# ==================================================== 2. 全部成功时保持原日志
def test_start_notice_logs_success_when_no_failure():
    server = _Server()
    mgr = _ConnectorManager({})

    _run(server_notice.broadcast_server_start(server, mgr, _Config()))

    assert "已广播启动消息" in server.logger.text(), (
        "全部成功时反而没有成功日志 —— 加固不能把正常路径弄坏"
    )
    assert "warning" not in server.logger.levels()


def test_stop_notice_logs_success_when_no_failure():
    server = _Server()
    mgr = _ConnectorManager({})

    _run(server_notice.broadcast_server_stop(server, mgr, _Config()))
    assert "已广播停止消息" in server.logger.text()


# ==================================================== 3. 静态：必须接住返回值
def test_broadcast_result_is_used():
    """两处广播都必须把返回值**赋给变量**（否则无从判断成败）。"""
    src = _NOTICE_MOD.read_text(encoding="utf-8")
    tree = ast.parse(src)

    for fn_name in ("broadcast_server_start", "broadcast_server_stop"):
        fn = None
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == fn_name
            ):
                fn = node
                break
        assert fn is not None, f"找不到 {fn_name}"

        uses_return = False
        for node in ast.walk(fn):
            # 形如 `failures = await connector_manager.broadcast_processed_info(...)`
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Await):
                uses_return = True
                break
        assert uses_return, (
            f"{fn_name} 丢弃了 broadcast_processed_info 的返回值 —— "
            "无法判断「谁没收到」，也就只能无条件报成功"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
