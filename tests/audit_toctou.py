# -*- coding: utf-8 -*-
"""全项目 TOCTOU（先查后用）模式审计。

背景
----
#70 修的 OneBot 竞态本质是一类模式：**先检查、再使用**，两次独立的
容器访问之间没有锁保护 → 中间条目可能被另一个线程改掉。

典型形态::

    if key in self.dict:          # ← 检查
        use(self.dict[key])        # ← 使用（中间可能已被删 → KeyError）

    v = self.dict.get(key)        # ← 检查
    if v is not None:             # ← 使用（中间可能已被改 → 值不是检查时那个）

这个脚本用 AST 扫描整个 ``plugin/gugubot`` 包，把所有「同一个容器上
先读后用」的复合操作列出来，供人工逐个判断是否真的需要加锁。

**它不判定对错，只负责把候选集中暴露出来** —— 并发的正确性最终仍
要靠针对性的测试来证明。
"""
import ast
import pathlib
import sys


# 会被视为「容器」的属性名（self.xxx / cls.xxx）
CONTAINER_HINTS = (
    "dict", "map", "cache", "pool", "store", "registry",
    "pending", "requests", "return", "records", "data", "table",
    "sessions", "state", "voters", "votes", "members", "players",
)

# 读容器的方法
READ_METHODS = {
    "get", "pop", "setdefault", "keys", "values", "items",
    "copy", "clear", "update",
}


class Visitor(ast.NodeVisitor):
    """在一个函数体里找「先查后用」的复合操作。"""

    def __init__(self, path, rel):
        self.path = path
        self.rel = rel
        self.findings = []
        # 记录「本函数内读过的容器变量名」→ 读所在的行号
        self.reads = {}

    # ---------- 工具 ----------
    def _container_name(self, node):
        """判断一个表达式是否像「容器读取」，返回变量名或 None。"""
        if isinstance(node, ast.Subscript):
            base = node.value
        elif isinstance(node, ast.Attribute):
            base = node.value
        elif isinstance(node, ast.Name):
            base = node
        else:
            return None

        if isinstance(base, ast.Name):
            return base.id
        if isinstance(base, ast.Attribute):
            # self.xxx / self.a.b
            return base.attr
        return None

    def _is_containerish(self, name):
        low = (name or "").lower()
        return any(h in low for h in CONTAINER_HINTS)

    # ---------- 记录读取 ----------
    def visit_Subscript(self, node):
        name = self._container_name(node)
        if name and self._is_containerish(name):
            self.reads.setdefault(name, []).append(node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node):
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in READ_METHODS
        ):
            base = node.func.value
            name = None
            if isinstance(base, ast.Name):
                name = base.id
            elif isinstance(base, ast.Attribute):
                name = base.attr
            if name and self._is_containerish(name):
                self.reads.setdefault(name, []).append(node.lineno)
        self.generic_visit(node)

    # ---------- 找「先查后用」 ----------
    def visit_If(self, node):
        # 形态 A: if key in D: ... D[key] ...
        test = node.test
        if isinstance(test, ast.Compare) and any(
            isinstance(op, (ast.In, ast.NotIn)) for op in test.ops
        ):
            for op in test.ops:
                if not isinstance(op, (ast.In, ast.NotIn)):
                    continue
                # 容器那一侧
                cmp_op = test.ops[test.ops.index(op)]
                for side in (test.left, test.comparators[0]):
                    name = self._container_name(side)
                    if not name or not self._is_containerish(name):
                        continue
                    # 在 body 里找同容器的再次访问
                    for sub in ast.walk(node):
                        if not isinstance(sub, ast.Subscript):
                            continue
                        if self._container_name(sub) != name:
                            continue
                        if sub.lineno <= test.lineno:
                            continue
                        self.findings.append((
                            self.rel, sub.lineno,
                            f"「先 in 后取值」{name}",
                            f"检查在 L{test.lineno}，取值在 L{sub.lineno}",
                        ))
                        break
                    break
        self.generic_visit(node)

    def visit_Assign(self, node):
        # 形态 B: v = D.get(k) / D.pop(k) ... if v is not None: use(v)
        val = node.value
        if isinstance(val, ast.Call) and isinstance(val.func, ast.Attribute):
            base = val.func.value
            name = None
            if isinstance(base, ast.Name):
                name = base.id
            elif isinstance(base, ast.Attribute):
                name = base.attr
            if name and self._is_containerish(name):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                for t in targets:
                    self.reads.setdefault(f"{t}<-{name}", []).append(node.lineno)
        self.generic_visit(node)


def scan_file(path, root):
    try:
        src = path.read_text(encoding="utf-8")
    except Exception:
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [(str(path), e.lineno or 0, "语法错误", str(e))]

    rel = str(path.relative_to(root)).replace("\\", "/")
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            v = Visitor(path, rel)
            for stmt in node.body:
                v.visit(stmt)
            for f in v.findings:
                out.append((rel, f[1], f[2], f[3]))
    return out


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "plugin/gugubot")
    if not root.exists():
        print(f"目录不存在: {root}")
        return 1

    all_findings = []
    files = sorted(root.rglob("*.py"))
    for p in files:
        all_findings.extend(scan_file(p, root))

    if not all_findings:
        print("未发现「先查后用」候选。")
        return 0

    print(f"扫描 {len(files)} 个文件，发现 {len(all_findings)} 处候选：\n")
    by_file = {}
    for rel, line, kind, detail in all_findings:
        by_file.setdefault(rel, []).append((line, kind, detail))
    for rel in sorted(by_file):
        print(f"--- {rel}")
        for line, kind, detail in sorted(by_file[rel]):
            print(f"    L{line:<5} {kind:<24} {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
