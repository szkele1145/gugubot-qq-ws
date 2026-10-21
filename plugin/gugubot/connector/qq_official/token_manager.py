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

import re
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
        # 刷新锁：单独于 _lock，保证「持 _refresh_lock 做网络请求」期间
        # 不会阻塞其他线程读取缓存 token（invalidate 等快路径）。
        self._refresh_lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        # 复用 HTTP 会话（连接池），避免每次刷新 token 都重新建立 TLS
        self._session = requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self._appid and self._secret)

    @property
    def appid(self) -> str:
        return self._appid

    def invalidate(self, token: Optional[str] = None) -> None:
        """强制下次重新获取。

        Parameters
        ----------
        token : Optional[str]
            传入「发起请求时实际使用的那枚 token」时，只作废这一枚：
            若当前缓存的 token 已被其他线程刷新成更新的值，则不动它。

            场景：线程 A 用 T1 发请求（此时 T1 已过期），同时线程 B 已把
            token 刷新成 T2。A 收到 401 后若无条件清空，会把 T2 也作废
            → 下一个请求被迫再刷一次 token（多一次网络往返 + 限流风险）。
        """
        with self._lock:
            if token is not None and self._token is not None and self._token != token:
                # 缓存的已是更新的一枚，不作废
                return
            self._token = None
            self._expires_at = 0.0

    def _fetch(self) -> tuple:
        """请求新 token。返回 (token, expire_in_seconds)。"""
        resp = self._session.post(
            _TOKEN_URL,
            json={"appId": self._appid, "clientSecret": self._secret},
            timeout=10,
        )
        if resp.status_code >= 400:
            # 官方在 4xx 时会在响应体里给出具体原因（如 appid/secret 错误）。
            # 直接 raise_for_status() 会丢掉 body，只剩一句
            # "400 Client Error: Bad Request for url: ..."——排障时完全看不出
            # 是凭证错了还是接口变了。这里把 body 一并带进异常。
            detail = (resp.text or "")[:300]
            raise RuntimeError(
                f"获取 access_token 失败（HTTP {resp.status_code}）: {detail}"
            )
        try:
            data = resp.json()
        except ValueError as e:
            raise RuntimeError(f"获取 access_token 返回非 JSON: {(resp.text or '')[:300]}") from e
        if not isinstance(data, dict):
            raise RuntimeError(f"获取 access_token 返回格式异常: {data!r}")
        token = data.get("access_token")
        try:
            expire_in = int(float(data.get("expires_in", 7200)))
        except (TypeError, ValueError):
            expire_in = 7200
        if not token:
            raise RuntimeError(f"获取 access_token 失败: {data}")
        return token, expire_in

    @staticmethod
    def _is_credential_error(err: Exception) -> bool:
        """凭证类错误（appid/secret 错、被拒）：重试必然再失败。

        旧实现靠 ``str(err)`` 做子串匹配（"401"/"403"/"invalid"）——
        既**漏判**（官方 4xx 的中文原因或别的措辞不含这些词 → 白重试
        一次，多耗 1s + 一次网络往返 + 限流风险），又可能**误判**
        （异常文案里恰好出现 "401" 子串，如 URL 或参数中含该数字）。
        现优先读结构化状态码，取不到才退回文案判断。
        """
        resp = getattr(err, "response", None)
        status = getattr(resp, "status_code", None)
        if isinstance(status, int):
            # 4xx（除 429 限流外）都是「请求本身有问题」，重试无意义；
            # 429 是限流，退避重试反而有意义。
            if 400 <= status < 500 and status != 429:
                return True
            return False
        msg = str(err)
        # 取不到状态码（如自抛的 RuntimeError）：按关键词粗判。
        # 注意只用**明确指向凭证**的词，不要用 "appid"/"secret" 这类
        # 会出现在普通网络错误文案里的词（URL 里就含 getAppAccessToken）。
        lowered = msg.lower()
        if "429" in lowered:
            return False  # 限流：退避重试有意义
        # 中文凭证文案（官方 body 常见措辞）
        if any(k in msg for k in ("不合法", "不正确", "无效", "校验失败")):
            # 但若整句其实是网络错误（含 url/超时），不算凭证问题
            if not any(k in lowered for k in ("timeout", "timed out", "connection")):
                return True
        # 英文关键词。注意 "401" 只在「独立出现」时才算——
        # URL/路径里恰好带 401 的数字（如 /401/refresh）不得误判。
        if "unauthorized" in lowered or "forbidden" in lowered:
            return True
        if "invalid appid" in lowered or "invalid secret" in lowered:
            return True
        if "client secret" in lowered:
            return True
        # 独立的 401/403：要求前后不是数字/字母/斜杠（避免 URL 片段命中）
        if re.search(r"(?<![\w/])(?:401|403)(?![\w/])", msg):
            return True
        return False

    def get_token(self) -> str:
        """获取可用 access_token（过期自动刷新；网络失败重试 1 次）。

        并发语义：采用**单飞（single-flight）**——同一时刻只允许一个线程
        真正发起刷新请求，其余线程等待其结果。这样既避免并发重复刷新
        token（浪费配额），又不会在网络请求期间**持有锁**（旧实现持锁
        做 I/O，最坏情况 10s 超时 ×2 次重试 ≈ 20s 全程阻塞所有其他
        API 请求）。
        """
        if not self.configured:
            raise RuntimeError("QQ 官方平台 appid/secret 未配置")

        # 快路径：缓存有效，直接返回（不碰刷新锁）
        with self._lock:
            if self._token and time.time() < self._expires_at:
                return self._token

        # 慢路径：抢刷新权。抢不到说明别的线程正在刷 → 等它的结果。
        with self._refresh_lock:
            # 双重检查：等锁期间可能已被别人刷好了
            with self._lock:
                if self._token and time.time() < self._expires_at:
                    return self._token

            logger = self._logger
            last_err: Optional[Exception] = None
            for attempt in range(2):
                try:
                    token, expire_in = self._fetch()
                    with self._lock:
                        self._token = token
                        self._expires_at = time.time() + max(
                            expire_in - _EXPIRE_MARGIN, 60
                        )
                    if logger:
                        logger.debug("[QQ官方] access_token 已刷新")
                    return token
                except Exception as e:
                    last_err = e
                    # 凭证错误不重试（appid/secret 错，重试无意义）
                    if self._is_credential_error(e):
                        break
                    time.sleep(1 + attempt)
            raise RuntimeError(f"获取 access_token 失败（已重试）: {last_err}")

    def auth_header(self) -> dict:
        """构造官方 API 需要的 Authorization 头（QQBot 前缀）。"""
        return {"Authorization": f"QQBot {self.get_token()}"}
