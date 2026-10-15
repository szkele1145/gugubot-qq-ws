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
    "minecraft": {
        "source_name": "Minecraft",
        "native_chat": {"enable": True, "show_group_prefix": True},
    },
    "QQ": {"source_name": "QQ", "permissions": {"custom_group_name": {"123456": "一群"}}},
}

EXECUTED = []
class FakeServer:
    logger = FakeLogger()
    def tr(self, *a, **k): return a[0] if a else ""
    def is_server_running(self): return True
    def get_server_information(self): return _Any()
    def execute(self, cmd): EXECUTED.append(cmd)
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
print("CMD:", repr(cmd))  # repr 对非 ASCII 转义，避免 gbk 控制台 UnicodeEncodeError

import json as _json

# 新格式：tellraw @a <json>，正文 = [群名] [QQ转发]名字:消息
assert cmd.startswith("tellraw @a "), cmd
payload = _json.loads(cmd[len("tellraw @a "):])
text = "".join(p.get("text", "") for p in payload)
# 群名前缀 + [QQ转发]名字: 都在
assert text.startswith("[一群] [QQ转发]Steve:"), text
# 消息原文（含引号/反斜杠）逐字保留，不做 brigadier 转义
assert '你好"世界"\\' in text, text
print("PASS: tellraw 系统消息注入（[群名] [QQ转发]名字: + 消息原文）")

# show_group_prefix=false → 只去掉 [群名]，[QQ转发]名字: 必须保留
cfg["connector"]["minecraft"]["native_chat"]["show_group_prefix"] = False
EXECUTED.clear()
asyncio.run(run())
cmd2 = EXECUTED[0]
assert cmd2.startswith("tellraw @a "), cmd2
text2 = "".join(p.get("text", "") for p in _json.loads(cmd2[len("tellraw @a "):]))
assert "[一群]" not in text2, text2
assert text2.startswith("[QQ转发]Steve:"), text2
print("PASS: show_group_prefix=false 只去掉[群名]，[QQ转发]名字: 依然存在")

# prefix_color 生效
cfg["connector"]["minecraft"]["native_chat"]["show_group_prefix"] = True
cfg["connector"]["minecraft"]["native_chat"]["prefix_color"] = "gold"
EXECUTED.clear()
asyncio.run(run())
pl3 = _json.loads(EXECUTED[0][len("tellraw @a "):])
assert any(p.get("color") == "gold" and "[QQ转发]" in p.get("text", "") for p in pl3), pl3
print("PASS: prefix_color 应用到 [QQ转发]名字: 片段")

# 危险字符不会破坏命令（换行/控制字符已转义，json 单行）
cfg["connector"]["minecraft"]["native_chat"]["prefix_color"] = ""
async def run_danger():
    pi = ProcessedInfo(
        processed_message=[{"type": "text", "data": {"text": "第一行\n第二行\r结尾"}}],
        _source=Source("QQ"),
        source_id="123456",
        sender="坏\"名",
        sender_id="100000000002",
    )
    await conn.send_message(pi)
EXECUTED.clear()
asyncio.run(run_danger())
c = EXECUTED[0]
assert "\n" not in c and "\r" not in c, repr(c)
pl4 = _json.loads(c[len("tellraw @a "):])
t4 = "".join(p.get("text", "") for p in pl4)
assert "第一行 第二行 结尾" in t4, t4
assert '[QQ转发]坏"名:' in t4, t4
print("PASS: 换行/引号安全（单行 json，正文换行折叠为空格）")

print()
print("ALL NATIVE_CHAT TESTS PASSED")



