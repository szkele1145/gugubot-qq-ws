# -*- coding: utf-8 -*-
"""配置形态审计：找出「读出来就当列表/数字用」的配置读取点。

为什么需要
----------
配置由**用户手工编辑**，形态不受程序控制。一个少写的 ``-`` 就会让

.. code-block:: yaml

    admin_ids: 111222333        # 本意是列表

变成**字符串**，于是 ``for x in admin_ids`` 逐字符遍历，造出
``{"1", "1", "2", …}`` 这样的垃圾数据。本项目已经因此出过两次真实缺陷：

* #90 群号配置写成标量 → 一个群号变出九个垃圾目标；
* #91 ``admin_ids`` 写成标量 → 真实管理员不在跳过集合里，被当成普通用户
  （收到「请先绑定」、被判不活跃）。

这两处都是靠**手工翻文件**发现的。本脚本把「找同类位置」这一步自动化：
扫出所有 ``get_keys(...)`` 调用，报告其中**结果被直接遍历/计数/取极值**的
位置，供人工判断是否缺 ``as_id_list`` / ``safe_int`` 等归一化。

用法
----
::

    py tests/audit_config_shape.py            # 只看可疑点
    py tests/audit_config_shape.py --all      # 连全部 get_keys 一起列

输出只是**线索**，不是结论：有些位置确实保证拿到列表（例如默认值就是
``[]`` 且配置项由插件自己写入），人工确认后可忽略。

判定规则（保守，宁可少报）
--------------------------
对每个 ``get_keys(...)`` 调用，看它的**直接父节点**：

* ``for x in get_keys(...)``            → 可疑（逐字符）
* ``set(x for x in get_keys(...))``     → 可疑（逐字符）
* ``len/any/all/sorted/max/min/sum(get_keys(...))`` → 可疑（标量也「能算」，
  但语义通常要求列表）
* 其余（比较、作为参数传递、``or []`` 兜底后赋值）→ 不报

**不报**「赋值给变量后再遍历」的情形（需要数据流分析，误报率高）。
那类位置由 ``tests/test_id_list_normalization.py`` 与
``tests/test_check_modules_id_shape.py`` 的静态断言兜住。
"""
from __future__ import annotations

import ast
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SCAN_ROOT = _ROOT / "plugin" / "gugubot"

_AGGREGATORS = ("len", "any", "all", "sorted", "max", "min", "sum")


def _is_get_keys(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_keys"
    )


def _display_path(path: pathlib.Path) -> str:
    """尽量显示相对仓库根的路径；仓库外的路径原样显示。

    ``scan()`` 是通用函数（测试里会喂临时目录），不能因为路径不在仓库下
    就抛 ``ValueError`` —— 那样审计器只能在「恰好扫仓库」时工作。
    """
    try:
        return str(path.relative_to(_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def scan(root: pathlib.Path) -> tuple[int, list[tuple[str, int, str]]]:
    """返回 (get_keys 调用总数, [(文件, 行号, 可疑原因)])。"""
    total = 0
    suspicious: list[tuple[str, int, str]] = []

    for path in sorted(root.rglob("*.py")):
        try:
            # utf-8-sig：源码若带 BOM，ast.parse 会因 U+FEFF 报 SyntaxError
            src = path.read_text(encoding="utf-8-sig")
            tree = ast.parse(src, filename=str(path))
        except (OSError, SyntaxError) as exc:      # pragma: no cover
            print(f"[跳过] {path}: {exc}", file=sys.stderr)
            continue

        # 建 parent 映射（一次遍历）
        parent: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent[child] = node

        for node in ast.walk(tree):
            if not _is_get_keys(node):
                continue
            total += 1
            par = parent.get(node)
            rel = _display_path(path)

            if isinstance(par, ast.For) and par.iter is node:
                suspicious.append((rel, node.lineno, "直接作为 for 的 iter"))
            elif isinstance(par, ast.comprehension) and par.iter is node:
                suspicious.append((rel, node.lineno, "推导式的 iter"))
            elif (
                isinstance(par, ast.Call)
                and isinstance(par.func, ast.Name)
                and par.func.id in _AGGREGATORS
            ):
                suspicious.append(
                    (rel, node.lineno, f"作为 {par.func.id}() 的参数")
                )

    return total, suspicious


def main(argv: list[str]) -> int:
    show_all = "--all" in argv
    total, suspicious = scan(_SCAN_ROOT)

    print(f"扫描目录: {_SCAN_ROOT}")
    print(f"get_keys 调用总数: {total}")
    print()
    if not suspicious:
        print("未发现「结果被直接遍历/计数」的可疑位置。")
        return 0

    print(f"== 可疑位置 {len(suspicious)} 处（人工确认是否缺归一化）==")
    for rel, lineno, why in suspicious:
        print(f"  {rel}:{lineno}  <- {why}")

    print()
    print("对照处理方式：")
    print("  * 列表形态（群号 / 用户号）→ gugubot.utils.num.as_id_list()")
    print("  * 数值形态（天数 / 间隔）  → gugubot.utils.num.safe_int() 等")
    if not show_all:
        print("  （加 --all 可查看全部 get_keys 调用清单）")
    else:                                       # pragma: no cover
        for path in sorted(_SCAN_ROOT.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if _is_get_keys(node):
                    print(f"    {path.relative_to(_ROOT)}:{node.lineno}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
