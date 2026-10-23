# -*- coding: utf-8 -*-
"""一个系统初始化失败不能拖垮其余系统（v2.2.10 续，81）。

问题 A：装配循环没有隔离
------------------------
``__init__.py`` 组装系统时是裸循环：

.. code-block:: python

    for system in systems:
        system_manager.register_system(system)      # ← 任一失败即中断整轮

而 ``register_system`` 失败时会**记日志后重新抛出**。于是：

* 某一个系统的 ``initialize()`` 抛异常（``players.json`` 损坏、白名单插件
  行为异常、某个第三方库缺失……）→ **循环中断** → 排在它**后面**的所有
  系统都没被注册；
* 异常继续冒泡出 ``on_load`` → 函数**剩余部分全不执行**（帮助消息注册、
  版本检查等）。

用户侧表现：机器人"能跑但缺功能"，而且日志里只有一条
「初始化 X 失败」，完全看不出"后面的系统全没了"。这与 #78 / #80
是同一类问题：**一个部件的失败把其他部件一起带走**。

问题 B：``register_system`` 的 ``after`` 排序失效（潜伏）
--------------------------------------------------------
.. code-block:: python

    for i, existing_system in enumerate(self.systems):
        if before and existing_system.name in before:
            break
        if after and existing_system.name in after:
            insert_pos = i + 1
        insert_pos = i + 1          # ← 无条件覆盖，把 after 的结果冲掉

最后那句无条件赋值让 ``after`` 分支形同虚设：只要锚点后面还有别的系统，
``insert_pos`` 就会被一路推到最后 → 新系统被追加到**末尾**，而不是紧跟
锚点。``before`` 分支因为 ``break`` 得早，反而正常。

（当前生产代码调用 ``register_system(system)`` 时不传 ``before``/``after``，
所以这是**潜伏**缺陷，不是线上故障。修它是因为它是公开 API 的逻辑错误，
而且改动只有一行。）

修复
----
* 新增 ``SystemManager.register_systems()``：批量注册，**单个失败只跳过
  它自己**，返回 ``{系统名: 异常}``；``__init__.py`` 改走它并汇总报告。
* ``register_system`` 去掉那句无条件 ``insert_pos = i + 1``。
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

from gugubot.logic.system.system_manager import SystemManager  # noqa: E402


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


class _Sys:
    """最小系统桩：只提供 register_system / register_systems 需要的属性。"""

    def __init__(self, name, fail=False):
        self.name = name
        self.fail = fail
        self.initialized = False
        self.system_manager = None
        self.logger = None
        self.config = None

    def initialize(self):
        if self.fail:
            raise RuntimeError(f"{self.name} 初始化炸了")
        self.initialized = True


def _make_manager():
    class _Server:
        logger = _Logger()

    return SystemManager(_Server()), _Server.logger


# ==================================================== 1. 单个失败不影响其余
def test_register_systems_isolates_failures():
    """中间那个系统初始化失败时，**前后的系统都必须注册成功**。"""
    mgr, logger = _make_manager()
    a = _Sys("a")
    bad = _Sys("bad", fail=True)
    c = _Sys("c")

    failures = mgr.register_systems([a, bad, c])

    names = [s.name for s in mgr.systems]
    assert names == ["a", "c"], (
        f"注册结果 {names} —— 一个系统初始化失败把其余系统一起带走了"
        "（register_system 会重新抛出，裸循环因此中断）"
    )
    assert "bad" in failures, "失败信息没有返回给调用方"
    assert a.initialized and c.initialized
    assert "error" in [lvl for lvl, _ in logger.records], "失败时没有 error 日志"


def test_register_systems_all_ok():
    mgr, _ = _make_manager()
    sys_objs = [_Sys("a"), _Sys("b")]
    failures = mgr.register_systems(sys_objs)
    assert failures == {}
    assert [s.name for s in mgr.systems] == ["a", "b"]


# ==================================================== 2. 失败的系统不留残骸
def test_failed_system_not_registered():
    """初始化失败的系统**不能**留在 ``systems`` 里（否则配置同步会去碰它）。"""
    mgr, _ = _make_manager()
    bad = _Sys("bad", fail=True)
    mgr.register_systems([bad])
    assert mgr.get_system("bad") is None, (
        "初始化失败的系统仍留在列表里 —— 配置热重载 / 广播会继续访问它"
    )


# ==================================================== 3. after 排序必须生效
def test_register_system_after_places_immediately_after_anchor():
    """``after=["a"]`` 必须把新系统插在 a 的**紧后面**，而不是列表末尾。

    旧实现末尾有一句无条件 ``insert_pos = i + 1``，把 ``after`` 分支算出的
    位置一路冲掉 → 只要锚点后面还有系统，新系统就被追加到末尾。
    """
    mgr, _ = _make_manager()
    mgr.register_systems([_Sys("a"), _Sys("b")])

    x = _Sys("x")
    mgr.register_system(x, after=["a"])

    names = [s.name for s in mgr.systems]
    assert names == ["a", "x", "b"], (
        f"注册顺序 {names}，期望 ['a', 'x', 'b'] —— after 排序失效，"
        "新系统被追加到了末尾"
    )


def test_register_system_before_still_works():
    """``before`` 分支不能被修坏。"""
    mgr, _ = _make_manager()
    mgr.register_systems([_Sys("a"), _Sys("b")])

    x = _Sys("x")
    mgr.register_system(x, before=["b"])
    names = [s.name for s in mgr.systems]
    assert names == ["a", "x", "b"], f"注册顺序 {names}，期望 ['a', 'x', 'b']"


# ==================================================== 4. 装配处不得裸循环
def test_init_uses_batch_registration():
    """``__init__.py`` 的系统装配必须走 ``register_systems``（有隔离）。"""
    src = (
        _ROOT / "plugin" / "gugubot" / "__init__.py"
    ).read_text(encoding="utf-8")
    assert "register_systems(" in src, (
        "__init__.py 仍在裸循环里逐个 register_system —— 任一系统初始化"
        "失败会中断整轮注册，且异常冒泡出 on_load 让剩余启动步骤全不执行"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
