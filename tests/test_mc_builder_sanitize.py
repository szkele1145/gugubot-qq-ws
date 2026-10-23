# -*- coding: utf-8 -*-
"""RText 路径也必须剥昵称里的 § 颜色码（v2.2.10 续，95）。

背景：这是 #94 的**对称缺口**
--------------------------------
#94 修的是 native_chat 路径（``/tellraw``）里昵称未脱敏。但同一个
``mc_connector.send_message`` 还有**默认路径**：

.. code-block:: python

    main_content = self.builder.build(
        rtext_content,
        group_name=source,
        group_id=source_id,
        sender=sender,          # ← QQ 昵称，原样传入
        sender_id=sender_id,
        receiver=receiver,
    )
    self.server.say(main_content)

而 ``McMessageBuilder.build`` 直接把这些字段塞进 RText：

.. code-block:: python

    rtext = RText(f"[{group_name}]", color=RColor.gold)
    rtext += RText(f" [{sender}]", color=RColor.green)      # ← 原样
    rtext += RText(f"[@{receiver}]", color=RColor.aqua)     # ← 原样

``RText`` 的文本最终由 MC 客户端渲染，``§`` 照样生效。也就是说
**#94 只堵了一条路，默认 path 仍然可以伪造「[QQ转发]管理员:」**。

（这正是《修复模式索引》§3 说的「修了一边没修对称的另一边」—— #80 也是
同一个教训。我修 #94 时没有检查另一条发送路径。）

修复
----
在**注入点**（``McMessageBuilder.build``）统一对 ``group_name`` /
``sender`` / ``receiver`` 剥颜色码 —— 而不是在调用方修，这样以后新增
调用点也自动受保护。

测试手法
--------
把模块里的 ``RText`` 换成记录器，捕获 ``build()`` 实际创建的所有文本，
断言其中不含 ``§``。这直接检验真实代码路径，不依赖 MCDR。
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

_MOD = "gugubot.builder.mc_builder"
sys.modules.pop(_MOD, None)
from gugubot.builder import mc_builder  # noqa: E402


class _Recorder:
    """记录所有被创建的 RText 文本。"""

    def __init__(self):
        self.texts = []

    def install(self, monkeypatch):
        recorder = self

        class _FakeRText:
            def __init__(self, text="", *a, **kw):
                self._text = str(text)
                recorder.texts.append(self._text)

            def __add__(self, other):
                return self

            def __radd__(self, other):
                return self

            def __iadd__(self, other):
                return self

            def set_hover_text(self, *a, **k):
                return self

            def set_click_event(self, *a, **k):
                return self

            def __str__(self):
                return self._text

        monkeypatch.setattr(mc_builder, "RText", _FakeRText)
        # RTextBase 判断：让 forward_content 走「非 RText」分支时也能构造
        monkeypatch.setattr(
            mc_builder, "RTextBase", type("RTextBase", (), {})
        )
        return recorder


def _joined(rec: _Recorder) -> str:
    return "\n".join(rec.texts)


# ==================================================== 1. 昵称必须脱敏
def test_build_strips_color_codes_from_sender(monkeypatch):
    rec = _Recorder().install(monkeypatch)

    mc_builder.McMessageBuilder.build(
        "正文",
        group_name="一群",
        group_id="200000000001",
        sender="§c§l管理员",
        sender_id="100000000001",
        receiver=None,
    )

    assert "§" not in _joined(rec), (
        f"RText 路径把昵称里的 § 原样放进去了\n实际文本: {rec.texts}\n"
        f"用户把 QQ 昵称改成 §c§l管理员 就能在默认路径里伪造红色粗体的"
        f"「[QQ转发]管理员:」—— #94 只堵了 native_chat 那条路"
    )
    assert "管理员" in _joined(rec), "脱敏不能把名字也删掉"


# ==================================================== 2. 群名 / 接收者同理
def test_build_strips_color_codes_from_group_and_receiver(monkeypatch):
    rec = _Recorder().install(monkeypatch)

    mc_builder.McMessageBuilder.build(
        "正文",
        group_name="§e攻击群",
        group_id=None,
        sender=None,
        sender_id=None,
        receiver="§4服主",
    )

    assert "§" not in _joined(rec), (
        f"群名 / 接收者里的 § 也原样进入了 RText: {rec.texts}"
    )
    assert "攻击群" in _joined(rec)
    assert "服主" in _joined(rec)


# ==================================================== 3. 正常内容不受影响
def test_build_keeps_normal_content(monkeypatch):
    rec = _Recorder().install(monkeypatch)

    mc_builder.McMessageBuilder.build(
        "你好世界",
        group_name="一群",
        group_id="123",
        sender="小明",
        sender_id="10001",
        receiver="小红",
    )

    joined = _joined(rec)
    assert "一群" in joined
    assert "小明" in joined
    assert "小红" in joined
    assert "你好世界" in joined


# ==================================================== 4. 非颜色码的 § 保留
def test_build_keeps_non_color_paragraph(monkeypatch):
    """``§x``（非合法格式码）不该被误删 —— 只在 § 后跟格式字符时才剥。"""
    rec = _Recorder().install(monkeypatch)

    mc_builder.McMessageBuilder.build(
        "正文",
        group_name="A§x群",
        group_id=None,
        sender=None,
        sender_id=None,
        receiver=None,
    )
    assert "A§x群" in _joined(rec), (
        f"误删了非格式码的 §（可能把正常文本改坏）: {rec.texts}"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
