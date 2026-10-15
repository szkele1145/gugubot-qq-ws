# -*- coding: utf-8 -*-
"""QQ 官方开放平台 access_token 管理。

使用 appid + appsecret（机器人密钥）通过
``https://bots.qq.com/app/getAppAccessToken`` 获取 access_token，
官方文档：https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html

token 有效期约 7200 秒，实现里提前 ``EXPIRE_MARGIN`` 秒刷新，并带互斥锁
防止并发重复请求。遵循官方规范：Authorization 头使用
``QQBot <access_token>``。
"""

from __future__ import annotations

import threading
import time
from typing import Optional

import requests

_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
_EXPIRE_MARGIN = 300  # 提前 5 分钟刷新


class QQTokenManager:
    """access_token 获取与自动刷新（线程安全）。"""

    def __init__(self, appid: str, secret: str, logger=None) -> None:
        self._appid = (appid or "").strip()
        self._secret = (secret or "").strip()
        self._logger = logger
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires_at: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._appid and self._secret)

    @property
    def appid(self) -> str:
        return self._appid

    def invalidate(self) -> None:
        """强制下次重新获取。"""
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def _fetch(self) -> tuple:
        """请求新 token。返回 (token, expire_in_seconds)。"""
        resp = requests.post(
            _TOKEN_URL,
            json={"appId": self._appid, "clientSecret": self._secret},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token")
        try:
            expire_in = int(float(data.get("expires_in", 7200)))
        except (TypeError, ValueError):
            expire_in = 7200
        if not token:
            raise RuntimeError(f"获取 access_token 失败: {data}")
        return token, expire_in

    def get_token(self) -> str:
        """获取可用 access_token（过期自动刷新；网络失败重试 1 次）。"""
        if not self.configured:
            raise RuntimeError("QQ 官方平台 appid/secret 未配置")

        with self._lock:
            if self._token and time.time() < self._expires_at:
                return self._token

            last_err: Optional[Exception] = None
            for attempt in range(2):
                try:
                    token, expire_in = self._fetch()
                    self._token = token
                    self._expires_at = time.time() + max(expire_in - _EXPIRE_MARGIN, 60)
                    if self._logger:
                        self._logger.debug("[QQ官方] access_token 已刷新")
                    return token
                except Exception as e:
                    last_err = e
                    # 凭证错误不重试（appid/secret 错，重试无意义）
                    msg = str(e)
                    if "401" in msg or "403" in msg or "invalid" in msg.lower():
                        break
                    time.sleep(1 + attempt)
            raise RuntimeError(f"获取 access_token 失败（已重试）: {last_err}")

    def auth_header(self) -> dict:
        """构造官方 API 需要的 Authorization 头（QQBot 前缀）。"""
        return {"Authorization": f"QQBot {self.get_token()}"}
