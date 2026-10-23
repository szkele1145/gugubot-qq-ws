# -*- coding: utf-8 -*-
"""广播不能因单个死客户端而中断（v2.2.10 续，79）。

问题
----
``ws/websocket_server.py`` 的 ``broadcast()`` 直接调库的
``self.server.send_message_to_all(message)``：

.. code-block:: python

    self.server.send_message_to_all(message)
    with self._clients_lock:
        count = len(self.clients)
    return count

``websocket-server`` 的 ``send_message_to_all`` 是**逐个客户端写 socket**
的朴素循环，**没有 per-client 异常保护**。只要有一个客户端已经断开
（socket 半开 / 对端进程被杀 / 网络掉线但还没被 TCP 感知），对它的写
就会抛异常 → 异常从循环里**冒泡中断**：

* 排在它**后面**的客户端**完全收不到这条消息**；
* 本方法的 ``except`` 把异常吞掉并 ``return 0`` —— 调用方（
  ``bridge_connector``）据此认为「**一条都没发出去**」，而实际上前面
  几个可能已经收到了。计数与事实相反。

对本插件的影响：MC 聊天要广播给多个游戏服，一个服掉线会让**后面的服
集体收不到聊天**，日志里只有一行「广播消息失败」，返回计数还是 0。

修复
----
改成「取快照 → 逐个发 → 单个失败不影响其余 → 返回真实成功数」：

.. code-block:: python

    for client in self.get_clients():
        try:
            self.server.send_message(client, payload)
            ok += 1
        except Exception:
            continue        # 该客户端已死，跳过，别中断轮询

测试手法
--------
假 server 的 ``send_message`` 对指定的「死客户端」抛异常；断言**第二个
客户端仍然收到消息**，且返回值是**真实成功数**（不是 0，也不是全量）。
"""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_WS_MOD = _ROOT / "plugin" / "gugubot" / "ws" / "websocket_server.py"


def _load_ws_module():
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

    def text(self):
        return "\n".join(t for _, t in self.records)


def _make_server(mod, dead_clients):
    """造一个「向 dead_clients 里的客户端写会抛异常」的假 server。

    ``dead_clients`` 用**列表**（dict 不可哈希，不能放进 set 做成员判断）。
    """
    dead_list = list(dead_clients)

    class _Fake:
        def __init__(self, host=None, port=None, loglevel=None):
            self.delivered = []          # (client, message)

        def set_fn_new_client(self, f): pass
        def set_fn_client_left(self, f): pass
        def set_fn_message_received(self, f): pass

        def run_forever(self):
            import threading
            threading.Event().wait(0.3)

        def shutdown_gracefully(self): pass
        def shutdown_abruptly(self): pass
        def disconnect_client(self, client, status=1000, reason=""): pass

        def send_message(self, client, msg):
            # 用 is 比较（dict 不可哈希，in set 会 TypeError）
            if any(client is d for d in dead_list):
                raise OSError("socket is already closed")
            self.delivered.append((client, msg))

        def send_message_to_all(self, msg):
            """复刻 websocket-server 的朴素实现：无 per-client 保护。"""
            self.to_all_calls = getattr(self, "to_all_calls", 0) + 1
            for client in list(_Fake.registry):
                self.send_message(client, msg)

    _Fake.registry = []
    mod.WebsocketServer = _Fake
    return _Fake


def _setup(mod, clients, dead):
    fake_cls = _make_server(mod, dead)   # 传列表（dict 不可哈希）
    fake_cls.registry = list(clients)
    logger = _FakeLogger()
    wss = mod.WebSocketServer(host="127.0.0.1", port=8797, logger=logger)
    wss.clients = list(clients)          # 直接注入客户端列表
    wss._is_running = True
    wss.server = fake_cls()
    return wss, wss.server, logger


# ==================================================== 1. 死客户端不中断广播
def test_broadcast_survives_dead_client():
    """第一个客户端已死时，第二个**仍然必须收到**广播。"""
    mod = _load_ws_module()
    alive = {"address": ("127.0.0.1", 6001)}
    dead = {"address": ("127.0.0.1", 6002)}
    wss, srv, logger = _setup(mod, [dead, alive], [dead])

    count = wss.broadcast({"type": "chat", "text": "hello"})

    assert any(c is alive for c, _ in srv.delivered), (
        "排在一个死客户端后面的客户端没收到广播 —— "
        "send_message_to_all 的异常把整个循环中断了"
    )
    assert count == 1, (
        f"broadcast() 返回 {count}，实际成功 1 个 —— "
        "旧实现异常时直接 return 0，调用方会误判「一条都没发出去」"
    )


# ==================================================== 2. 全活时计数正确
def test_broadcast_counts_all_alive_clients():
    mod = _load_ws_module()
    c1 = {"address": ("127.0.0.1", 6101)}
    c2 = {"address": ("127.0.0.1", 6102)}
    wss, srv, logger = _setup(mod, [c1, c2], [])

    count = wss.broadcast({"a": 1})
    assert count == 2, f"两个存活客户端，broadcast 却返回 {count}"
    assert len(srv.delivered) == 2


# ==================================================== 3. 全死时返回 0 且不抛
def test_broadcast_all_dead_returns_zero():
    mod = _load_ws_module()
    d1 = {"address": ("127.0.0.1", 6201)}
    d2 = {"address": ("127.0.0.1", 6202)}
    wss, srv, logger = _setup(mod, [d1, d2], [d1, d2])

    count = wss.broadcast({"a": 1})
    assert count == 0
    assert srv.delivered == []


# ==================================================== 4. 不能依赖全局广播 API
def test_broadcast_does_not_use_send_message_to_all():
    """``broadcast()`` 不得再**调用**无 per-client 保护的 ``send_message_to_all``。

    用 AST 查调用点，而不是查源码字符串 —— 注释里提到这个名字（解释
    为什么不用它）是完全合理的。
    """
    import ast

    tree = ast.parse(_WS_MOD.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "send_message_to_all"
        ):
            offenders.append(node.lineno)

    assert not offenders, (
        f"broadcast() 仍在调用 send_message_to_all（第 {offenders} 行）—— "
        "该 API 逐个写 socket 且无 per-client 保护，一个死客户端会中断"
        "整轮广播，让后面的客户端收不到消息，返回值还会是 0"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
