# -*- coding: utf-8 -*-
"""reply()/reply_to_source() 的 target key 路由测试。

真实遗漏：两处都用 ``source_id.isdigit()`` 决定 source_id 能否直接作为
回复的 target key。QQ 官方直连支持在配置里**直接填写 openid 原文**
（32 位十六进制，非数字），此时 source_id 非数字 → 回退到 origin_source
（即连接器名 "QQ"）→ 命令回复发不出去。

修复：改为「非空即可路由」，把判定收进 _is_routable_source_id()。
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

from gugubot.logic.system.basic_system import BasicSystem
from gugubot.utils.types import BroadcastInfo, Source

loop = asyncio.new_event_loop()


class FL:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


SENT = []


class FakeCM:
    async def broadcast_processed_info(self, info, include=None):
        SENT.append((info, include))


class FakeServer:
    def tr(self, *a, **k):
        return "GuguBot"


def _make_system():
    bs = BasicSystem.__new__(BasicSystem)
    bs.logger = FL()

    class FC(dict):
        def get_keys(self, key, default=None):
            r = self
            for k in key[:-1]:
                r = r.get(k, {})
            return r.get(key[-1], default)

    bs.config = FC()
    bs.config["connector"] = {"minecraft_bridge": {"source_name": "Bridge"}}
    bs.system_manager = types.SimpleNamespace(
        connector_manager=FakeCM(), server=FakeServer()
    )
    return bs


def _make_info(source_id):
    return BroadcastInfo(
        event_type="message",
        event_sub_type="group",
        message=[{"type": "text", "data": {"text": "hi"}}],
        raw=None,
        _source=Source("QQ"),
        source_id=source_id,
        sender="测试",
        sender_id="100000000001",
        is_admin=False,
    )


def test_routable_accepts_virtual_id():
    """虚拟号（纯数字）可路由。"""
    bs = _make_system()
    assert bs._is_routable_source_id("200000000001") is True
    print("PASS: 虚拟号可路由 ✅")


def test_routable_accepts_openid_string():
    """官方直连直填的 openid 原串（非数字）也必须可路由。

    这是本修复的核心：旧逻辑用 isdigit() 会把它判为不可路由，
    回复于是回退到连接器名 "QQ"，用户收不到任何回应。
    """
    bs = _make_system()
    assert bs._is_routable_source_id("aabbccddeeff00112233445566778899") is True
    print("PASS: openid 原串可路由 ✅")


def test_routable_rejects_empty():
    """空 source_id 不可路由（回退到 origin）。"""
    bs = _make_system()
    assert bs._is_routable_source_id("") is False
    assert bs._is_routable_source_id(None) is False
    print("PASS: 空 source_id 不可路由 ✅")


def test_reply_target_uses_openid_source_id():
    """reply() 的 target key 应使用 openid 原串，而不是退化成 "QQ"。"""
    SENT.clear()
    bs = _make_system()
    oid = "aabbccddeeff00112233445566778899"
    loop.run_until_complete(bs.reply(_make_info(oid), [{"type": "text", "data": {"text": "ok"}}]))

    assert SENT, "应发出消息"
    info, include = SENT[0]
    assert oid in info.target, f"target 应为 {oid}，实际 {info.target}"
    print("PASS: reply() target 使用 openid 原串 ✅")


def test_reply_target_falls_back_when_empty():
    """source_id 为空时回退到 origin。"""
    SENT.clear()
    bs = _make_system()
    loop.run_until_complete(bs.reply(_make_info(""), [{"type": "text", "data": {"text": "ok"}}]))

    assert SENT, "应发出消息"
    info, include = SENT[0]
    assert "QQ" in info.target, f"应回退到 origin，实际 {info.target}"
    print("PASS: source_id 为空时回退到 origin ✅")
