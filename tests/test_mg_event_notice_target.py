# -*- coding: utf-8 -*-
"""mg_event 播报目标配置形态适配（v2.2.10 续，105）。

问题
----
``mg_event.py`` 的 ``_build_notice_target`` 原来是手写的列表推导：

.. code-block:: python

    groups = [g for g in (group_ids or []) if g]
    target = {str(g): "group" for g in groups}

它**没有走 ``utils.num.as_id_list``**。而 ``notice_forward_groups`` 是
用户可编辑的配置项（YAML 列表），用户很容易写成**标量**：

.. code-block:: yaml

    notice_forward_groups: 123456789

此时 ``for g in group_ids`` 会**逐字符遍历**这个字符串（``"1"``、
``"2"``……），造出 ``{"1": "group", "2": "group", ...}`` 这种假目标 ——
成就/死亡播报发到不存在的群，日志里一堆发送失败，却完全看不出是配置
写错了。同模块的 ``player_notice.py`` 早就用 ``as_id_list`` 防住了
同样的问题，这里是漏掉的一处（同一仓库里的同类缺陷已修过 N 次，
这次是同一个模式的又一个漏网点）。

修复
----
``_build_notice_target`` 改走 ``as_id_list``：``None``/空 → ``[]``；
标量 → 单元素列表；可迭代 → 逐项 ``str()`` 并去掉空值。与
``player_notice._build_notice_target`` 同源。

测试手法
--------
不 import 整个插件（MCDR 环境依赖重）：用 AST 断言函数体内必须出现
``as_id_list`` 调用；再把函数源码抠出来、注入假 ``as_id_list`` 后
``exec``，验证标量输入确实被归一化成单元素目标（而不是逐字符拆开）。
"""
import ast
import pathlib
import textwrap

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SRC = _ROOT / "plugin" / "gugubot" / "logic" / "plugins" / "mg_event.py"


def _get_func_source() -> str:
    """抠出 _build_notice_target 的完整源码（含装饰器则不含）。"""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_build_notice_target":
            # ast.get_source_segment 拿到的是函数体原文（含签名与冒号后内容）
            seg = ast.get_source_segment(_SRC.read_text(encoding="utf-8"), node)
            assert seg is not None
            return textwrap.dedent(seg)
    raise AssertionError("找不到 _build_notice_target")


def test_build_notice_target_uses_as_id_list():
    """函数体内必须调用 as_id_list（标量配置不能被逐字符遍历）。"""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_build_notice_target"
    )
    calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "as_id_list"
    ]
    assert calls, (
        "_build_notice_target 必须走 as_id_list 归一化，"
        "否则标量 notice_forward_groups 会被逐字符遍历成假目标"
    )


def test_scalar_group_id_is_normalized():
    """标量输入必须归一化成单元素列表，不能逐字符拆开。"""
    captured = {}

    def fake_as_id_list(value):
        captured["value"] = value
        if value is None:
            return []
        if isinstance(value, (str, bytes)):
            s = value.decode() if isinstance(value, bytes) else value
            s = s.strip()
            return [s] if s else []
        if isinstance(value, (int, float)):
            return [str(value)]
        try:
            items = list(value)
        except TypeError:
            return []
        out = []
        for item in items:
            if item is None:
                continue
            s = str(item).strip()
            if s:
                out.append(s)
        return out

    ns = {"as_id_list": fake_as_id_list, "Optional": object, "Dict": dict, "List": list}
    exec(compile(_get_func_source(), "<mg_event_test>", "exec"), ns)
    result = ns["_build_notice_target"]("123456789")
    assert captured["value"] == "123456789", (
        f"as_id_list 应收到原始标量 '123456789'，实际收到 {captured['value']!r}"
    )
    assert result == {"123456789": "group", "_": "group"}, (
        f"标量输入应归一化成单元素目标（含 '_' 防桥接单目标过滤），"
        f"实际得到 {result!r}"
    )


def test_none_and_empty_return_none():
    """None / 空列表输入应返回 None（不播报）。"""
    ns = {
        "as_id_list": lambda v: [] if v is None else (
            [str(x) for x in v] if not isinstance(v, (str, bytes)) else ([v] if v else [])
        ),
        "Optional": object, "Dict": dict, "List": list,
    }
    exec(compile(_get_func_source(), "<mg_event_test>", "exec"), ns)
    assert ns["_build_notice_target"](None) is None
    assert ns["_build_notice_target"]([]) is None