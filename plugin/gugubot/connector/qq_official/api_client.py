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

# 鉴权类错误码（官方《API 调用指南 · OpenAPI 错误码 · 公共错误码》）
# 这些错误码**可能伴随 HTTP 200 返回**，此时必须同样走「刷新 token 重试」
# ——否则 access_token 过期后所有请求都会静默失败。
_AUTH_ERR_CODES = frozenset({
    11241,     # ErrorWrongToken 参数中缺少 token
    11242,     # ErrorCheckTokenFailed 校验 token 失败（系统错误，可重试）
    11243,     # ErrorCheckTokenNotPass 校验 token 未通过
    11251,     # ErrorWrongAppid 参数中的 appid 错误
    11261,     # ErrorWrongAppid 参数中缺少 appid
    11265,     # ErrorRobotHasBaned 机器人已经被封禁
})


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
        self._timeout = timeout if timeout is not None else self.DEFAULT_TIMEOUT
        # 复用 HTTP 会话（连接池），避免每次请求重新建立 TCP 握手与 TLS
        self._session = requests.Session()

    # ------------------------------------------------------------------ #
    # 基础请求
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_retry_safe(method: str, payload, exc: Exception) -> bool:
        """网络异常后重试是否安全（不会造成重复副作用）。

        判据分两段：

        1. **失败发生在哪个阶段**。连接阶段失败（`ConnectTimeout` /
           `ConnectionError`）= 请求根本没送达 → 任何请求都可安全重试。
           读超时（`ReadTimeout`）意味着请求**可能已被服务端处理**。
        2. **请求是否幂等/带去重键**。GET 天然幂等；消息发送只有带
           `msg_id` 或 `event_id`（被动回复）时官方才按
           ``msg_id + msg_seq`` 去重。

        所以：**主动消息遇到读超时不能重试** —— 它没有去重键，重试会让
        群里出现两条一模一样的消息。
        """
        method = (method or "").upper()
        # ① 连接阶段失败：请求未送达，重试安全
        if isinstance(
            exc,
            (
                requests.exceptions.ConnectTimeout,
                requests.exceptions.ConnectionError,
            ),
        ):
            return True
        # ② 其余（读超时等）：只允许幂等请求
        if method == "GET":
            return True
        if isinstance(payload, dict) and (
            payload.get("msg_id") or payload.get("event_id")
        ):
            return True
        return False

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
        # 先取一次 token 并留住「本次实际使用的那枚」：
        # 收到 401 时只作废这一枚，避免误伤其他线程刚刷新的新 token。
        headers = self._headers()
        used_token = headers.get("Authorization", "")
        if used_token.startswith("QQBot "):
            used_token = used_token[len("QQBot "):]

        try:
            resp = self._session.request(
                method,
                _API_BASE + path,
                json=payload,
                params=params,
                headers=headers,
                timeout=self._timeout,
            )
        except requests.RequestException as e:
            # 是否重试要按「幂等性 + 失败发生在哪个阶段」判断。
            #
            # 读超时（ReadTimeout）的语义是「请求**可能已经被服务端处理**，
            # 只是响应没等到」。此时重试：
            #   * 被动回复（带 msg_id / event_id）→ 官方按 msg_id+msg_seq
            #     去重，安全；
            #   * **主动消息（无去重键）→ 群里出现两条一模一样的消息**。
            #
            # 连接阶段失败（ConnectTimeout / ConnectionError）= 请求**未送达**
            # → 任何请求都可以安全重试。
            if _net_attempt < 1 and self._is_retry_safe(method, payload, e):
                time.sleep(1)
                return self._request(
                    method, path, payload, params,
                    retry_on_auth=retry_on_auth, _net_attempt=_net_attempt + 1,
                )
            raise QQOfficialAPIError(0, 0, f"网络异常: {e}") from e

        trace_id = resp.headers.get("X-Tps-Trace-Id", "")

        # 解析响应体（业务错误可能藏在 HTTP 200 里）
        body: Optional[Dict[str, Any]] = None
        if resp.status_code != 204:
            try:
                parsed = resp.json()
                if isinstance(parsed, dict):
                    body = parsed
            except Exception:
                body = None

        # 业务错误码：官方文档明确字段名是 err_code（少数历史接口用
        # code），成功时为 0；HTTP 状态码可能仍是 200。
        err_code: Optional[int] = None
        if body is not None:
            raw = body.get("err_code")
            if raw is None:
                raw = body.get("code")
            if isinstance(raw, int):
                err_code = raw

        # 鉴权失效：token 过期/校验失败（HTTP 401/403 或 body err_code
        # 11241/11242/11243 —— 官方公共错误码里的 token 类错误）。
        # 刷新后重试一次；重试仍失败则原样抛出。
        is_auth_err = (
            resp.status_code in (401, 403)
            or err_code in _AUTH_ERR_CODES
        )
        if is_auth_err and retry_on_auth:
            self._token.invalidate(used_token)
            # 透传 _net_attempt：网络异常重试后若又遇到鉴权失败，重试次数
            # 不应被重置（否则最坏情况请求次数翻倍）
            return self._request(
                method, path, payload, params,
                retry_on_auth=False, _net_attempt=_net_attempt,
            )

        # 429 流量限制：不刷新 token、不重试，事件级直接抛出，
        # 避免把限流误判为 token 失效导致无效刷新。
        if resp.status_code == 429:
            if self._logger:
                self._logger.warning(
                    f"[QQ官方API] 触发 429 流量限制 {path}，直接失败不重试"
                )
            raise QQOfficialAPIError(429, err_code or 42900, "触发流量限制（429）", trace_id)

        # 业务错误（err_code != 0，HTTP 可能 200）：以 err_code 为准，
        # 不依据 message 判定（官方文档明确 message 可能随时调整）。
        if err_code is not None and err_code != 0:
            err_msg = body.get("message") or resp.text[:200]
            raise QQOfficialAPIError(
                resp.status_code, err_code, err_msg, body.get("trace_id") or trace_id
            )

        if resp.status_code >= 400:
            if body is not None:
                code = body.get("err_code")
                if not isinstance(code, int):
                    code = body.get("code")
                if not isinstance(code, int):
                    code = resp.status_code * 100
                message = body.get("message", resp.text[:200])
            else:
                code = resp.status_code * 100
                message = resp.text[:200]
            raise QQOfficialAPIError(resp.status_code, code, message, trace_id)

        if body is not None:
            return body
        if resp.status_code == 204 or not resp.content:
            return {}
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

    @staticmethod
    def _extract_file_info(data) -> Any:
        """从 /files 上传的返回体里取出 file_info。

        官方返回体形如 ``{"file_info": "..."}``；历史上也见过「整个返回体
        就是 file_info 包装」的形态。两条发送路径（群聊 / 单聊）必须用
        **同一套**判定——否则同一份返回在群聊能发、单聊报「上传返回异常」。

        值为空串 / 空 dict 一律视为无效：否则会拼出 ``media.file_info``
        为空的富媒体消息，官方侧报错且用户端什么都收不到。
        """
        if not isinstance(data, dict):
            return None
        raw = data.get("file_info")
        if raw:
            return raw
        if set(data.keys()) < {"file_info"}:
            # data 里没有 file_info 这个键 → 把整个 data 当 file_info
            return data or None
        return None

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
        file_info = self._extract_file_info(info)
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
        # 与群聊路径共用同一套判定（见 _extract_file_info）
        file_info = self._extract_file_info(data)
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
