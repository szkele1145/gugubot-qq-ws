# -*- coding: utf-8 -*-
"""QQIDMapper 群集合语义 + #状态 显示测试。

覆盖：

1. ``known_legacy_groups()`` 应包含**用户直接在配置里填 openid 原文**的群。
   这类群不占虚拟号槽位、也不走「待绑定」流程，之前完全不被计入 →
   ``#状态`` 显示的群数偏少，用户以为没配上。

2. ``#状态`` 应回显「直填 openid 群」——之前这类群在状态里毫无痕迹，
   用户配了却看不到任何提示。

3. ``known_group_openids()`` 与 ``known_legacy_groups()`` 的关系：
   前者按 openid 维度统计，后者按 legacy 号维度统计；两者都应覆盖
   直填 openid 的群。
"""
import sys, types, asyncio, importlib.abc, importlib.machinery


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

from gugubot.connector.qq_official.id_map import QQIDMapper
from gugubot.logic.system.official_status import OfficialStatusSystem
from gugubot.utils.types import BroadcastInfo, Source

loop = asyncio.new_event_loop()


class FL:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


EXPLICIT_OPENID = "aabbccddeeff00112233445566778899"
BOUND_OPENID = "bound_group_openid_0001"


def _make_mapper():
    m = QQIDMapper(data_file=None, logger=FL())
    # 配置里：真实群号 123456 + 直接填的 openid 原文
    m.register_legacy_groups(["123456", EXPLICIT_OPENID])
    # 某个群已自动绑到 123456
    m.bind_group_openid(BOUND_OPENID)
    # 另一个群只有虚拟号
    m.group_legacy_id("virtual_only_openid")
    return m


def test_known_legacy_groups_includes_explicit_openid():
    """直填 openid 的群必须计入已知群集合。"""
    m = _make_mapper()
    groups = m.known_legacy_groups()
    assert EXPLICIT_OPENID in groups, f"直填 openid 未计入: {groups}"
    assert "123456" in groups, f"真实绑定群号未计入: {groups}"
    assert any(g.startswith("2000000") for g in groups), f"虚拟群号未计入: {groups}"
    print(f"PASS: known_legacy_groups 含直填 openid（共 {len(groups)} 个）✅")


def test_known_group_openids_includes_explicit_openid():
    """openid 维度的集合也应覆盖直填 openid。"""
    m = _make_mapper()
    openids = m.known_group_openids()
    assert EXPLICIT_OPENID in openids, openids
    assert BOUND_OPENID in openids, openids
    print("PASS: known_group_openids 含直填 openid ✅")


def test_explicit_openid_does_not_consume_legacy_slot():
    """直填 openid 不应占用真实群号槽位（回归保护）。"""
    m = _make_mapper()
    # 123456 已被 BOUND_OPENID 占掉
    assert m.to_legacy_group(BOUND_OPENID) == "123456"
    # 直填的 openid 原样返回
    assert m.to_legacy_group(EXPLICIT_OPENID) == EXPLICIT_OPENID
    # 虚拟群号应分配新号，不复用 123456
    v = m.group_legacy_id("another_openid")
    assert v != "123456", v
    print("PASS: 直填 openid 不占用真实群号槽位 ✅")


# --------------------------------------------------------------------- #
# #状态 显示
# --------------------------------------------------------------------- #

REPLIED = []


def _make_status(connector):
    os_ = OfficialStatusSystem.__new__(OfficialStatusSystem)
    os_.logger = FL()
    os_.name = "official_status"
    os_.enable = True

    class FakeServer:
        def tr(self, key, **kwargs):
            return key

    class CM:
        def get_connector(self, name):
            return connector

    os_.system_manager = types.SimpleNamespace(
        connector_manager=CM(), server=FakeServer()
    )
    os_.server = FakeServer()

    async def reply_to_source(self, info, msgs):
        REPLIED.append(msgs)

    os_.reply_to_source = reply_to_source.__get__(os_, OfficialStatusSystem)
    return os_


def _info():
    return BroadcastInfo(
        event_type="message",
        event_sub_type="group",
        message=[{"type": "text", "data": {"text": "#状态"}}],
        raw=None,
        _source=Source("QQ"),
        source_id="200000000001",
        sender="测试",
        sender_id="100000000001",
        is_admin=True,
    )


def _run_status():
    REPLIED.clear()
    m = _make_mapper()

    class Conn:
        id_mapper = m
        ws_client = None
        token_manager = None
        reply_pool = None

    os_ = _make_status(Conn())
    loop.run_until_complete(os_._handle_status(_info(), Conn()))
    assert REPLIED, "应有回复"
    msgs = REPLIED[0]
    text = "".join(
        seg.get("data", {}).get("text", "")
        for seg in msgs
        if seg.get("type") == "text"
    )
    return text


def test_status_shows_explicit_openid_groups():
    """#状态 应回显「直填 openid 群」及其数量。"""
    text = _run_status()
    assert "直填 openid 群" in text, f"#状态 未显示直填 openid 群:\n{text}"
    assert EXPLICIT_OPENID[:12] in text, f"未显示具体 openid:\n{text}"
    print("PASS: #状态 回显直填 openid 群 ✅")


def test_status_group_count_includes_explicit():
    """#状态 的群数应把直填 openid 的群算进去。"""
    text = _run_status()
    # 123456(真实绑定) + 1 个虚拟群 + 1 个直填 openid = 3
    assert "3 个群" in text, f"群数应含直填 openid:\n{text}"
    print("PASS: #状态 群数含直填 openid ✅")
