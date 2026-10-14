# -*- coding: utf-8 -*-
"""OfficialStatusSystem 单测：官方模式响应 #状态，OneBot 模式跳过。"""
import sys, types, asyncio

class _Any:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return _Any()
    def __call__(self, *a, **k): return _Any()
class _StubModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"): raise AttributeError(name)
        v = _Any(); setattr(self, name, v); return v
import importlib.abc, importlib.machinery
class _Loader(importlib.abc.Loader):
    def create_module(self, spec): return _StubModule(spec.name)
    def exec_module(self, m): pass
class _StubFinder:
    def find_spec(self, f, p=None, t=None):
        if f.startswith("mcdreforged") or f.startswith("packaging"):
            return importlib.machinery.ModuleSpec(f, _Loader(), is_package=True)
sys.meta_path.insert(0, _StubFinder())
sys.path.insert(0, r'F:\vibe coding工程\gugubot-qq-ws\plugin')

from gugubot.logic.system.official_status import OfficialStatusSystem
from gugubot.utils.types import BroadcastInfo, Source

class FL:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass

class FC(dict):
    def get_keys(self, key, default=None):
        r = self
        for k in key[:-1]: r = r.get(k, {})
        return r.get(key[-1], default)

cfg = FC(); cfg["GUGUBot"] = {"command_prefix": "#"}; cfg["connector"] = {}

class FakeWs:
    def is_connected(self): return True
    session_id = "SESS123456"
class FakeTm:
    configured = True
    _expires_at = 9999999999.0
class FakePool:
    _pools = {"G1": 1}
    def size(self, k): return 5
class FakeMapper:
    def known_legacy_groups(self): return {"200000000000"}
    _user_map = {"U1": "100000000000"}
    _openid_group_legacy = {"G1": "1103662998"}
    _pending_legacy_groups = []
class FakeOfficialConnector:
    # duck typing：官方连接器判定依据 id_mapper + ws_client 属性
    ws_client = FakeWs()
    token_manager = FakeTm()
    reply_pool = FakePool()
    id_mapper = FakeMapper()
class FakeCM:
    connectors = [FakeOfficialConnector()]

class FakeSM:
    class _B:
        name = "bound"
    def get_system(self, n): return None

REPLIES = []
class _Self:
    def __init__(self):
        self.logger = FL()
        self.config = cfg
        self.system_manager = types.SimpleNamespace(
            server=None, connector_manager=FakeCM()
        )

async def _reply(self, info, msg):
    REPLIES.append(msg)

system = OfficialStatusSystem.__new__(OfficialStatusSystem)
system.name = "official_status"
system.enable = True
system.logger = FL()
system.config = cfg
system._connector_manager = FakeCM()
system.system_manager = types.SimpleNamespace(server=None, connector_manager=FakeCM())
system._Self = None
import gugubot.logic.system.official_status as os_mod
os_mod.OfficialStatusSystem.reply_to_source = _reply

def mk_info(admin=True):
    return BroadcastInfo(
        event_type="message",
        event_sub_type="group",
        message=[{"type": "text", "data": {"text": "#状态"}}],
        raw=None,
        _source=Source("QQ"),
        source_id="1103662998",
        sender="测试",
        sender_id="100000000000",
        is_admin=admin,
    )

loop = asyncio.new_event_loop()
# 官方模式 + 管理员 → 响应
REPLIES.clear()
dbg_info = mk_info(True)
print("DEBUG is_command:", system.is_command(dbg_info))
print("DEBUG connector:", type(list(FakeCM.connectors)[0]).__name__)
r = loop.run_until_complete(system.process_broadcast_info(dbg_info))
print("DEBUG result:", r, "replies:", len(REPLIES))
assert r is True and REPLIES, (r, REPLIES)
text = REPLIES[0][0]["data"]["text"]
assert "官方直连状态" in text and "已连接" in text and "1103662998" in text, text
print("PASS: #状态 官方模式响应（WS/token/池/绑定全展示）")

# 非管理员 → 拒绝
REPLIES.clear()
r = loop.run_until_complete(system.process_broadcast_info(mk_info(False)))
assert r is True and "仅管理员" in REPLIES[0][0]["data"]["text"], REPLIES
print("PASS: 非管理员拒绝")

# OneBot 模式（无官方连接器）→ 跳过
system._connector_manager = types.SimpleNamespace(connectors=[])
REPLIES.clear()
r = loop.run_until_complete(system.process_broadcast_info(mk_info(True)))
assert r is False and not REPLIES
print("PASS: OneBot 模式自动跳过")

# ===== #我是谁：官方模式响应 =====
system._connector_manager = FakeCM()  # 恢复官方连接器

def mk_whoami():
    return BroadcastInfo(
        event_type="message",
        event_sub_type="group",
        message=[{"type": "text", "data": {"text": "#我是谁"}}],
        raw={"author": {"member_openid": "OPENID_U1", "member_role": "admin"}},
        _source=Source("QQ"),
        source_id="1103662998",
        sender="小明",
        sender_id="100000000000",
        is_admin=True,
    )

REPLIES.clear()
r = loop.run_until_complete(system.process_broadcast_info(mk_whoami()))
assert r is True and REPLIES, (r, REPLIES)
text = REPLIES[0][0]["data"]["text"]
assert "100000000000" in text, text            # 虚拟号
assert "管理员" in text, text                   # 群角色
assert "admin_ids" in text, text                # 管理员设置指引
print("PASS: #我是谁 虚拟号/角色/指引全展示")

# OneBot 模式下 #我是谁 → 跳过
system._connector_manager = types.SimpleNamespace(connectors=[])
REPLIES.clear()
r = loop.run_until_complete(system.process_broadcast_info(mk_whoami()))
assert r is False and not REPLIES
print("PASS: OneBot 模式 #我是谁 自动跳过")

print()
print("OFFICIAL STATUS TEST PASSED")
