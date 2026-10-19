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


# ---------------------------------------------------------------- reload_config 钩子
def test_vote_system_caches_are_rebuilt(tmp_path):
    """投票系统把关键词/投票类型缓存成实例属性，热重载必须重建。"""
    system, cfg, cfg_path = make_system(str(tmp_path))

    # 用真实的 VoteSystem 实例（__new__ 跳过 server 依赖），确保测的是真代码路径
    from gugubot.logic.system.vote import VoteSystem
    from gugubot.utils.vote_manager import VoteTypeRegistry

    vote = VoteSystem.__new__(VoteSystem)
    vote.name = "vote"
    vote.enable = True
    vote.logger = FL()
    vote.config = cfg
    vote.vote_type_registry = VoteTypeRegistry()
    vote.debug_enabled = False
    vote._monitor_tasks = {}

    # 用真实 _load_config 填充初始关键词
    vote._load_config()
    assert vote.yes_keywords == ["111", "同意", "赞成", "yes"], vote.yes_keywords

    # 初始注册 shutdown 投票类型（真实路径）
    vote._register_default_vote_types()
    sc0 = vote.vote_type_registry.get_config("shutdown")
    assert sc0 is not None and sc0.required_percentage == 1.0, sc0
    assert sc0.timeout == 300, sc0

    # 配置里给 vote 换关键词 + 换 shutdown 参数
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(
            "GUGUBot:\n  command_prefix: '#'\n"
            "system:\n  echo: {enable: true}\n"
            "  vote:\n"
            "    keywords:\n"
            "      yes: ['好的', '可以']\n"
            "    shutdown:\n"
            "      required_percentage: 0.8\n"
            "      timeout: 120\n"
            "      enable: true\n"
        )
    REPLIES.clear()
    r = loop.run_until_complete(system.process_broadcast_info(mk_info(True)))
    assert r is True and REPLIES
    # 先验证配置已进内存
    assert cfg.get_keys(["system", "vote", "keywords", "yes"]) == ["好的", "可以"]

    # 把 vote 塞进 system_manager，钩子应重建其缓存
    system.system_manager.systems.append(vote)
    changed = system._reload_system_caches()
    assert vote.yes_keywords == ["好的", "可以"], vote.yes_keywords
    sc = vote.vote_type_registry.get_config("shutdown")
    assert sc is not None and sc.required_percentage == 0.8, sc
    assert sc.timeout == 120, sc
    assert any("vote" in c for c in changed), changed
    print("PASS: vote 缓存经 reload_config 钩子重建 ✅")


def test_reload_config_hook_failure_is_reported(tmp_path):
    """某个系统 reload_config 抛异常不应炸掉整个重载流程。"""
    system, cfg, cfg_path = make_system(str(tmp_path))
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("GUGUBot:\n  command_prefix: '#'\nsystem:\n  echo:\n    enable: true\n")

    class BadSystem:
        name = "bad_system"
        enable = True

        def reload_config(self):
            raise RuntimeError("boom")

    system.system_manager.systems.append(BadSystem())
    notes = system._reload_system_caches()
    assert any("bad_system" in n and "重载失败" in n for n in notes), notes
    print("PASS: 单系统重载异常被隔离并提示 ✅")


# ---------------------------------------------------------------- 重载后自动语义校验
class FakeChecker:
    """ConfigCheckSystem 的行为替身：_check_* 返回可注入的结果。"""
    name = "config_check"

    def __init__(self, syntax_ok=True, general=None, qq=None):
        self._syntax_ok = syntax_ok
        self._general = general or {"ok": True, "lines": []}
        self._qq = qq or {"ok": True, "lines": []}

    def _check_syntax(self):
        return self._syntax_ok, "" if self._syntax_ok else "bad yaml"

    def _check_general(self):
        return self._general

    def _check_qq_connector(self):
        return self._qq


def attach_checker(system, checker):
    """给 system_manager 装一个能返回 checker 的 get_system（保留 systems 列表）。"""
    manager = system.system_manager

    def _get_system(name):
        return checker if name == "config_check" else None

    manager.get_system = _get_system


def test_auto_check_reports_problems(tmp_path):
    """重载成功但配置有问题 → 反馈里带问题清单。"""
    system, cfg, _ = make_system(str(tmp_path))
    attach_checker(system, FakeChecker(
        general={"ok": False, "lines": ["admin_ids: 未配置 ⚠️ （建议至少填一个）"]},
        qq={"ok": False, "lines": ["official.appid: 未配置 ❌（官方直连必填）"]},
    ))
    with open(system.config.path, "w", encoding="utf-8") as f:
        f.write("GUGUBot:\n  command_prefix: '#'\nsystem:\n  echo:\n    enable: true\n")
    REPLIES.clear()
    r = loop.run_until_complete(system.process_broadcast_info(mk_info(True)))
    assert r is True and REPLIES
    text = REPLIES[0][0]["data"]["text"]
    assert "重载成功" in text
    assert "语义校验: 发现 2 处问题" in text, text
    assert "admin_ids" in text and "official.appid" in text
    print("PASS: 重载反馈自动附带语义校验问题清单 ✅")


def test_auto_check_all_pass(tmp_path):
    system, cfg, _ = make_system(str(tmp_path))
    attach_checker(system, FakeChecker())
    with open(system.config.path, "w", encoding="utf-8") as f:
        f.write("GUGUBot:\n  command_prefix: '#'\nsystem:\n  echo:\n    enable: true\n")
    REPLIES.clear()
    loop.run_until_complete(system.process_broadcast_info(mk_info(True)))
    text = REPLIES[0][0]["data"]["text"]
    assert "语义校验: 全部通过" in text, text
    print("PASS: 语义校验全部通过时反馈 ✅")


def test_auto_check_survives_checker_crash(tmp_path):
    """校验器内部异常不影响重载结果。"""
    system, cfg, _ = make_system(str(tmp_path))

    class CrashChecker(FakeChecker):
        def _check_general(self):
            raise RuntimeError("boom")

    attach_checker(system, CrashChecker())
    with open(system.config.path, "w", encoding="utf-8") as f:
        f.write("GUGUBot:\n  command_prefix: '#'\nsystem:\n  echo:\n    enable: true\n")
    REPLIES.clear()
    r = loop.run_until_complete(system.process_broadcast_info(mk_info(True)))
    assert r is True
    text = REPLIES[0][0]["data"]["text"]
    assert "重载成功" in text and "语义校验" not in text, text
    print("PASS: 校验器崩溃不影响重载 ✅")


def test_auto_check_skipped_without_checker(tmp_path):
    """config_check 未注册时跳过校验（不报错）。"""
    system, cfg, _ = make_system(str(tmp_path))
    with open(system.config.path, "w", encoding="utf-8") as f:
        f.write("GUGUBot:\n  command_prefix: '#'\nsystem:\n  echo:\n    enable: true\n")
    REPLIES.clear()
    r = loop.run_until_complete(system.process_broadcast_info(mk_info(True)))
    assert r is True
    assert "语义校验" not in REPLIES[0][0]["data"]["text"]
    print("PASS: 无校验器时跳过 ✅")


# ---------------------------------------------------------------- 连接器热重载
def test_connector_enable_switches_refreshed(tmp_path):
    """连接器把 enable/enable_receive/enable_send 缓存成构造期属性，
    热重载后必须刷新，否则在配置里关掉连接器不生效。"""
    system, cfg, cfg_path = make_system(str(tmp_path))

    from gugubot.connector.basic_connector import BasicConnector

    class FakeConn(BasicConnector):
        async def connect(self): pass
        async def disconnect(self): pass
        async def send_message(self, processed_info): pass
        async def on_message(self, raw): pass

    conn = FakeConn(source="QQ", config=cfg)
    conn.enable = True
    conn.enable_receive = True
    conn.enable_send = True

    class CM:
        connectors = [conn]

    system._connector_manager = CM()

    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(
            "GUGUBot:\n  command_prefix: '#'\n"
            "connector:\n  QQ:\n    enable: true\n"
            "    enable_receive: false\n"
            "    enable_send: false\n"
            "system:\n  echo:\n    enable: true\n"
        )
    REPLIES.clear()
    r = loop.run_until_complete(system.process_broadcast_info(mk_info(True)))
    assert r is True and REPLIES
    text = REPLIES[0][0]["data"]["text"]
    assert conn.enable_receive is False, conn.enable_receive
    assert conn.enable_send is False, conn.enable_send
    assert "连接器 QQ 已重新加载" in text, text
    print("PASS: 连接器收发开关经热重载刷新 ✅")


def test_official_connector_reregisters_groups(tmp_path):
    """官方直连连接器热重载后应重新登记监听群（加群免重启）。"""
    system, cfg, cfg_path = make_system(str(tmp_path))

    from gugubot.connector.qq_official.connector import QQOfficialConnector

    class FakeMapper:
        def __init__(self):
            self.registered = None

        def register_legacy_groups(self, ids):
            self.registered = list(ids or [])

    conn = QQOfficialConnector.__new__(QQOfficialConnector)
    conn.config = cfg
    conn.logger = FL()
    conn.source = "QQ"
    conn.enable = True
    conn.enable_receive = True
    conn.enable_send = True
    conn.id_mapper = FakeMapper()

    class CM:
        connectors = [conn]

    system._connector_manager = CM()

    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(
            "GUGUBot:\n  command_prefix: '#'\n"
            "connector:\n  QQ:\n    permissions:\n"
            "      group_ids: ['100000000001']\n"
            "      forward_group_ids: ['200000000002']\n"
            "system:\n  echo:\n    enable: true\n"
        )
    REPLIES.clear()
    r = loop.run_until_complete(system.process_broadcast_info(mk_info(True)))
    assert r is True and REPLIES
    registered = conn.id_mapper.registered
    assert registered is not None, "监听群未重新登记"
    assert "100000000001" in registered, registered
    assert "200000000002" in registered, registered
    text = REPLIES[0][0]["data"]["text"]
    assert "监听群已重新登记" in text, text
    print("PASS: 官方直连监听群经热重载重新登记 ✅")


def test_connector_reload_failure_is_isolated(tmp_path):
    """某个连接器 reload_config 抛异常不影响重载结果。"""
    system, cfg, cfg_path = make_system(str(tmp_path))

    class BadConn:
        source = "QQ"

        def reload_config(self):
            raise RuntimeError("boom")

    class CM:
        connectors = [BadConn()]

    system._connector_manager = CM()
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("GUGUBot:\n  command_prefix: '#'\nsystem:\n  echo:\n    enable: true\n")
    REPLIES.clear()
    r = loop.run_until_complete(system.process_broadcast_info(mk_info(True)))
    assert r is True
    text = REPLIES[0][0]["data"]["text"]
    assert "重载成功" in text
    assert "连接器 QQ 重载失败" in text, text
    print("PASS: 连接器重载异常被隔离并提示 ✅")
