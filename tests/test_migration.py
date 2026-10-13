# -*- coding: utf-8 -*-
"""旧配置迁移测试：v2.2.4 老配置 → v2.2.5 新键自动补全且用户值保留。

用户要求"和原有配置文件兼容"——本测试模拟：
1. 服务器上已有一份 v2.2.4 时代的 config.yml（用户改过 group_ids/admin_id 等）
2. 装上改造版插件后 add_new_config() 做合并迁移
3. 验证：用户值原样保留 + 新键（connection.type / official / native_chat）补全
"""
import sys, types, io, os, tempfile, pathlib

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

from ruamel.yaml import YAML
yaml = YAML()
yaml.preserve_quotes = True

PLUGIN_DIR = pathlib.Path(r'F:\vibe coding工程\gugubot-qq-ws\plugin')

# ===== 模拟 v2.2.4 老配置（用户自定义值：真实群号、管理员、自定义群名） =====
OLD_CONFIG = """\
######################### GUGUBot设置 #################################

GUGUBot:
  command_prefix: "#"
  group_admin: false
  show_message_in_console: false

########################### 连接设置 ##################################

connector:
  QQ:
    source_name: "QQ"
    enable: true
    enable_receive: true
    enable_send: true
    connection:
      host: 127.0.0.1
      port: 8777
      post_path: ""
      use_ssl: false
      verify: true
      ca_certs: ""
      sslopt: { }
      reconnect: 5
      max_wait_time: 5
      token: "my-secret-onebot-token"
    permissions:
      admin_ids:
        - 1377820366
      admin_group_ids:
        -
      group_ids:
        - 111222333
      forward_group_ids:
        -
      notice_forward_groups:
        -
      friend_is_admin: false
      custom_group_name:
        "111222333": 我的玩家群
    chat_templates:
      - "({display_name}) {sender}: ": 1
    others:
      forward_other_bot: false
      change_group_card: true

  minecraft:
    source_name: "Minecraft"
    enable: true
    enable_receive: true
    enable_send: true
    mc_achievement: true
    mc_death: true
    player_join_notice: true
    player_left_notice: true
    bot_join_notice: true
    bot_left_notice: true
    player_notice_cooldown: 0
    server_start_notice: true
    server_stop_notice: true
    chat_image: false
    image_previewer: false
    player_join_patterns:
      - "([^\\\\s]+) joined the game"
    player_left_patterns:
      - "([^\\\\s]+) left the game"
    bot_names_pattern:
      - ".*bot.*"
    ignore_mc_command_patterns:
      - "!!.*"

  minecraft_bridge:
    source_name: "Main"
    enable: true
    enable_receive: true
    enable_send: true
    is_main_server: true
"""

data_dir = pathlib.Path(tempfile.mkdtemp())
config_path = data_dir / "config.yml"
config_path.write_text(OLD_CONFIG, encoding="utf-8")

# ===== 模拟 server.open_bundled_file（返回新默认配置） =====
DEFAULT_YML = (PLUGIN_DIR / "gugubot" / "config" / "defaults" / "default_config.yml").read_bytes()

class FakeServer:
    def open_bundled_file(self, rel):
        assert rel == "gugubot/config/defaults/default_config.yml", rel
        return io.BytesIO(DEFAULT_YML)
    def get_data_folder(self):
        return str(data_dir)

from gugubot.config import BotConfig

server = FakeServer()
cfg = BotConfig(config_path)
cfg.add_new_config(server)   # ← 迁移发生在这里

# ===== 断言 =====
conn = cfg["connector"]

# 1. 用户值保留
assert conn["QQ"]["connection"]["token"] == "my-secret-onebot-token", "用户 token 被覆盖！"
assert conn["QQ"]["connection"]["host"] == "127.0.0.1"
assert conn["QQ"]["connection"]["port"] == 8777
assert conn["QQ"]["permissions"]["group_ids"] == [111222333], "用户群号被覆盖！"
assert conn["QQ"]["permissions"]["admin_ids"] == [1377820366]
assert conn["QQ"]["permissions"]["custom_group_name"]["111222333"] == "我的玩家群"
assert conn["QQ"]["chat_templates"] == [{"({display_name}) {sender}: ": 1}]
assert conn["minecraft"]["source_name"] == "Minecraft"
print("PASS: 旧配置用户自定义值全部保留")

# 2. 新键补全
assert conn["QQ"]["connection"]["type"] == "onebot", "应补全 connection.type=onebot（默认走旧 OneBot 行为）"
assert "official" in conn["QQ"]["connection"], "应补全 official 配置段"
assert conn["QQ"]["connection"]["official"]["appid"] == ""
assert conn["QQ"]["connection"]["official"]["secret"] == ""
assert conn["QQ"]["connection"]["official"]["intents"]["group_at_messages"] is True
assert "native_chat" in conn["minecraft"], "应补全 minecraft.native_chat 配置段"
assert conn["minecraft"]["native_chat"]["enable"] is False
assert conn["minecraft"]["native_chat"]["show_group_prefix"] is True
print("PASS: 新键自动补全（type/official/native_chat），默认不改变旧行为")

# 3. 迁移后文件可再次正常加载（无损坏）
cfg2 = BotConfig(config_path)
assert cfg2["connector"]["QQ"]["connection"]["type"] == "onebot"
print("PASS: 迁移产物可重复加载")

print()
print("=" * 40)
print("OLD-CONFIG MIGRATION TEST PASSED")
