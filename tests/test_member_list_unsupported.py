# -*- coding: utf-8 -*-
"""「无法获取成员列表」与「没有成员」的区分测试。

真实缺陷：QQ 官方直连模式不提供群成员列表接口，旧实现统一返回空列表
``data: []``，调用方无法区分「这个群没有成员」和「这个平台查不到成员」。

后果（数据丢失级）：
- ``#检查多余绑定`` 恒报「全部绑定都不在群里」；
- ``#移除多余绑定`` 把**全部**绑定逐个删除、移出白名单、可能删掉整个 Player；
- 退群清理路径无条件删除用户绑定。

修复：官方 bot 返回 ``data: None`` + ``unsupported: True`` 明确表达
「无法获取」，bound 系统据此返回 ``None``（不可用）并跳过相关操作。
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

from gugubot.connector.qq_official.connector import QQOfficialBot
from gugubot.logic.system.bound import BoundSystem

loop = asyncio.new_event_loop()


class FL:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


# ---------------------------------------------------------------- 官方 bot 标记
def test_official_bot_marks_member_list_unsupported():
    """官方 bot 必须返回明确的「不支持」标记，而不是空列表。"""
    bot = QQOfficialBot.__new__(QQOfficialBot)
    result = loop.run_until_complete(bot.get_group_member_list(group_id="200000000001"))
    assert result is not None, result
    assert result.get("unsupported") is True, result
    # data 必须是 None（表示「查不到」），绝不能是 []（表示「没有成员」）
    assert result.get("data") is None, result
    print("PASS: 官方 bot 成员列表返回「不支持」标记而非空列表 ✅")


# ---------------------------------------------------------------- bound 系统
def _make_bound(bot, group_ids=("200000000001",), connector_ready=True, raise_exc=False):
    """构造一个只依赖 bot 的 BoundSystem（绕过完整初始化）。

    bot 为 None 时表示"连接器未就绪"；raise_exc=True 时成员列表查询抛异常。
    """
    bs = BoundSystem.__new__(BoundSystem)
    bs.logger = FL()

    class FC(dict):
        def get_keys(self, key, default=None):
            r = self
            for k in key[:-1]:
                r = r.get(k, {})
            return r.get(key[-1], default)

    bs.config = FC()
    bs.config["connector"] = {"QQ": {"permissions": {
        "group_ids": list(group_ids),
        "admin_group_ids": [],
    }}}

    class Bot:
        async def get_group_member_list(self, group_id, **kwargs):
            if raise_exc:
                raise RuntimeError("WebSocket disconnected")
            return bot

    class Conn:
        def __init__(self):
            self.bot = Bot() if connector_ready else None

    class CM:
        def get_connector(self, source):
            return Conn()

        async def broadcast_processed_info(self, info, include=None):
            pass

    class FakeServer:
        def tr(self, *a, **k):
            return "GuguBot"

    class SM:
        connector_manager = CM()
        server = FakeServer()

    bs.system_manager = SM()
    return bs


def test_member_list_unsupported_returns_none():
    """成员列表不可用时，_get_member_in_all_groups 必须返回 None（而非空集）。"""
    bs = _make_bound({"status": "ok", "retcode": 0, "data": None, "unsupported": True})
    result = loop.run_until_complete(bs._get_member_in_all_groups())
    assert result is None, result
    print("PASS: 成员列表不可用 → 返回 None（不是空集）✅")


def test_extra_bound_check_skipped_when_unsupported():
    """不可用时不得把全部绑定判为「多余」。"""
    bs = _make_bound({"status": "ok", "retcode": 0, "data": None, "unsupported": True})
    result = loop.run_until_complete(bs._get_extra_bound_member())
    assert result is None, result
    print("PASS: 多余绑定检查在成员列表不可用时返回 None ✅")


def test_empty_member_list_is_not_none():
    """真正的「空群」（data: []）必须仍返回空集，不能与「不可用」混淆。"""
    bs = _make_bound({"status": "ok", "retcode": 0, "data": []})
    result = loop.run_until_complete(bs._get_member_in_all_groups())
    assert result == set(), result
    print("PASS: 真空群返回空集（与「不可用」正确区分）✅")


def test_normal_member_list_collected():
    """正常成员列表照常收集。"""
    bs = _make_bound({"status": "ok", "retcode": 0, "data": [
        {"user_id": "100000000001"},
        {"user_id": "100000000002"},
    ]})
    result = loop.run_until_complete(bs._get_member_in_all_groups())
    assert result == {"100000000001", "100000000002"}, result
    print("PASS: 正常成员列表照常收集 ✅")


def test_lenient_mode_without_groups_returns_none():
    """宽松模式（未配置监听群）应返回 None，而不是空集。

    未配置监听群时机器人转发所有群的消息，根本无从得知有哪些群、
    成员是谁——当成空集会让「多余绑定检查」把全部绑定报为多余。
    """
    bs = _make_bound({"status": "ok", "data": []}, group_ids=())
    result = loop.run_until_complete(bs._get_member_in_all_groups())
    assert result is None, result
    print("PASS: 未配置监听群（宽松模式）→ 返回 None ✅")


def test_connector_not_ready_returns_none():
    """连接器未就绪时结果不可信，必须返回 None。"""
    bs = _make_bound(None, connector_ready=False)
    result = loop.run_until_complete(bs._get_member_in_all_groups())
    assert result is None, result
    print("PASS: 连接器未就绪 → 返回 None ✅")


def test_all_queries_failed_returns_none():
    """所有群查询都失败（如 WS 断连）时，返回 None 而非空集。

    否则每个失败群的成员都被当作「不在群里」，全部绑定会被误判。
    """
    bs = _make_bound(None, group_ids=("200000000001", "200000000002"),
                     raise_exc=True)
    result = loop.run_until_complete(bs._get_member_in_all_groups())
    assert result is None, result
    print("PASS: 所有群查询失败 → 返回 None ✅")


def test_partial_query_failure_still_returns_set():
    """部分群查询失败时仍返回已拿到的集合（不因个别失败全盘放弃）。"""
    bs = _make_bound({"status": "ok", "data": [{"user_id": "100000000001"}]},
                     group_ids=("200000000001",))
    # 追加一个必然抛异常的群：直接改配置
    bs.config["connector"]["QQ"]["permissions"]["group_ids"] = [
        "200000000001", "200000000002"
    ]
    orig = bs.system_manager.connector_manager.get_connector

    class CM2:
        def get_connector(self, source):
            c = orig(source)

            class Bot2:
                async def get_group_member_list(self, group_id, **kwargs):
                    if group_id == "200000000002":
                        raise RuntimeError("timeout")
                    return {"status": "ok", "data": [{"user_id": "100000000001"}]}
            c.bot = Bot2()
            return c

        async def broadcast_processed_info(self, info, include=None):
            pass

    bs.system_manager.connector_manager = CM2()
    result = loop.run_until_complete(bs._get_member_in_all_groups())
    assert result == {"100000000001"}, result
    print("PASS: 部分群查询失败仍返回已拿到的集合 ✅")


def test_remove_extra_bound_refuses_when_unsupported():
    """#移除多余绑定 在成员列表不可用时必须拒绝执行（不删任何绑定）。"""
    bs = _make_bound({"status": "ok", "retcode": 0, "data": None, "unsupported": True})

    REPLIES = []

    from gugubot.utils.types import BroadcastInfo, Source

    info = BroadcastInfo(
        event_type="message",
        event_sub_type="group",
        message=[{"type": "text", "data": {"text": "#移除多余绑定"}}],
        raw=None,
        _source=Source("QQ"),
        source_id="200000000001",
        sender="测试",
        sender_id="100000000001",
        is_admin=True,
    )

    # reply() 走 system_manager.connector_manager.broadcast_processed_info，
    # 从那里截获实际发出的文本
    class CapCM:
        def get_connector(self, source):
            return type("C", (), {
                "bot": type("B", (), {
                    "get_group_member_list": staticmethod(
                        lambda **kw: _async_return(
                            {"status": "ok", "retcode": 0, "data": None,
                             "unsupported": True}
                        )
                    ),
                })(),
            })()

        async def broadcast_processed_info(self, info, include=None):
            REPLIES.append(info.processed_message)

    async def _async_return(v):
        return v

    class FakeServer:
        def tr(self, *a, **k):
            return "GuguBot"

    class SM2:
        connector_manager = CapCM()
        server = FakeServer()

    bs.system_manager = SM2()

    r = loop.run_until_complete(bs._handle_remove_extra_bound(info))

    assert r is True, r
    assert REPLIES, "应给出明确提示"
    text = REPLIES[0][0]["data"]["text"]
    assert "不支持" in text, text
    assert "跳过" in text, text
    print("PASS: #移除多余绑定 在成员列表不可用时拒绝执行并提示 ✅")
