# -*- coding: utf-8 -*-
"""echo 系统「管理群消息不广播」在两种链路下的行为测试。

真实回退：早期版本直接访问 qq_connector.id_mapper，OneBot 链路没有该属性
→ AttributeError；改成 getattr 兜底后异常消失了，但**管理群过滤在 OneBot
模式下变成静默不生效**——管理群的消息会被 echo 错误地广播到其他平台。

修复：id_mapper 缺失时（OneBot），source_id 本身就是真实群号，
直接按字符串比对。
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

from gugubot.logic.system.echo import EchoSystem
from gugubot.utils.types import BroadcastInfo, Source

loop = asyncio.new_event_loop()


class FL:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


ADMIN_GID = "123456"


class FakeIdMapper:
    """模拟官方模式的映射：虚拟号/真实号 → openid。"""

    def to_official_group(self, legacy):
        s = str(legacy)
        if s == ADMIN_GID:
            return "openid_admin_group"
        if s == "200000000001":       # 虚拟号
            return "openid_admin_group"
        return s


def _make_echo(with_mapper):
    es = EchoSystem.__new__(EchoSystem)
    es.logger = FL()
    es.enable = True

    class FC(dict):
        def get_keys(self, key, default=None):
            r = self
            for k in key[:-1]:
                r = r.get(k, {})
            return r.get(key[-1], default)

    es.config = FC()
    es.config["connector"] = {"QQ": {"permissions": {
        "admin_group_ids": [ADMIN_GID],
        "group_ids": ["654321"],
    }}}

    class Conn:
        pass

    if with_mapper:
        Conn.id_mapper = FakeIdMapper()
    Conn.enable_send = True
    Conn.enable_receive = True

    BROADCASTS = []

    class CM:
        connectors = []

        def get_connector(self, name):
            return Conn()

        async def broadcast_processed_info(self, info, exclude=None, include=None):
            BROADCASTS.append(info)

    es.system_manager = types.SimpleNamespace(connector_manager=CM())
    es._broadcasts = BROADCASTS
    return es


def _info(source_id):
    return BroadcastInfo(
        event_type="message",
        event_sub_type="group",
        message=[{"type": "text", "data": {"text": "hi"}}],
        raw=None,
        _source=Source("QQ"),
        source_id=source_id,
        sender="测试",
        sender_id="100000000001",
        is_admin=False,
    )


async def _handle_enable_disable(self, info):
    return False


# 统一在模块级替换一次，避免每个用例重复打补丁
_ORIG_HED = EchoSystem.handle_enable_disable
EchoSystem.handle_enable_disable = _handle_enable_disable


def test_onebot_admin_group_not_broadcast():
    """OneBot 链路（无 id_mapper）：管理群消息必须被拦下。"""
    es = _make_echo(with_mapper=False)
    r = loop.run_until_complete(es.process_broadcast_info(_info(ADMIN_GID)))
    assert r is False, f"管理群消息应返回 False（不广播），实际 {r}"
    assert not es._broadcasts, f"不应有任何广播，实际 {es._broadcasts}"
    print("PASS: OneBot 链路管理群消息被拦下 ✅")


def test_onebot_normal_group_still_broadcast():
    """OneBot 链路：普通群的消息不受影响。"""
    es = _make_echo(with_mapper=False)
    r = loop.run_until_complete(es.process_broadcast_info(_info("654321")))
    assert r is True, f"普通群消息应照常处理，实际 {r}"
    print("PASS: OneBot 链路普通群消息照常处理 ✅")


def test_official_virtual_admin_group_not_broadcast():
    """官方链路（有 id_mapper）：虚拟号对应的管理群也要拦下。"""
    es = _make_echo(with_mapper=True)
    r = loop.run_until_complete(es.process_broadcast_info(_info("200000000001")))
    assert r is False, f"虚拟号管理群应被拦下，实际 {r}"
    print("PASS: 官方链路虚拟号管理群被拦下 ✅")


def test_official_real_admin_group_not_broadcast():
    """官方链路：真实群号形式的管理群也要拦下。"""
    es = _make_echo(with_mapper=True)
    r = loop.run_until_complete(es.process_broadcast_info(_info(ADMIN_GID)))
    assert r is False, f"真实群号管理群应被拦下，实际 {r}"
    print("PASS: 官方链路真实群号管理群被拦下 ✅")


def teardown_module(module):
    EchoSystem.handle_enable_disable = _ORIG_HED
