# -*- coding: utf-8 -*-
"""配置热重载（ConfigReloadSystem / BotConfig.reload）测试。

覆盖真实缺陷与不变量：
1. 重载必须是**原子**的——YAML 语法错误时旧配置原封不动继续生效；
2. 重载**不得**把内存配置清成插件生成的默认模板；
3. 系统 enable 开关需在重载后**显式同步**（BasicSystem.enable 是构造期属性）；
4. 配置文件前缀本身可被重载改写，后续命令要用新前缀；
5. 非管理员不得触发重载。
"""
import sys, types, asyncio, tempfile, os, importlib.abc, importlib.machinery


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

from gugubot.config.bot_config import BotConfig
from gugubot.logic.system.config_reload import ConfigReloadSystem
from gugubot.utils.types import BroadcastInfo, Source


class FL:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


REPLIES = []


async def _reply(self, info, msg):
    REPLIES.append(msg)


import gugubot.logic.system.config_reload as cr_mod
cr_mod.ConfigReloadSystem.reply_to_source = _reply


def mk_info(admin=True, text="#重载配置"):
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


class FakeSystem:
    def __init__(self, name, enable):
        self.name = name
        self.enable = enable


def make_system(tmpdir):
    """构造一个带真实临时配置文件的重载系统。"""
    cfg_path = os.path.join(tmpdir, "config.yml")
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("GUGUBot:\n  command_prefix: '#'\nsystem:\n  echo:\n    enable: true\n")

    cfg = BotConfig(cfg_path)
    cfg["GUGUBot"] = {"command_prefix": "#"}
    cfg["system"] = {"echo": {"enable": True}, "vote": {"enable": False}}

    system = ConfigReloadSystem.__new__(ConfigReloadSystem)
    system.name = "config_reload"
    system.enable = True
    system.logger = FL()
    system.config = cfg
    system._connector_manager = None
    system.system_manager = types.SimpleNamespace(systems=[
        FakeSystem("echo", True),
        FakeSystem("vote", False),
        FakeSystem("todo", True),
    ])
    return system, cfg, cfg_path


loop = asyncio.new_event_loop()


# ---------------------------------------------------------------- 权限
def test_non_admin_is_rejected(tmp_path):
    system, cfg, _ = make_system(str(tmp_path))
    REPLIES.clear()
    r = loop.run_until_complete(system.process_broadcast_info(mk_info(False)))
    assert r is True and REPLIES
    assert "仅管理员" in REPLIES[0][0]["data"]["text"], REPLIES
    print("PASS: 非管理员拒绝重载")


def test_non_command_message_is_ignored(tmp_path):
    system, cfg, _ = make_system(str(tmp_path))
    REPLIES.clear()
    r = loop.run_until_complete(system.process_broadcast_info(mk_info(True, "你好")))
    assert r is False and not REPLIES
    print("PASS: 非命令消息跳过")


# ---------------------------------------------------------------- 正常路径
def test_reload_applies_disk_content_and_syncs_enable(tmp_path):
    system, cfg, cfg_path = make_system(str(tmp_path))
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(
            "GUGUBot:\n  command_prefix: '!'\n"
            "system:\n  echo:\n    enable: false\n  vote:\n    enable: true\n"
        )
    REPLIES.clear()
    r = loop.run_until_complete(system.process_broadcast_info(mk_info(True)))
    assert r is True and REPLIES
    text = REPLIES[0][0]["data"]["text"]
    assert "重载成功" in text, text
    # 内存配置已被磁盘内容替换
    assert cfg["GUGUBot"]["command_prefix"] == "!", cfg["GUGUBot"]
    assert cfg["system"]["echo"]["enable"] is False, cfg["system"]
    assert cfg["system"]["vote"]["enable"] is True, cfg["system"]
    # 系统开关已同步（enable 是构造期属性，必须显式刷新）
    systems = system.system_manager.systems
    assert systems[0].enable is False, systems[0].enable
    assert systems[1].enable is True, systems[1].enable
    # 配置里没有 todo 段 → 不动
    assert systems[2].enable is True
    assert "系统开关已同步" in text, text
    print("PASS: 管理员重载成功 + 系统开关同步")


def test_english_alias_works(tmp_path):
    system, cfg, cfg_path = make_system(str(tmp_path))
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("GUGUBot:\n  command_prefix: '#'\nsystem:\n  echo:\n    enable: true\n")
    REPLIES.clear()
    r = loop.run_until_complete(system.process_broadcast_info(mk_info(True, "#reload config")))
    assert r is True and REPLIES
    assert "重载成功" in REPLIES[0][0]["data"]["text"], REPLIES
    print("PASS: 英文别名 reload config 生效")


def test_new_prefix_takes_effect_after_reload(tmp_path):
    """重载把 command_prefix 改成 '!' 之后，'#重载配置' 不再被识别。"""
    system, cfg, cfg_path = make_system(str(tmp_path))
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("GUGUBot:\n  command_prefix: '!'\nsystem:\n  echo:\n    enable: true\n")
    REPLIES.clear()
    assert loop.run_until_complete(
        system.process_broadcast_info(mk_info(True))
    ) is True
    # 前缀已变 → 旧前缀命令不再匹配
    REPLIES.clear()
    r = loop.run_until_complete(system.process_broadcast_info(mk_info(True, "#重载配置")))
    assert r is False and not REPLIES, (r, REPLIES)
    # 新前缀可继续重载
    REPLIES.clear()
    r = loop.run_until_complete(system.process_broadcast_info(mk_info(True, "!重载配置")))
    assert r is True and REPLIES
    print("PASS: 重载后的新 command_prefix 立即生效")


# ---------------------------------------------------------------- 原子性
def test_broken_yaml_keeps_old_config(tmp_path):
    system, cfg, cfg_path = make_system(str(tmp_path))
    before = dict(cfg.get("GUGUBot", {}))
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("GUGUBot:\n  command_prefix: '#'\n  bad: [unclosed\n")
    REPLIES.clear()
    r = loop.run_until_complete(system.process_broadcast_info(mk_info(True)))
    assert r is True and REPLIES, (r, REPLIES)
    text = REPLIES[0][0]["data"]["text"]
    assert "重载失败" in text, text
    assert "旧配置仍在生效" in text, text
    # 旧配置未被破坏
    assert cfg["GUGUBot"]["command_prefix"] == before["command_prefix"], cfg["GUGUBot"]
    print("PASS: 语法错误 → 重载失败且旧配置保留 ✅")


def test_reload_missing_file_returns_failure(tmp_path):
    missing = BotConfig(str(tmp_path / "nonexistent.yml"))
    ok, msg = missing.reload()
    assert ok is False and msg, (ok, msg)
    print("PASS: reload() 文件不存在 → 返回失败而非抛异常")


def test_default_template_is_not_reloadable(tmp_path):
    """插件启动时自动生成的默认模板不是用户配置，重载它会清空内存配置。"""
    fresh = BotConfig(
        str(tmp_path / "fresh.yml"),
        default_content={"GUGUBot": {"command_prefix": "#"}},
    )
    ok, msg = fresh.reload()
    assert ok is False, (ok, msg)
    assert "默认模板" in msg, msg
    assert fresh.get("GUGUBot", {}).get("command_prefix") == "#", fresh.get("GUGUBot")
    print("PASS: 默认模板 → 拒绝重载且不破坏内存配置")
