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

# get_tr(...) 的实参列表：需要**括号配对**提取（内部可能有嵌套括号/方括号，
# 例如 get_tr("x", yes_votes=progress["yes_votes"])）。
# 正则 `[^)]*` 会在 progress["x"] 的括号处提前截断，必须手工配对扫描。
STR_LIT_RE = re.compile(r"""['"]([A-Za-z0-9_.]+)['"]""")
# 动态 key 的来源：投票类型配置里的 name_key="xxx" / description_key="xxx"
# 这类**关键字实参**里的字符串最终会被 get_tr(config.name_key) 取用，
# 但静态扫描 get_tr() 调用点抓不到——必须单独收。
DYN_KEY_RE = re.compile(
    r"""\b(?:name_key|description_key)\s*=\s*['"]([A-Za-z0-9_]+)['"]"""
)
# 动态 key 形态二：变量赋值后经 get_tr(变量) 取用
#     tr_key = "multiple_votes_specify_yes"
#     self.get_tr(tr_key, ...)
# 必须**两步**：先认出哪些变量被传给了 get_tr，再只收这些变量的赋值——
# 否则会把源码里所有字符串赋值都收进来，校验形同虚设。
GET_TR_VAR_RE = re.compile(r"""get_tr\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*[,)]""")
VAR_ASSIGN_RE = re.compile(
    r"""\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*['"]([A-Za-z0-9_]+)['"]"""
)
# 三元赋值：tr_key = "vote_new" if is_new_vote else "vote_changed"
# 两侧分支分别抓取（正则本身只定位三元表达式的整体范围）。
TERNARY_ASSIGN_RE = re.compile(
    r"""\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(['"][^'"]+['"])\s+if\s+[^()\n]+?\s+else\s+(['"][^'"]+['"])"""
)
# 动态 key 形态三：元组/列表常量里的键名
#     for key in ("name", "tps"):  self.get_tr(key)
#     admin_subcommands = (("a", h1), ("b", h2))
#     for key, handler in admin_subcommands: self.get_tr(key)
# 两种形态都要覆盖：直接 for + 变量间接引用。
TUPLE_KEY_RE = re.compile(
    r"""for\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:,\s*[A-Za-z_][A-Za-z0-9_]*\s*)?in\s+[\(\[\{]([^)\]\}]+)[\)\]\}]"""
)
# 变量 → 常量元组的间接引用：name = ( ... )
# 元组里可能有**嵌套括号**（("key", self._handler)），正则不好精确匹配，
# 改用配对扫描函数 iter_var_tuples()。
VAR_TUPLE_ASSIGN_RE = re.compile(
    r"""\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*[\(\[]"""
)
# for <var> in <名字>（名字是上面那种变量）
FOR_IN_VAR_RE = re.compile(
    r"""for\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:,\s*[A-Za-z_][A-Za-z0-9_]*\s*)?in\s+([A-Za-z_][A-Za-z0-9_]*)\b"""
)
# 系统注册名：BasicSystem.__init__(self, "name", ...)
SYSTEM_NAME_RE = re.compile(r"""BasicSystem\.__init__\(\s*self,\s*['"]([A-Za-z0-9_]+)['"]""")


def iter_var_tuples(src: str):
    """产出 (变量名, 常量元组/列表的源码文本)。

    形如 ``admin_subcommands = (("a", h1), ("b", h2))``。用配对扫描正确
    处理嵌套括号——正则很难精确匹配。
    """
    for m in VAR_TUPLE_ASSIGN_RE.finditer(src):
        name = m.group(1)
        start = m.end() - 1
        opener = src[start]
        closer = ")" if opener == "(" else "]"
        depth = 0
        k = start
        while k < len(src):
            ch = src[k]
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        yield name, src[start:k + 1]


def iter_get_tr_first_args(src: str):
    """产出每个 ``get_tr(...)`` 调用的**第一个位置参数**（去掉首尾空白）。

    手工做括号配对扫描，正确处理嵌套：
        get_tr("x", a=b["c"])
        get_tr("yes" if flag else "no")
        get_tr("outer", title=self.get_tr("title"))   ← 内层也要抓
    只取第一个位置参数——lang key 永远是它；后面的 ``name=value`` 是
    **kwargs 格式化占位符（模板变量名），不是 lang key。
    """
    i = 0
    n = len(src)
    while True:
        j = src.find("get_tr(", i)
        if j < 0:
            return
        start = j + len("get_tr(")
        depth = 1
        k = start
        while k < n and depth:
            ch = src[k]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        args = src[start:k]

        # 按顶层逗号切分实参（跳过括号/引号内部）
        depth = 0
        quote = None
        cut = None
        for idx, ch in enumerate(args):
            if quote:
                if ch == quote:
                    quote = None
                continue
            if ch in "\"'":
                quote = ch
            elif ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == "," and depth == 0:
                cut = idx
                break
        first = args if cut is None else args[:cut]
        yield first.strip()

        # 继续从**外层实参内部**往后找：嵌套的 get_tr("title") 也要抓。
        # 注意不能跳过整个实参列表——那会漏掉嵌套调用。
        i = start


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

            for first in iter_get_tr_first_args(src):
                # 第一个位置参数里的字符串字面量就是候选 key
                # （三元表达式的各个分支都是位置参数，都要抓）
                for lit in STR_LIT_RE.finditer(first):
                    if lit.group(1).startswith("gugubot."):
                        top_level.add(lit.group(1))
                    else:
                        all_keys.add(lit.group(1))

            # 动态 key：name_key="xxx" / description_key="xxx"
            # （最终经 get_tr(config.name_key) 取用，get_tr 调用点抓不到）
            for m in DYN_KEY_RE.finditer(src):
                all_keys.add(m.group(1))

            # 动态 key：变量赋值后经 get_tr(变量) 取用
            # （tr_key = "multiple_votes_specify_yes"）
            # 两步走：先认出被传给 get_tr 的变量名，再只收这些变量的赋值
            tr_vars = {m.group(1) for m in GET_TR_VAR_RE.finditer(src)}
            if tr_vars:
                for m in VAR_ASSIGN_RE.finditer(src):
                    if m.group(1) in tr_vars:
                        all_keys.add(m.group(2))
                # 三元赋值：tr_key = "vote_new" if is_new_vote else "vote_changed"
                # （两侧分支都是候选 key）
                for m in TERNARY_ASSIGN_RE.finditer(src):
                    if m.group(1) in tr_vars:
                        all_keys.add(STR_LIT_RE.match(m.group(2)).group(1))
                        all_keys.add(STR_LIT_RE.match(m.group(3)).group(1))

            # 动态 key：元组/列表常量里的键名
            # （for key in ("name", "tps"): self.get_tr(key)）
            # 同样要两步：只认「循环变量被传给 get_tr」的那一处
            tr_vars = {m.group(1) for m in GET_TR_VAR_RE.finditer(src)}
            for m in TUPLE_KEY_RE.finditer(src):
                loop_var = m.group(1)
                if loop_var not in tr_vars:
                    continue
                for lit in re.findall(r"""['"]([A-Za-z0-9_]+)['"]""", m.group(2)):
                    all_keys.add(lit)

            # 动态 key（元组的间接引用）：
            #     admin_subcommands = (("whitelist_check", h1), ...)
            #     for key, handler in admin_subcommands: self.get_tr(key)
            # 先收「变量 → 常量元组」的映射，再看哪些 for 循环在遍历它。
            var_tuples = {}
            for name, text in iter_var_tuples(src):
                var_tuples[name] = text
            for m in FOR_IN_VAR_RE.finditer(src):
                loop_var = m.group(1)
                seq_var = m.group(2)
                if loop_var not in tr_vars or seq_var not in var_tuples:
                    continue
                for lit in re.findall(r"""['"]([A-Za-z0-9_]+)['"]""", var_tuples[seq_var]):
                    all_keys.add(lit)

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
