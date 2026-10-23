# -*- coding: utf-8 -*-
"""保存失败时**绝不能把旧文件弄丢**（v2.2.10 续，76）。

问题
----
``config/basic_config.py`` 的 ``_install_tmp`` 降级链里有一级是
「**先删目标文件、再 rename**」：

.. code-block:: python

    try:
        self._atomic_replace(tmp_path, self.path)    # os.replace + 重试
        return
    except PermissionError:
        pass

    # 降级：先删目标再 rename
    if self.path.exists():
        os.remove(self.path)          # ← 目标没了
    os.replace(tmp_path, self.path)   # ← 如果这里也失败……

后一步一旦再失败（tmp 被杀软锁住、跨卷 rename、目标目录权限变化），
**旧配置已经删掉了，新内容又没写进去** → 配置文件**凭空消失**。

这个降级级本身的动机是「比重试到底失败强」，但它把「保存失败」
升级成了「数据丢失」——正好违背这个方法的注释所写的
「『保存失败』比『文件损坏』安全得多」。

同样的降级在 ``connector/qq_official/id_map.py`` 里也出现过，
已在同一次修复中删除（那里测试直接抓到了「数据文件消失」）。

修复
----
去掉「先删目标」这一级。占用是**瞬时**的，靠退避重试即可；重试
到底仍失败就抛错，**保留旧文件**由调用方重试或报错。
"""
import importlib.abc
import importlib.machinery
import json
import os
import pathlib
import sys
import types

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


# ==================================================== 1. 关键：失败必须保留旧文件
def test_save_failure_must_not_delete_existing_file(tmp_path, monkeypatch):
    """``os.replace`` 一直失败时，**旧配置文件必须还在**。

    旧实现的降级「先删目标再 rename」在 rename 也失败时会把文件删掉
    → 配置凭空消失（下次启动读到空/默认值，用户的全部设置没了）。
    """
    path = tmp_path / "config.json"
    cfg = BasicConfig(path=str(path), default_content={"n": 0})
    cfg["n"] = 1
    cfg.save()
    assert path.exists(), "初始保存失败，测试前提不成立"
    before = json.loads(path.read_text(encoding="utf-8"))

    # 让 os.replace 永远失败（模拟目标被占用 / 跨卷 / 杀软锁 tmp）
    def always_fail(src, dst):
        raise PermissionError("[WinError 32] 文件正被另一进程使用")

    monkeypatch.setattr(os, "replace", always_fail)

    # __setitem__ 会自动 save()，所以异常在这一行就抛出
    with pytest.raises(OSError):
        cfg["n"] = 2

    assert path.exists(), (
        "保存失败后配置文件**被删掉了** —— 降级链里的「先删目标再 "
        "rename」在 rename 也失败时会把旧数据弄丢，"
        "「保存失败」被升级成了「数据丢失」"
    )
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after == before, "保存失败后旧文件内容被改坏了"


# ==================================================== 2. 瞬时占用要能重试成功
def test_save_retries_transient_permission_error(tmp_path, monkeypatch):
    """``os.replace`` 第一次失败、第二次成功时，保存必须成功落盘。

    Windows 上「文件被读者短暂打开」是常态，退避重试就能过去。
    """
    path = tmp_path / "config.json"
    cfg = BasicConfig(path=str(path), default_content={"n": 0})

    real_replace = os.replace
    state = {"n": 0}

    def flaky_replace(src, dst):
        state["n"] += 1
        if state["n"] == 1:
            raise PermissionError("[WinError 32] 文件正被另一进程使用")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)

    cfg["n"] = 42
    cfg.save()

    assert state["n"] >= 2, (
        "os.replace 只被调用了一次 —— 没有重试，Windows 上遇到瞬时"
        "占用就会保存失败"
    )
    assert json.loads(path.read_text(encoding="utf-8"))["n"] == 42


# ==================================================== 3. 静态：不得删目标
def test_source_must_not_remove_target_before_rename():
    """源码里**不得**在 rename 之前删除目标配置文件。

    这是一条刻意的结构性断言：删目标这一步永远只会在「rename 也失败」
    时把数据一起带走，收益（比重试到底强一点）远小于风险。
    """
    src_path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "plugin" / "gugubot" / "config" / "basic_config.py"
    )
    src = src_path.read_text(encoding="utf-8")

    # 只看保存相关的实现（避免误伤别处合法的 unlink）
    start = src.find("def _install_tmp")
    end = src.find("def _atomic_replace")
    assert start != -1 and end != -1, "找不到 _install_tmp / _atomic_replace"
    body = src[start:end]

    assert "os.remove(self.path)" not in body, (
        "_install_tmp 里仍然有「先删目标文件再 rename」的降级 —— "
        "rename 再失败时旧配置会凭空消失"
    )
    assert "self.path.unlink()" not in body, (
        "_install_tmp 里仍然有「先删目标文件再 rename」的降级 —— "
        "rename 再失败时旧配置会凭空消失"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
