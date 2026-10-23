# -*- coding: utf-8 -*-
"""配置形态审计必须持续为「零可疑」（v2.2.10 续，92）。

背景
----
``tests/audit_config_shape.py`` 是本轮新增的审计脚本：扫描全部
``get_keys(...)`` 调用，报告其中**结果被直接遍历/计数**的位置。

本项目已因「配置形态」出过两次真实缺陷（#90 群号标量 → 一个群号变九个
垃圾目标；#91 ``admin_ids`` 标量 → 管理员被当普通用户），两次都靠**手工
翻文件**发现。把审计脚本固化进仓库之后，这类问题可以**自动**兜住。

本文件把它变成回归守卫，并额外验证**审计器自己有效** ——
否则它可能永远「零可疑」而毫无保护作用（#72 踩过同样的坑：并发测试在
旧代码上也通过，等于没测）。
"""
import importlib.util
import pathlib
import sys
import tempfile
import textwrap

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_AUDIT = _ROOT / "tests" / "audit_config_shape.py"


def _load_audit():
    spec = importlib.util.spec_from_file_location("_audit_config_shape", _AUDIT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_audit_config_shape"] = mod
    spec.loader.exec_module(mod)
    return mod


# ==================================================== 1. 真实代码必须零可疑
def test_no_suspicious_config_usage_in_plugin():
    """插件源码里不得再有「直接把 get_keys 结果拿去遍历」的位置。

    发现了就把那行改成 ``as_id_list(...)``（列表形态）或
    ``safe_int(...)``（数值形态）—— 见 ``gugubot/utils/num.py``。
    """
    audit = _load_audit()
    total, suspicious = audit.scan(_ROOT / "plugin" / "gugubot")

    assert total > 100, (
        f"只扫到 {total} 个 get_keys 调用 —— 扫描器可能坏了（正常应 200+）"
    )
    assert suspicious == [], (
        "以下位置把 get_keys 的结果直接拿去遍历/计数，用户把配置写成标量时"
        "会被逐字符拆开（历史缺陷 #90 / #91）：\n"
        + "\n".join(f"  {f}:{ln}  <- {why}" for f, ln, why in suspicious)
        + "\n\n修复：列表形态改用 as_id_list()，数值形态改用 safe_int()。"
    )


# ==================================================== 2. 审计器本身必须有效
def test_scanner_detects_known_bad_patterns():
    """喂一份**故意写坏**的样本，扫描器必须报出来。

    没有这条，上面的「零可疑」可能只是因为扫描器什么都不报。
    """
    audit = _load_audit()

    bad_module = textwrap.dedent(
        """
        class Cfg:
            def get_keys(self, path, default=None):
                return default


        def bad_for(cfg):
            for x in cfg.get_keys(["a"], []):
                print(x)


        def bad_comprehension(cfg):
            return {str(x) for x in cfg.get_keys(["b"], [])}


        def bad_len(cfg):
            return len(cfg.get_keys(["c"], []))


        def fine(cfg):
            ids = cfg.get_keys(["d"], [])
            for x in ids:          # 赋值后再遍历：本扫描器不报（需数据流分析）
                print(x)
        """
    )

    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "sample.py").write_text(bad_module, encoding="utf-8")
        total, suspicious = audit.scan(root)

    assert total == 4, f"样本里有 4 个 get_keys 调用，实际扫到 {total}"

    reasons = {why for _, _, why in suspicious}
    assert any("for" in r for r in reasons), f"没检出 for 遍历: {suspicious}"
    assert any("推导式" in r for r in reasons), f"没检出推导式: {suspicious}"
    assert any("len" in r for r in reasons), f"没检出 len(): {suspicious}"

    # 样本里 3 个坏例子 + 1 个「赋值后再遍历」的正常写法：
    # 只应报 3 个 —— `fine()` 不能被误报（扫描器刻意不做数据流分析）
    assert len(suspicious) == 3, (
        f"预期 3 处可疑（fine() 不该被报），实际 {len(suspicious)} 处: {suspicious}"
    )


# ==================================================== 3. 脚本可独立运行
def test_script_runs_as_main():
    """脚本要能直接 `py tests/audit_config_shape.py` 跑起来（文档里这么写）。"""
    src = _AUDIT.read_text(encoding="utf-8")
    assert '__name__ == "__main__"' in src or "__name__ == '__main__'" in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
