# -*- coding: utf-8 -*-
"""player_manager.is_admin 管理群成员缓存测试。

真实性能缺陷：is_admin() 每次调用都遍历所有管理群拉一遍成员列表。
MC 侧每条聊天消息（mc_parser / bridge_connector）都可能触发一次 →
N 条消息 × M 个管理群 = 请求放大。成员列表短时间内不会变，
缓存 30s 即可消除放大。

缓存规则：
- 仅在平台支持成员列表时缓存（官方直连恒不可得，缓存无意义）
- 失败结果（空集）同样缓存——避免 WS 断连期间每条消息打一遍 error 刷屏
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

from gugubot.utils.player_manager import PlayerManager

loop = asyncio.new_event_loop()


class FL:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


class FakeServer:
    def get_data_folder(self):
        import tempfile
        return tempfile.gettempdir()

    @property
    def logger(self):
        return FL()


class FakeConfig(dict):
    def get_keys(self, key, default=None):
        r = self
        for k in key[:-1]:
            r = r.get(k, {})
        return r.get(key[-1], default)


CALLS = {"n": 0}


class FakeBot:
    async def get_group_member_list(self, group_id, **kwargs):
        CALLS["n"] += 1
        # 模拟玩家本人就在管理群里 → is_admin 应为 True
        return {"status": "ok", "data": [{"user_id": "100000000001"}]}


class FakeConn:
    bot = FakeBot()


class FakeCM:
    def get_connector(self, source):
        return FakeConn()


class FakeBS:
    config = FakeConfig()
    system_manager = types.SimpleNamespace(connector_manager=FakeCM())


def _make_pm():
    """绕过文件系统初始化，直接挂字段。"""
    pm = PlayerManager.__new__(PlayerManager)
    pm.logger = FL()
    pm.bound_system = FakeBS()
    pm._players = {}
    pm._admin_member_cache = None
    return pm


def _make_player(qq_id):
    from gugubot.utils.player_manager import Player
    p = Player(name="测试玩家")
    p.accounts = {"QQ": [qq_id]}
    return p


def test_cache_hits_avoid_repeat_requests():
    """同一玩家 30s 内多次 is_admin → 只拉一次成员列表。"""
    pm = _make_pm()
    pm._players = {"测试玩家": _make_player("100000000001")}
    CALLS["n"] = 0

    FakeBS.config.clear()
    FakeBS.config["connector"] = {
        "QQ": {
            "source_name": "QQ",
            "permissions": {
                "admin_ids": [],
                "admin_group_ids": ["200000000001"],
            },
        }
    }

    r1 = loop.run_until_complete(pm.is_admin("100000000001"))
    r2 = loop.run_until_complete(pm.is_admin("100000000001"))
    r3 = loop.run_until_complete(pm.is_admin("100000000001"))

    assert r1 is True and r2 is True and r3 is True, (r1, r2, r3)
    assert CALLS["n"] == 1, f"应只拉一次成员列表，实际 {CALLS['n']} 次"
    print("PASS: 缓存命中，3 次检查仅 1 次成员列表请求 ✅")


def test_cache_expires_after_ttl():
    """TTL 过期后重新拉取。"""
    pm = _make_pm()
    pm._players = {"测试玩家": _make_player("100000000001")}
    CALLS["n"] = 0

    loop.run_until_complete(pm.is_admin("100000000001"))
    # 人为把缓存时间戳拨老
    import time as _t
    ts, ids = pm._admin_member_cache
    pm._admin_member_cache = (ts - pm._ADMIN_MEMBER_CACHE_TTL - 1, ids)
    loop.run_until_complete(pm.is_admin("100000000001"))

    assert CALLS["n"] == 2, f"过期后应重新拉取，实际 {CALLS['n']} 次"
    print("PASS: TTL 过期后重新拉取 ✅")


def test_failed_result_is_cached():
    """查询失败（空集）同样缓存——避免断连期间每条消息打一遍 error。"""
    pm = _make_pm()
    pm._players = {"测试玩家": _make_player("100000000001")}

    class DeadBot:
        async def get_group_member_list(self, group_id, **kwargs):
            CALLS["n"] += 1
            raise RuntimeError("WebSocket disconnected")

    FakeConn.bot = DeadBot()
    CALLS["n"] = 0

    r1 = loop.run_until_complete(pm.is_admin("100000000001"))
    r2 = loop.run_until_complete(pm.is_admin("100000000001"))

    assert r1 is False and r2 is False
    assert CALLS["n"] == 1, f"失败结果应缓存，实际请求 {CALLS['n']} 次"
    print("PASS: 失败结果（空集）也缓存，断连期间不刷请求 ✅")


def test_unsupported_platform_not_cached():
    """官方直连（不支持成员列表）不缓存——恢复 OneBot 后立即可用。"""
    pm = _make_pm()
    pm._players = {"测试玩家": _make_player("100000000001")}

    class OfficialBot:
        async def get_group_member_list(self, group_id, **kwargs):
            CALLS["n"] += 1
            return {"status": "ok", "data": None, "unsupported": True}

    FakeConn.bot = OfficialBot()
    CALLS["n"] = 0

    r1 = loop.run_until_complete(pm.is_admin("100000000001"))
    assert r1 is False
    assert pm._admin_member_cache is None, "不支持平台的结果不应缓存"
    print("PASS: 官方直连（unsupported）不缓存 ✅")
