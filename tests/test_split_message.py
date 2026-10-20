# -*- coding: utf-8 -*-
"""消息分片（_split_message）边界测试。

覆盖真实缺陷：
1. ``max_length <= 0``（用户把 connector.QQ.max_message_length 填 0/负数）
   会让分片循环**死循环**——space=0 → chunk 恒为空串 → text 不缩短，
   直接挂死 MCDR 事件循环（不是抛异常，无 traceback 可查）。
2. ``max_length`` 为非数字字符串时 ``int()`` 抛 ValueError。
"""
import sys
import pathlib, types, importlib.abc, importlib.machinery


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

from gugubot.connector.qq_official.connector import QQOfficialConnector
from gugubot.connector.qq_connector import QQWebSocketConnector


def _text(s):
    return [{"type": "text", "data": {"text": s}}]


def _official():
    return QQOfficialConnector.__new__(QQOfficialConnector)


def _onebot():
    return QQWebSocketConnector.__new__(QQWebSocketConnector)


# ---------------------------------------------------------------- 死循环防护
def test_official_zero_max_length_does_not_hang():
    """max_length=0：必须回退默认值，不得死循环。"""
    c = _official()
    # 若未防护，这里会无限循环（测试会超时）
    parts = c._split_message(_text("x" * 5000), max_length=0)
    assert parts, parts
    assert all(isinstance(p, list) for p in parts)
    # 文本全部保留，不得丢内容
    joined = "".join(
        seg["data"]["text"] for p in parts for seg in p if seg.get("type") == "text"
    )
    assert joined == "x" * 5000, len(joined)
    print("PASS: 官方连接器 max_length=0 不死循环且不丢内容 ✅")


def test_official_negative_max_length_does_not_hang():
    c = _official()
    parts = c._split_message(_text("hello"), max_length=-100)
    assert parts
    print("PASS: 官方连接器 max_length 负数不挂死 ✅")


def test_onebot_zero_max_length_does_not_hang():
    """OneBot 连接器有同样的 _split_message，同样需要防护。"""
    c = _onebot()
    parts = c._split_message(_text("y" * 3000), max_length=0)
    assert parts
    joined = "".join(
        seg["data"]["text"] for p in parts for seg in p if seg.get("type") == "text"
    )
    assert joined == "y" * 3000, len(joined)
    print("PASS: OneBot 连接器 max_length=0 不死循环且不丢内容 ✅")


def test_non_numeric_max_length_falls_back():
    """非数字字符串不应抛 ValueError。"""
    c = _official()
    parts = c._split_message(_text("z" * 100), max_length="不是数字")
    assert parts
    print("PASS: max_length 非数字 → 回退默认值不抛异常 ✅")


# ---------------------------------------------------------------- 正常分片行为不回归
def test_normal_split_preserves_content():
    c = _official()
    msg = _text("a" * 100) + [{"type": "text", "data": {"text": "b" * 100}}]
    parts = c._split_message(msg, max_length=60)
    assert len(parts) >= 2, parts
    joined = "".join(
        seg["data"]["text"] for p in parts for seg in p if seg.get("type") == "text"
    )
    assert joined == "a" * 100 + "b" * 100, joined
    print("PASS: 正常分片内容完整 ✅")


def test_short_message_not_split():
    c = _official()
    msg = _text("hello")
    parts = c._split_message(msg, max_length=2000)
    assert parts == [msg], parts
    print("PASS: 短消息不分片 ✅")


def test_non_text_item_kept():
    c = _official()
    msg = [{"type": "image", "data": {"url": "http://x"}}, {"type": "text", "data": {"text": "t"}}]
    parts = c._split_message(msg, max_length=2000)
    assert parts[0][0]["type"] == "image", parts
    print("PASS: 非文本段保留在首片 ✅")
