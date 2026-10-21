# -*- coding: utf-8 -*-
"""官方模式手动群绑定命令（``#绑群`` / ``#解绑群`` / ``#群绑定``）测试。

背景
----
``bind_group_openid()`` 是「先到先得抢槽位」：配置里写了 N 个真实群号时，
**哪个群先 @ 机器人就抢到哪个槽位**。官方 API 不返回真实 QQ 群号（隐私
设计），插件无从自动对应 —— 猜错就是**群号错位**：``forward_group_ids``
里的号会把消息发到**别的群**，且全程无任何报错。

本组测试覆盖：
- ``QQIDMapper.bind_group_manually()`` 的全部语义（新绑 / 改绑 / 换绑 /
  重绑同号 / 无效参数）；
- ``unbind_group()`` / ``list_group_bindings()``；
- 三个命令的权限门槛与非群聊拒绝。
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

from gugubot.connector.qq_official.id_map import QQIDMapper


# ============================================================ mapper 层
class TestBindGroupManually:
    def _mapper(self, tmp_path):
        return QQIDMapper(data_file=tmp_path / "ids.json")

    def test_new_bind(self, tmp_path):
        m = self._mapper(tmp_path)
        ok, msg = m.bind_group_manually("grp-oid-A", "123456789")
        assert ok
        assert m.legacy_group_display_id("grp-oid-A") == "123456789"
        assert m.to_legacy_group("grp-oid-A") == "123456789"

    def test_bind_same_idempotent(self, tmp_path):
        m = self._mapper(tmp_path)
        m.bind_group_manually("grp-oid-A", "123456789")
        ok, msg = m.bind_group_manually("grp-oid-A", "123456789")
        assert ok
        assert "无需重复绑定" in msg

    def test_rebind_changes_legacy(self, tmp_path):
        """改绑：同一 openid 换一个真实群号。"""
        m = self._mapper(tmp_path)
        m.bind_group_manually("grp-oid-A", "111111")
        ok, msg = m.bind_group_manually("grp-oid-A", "222222")
        assert ok
        assert m.legacy_group_display_id("grp-oid-A") == "222222"
        # 旧群号释放
        assert "111111" not in m._legacy_group_openid

    def test_steal_from_other_openid(self, tmp_path):
        """换绑：目标群号已被别的 openid 占用 → 旧绑定摘除。"""
        m = self._mapper(tmp_path)
        # 先分配虚拟号（虚拟号是 group_legacy_id 惰性分配的）
        virtual_a = m.group_legacy_id("grp-oid-A")
        m.bind_group_manually("grp-oid-A", "123456789")
        ok, msg = m.bind_group_manually("grp-oid-B", "123456789")
        assert ok
        # A 的绑定被摘掉
        assert m.legacy_group_display_id("grp-oid-A") is None
        assert m.legacy_group_display_id("grp-oid-B") == "123456789"
        # A 的虚拟号映射不被破坏（永久映射，号永不复用）
        assert m._group_map.get("grp-oid-A") == virtual_a

    def test_virtual_map_survives(self, tmp_path):
        """绑真实号前后，openid 的虚拟号映射保持存在（永不复用）。"""
        m = self._mapper(tmp_path)
        virtual_before = m.group_legacy_id("grp-oid-A")
        m.bind_group_manually("grp-oid-A", "123456789")
        assert m._group_map.get("grp-oid-A") == virtual_before

    def test_non_numeric_rejected(self, tmp_path):
        m = self._mapper(tmp_path)
        ok, msg = m.bind_group_manually("grp-oid-A", "abc123")
        assert not ok
        assert "纯数字" in msg

    def test_empty_args(self, tmp_path):
        m = self._mapper(tmp_path)
        ok, msg = m.bind_group_manually("grp-oid-A", "")
        assert not ok
        ok, msg = m.bind_group_manually("", "123456789")
        assert not ok

    def test_pending_slot_consumed(self, tmp_path):
        """手动绑定的群号若在待绑定槽位里，应被消费掉。"""
        m = self._mapper(tmp_path)
        m.register_legacy_groups(["123456789", "987654321"])
        assert "123456789" in m._pending_legacy_groups
        m.bind_group_manually("grp-oid-A", "123456789")
        assert "123456789" not in m._pending_legacy_groups
        # 另一个群还能自动抢剩下的槽位
        assert m.bind_group_openid("grp-oid-B") == "987654321"

    def test_persisted_to_disk(self, tmp_path):
        m = self._mapper(tmp_path)
        m.bind_group_manually("grp-oid-A", "123456789")
        m.flush()
        m2 = QQIDMapper(data_file=tmp_path / "ids.json")
        assert m2.legacy_group_display_id("grp-oid-A") == "123456789"

    def test_round_trip_to_official(self, tmp_path):
        """端到端：绑定的真实群号必须能翻回 openid（发送链路靠它）。

        这是手动绑定的**核心价值**：``forward_group_ids`` 里写的是真实
        群号，发送时 ``to_official_group()`` 必须把它翻成 openid ——
        翻错就是把消息发到别的群。
        """
        m = self._mapper(tmp_path)
        m.bind_group_manually("grp-oid-A", "123456789")
        assert m.to_official_group("123456789") == "grp-oid-A"
        # 反向：openid → 显示号
        assert m.to_legacy_group("grp-oid-A") == "123456789"

    def test_round_trip_to_official_after_rebind(self, tmp_path):
        """改绑后发送链路指向新群（否则消息还发到旧群）。"""
        m = self._mapper(tmp_path)
        m.bind_group_manually("grp-oid-A", "111111")
        m.bind_group_manually("grp-oid-A", "222222")
        assert m.to_official_group("222222") == "grp-oid-A"
        assert m.to_official_group("111111") == "111111"  # 旧号已释放


class TestUnbindGroup:
    def _mapper(self, tmp_path):
        return QQIDMapper(data_file=tmp_path / "ids.json")

    def test_unbind(self, tmp_path):
        m = self._mapper(tmp_path)
        m.bind_group_manually("grp-oid-A", "123456789")
        ok, msg = m.unbind_group("grp-oid-A")
        assert ok
        assert m.legacy_group_display_id("grp-oid-A") is None
        # 解绑后回落到虚拟号
        assert m.to_legacy_group("grp-oid-A").isdigit()

    def test_unbind_not_bound(self, tmp_path):
        m = self._mapper(tmp_path)
        ok, msg = m.unbind_group("grp-oid-X")
        assert not ok
        assert "尚未绑定" in msg

    def test_unbind_empty(self, tmp_path):
        m = self._mapper(tmp_path)
        ok, msg = m.unbind_group("")
        assert not ok

    def test_list_after_unbind(self, tmp_path):
        m = self._mapper(tmp_path)
        m.bind_group_manually("grp-oid-A", "111111")
        m.bind_group_manually("grp-oid-B", "222222")
        assert len(m.list_group_bindings()) == 2
        m.unbind_group("grp-oid-A")
        assert len(m.list_group_bindings()) == 1
        assert dict(m.list_group_bindings()) == {"222222": "grp-oid-B"}


# ============================================================ 命令层
def _mk_system(admin=True, group_openid="grp-oid-CUR", raw=None):
    """构造最小 OfficialStatusSystem（绕开重型 __init__ 依赖）。"""
    from gugubot.logic.system.official_status import OfficialStatusSystem
    from gugubot.utils.types import BroadcastInfo

    sys_mod = OfficialStatusSystem.__new__(OfficialStatusSystem)
    # BasicSystem 必需的最小属性
    sys_mod.config = types.SimpleNamespace(
        get_keys=lambda keys, default=None: default,
    )

    replies = []

    async def _reply(bi, segs):
        replies.append("".join(s.get("data", {}).get("text", "") for s in segs))
        return True

    sys_mod.reply_to_source = _reply
    sys_mod.system_manager = types.SimpleNamespace(get_system=lambda n: None)

    raw_dict = {"group_openid": group_openid}
    if raw:
        raw_dict.update(raw)

    bi = BroadcastInfo(
        event_type="message",
        event_sub_type="group",
        message=[{"type": "text", "data": {"text": "#cmd"}}],
        sender="admin",
        sender_id="100000000001",
        _source=None,
        source_id="999",
        raw=raw_dict,
        is_admin=admin,
    )
    return sys_mod, bi, replies


class TestCommandHandlers:
    def _connector(self, tmp_path):
        mapper = QQIDMapper(data_file=tmp_path / "ids.json")
        mapper.register_legacy_groups(["123456789"])
        return types.SimpleNamespace(id_mapper=mapper)

    def test_bind_command_success(self, tmp_path):
        sys_mod, bi, replies = _mk_system()
        conn = self._connector(tmp_path)
        import asyncio
        asyncio.new_event_loop().run_until_complete(
            sys_mod._handle_bind_group(bi, conn, "绑群 123456789")
        )
        assert "已把本群绑定" in replies[0]
        assert conn.id_mapper.legacy_group_display_id("grp-oid-CUR") == "123456789"

    def test_bind_command_no_arg(self, tmp_path):
        sys_mod, bi, replies = _mk_system()
        conn = self._connector(tmp_path)
        import asyncio
        asyncio.new_event_loop().run_until_complete(
            sys_mod._handle_bind_group(bi, conn, "绑群")
        )
        assert "用法" in replies[0]

    def test_bind_command_not_admin(self, tmp_path):
        sys_mod, bi, replies = _mk_system(admin=False)
        conn = self._connector(tmp_path)
        import asyncio
        asyncio.new_event_loop().run_until_complete(
            sys_mod._handle_bind_group(bi, conn, "绑群 123456789")
        )
        assert "仅管理员" in replies[0]
        # 未绑定
        assert conn.id_mapper.legacy_group_display_id("grp-oid-CUR") is None

    def test_bind_command_no_group_openid(self, tmp_path):
        sys_mod, bi, replies = _mk_system(group_openid="")
        conn = self._connector(tmp_path)
        import asyncio
        asyncio.new_event_loop().run_until_complete(
            sys_mod._handle_bind_group(bi, conn, "绑群 123456789")
        )
        assert "无法识别当前群" in replies[0]

    def test_unbind_command(self, tmp_path):
        sys_mod, bi, replies = _mk_system()
        conn = self._connector(tmp_path)
        mapper = conn.id_mapper
        mapper.bind_group_manually("grp-oid-CUR", "123456789")
        import asyncio
        asyncio.new_event_loop().run_until_complete(
            sys_mod._handle_unbind_group(bi, conn)
        )
        assert "已解除" in replies[0]

    def test_list_command(self, tmp_path):
        sys_mod, bi, replies = _mk_system()
        conn = self._connector(tmp_path)
        conn.id_mapper.bind_group_manually("grp-oid-X", "555555")
        import asyncio
        asyncio.new_event_loop().run_until_complete(
            sys_mod._handle_group_bindings(bi, conn)
        )
        assert "555555" in replies[0]

    def test_list_command_empty(self, tmp_path):
        sys_mod, bi, replies = _mk_system()
        conn = self._connector(tmp_path)
        import asyncio
        asyncio.new_event_loop().run_until_complete(
            sys_mod._handle_group_bindings(bi, conn)
        )
        assert "尚无任何" in replies[0]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
