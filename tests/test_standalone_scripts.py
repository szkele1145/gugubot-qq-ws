# -*- coding: utf-8 -*-
"""把「顶层脚本式」的独立测试纳入 pytest 回归网。

背景
----
`tests/` 下有两类文件：

1. **pytest 用例**——有模块级 ``def test_*``，pytest 能直接收集
   （当前 17 个文件 / 125 项）；
2. **独立脚本**——执行即跑，靠 ``print("PASS: ...")`` 和退出码表达结果
   （``test_behavior`` / ``test_ws`` / ``test_native`` / ``test_e2e`` /
   ``test_migration`` / ``test_status`` / ``test_send_quota`` /
   ``test_config_check`` / ``test_imports``）。

第 2 类**从不被 ``pytest tests/`` 执行**——回归网漏了一大块。这正是
``test_rate_limit.py`` 长期「从未真正运行」却没人发现的同类根源。

本文件用 ``subprocess`` 把每个独立脚本跑一遍，断言：
- 退出码为 0；
- 输出里含该脚本自己的成功标志（如 ``ALL BEHAVIOR TESTS PASSED``）。

这样任何一个脚本因代码改动而失败，``pytest tests/ -q`` 立刻报警。
"""
import os
import subprocess
import sys

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS_DIR)
PYTHON = sys.executable

# (脚本名, 期望出现在输出里的成功标志)
# 标志取自各脚本自己的结尾 print；不写标志则只断言退出码为 0。
STANDALONE_SCRIPTS = [
    ("test_imports.py", "FULL PLUGIN IMPORT PASSED"),
    ("test_config_check.py", "CONFIG CHECK TEST PASSED"),
    ("test_behavior.py", "ALL BEHAVIOR TESTS PASSED"),
    ("test_ws.py", "WS PROTOCOL TEST PASSED"),
    ("test_native.py", "ALL NATIVE_CHAT TESTS PASSED"),
    ("test_e2e.py", "END-TO-END INTEGRATION TEST PASSED"),
    ("test_migration.py", "OLD-CONFIG MIGRATION TEST PASSED"),
    ("test_status.py", "OFFICIAL STATUS TEST PASSED"),
    ("test_send_quota.py", "SEND QUOTA TEST PASSED"),
    ("test_lang_consistency.py", "lang 一致性检查 PASSED"),
    ("test_lang_orphans.py", "lang 孤儿键检查 PASSED"),
]


def _run(script: str):
    path = os.path.join(TESTS_DIR, script)
    assert os.path.exists(path), f"独立脚本不存在: {path}"
    # cwd 设到仓库根：部分脚本用相对路径引用 plugin/ 与数据文件
    proc = subprocess.run(
        [PYTHON, path],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    return proc


@pytest.mark.parametrize(
    "script,marker",
    STANDALONE_SCRIPTS,
    ids=[s for s, _ in STANDALONE_SCRIPTS],
)
def test_standalone_script_passes(script, marker):
    """独立脚本必须退出码 0 且打印自己的成功标志。"""
    proc = _run(script)
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, (
        f"{script} 退出码 {proc.returncode}，输出：\n{out[-2000:]}"
    )
    assert marker in out, f"{script} 未打印成功标志 {marker!r}，输出：\n{out[-2000:]}"


def test_all_standalone_scripts_accounted():
    """仓库里新增独立脚本时必须同步登记到本文件，否则会被遗漏。"""
    on_disk = {
        f
        for f in os.listdir(TESTS_DIR)
        if f.startswith("test_") and f.endswith(".py")
    }
    registered = {s for s, _ in STANDALONE_SCRIPTS}
    unregistered = on_disk - registered
    for name in sorted(unregistered):
        path = os.path.join(TESTS_DIR, name)
        with open(path, encoding="utf-8") as f:
            src = f.read()
        # 判定「是不是 pytest 用例」：必须有**模块级**的 def test_*，
        # 或 Test* 类。缩进在函数/类内部的 test_ 不算。
        lines = src.split("\n")
        has_pytest_case = any(
            line.startswith("def test_") or line.startswith("class Test")
            for line in lines
        )
        assert has_pytest_case, (
            f"{name} 既没有模块级 def test_* / class Test，也没登记到 "
            f"STANDALONE_SCRIPTS —— 它永远不会被执行"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-v"]))
