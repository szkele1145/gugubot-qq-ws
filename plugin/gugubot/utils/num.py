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
