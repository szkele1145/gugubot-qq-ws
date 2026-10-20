# -*- coding: utf-8 -*-
"""ConfigCheckSystem 单测：#配置检验 / #config check 语法+语义校验。"""
import sys
import pathlib, types, asyncio

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
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'plugin'))

from gugubot.logic.system.config_check import ConfigCheckSystem
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

# 模拟 config（yaml_format True，path 不存在即走默认）
cfg = FC(); cfg["GUGUBot"] = {"command_prefix": "#"}
cfg.path = None  # 模拟未写盘的默认配置
cfg["connector"] = {}
cfg["connector"]["QQ"] = {}
cfg["connector"]["QQ"]["permissions"] = {"admin_ids": [], "group_ids": []}
cfg["connector"]["QQ"]["connection"] = {"type": "onebot"}

REPLIES = []
class _Self:
    def __init__(self):
        self.logger = FL()
        self.config = cfg
        self.system_manager = types.SimpleNamespace(server=None, connector_manager=None)

system = ConfigCheckSystem.__new__(ConfigCheckSystem)
system.name = "config_check"
system.enable = True
system.logger = FL()
system.config = cfg
system._connector_manager = None
import gugubot.logic.system.config_check as cc_mod
async def _reply(self, info, msg):
    REPLIES.append(msg)
cc_mod.ConfigCheckSystem.reply_to_source = _reply

def mk_info(admin=True, text="#配置检验"):
    return BroadcastInfo(
        event_type="message",
        event_sub_type="group",
        message=[{"type": "text", "data": {"text": text}}],
        raw=None,
        _source=Source("QQ"),
        source_id="1103662998",
        sender="测试",
        sender_id="100000000000",
        is_admin=admin,
    )

loop = asyncio.new_event_loop()

class FakeWs:
    def is_connected(self): return True
class FakeConnector:
    ws_client = FakeWs()
    id_mapper = _Any()

# ---- 1) 管理员 #配置检验（onebot 语义通过）----
REPLIES.clear()
r = loop.run_until_complete(system.process_broadcast_info(mk_info(True)))
assert r is True and REPLIES, (r, REPLIES)
text = REPLIES[0][0]["data"]["text"]
assert "全部通过" in text or "存在问题" in text, text
print("PASS: #配置检验 管理员可执行")

# 非管理员 → 拒绝
REPLIES.clear()
r = loop.run_until_complete(system.process_broadcast_info(mk_info(False)))
assert r is True and "仅管理员" in REPLIES[0][0]["data"]["text"], REPLIES
print("PASS: 非管理员拒绝")

# 非命令 → 跳过
REPLIES.clear()
r = loop.run_until_complete(system.process_broadcast_info(mk_info(True, text="hello")))
assert r is False and not REPLIES, (r, REPLIES)
print("PASS: 非命令消息跳过")

# ---- 2) 官方直连语义：缺 appid/secret → 存在问题 ----
cfg["connector"]["QQ"]["connection"] = {"type": "official", "official": {"appid": "", "secret": "", "intents": {}}}
REPLIES.clear()
r = loop.run_until_complete(system.process_broadcast_info(mk_info(True)))
assert r is True and REPLIES
text = REPLIES[0][0]["data"]["text"]
assert "appid: 未配置" in text, text
assert "secret: 未配置" in text, text
assert "存在问题" in text, text
print("PASS: 官方直连缺 appid/secret → 存在问题")

# ---- 3) 官方直连：appid/secret/intents 全配 + 连接器存在，但 admin/group 未配 → 存在问题 ----
cfg["connector"]["QQ"]["permissions"] = {"admin_ids": [], "group_ids": []}
cfg["connector"]["QQ"]["connection"] = {
    "type": "official",
    "official": {"appid": "100000", "secret": "abcdef0123456789", "intents": {"AT_MESSAGE": True}},
}
system._connector_manager = types.SimpleNamespace(connectors=[FakeConnector()])
REPLIES.clear()
r = loop.run_until_complete(system.process_broadcast_info(mk_info(True)))
assert r is True and REPLIES
text = REPLIES[0][0]["data"]["text"]
assert "官方直连" in text and "存在问题" in text, text
print("PASS: 官方直连全配置但 admin/group 缺 → 存在问题")

# ---- 3.5) 全配置含 admin/group → 全部通过 ----
cfg["connector"]["QQ"]["permissions"] = {"admin_ids": ["100000000000"], "group_ids": ["200000000001"]}
REPLIES.clear()
r = loop.run_until_complete(system.process_broadcast_info(mk_info(True)))
assert r is True and REPLIES
text = REPLIES[0][0]["data"]["text"]
assert "全部通过" in text, text
print("PASS: 全配置 → 全部通过 ✅")

# ---- 3.6) 官方模式：group_ids 填了 OneBot 真实群号（非 12 位虚拟号）→ 存在问题 ----
cfg["connector"]["QQ"]["permissions"] = {
    "admin_ids": ["100000000000"],
    "group_ids": ["1103662998"],
}
REPLIES.clear()
r = loop.run_until_complete(system.process_broadcast_info(mk_info(True)))
assert r is True and REPLIES
text = REPLIES[0][0]["data"]["text"]
assert "非虚拟号" in text and "存在问题" in text, text
print("PASS: 官方模式 group_ids 填真实群号 → 提示改用虚拟号 ❌")

# ---- 3.7) 官方模式：启用依赖成员列表的系统 → ⚠️ 提示将不生效 ----
cfg["connector"]["QQ"]["permissions"] = {
    "admin_ids": ["100000000000"],
    "group_ids": ["200000000001"],
}
cfg["system"] = {"unbound_check": {"enable": True}}
REPLIES.clear()
r = loop.run_until_complete(system.process_broadcast_info(mk_info(True)))
assert r is True and REPLIES
text = REPLIES[0][0]["data"]["text"]
assert "未绑定检查" in text and "将不生效" in text, text
print("PASS: 官方模式启用未绑定检查 → 提示不生效 ⚠️")
cfg["system"] = {}

# ---- 3.8) max_message_length <= 0 → 提示分片死循环风险 ----
cfg["connector"]["QQ"]["max_message_length"] = 0
REPLIES.clear()
r = loop.run_until_complete(system.process_broadcast_info(mk_info(True)))
assert r is True and REPLIES
text = REPLIES[0][0]["data"]["text"]
assert "max_message_length" in text and "存在问题" in text, text
print("PASS: max_message_length<=0 → 提示死循环风险 ❌")

# ---- 3.9) max_message_length 非数字 → 提示回退默认 ----
cfg["connector"]["QQ"]["max_message_length"] = "不是数字"
REPLIES.clear()
r = loop.run_until_complete(system.process_broadcast_info(mk_info(True)))
assert r is True and REPLIES
text = REPLIES[0][0]["data"]["text"]
assert "不是数字" in text, text
print("PASS: max_message_length 非数字 → 提示回退默认 ❌")

# ---- 3.10) max_message_length 正常值 → 通过 ----
cfg["connector"]["QQ"]["max_message_length"] = 2000
REPLIES.clear()
r = loop.run_until_complete(system.process_broadcast_info(mk_info(True)))
assert r is True and REPLIES
text = REPLIES[0][0]["data"]["text"]
assert "max_message_length: 2000" in text, text
print("PASS: max_message_length 正常 → 通过 ✅")

# ---- 4) 官方直连但连接器未启动 → 存在问题 ----
system._connector_manager = types.SimpleNamespace(connectors=[])
REPLIES.clear()
r = loop.run_until_complete(system.process_broadcast_info(mk_info(True)))
assert r is True and REPLIES
text = REPLIES[0][0]["data"]["text"]
assert "未启动" in text or "未连接" in text, text
assert "存在问题" in text, text
print("PASS: 官方直连连接器未启动 → 存在问题 ❌")

print()
print("CONFIG CHECK TEST PASSED")
