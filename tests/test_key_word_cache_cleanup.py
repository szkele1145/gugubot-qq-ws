# -*- coding: utf-8 -*-
"""删除关键词时必须清理它引用的缓存图片（v2.2.10 续，93）。

问题
----
``key_word.py`` 的 ``_handle_remove`` 把清理**挂在了配置开关上**：

.. code-block:: python

    cache_image = self.config.get_keys(
        ["system", "key_words", "cache_image"], False
    )
    if cache_image:
        self._cleanup_cached_images(self[command])
    del self[command]

于是这条路径会漏文件：

1. 用户在 ``cache_image: true`` 时添加了带图的关键词 → 图片被下载到
   ``key_words_images/``；
2. 之后用户在配置里把 ``cache_image`` 改成 ``false``；
3. 再删除那个关键词 → ``cache_image`` 为假 → **清理被跳过** →
   图片文件**永久留在磁盘上**（没有任何其他代码会清它：插件启动不扫
   缓存目录，``_cleanup_cached_images`` 只从这条路径调用）。

而清理本身与开关**无关**：它只删「该关键词引用、且确实位于
``image_cache_dir`` 内」的文件，没图就是空操作。开关在这里是多余的门槛。

修复
----
无条件调用 ``_cleanup_cached_images``（去掉 ``if cache_image:``）。

测试手法
--------
用 ``__new__`` 造实例（``KeyWordSystem`` 继承 ``BasicConfig(dict)``，
实例本身是空字典），在真实临时目录里放一个"缓存图片"文件，让关键词
引用它，然后走**真实的** ``_handle_remove``，断言文件被删。
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


class _Config:
    def __init__(self, cache_image: bool):
        self._data = {
            "GUGUBot": {"command_prefix": "#"},
            "system": {"key_words": {"cache_image": cache_image}},
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
    def __init__(self, text):
        self.message = [{"type": "text", "data": {"text": text}}]
        self.sender_id = "u1"
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


def _make_system(cache_dir: pathlib.Path, cache_image: bool):
    inst = key_word.KeyWordSystem.__new__(key_word.KeyWordSystem)
    inst.name = "key_words"
    inst.enable = True
    inst.logger = types.SimpleNamespace(
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    inst.config = _Config(cache_image)
    inst.image_cache_dir = cache_dir
    inst.adding_request_dict = {}
    inst.save = lambda: None

    replies = []

    async def _reply(bi, message):
        replies.append(message)

    inst.reply = _reply
    _TR = {"name": "关键词", "remove": "删除", "remove_success": "已删除",
           "remove_not_exist": "不存在"}
    inst.get_tr = lambda key, **kw: _TR.get(key, f"<{key}>")
    inst.system_manager = types.SimpleNamespace(
        server=types.SimpleNamespace(tr=lambda k, **kw: f"<{k}>"),
        connector_manager=types.SimpleNamespace(get_connector=lambda n: None),
        style_manager=None,
    )
    return inst, replies


def _run(coro):
    return asyncio.run(coro)


# ==================================================== 1. 开关关闭时也要清理
def test_remove_cleans_cache_even_when_flag_off(tmp_path):
    """``cache_image`` 已关闭时，删除关键词**仍要**清掉它引用的缓存图片。

    旧实现把清理挂在开关上 → 用户先缓存、后关开关、再删关键词 →
    图片永久残留（没有其他代码会清它）。
    """
    cache_dir = tmp_path / "key_words_images"
    cache_dir.mkdir()
    img = cache_dir / "abc123.png"
    img.write_bytes(b"\x89PNG-fake")

    inst, replies = _make_system(cache_dir, cache_image=False)
    # 直接写底层 dict，模拟"当年缓存了图片的关键词"
    dict.__setitem__(
        inst, "你好", [{"type": "image", "data": {"file": str(img)}}]
    )

    _run(inst._handle_remove(_BroadcastInfo("#关键词 删除 你好")))

    assert "你好" not in inst, "关键词没有被删除（测试前提）"
    assert not img.exists(), (
        "删除关键词后缓存图片仍然存在 —— cache_image 开关关闭时清理被跳过，"
        "而这些文件没有任何其他代码会清理（插件启动不扫缓存目录）"
    )
    assert replies, "删除后没有任何回复"


# ==================================================== 2. 开关开启时照常清理
def test_remove_cleans_cache_when_flag_on(tmp_path):
    cache_dir = tmp_path / "key_words_images"
    cache_dir.mkdir()
    img = cache_dir / "def456.png"
    img.write_bytes(b"\x89PNG-fake")

    inst, _ = _make_system(cache_dir, cache_image=True)
    dict.__setitem__(
        inst, "早", [{"type": "image", "data": {"file": str(img)}}]
    )

    _run(inst._handle_remove(_BroadcastInfo("#关键词 删除 早")))
    assert not img.exists()


# ==================================================== 3. 不删缓存目录外的文件
def test_remove_does_not_delete_files_outside_cache(tmp_path):
    """关键词若引用了缓存目录**之外**的文件，绝不能删（可能是用户自己的图）。"""
    cache_dir = tmp_path / "key_words_images"
    cache_dir.mkdir()
    outside = tmp_path / "user_own_picture.png"
    outside.write_bytes(b"\x89PNG-user")

    inst, _ = _make_system(cache_dir, cache_image=False)
    dict.__setitem__(
        inst, "外图", [{"type": "image", "data": {"file": str(outside)}}]
    )

    _run(inst._handle_remove(_BroadcastInfo("#关键词 删除 外图")))
    assert outside.exists(), (
        "删掉了缓存目录之外的文件 —— 那可能是用户自己的图片，"
        "清理必须限定在 image_cache_dir 内"
    )


# ==================================================== 4. 纯文字关键词不受影响
def test_remove_text_only_keyword(tmp_path):
    cache_dir = tmp_path / "key_words_images"
    cache_dir.mkdir()
    inst, replies = _make_system(cache_dir, cache_image=False)
    dict.__setitem__(inst, "纯文字", [{"type": "text", "data": {"text": "hi"}}])

    _run(inst._handle_remove(_BroadcastInfo("#关键词 删除 纯文字")))
    assert "纯文字" not in inst
    assert replies


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
