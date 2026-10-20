# -*- coding: utf-8 -*-
"""lang 孤儿键检查：找出「语言包里有、但代码从不引用」的键。

为什么值得查
------------
孤儿键往往不是无害的历史包袱，而是**拼写错误的信号**：

- en_us 里把 ``withdraw`` 写成 ``abstain``，代码引用的是 ``withdraw``
  → 英文环境取不到（正向检查能抓到）
- 反过来，如果某语言包里有 ``abstain`` 而代码引用 ``withdraw``，
  孤儿键 ``abstain`` 就是**错位留下的残骸**
- zh_cn 漏译某个键（en_us 有、zh_cn 无）→ zh_cn 会出现「孤儿」视角：
  代码引用了它但 zh_cn 缺（正向检查抓到）

更实用的场景是**两个语言包互为对照**：zh_cn 有的键 en_us 没有，
说明 en_us 漏译——正向检查（代码引用集合 vs 语言包）只能保证「代码用
到的都有」，保证不了「语言包有的两边都有」。

本脚本做三件事：
1. 两个语言包的 system 段键集合互查（孤儿 / 缺失双向）
2. 列出**两边都有但代码从不引用**的键——这些是纯死键，可安全清理
   （只报告，不自动删）
3. 顶层（非 system 段）键同样互查

作为独立脚本运行：
    python tests\\test_lang_orphans.py
"""
import io
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LANG_DIR = REPO / "plugin" / "lang"
SRC_DIR = REPO / "plugin" / "gugubot"

STR_LIT_RE = re.compile(r"""['"]([A-Za-z0-9_.]+)['"]""")
DYN_KEY_RE = re.compile(
    r"""\b(?:name_key|description_key)\s*=\s*['"]([A-Za-z0-9_]+)['"]"""
)
# 动态 key（变量式 + 元组式）——与 test_lang_consistency.py 同源逻辑
GET_TR_VAR_RE = re.compile(r"""get_tr\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*[,)]""")
VAR_ASSIGN_RE = re.compile(
    r"""\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*['"]([A-Za-z0-9_]+)['"]"""
)
TERNARY_ASSIGN_RE = re.compile(
    r"""\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(['"][^'"]+['"])\s+if\s+[^()\n]+?\s+else\s+(['"][^'"]+['"])"""
)
TUPLE_KEY_RE = re.compile(
    r"""for\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:,\s*[A-Za-z_][A-Za-z0-9_]*\s*)?in\s+[\(\[\{]([^)\]\}]+)[\)\]\}]"""
)
VAR_TUPLE_RE = re.compile(
    r"""\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\([^()]*\)|\[[^\[\]]*\])""",
    re.S,
)
FOR_IN_VAR_RE = re.compile(
    r"""for\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:,\s*[A-Za-z_][A-Za-z0-9_]*\s*)?in\s+([A-Za-z_][A-Za-z0-9_]*)\b"""
)
SYSTEM_NAME_RE = re.compile(
    r"""BasicSystem\.__init__\(\s*self,\s*['"]([A-Za-z0-9_]+)['"]"""
)

# 复用一致性检查脚本里的实参解析（同一份实现，避免两处漂移）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_lang_consistency import (  # noqa: E402
    iter_get_tr_first_args,
    iter_var_tuples,
)


def load_lang(path):
    from ruamel.yaml import YAML

    y = YAML(typ="safe", pure=True)
    with io.open(path, encoding="utf-8") as f:
        return y.load(f)


def collect_code_keys():
    all_keys = set()
    system_names = set()
    for root, _dirs, files in os.walk(SRC_DIR):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            src = io.open(os.path.join(root, fn), encoding="utf-8").read()
            for m in SYSTEM_NAME_RE.finditer(src):
                system_names.add(m.group(1))
            for first in iter_get_tr_first_args(src):
                for lit in STR_LIT_RE.finditer(first):
                    all_keys.add(lit.group(1))
            for m in DYN_KEY_RE.finditer(src):
                all_keys.add(m.group(1))

            # 动态 key（变量式）：先认出被传给 get_tr 的变量，再收其赋值
            tr_vars = {m.group(1) for m in GET_TR_VAR_RE.finditer(src)}
            if tr_vars:
                for m in VAR_ASSIGN_RE.finditer(src):
                    if m.group(1) in tr_vars:
                        all_keys.add(m.group(2))
                # 三元赋值：两侧分支都是候选 key
                for m in TERNARY_ASSIGN_RE.finditer(src):
                    if m.group(1) in tr_vars:
                        all_keys.add(STR_LIT_RE.match(m.group(2)).group(1))
                        all_keys.add(STR_LIT_RE.match(m.group(3)).group(1))

            # 动态 key（元组式）：for key in ("a", "b"): get_tr(key)
            for m in TUPLE_KEY_RE.finditer(src):
                if m.group(1) not in tr_vars:
                    continue
                for lit in re.findall(r"""['"]([A-Za-z0-9_]+)['"]""", m.group(2)):
                    all_keys.add(lit)

            # 动态 key（元组的间接引用）：
            #     admin_subcommands = (("whitelist_check", h1), ...)
            #     for key, handler in admin_subcommands: get_tr(key)
            var_tuples = {}
            for name, text in iter_var_tuples(src):
                var_tuples[name] = text
            for m in FOR_IN_VAR_RE.finditer(src):
                if m.group(1) not in tr_vars or m.group(2) not in var_tuples:
                    continue
                for lit in re.findall(r"""['"]([A-Za-z0-9_]+)['"]""", var_tuples[m.group(2)]):
                    all_keys.add(lit)
    return system_names, all_keys


def flat_keys(d, prefix=""):
    """把嵌套 dict 摊平成点分隔的键集合。"""
    out = set()
    if isinstance(d, dict):
        for k, v in d.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            out.add(p)
            out |= flat_keys(v, p)
    return out


def main():
    zh = load_lang(LANG_DIR / "zh_cn.yml")
    en = load_lang(LANG_DIR / "en_us.yml")

    _sys_names, code_keys = collect_code_keys()

    problems = []
    info = []

    # --- 1. system 段逐系统比对：孤儿键 ---
    zh_sys = zh.get("gugubot", {}).get("system", {})
    en_sys = en.get("gugubot", {}).get("system", {})

    for sname in sorted(set(zh_sys) & set(en_sys)):
        zh_k = set(zh_sys[sname])
        en_k = set(en_sys[sname])
        only_zh = zh_k - en_k
        only_en = en_k - zh_k
        if only_zh:
            problems.append(
                f"system '{sname}': zh_cn 独有键（en_us 漏译）{sorted(only_zh)}"
            )
        if only_en:
            problems.append(
                f"system '{sname}': en_us 独有键（zh_cn 漏译）{sorted(only_en)}"
            )
        # 两边都有、但代码从不引用的死键
        dead = (zh_k & en_k) - code_keys
        if dead:
            info.append(f"system '{sname}': 两边都有但代码未引用的键 {sorted(dead)}")

    # --- 2. 顶层（system 段之外）键互查 ---
    zh_top = flat_keys(zh.get("gugubot", {}), "gugubot")
    en_top = flat_keys(en.get("gugubot", {}), "gugubot")
    zh_out = {k for k in zh_top if not k.startswith("gugubot.system.")}
    en_out = {k for k in en_top if not k.startswith("gugubot.system.")}
    only_zh = zh_out - en_out
    only_en = en_out - zh_out
    if only_zh:
        problems.append(f"顶层键 zh_cn 独有: {sorted(only_zh)}")
    if only_en:
        problems.append(f"顶层键 en_us 独有: {sorted(only_en)}")

    if info:
        print("死键（两边都有、代码未引用——可安全清理，仅报告）：")
        for i in info:
            print("  *", i)
        print()

    if problems:
        print("lang 孤儿键检查 FAILED:")
        for p in problems:
            print("  -", p)
        return 1

    print("lang 孤儿键检查 PASSED（两个语言包结构对称，无单边漏译）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
