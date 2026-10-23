# -*- coding: utf-8 -*-
"""白名单 API 未就绪时的崩溃 + 解绑时名称删除口径不一致（v2.2.10 续，77）。

问题 A：``#白名单检查`` 在白名单 API 缺失时抛 AttributeError
--------------------------------------------------------------
``logic/system/whitelist.py`` 的 ``_api`` 是 ``Optional[object] = None``，
``initialize()`` 里取不到 ``whitelist_api`` 插件时就**保持 None**：

.. code-block:: python

    self._api = self.server.get_plugin_instance("whitelist_api")
    ...
    else:
        self.logger.warning("未找到白名单API插件")

``WhitelistSystem`` 自己的每个方法都做了 ``if not self._api: return False``
守卫（6 处）。但 ``bound.py`` 的 ``_get_unbound_whitelist()`` 只守了
``self.whitelist``（系统对象），**没守 ``_api``**：

.. code-block:: python

    for whitelist_player in self.whitelist._api.get_whitelist():   # ← None.get_whitelist()

→ 未安装 ``whitelist_api`` 时管理员执行 ``#绑定 白名单检查`` /
``移除未绑定白名单`` 会抛 ``AttributeError: 'NoneType' object has no
attribute 'get_whitelist'``，用户**看不到任何回复**（只有服务端日志里
一段 traceback）。而"没装 whitelist_api"正是最常见的部署形态之一 ——
它是声明依赖，但用户完全可以不装。

问题 B：解绑时「按 str 判断、按原值删除」
------------------------------------------
``_handle_unbind`` 里：

.. code-block:: python

    bedrock_names_str = [str(name) for name in player.bedrock_name]
    if player_name in bedrock_names_str:          # ← 按 str 比较
        ...
        player.bedrock_name.remove(player_name)   # ← 按原值删除

两个比较口径不一致。列表里只要有一个非 ``str`` 条目（旧版本数据、手工
编辑过的 ``players.json``、YAML 把纯数字名解析成 int），**判断会通过、
删除会抛 ValueError** → 解绑命令整个失败。源码里那句注释
「转换为字符串进行比较」说明作者本来就预期存在非 str 条目。

修复
----
* ``_get_unbound_whitelist()`` 改成**三态**：返回 ``None`` 表示「白名单
  不可用」（与同文件 ``_get_extra_bound_member()`` 的既有约定一致），
  ``[]`` 才表示「确实没有未绑定成员」——两者绝不能混为一谈：把"不可用"
  当成"空"会让 ``移除未绑定白名单`` 报告"没有未绑定的成员"（假消息），
  更危险的是同类误判在 ``移除多余绑定`` 上会删光全部绑定。
* 两个 handler 各自先判 ``None`` 并回一条**明确**的中文提示。
* 解绑改为 ``_drop_name()``：统一按 ``str()`` 口径删除。
"""
import ast
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

BOUND_SRC = _ROOT / "plugin" / "gugubot" / "logic" / "system" / "bound.py"


def _bound_tree():
    return ast.parse(BOUND_SRC.read_text(encoding="utf-8"))


def _find_method(cls_name, method_name):
    tree = _bound_tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            for sub in node.body:
                if (
                    isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and sub.name == method_name
                ):
                    return sub
    raise AssertionError(f"找不到 {cls_name}.{method_name}")


# ==================================================== 1. 关键：_api 必须守卫
def test_get_unbound_whitelist_guards_api():
    """``_get_unbound_whitelist`` 必须先判 ``_api`` 再调用它。

    旧实现直接 ``self.whitelist._api.get_whitelist()`` ——
    未安装 ``whitelist_api`` 时 ``_api`` 是 None → AttributeError。
    """
    src = BOUND_SRC.read_text(encoding="utf-8")
    fn = _find_method("BoundSystem", "_get_unbound_whitelist")
    body = ast.get_source_segment(src, fn)

    assert "_api" in body, "方法里没提到 _api，说明行为已改变，请更新本测试"

    # 必须存在形如 `if not ..._api` 或 `..._api is None` 的守卫
    has_guard = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        seg = ast.get_source_segment(src, node) or ""
        if "_api" in seg:
            has_guard = True
            break
    assert has_guard, (
        "_get_unbound_whitelist 里没有针对 `_api` 的判空守卫 —— "
        "未安装 whitelist_api 插件时管理员执行 #白名单检查 会抛 "
        "AttributeError: 'NoneType' object has no attribute 'get_whitelist'，"
        "且用户看不到任何回复"
    )


def test_get_unbound_whitelist_returns_none_when_unavailable():
    """不可用时返回 ``None``（而不是空列表）—— 与同文件既有约定一致。

    ``_get_extra_bound_member()`` 就是用 ``None`` 表示「成员列表不可用」，
    调用方据此跳过。把"不可用"当"空"在删除类命令上是灾难。
    """
    src = BOUND_SRC.read_text(encoding="utf-8")
    fn = _find_method("BoundSystem", "_get_unbound_whitelist")
    body = ast.get_source_segment(src, fn)
    assert "return None" in body, (
        "白名单不可用时应 `return None`（三态），不能返回 [] —— "
        "返回 [] 会让「移除未绑定白名单」报告『没有未绑定的成员』这种假消息"
    )


def test_handlers_handle_none_result():
    """两个白名单 handler 都必须处理 ``None``（不可用）而不是当空列表。"""
    src = BOUND_SRC.read_text(encoding="utf-8")
    for name in ("_handle_check_whitelist", "_handle_remove_unbound_whitelist"):
        fn = _find_method("BoundSystem", name)
        body = ast.get_source_segment(src, fn)
        assert "is None" in body, (
            f"{name} 没有判 `result is None` —— 白名单 API 不可用时会"
            "走到「没有未绑定的成员」分支，给用户一条假消息"
        )


# ==================================================== 2. 名称删除口径一致
def test_drop_name_handles_non_str_entries():
    """``_drop_name`` 必须能删掉「字符串形式匹配但类型不是 str」的条目。

    这是 ``_handle_unbind`` 的真实故障：判断用 ``str(name)`` 比较、
    删除用 ``list.remove(原值)``。列表里存 int 时判断通过、删除抛
    ValueError → 解绑命令整个失败。
    """
    spec = importlib.util.spec_from_file_location(
        "_bound_under_test", BOUND_SRC
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_bound_under_test"] = mod
    spec.loader.exec_module(mod)

    BoundSystem = mod.BoundSystem
    drop = getattr(BoundSystem, "_drop_name", None)
    assert drop is not None, "BoundSystem 缺少 _drop_name 帮助方法"

    # 纯字符串
    names = ["Alice", "Bob"]
    assert drop(names, "Alice") is True
    assert names == ["Bob"]

    # 混入非 str（旧数据 / 手工编辑过的 players.json / YAML 把纯数字解析成 int）
    names = [12345, "Bob"]
    assert drop(names, "12345") is True, (
        "按字符串形式匹配的条目删不掉 —— 解绑会抛 ValueError"
    )
    assert names == ["Bob"]

    # 不存在 → 返回 False，不改动
    names = ["Alice"]
    assert drop(names, "Nobody") is False
    assert names == ["Alice"]


def test_unbind_uses_drop_name():
    """``_unbind_specific_player`` 不得再用 ``list.remove(原值)`` 删名称。"""
    src = BOUND_SRC.read_text(encoding="utf-8")
    fn = _find_method("BoundSystem", "_unbind_specific_player")
    body = ast.get_source_segment(src, fn)

    # 允许出现 "_drop_name("
    assert "_drop_name(" in body, (
        "_unbind_specific_player 没有改用 _drop_name —— 仍按原值删除，"
        "非 str 条目会让解绑抛 ValueError"
    )
    # 旧写法：直接对 dataclass 列表调用 remove
    for bad in (".bedrock_name.remove(", ".java_name.remove("):
        assert bad not in body, (
            f"_unbind_specific_player 里仍有 `{bad}` —— 按原值删除与 str "
            "口径的存在性判断不一致"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
