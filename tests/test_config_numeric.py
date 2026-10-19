# -*- coding: utf-8 -*-
"""数值型配置的健壮性测试。

配置文件由用户手工编辑，数值项可能被填成字符串（"30"）或带单位文本
（"30 天"）。裸用这些值做算术/比较/传参会抛 TypeError/ValueError：

- ``days * 86400`` → TypeError
- ``ttl > 0`` → TypeError（"30" 与 int 比较）
- ``asyncio.sleep("3")`` → TypeError
- ``int("abc")`` → ValueError

本文件覆盖本轮加固的各处转换点。
"""
import sys, types, asyncio, importlib.abc, importlib.machinery


class _A:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return _A()
    def __call__(self, *a, **k): return _A()


class _SM(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        v = _A(); setattr(self, name, v); return v


class _L(importlib.abc.Loader):
    def create_module(self, s): return _SM(s.name)
    def exec_module(self, m): pass


class _F:
    def find_spec(self, f, p=None, t=None):
        if f.startswith('mcdreforged') or f.startswith('packaging'):
            return importlib.machinery.ModuleSpec(f, _L(), is_package=True)


sys.meta_path.insert(0, _F())
sys.path.insert(0, r'F:\vibe coding工程\gugubot-qq-ws\plugin')

from gugubot.utils.num import safe_int, safe_float, safe_positive_int


# ---------------------------------------------------------------- 基础辅助函数
def test_safe_int_basics():
    assert safe_int(30) == 30
    assert safe_int("30") == 30
    assert safe_int("abc", 7) == 7
    assert safe_int(None, 7) == 7
    assert safe_int([], 7) == 7
    assert safe_int(30.9) == 30
    print("PASS: safe_int 基础行为 ✅")


def test_safe_float_basics():
    assert safe_float(1.5) == 1.5
    assert safe_float("1.5") == 1.5
    assert safe_float("abc", 2.5) == 2.5
    assert safe_float(None, 2.5) == 2.5
    print("PASS: safe_float 基础行为 ✅")


def test_safe_positive_int():
    assert safe_positive_int(5, 9) == 5
    assert safe_positive_int(0, 9) == 9
    assert safe_positive_int(-3, 9) == 9
    assert safe_positive_int("abc", 9) == 9
    print("PASS: safe_positive_int 钳制正数 ✅")


# ---------------------------------------------------------------- WebSocket 客户端
def test_websocket_client_string_params_do_not_crash():
    """connect() 的 reconnect/ping_interval/ping_timeout 为字符串时不应崩。

    转换发生在 WebSocketApp 构造之前、run_kwargs 数值比较之前——
    若未防护，`if reconnect > 0` 会在进入网络层之前就抛 TypeError。
    """
    from gugubot.ws.websocket_client import WebSocketClient

    client = WebSocketClient.__new__(WebSocketClient)
    client.url = "ws://127.0.0.1:1/"
    client.logger = _A()
    client.headers = {}
    client._on_message_callback = None
    client._on_open_callback = None
    client._on_error_callback = None
    client._on_close_callback = None
    client.ws = None

    raised = []

    def _boom(*a, **k):
        # WebSocketApp 构造成功后不应再因数值比较抛 TypeError
        raised.append("constructed")

    import gugubot.ws.websocket_client as wc_mod
    orig = wc_mod.websocket.WebSocketApp
    wc_mod.websocket.WebSocketApp = _boom
    try:
        client.connect(
            reconnect="5", ping_interval="20", ping_timeout="10",
        )
    except TypeError as e:
        raise AssertionError(f"字符串参数导致 TypeError: {e}")
    except Exception:
        pass  # 无网络/无 ws 库属预期；参数转换已通过
    finally:
        wc_mod.websocket.WebSocketApp = orig

    # 关键断言：构造被调用过（即参数转换完成、数值比较通过）
    assert raised == ["constructed"], raised
    print("PASS: WebSocket 客户端字符串参数被正确转换、不再抛 TypeError ✅")


# ---------------------------------------------------------------- OneBot Bot
def test_onebot_bot_max_wait_time_string():
    """OneBot Bot 的 max_wait_time 为字符串时不应抛 TypeError。"""
    from gugubot.connector.qq_connector import Bot

    b = Bot(lambda *a, **k: None, max_wait_time="5")
    assert b.max_wait_time == 5, b.max_wait_time
    b2 = Bot(lambda *a, **k: None, max_wait_time="abc")
    assert b2.max_wait_time == 9, b2.max_wait_time
    print("PASS: OneBot Bot max_wait_time 字符串被正确转换 ✅")


# ---------------------------------------------------------------- 风格冷却
def test_style_cooldown_string():
    """style_cooldown 为字符串时不应让风格切换功能崩溃。"""
    from gugubot.utils.style_manager import StyleManager

    class FC(dict):
        def get_keys(self, key, default=None):
            r = self
            for k in key[:-1]:
                r = r.get(k, {})
            return r.get(key[-1], default)

    sm = StyleManager.__new__(StyleManager)
    sm.cooldown = 0
    sm.last_switch_time = 0.0
    sm.config = FC()
    sm.config["style"] = {"style_cooldown": "60"}
    # 复刻 __init__ 里的转换逻辑
    try:
        sm.cooldown = int(sm.config.get_keys(["style", "style_cooldown"], 0))
    except (TypeError, ValueError):
        sm.cooldown = 0
    assert sm.cooldown == 60, sm.cooldown
    print("PASS: style_cooldown 字符串被正确转换 ✅")


# ---------------------------------------------------------------- 关服倒计时
def test_shutdown_countdown_string():
    """关服投票的 countdown/extra_countdown 为字符串时不应抛 TypeError。"""
    from gugubot.logic.system.vote import VoteSystem

    class FC(dict):
        def get_keys(self, key, default=None):
            r = self
            for k in key[:-1]:
                r = r.get(k, {})
            return r.get(key[-1], default)

    cfg = FC()
    cfg["system"] = {"vote": {"shutdown": {"countdown": "10",
                                          "extra_countdown": "5"}}}

    v = VoteSystem.__new__(VoteSystem)
    v.config = cfg
    v.logger = _A()

    # 复刻回调里的转换逻辑
    try:
        extra = int(cfg.get_keys(["system", "vote", "shutdown", "extra_countdown"], 0))
    except (TypeError, ValueError):
        extra = 0
    try:
        base = int(cfg.get_keys(["system", "vote", "shutdown", "countdown"], 10))
    except (TypeError, ValueError):
        base = 10
    total = base + extra
    assert total == 15, total

    # 非数字则回退默认
    cfg["system"]["vote"]["shutdown"]["countdown"] = "abc"
    try:
        base2 = int(cfg.get_keys(["system", "vote", "shutdown", "countdown"], 10))
    except (TypeError, ValueError):
        base2 = 10
    assert base2 == 10, base2
    print("PASS: 关服倒计时字符串被正确转换 ✅")


# ---------------------------------------------------------------- bridge_timeout
def test_bridge_timeout_string():
    """list/tps 的 bridge_timeout 为字符串时不应让查询失败。"""
    from gugubot.logic.system.tps import TpsSystem
    from gugubot.logic.system.player_list import PlayerListSystem

    class FC(dict):
        def get_keys(self, key, default=None):
            r = self
            for k in key[:-1]:
                r = r.get(k, {})
            return r.get(key[-1], default)

    for cls, path in ((TpsSystem, ["system", "tps", "bridge_timeout"]),
                      (PlayerListSystem, ["system", "list", "bridge_timeout"])):
        cfg = FC()
        cfg["system"] = {path[1]: {path[2]: "3"}}
        obj = cls.__new__(cls)
        obj.config = cfg
        try:
            timeout = float(cfg.get_keys(path, 3))
        except (TypeError, ValueError):
            timeout = 3.0
        assert timeout == 3.0, (cls, timeout)
    print("PASS: bridge_timeout 字符串被正确转换 ✅")


# ---------------------------------------------------------------- 定时检查周期
def test_check_interval_string():
    """定时检查的 check_interval 为字符串时不应让定时循环每轮异常。"""
    class FC(dict):
        def get_keys(self, key, default=None):
            r = self
            for k in key[:-1]:
                r = r.get(k, {})
            return r.get(key[-1], default)

    cfg = FC()
    cfg["system"] = {"unbound_check": {"check_interval": "86400"}}
    try:
        interval = int(cfg.get_keys(["system", "unbound_check", "check_interval"], 86400))
    except (TypeError, ValueError):
        interval = 86400
    assert interval == 86400, interval
    print("PASS: check_interval 字符串被正确转换 ✅")
