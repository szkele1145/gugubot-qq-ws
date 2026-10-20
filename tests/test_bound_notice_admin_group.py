# -*- coding: utf-8 -*-
"""bound_notice「管理群不发未绑定提醒」在两种链路下的行为测试。

真实回退：早期直接访问 qq_connector.id_mapper，OneBot 链路没有该属性
→ AttributeError；改成 getattr 兜底后异常消失，但**管理群过滤整个被
跳过**——管理群里的未绑定玩家会收到「请先绑定」提醒，而设计意图是
管理群不发这类提醒。

与 tests/test_echo_admin_group.py 是同一类缺陷的第二个实例。
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

from gugubot.logic.system.bound_notice import BoundNoticeSystem
from gugubot.utils.types import BroadcastInfo, Source

loop = asyncio.new_event_loop()


class FL:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


ADMIN_GID = "123456"
SENT = []


class FakeIdMapper:
    def to_official_group(self, legacy):
        s = str(legacy)
        if s in (ADMIN_GID, "200000000001"):
            return "openid_admin_group"
        return s


def _make_notice(with_mapper):
    ns = BoundNoticeSystem.__new__(BoundNoticeSystem)
    ns.logger = FL()
    ns.enable = True
    ns.name = "bound_notice"   # get_tr() 拼 key 时用

    class FC(dict):
        def get_keys(self, key, default=None):
            r = self
            for k in key[:-1]:
                r = r.get(k, {})
            return r.get(key[-1], default)

    ns.config = FC()
    ns.config["connector"] = {"QQ": {
        "source_name": "QQ",
        "permissions": {"admin_group_ids": [ADMIN_GID], "group_ids": ["654321"]},
    }}

    class Conn:
        pass

    if with_mapper:
        Conn.id_mapper = FakeIdMapper()

    class CM:
        def get_connector(self, name):
            return Conn()

        async def broadcast_processed_info(self, info, exclude=None, include=None):
            SENT.append(info)

    class FakeServer:
        def tr(self, *a, **k):
            return "GuguBot"

    # bound_system：只提供 player_manager.get_player（返回 None → 未绑定）
    class PM:
        def get_player(self, *a, **k):
            return None

    class FakeBS:
        player_manager = PM()

        def get_tr(self, key, *a, **k):
            return "绑定系统"

    ns.bound_system = FakeBS()
    ns.system_manager = types.SimpleNamespace(
        connector_manager=CM(), server=FakeServer()
    )
    return ns


def _info(source_id, sender_id="100000000001"):
    return BroadcastInfo(
        event_type="message",
        event_sub_type="group",
        message=[{"type": "text", "data": {"text": "hi"}}],
        raw=None,
        _source=Source("QQ"),
        source_id=source_id,
        sender="测试",
        sender_id=sender_id,
        is_admin=False,
    )


def test_onebot_admin_group_no_notice():
    """OneBot 链路：管理群里的未绑定玩家不该收到提醒。"""
    SENT.clear()
    ns = _make_notice(with_mapper=False)
    r = loop.run_until_complete(ns._check_and_notify(_info(ADMIN_GID)))
    assert r is False, r
    assert not SENT, f"管理群不应发出提醒，实际 {SENT}"
    print("PASS: OneBot 链路管理群不发未绑定提醒 ✅")


def test_onebot_normal_group_sends_notice():
    """OneBot 链路：普通群里的未绑定玩家应收到提醒。"""
    SENT.clear()
    ns = _make_notice(with_mapper=False)
    r = loop.run_until_complete(ns._check_and_notify(_info("654321")))
    assert r is False, r  # 不拦截消息
    assert SENT, "普通群应发出未绑定提醒"
    print("PASS: OneBot 链路普通群正常提醒 ✅")


def test_official_virtual_admin_group_no_notice():
    """官方链路：虚拟号形式的管理群也不该收到提醒。"""
    SENT.clear()
    ns = _make_notice(with_mapper=True)
    r = loop.run_until_complete(ns._check_and_notify(_info("200000000001")))
    assert r is False, r
    assert not SENT, f"管理群不应发出提醒，实际 {SENT}"
    print("PASS: 官方链路虚拟号管理群不发提醒 ✅")


def test_official_real_admin_group_no_notice():
    """官方链路：真实群号形式的管理群也不该收到提醒。"""
    SENT.clear()
    ns = _make_notice(with_mapper=True)
    r = loop.run_until_complete(ns._check_and_notify(_info(ADMIN_GID)))
    assert r is False, r
    assert not SENT, f"管理群不应发出提醒，实际 {SENT}"
    print("PASS: 官方链路真实群号管理群不发提醒 ✅")
