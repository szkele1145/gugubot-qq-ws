# -*- coding: utf-8 -*-
"""官方发送链路的 at 段必须还原成 ``<@!openid>``（v2.2.10 续）。

背景
----
收发**不对称**：

- 接收端：官方 ``<@!openid>`` → 展开成 at 段，其中 ``qq`` 字段存的是
  **虚拟用户号**（见 ``parser._expand_content_segments``，用
  ``id_mapper.to_legacy_user`` 翻译）；
- 发送端：at 段 → ``segments_to_plain_text`` 统一转成纯文本
  ``@100000000001`` → 群里显示一串**无意义数字**，根本 @ 不到人。

官方的 @ 语法是 ``<@!openid>``，因此发送前必须把虚拟号翻回 openid。

这个 bug 的可见后果不止「@ 不到人」：``bound_notice.py`` 的
「未绑定玩家提醒」正是用 ``MessageBuilder.at(sender_id)`` @ 对方的
—— 官方模式下这条提醒 @ 的是一串数字，玩家收不到 @ 通知。
"""
import sys
import types
import pathlib
import importlib.abc
import importlib.machinery

import pytest


class _A:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return _A()
    def __call__(self, *a, **k): return _A()


class _SM(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        v = _A()
        setattr(self, name, v)
        return v


class _L(importlib.abc.Loader):
    def create_module(self, s): return _SM(s.name)
    def exec_module(self, m): pass


class _F:
    def find_spec(self, f, p=None, t=None):
        if f.startswith("mcdreforged") or f.startswith("packaging"):
            return importlib.machinery.ModuleSpec(f, _L(), is_package=True)


sys.meta_path.insert(0, _F())
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "plugin"))

from gugubot.connector.qq_official import connector as cm


USER_MAP = {
    "100000000001": "USEROPENID000000000000000001",
    "100000000002": "USEROPENID000000000000000002",
}


def _mk_bot():
    """构造一个只带 id_mapper 的 QQOfficialBot。"""
    mapper = types.SimpleNamespace(
        is_legacy_user=lambda lid: str(lid) in USER_MAP,
        to_official_user=lambda lid: USER_MAP.get(str(lid), str(lid)),
        to_official_group=lambda lid: str(lid),
    )
    bot = cm.QQOfficialBot.__new__(cm.QQOfficialBot)
    bot._c = types.SimpleNamespace(id_mapper=mapper)
    bot.logger = None
    return bot


def _at(qq):
    return {"type": "at", "data": {"qq": qq}}


def _text(t):
    return {"type": "text", "data": {"text": t}}


# ================================================ 1. 虚拟号 → openid
def test_known_virtual_user_becomes_official_mention():
    """已登记的虚拟用户号 → 官方 <@!openid>。"""
    bot = _mk_bot()
    out = bot._officialize_at_segments([_at("100000000001")])
    assert out == [{"type": "text", "data": {"text": "<@!USEROPENID000000000000000001>"}}], out


def test_unregistered_number_falls_back_to_plain_text():
    """未登记的数字号 → 降级纯文本 @xxx（不发无效标记给官方）。"""
    bot = _mk_bot()
    out = bot._officialize_at_segments([_at("999999999999")])
    assert out == [{"type": "text", "data": {"text": "@999999999999"}}], out


def test_raw_openid_is_used_directly():
    """at 段里本来就是 openid 原文（非纯数字）→ 直接用。"""
    bot = _mk_bot()
    out = bot._officialize_at_segments([_at("ABCDEF0123456789ABCDEF0123456789")])
    assert out == [
        {"type": "text", "data": {"text": "<@!ABCDEF0123456789ABCDEF0123456789>"}}
    ], out


# ================================================ 2. 其他段不受影响
def test_text_segments_untouched():
    """text 段原样保留。"""
    bot = _mk_bot()
    out = bot._officialize_at_segments([_text("你好")])
    assert out == [_text("你好")]


def test_non_at_segments_untouched():
    """非 at 段（图片等）原样保留。"""
    bot = _mk_bot()
    seg = {"type": "image", "data": {"file": "a.png"}}
    out = bot._officialize_at_segments([seg])
    assert out == [seg]


def test_mixed_segments_keep_order():
    """混合段保持原顺序。"""
    bot = _mk_bot()
    out = bot._officialize_at_segments([
        _text("前面"),
        _at("100000000001"),
        _text("后面"),
    ])
    assert len(out) == 3
    assert out[0] == _text("前面")
    assert out[2] == _text("后面")
    assert "<@!USEROPENID000000000000000001>" in out[1]["data"]["text"]


def test_empty_at_qq_kept_as_is():
    """qq 为空的 at 段不处理（保持原样）。"""
    bot = _mk_bot()
    seg = {"type": "at", "data": {"qq": ""}}
    out = bot._officialize_at_segments([seg])
    assert out == [seg]


def test_non_list_input_untouched():
    """非 list 输入原样返回。"""
    bot = _mk_bot()
    assert bot._officialize_at_segments("abc") == "abc"
    assert bot._officialize_at_segments(None) is None


# ================================================ 3. 端到端：最终 content
def test_plain_text_conversion_uses_mention_mark():
    """经 segments_to_plain_text 后，文本里是 <@!openid> 而非 @虚拟号。"""
    from gugubot.utils.message import segments_to_plain_text

    bot = _mk_bot()
    out = bot._officialize_at_segments([_at("100000000001"), _text(" 请注意")])
    text = segments_to_plain_text(out)
    assert text == "<@!USEROPENID000000000000000001> 请注意", text
    assert "100000000001" not in text, f"虚拟号不应出现在发给官方的内容里: {text}"


def test_receive_side_produces_legacy_qq():
    """接收端把官方 <@!openid> 转成 at 段时 qq 存的是虚拟号（既有行为）。

    这是本 bug 的另一半：收发两侧对「at 段的 qq 是什么」的约定必须
    一致，否则发送端无从翻译。
    """
    from gugubot.connector.qq_official import parser as pm

    mapper = types.SimpleNamespace(
        to_legacy_user=lambda oid: {
            "USEROPENID000000000000000001": "100000000001",
        }.get(oid, "100000000009"),
    )
    segs = pm.QQOfficialParser._expand_content_segments(
        pm.QQOfficialParser, "<@!USEROPENID000000000000000001> 你好", mapper, None, ""
    )
    at_segs = [s for s in segs if s.get("type") == "at"]
    assert len(at_segs) == 1, segs
    assert at_segs[0]["data"]["qq"] == "100000000001", at_segs


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
