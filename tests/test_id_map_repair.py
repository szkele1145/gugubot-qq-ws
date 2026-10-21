# -*- coding: utf-8 -*-
"""ID 映射脏数据自愈（v2.2.10 续）。

背景
----
``QQIDMapper._load`` 从 JSON 恢复映射时：

1. 若文件缺 ``next_user`` / ``next_group`` 字段（旧版文件或手工编辑过），
   计数器会回落到起始号，而映射表里**早已占用**这些号 → 之后的新 openid
   被分到**重复的虚拟号**。项目里已为此加了「按已有最大号 +1 兜底」。

2. 但**已经落盘的脏数据**（多个 openid 共用同一虚拟号）从来没有被修复过：

   ```python
   self._user_rev = {v: k for k, v in self._user_map.items()}
   ```

   字典推导式「后写覆盖先写」→ 冲突时反向表只保留**最后一个** openid
   → 其余 openid 的虚拟号指向**别人**。下游（bound / echo / 投票 / 各种
   ``to_official_user`` 调用）拿虚拟号去解析真实 openid 时，**消息发给
   错的人**，而且全程没有任何报错或日志。

修复：加载时先修复冲突（先到者保留原号，后到者重新分配），**再**建
反向表。本文件覆盖该自愈逻辑。
"""
import sys
import json
import pathlib
import types
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

from gugubot.connector.qq_official import id_map as im


class FL:
    def __init__(self):
        self.warnings = []
        self.infos = []
        self.debugs = []

    def debug(self, m, *a, **k): self.debugs.append(str(m))
    def info(self, m, *a, **k): self.infos.append(str(m))
    def warning(self, m, *a, **k): self.warnings.append(str(m))
    def error(self, m, *a, **k): self.warnings.append(str(m))


def _write_map(tmp_path, payload):
    p = tmp_path / "id_map.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


# ============================================ 1. 用户号冲突自愈
def test_duplicate_user_ids_are_repaired(tmp_path):
    """回归：两个 openid 共用同一虚拟号 → 加载时必须重新分配，不能塌缩。

    旧实现：``_user_rev = {v: k for k, v in _user_map.items()}``
    → 后写覆盖先写 → 只有最后一个 openid 能被反向解析，其余 openid
    的消息会发给**最后一个**那个人。
    """
    path = _write_map(tmp_path, {
        "user_map": {"openidA": "10001", "openidB": "10001"},
        "group_map": {},
        "next_user": 10002,
        "next_group": 20001,
    })
    logger = FL()
    mapper = im.QQIDMapper(data_file=path, logger=logger)

    a = mapper.user_legacy_id("openidA")
    b = mapper.user_legacy_id("openidB")
    assert a != b, f"两个 openid 必须拿到不同虚拟号，实际 {a} / {b}"
    # 双向解析必须一致（这是「不发错人」的核心保证）
    assert mapper.user_openid(a) == "openidA"
    assert mapper.user_openid(b) == "openidB"
    assert any("自愈" in w for w in logger.warnings), "应留下自愈日志"


def test_duplicate_group_ids_are_repaired(tmp_path):
    """群号同理：两个 group_openid 共用同一虚拟群号必须修掉。"""
    path = _write_map(tmp_path, {
        "user_map": {},
        "group_map": {"gA": "20001", "gB": "20001"},
        "next_user": 10001,
        "next_group": 20002,
    })
    logger = FL()
    mapper = im.QQIDMapper(data_file=path, logger=logger)

    ga = mapper.to_legacy_group("gA")
    gb = mapper.to_legacy_group("gB")
    assert ga != gb, f"两个群必须拿到不同虚拟号，实际 {ga} / {gb}"
    assert mapper.to_official_group(ga) == "gA"
    assert mapper.to_official_group(gb) == "gB"


def test_first_wins_and_legal_mappings_untouched(tmp_path):
    """先到者保留原号；无冲突的映射一律不动。"""
    path = _write_map(tmp_path, {
        # 插入顺序：okA(10001) → dup(10001) → okB(10002)
        "user_map": {"okA": "10001", "dup": "10001", "okB": "10002"},
        "group_map": {},
        "next_user": 10003,
        "next_group": 20001,
    })
    mapper = im.QQIDMapper(data_file=path, logger=FL())
    assert mapper.user_legacy_id("okA") == "10001", "先到者应保留原号"
    assert mapper.user_legacy_id("okB") == "10002", "无冲突映射不得被改动"
    assert mapper.user_legacy_id("dup") not in ("10001", "10002")


def test_repair_is_persisted(tmp_path):
    """自愈结果必须落盘，否则每次启动都要重跑（期间消息仍会发错人）。"""
    path = _write_map(tmp_path, {
        "user_map": {"a": "10001", "b": "10001"},
        "group_map": {},
        "next_user": 10002,
        "next_group": 20001,
    })
    mapper = im.QQIDMapper(data_file=path, logger=FL())
    saved = json.loads(path.read_text(encoding="utf-8"))
    values = list(saved["user_map"].values())
    assert len(values) == len(set(values)), f"落盘后仍有重复号: {saved['user_map']}"


def test_repair_advances_counter(tmp_path):
    """重新分配后计数器必须前移，避免下次又撞号。"""
    path = _write_map(tmp_path, {
        "user_map": {"a": "10001", "b": "10001"},
        "group_map": {},
        "next_user": 10002,
        "next_group": 20001,
    })
    mapper = im.QQIDMapper(data_file=path, logger=FL())
    # 新 openid 应当拿到一个全新的号，不与任何已有号冲突
    existing = {mapper.user_legacy_id("a"), mapper.user_legacy_id("b")}
    fresh = mapper.user_legacy_id("newcomer")
    assert fresh not in existing, f"新号 {fresh} 与已有 {existing} 冲突"


def test_no_repair_when_clean(tmp_path):
    """干净数据不触发自愈，也不产生多余日志/写盘。"""
    path = _write_map(tmp_path, {
        "user_map": {"a": "10001", "b": "10002"},
        "group_map": {"g": "20001"},
        "next_user": 10003,
        "next_group": 20002,
    })
    logger = FL()
    mtime_before = path.stat().st_mtime_ns
    mapper = im.QQIDMapper(data_file=path, logger=logger)
    assert not any("自愈" in w for w in logger.warnings)
    assert mapper.user_openid("10001") == "a"
    assert mapper.user_openid("10002") == "b"
    # 未修复则不应写盘（mtime 不变或仅被读取）
    assert path.stat().st_mtime_ns == mtime_before


def test_three_way_duplicate(tmp_path):
    """三个 openid 共用一个号：全部要修，且两两不同。"""
    path = _write_map(tmp_path, {
        "user_map": {"x": "10001", "y": "10001", "z": "10001"},
        "group_map": {},
        "next_user": 10002,
        "next_group": 20001,
    })
    mapper = im.QQIDMapper(data_file=path, logger=FL())
    ids = [mapper.user_legacy_id(k) for k in ("x", "y", "z")]
    assert len(set(ids)) == 3, f"三个 openid 应拿到三个不同号，实际 {ids}"
    for k, v in zip(("x", "y", "z"), ids):
        assert mapper.user_openid(v) == k


def test_roundtrip_after_repair(tmp_path):
    """自愈 → 落盘 → 重新加载，映射关系必须稳定不变（幂等）。"""
    path = _write_map(tmp_path, {
        "user_map": {"a": "10001", "b": "10001"},
        "group_map": {},
        "next_user": 10002,
        "next_group": 20001,
    })
    m1 = im.QQIDMapper(data_file=path, logger=FL())
    first = {k: m1.user_legacy_id(k) for k in ("a", "b")}

    m2 = im.QQIDMapper(data_file=path, logger=FL())
    second = {k: m2.user_legacy_id(k) for k in ("a", "b")}
    assert first == second, f"重新加载后映射变了: {first} -> {second}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
