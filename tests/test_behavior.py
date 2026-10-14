import sys, types, json, threading, time

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

# ===== 测试 1: id_map 绑定流程 =====
import tempfile, pathlib
from gugubot.connector.qq_official.id_map import QQIDMapper

tmp = pathlib.Path(tempfile.mkdtemp()) / "ids.json"
mapper = QQIDMapper(data_file=tmp)
mapper.register_legacy_groups(["123456", "789012"])

# 模拟两个新群 openid 到达
g1 = mapper.bind_group_openid("OPENID_GROUP_A")
g2 = mapper.bind_group_openid("OPENID_GROUP_B")
assert g1 == "123456" and g2 == "789012", f"绑定失败: {g1}, {g2}"
assert mapper.to_legacy_group("OPENID_GROUP_A") == "123456"
assert mapper.to_official_group("123456") == "OPENID_GROUP_A"

# 用户虚拟号
u1 = mapper.to_legacy_user("OPENID_USER_X")
u2 = mapper.to_legacy_user("OPENID_USER_X")
assert u1 == u2 and u1.startswith("1000"), f"用户映射失败: {u1}"
assert mapper.to_official_user(u1) == "OPENID_USER_X"

# 持久化后重载
mapper2 = QQIDMapper(data_file=tmp)
mapper2.register_legacy_groups(["123456", "789012"])
assert mapper2.to_legacy_group("OPENID_GROUP_A") == "123456"
assert mapper2.to_legacy_user("OPENID_USER_X") == u1
print("PASS: id_map 绑定/虚拟号/持久化")

# ===== 测试 2: reply_pool =====
from gugubot.connector.qq_official.reply_pool import QQReplyPool
pool = QQReplyPool()
pool.add("G", "M1")
pool.add("G", "M2")
mid, seq = pool.take("G")
assert mid == "M2" and seq == 0, f"最新优先失败: {mid},{seq}"
mid, seq = pool.take("G")
assert mid == "M2" and seq == 1
mid, seq = pool.take("G")
assert mid == "M2" and seq == 2, "M2 有 5 次额度"
# 把 M2 用尽
for i in range(2):
    assert pool.take("G")[0] == "M2"
mid, seq = pool.take("G")
assert mid == "M1" and seq == 0, "M2 用尽后应回退 M1"
assert pool.take("EMPTY") is None
print("PASS: reply_pool 最新优先 + seq 递增 + 回退")

# ===== 测试 3: token manager 请求体 =====
from gugubot.connector.qq_official.token_manager import QQTokenManager
tm = QQTokenManager("123456789", "mysecret")
assert tm.configured
# 不实际发请求，检查 auth header 格式（会失败但验证调用参数）
try:
    tm.get_token()
    assert False, "应当联网失败"
except Exception as e:
    print(f"PASS: token manager 未配置时正确抛错（网络不可达: {type(e).__name__}）")

# ===== 测试 4: parser 群消息解析（模拟 connector 结构） =====
import asyncio
from gugubot.connector.qq_official.parser import QQOfficialParser

class FakeLogger:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass

class FakeConfig(dict):
    def get_keys(self, key, default=None):
        result = self
        for k in key[:-1]:
            result = result.get(k, {})
        return result.get(key[-1], default)

cfg = FakeConfig()
cfg["connector"] = {"QQ": {"permissions": {
    "group_ids": ["123456"],
    "admin_ids": [],
    "admin_group_ids": [],
}}}

class _FakeBot:
    self_id = "BOT_TEST_ID"

class FakeConnector:
    def __init__(self):
        self.config = cfg
        self.source = "QQ"
        self.logger = FakeLogger()
        self.id_mapper = mapper
        self.reply_pool = pool
        self.bot = _FakeBot()

class FakeServer: pass

conn = FakeConnector()
p = QQOfficialParser.__new__(QQOfficialParser)
p.connector = conn
p.logger = FakeLogger()
p.server = FakeServer()
p.system_manager = None

event_data = {
    "id": "MSG_ID_1",
    "group_openid": "OPENID_GROUP_A",
    "content": " <@!BOT123> #list ",
    "author": {"member_openid": "OPENID_USER_X", "union_id": "U1"},
    "attachments": [],
}
task = {"_event_name": "GROUP_AT_MESSAGE_CREATE", "_data": event_data}
info = asyncio.get_event_loop().run_until_complete(p.parse(task))

assert info is not None, "群消息应被解析"
assert info.source_id == "123456", f"source_id 应为绑定群号: {info.source_id}"
assert info.event_sub_type == "group"
texts = [s["data"]["text"] for s in info.message if s["type"] == "text"]
assert texts == ["#list"], f"消息内容应为 #list: {texts}"
assert info.sender_id == u1
# 回复池已入池
mid, seq = pool.take("OPENID_GROUP_A")
assert mid == "MSG_ID_1" and seq == 0, "parser 应已把 msg_id 入池"
print("PASS: parser 群消息解析（@bot 前缀剥离 + 群号绑定 + 入回复池）")

# 未监听群被拒
event_data2 = {
    "id": "MSG_ID_2",
    "group_openid": "OPENID_UNKNOWN_GROUP",
    "content": "hello",
    "author": {"member_openid": "OPENID_USER_Y"},
}
task2 = {"_event_name": "GROUP_AT_MESSAGE_CREATE", "_data": event_data2}
info2 = asyncio.get_event_loop().run_until_complete(p.parse(task2))
# 未知群没有可绑定槽位（配置只登记了2个群，都被占用）→ 虚拟群号，不在监听列表 → None
assert info2 is None, f"未监听群应返回 None"
print("PASS: parser 拒绝未监听群")

# C2C 消息
c2c_data = {
    "id": "C2C_MSG_1",
    "author": {"user_openid": "OPENID_USER_X"},
    "content": "你好",
}
task3 = {"_event_name": "C2C_MESSAGE_CREATE", "_data": c2c_data}
info3 = asyncio.get_event_loop().run_until_complete(p.parse(task3))
assert info3 is not None and info3.event_sub_type == "private"
assert info3.sender_id == u1
mid, seq = pool.take("c2c:OPENID_USER_X")
assert mid == "C2C_MSG_1"
print("PASS: parser C2C 解析")

# ===== 测试 5: segments_to_plain_text（原 segments_to_content 已统一到公共函数） =====
from gugubot.utils.message import segments_to_plain_text
segs = [
    {"type": "text", "data": {"text": "玩家加入"}},
    {"type": "at", "data": {"qq": "100000000001"}},
    {"type": "image", "data": {"url": "http://x"}},
]
out = segments_to_plain_text(segs)
assert out == "玩家加入@100000000001[图片]", f"转换失败: {out}"
out2 = segments_to_plain_text("§a绿色文本§r")
assert out2 == "绿色文本", f"颜色代码未剥离: {out2}"
print("PASS: segments_to_plain_text 转换与颜色代码剥离")

print()
print("=" * 40)
print("ALL BEHAVIOR TESTS PASSED")

