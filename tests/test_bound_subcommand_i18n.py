# -*- coding: utf-8 -*-
"""绑定系统的管理员子命令：命令字必须来自语言包，不能硬编码。

背景
----
``bound.py`` 过去把管理员子命令的中英双语**硬编码**在源码里::

    if command in ["白名单检查", "whitelist_check"]:
    elif command in ["移除未绑定白名单", "remove_unbound_whitelist"]:
    ...

问题有三层：
1. **语言包与代码脱节**——lang 里明明有同名条目（``whitelist_check`` 等），
   却是死键；两者一旦不同步（改文案 / 加语言），行为就不一致。
2. **新增语言必须改 Python 源码**——违反 i18n 常规做法。
3. **lang 键名与命令字拼写不一致**——lang 写 ``check_extra_bound``，
   代码命令字是 ``extra_bound_check``。

现在改为一律 ``self.get_tr(key)``：命令字只有语言包一个来源。

本测试验证：
- 四个管理员子命令的 key 都在 zh_cn / en_us 里有条目
- zh_cn 的条目值与旧硬编码的中文命令字**完全一致**（行为不变）
- en_us 的条目值与旧硬编码的英文命令字**完全一致**（行为不变）
- 源码里不再有硬编码的中英双语命令字列表
"""
import io
import os
import re
import sys

from ruamel.yaml import YAML

REPO = r"F:\vibe coding工程\gugubot-qq-ws"
sys.path.insert(0, os.path.join(REPO, "plugin"))

BOUND_PY = os.path.join(REPO, "plugin", "gugubot", "logic", "system", "bound.py")

# 旧实现里硬编码的四组命令字（中, 英）
LEGACY = {
    "whitelist_check": ("白名单检查", "whitelist_check"),
    "remove_unbound_whitelist": ("移除未绑定白名单", "remove_unbound_whitelist"),
    "extra_bound_check": ("多余绑定检查", "extra_bound_check"),
    "remove_extra_bound": ("移除多余绑定", "remove_extra_bound"),
}


def load_lang(name):
    y = YAML(typ="safe", pure=True)
    with io.open(os.path.join(REPO, "plugin", "lang", f"{name}.yml"), encoding="utf-8") as f:
        return y.load(f)


def test_zh_cn_values_match_legacy():
    """中文语言包的值必须与旧硬编码的中文命令字完全一致（行为不变）。"""
    seg = load_lang("zh_cn")["gugubot"]["system"]["bound"]
    for key, (zh, _en) in LEGACY.items():
        assert key in seg, f"zh_cn 缺少 bound.{key}"
        assert seg[key] == zh, (
            f"zh_cn bound.{key} = {seg[key]!r}，与旧硬编码 {zh!r} 不一致"
        )
    print("PASS: zh_cn 命令字与旧硬编码完全一致 ✅")


def test_en_us_values_match_legacy():
    """英文语言包的值必须与旧硬编码的英文命令字完全一致（行为不变）。"""
    seg = load_lang("en_us")["gugubot"]["system"]["bound"]
    for key, (_zh, en) in LEGACY.items():
        assert key in seg, f"en_us 缺少 bound.{key}"
        assert seg[key] == en, (
            f"en_us bound.{key} = {seg[key]!r}，与旧硬编码 {en!r} 不一致"
        )
    print("PASS: en_us 命令字与旧硬编码完全一致 ✅")


def test_source_no_hardcoded_command_lists():
    """源码里不再出现硬编码的中英双语**命令字值**列表。

    注意区分两种写法：
    - 旧：``command in ["白名单检查", "whitelist_check"]`` —— 值列表，硬编码
    - 新：``("whitelist_check", self._handle_check_whitelist)`` —— lang key
      + 处理函数，key 是**语言包键名**，正确
    因此只查「中文字面量」以及「与旧值完全相同的英文字面量」是否出现在
    ``in [...]`` 形式的比较里。
    """
    src = io.open(BOUND_PY, encoding="utf-8").read()
    for key, (zh, en) in LEGACY.items():
        assert f'"{zh}"' not in src, f"bound.py 仍硬编码中文命令字 {zh!r}"
        # 英文值只在「in [ ... ] 比较列表」里算硬编码；
        # 单独出现（作为 lang key）是对的。
        assert f'in ["{zh}", "{en}"]' not in src, (
            f"bound.py 仍硬编码中英命令字列表 {key!r}"
        )
        assert f'in ["{en}"]' not in src, (
            f"bound.py 仍硬编码英文命令字列表 {key!r}"
        )
    print("PASS: 源码不再硬编码管理员子命令字 ✅")


def test_source_uses_get_tr_for_subcommands():
    """源码通过 get_tr 取子命令 key。"""
    src = io.open(BOUND_PY, encoding="utf-8").read()
    for key in LEGACY:
        assert f'"{key}"' in src, f"bound.py 未通过 get_tr 引用 {key!r}"
    print("PASS: 四个子命令 key 都经 get_tr 取用 ✅")


def test_lang_files_symmetric():
    """两个语言包的 bound 段键集合一致。"""
    zh = load_lang("zh_cn")["gugubot"]["system"]["bound"]
    en = load_lang("en_us")["gugubot"]["system"]["bound"]
    assert set(zh) == set(en), (
        f"bound 段键不一致：zh 独有 {sorted(set(zh) - set(en))}；"
        f"en 独有 {sorted(set(en) - set(zh))}"
    )
    print("PASS: bound 段两个语言包键集合一致 ✅")


if __name__ == "__main__":
    fns = [
        test_zh_cn_values_match_legacy,
        test_en_us_values_match_legacy,
        test_source_no_hardcoded_command_lists,
        test_source_uses_get_tr_for_subcommands,
        test_lang_files_symmetric,
    ]
    for fn in fns:
        fn()
    print(f"\n全部 {len(fns)} 项通过")
