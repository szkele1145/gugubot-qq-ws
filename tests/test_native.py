# -*- coding: utf-8 -*-
import sys, types

class _Any:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return _Any()
    def __call__(self, *a, **k): return _Any()

class _StubModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        v = _Any()
        setattr(self, name, v)
        return v

import importlib.abc, importlib.machinery

class _Loader(importlib.abc.Loader):
    def create_module(self, spec):
        return _StubModule(spec.name)
    def exec_module(self, module):
        pass

class _StubFinder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("mcdreforged") or fullname.startswith("packaging"):
            return importlib.machinery.ModuleSpec(fullname, _Loader(), is_package=True)
        return None

sys.meta_path.insert(0, _StubFinder())
sys.path.insert(0, r'F:\vibe coding工程\gugubot-qq-ws\plugin')

# ===== 测试 segments_to_plain_text =====
from gugubot.utils.message import segments_to_plain_text, extract_images, strip_minecraft_color_codes

segs = [
    {"type": "text", "data": {"text": "看这张图§a绿色"}},
    {"type": "image", "data": {"file": r"C:\attach\x1.png", "url": "https://qq/cdn/x1", "summary": "[截图]"}},
    {"type": "at", "data": {"qq": "100000000001"}},
]
t = segments_to_plain_text(segs, image_as="cicode")
assert "[[CICode,url=https://qq/cdn/x1,name=截图]]" in t, t  # 新设计：公网 url 优先
assert "看这张图绿色" in t
assert "@100000000001" in t
t2 = segments_to_plain_text(segs)  # label 模式
assert "[图片:截图]" in t2, t2
imgs = extract_images(segs)
assert len(imgs) == 1 and imgs[0]["url"] == "https://qq/cdn/x1"
print("PASS: segments_to_plain_text / extract_images / cicode / 颜色码剥离")

# ===== 测试 native_chat 分支（mock connector）=====
import asyncio
from gugubot.connector.mc_connector import MCConnector

class FakeLogger:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): print("[warn]", *a)
    def error(self, *a, **k): print("[err]", *a)

class FakeConfig(dict):
    def get_keys(self, key, default=None):
        r = self
        for k in key[:-1]:
            r = r.get(k, {})
        return r.get(key[-1], default)

cfg = FakeConfig()
cfg["connector"] = {
    "minecraft": {"source_name": "Minecraft", "native_chat": {"enable": True, "show_group_prefix": True}},
    "QQ": {"source_name": "QQ", "permissions": {"custom_group_name": {}}},
}

EXECUTED = []
class FakeServer:
    logger = FakeLogger()
    def tr(self, *a, **k): return a[0] if a else ""
    def is_server_running(self): return True
    def get_server_information(self): return _Any()
    def execute_command(self, cmd): EXECUTED.append(cmd)
    def say(self, x): pass

conn = MCConnector.__new__(MCConnector)
conn.source = "Minecraft"
conn.config = cfg
conn.server = FakeServer()
conn.logger = FakeLogger()
conn.builder = _Any()
class FakeSM:
    def get_system(self, name): return None
class FakeCM:
    def get_connector(self, name): return None
    system_manager = FakeSM()
conn.connector_manager = FakeCM()
conn.log_prefix = "[MC]"
conn.enable = True
conn._native_chat_checked = False
conn._native_chat_available = False

from gugubot.utils.types import ProcessedInfo, Source

async def run():
    pi = ProcessedInfo(
        processed_message=[{"type": "text", "data": {"text": '你好"世界"\\���斜杠'}}],
        _source=Source("QQ"),
        source_id="123456",
        sender="Steve",
        sender_id="100000000001",
    )
    await conn.send_message(pi)

asyncio.run(run())
assert len(EXECUTED) == 1, EXECUTED
cmd = EXECUTED[0]
print("CMD:", cmd)
assert cmd.startswith('guguchat inject "Steve" "'), cmd
# 引号/反斜杠已转义
assert chr(92) + chr(34) in cmd, cmd  # 引号已转义为 \" 
# 群名前缀
assert '"[QQ] ' in cmd, cmd
print("PASS: native_chat 注入命令生成 + brigadier 转义 + 群前缀")

# show_group_prefix=false
cfg["connector"]["minecraft"]["native_chat"]["show_group_prefix"] = False
EXECUTED.clear()
asyncio.run(run())
assert '"[QQ] ' not in EXECUTED[0], EXECUTED[0]
print("PASS: show_group_prefix=false 无前缀")

print()
print("ALL NATIVE_CHAT TESTS PASSED")



