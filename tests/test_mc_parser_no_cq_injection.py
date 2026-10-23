# -*- coding: utf-8 -*-
"""MC 聊天内容里的 [CQ:...] 是字面文本，不是指令（v2.2.10 续，97）。

问题
----
``parser/mc_parser.py`` 解析玩家聊天时，把**原始聊天内容**直接丢给
``CQHandler.parse``：

.. code-block:: python

    broadcast_info = BroadcastInfo(
        ...
        message=CQHandler.parse(content),      # ← content 是玩家打的字
        raw=content,
        ...
    )

而 ``CQHandler.parse`` 会把 ``[CQ:at,qq=all]`` 这样的子串**解析成 CQ 段**。
于是任何能在 MC 里说话的人都可以打：

.. code-block:: text

    [CQ:at,qq=all] 都来看这个

这条消息转发到 QQ 群里就变成**真正的 @全体成员**。同理还能注入
``[CQ:image,file=...]``（往群里发任意图片 URL，可用于钓鱼）等。

**方向搞反了**：CQ 码是 QQ 侧的表示法，只应出现在**来自 QQ** 的消息里
（``qq_parser`` 解析它是对的）；MC 侧的玩家聊天是**纯文本**，没有 CQ 语义。

修复
----
MC 聊天一律构造**单个 text 段**，不再经过 CQ 解析。

测试手法
--------
``MCParser`` 需要 connector / logger，用 ``__new__`` 绕过后只装必需属性，
喂一条含 ``[CQ:at,qq=all]`` 的假聊天，断言解析结果里**没有 at 段**、
且文本原样保留。
"""
import asyncio
import importlib.abc
import importlib.machinery
import pathlib
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


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
sys.path.insert(0, str(_ROOT / "plugin"))

_MOD = "gugubot.parser.mc_parser"
sys.modules.pop(_MOD, None)
from gugubot.parser import mc_parser  # noqa: E402


class _Logger:
    def getattr(self, n):
        return lambda *a, **k: None

    def __getattr__(self, n):
        return lambda *a, **k: None


class _Raw:
    def __init__(self, player, content):
        self.player = player
        self.content = content


def _make_parser():
    inst = mc_parser.MCParser.__new__(mc_parser.MCParser)
    inst.logger = _Logger()
    inst.connector = types.SimpleNamespace(
        source="Minecraft",
        _is_admin=lambda player: _async_false(),
    )
    inst.config = types.SimpleNamespace(
        get_keys=lambda path, default=None: default,
        get=lambda key, default=None: default,
    )
    # 不用真实的忽略规则
    inst._ignore_message = lambda content: False
    return inst


async def _async_false():
    return False


def _run(coro):
    return asyncio.run(coro)


# ==================================================== 1. CQ 码不得被解析
def test_mc_chat_cq_code_stays_literal():
    """玩家打的 ``[CQ:at,qq=all]`` 必须保留为**文本**，不能变成 at 段。"""
    parser = _make_parser()
    info = _run(
        parser.parse(_Raw("Steve", "[CQ:at,qq=all] 都来看"), server=types.SimpleNamespace())
    )

    assert info is not None, "解析返回 None，测试前提不成立"
    types_ = [seg.get("type") for seg in info.message]
    assert "at" not in types_, (
        f"MC 聊天里的 [CQ:at,qq=all] 被解析成了 at 段: {info.message}\n"
        f"任何能说话的人都能借此在 QQ 群里 @全体成员"
    )
    joined = "".join(
        seg.get("data", {}).get("text", "")
        for seg in info.message
        if seg.get("type") == "text"
    )
    assert "[CQ:at,qq=all]" in joined, (
        f"文本内容被改动了，应原样保留: {info.message}"
    )


def test_mc_chat_image_cq_stays_literal():
    """``[CQ:image,file=...]`` 同理 —— 不能变成发图指令。"""
    parser = _make_parser()
    info = _run(
        parser.parse(
            _Raw("Steve", "[CQ:image,file=https://evil/x.png]"),
            server=types.SimpleNamespace(),
        )
    )
    types_ = [seg.get("type") for seg in info.message]
    assert "image" not in types_, (
        f"MC 聊天里的 [CQ:image,...] 被解析成了 image 段: {info.message}"
    )


# ==================================================== 2. 普通文本仍然正常
def test_mc_plain_chat_ok():
    parser = _make_parser()
    info = _run(parser.parse(_Raw("Steve", "你好 世界"), server=types.SimpleNamespace()))

    assert info is not None
    assert info.message == [{"type": "text", "data": {"text": "你好 世界"}}]
    assert info.sender == "Steve"
    assert info.raw == "你好 世界"


def test_mc_chat_with_brackets_ok():
    """带方括号但不是 CQ 码的文本也要原样保留。"""
    parser = _make_parser()
    info = _run(parser.parse(_Raw("Steve", "看这个 [图] 和 [1]"), server=types.SimpleNamespace()))
    joined = "".join(
        seg.get("data", {}).get("text", "") for seg in info.message
    )
    assert joined == "看这个 [图] 和 [1]"


# ==================================================== 3. QQ 侧仍然解析 CQ
def test_qq_side_still_parses_cq():
    """反向：来自 QQ 的消息**必须**继续解析 CQ 码（那是 QQ 侧的表示法）。"""
    from gugubot.builder import CQHandler

    segs = CQHandler.parse("[CQ:at,qq=10001] 你好")
    assert segs[0]["type"] == "at"
    assert segs[0]["data"]["qq"] == "10001"
    assert segs[1]["data"]["text"].strip() == "你好"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
