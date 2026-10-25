# -*- coding: utf-8 -*-
"""on_unload 清理步骤必须互相隔离（裸 except 移除）。

问题
----
``plugin/gugubot/__init__.py`` 的 ``on_unload`` 曾用 ``try/except: pass``
把全部清理一网打尽：任何一个清理步骤（停定时任务、断开连接器）抛异常都会被吞掉，
且后续步骤都不会执行。

修复
----
每个清理步骤单独包裹 ``try/except``，异常仅记录到 logger.debug，不影响其余清理。

测试
----
用 ``ast`` 解析源码，断言：
- 不存在裸 ``except:``（应为 ``except Exception``）
- ``on_unload`` 中包含对 ``stop_schedule_task``、``disconnect_all`` 的独立异常隔离
"""
import ast
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SRC = _ROOT / "plugin" / "gugubot" / "__init__.py"


def _get_on_unload_func():
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_unload":
            return node
    raise AssertionError("找不到 on_unload")


def test_on_unload_has_no_bare_except():
    src = _SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                raise AssertionError("存在裸 except:，应改用 except Exception:")


def test_on_unload_isolates_unbound_check_stop():
    fn = _get_on_unload_func()
    excepts = []
    for node in ast.walk(fn):
        if isinstance(node, ast.ExceptHandler):
            excepts.append(node)
    assert len(excepts) >= 3, f"on_unload 内至少应有 3 个 except（隔离三个清理步骤），实际 {len(excepts)}"


def test_on_unload_isolates_inactive_check_stop():
    src = _SRC.read_text(encoding="utf-8")
    assert "unbound_check_system" in src
    assert "inactive_check_system" in src
    assert src.count("stop_schedule_task()") >= 1


def test_on_unload_isolates_connector_disconnect():
    src = _SRC.read_text(encoding="utf-8")
    assert "disconnect_all()" in src
    fn = _get_on_unload_func()
    excepts = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
    assert len(excepts) >= 4


def test_on_unload_logs_on_cleanup_failure():
    src = _SRC.read_text(encoding="utf-8")
    assert "[GUGUBot]" in src
    assert "server.logger" in src
