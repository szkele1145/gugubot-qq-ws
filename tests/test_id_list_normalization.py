# -*- coding: utf-8 -*-
"""配置里的「ID 列表」写成标量时不得逐字符遍历（v2.2.10 续，90）。

问题
----
群号 / 用户号这类配置项期望是**列表**：

.. code-block:: yaml

    group_ids:
      - 123456789

但用户很容易写成**标量**：

.. code-block:: yaml

    group_ids: 123456789

于是这些代码会**逐字符遍历**这个字符串：

.. code-block:: python

    # logic/plugins/server_notice.py / player_notice.py
    groups = [g for g in (group_ids or []) if g]

    # logic/plugins/cross_broadcast.py
    if group_ids and any(group_ids):
        return {str(gid): "group" for gid in group_ids if gid}

``any("123456789")`` 为真 → 造出 ``{"1": "group", "2": "group", ...}``
九个无意义目标 → 消息发到不存在的群、日志里一堆发送失败，而用户完全
看不出是配置写错了（配置检验也不查这一项）。

修复
----
新增 ``utils/num.py::as_id_list()``：``None``/空 → ``[]``；标量（str/int）
→ 单元素列表；可迭代 → 逐项 ``str()`` 去空。三处调用点改用它。
"""
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

from gugubot.utils.num import as_id_list  # noqa: E402


# ==================================================== 1. 归一化本身
def test_scalar_string_becomes_single_item():
    """标量字符串必须变成**单元素**列表，不能逐字符拆开。"""
    assert as_id_list("123456789") == ["123456789"], (
        "标量字符串被逐字符拆开了 —— 会造出 {'1','2','3'...} 这种垃圾目标"
    )


def test_scalar_int_becomes_single_item():
    assert as_id_list(123456789) == ["123456789"]


def test_list_is_normalized_to_str():
    assert as_id_list([123, "456", None, "", "  "]) == ["123", "456"]


def test_tuple_and_set_work():
    assert as_id_list((1, 2)) == ["1", "2"]
    assert sorted(as_id_list({3, 4})) == ["3", "4"]


def test_none_and_empty():
    assert as_id_list(None) == []
    assert as_id_list([]) == []
    assert as_id_list("") == []
    assert as_id_list("   ") == []


# ==================================================== 2. 调用点必须使用它
def test_notice_modules_use_normalizer():
    """两个播报模块的目标构建必须走 as_id_list（不能裸遍历）。"""
    for name in ("server_notice.py", "player_notice.py"):
        src = (
            _ROOT / "plugin" / "gugubot" / "logic" / "plugins" / name
        ).read_text(encoding="utf-8-sig")
        assert "as_id_list" in src, (
            f"{name} 没有使用 as_id_list —— 用户把群号写成标量时，"
            f"`for g in group_ids` 会逐字符遍历，造出无意义的目标"
        )


def test_cross_broadcast_uses_normalizer():
    src = (
        _ROOT / "plugin" / "gugubot" / "logic" / "plugins" / "cross_broadcast.py"
    ).read_text(encoding="utf-8-sig")
    assert "as_id_list" in src, (
        "cross_broadcast 没有使用 as_id_list —— qq_forward_group_ids 写成"
        "标量时会生成 {'1': 'group', '2': 'group', ...} 这种垃圾目标"
    )


# ==================================================== 3. 端到端：标量配置
def test_notice_target_with_scalar_config():
    """标量配置下构建出的 target 必须是**一个**群，不是一个字符一个。"""
    _MOD = "gugubot.logic.plugins.server_notice"
    sys.modules.pop(_MOD, None)
    from gugubot.logic.plugins import server_notice  # noqa: E402

    target = server_notice._build_notice_target("123456789")
    assert target is not None
    assert "123456789" in target, (
        f"标量群号没有成为单个目标，实际: {target}"
    )
    # 单目标时会额外塞一个 "_"（防桥接单目标过滤），所以只看数字键
    digits = [k for k in target if k.isdigit()]
    assert digits == ["123456789"], f"目标里出现了非预期的群号: {digits}"


def test_notice_target_with_list_config():
    _MOD = "gugubot.logic.plugins.server_notice"
    sys.modules.pop(_MOD, None)
    from gugubot.logic.plugins import server_notice  # noqa: E402

    target = server_notice._build_notice_target([111, "222"])
    digits = sorted(k for k in target if k.isdigit())
    assert digits == ["111", "222"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
