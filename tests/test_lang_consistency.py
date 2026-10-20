# -*- coding: utf-8 -*-
"""lang 文件键一致性检查：zh_cn 与 en_us 必须覆盖同一套键。

背景
----
``BasicSystem.get_tr(key)`` 走 ``server.tr("gugubot.system.<name>.<key>")``。
MCDR 的 ``tr`` 在 key 不存在时**返回 key 字符串本身**（不抛异常），于是
「取不到翻译」会退化成「命令前缀变成一串 key 路径」→ 相应命令**静默失
效**，且没有任何报错。

这在本项目里真实发生过：
- en_us 里把 ``ban_words`` / ``key_words`` 写成单数 → 英文环境违禁词、
  关键词系统取不到系统名
- en_us 用 ``abstain`` 而代码用 ``withdraw`` → 英文环境投票弃票失效
- en_us 用 ``whitelist_enable_success`` 而代码用 ``enable_success``
  → 英文环境白名单开关反馈失效
- en_us 缺 ``list`` 系统的 13 个多服聚合标签键

本脚本做两件事：
1. **静态扫描**：从源码里正则抽出所有 ``get_tr("...")`` 字面量 key，
   逐个确认两个 lang 文件都有对应条目。
2. **结构比对**：两个 lang 文件的 system 段 key 集合必须完全一致。

作为独立脚本运行（不被 pytest 收集，因为它是静态检查而非断言式用例）：
    python tests\\test_lang_consistency.py
"""
import io
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LANG_DIR = REPO / "plugin" / "lang"
SRC_DIR = REPO / "plugin" / "gugubot"

# get_tr("literal") 的字面量 key（不含 global_key 调用的 gugubot.* 顶层键，
# 那些在 lang 根下核对）
GET_TR_RE = re.compile(r"""get_tr\(\s*['"]([A-Za-z0-9_.]+)['"]""")
# 系统注册名：BasicSystem.__init__(self, "name", ...)
SYSTEM_NAME_RE = re.compile(r"""BasicSystem\.__init__\(\s*self,\s*['"]([A-Za-z0-9_]+)['"]""")


def load_lang(path: Path) -> dict:
    from ruamel.yaml import YAML

    y = YAML(typ="safe", pure=True)
    with io.open(path, encoding="utf-8") as f:
        return y.load(f)


def collect_code_keys():
    """扫描源码，收集 (系统名, 键) 与顶层键。

    ``get_tr`` 的 key 归属到哪个系统取决于运行时的 ``self.name``，静态
    分析无法确定。因此这里**不按系统归属**，而是收集「所有被引用过的
    key 名」——后续检查时，任何 system 段缺这些 key 都会报出来。
    """
    all_keys = set()
    top_level = set()
    system_names = set()

    for root, _dirs, files in os.walk(SRC_DIR):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(root, fn)
            src = io.open(p, encoding="utf-8").read()

            for m in SYSTEM_NAME_RE.finditer(src):
                system_names.add(m.group(1))

            for m in GET_TR_RE.finditer(src):
                key = m.group(1)
                if key.startswith("gugubot."):
                    top_level.add(key)
                else:
                    all_keys.add(key)

    return system_names, all_keys, top_level


def dig(data, dotted):
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def main() -> int:
    zh = load_lang(LANG_DIR / "zh_cn.yml")
    en = load_lang(LANG_DIR / "en_us.yml")

    sys_names, code_keys, top_level = collect_code_keys()

    problems = []

    zh_sys = zh.get("gugubot", {}).get("system", {})
    en_sys = en.get("gugubot", {}).get("system", {})

    # --- 1. system 段 key 集合一致 ---
    zh_only = set(zh_sys) - set(en_sys)
    en_only = set(en_sys) - set(zh_sys)
    if zh_only:
        problems.append(f"en_us 缺少 system 条目: {sorted(zh_only)}")
    if en_only:
        problems.append(f"en_us 多出 system 条目（代码无对应）: {sorted(en_only)}")

    # --- 2. 代码注册的每个系统，在两个 lang 里都要有条目 ---
    for sname in sorted(sys_names):
        for lang_name, lang_sys in (("zh_cn", zh_sys), ("en_us", en_sys)):
            if sname not in lang_sys:
                problems.append(
                    f"{lang_name} 缺少 system 条目 '{sname}'"
                    f"（代码注册了该系统）"
                )

    # --- 3. 两个 lang 文件互相之间，system 段内的键集合必须一致 ---
    for sname in sorted(set(zh_sys) & set(en_sys)):
        a = set(zh_sys[sname])
        b = set(en_sys[sname])
        if a != b:
            problems.append(
                f"system '{sname}' 键不一致："
                f"zh_cn 独有 {sorted(a - b)}；en_us 独有 {sorted(b - a)}"
            )

    # --- 4. 代码引用的 key，必须在「所有」system 段的并集里能取到 ---
    #      （键归属静态不可知，取并集是最严格且无误报的判据：
    #       若某 key 在任何 system 段都不存在，则必然取不到）
    for key in sorted(code_keys):
        if not any(key in seg for seg in zh_sys.values()):
            problems.append(f"zh_cn 所有 system 段都缺键 '{key}'（代码引用了它）")
        if not any(key in seg for seg in en_sys.values()):
            problems.append(f"en_us 所有 system 段都缺键 '{key}'（代码引用了它）")

    for key in sorted(top_level):
        for lang_name, lang in (("zh_cn", zh), ("en_us", en)):
            if not dig(lang, key):
                problems.append(f"{lang_name} 缺少顶层键 {key}")

    if problems:
        print("lang 一致性检查 FAILED:")
        for p in problems:
            print("  -", p)
        return 1

    print(
        f"lang 一致性检查 PASSED"
        f"（system 条目 {len(zh_sys)} 个，"
        f"代码注册系统 {len(sys_names)} 个，"
        f"代码引用键 {len(code_keys)} 个，"
        f"顶层键 {len(top_level)} 个）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
