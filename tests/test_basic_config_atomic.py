# -*- coding: utf-8 -*-
"""BasicConfig 原子写测试（v2.2.10 新增）。

背景
----
``BasicConfig`` 是**所有**持久化系统的基类（玩家绑定、待办、违禁词、
关键词、白名单、未绑定/不活跃检查、启动命令……）。旧实现的 ``save()``
直接 ``open(path, "w")`` 覆盖写：进程在写入过程中崩溃 / 断电 / 磁盘
写满 → 文件停在半截 → **旧数据已被截断、无法恢复**，该系统的数据直接
损坏（待办、绑定关系全丢）。

现改为「先写 .tmp 再 os.replace」的原子写：同一文件系统上 ``os.replace``
是原子操作 → 要么旧文件完整保留、要么新文件完整生效，不会出现半截文件。
"""
import json
import os
import pathlib
import sys
import tempfile
import types
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

from gugubot.config.basic_config import BasicConfig


def test_save_writes_file():
    c = BasicConfig(path=str(pathlib.Path(tempfile.mkdtemp()) / "x.json"))
    c["a"] = 1
    assert c.path.is_file()
    assert json.loads(c.path.read_text(encoding="UTF-8"))["a"] == 1


def test_save_leaves_no_tmp_file():
    """正常保存后不应残留 .tmp 文件。"""
    d = pathlib.Path(tempfile.mkdtemp())
    c = BasicConfig(path=str(d / "x.json"))
    c["a"] = 1
    leftovers = [p.name for p in d.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], f"不应残留临时文件: {leftovers}"


def test_crash_mid_write_keeps_old_file_intact(monkeypatch):
    """模拟写入过程中崩溃：旧文件必须完好无损。"""
    d = pathlib.Path(tempfile.mkdtemp())
    c = BasicConfig(path=str(d / "x.json"))
    c["a"] = "旧值"

    def _boom(*a, **k):
        raise RuntimeError("模拟写入中途崩溃")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(RuntimeError):
        c["b"] = "新值"

    # 旧文件必须仍是完整的旧内容（而不是半截 / 空）
    assert c.path.is_file(), "旧文件不应被删除"
    data = json.loads(c.path.read_text(encoding="UTF-8"))
    assert data.get("a") == "旧值", "旧数据必须完好"
    assert "b" not in data, "崩溃时新数据不应被写入（半成品不得生效）"


def test_replace_is_used_for_atomicity(monkeypatch):
    """保存必须走 os.replace（原子替换），而不是直接覆盖写。"""
    d = pathlib.Path(tempfile.mkdtemp())
    c = BasicConfig(path=str(d / "x.json"))
    called = {}

    real_replace = os.replace

    def _spy(src, dst):
        called["src"] = str(src)
        called["dst"] = str(dst)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _spy)
    c["k"] = "v"

    assert called, "保存未调用 os.replace"
    assert called["src"].endswith(".tmp"), "应先写临时文件"
    assert called["dst"] == str(c.path), "临时文件应替换到目标路径"


def test_yaml_format_atomic_save():
    """YAML 格式同样走原子写。"""
    d = pathlib.Path(tempfile.mkdtemp())
    c = BasicConfig(path=str(d / "x.yml"), yaml_format=True)
    c["todos"] = {"1": {"content": "x"}}
    assert c.path.is_file()
    assert not [p for p in d.iterdir() if p.name.endswith(".tmp")]

    # 重新加载，内容应完整
    c2 = BasicConfig(path=str(d / "x.yml"), yaml_format=True)
    assert c2["todos"]["1"]["content"] == "x"


def test_overwrite_existing_file_is_atomic(monkeypatch):
    """覆盖已有文件时也是原子替换（旧文件不会先被截断）。"""
    d = pathlib.Path(tempfile.mkdtemp())
    p = d / "x.json"
    c = BasicConfig(path=str(p))
    c["a"] = 1
    original = p.read_text(encoding="UTF-8")

    seen = {}

    def _spy(src, dst):
        # 替换发生前，旧文件内容必须仍是完整的
        seen["old_at_replace"] = pathlib.Path(dst).read_text(encoding="UTF-8")
        return os.replace.__wrapped__(src, dst) if hasattr(os.replace, "__wrapped__") else _real(src, dst)

    _real = os.replace
    monkeypatch.setattr(os, "replace", _spy)
    c["b"] = 2

    assert seen["old_at_replace"] == original, "替换发生时旧文件应是完整内容"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
