# -*- coding: utf-8 -*-
"""QQ 官方开放平台直连支持。

内含：
- ``id_map``       : openid ↔ 虚拟号码 双向映射（兼容既有按 QQ 号写的配置/逻辑）
- ``token_manager``: access_token 自动获取与提前刷新
- ``reply_pool``   : Gensokyo 式 msg_id 回复池（被动回复绕开主动消息限额）
- ``api_client``   : 官方 REST API 客户端（群/单聊、媒体上传等）
- ``ws_client``     : 官方 WebSocket 网关客户端（op 状态机 + 心跳 + 断线重连）
- ``parser``       : 官方事件 → BroadcastInfo
- ``connector``     : QQOfficialConnector（含 OneBot 风格 ``Bot`` 兼容包装）
"""

from gugubot.connector.qq_official.api_client import QQOfficialClient
from gugubot.connector.qq_official.connector import QQOfficialConnector
from gugubot.connector.qq_official.id_map import QQIDMapper
from gugubot.connector.qq_official.parser import QQOfficialParser
from gugubot.connector.qq_official.reply_pool import QQReplyPool
from gugubot.connector.qq_official.token_manager import QQTokenManager
from gugubot.connector.qq_official.ws_client import QQOfficialWSClient

__all__ = [
    "QQOfficialClient",
    "QQOfficialConnector",
    "QQIDMapper",
    "QQOfficialParser",
    "QQReplyPool",
    "QQTokenManager",
    "QQOfficialWSClient",
]
