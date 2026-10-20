# -*- coding: utf-8 -*-
"""general_help 的模板参数集一致性测试。

背景：管理员分支用 admin_help_msg、非管理员分支用 help_msg——两个不同
的 key。但插件侧给两个分支传的占位符参数集**必须一致**，否则自定义
语言/风格模板在非管理员路径下引用某个占位符时会因缺参数而失败
（str.format 抛 KeyError → 用户收到异常而不是帮助文本）。

修复：非管理员分支补齐 official_status / ban_words / bound_notice /
whitelist / active_whitelist / startup_command / execute / execute_execute /
execute_mcdr / unbound_check / inactive_check。

本测试直接比对两个分支实际传入的 kwargs 键集合。
"""
import sys
import pathlib, types, asyncio, importlib.abc, importlib.machinery


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
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'plugin'))

from gugubot.logic.system.general_help import GeneralHelpSystem
from gugubot.utils.types import BroadcastInfo, Source

loop = asyncio.new_event_loop()


class FL:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


CAPTURED = []


def _make_help(is_admin):
    hs = GeneralHelpSystem.__new__(GeneralHelpSystem)
    hs.logger = FL()

    class FC(dict):
        def get_keys(self, key, default=None):
            r = self
            for k in key[:-1]:
                r = r.get(k, {})
            return r.get(key[-1], default)

    hs.config = FC()
    hs.config["GUGUBot"] = {"command_prefix": "#"}

    class FakeServer:
        def tr(self, key, **kwargs):
            # 系统名类的翻译：直接回显 key 末段
            return key.split(".")[-1]

    class CM:
        async def broadcast_processed_info(self, info, exclude=None, include=None):
            CAPTURED.append(info)

    hs.system_manager = types.SimpleNamespace(
        connector_manager=CM(), server=FakeServer()
    )
    hs.server = FakeServer()   # BasicSystem.server 代理到 system_manager.server

    def get_tr(self, key, global_key=False, **kwargs):
        if key in ("help_msg", "admin_help_msg"):
            CAPTURED.append(("TR", key, set(kwargs.keys())))
            return "帮助文本"
        if key == "help_command":
            return "帮助"
        # 其余翻译直接回显 key 末段
        return key.split(".")[-1]

    hs.get_tr = get_tr.__get__(hs, GeneralHelpSystem)
    return hs


def _info(is_admin):
    return BroadcastInfo(
        event_type="message",
        event_sub_type="group",
        message=[{"type": "text", "data": {"text": "#帮助"}}],
        raw=None,
        _source=Source("QQ"),
        source_id="200000000001",
        sender="测试",
        sender_id="100000000001",
        is_admin=is_admin,
    )


def _run(is_admin):
    CAPTURED.clear()
    hs = _make_help(is_admin)
    loop.run_until_complete(hs.process_broadcast_info(_info(is_admin)))
    trs = [c for c in CAPTURED if isinstance(c, tuple) and c[0] == "TR"]
    assert trs, f"应调用 get_tr，实际 {CAPTURED}"
    return trs[-1]


def test_admin_and_user_param_sets_identical():
    """管理员与非管理员分支传入的占位符集合必须完全一致。"""
    admin = _run(True)
    user = _run(False)
    assert admin[1] == "admin_help_msg", admin
    assert user[1] == "help_msg", user
    missing = admin[2] - user[2]
    assert not missing, f"非管理员分支缺少占位符参数: {sorted(missing)}"
    extra = user[2] - admin[2]
    assert not extra, f"非管理员分支多出占位符参数: {sorted(extra)}"
    print(f"PASS: 两个分支传参集一致（{len(user[2])} 个占位符）✅")


def test_official_status_placeholder_present_for_non_admin():
    """自定义模板常引用 {official_status}，非管理员分支也必须能拿到。"""
    user = _run(False)
    assert "official_status" in user[2], user[2]
    assert "whoami" in user[2], user[2]
    print("PASS: 非管理员分支含 official_status / whoami 占位符 ✅")


def test_system_name_placeholders_present_for_non_admin():
    """其余系统名占位符在非管理员分支也应齐备。"""
    user = _run(False)
    expected = {
        "ban_words", "bound_notice", "whitelist", "active_whitelist",
        "startup_command", "execute", "execute_execute", "execute_mcdr",
        "unbound_check", "inactive_check",
    }
    missing = expected - user[2]
    assert not missing, f"缺少占位符: {sorted(missing)}"
    print("PASS: 非管理员分支含全部系统名占位符 ✅")
