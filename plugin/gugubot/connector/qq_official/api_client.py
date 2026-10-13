# -*- coding: utf-8 -*-
"""QQ 官方开放平台 REST API 客户端。

文档：https://bot.q.qq.com/wiki/develop/api-v2/

要点：
- 群聊发消息   POST /v2/groups/{group_openid}/messages   （msg_type=2 文本 / 3 富媒体）
- 单聊发消息   POST /v2/users/{user_openid}/messages
- 群富媒体     POST /v2/groups/{group_openid}/files → file_info → 发 msg_type=3
- 鉴权         Authorization: QQBot <access_token>；REST 需带 X-Union-Appid
- 被动回复     body 中带 msg_id + msg_seq 即不受主动消息限额
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

import requests

from gugubot.connector.qq_official.reply_pool import QQReplyPool
from gugubot.connector.qq_official.token_manager import QQTokenManager

# 官方 API 基地址
_API_BASE = "https://api.sgroup.qq.com"

# 官方消息类型（群/单聊统一）
MSG_TYPE_TEXT = 0      # 纯文本（content 字段）
MSG_TYPE_MARKDOWN = 2  # Markdown（markdown.content 字段）
MSG_TYPE_MEDIA = 7     # 富媒体（media.file_info 字段，来自 /files 上传）


class QQOfficialAPIError(RuntimeError):
    """QQ 官方 API 调用错误。"""

    def __init__(self, status: int, code: int, message: str, trace_id: str = ""):
        self.status = status
        self.code = code
        self.message = message
        self.trace_id = trace_id
        super().__init__(
            f"[{status}/{code}] {message}" + (f" (trace:{trace_id})" if trace_id else "")
        )


class QQOfficialClient:
    """QQ 官方 REST API 客户端（线程安全）。"""

    def __init__(
        self,
        token_manager: QQTokenManager,
        reply_pool: QQReplyPool,
        logger=None,
    ) -> None:
        self._token = token_manager
        self._pool = reply_pool
        self._logger = logger
        self._seq_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 基础请求
    # ------------------------------------------------------------------ #

    def _headers(self) -> dict:
        headers = self._token.auth_header()
        headers["Content-Type"] = "application/json"
        headers["X-Union-Appid"] = self._token._appid
        return headers

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[dict] = None,
        params: Optional[dict] = None,
        retry_on_auth: bool = True,
    ) -> Dict[str, Any]:
        resp = requests.request(
            method,
            _API_BASE + path,
            json=payload,
            params=params,
            headers=self._headers(),
            timeout=15,
        )

        # token 失效自动重试一次
        if resp.status_code in (401, 403) and retry_on_auth:
            self._token.invalidate()
            return self._request(method, path, payload, params, retry_on_auth=False)

        trace_id = resp.headers.get("X-Tps-Trace-Id", "")
        if resp.status_code >= 400:
            try:
                err = resp.json()
                code = err.get("code", resp.status_code * 100)
                message = err.get("message", resp.text[:200])
            except Exception:
                code = resp.status_code * 100
                message = resp.text[:200]
            raise QQOfficialAPIError(resp.status_code, code, message, trace_id)

        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text}

    # ------------------------------------------------------------------ #
    # 发消息（Gensokyo 式被动回复绕限额逻辑在此）
    # ------------------------------------------------------------------ #

    def send_group_message(
        self,
        group_openid: str,
        content: str,
        msg_id: Optional[str] = None,
        msg_seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        """发送群消息文本。

        未显式给 msg_id 时从回复池取最新的（被动回复），从而绕开
        主动消息月度限额；池子空则退化为主动消息（可能受限）。
        """
        if msg_id is None:
            taken = self._pool.take(group_openid)
            if taken:
                msg_id, msg_seq = taken

        payload: Dict[str, Any] = {"msg_type": MSG_TYPE_TEXT, "content": content}
        if msg_id:
            payload["msg_id"] = msg_id
            if msg_seq is not None:
                payload["msg_seq"] = msg_seq
        return self._request("POST", f"/v2/groups/{group_openid}/messages", payload)

    def send_group_media_message(
        self,
        group_openid: str,
        file_type: int,
        url: str,
        msg_id: Optional[str] = None,
        msg_seq: Optional[int] = None,
        srv: int = 7,
    ) -> Dict[str, Any]:
        """上传并发送群富媒体消息。

        file_type: 1=图片 2=视频 3=语音；url 需为公网可访问地址。
        """
        info = self.upload_group_media(group_openid, file_type, url, srv=srv)
        if not isinstance(info, dict) or "file_info" in info:
            # upload_group_media 已返回 file_info 字段
            file_info = info.get("file_info") if isinstance(info, dict) else None
            if file_info is None:
                raise QQOfficialAPIError(
                    500, 500, f"富媒体上传返回异常: {info!r}"
                )
            media = {"file_info": file_info}
        else:
            media = {"file_info": info}

        if msg_id is None:
            taken = self._pool.take(group_openid)
            if taken:
                msg_id, msg_seq = taken

        payload: Dict[str, Any] = {
            "msg_type": MSG_TYPE_MEDIA,
            "media": media,
            "content": "",
        }
        if msg_id:
            payload["msg_id"] = msg_id
            if msg_seq is not None:
                payload["msg_seq"] = msg_seq
        return self._request("POST", f"/v2/groups/{group_openid}/messages", payload)

    def send_c2c_message(
        self,
        user_openid: str,
        content: str,
        msg_id: Optional[str] = None,
        msg_seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        """发送 C2C 单聊消息（同样支持被动回复绕限额）。"""
        if msg_id is None:
            taken = self._pool.take(f"c2c:{user_openid}")
            if taken:
                msg_id, msg_seq = taken

        payload = {"msg_type": MSG_TYPE_TEXT, "content": content}
        if msg_id:
            payload["msg_id"] = msg_id
            if msg_seq is not None:
                payload["msg_seq"] = msg_seq
        return self._request("POST", f"/v2/users/{user_openid}/messages", payload)

    # ------------------------------------------------------------------ #
    # 富媒体上传
    # ------------------------------------------------------------------ #

    def upload_group_media(
        self, group_openid: str, file_type: int, url: str, srv: int = 7
    ) -> Dict[str, Any]:
        """群富媒体上传。

        Returns
        -------
        Dict[str, Any]
            官方返回体，其中 ``file_info`` 字段可直接放入消息的 media。
        """
        payload = {"file_type": file_type, "url": url, "srv": srv}
        data = self._request("POST", f"/v2/groups/{group_openid}/files", payload)
        return data

    def upload_c2c_media(
        self, user_openid: str, file_type: int, url: str, srv: int = 7
    ) -> Dict[str, Any]:
        """C2C 单聊富媒体上传。"""
        payload = {"file_type": file_type, "url": url, "srv": srv}
        data = self._request("POST", f"/v2/users/{user_openid}/files", payload)
        return data

    def send_c2c_media_message(
        self,
        user_openid: str,
        file_type: int,
        url: str,
        msg_id: Optional[str] = None,
        msg_seq: Optional[int] = None,
        srv: int = 7,
    ) -> Dict[str, Any]:
        """上传并发送 C2C 富媒体。"""
        data = self.upload_c2c_media(user_openid, file_type, url, srv=srv)
        file_info = data.get("file_info") if isinstance(data, dict) else None
        if not file_info:
            raise QQOfficialAPIError(500, 500, f"富媒体上传返回异常: {data!r}")

        if msg_id is None:
            taken = self._pool.take(f"c2c:{user_openid}")
            if taken:
                msg_id, msg_seq = taken

        payload = {
            "msg_type": MSG_TYPE_MEDIA,
            "media": {"file_info": file_info},
            "content": "",
        }
        if msg_id:
            payload["msg_id"] = msg_id
            if msg_seq is not None:
                payload["msg_seq"] = msg_seq
        return self._request("POST", f"/v2/users/{user_openid}/messages", payload)

    # ------------------------------------------------------------------ #
    # 附加 API
    # ------------------------------------------------------------------ #

    def get_wss_url(self) -> Dict[str, Any]:
        """获取 wss 网关地址（GET /gateway）。"""
        return self._request("GET", "/gateway")
