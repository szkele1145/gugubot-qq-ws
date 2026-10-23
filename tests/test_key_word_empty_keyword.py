# -*- coding: utf-8 -*-
"""空关键词会命中所有纯图片消息（v2.2.10 续，85）。

问题
----
``logic/system/key_word.py`` 的添加流程没有校验**关键词非空**：

.. code-block:: python

    # _handle_add（第 205-218 行）
    for i in [command_prefix, system_name, add_command]:
        command = command.replace(i, "", 1).strip()
    ...
    self.adding_request_dict[broadcast_info.sender_id] = command   # 可能是 ""

    # _handle_msg（第 104 行）
    if content in self:            # content 来自消息里的 text 段拼接

用户敲 ``#关键词 添加`` 但没写关键词 → ``command == ""`` → 进入等待状态
→ 下一条消息（比如一张图）被存成 ``self[""] = [image]``。

**后果**：之后**任何没有文字的消息**（纯图片 / 纯表情）``content == ""``
→ ``"" in self`` 成立 → 机器人把那张缓存图**当成关键词回复发出来**。
用户发一张图、机器人回一张图，看起来像幽灵回复，而且完全不知道是哪来的。

另外：即便用户没走这条路，旧数据里若已存在空键，行为同样如此。所以修
两处 —— 添加时**拒绝**空关键词，匹配时**跳过**空文本。

修复
----
1. ``_handle_add``：``command`` 为空时回一条提示并**不进入**等待状态；
2. ``_handle_msg``：``content`` 为空时直接跳过关键词匹配（顺带免疫旧
   数据里的空键）。
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

_MOD = "gugubot.logic.system.key_word"
sys.modules.pop(_MOD, None)
from gugubot.logic.system import key_word  # noqa: E402


class _Logger:
    def __init__(self):
        self.records = []

    def _add(self, level, msg, *a):
        try:
            text = msg % a if a else str(msg)
        except Exception:
            text = f"{msg} {a}"
        self.records.append((level, text))

    def debug(self, m, *a): self._add("debug", m, *a)
    def info(self, m, *a): self._add("info", m, *a)
    def warning(self, m, *a): self._add("warning", m, *a)
    def error(self, m, *a): self._add("error", m, *a)

    def text(self):
        return "\n".join(t for _, t in self.records)


class _Config:
    def __init__(self):
        self._data = {
            "GUGUBot": {"command_prefix": "#"},
            "system": {"key_words": {"max_add_time": 30}},
        }

    def get(self, key, default=None):
        return self._data.get(key, default)

    def get_keys(self, path, default=None):
        cur = self._data
        for p in path:
            if not isinstance(cur, dict) or p not in cur:
                return default
            cur = cur[p]
        return cur


class _BroadcastInfo:
    def __init__(self, text, sender_id="u1"):
        self.message = (
            [{"type": "text", "data": {"text": text}}] if text is not None else
            [{"type": "image", "data": {"url": "http://x/y.png"}}]
        )
        self.sender_id = sender_id
        self.event_type = "message"
        self.source = types.SimpleNamespace(origin="QQ", current="QQ")
        self.receiver_source = "QQ"
        self.receiver = None
        self.target = None
        self.event_sub_type = "group"
        self.source_id = "Group_1"
        self.raw = None
        self.server = None
        self.logger = None
        self.sender = "tester"


def _make_system():
    """绕过 __init__（KeyWordSystem 继承 BasicConfig(dict)，实例即空 dict）。"""
    inst = key_word.KeyWordSystem.__new__(key_word.KeyWordSystem)
    inst.name = "key_words"
    inst.enable = True
    inst.logger = _Logger()
    inst.config = _Config()
    inst.adding_request_dict = {}
    inst.replies = []

    async def _reply(bi, message):
        inst.replies.append(message)

    inst.reply = _reply
    # 关键：get_tr("name") / get_tr("add") 必须返回真实词，否则
    # _handle_add 里的 replace 循环剥不掉命令词，测不到真实行为
    _TR = {"name": "关键词", "add": "添加", "add_instruction": "请发送内容",
           "add_existed": "已存在", "add_timeout": "超时"}
    inst.get_tr = lambda key, **kw: _TR.get(key, f"<{key}>")
    # 阻断落盘（BasicConfig.__setitem__ 会 save()）
    inst.save = lambda: None
    inst.system_manager = types.SimpleNamespace(
        server=types.SimpleNamespace(
            tr=lambda k, **kw: f"<{k}>",
            schedule_task=lambda task: None,     # 不真的调度超时任务
        ),
        connector_manager=types.SimpleNamespace(
            get_connector=lambda name: None,     # 「来源连接器不存在」
            broadcast_processed_info=None,       # 各测试自行覆盖
        ),
        style_manager=None,
    )
    return inst


def _run(coro):
    return asyncio.run(coro)


# ==================================================== 1. 添加时拒绝空关键词
def test_add_rejects_empty_keyword():
    """``#关键词 添加``（不带关键词）不得进入等待状态。"""
    inst = _make_system()
    info = _BroadcastInfo("#关键词 添加")

    _run(inst._handle_add(info))

    assert inst.adding_request_dict == {}, (
        "空关键词被接受了 —— 用户接下来发的那条消息（很可能是一张图）"
        "会被存成 self[\"\"]，之后**所有没有文字的消息**都会命中它"
    )
    assert inst.replies, "拒绝时没有任何回复 —— 用户不知道自己做错了"


def test_add_accepts_normal_keyword():
    """正常关键词仍要能进入等待状态（不能把功能弄坏）。"""
    inst = _make_system()
    info = _BroadcastInfo("#关键词 添加 你好")

    _run(inst._handle_add(info))

    assert inst.adding_request_dict.get("u1") == "你好"


# ==================================================== 2. 匹配时跳过空文本
def test_msg_skips_empty_content(monkeypatch):
    """没有文字的消息不得命中关键词（即使旧数据里有空键）。"""
    inst = _make_system()
    # 直接写底层 dict，模拟旧版本留下的空键
    dict.__setitem__(inst, "", [{"type": "image", "data": {"file": "x.png"}}])

    sent = []

    async def _fake_broadcast(processed_info, **kwargs):
        sent.append(processed_info)
        return {}

    inst.system_manager.connector_manager.broadcast_processed_info = _fake_broadcast
    inst.is_command = lambda bi: False

    info = _BroadcastInfo(None)          # 纯图片消息，没有 text 段
    result = _run(inst._handle_msg(info))

    assert result is False, "空文本消息被当成关键词处理了"
    assert sent == [], (
        "纯图片消息命中了空关键词并触发了回复 —— 用户发一张图，机器人"
        "莫名其妙回一张图"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
