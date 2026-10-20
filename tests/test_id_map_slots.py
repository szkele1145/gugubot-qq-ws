# -*- coding: utf-8 -*-
"""QQIDMapper 真实群号槽位绑定的回归测试。

真实缺陷（高危）：register_legacy_groups 里判重用的是
``_legacy_group_openid.values()``（openid 集合），而待绑定队列里装的是
**真实群号**——两者永不相交，于是「已绑定的群号」每次都被重新放回待
绑定队列。

后果：每次插件重启（或配置重载）后，第一个开口的**新群**会 pop 掉一个
已被别的群占用的群号并覆盖绑定：

    _legacy_group_openid["123456"] = "新群openid"
    _openid_group_legacy["旧群openid"] = "123456"   # 仍指向同一群号

旧群从此收不到消息，且没有任何报错提示。

修复：判重必须用 ``_legacy_group_openid.keys()``（已占用的真实群号）。
"""
import sys
import pathlib, types, importlib.abc, importlib.machinery, json, tempfile
from pathlib import Path


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

from gugubot.connector.qq_official.id_map import QQIDMapper


class FL:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


def _tmp_file():
    d = Path(tempfile.mkdtemp())
    return d / "qq_official_ids.json"


def test_bound_slot_not_reoffered():
    """已绑定的群号不应重新进入待绑定队列。"""
    f = _tmp_file()
    m = QQIDMapper(f, FL())
    m.register_legacy_groups(["111111", "222222"])

    # 第一个群绑定到 111111
    got = m.bind_group_openid("openidA")
    assert got == "111111", got
    m.flush()  # _save 有 0.2s 去抖，落盘后再模拟重启

    # 模拟重启：同一份数据文件重新加载，再登记同样的群号
    m2 = QQIDMapper(f, FL())
    m2.register_legacy_groups(["111111", "222222"])

    # 新群出现时，绝不能再拿到已被 openidA 占用的 111111
    got2 = m2.bind_group_openid("openidB")
    assert got2 == "222222", (
        f"新群应绑定到空闲槽位 222222，实际拿到 {got2}——"
        f"说明已绑定的槽位被重新放回了待绑定队列"
    )
    print("PASS: 已绑定槽位不会被重新分配 ✅")


def test_existing_binding_not_overwritten():
    """覆盖绑定的情形根本不应发生：旧群必须保持原群号。"""
    f = _tmp_file()
    m = QQIDMapper(f, FL())
    m.register_legacy_groups(["111111"])
    assert m.bind_group_openid("openidA") == "111111"
    m.flush()

    m2 = QQIDMapper(f, FL())
    m2.register_legacy_groups(["111111"])
    # 新群出现：无空闲槽位，必须返回 None（而不是抢走 111111）
    got = m2.bind_group_openid("openidB")
    assert got is None, f"无空闲槽位时应返回 None，实际返回 {got}"

    # 旧群映射完好
    assert m2.to_official_group("111111") == "openidA", m2._legacy_group_openid
    assert m2.legacy_group_display_id("openidA") == "111111"
    print("PASS: 旧群绑定不被新群覆盖 ✅")


def test_pending_after_partial_binding():
    """部分绑定后，剩余槽位仍可正常分配。"""
    f = _tmp_file()
    m = QQIDMapper(f, FL())
    m.register_legacy_groups(["111111", "222222", "333333"])
    assert m.bind_group_openid("openidA") == "111111"
    assert m.bind_group_openid("openidB") == "222222"
    m.flush()

    m2 = QQIDMapper(f, FL())
    m2.register_legacy_groups(["111111", "222222", "333333"])
    got = m2.bind_group_openid("openidC")
    assert got == "333333", f"应拿到唯一空闲槽位 333333，实际 {got}"
    print("PASS: 重启后仅分配真正空闲的槽位 ✅")


def test_restart_no_new_group_keeps_everything():
    """重启后若没有新群开口，绑定关系必须原样保留。"""
    f = _tmp_file()
    m = QQIDMapper(f, FL())
    m.register_legacy_groups(["111111"])
    m.bind_group_openid("openidA")
    m.flush()

    m2 = QQIDMapper(f, FL())
    m2.register_legacy_groups(["111111"])
    # openidA 再发消息：已绑定，返回 None 且映射不变
    assert m2.bind_group_openid("openidA") is None
    assert m2.legacy_group_display_id("openidA") == "111111"
    assert m2.to_official_group("111111") == "openidA"
    print("PASS: 重启后无新群时绑定关系原样保留 ✅")


def test_nondigit_openid_entry_selfmapped():
    """配置里直接填 openid 原文时自映射登记，发送可直接使用。"""
    m = QQIDMapper(None, FL())
    m.register_legacy_groups(["aabbccddeeff00112233"])
    assert m.to_official_group("aabbccddeeff00112233") == "aabbccddeeff00112233"
    print("PASS: 非数字 openid 自映射登记 ✅")


def test_openid_selfmap_does_not_pollute_reverse_table():
    """openid 自映射不得污染「虚拟号反向表」语义。

    _group_rev 的语义是 {虚拟群号: openid}。若把 openid 原串也塞进去
    （自映射），known_legacy_groups()/is_legacy_group() 就把 openid 串
    也当「群号」——下游把配置群号列表画出来时会混入不可读的十六进制串。
    """
    m = QQIDMapper(None, FL())
    openid = "aabbccddeeff00112233"
    m.register_legacy_groups([openid])

    # is_legacy_group 应只认「数字群号」语义
    assert not m.is_legacy_group(openid), (
        "openid 原串不应被当作虚拟群号"
    )
    # 但发送翻译必须仍然可用
    assert m.to_official_group(openid) == openid
    # 显示名应可用（自映射群没有更好的显示名，用原串）
    assert m.group_legacy_display(openid) != ""
    print("PASS: openid 自映射不污染虚拟号反向表 ✅")
