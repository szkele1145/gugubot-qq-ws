# -*- coding: utf-8 -*-
"""绑定与白名单必须「全成功或全不留」（v2.2.10 续）。

问题
----
``bound.py`` 的 ``_bind_player`` 原本是**先落盘玩家数据、后加白名单**：

.. code-block:: python

    self.player_manager.add_player_account(...)   # ← 内部就 save() 了
    self.player_manager.save()
    success = _bound_whitelist(...)               # ← 失败返回 False
    return success

``whitelist.add_player()`` 在三种情况下返回 ``False``：

* 白名单 API 未初始化（MCDR 白名单插件缺失 / 未就绪）
* 底层 ``add_player`` 抛异常
* 玩家**本来就在**白名单里（这一种是良性的）

前两种是**真失败**。此时：

* **玩家数据已经落盘**（绑定记录在 ``players.json`` 里）
* 函数**返回 False** → 用户看到「绑定失败」

→ **不一致状态**：用户以为没绑上，实际数据里已经绑了。他再试一次：

* ``is_name_bound_by_other_user()`` 返回 ``True``
* → 报「该名字已被其他用户绑定」→ **用户永久卡死**（既绑不上、也解不掉）

修复
----
白名单失败时**回滚**玩家数据（``remove_account`` / ``remove_name``；
若该玩家再无任何 account 则整个条目删掉）。

本测试用**两条**互补的断言把它钉住：

1. **运行时**：``Player`` 的回滚方法真实存在且行为正确（不是靠调用方
   手工拼 dict）
2. **AST 静态**：``_bind_player`` 里 ``_bound_whitelist(...)`` 的结果
   必须被检查、且失败路径上有回滚调用 —— 旧代码没有，这条会**失败**。
"""
import ast
import importlib.abc
import importlib.machinery
import pathlib
import sys
import tempfile
import types

import pytest


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
_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "plugin"))

from gugubot.utils.player_manager import Player, PlayerManager


# ==================================================== 1. 回滚方法真实存在
def test_player_has_rollback_methods():
    """``Player`` 必须提供 ``remove_name`` / ``remove_account``。

    没有这两个方法，绑定失败时就只能「整条删玩家」—— 会把该玩家在
    其他平台上的绑定一起抹掉（例如 QQ 绑定失败却把 MC 名字解绑了）。
    """
    assert hasattr(Player, "remove_name"), "Player 缺少 remove_name，无法精确回滚名称"
    assert hasattr(Player, "remove_account"), (
        "Player 缺少 remove_account，失败回滚只能整条删玩家，会误删其他平台的绑定"
    )


def test_remove_account_rolls_back_exactly():
    """``remove_account`` 只删指定的那一条，其他绑定原样保留。"""
    p = Player(name="Alex")
    p.add_account("QQ", "10001")
    p.add_account("QQ", "10002")
    p.add_account("Discord", "d-1")

    p.remove_account("QQ", "10001")
    assert p.accounts["QQ"] == ["10002"], "删错了条目"
    assert p.accounts["Discord"] == ["d-1"], "误删了其他平台的绑定"

    # 删空后该平台键也要清掉（不留空列表，避免落盘噪音）
    p.remove_account("QQ", "10002")
    assert "QQ" not in p.accounts, "删空后留下了空列表"


def test_remove_name_rolls_back_exactly():
    """``remove_name`` 按 java / bedrock 分别精确回滚。"""
    p = Player(name="Alex")
    p.add_name("AlexJava")
    p.add_name("AlexBedrock", is_bedrock=True)

    p.remove_name("AlexJava")
    assert p.java_name == [], "java_name 没删掉"
    assert p.bedrock_name == ["AlexBedrock"], "误删了基岩版名称"

    p.remove_name("AlexBedrock", is_bedrock=True)
    assert p.bedrock_name == []


def test_remove_account_persists_after_save():
    """回滚 + ``save()`` 之后，磁盘上确实没有那条绑定了。"""
    with tempfile.TemporaryDirectory() as td:
        PlayerManager._instances.clear()

        class _Logger:
            def __getattr__(self, n):
                return lambda *a, **k: None

        class _Server:
            logger = _Logger()

            def get_data_folder(self):
                return str(td)

        class _BoundSystem:
            config = types.SimpleNamespace(
                get_keys=lambda path, default=None: default
            )

        pm = PlayerManager.get_instance(_Server(), _BoundSystem())
        pm.add_player_account("Alex", platform="QQ", account_id="10001")

        # 模拟「白名单添加失败 → 回滚」
        p = pm.get_player("10001", platform="QQ")
        assert p is not None
        p.remove_account("QQ", "10001")
        pm.remove_player(p.name)

        # 从磁盘重新读
        PlayerManager._instances.clear()
        pm2 = PlayerManager.get_instance(_Server(), _BoundSystem())
        assert pm2.get_player("10001", platform="QQ") is None, (
            "回滚后磁盘上仍有该绑定 → 用户重试会被判「该名字已被其他用户绑定」"
        )


# ==================================================== 2. 关键：源码必须有回滚
def _find_bind_player_fn():
    src_path = _ROOT / "plugin" / "gugubot" / "logic" / "system" / "bound.py"
    src = src_path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_bind_player":
            return src, node
    raise AssertionError("bound.py 里找不到 _bind_player")


def test_bind_player_rolls_back_when_whitelist_fails():
    """``_bind_player`` 里必须有回滚动作，且在 ``return success`` 之前。

    这是 #74 的核心断言。旧实现是「先落盘 → 调白名单 → 直接 return
    success」，白名单失败时玩家数据已经落盘 → 用户重试被判「已被绑定」
    → 永久卡死。
    """
    src, fn = _find_bind_player_fn()
    fn_src = ast.get_source_segment(src, fn)

    assert fn_src.count("_bound_whitelist(") >= 3, (
        "预期 _bind_player 有三条绑定路径（补名称 / 补账号 / 新玩家）"
    )
    for name in ("remove_account", "remove_name", "remove_player"):
        assert name in fn_src, (
            f"_bind_player 里没有 {name}() —— 白名单添加失败时玩家数据"
            " 已经落盘、不会回滚 → 用户重试被判「该名字已被其他用户绑定」"
            " → 永久卡死"
        )

    # 回滚必须发生在 return success 之前（不能是死代码）
    ret_idx = fn_src.rfind("return success")
    assert ret_idx > 0
    assert any(
        fn_src.find(n) != -1 and fn_src.find(n) < ret_idx
        for n in ("remove_account", "remove_name", "remove_player")
    ), "回滚调用出现在 return 之后（不可达），等于没回滚"


def test_bind_player_checks_whitelist_result():
    """``_bound_whitelist`` 的返回值必须被**据此分支**（不能只赋值）。

    旧代码里 ``success = _bound_whitelist(...)`` 之后直接
    ``return success`` —— 值虽然返回了，但**失败时不回滚**。
    这条断言「返回值后面跟着 ``if not success`` 之类的判断」。
    """
    src, fn = _find_bind_player_fn()
    fn_src = ast.get_source_segment(src, fn)
    assert (
        "if not success" in fn_src
        or "if success is False" in fn_src
        or "if success ==" in fn_src
    ), (
        "_bind_player 没有根据白名单结果（success）做判断 → 失败时不会回滚"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
