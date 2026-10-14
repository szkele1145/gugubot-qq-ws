# -*- coding: utf-8 -*-
"""端到端集成测试：官方 WS 事件 → parser → (BroadcastInfo) → MC native_chat 注入。

模拟完整数据流，验证：
1. 官方 GROUP_AT_MESSAGE_CREATE 事件解析（含图片附件）
2. 图片附件下载缓存（用本地 http server 提供）
3. BroadcastInfo 结构正确（群号绑定/虚拟号/管理员判定）
4. native_chat 注入命令生成（CICode 图片 + 转义 + 前缀）
"""
import sys, types, json, threading, time, socket, base64, hashlib, struct
import os, tempfile, pathlib

# ===== stub mcdreforged =====
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

# ===== 本地 HTTP 附件服务器 =====
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

def serve_http():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 19878))
    srv.listen(2)
    def handle(c):
        req = c.recv(4096)  # 忽略请求头
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: %d\r\n\r\n" % len(PNG) + PNG)
        c.close()
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()

threading.Thread(target=serve_http, daemon=True).start()
time.sleep(0.2)

# ===== 构造环境 =====
import asyncio
from gugubot.connector.qq_official.id_map import QQIDMapper
from gugubot.connector.qq_official.reply_pool import QQReplyPool
from gugubot.connector.qq_official.parser import QQOfficialParser

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

data_dir = pathlib.Path(tempfile.mkdtemp())

cfg = FakeConfig()
cfg["connector"] = {
    "QQ": {
        "source_name": "QQ",
        "permissions": {
            "group_ids": ["987654"],
            "admin_ids": [],
            "admin_group_ids": [],
            "custom_group_name": {},
        },
        "connection": {"official": {"cache_attachments": True}},
    },
    "minecraft": {
        "source_name": "Minecraft",
        "native_chat": {"enable": True, "show_group_prefix": True},
    },
}

class FakeServer:
    def get_data_folder(self): return str(data_dir)

class _FakeBot:
    self_id = "BOT_E2E_ID"

class FakeConnector:
    def __init__(self):
        self.config = cfg
        self.source = "QQ"
        self.logger = FakeLogger()
        self.server = FakeServer()
        self.id_mapper = QQIDMapper(data_file=data_dir / "ids.json")
        self.reply_pool = QQReplyPool()
        self.bot = _FakeBot()
        # 真实流程：connector.__init__ 会登记配置群号
        self.id_mapper.register_legacy_groups(["987654"])

conn = FakeConnector()
p = QQOfficialParser.__new__(QQOfficialParser)
p.connector = conn
p.logger = FakeLogger()
p.server = FakeServer()
p.system_manager = None

# ===== 事件：群消息带图片附件 =====
event = {
    "id": "MSG_E2E_1",
    "group_openid": "GD_E2E",
    "content": "<@!BOT> 看图 ",
    "author": {"member_openid": "UD_E2E"},
    "attachments": [{
        "content_type": "image/png",
        "url": "http://127.0.0.1:19878/img.png",
        "filename": "img.png",
        "summary": "[截图]",
    }],
}
info = asyncio.new_event_loop().run_until_complete(p.parse({"_event_name": "GROUP_AT_MESSAGE_CREATE", "_data": event}))
assert info is not None, "消息应被解析"
assert info.source_id == "987654", f"应绑定到配置群号: {info.source_id}"
assert info.event_sub_type == "group"
assert info.event_type == "message"

# 图片段应已缓存为本地文件
img_seg = next(s for s in info.message if s["type"] == "image")
local_file = img_seg["data"]["file"]
assert os.path.isfile(local_file), f"附件应已下载: {local_file}"
assert open(local_file, "rb").read() == PNG, "缓存内容应与源一致"
print("PASS: 事件解析 + 群绑定 + 图片附件下载缓存")

# ===== native_chat 注入命令生成 =====
from gugubot.utils.message import segments_to_plain_text
from gugubot.connector.mc_connector import MCConnector

EXECUTED = []
class FakeServerInfo:
    version = "1.21.1"
class FakeMCServer:
    def is_server_running(self): return True
    def get_server_information(self): return FakeServerInfo()
    def execute(self, cmd): EXECUTED.append(cmd)
    def say(self, x): pass

class FakeSM:
    def get_system(self, name): return None
class FakeCM:
    def get_connector(self, name): return None
    system_manager = FakeSM()

mc = MCConnector.__new__(MCConnector)
mc.source = "Minecraft"
mc.config = cfg
mc.server = FakeMCServer()
mc.logger = FakeLogger()
mc.builder = _Any()
mc.connector_manager = FakeCM()
mc.enable = True
mc._native_chat_checked = False
mc._native_chat_available = False
mc.log_prefix = "[MC]"

from gugubot.utils.types import ProcessedInfo, Source
pi = ProcessedInfo(
    processed_message=info.message,
    _source=Source("QQ"),
    source_id="987654",
    sender="Steve",
    sender_id="100000000001",
)
import traceback as _tb

async def _run():
    try:
        await mc.send_message(pi)
    except Exception:
        _tb.print_exc()

asyncio.new_event_loop().run_until_complete(_run())
assert len(EXECUTED) == 1, EXECUTED
cmd = EXECUTED[0]
print("CMD:", cmd[:150])
assert cmd.startswith('guguchat inject "Steve" [QQ转发]Steve:'), cmd
# CICode 应优先公网 url（客户端可访问），而非服务器本地路径
assert "[[CICode,url=http://127.0.0.1:19878/img.png,name=截图]]" in cmd, cmd
assert "看图" in cmd
print("PASS: native_chat 端到端注入命令（[QQ转发]格式 + CICode 公网图片 + message 原文）")

# ===== C2C 单聊链路 =====
c2c_event = {
    "id": "C2C_E2E_1",
    "author": {"user_openid": "UD_E2E"},
    "content": "你好",
}
c2c_info = asyncio.new_event_loop().run_until_complete(
    p.parse({"_event_name": "C2C_MESSAGE_CREATE", "_data": c2c_event})
)
assert c2c_info is not None and c2c_info.event_sub_type == "private"
assert c2c_info.source_id == conn.id_mapper.to_legacy_user("UD_E2E")
mid, seq = conn.reply_pool.take("c2c:UD_E2E")
assert mid == "C2C_E2E_1"
print("PASS: C2C 单聊链路（虚拟号 + 回复池）")

print()
print("=" * 40)
print("END-TO-END INTEGRATION TEST PASSED")
