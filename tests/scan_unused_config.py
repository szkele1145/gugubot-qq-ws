# -*- coding: utf-8 -*-
"""扫描 default_config.yml 里的配置键，找出「定义了但源码里没人读」的项。

这类项最容易让用户困惑：改了没反应、文档里写了但功能不存在。
"""
import re
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CFG = ROOT / "plugin" / "gugubot" / "config" / "defaults" / "default_config.yml"
SRC = ROOT / "plugin" / "gugubot"

text = CFG.read_text(encoding="utf-8")

# 解析 YAML 的缩进结构（只取纯映射，不含 list 展开）
lines = text.splitlines()
entries = []  # (path_tuple, indent, is_list_item)
stack = []
for ln in lines:
    if not ln.strip() or ln.strip().startswith("#"):
        continue
    m = re.match(r"^(\s*)([A-Za-z_][\w\-]*):\s*(.*)$", ln)
    if not m:
        continue
    indent = len(m.group(1))
    key = m.group(2)
    val = m.group(3).strip()
    while stack and stack[-1][0] >= indent:
        stack.pop()
    stack.append((indent, key))
    if val == "":
        continue  # 父节点
    entries.append((tuple(k for _, k in stack), val))

# 收集源码里出现的所有 get_keys / get 路径字符串
src_text = "\n".join(
    p.read_text(encoding="utf-8", errors="ignore")
    for p in SRC.rglob("*.py")
)
# 提取形如 "a", "b", "c" 的连续字符串组
groups = re.findall(r"\[\s*((?:\"[^\"]+\"\s*,\s*)*\"[^\"]+\")\s*\]", src_text)
used_paths = set()
for g in groups:
    parts = re.findall(r'"([^"]+)"', g)
    if parts:
        used_paths.add(tuple(parts))

# 也收集单独出现的键名（作为兜底：有些键通过变量拼接）
all_keys = set()
for p in SRC.rglob("*.py"):
    all_keys.update(re.findall(r'"([A-Za-z_][\w\-]*)"', p.read_text(encoding="utf-8", errors="ignore")))

print(f"default_config.yml 叶子节点: {len(entries)}")
print(f"源码里出现的 get_keys 路径: {len(used_paths)}")
print()
print("=== 疑似「定义了但没人读」===")
suspects = []
for path, val in entries:
    if path in used_paths:
        continue
    leaf = path[-1]
    # 兜底：叶子键名在源码里根本没出现过
    if leaf in all_keys:
        continue
    suspects.append((path, val))

for path, val in suspects:
    print(f"  {'.'.join(path):60s} = {val[:40]}")

print()
print(f"疑似项: {len(suspects)} / {len(entries)}")
