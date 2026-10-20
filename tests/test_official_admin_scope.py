# -*- coding: utf-8 -*-
"""官方直连解析器：管理群上下文不得泄漏到 C2C 单聊（越权误判）。

真实漏洞：``_is_admin`` 依赖实例上的粘性字段 ``_current_source_id``
（由上一条**群消息**设置）。C2C 单聊路径调用 ``_is_admin(legacy_user)``
时会读到这份残留——于是：

    群消息（来自管理群） → _current_source_id = 管理群号
    ↓ 紧接着
    C2C 单聊消息       → _is_admin 读到管理群号 → 判定为管理员

该行注释明写「官方单聊默认不放开权限」，实现与意图正好相反。

修复：群上下文改为**显式传参** ``_is_admin(sender_id, source_id=...)``，
C2C 路径传 None 表示「无群上下文」；粘性字段整体移除。

本测试直接针对 _is_admin 的调用契约，不依赖网络与 WS。
"""
import sys
import pathlib, types, importlib.abc, importlib.machinery


class _A:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return _A()
    def __call__(self, *a, **k): return _A()


class _SM(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        v = _A(); setattr(self, name, v); return v


class _L(importlib.abc.Loader):
    def create_module(self, s): return _SM(s.name)
    def exec_module(self, m): pass


class _F:
    def find_spec(self, f, p=None, t=None):
        if f.startswith('mcdreforged') or f.startswith('packaging'):
            return importlib.machinery.ModuleSpec(f, _L(), is_package=True)


sys.meta_path.insert(0, _F())
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'plugin'))

from gugubot.connector.qq_official.parser import QQOfficialParser

ADMIN_GROUP = "123456"      # 配置里的管理群号
ADMIN_USER = "100000000001"  # 配置里的管理员虚拟号
NORMAL_USER = "100000000002"


class FakeConfig:
    def __init__(self, data):
        self._data = data

    def get_keys(self, key, default=None):
        r = self._data
        for k in key[:-1]:
            r = r.get(k, {})
            if not isinstance(r, dict):
                return default
        return r.get(key[-1], default)


def _make_parser(admin_ids=None, admin_group_ids=None):
    p = QQOfficialParser.__new__(QQOfficialParser)

    class Conn:
        config = FakeConfig({
            "connector": {"QQ": {"permissions": {
                "admin_ids": admin_ids if admin_ids is not None else [],
                "admin_group_ids": admin_group_ids if admin_group_ids is not None else [],
                "group_ids": [],
            }}}
        })

    p.connector = Conn()
    p.logger = None
    return p


def test_c2c_not_admin_despite_sticky_admin_group():
    """管理群消息之后来的 C2C 单聊，不得被判为管理员。"""
    p = _make_parser(admin_ids=[ADMIN_USER], admin_group_ids=[ADMIN_GROUP])

    # 模拟「上一条群消息来自管理群」留下的粘性上下文
    p._current_source_id = ADMIN_GROUP

    # C2C 路径：显式 source_id=None（无群上下文）
    assert p._is_admin(NORMAL_USER, source_id=None) is False, \
        "C2C 单聊不应因上一条管理群消息而被判为管理员"

    # 即便实例上还残留着粘性字段，source_id=None 也必须忽略它
    assert p._is_admin(NORMAL_USER, source_id=None) is False
    print("PASS: C2C 单聊不因残留的管理群上下文而越权 ✅")


def test_c2c_admin_in_list_still_admin():
    """在 admin_ids 列表里的用户，C2C 里仍应是管理员。"""
    p = _make_parser(admin_ids=[ADMIN_USER], admin_group_ids=[ADMIN_GROUP])
    p._current_source_id = ADMIN_GROUP
    assert p._is_admin(ADMIN_USER, source_id=None) is True, \
        "显式列入 admin_ids 的用户单聊中仍应是管理员"
    print("PASS: admin_ids 列表中的用户单聊仍是管理员 ✅")


def test_group_message_in_admin_group_is_admin():
    """管理群内的普通用户（群消息，显式传 source_id）应是管理员。"""
    p = _make_parser(admin_ids=[], admin_group_ids=[ADMIN_GROUP])
    assert p._is_admin(NORMAL_USER, source_id=ADMIN_GROUP) is True
    print("PASS: 管理群内用户群消息仍是管理员 ✅")


def test_group_message_outside_admin_group_not_admin():
    """非管理群内的普通用户不是管理员。"""
    p = _make_parser(admin_ids=[], admin_group_ids=[ADMIN_GROUP])
    assert p._is_admin(NORMAL_USER, source_id="654321") is False
    print("PASS: 非管理群内用户不是管理员 ✅")


def test_no_sticky_attribute_written():
    """解析器不应再往实例上写粘性群上下文字段。"""
    p = _make_parser(admin_ids=[], admin_group_ids=[ADMIN_GROUP])
    # 新实例上不应存在该属性
    assert not hasattr(p, "_current_source_id"), \
        "_current_source_id 已移除，不应再被创建"
    print("PASS: 不再使用粘性群上下文字段 ✅")
