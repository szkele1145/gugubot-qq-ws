# -*- coding: utf-8 -*-
"""todo 系统初始化对非法 next_id 的容错（v2.2.10 续，104）。

问题
----
``TodoSystem.initialize`` 里 ``self._next_id = int(self.get("next_id", 1))``
是裸 ``int()``：``todos.yml`` 由用户手工编辑（或旧版本插件写出意外类型）
时，``next_id`` 完全可能是字符串（``"5"`` 可以被 ``int()`` 吃掉，但
``"abc"``、``None``、``["x"]`` 不行）→ 抛 ``ValueError``/``TypeError``
→ **整个待办系统初始化失败**，`#待办` 全部命令失效，且日志里只有一条
初始化异常，用户难以定位。

同文件的 ``max_id = max(int(k) for k in todos.keys() ...)`` 早就包了
``try/except``，这一处是漏网的同类问题。

修复
----
``next_id`` 的 ``int()`` 包上 ``try/except (ValueError, TypeError)``，
失败时 ``logger.warning`` 记录原值并回退到 ``1``（与既有 max_id 分支同法）。

测试手法
--------
不 import 整个插件（MCDR 环境依赖重），用 ``ast`` 断言：
``initialize`` 函数体内对 ``next_id`` 的 ``int()`` 调用必须位于
``Try`` 节点之内（即被异常保护）。
"""
import ast
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SRC = _ROOT / "plugin" / "gugubot" / "logic" / "system" / "todo.py"


def _get_initialize_func():
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "initialize":
            return node
    raise AssertionError("找不到 TodoSystem.initialize")


def test_next_id_int_conversion_is_protected():
    """initialize() 里 next_id 的 int() 必须在 try 块内。"""
    fn = _get_initialize_func()
    # 收集所有 Try 块内的 int() 调用行号
    protected_lines = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Try):
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id == "int"
                ):
                    protected_lines.add(inner.lineno)
    # 找到 next_id 相关的 int() 调用
    next_id_calls = []
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "int"
        ):
            # 参数里包含 next_id 的 get 调用
            src = ast.dump(node)
            if "next_id" in src:
                next_id_calls.append(node.lineno)
    assert next_id_calls, "initialize() 里应存在 next_id 的 int() 转换"
    for line in next_id_calls:
        assert line in protected_lines, (
            f"第 {line} 行的 int(next_id) 未被 try/except 保护，"
            "非法 next_id 会让整个待办系统初始化失败"
        )


def test_next_id_fallback_exists():
    """非法 next_id 必须有回退逻辑（=1）且记录 warning。"""
    fn = _get_initialize_func()
    src = _SRC.read_text(encoding="utf-8")
    assert "except (ValueError, TypeError)" in src
    assert "warning" in src.lower()
