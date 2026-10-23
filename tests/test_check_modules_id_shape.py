# -*- coding: utf-8 -*-
"""admin_ids / admin_group_ids 写成标量会把管理员当普通用户（v2.2.10 续，91）。

问题
----
这是 #90 的**同源缺口**。做完 #90 后我按 AST 做了一次配置形态审计（找出
所有「读出来就当列表遍历」的 ``get_keys`` 调用），命中 ``unbound_check.py``
与 ``inactive_check.py`` 各 4 处：

.. code-block:: python

    admin_ids = set(
        str(admin_id)
        for admin_id in self.config.get_keys(
            ["connector", "QQ", "permissions", "admin_ids"], []
        )
    )

    admin_group_ids = self.config.get_keys([...], [])
    for admin_group_id in admin_group_ids:     # 逐字符

配置里写成**标量**时（``admin_ids: 111222333``，少写一行 ``-``）：

* ``admin_ids`` 变成 ``{"1", "1", "2", ...}`` 这样的**字符集合** →
  真实管理员 ID **不在**跳过集合里 → 管理员被当成普通用户：
  * **未绑定检查**：管理员自己收到「请先绑定」提醒；
  * **不活跃检查**：管理员被判定为不活跃（可能触发移除/警告）。
* ``admin_group_ids`` 被逐字符遍历 → 对 ``"1"``、``"2"``…… 逐个发
  ``get_group_member_list`` → 每个都失败 → **日志刷屏**。

管理员是最不该被打扰的人群，而这只需要用户少写一个 ``-``。

修复
----
8 处读取全部改用 ``as_id_list()``（#90 新增的归一化助手）。

测试手法
--------
行为测试跑**真实路径**（``unbound_check._check_unbound_users``），只把
连接器与玩家管理器换成桩：
* 标量 ``admin_ids="111"`` 时，成员里 user_id 为 ``"111"`` 的管理员
  **必须被跳过**（旧代码会把字符集 ``{"1"}`` 当跳过集合 → 管理员被报未绑定）；
* 标量 ``admin_group_ids="999"`` 时，``get_group_member_list`` 收到的
  群号必须是 ``"999"`` 整体，而不是 ``"9"``（逐字符）。
"""
import asyncio
import importlib.abc
import importlib.machinery
import pathlib
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


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

_MOD = "gugubot.logic.plugins.unbound_check"
sys.modules.pop(_MOD, None)
from gugubot.logic.plugins import unbound_check  # noqa: E402


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


def _make_system(config_map):
    inst = unbound_check.UnboundCheckSystem.__new__(
        unbound_check.UnboundCheckSystem
    )
    inst.name = "unbound_check"
    inst.logger = _Logger()
    inst._checking = False
    inst.save = lambda: None

    class _Config:
        def get_keys(self, path, default=None):
            cur = config_map
            for p in path:
                if not isinstance(cur, dict) or p not in cur:
                    return default
                cur = cur[p]
            return cur

    inst.config = _Config()

    # 连接器桩：记录被查询的群号，按群号返回成员列表
    calls = []

    async def _get_group_member_list(group_id=None, **kw):
        calls.append(str(group_id))
        if str(group_id) == "g1":
            return {
                "status": "ok",
                "data": [
                    {"user_id": "111", "join_time": 0, "nickname": "admin"},
                    {"user_id": "222", "join_time": 0, "nickname": "normal"},
                ],
            }
        return {"status": "ok", "data": []}

    inst.qq_connector = types.SimpleNamespace(
        bot=types.SimpleNamespace(get_group_member_list=_get_group_member_list)
    )

    class _PM:
        def get_player(self, identifier, platform=None, name_only=False):
            return None      # 谁都没绑定

    inst.bound_system = types.SimpleNamespace(player_manager=_PM())

    # BasicConfig 的 dict 行为（实例是 dict 子类，__new__ 已给出空字典）
    return inst, calls


def _run(coro):
    return asyncio.run(coro)


# ==================================================== 1. 标量 admin_ids
def test_scalar_admin_ids_skips_real_admin():
    """``admin_ids="111"`` 时，user_id 为 "111" 的管理员必须被跳过。"""
    inst, calls = _make_system(
        {
            "connector": {
                "QQ": {
                    "source_name": "QQ",
                    "permissions": {
                        "admin_ids": "111",          # ← 标量（少写一个 "-"）
                        "admin_group_ids": [],
                        "group_ids": ["g1"],
                    },
                }
            },
            "system": {"unbound_check": {"timeout_days": 0}},
        }
    )

    result = _run(inst._check_unbound_users())
    reported = [u["user_id"] for users in result.values() for u in users]

    assert "111" not in reported, (
        "管理员被当成普通用户报成「未绑定」—— admin_ids 写成标量时被逐字符"
        "拆成了字符集合 {'1'}，真实管理员不在跳过集合里"
    )
    assert "222" in reported, "普通未绑定用户应该被报出来（测试前提）"


# ==================================================== 2. 标量 admin_group_ids
def test_scalar_admin_group_ids_queried_as_whole():
    """``admin_group_ids="999"`` 必须整体查询，不能逐字符。"""
    inst, calls = _make_system(
        {
            "connector": {
                "QQ": {
                    "source_name": "QQ",
                    "permissions": {
                        "admin_ids": [],
                        "admin_group_ids": "999",    # ← 标量
                        "group_ids": ["g1"],
                    },
                }
            },
            "system": {"unbound_check": {"timeout_days": 0}},
        }
    )

    _run(inst._check_unbound_users())

    assert "999" in calls, (
        f"管理群没有按整体群号查询，实际查询序列: {calls} —— "
        f"admin_group_ids 写成标量时被逐字符遍历，对每个字符都发一次请求"
        f"（每个都失败 → 日志刷屏）"
    )
    assert "9" not in calls, (
        f"出现了对单个字符 '9' 的群查询，实际: {calls} —— 逐字符遍历"
    )


# ==================================================== 3. 列表配置照常工作
def test_list_config_still_works():
    inst, calls = _make_system(
        {
            "connector": {
                "QQ": {
                    "source_name": "QQ",
                    "permissions": {
                        "admin_ids": ["111"],
                        "admin_group_ids": ["999"],
                        "group_ids": ["g1"],
                    },
                }
            },
            "system": {"unbound_check": {"timeout_days": 0}},
        }
    )

    result = _run(inst._check_unbound_users())
    reported = [u["user_id"] for users in result.values() for u in users]
    assert "111" not in reported
    assert "222" in reported
    assert "999" in calls and "9" not in calls


# ==================================================== 4. 静态：8 处全部归一化
def test_all_id_reads_normalized():
    """两个模块里所有 id 列表读取都必须走 as_id_list。"""
    for name in ("unbound_check.py", "inactive_check.py"):
        src = (_ROOT / "plugin" / "gugubot" / "logic" / "plugins" / name
               ).read_text(encoding="utf-8-sig")
        count = src.count("as_id_list(")
        assert count >= 4, (
            f"{name} 里只有 {count} 处 as_id_list —— 该文件有 4 处 id 列表"
            f"读取（group_ids / admin_group_ids ×2 / admin_ids）都应归一化"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
