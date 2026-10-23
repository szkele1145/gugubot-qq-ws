# -*- coding: utf-8 -*-
"""附件缓存清理节流的状态应**按实例隔离**（v2.2.10 续，99）。

问题
----
``QQOfficialParser`` 把清理节流状态放在**类属性**里：

    _CACHE_CLEAN_STATE = {"last_check": 0.0}

所有实例共享同一时间戳 → 一个实例刚做完清理，其他实例的清理被
跳过 10 分钟。更麻烦的是测试间互相污染：``test_attachment_concurrent_download``
为了不让测试触发目录扫描，把
``QQOfficialParser._CACHE_CLEAN_STATE["last_check"] = 1e18`` 写到了
**全进程共享的类属性**里——如果这个测试跑在别的测试前面，后续的
``_cache_attachment`` 调用**永远**不会触发清理，LRU 失效。

这不是 bug 是 bug：**节流状态是实例级运行期状态，却放在类上**。

修复
----
把 ``last_check`` 移到实例属性 ``self._cache_last_check``；``__init__`` 里
初始化。``_cache_attachment`` 用实例属性。
"""
import importlib.abc
import importlib.machinery
import pathlib
import sys
import types
import time

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

from gugubot.connector.qq_official import parser as pp  # noqa: E402


def _make_parser(tmpdir):
    inst = pp.QQOfficialParser.__new__(pp.QQOfficialParser)
    inst.logger = types.SimpleNamespace(debug=lambda *a, **k: None,
                                          info=lambda *a, **k: None,
                                          warning=lambda *a, **k: None)
    inst.connector = types.SimpleNamespace(
        config=types.SimpleNamespace(get_keys=lambda path, default=None: default),
        server=types.SimpleNamespace(get_data_folder=lambda: str(tmpdir)),
    )
    # 调 __init__ 里初始化的那部分：手动设实例属性
    if not hasattr(inst, "_cache_last_check"):
        inst._cache_last_check = 0.0
    return inst


def test_cache_state_is_instance_attribute():
    """``_CACHE_CLEAN_STATE`` 类属性必须被移除（或至少不被使用）。"""
    assert not hasattr(pp.QQOfficialParser, "_CACHE_CLEAN_STATE"), (
        "清理节流状态仍放在类属性上 → 所有实例共享同一时间戳，"
        "并发/测试间互相污染。应移到实例属性 self._cache_last_check。"
    )


def test_two_instances_independent_throttle(monkeypatch, tmp_path):
    """两个实例的清理节流互不影响。"""
    inst_a = _make_parser(tmp_path)
    inst_b = _make_parser(tmp_path)

    # A 刚做过清理 → A 的节流在冷却期
    inst_a._cache_last_check = time.time()
    # B 没做过 → B 应当能立刻触发清理
    inst_b._cache_last_check = 0.0

    calls = {"n": 0}

    def fake_cleanup(cls, data_dir):
        calls["n"] += 1

    monkeypatch.setattr(pp.QQOfficialParser, "_cleanup_attachment_cache",
                        classmethod(fake_cleanup))

    # 触发一次 _cache_attachment 的开头（不真正下载）
    monkeypatch.setattr(inst_a, "_download_attachment", lambda *a, **k: False)
    monkeypatch.setattr(inst_b, "_download_attachment", lambda *a, **k: False)

    att_dir = tmp_path / "attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    # 用一个不存在的 url，让 _cache_attachment 走清理分支后再下载失败返回 None
    inst_a._cache_attachment("https://x/a.png", "a.png", "image/png")
    inst_b._cache_attachment("https://x/b.png", "b.png", "image/png")

    # B 应该至少触发过一次清理（A 不会，因为 A 在冷却期）
    assert calls["n"] >= 1, (
        "实例 B 的清理未被触发 —— 节流状态可能仍被 A 的冷却期污染"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
