# -*- coding: utf-8 -*-
"""WS 服务器启动失败必须如实报错（v2.2.10 续，78）。

问题
----
``ws/websocket_server.py`` 的 ``start()`` 里顺序是：

.. code-block:: python

    self.server_thread = threading.Thread(target=self._run_server, ...)
    self.server_thread.start()
    self._is_running = True          # ← 在 start() 之后

而 ``_run_server`` 的 ``finally`` 会把 ``_is_running`` 置回 ``False``：

.. code-block:: python

    def _run_server(self):
        try:
            self.server.run_forever()
        except Exception as e:
            self.logger.error(...)       # 只记日志，不冒泡
        finally:
            self._is_running = False

**竞态**：``websocket-server`` 的 socket 是在 ``run_forever()`` 里才
绑定的。端口已被占用时 ``run_forever()`` **立刻抛异常** → 子线程走
``finally`` → ``_is_running = False`` → 然后主线程才执行
``self._is_running = True`` → **状态被改回 True**。

后果（不是"日志误导"这么轻）：

* ``start()`` 打印「WebSocket服务器已启动在 host:port」，**实际没有监听**；
* ``is_running()`` 返回 ``True`` → ``bridge_connector`` 据此认为服务器
  已就绪（L463 / L480 都判它），**不会重试启动**，也不会提示用户；
* ``send_message()`` 也过 ``if not self._is_running`` 这道闸 → 一路走到
  ``self.server.send_message(...)``，对着一个没起来的 server 发消息；
* 用户侧表现：mod／桥接端**一直连不上**，插件日志却写着「已启动」。

端口占用恰恰是这个插件最常见的启动失败原因（源码注释里就提到「插件
reload 时报『端口已被占用』」）。

修复
----
1. ``_is_running = True`` **先于** ``thread.start()`` 设置 —— 消除竞态：
   无论线程多快退出，最终值都由 ``_run_server`` 的 ``finally`` 决定；
2. ``_run_server`` 把启动期异常记进 ``self._start_error``，并区分
   「主动停止」与「意外退出」（``_stop_requested``）；
3. ``start()`` 起线程后短暂 ``join``：线程已死 = 启动失败 → **抛
   RuntimeError**，把真实原因（端口占用 / 权限不足）带给调用方，
   不让它打印虚假的成功日志。
"""
import ast
import importlib.util
import pathlib
import sys
import threading
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_WS_MOD = _ROOT / "plugin" / "gugubot" / "ws" / "websocket_server.py"


def _load_ws_module():
    """按路径加载 websocket_server（它只依赖标准库 + 可选第三方库）。"""
    spec = importlib.util.spec_from_file_location("_ws_under_test", _WS_MOD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_ws_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeLogger:
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
    def exception(self, m, *a): self._add("exception", m, *a)

    def text(self):
        return "\n".join(t for _, t in self.records)


def _install_fake_server(mod, behaviour: str):
    """把模块里的 WebsocketServer 换成假实现。

    behaviour:
      * ``"fail"``   → ``run_forever`` 立刻抛 OSError（端口占用）
      * ``"block"``  → ``run_forever`` 阻塞到 ``shutdown_*`` 被调用
    """
    state = {"shutdown": threading.Event()}

    class _Fake:
        def __init__(self, host=None, port=None, loglevel=None):
            self.host = host
            self.port = port
            self.sent = []

        def set_fn_new_client(self, f): pass
        def set_fn_client_left(self, f): pass
        def set_fn_message_received(self, f): pass

        def run_forever(self):
            if behaviour == "fail":
                raise OSError("[WinError 10048] 通常每个套接字地址只允许使用一次")
            state["shutdown"].wait(10)

        def shutdown_gracefully(self):
            state["shutdown"].set()

        def shutdown_abruptly(self):
            state["shutdown"].set()

        def send_message(self, client, msg):
            self.sent.append((client, msg))

        def disconnect_client(self, client, status=1000, reason=""):
            pass

    mod.WebsocketServer = _Fake
    return state


# ==================================================== 1. 端口占用必须抛错
def test_start_raises_when_server_dies_immediately():
    """``run_forever`` 立刻失败（端口占用）时，``start()`` 必须抛错。

    注意模块里有两个**名字极易混淆**的类：

    * ``WebsocketServer`` —— 第三方库的类（本测试替换成假实现）
    * ``WebSocketServer`` —— 本文件要测的外层封装类
    """
    mod = _load_ws_module()
    _install_fake_server(mod, "fail")     # 替换第三方库类
    logger = _FakeLogger()

    inst = mod.WebSocketServer(host="127.0.0.1", port=8799, logger=logger)

    with pytest.raises(RuntimeError) as ei:
        inst.start()

    msg = str(ei.value)
    assert "启动" in msg or "端口" in msg, f"异常信息没有说明原因: {msg}"
    assert inst.is_running() is False, (
        "启动失败后 is_running() 仍为 True —— 竞态：_run_server 的 finally "
        "已把标志置回 False，主线程随后又置 True。"
        "bridge_connector 会因此认为服务器已就绪、不再重试启动"
    )
    # 不能打印虚假的成功日志
    assert "已启动" not in logger.text(), (
        f"启动失败却打印了成功日志:\n{logger.text()}"
    )


# ==================================================== 2. 正常启动仍可用
def test_start_success_sets_running_and_stop_clears():
    """正常路径不能被破坏：启动后 ``is_running()`` 为 True，停止后为 False。"""
    mod = _load_ws_module()
    _install_fake_server(mod, "block")
    logger = _FakeLogger()

    inst = mod.WebSocketServer(host="127.0.0.1", port=8798, logger=logger)
    inst.start()
    try:
        assert inst.is_running() is True
    finally:
        inst.stop(timeout=3)

    assert inst.is_running() is False, "stop() 之后 is_running() 仍为 True"


# ==================================================== 3. 静态：置位先于 start
def test_running_flag_set_before_thread_start():
    """``_is_running = True`` 必须在 ``server_thread.start()`` **之前**。

    这是一条结构性断言：放在之后就会被 ``_run_server`` 的 ``finally``
    抢先置回 False，形成「线程已死、标志却是 True」的假状态。
    """
    src = _WS_MOD.read_text(encoding="utf-8")
    tree = ast.parse(src)
    start_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "start":
            start_fn = node
            break
    assert start_fn is not None, "找不到 start()"

    set_line = None
    thread_start_line = None
    for node in ast.walk(start_fn):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Attribute) and t.attr == "_is_running"
                for t in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and node.value.value is True
        ):
            set_line = node.lineno
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "start"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "server_thread"
        ):
            thread_start_line = node.lineno

    assert set_line is not None, "start() 里没有把 _is_running 置为 True"
    assert thread_start_line is not None, "start() 里没有 server_thread.start()"
    assert set_line < thread_start_line, (
        f"_is_running = True 在第 {set_line} 行、thread.start() 在第 "
        f"{thread_start_line} 行 —— 置位晚于起线程。若线程立刻失败"
        "（端口占用），finally 会把标志置回 False，主线程随后又置 True，"
        "形成「线程已死、状态却说在跑」的假象"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
