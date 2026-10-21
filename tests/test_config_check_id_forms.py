# -*- coding: utf-8 -*-
"""官方模式配置检查的 ID 形态判定（v2.2.10 续）。

背景
----
``config_check.py`` 在官方（official）直连模式下会检查三类 ID 配置的形态。
旧实现把 **admin_ids / admin_group_ids / group_ids** 一律按
「必须是 12 位虚拟号（100000xxx / 200000xxx）」检查 —— 这是**误报**：

- **group_ids**：官方 openid 不透明，插件内部用「虚拟号」代表群，但
  ``id_mapper.register_legacy_groups()`` 的**槽位绑定机制**就是为
  「用户在配置里填**真实 QQ 群号**」设计的 —— 填了之后，群内 @机器人
  说句话即自动绑定该 openid，实现旧配置无感迁移。
- **admin_group_ids**：群过滤逻辑（``echo.py`` / ``bound_notice.py``）
  明确按「source_id 是虚拟群号，admin_group_ids 是配置里的真实群号」
  处理（见 bound_notice.py 的注释）。

也就是说，用户**按官方文档填真实群号是完全正确的用法**，却被检查
命令告知「疑似非虚拟号 ❌」→ 用户以为自己配错了，反复折腾。

而 **admin_ids** 确实必须填虚拟用户号：它与「玩家绑定账号」比对
（``player_manager.py`` 里 ``str(account_id) in admin_ids + ...``），
官方模式下用户拿不到自己的真实 QQ 号 → 填真实号永远匹配不上。

修复
----
- ``admin_ids``：保持严格虚拟号检查（且只接受 100000xxx 用户号段）；
- ``group_ids`` / ``admin_group_ids``：改为**三态提示** ——
  虚拟号 ✅ / 真实群号 ✅（提示需在群内 @机器人 自动绑定）/
  非数字且非虚拟号 → ❌（无法识别的条目）。
"""
import sys
import types
import pathlib
import importlib.abc
import importlib.machinery

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
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "plugin"))

from gugubot.logic.system import config_check as ccm


class FakeConfig:
    def __init__(self, data):
        self._data = data

    def get_keys(self, path, default=None):
        cur = self._data
        for p in path:
            if not isinstance(cur, dict) or p not in cur:
                return default
            cur = cur[p]
        return cur


class FakeMapper:
    """鸭子类型的官方 id_mapper（duck typing 只要有 id_mapper 属性即可）。"""
    def known_legacy_groups(self):
        return set()


def _mk_checker(permissions, conn_type="official"):
    sysm = ccm.ConfigCheckSystem.__new__(ccm.ConfigCheckSystem)
    cfg = {
        "connector": {
            "QQ": {
                "connection": {
                    "type": conn_type,
                    "official": {"appid": "123", "secret": "s", "intents": {"a": 1}},
                },
                "permissions": permissions,
            }
        }
    }
    sysm.config = FakeConfig(cfg)
    sysm.logger = None
    # 造一个带 id_mapper 的假连接器 → 触发官方专项检查
    conn = types.SimpleNamespace(
        id_mapper=FakeMapper(),
        ws_client=types.SimpleNamespace(is_connected=lambda: True),
    )
    sysm._connector_manager = types.SimpleNamespace(connectors=[conn])
    return sysm


def _run(sysm):
    return sysm._check_qq_connector()


def _line_for(lines, label):
    for ln in lines:
        if ln.startswith(label + ":"):
            return ln
    return None


# ================================================ 1. 真实群号不再误报
def test_group_ids_with_real_group_number_is_not_flagged():
    """回归：group_ids 填真实 QQ 群号 → 不应报 ❌。"""
    sysm = _mk_checker({"group_ids": ["123456789"], "admin_ids": ["100000000001"]})
    res = _run(sysm)
    ln = _line_for(res["lines"], "group_ids")
    assert ln is not None
    assert "❌" not in ln, f"真实群号不应被判错: {ln}"
    assert res["ok"] is True, f"整体不应失败: {res['lines']}"


def test_admin_group_ids_with_real_group_number_is_not_flagged():
    """回归：admin_group_ids 填真实 QQ 群号 → 不应报 ❌。"""
    sysm = _mk_checker(
        {"group_ids": ["123456789"], "admin_group_ids": ["987654321"],
         "admin_ids": ["100000000001"]}
    )
    res = _run(sysm)
    ln = _line_for(res["lines"], "admin_group_ids")
    assert ln is not None
    assert "❌" not in ln, f"真实群号不应被判错: {ln}"
    assert res["ok"] is True


def test_real_group_number_hint_mentions_binding():
    """填真实群号时应提示「需在群内 @机器人 完成自动绑定」。"""
    sysm = _mk_checker({"group_ids": ["123456789"], "admin_ids": ["100000000001"]})
    res = _run(sysm)
    ln = _line_for(res["lines"], "group_ids")
    assert "自动绑定" in ln, f"应提示自动绑定: {ln}"


# ================================================ 2. admin_ids 仍严格
def test_admin_ids_with_real_qq_number_still_flagged():
    """admin_ids 填真实 QQ 号必须仍报错（官方模式拿不到真实 QQ 号）。"""
    sysm = _mk_checker({"group_ids": ["123456789"], "admin_ids": ["12345678"]})
    res = _run(sysm)
    ln = _line_for(res["lines"], "admin_ids")
    assert ln is not None
    assert "❌" in ln, f"admin_ids 填真实号应报错: {ln}"
    assert res["ok"] is False


def test_admin_ids_with_openid_still_flagged():
    """admin_ids 填 openid 原文必须报错。"""
    sysm = _mk_checker(
        {"group_ids": ["123456789"], "admin_ids": ["ABCDEF0123456789ABCDEF0123456789"]}
    )
    res = _run(sysm)
    ln = _line_for(res["lines"], "admin_ids")
    assert "❌" in ln


def test_admin_ids_with_virtual_user_id_ok():
    """admin_ids 填虚拟用户号（100000xxx）→ 通过。"""
    sysm = _mk_checker({"group_ids": ["123456789"], "admin_ids": ["100000000001"]})
    res = _run(sysm)
    ln = _line_for(res["lines"], "admin_ids")
    assert ln is None or "❌" not in ln, f"虚拟号应通过: {ln}"


def test_admin_ids_with_group_virtual_id_flagged():
    """admin_ids 填群虚拟号段（200000xxx）必须报错——那是群号段，不是用户号。

    管理员是「人」，人绑定在 100000xxx 用户号段；200000xxx 是群虚拟号段，
    填进去永远不会与任何玩家绑定账号匹配 → 管理员判定静默失效。
    """
    sysm = _mk_checker({"group_ids": ["123456789"], "admin_ids": ["200000000001"]})
    res = _run(sysm)
    ln = _line_for(res["lines"], "admin_ids")
    assert ln is not None and "❌" in ln, f"群号段应报错: {ln}"
    assert res["ok"] is False


# ================================================ 3. 群类配置三态
def test_group_ids_with_virtual_group_id_ok():
    """group_ids 填虚拟群号（200000xxx）→ 通过。"""
    sysm = _mk_checker({"group_ids": ["200000000001"], "admin_ids": ["100000000001"]})
    res = _run(sysm)
    ln = _line_for(res["lines"], "group_ids")
    assert ln is None or "❌" not in ln, f"虚拟群号应通过: {ln}"


def test_group_ids_with_openid_text_ok():
    """group_ids 填 openid 原文（用户直接填 openid）→ 通过。"""
    sysm = _mk_checker(
        {"group_ids": ["ABCDEF0123456789ABCDEF0123456789"],
         "admin_ids": ["100000000001"]}
    )
    res = _run(sysm)
    ln = _line_for(res["lines"], "group_ids")
    assert ln is None or "❌" not in ln, f"openid 原文应通过: {ln}"


def test_group_ids_mixed_real_and_virtual_ok():
    """真实群号 + 虚拟号混填 → 通过并说明两者数量。"""
    sysm = _mk_checker(
        {"group_ids": ["123456789", "200000000001"], "admin_ids": ["100000000001"]}
    )
    res = _run(sysm)
    ln = _line_for(res["lines"], "group_ids")
    assert ln is not None
    assert "❌" not in ln
    assert "1 个虚拟号" in ln and "1 个真实群号" in ln, f"应说明两类数量: {ln}"


def test_group_ids_with_garbage_flagged():
    """既非数字也非已知形态的条目 → 报错。"""
    sysm = _mk_checker(
        {"group_ids": ["not-a-valid-id"], "admin_ids": ["100000000001"]}
    )
    res = _run(sysm)
    ln = _line_for(res["lines"], "group_ids")
    assert ln is not None
    assert "❌" in ln, f"无法识别的条目应报错: {ln}"


# ================================================ 4. 非官方模式不受影响
def test_onebot_mode_skips_official_id_checks():
    """OneBot 模式：不做官方 ID 形态检查（真实 QQ 号完全正常）。"""
    sysm = _mk_checker(
        {"group_ids": ["123456789"], "admin_ids": ["12345678"]}, conn_type="onebot"
    )
    res = _run(sysm)
    assert res["ok"] is True
    assert _line_for(res["lines"], "admin_ids") is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
