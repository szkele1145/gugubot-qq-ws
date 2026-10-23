# -*- coding: utf-8 -*-
"""数值配置的安全读取辅助。

背景：配置文件由用户手工编辑，数值项可能被填成字符串（``"30"``）或
含单位的文本（``"30 天"``）。裸用这些值做算术（``days * 86400``）、
比较（``ttl > 0``）或传给需要数值的 API（``asyncio.sleep``）会抛
TypeError/ValueError——轻则单条功能报错，重则整个回调链崩掉。

本模块提供统一的转换入口：转换失败一律回退默认值，绝不向上抛异常。
"""
from typing import Any


def safe_int(value: Any, default: int = 0) -> int:
    """把任意配置值安全转成 int；失败回退 default。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    """把任意配置值安全转成 float；失败回退 default。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_positive_int(value: Any, default: int) -> int:
    """转 int 并钳制为正数（<= 0 或转换失败均回退 default）。"""
    n = safe_int(value, default)
    return n if n > 0 else default


def as_id_list(value: Any) -> list:
    """把配置里的「ID 列表」归一化成 list[str]。

    为什么需要
    ----------
    群号 / 用户号这类配置项期望是**列表**：

    .. code-block:: yaml

        group_ids:
          - 123456789
          - 987654321

    但用户很容易写成**标量**：

    .. code-block:: yaml

        group_ids: 123456789

    此时 ``for gid in group_ids`` 会**逐字符遍历**这个字符串（``"1"``、
    ``"2"``……），``any(group_ids)`` 也是真（非空字符串），于是悄悄造出
    一堆无意义的目标：``{"1": "group", "2": "group", ...}``。表现为消息
    发到不存在的群、日志里一堆发送失败，却完全看不出是配置写错了。

    本函数把三种形态都归一化成列表：

    * ``None`` / 空 → ``[]``
    * 标量（str / int）→ 单元素列表
    * 可迭代 → 逐项 ``str()`` 并去掉空值

    不做「是否像群号」的合法性判断（那是 config_check 的职责）。
    """
    if value is None:
        return []
    # str 与 bytes 是「容器形态的标量」—— 不能当可迭代拆开
    if isinstance(value, (str, bytes)):
        s = value.decode() if isinstance(value, bytes) else value
        s = s.strip()
        return [s] if s else []
    if isinstance(value, (int, float)):
        return [str(value)]
    try:
        items = list(value)
    except TypeError:
        return []
    out = []
    for item in items:
        if item is None:
            continue
        s = str(item).strip()
        if s:
            out.append(s)
    return out
