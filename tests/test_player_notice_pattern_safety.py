# -*- coding: utf-8 -*-
"""玩家进出正则写错不能炸掉事件回调（v2.2.10 续，84）。

问题
----
``logic/plugins/player_notice.py`` 的 ``_match_player_name``：

.. code-block:: python

    def _match_player_name(content, patterns):
        for pattern in patterns:
            if match := re.search(pattern, content):      # ← 无 re.error 保护
                ...

``player_join_patterns`` / ``player_left_patterns`` 是**用户可编辑的正则
表达式**（``default_config.yml`` 里明确写着「正则表达式模式」）。用户在
配置里写错一个括号（``"([^ ]+ joined the game"``）就会：

* ``re.search`` 抛 ``re.error``；
* 异常**没有人接**（``on_player_join`` / ``on_player_left`` 都是 MCDR
  事件回调，没有 try/except）→ MCDR 记一条带 traceback 的错误；
* **每次**有玩家进出都再炸一次 → 日志刷屏，而进出播报功能直接死掉。

同一模块的 ``is_bot()`` 早就防住了同样的问题（``except re.error:
continue``），只有 ``_match_player_name`` 漏了 —— 模块内部自相不一致。

修复
----
1. ``_match_player_name`` 捕获 ``re.error`` 并**跳到下一个 pattern**；
2. 新增可选 ``logger`` 参数，在 pattern 非法时打一条 **warning** 点名
   那个 pattern —— 否则用户只看到"播报不工作"，却不知道是配置写错了。
   两个调用点都传入 ``server.logger``。
"""
import importlib.abc
import importlib.machinery
import pathlib
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_MOD_FILE = _ROOT / "plugin" / "gugubot" / "logic" / "plugins" / "player_notice.py"


class _A:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return _A()
    def __call__(self, *a, **k): return _A()


class _SM(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        v = _A()
        setattr(self, name, v)
        return v


class _L(importlib.abc.Loader):
    def create_module(self, s): return _SM(s.name)
    def exec_module(self, m): pass


class _F:
    def find_spec(self, f, p=None, t=None):
        if f.startswith("mcdreforged") or f.startswith("packaging"):
            return importlib.machinery.ModuleSpec(f, _L(), is_package=True)


sys.meta_path.insert(0, _F())
sys.path.insert(0, str(_ROOT / "plugin"))

_MOD = "gugubot.logic.plugins.player_notice"
sys.modules.pop(_MOD, None)
from gugubot.logic.plugins import player_notice  # noqa: E402


class _Logger:
    def __init__(self):
        self.records = []

    def _add(self, level, msg, *a):
        try:
            text = msg % a if a else str(msg)
        except Exception:
            text = f"{msg} {a}"
        self.records.append((level, text))

    def debug(self, m, *a): self._add("debug", m, *a)
    def info(self, m, *a): self._add("info", m, *a)
    def warning(self, m, *a): self._add("warning", m, *a)
    def error(self, m, *a): self._add("error", m, *a)

    def levels(self):
        return [lvl for lvl, _ in self.records]

    def text(self):
        return "\n".join(t for _, t in self.records)


# ==================================================== 1. 非法正则不能抛
def test_invalid_pattern_does_not_raise():
    """写错的正则必须被跳过去，而不是把事件回调整个炸掉。"""
    match = player_notice._match_player_name(
        "Steve joined the game",
        ["([^ ]+ joined the game"],      # 少一个右括号
    )
    assert match is None, "非法正则应该被跳过（返回 None），这里却匹配到了"


def test_invalid_pattern_then_valid_still_matches():
    """非法 pattern 之后的合法 pattern 仍必须生效（不能整轮放弃）。"""
    match = player_notice._match_player_name(
        "Steve joined the game",
        ["([^ ]+", "([^ ]+) joined the game"],   # 第一个非法，第二个合法
    )
    assert match == "Steve"


def test_valid_pattern_unchanged():
    """正常路径不能被改坏。"""
    assert (
        player_notice._match_player_name(
            "Steve joined the game", ["([^\\s]+) joined the game"]
        )
        == "Steve"
    )
    # 默认配置里的两条 left 模式
    assert (
        player_notice._match_player_name(
            "Steve[/127.0.0.1] left the game", ["([^\\[]+)\\[.*?\\] left the game"]
        )
        == "Steve"
    )


# ==================================================== 2. 非法 pattern 要告警
def test_invalid_pattern_logs_warning():
    """配置写错必须留下 warning 点名那个 pattern，否则用户只看到"功能不工作"。"""
    logger = _Logger()
    player_notice._match_player_name(
        "Steve joined the game",
        ["([^ ]+"],
        logger=logger,
    )
    assert "warning" in logger.levels(), (
        "正则非法却没有任何 warning —— 用户只会看到进出播报不工作，"
        "完全想不到是配置里的正则写错了"
    )
    assert "([^ ]+" in logger.text(), "警告里没有点名出错的 pattern"


def test_no_logger_still_safe():
    """不传 logger 时也不能崩（向后兼容）。"""
    assert player_notice._match_player_name("x", ["("]) is None


# ==================================================== 3. 静态：必须有 re.error 保护
def test_match_player_name_guards_re_error():
    import ast

    # 用 utf-8-sig 读：源码若带 BOM，ast.parse 会因 U+FEFF 报 SyntaxError
    # （这不是逻辑问题，但会让本断言假失败）
    src = _MOD_FILE.read_text(encoding="utf-8-sig")
    tree = ast.parse(src)

    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_match_player_name":
            fn = node
            break
    assert fn is not None, "找不到 _match_player_name"

    has_re_error = False
    for node in ast.walk(fn):
        if isinstance(node, ast.ExceptHandler) and node.type is not None:
            seg = ast.get_source_segment(src, node) or ""
            if "re.error" in seg:
                has_re_error = True
                break
    assert has_re_error, (
        "_match_player_name 没有捕获 re.error —— 用户在配置里写错正则会让"
        "**每一次**玩家进出都抛未捕获异常（MCDR 事件回调没有 try/except），"
        "日志刷屏且播报功能直接死掉"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
