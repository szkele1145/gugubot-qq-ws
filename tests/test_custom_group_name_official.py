# -*- coding: utf-8 -*-
"""官方模式下 ``custom_group_name`` 按真实群号配置必须生效（v2.2.10 续）。

背景
----
「改造说明.md」的「群号绑定流程（旧配置无感迁移）」第 3 条承诺：

    此后该群显示为配置里的真实群号，``custom_group_name`` 等
    按群号配置的功能照常生效；

但 ``mc_connector.py`` 的实现是 ``custom_group_name.get(source_id, source)``
—— 官方模式下 ``source_id`` 是**虚拟群号**（12 位 200000xxx），而用户
在 ``custom_group_name`` 里填的键是**真实 QQ 群号**（旧配置）→
**永远匹配不上** → 自定义群名静默失效，游戏里显示的一直是「QQ」。

修复
----
``MCConnector._map_custom_group_name()``：先按 ``source_id`` 直接查表
（OneBot 模式 / 用户直接填虚拟号的场景），查不到再取官方连接器的
``id_mapper``，把 ``source_id`` 与配置里每个键都翻成 ``group_openid``
再比对 —— 与 ``echo.py`` / ``bound_notice.py`` / ``bound.py`` 同一套
「统一翻 openid 再比对」的修法。
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

from gugubot.connector import mc_connector as mm


# 虚拟群号 200000000001 与真实群号 1103662998 都指向同一个 openid
_OPENID_MAP = {
    "200000000001": "GRP-openid-1",
    "1103662998": "GRP-openid-1",     # 已绑定：真实群号 ↔ openid
}


def _mk_mc(connector_for_source):
    """构造最小 MCConnector（只测 _map_custom_group_name）。

    connector_for_source: get_connector() 的返回值（QQ 连接器或 None）。
    """
    mc = mm.MCConnector.__new__(mm.MCConnector)
    mc.connector_manager = types.SimpleNamespace(
        get_connector=lambda name: connector_for_source
    )
    return mc


def _official_connector():
    mapper = types.SimpleNamespace(
        to_official_group=lambda lid: _OPENID_MAP.get(str(lid), f"unknown-{lid}"),
    )
    return types.SimpleNamespace(id_mapper=mapper)


# ================================================ 1. 直接命中（回归保护）
def test_direct_key_match_onebot():
    """OneBot 模式：source_id 就是真实群号 → 直接查表命中。"""
    mc = _mk_mc(None)  # OneBot 无 id_mapper
    cfg = {"1103662998": "生存服大群"}
    out = mc._map_custom_group_name(cfg, "1103662998", "QQ", "QQ")
    assert out == "生存服大群"


def test_direct_key_match_virtual():
    """用户直接填虚拟号当键 → 直接命中。"""
    mc = _mk_mc(_official_connector())
    cfg = {"200000000001": "生存服大群"}
    out = mc._map_custom_group_name(cfg, "200000000001", "QQ", "QQ")
    assert out == "生存服大群"


# ================================================ 2. 官方模式虚实号混叠
def test_real_number_key_matches_virtual_source_id():
    """回归：配置键=真实群号、source_id=虚拟群号 → 必须命中。

    这是本 bug 的核心场景：官方模式下消息的 source_id 是虚拟群号，
    用户按旧配置填的 custom_group_name 键是真实群号 → 旧实现永远
    查不到 → 自定义群名静默失效。
    """
    mc = _mk_mc(_official_connector())
    cfg = {"1103662998": "生存服大群"}  # 键 = 真实群号
    out = mc._map_custom_group_name(cfg, "200000000001", "QQ", "QQ")
    assert out == "生存服大群", (
        f"官方模式：真实群号键应匹配虚拟群号 source_id，得到 {out!r}"
    )


def test_no_match_falls_back_to_source():
    """群号不在 custom_group_name 里 → 回退原始 source。"""
    mc = _mk_mc(_official_connector())
    cfg = {"999888777": "别的群"}
    out = mc._map_custom_group_name(cfg, "200000000001", "QQ", "QQ")
    assert out == "QQ"


def test_empty_config_falls_back():
    """custom_group_name 未配置 → 回退 source。"""
    mc = _mk_mc(_official_connector())
    assert mc._map_custom_group_name({}, "200000000001", "QQ", "QQ") == "QQ"
    assert mc._map_custom_group_name(None, "200000000001", "QQ", "QQ") == "QQ"


# ================================================ 3. OneBot 行为不变
def test_onebot_no_mapper_falls_back():
    """OneBot 模式（连接器无 id_mapper）查不到 → 回退 source，不抛错。"""
    mc = _mk_mc(types.SimpleNamespace())  # 连接器存在但无 id_mapper
    cfg = {"1103662998": "生存服大群"}
    out = mc._map_custom_group_name(cfg, "1103662998", "QQ", "QQ")
    # 直接命中先行返回，无需 id_mapper
    assert out == "生存服大群"


def test_onebot_miss_stays_source():
    """OneBot 模式查不到键 → 回退（不会因无 id_mapper 抛 AttributeError）。"""
    mc = _mk_mc(None)
    cfg = {"1103662998": "生存服大群"}
    out = mc._map_custom_group_name(cfg, "888888", "QQ", "QQ")
    assert out == "QQ"


# ================================================ 4. 健壮性
def test_mapper_exception_falls_back():
    """id_mapper 翻译抛异常 → 回退 source，绝不向上抛。"""
    bad = types.SimpleNamespace(
        id_mapper=types.SimpleNamespace(
            to_official_group=lambda lid: (_ for _ in ()).throw(RuntimeError("boom"))
        )
    )
    mc = _mk_mc(bad)
    cfg = {"1103662998": "生存服大群"}
    out = mc._map_custom_group_name(cfg, "200000000001", "QQ", "QQ")
    assert out == "QQ"


def test_connector_missing_falls_back():
    """QQ 连接器不存在（改名后取不到）→ 回退 source。"""
    mc = _mk_mc(None)
    cfg = {"1103662998": "生存服大群"}
    out = mc._map_custom_group_name(cfg, "200000000001", "QQ", "QQ")
    assert out == "QQ"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
