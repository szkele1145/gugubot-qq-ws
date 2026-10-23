# -*- coding: utf-8 -*-
"""QQ 昵称里的 § 颜色码不得注入 MC 聊天（v2.2.10 续，94）。

问题
----
``mc_connector._inject_system_chat``（native_chat 路径：用 ``/tellraw``
把 QQ 消息送进 MC 的系统聊天通道）里，正文已经过
``segments_to_plain_text`` → 内部会调 ``strip_minecraft_color_codes``，
**但发送者昵称没有**：

.. code-block:: python

    display_name = (sender or "QQ用户").strip()
    display_name = "".join(ch for ch in display_name if ch.isprintable()) or "QQ用户"
    head = f"[QQ转发]{display_name}:"

``§`` **是可打印字符**，``isprintable()`` 为真，于是原样进入 tellraw 的
JSON 文本。一个用户把 QQ 群昵称改成 ``§c§l管理员`` 就能在 MC 里让这行
消息显示成红色粗体的「[QQ转发]管理员:」—— **伪造格式、冒充管理/系统
发言**。

同类能力在另一条发送路径上是被挡住的：``qq_official/connector.py`` 调
``_strip_color_codes_from_message``，``gugubot/utils/message.py`` 也有
公共的 ``strip_minecraft_color_codes``。只有 native_chat 这条漏了昵称。

修复
----
昵称也过一遍 ``strip_minecraft_color_codes``（复用已有公共函数，不再新增
第五份实现 —— 项目里这个正则已经重复了四份）。
"""
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

from gugubot.utils.message import strip_minecraft_color_codes  # noqa: E402

_MC_CONN = _ROOT / "plugin" / "gugubot" / "connector" / "mc_connector.py"


# ==================================================== 1. 公共函数本身
def test_helper_strips_all_format_codes():
    assert strip_minecraft_color_codes("§c红色§r正常") == "红色正常"
    assert strip_minecraft_color_codes("§l§o粗斜§r") == "粗斜"
    assert strip_minecraft_color_codes("§k乱码") == "乱码"
    # 大小写都要认（§A / §F 在部分客户端等价）
    assert strip_minecraft_color_codes("§A大写§F") == "大写"
    # 不是颜色码的保留
    assert strip_minecraft_color_codes("价格§x100") == "价格§x100"


# ==================================================== 2. 昵称必须脱敏
def test_native_chat_sanitizes_display_name():
    """native_chat 路径构造昵称时必须剥颜色码。

    用源码断言而非端到端（端到端需要 mod + 服务器），但断言的是**关键
    机制**：昵称经过 ``strip_minecraft_color_codes``，与正文走同一套
    脱敏 —— 而不是只看「有没有 import」。
    """
    src = _MC_CONN.read_text(encoding="utf-8-sig")

    # 找到 native_chat 里 display_name 的赋值块
    idx = src.find("display_name = (sender or")
    assert idx != -1, "找不到 native_chat 的 display_name 赋值，测试需更新"
    block = src[idx: idx + 800]

    assert "strip_minecraft_color_codes" in block, (
        "native_chat 的 display_name 没有剥 MC 颜色码 —— 用户把 QQ 昵称改成 "
        "§c§l管理员 就能在 MC 里伪造红色粗体格式、冒充管理发言"
    )


def test_native_chat_still_keeps_control_char_filter():
    """原有「剔除非可打印字符」的过滤不能被删掉。"""
    src = _MC_CONN.read_text(encoding="utf-8-sig")
    idx = src.find("display_name = (sender or")
    block = src[idx: idx + 800]
    assert "isprintable" in block, (
        "display_name 的 isprintable 过滤被移除了 —— 控制字符会破坏命令解析"
    )


# ==================================================== 3. 正文仍然脱敏
def test_plain_text_still_stripped():
    from gugubot.utils.message import segments_to_plain_text

    segs = [{"type": "text", "data": {"text": "§c正文§r"}}]
    assert segments_to_plain_text(segs) == "正文"
    assert segments_to_plain_text("§a字符串正文") == "字符串正文"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
