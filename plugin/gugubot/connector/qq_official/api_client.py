# -*- coding: utf-8 -*-
"""QQ 官方开放平台 REST API 客户端。

文档：https://bot.q.qq.com/wiki/develop/api-v2/

要点：
- 群聊发消息   POST /v2/groups/{group_openid}/messages   （msg_type=0 文本 / 7 富媒体）
- 单聊发消息   POST /v2/users/{user_openid}/messages
- 群富媒体     POST /v2/groups/{group_openid}/files → file_info → 发 msg_type=7
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

    DEFAULT_TIMEOUT = 15  # 秒；可通过构造参数 timeout 覆盖

    def __init__(
        self,
        token_manager: QQTokenManager,
        reply_pool: QQReplyPool,
        logger=None,
        timeout: Optional[int] = None,
    ) -> None:
        self._token = token_manager
        self._pool = reply_pool
        self._logger = logger
        self._seq_lock = threading.Lock()
        self._timeout = timeout if timeout is not None else self.DEFAULT_TIMEOUT

    # ------------------------------------------------------------------ #
    # 基础请求
    # ------------------------------------------------------------------ #

    def _headers(self) -> dict:
        headers = self._token.auth_header()
        headers["Content-Type"] = "application/json"
        headers["X-Union-Appid"] = self._token.appid
        return headers

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[dict] = None,
        params: Optional[dict] = None,
        retry_on_auth: bool = True,
        _net_attempt: int = 0,
    ) -> Dict[str, Any]:
        """REST 请求。

        - 401/403：token 失效自动刷新重试一次
        - 网络异常（超时/连接失败）：短暂退避重试一次（幂等的读操作
          与消息发送均安全——官方按 msg_id+msg_seq 去重）
        - 429：流量限制，不刷新 token、不重试，直接抛出交由上层降级
        """
        try:
            resp = requests.request(
                method,
                _API_BASE + path,
                json=payload,
                params=params,
                headers=self._headers(),
                timeout=self._timeout,
            )
        except requests.RequestException as e:
            if _net_attempt < 1:
                time.sleep(1)
                return self._request(
                    method, path, payload, params,
                    retry_on_auth=retry_on_auth, _net_attempt=_net_attempt + 1,
                )
            raise QQOfficialAPIError(0, 0, f"网络异常: {e}") from e

        # 429 流量限制：不刷新 token、不重试，事件级直接抛出，
        # 避免把限流误判为 token 失效导致无效刷新。
        if resp.status_code == 429:
            if self._logger:
                self._logger.warning(
                    f"[QQ官方API] 触发 429 流量限制 {path}，直接失败不重试"
                )
            trace_id = resp.headers.get("X-Tps-Trace-Id", "")
            raise QQOfficialAPIError(429, 42900, "触发流量限制（429）", trace_id)

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

        被动回复降级链：msg_id 池（msg_seq 递增）→ event_id 池 → 主动
        消息（可能受月度限额）。
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
        else:
            # msg_id 通道无货 → 尝试 event_id 备用通道
            event_id = self._pool.take_event(group_openid)
            if event_id:
                payload["event_id"] = event_id
        return self._request("POST", f"/v2/groups/{group_openid}/messages", payload)

    def send_group_media_message(
        self,
        group_openid: str,
        file_type: int,
        url: str,
        msg_id: Optional[str] = None,
        msg_seq: Optional[int] = None,
        srv: int = 7,
        use_reply_pool: bool = True,
    ) -> Dict[str, Any]:
        """上传并发送群富媒体消息。

        file_type: 1=图片 2=视频 3=语音；url 需为公网可访问地址。

        use_reply_pool=False 时强制主动消息（不消耗被动回复额度）——
        多图连发场景调用方应传 False，把宝贵的 msg_id seq 留给文本。
        """
        info = self.upload_group_media(group_openid, file_type, url, srv=srv)
        # 上传返回体形如 {"file_info": "...", ...}；兼容直接给 file_info 的返回
        file_info = None
        if isinstance(info, dict):
            file_info = info.get("file_info") or (
                info if set(info.keys()) <= {"file_info"} else None
            )
        if not file_info:
            raise QQOfficialAPIError(500, 500, f"富媒体上传返回异常: {info!r}")
        media = {"file_info": file_info}

        if msg_id is None and use_reply_pool:
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
        elif use_reply_pool:
            # 与 send_group_message 对称：msg_id 通道无货时尝试 event_id
            event_id = self._pool.take_event(group_openid)
            if event_id:
                payload["event_id"] = event_id
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
        else:
            # 与群聊路径对称：msg_id 无货时尝试 event_id 备用通道
            event_id = self._pool.take_event(f"c2c:{user_openid}")
            if event_id:
                payload["event_id"] = event_id
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
        use_reply_pool: bool = True,
    ) -> Dict[str, Any]:
        """上传并发送 C2C 富媒体（use_reply_pool=False 强制主动消息省额度）。"""
        data = self.upload_c2c_media(user_openid, file_type, url, srv=srv)
        file_info = data.get("file_info") if isinstance(data, dict) else None
        if not file_info:
            raise QQOfficialAPIError(500, 500, f"富媒体上传返回异常: {data!r}")

        if msg_id is None and use_reply_pool:
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
        elif use_reply_pool:
            # 与群聊富媒体路径对称：msg_id 无货时尝试 event_id
            event_id = self._pool.take_event(f"c2c:{user_openid}")
            if event_id:
                payload["event_id"] = event_id
        return self._request("POST", f"/v2/users/{user_openid}/messages", payload)

    # ------------------------------------------------------------------ #
    # 附加 API
    # ------------------------------------------------------------------ #

    def get_wss_url(self) -> Dict[str, Any]:
        """获取 wss 网关地址（GET /gateway）。"""
        return self._request("GET", "/gateway")
