# -*- coding: utf-8 -*-
"""QQ 官方开放平台事件解析器：官方事件 → BroadcastInfo。

处理的事件类型：
- ``GROUP_AT_MESSAGE_CREATE``  群聊 @机器人消息
- ``C2C_MESSAGE_CREATE``       单聊消息
- ``FRIEND_ADD``               好友添加（→ notice）
- ``GROUP_ADD_ROM`` / ``GROUP_DEL_ROM`` 群机器人被添加/移除（→ notice）

官方事件结构与 OneBot 差异极大，关键映射：
- 无真实 QQ 号：只有 ``author.member_openid`` / ``group_openid``，
  通过 :class:`QQIDMapper` 翻译成「虚拟数字号」供下游系统使用；
- 消息体在 ``content``（纯文本，可能带 ``<@!botid>`` at 机器人标记）
  或 ``attachments``（富媒体）；
- ``GROUP_AT_MESSAGE`` 的 content 会自动去掉 @机器人的前缀。

下游 BroadcastInfo 兼容约定（与 QQParser 保持一致）：
- event_type: "message" / "notice"
- event_sub_type: "group" / "private"
- message: OneBot array 格式 [{"type": "text", "data": {...}}, ...]
- source_id: 群号（真实绑定号或虚拟号）
- sender: 显示昵称（官方只有 openid，用映射号显示）
- sender_id: 虚拟用户号
"""

from __future__ import annotations

import re
from typing import Optional

from gugubot.parser.basic_parser import BasicParser
from gugubot.utils.types import BroadcastInfo, Source

# <@!xxxx> 官方 at 机器人标记
_AT_BOT_PATTERN = re.compile(r"<@!\s*[A-Za-z0-9_-]+\s*>")


class QQOfficialParser(BasicParser):
    """QQ 官方平台事件解析器。"""

    def _is_admin(self, sender_id) -> bool:
        """检查是否是管理员（sender_id 已是虚拟号）。"""
        config = self.connector.config
        admin_ids = config.get_keys(["connector", "QQ", "permissions", "admin_ids"], [])
        admin_groups = config.get_keys(
            ["connector", "QQ", "permissions", "admin_group_ids"], []
        )
        if str(sender_id) in [str(i) for i in admin_ids if i]:
            return True
        # 官方平台没有"管理员"概念，管理群内所有人可用
        source_id = getattr(self, "_current_source_id", None)
        if source_id and str(source_id) in [str(g) for g in admin_groups if g]:
            return True
        return False

    async def _should_exclude_user(self, user_id: str) -> bool:
        """官方平台没有真实 QQ 号，排除列表按虚拟号匹配。"""
        exclude_ids = self.connector.config.get_keys(
            ["system", "bound_notice", "exclude_ids"], []
        )
        if user_id in [str(i) for i in exclude_ids if i]:
            return True
        return False

    async def parse(self, raw_message) -> Optional[BroadcastInfo]:
        """解析官方 WS 推送的 Dispatch 事件。

        Parameters
        ----------
        raw_message : dict
            事件 ``d`` 字段（已由 connector 从 WS 帧解出）。

        Returns
        -------
        Optional[BroadcastInfo]
            可广播的事件返回 BroadcastInfo，否则 None。
        """
        try:
            if not isinstance(raw_message, dict):
                return None

            # connector 会把 event_type 塞进 raw（因为 parse 只拿到 d）
            event_name = raw_message.get("_event_name", "")
            data = raw_message.get("_data", raw_message)

            if event_name == "GROUP_AT_MESSAGE_CREATE":
                return await self._parse_group_message(data)
            elif event_name == "C2C_MESSAGE_CREATE":
                return await self._parse_c2c_message(data)
            elif event_name in ("FRIEND_ADD",):
                return self._parse_friend_add(data)
            elif event_name in ("GROUP_ADD_ROM", "GROUP_DEL_ROM"):
                return self._parse_group_bot_change(data, event_name)
            return None
        except Exception as e:
            if self.logger:
                import traceback
                self.logger.error(
                    f"QQ官方消息解析失败: {e}\n{traceback.format_exc()}"
                )
            raise

    # ------------------------------------------------------------------ #
    # 群消息
    # ------------------------------------------------------------------ #

    async def _parse_group_message(self, data: dict) -> Optional[BroadcastInfo]:
        connector = self.connector
        id_mapper = connector.id_mapper

        group_openid = data.get("group_openid", "")
        author = data.get("author", {}) or {}
        member_openid = author.get("member_openid", "")

        # 先入回复池（Gensokyo 式绕限额的关键：收到即可回复）
        msg_id = data.get("id", "")
        if msg_id:
            connector.reply_pool.add(group_openid, msg_id)

        # 新群首次出现：尝试绑定到配置中的真实群号槽位
        # （用户在 permissions.group_ids 里填了真实群号，此群第一条
        #   消息到达时按登记顺序自动绑定，实现旧配置完全兼容）
        if not id_mapper.legacy_group_display_id(group_openid):
            id_mapper.bind_group_openid(group_openid)

        # openid → 虚拟号 / 绑定真实群号
        legacy_group = id_mapper.to_legacy_group(group_openid)
        legacy_user = id_mapper.to_legacy_user(member_openid)

        content = data.get("content", "") or ""
        # 去掉 @机器人 标记（官方会在消息头加）
        content = _AT_BOT_PATTERN.sub("", content).strip()

        # 富媒体附件 → image/record/video 段
        segments = self._build_segments(content, data.get("attachments"))

        sender_name = f"成员{legacy_user[-4:]}"

        if await self._should_exclude_user(legacy_user):
            self.logger.debug(f"已在connector层拦截排除用户 {legacy_user} 的消息")
            return None

        # 权限校验：该群是否在监听列表（含绑定真实群号）
        if not self._is_valid_group(legacy_group):
            self.logger.debug(f"群 {legacy_group} 不在监听列表，忽略消息")
            return None

        self._current_source_id = legacy_group

        return BroadcastInfo(
            event_type="message",
            event_sub_type="group",
            message=segments,
            raw=data,
            server=self.server,
            logger=self.logger,
            _source=Source(self.connector.source),
            source_id=str(legacy_group),
            sender=sender_name,
            sender_id=legacy_user,
            is_admin=self._is_admin(legacy_user) or self._is_admin_group(legacy_group),
        )

    # ------------------------------------------------------------------ #
    # C2C 单聊
    # ------------------------------------------------------------------ #

    async def _parse_c2c_message(self, data: dict) -> Optional[BroadcastInfo]:
        connector = self.connector
        id_mapper = connector.id_mapper

        user_openid = data.get("author", {}).get("user_openid", "") or data.get(
            "user_openid", ""
        )
        msg_id = data.get("id", "")
        if msg_id:
            connector.reply_pool.add(f"c2c:{user_openid}", msg_id)

        legacy_user = id_mapper.to_legacy_user(user_openid)
        content = (data.get("content", "") or "").strip()
        content = _AT_BOT_PATTERN.sub("", content).strip()

        segments = self._build_segments(content, data.get("attachments"))

        if await self._should_exclude_user(legacy_user):
            return None

        # 官方单聊默认不放开权限（无 friend_is_admin 概念），保持与管理员列表匹配
        is_admin = self._is_admin(legacy_user)

        return BroadcastInfo(
            event_type="message",
            event_sub_type="private",
            message=segments,
            raw=data,
            server=self.server,
            logger=self.logger,
            _source=Source(self.connector.source),
            source_id=str(legacy_user),
            sender=f"用户{legacy_user[-4:]}",
            sender_id=legacy_user,
            is_admin=is_admin,
        )

    # ------------------------------------------------------------------ #
    # 其他事件
    # ------------------------------------------------------------------ #

    def _parse_friend_add(self, data: dict) -> Optional[BroadcastInfo]:
        openid = data.get("openid", "")
        if not openid:
            return None
        legacy = self.connector.id_mapper.to_legacy_user(openid)
        return BroadcastInfo(
            event_type="notice",
            event_sub_type="friend_add",
            message=[
                {
                    "type": "text",
                    "data": {"text": f"用户 {legacy} 添加了机器人好友"},
                }
            ],
            raw=data,
            server=self.server,
            logger=self.logger,
            _source=Source(self.connector.source),
            source_id=str(legacy),
            sender="",
            sender_id=str(legacy),
        )

    def _parse_group_bot_change(self, data: dict, event_name: str) -> Optional[BroadcastInfo]:
        """群机器人被添加/移除（payload 与群消息一致）。"""
        group_openid = data.get("group_openid", "")
        if not group_openid:
            return None
        legacy = self.connector.id_mapper.to_legacy_group(group_openid)
        op_user = data.get("op_member_openid", "")
        user_legacy = (
            self.connector.id_mapper.to_legacy_user(op_user) if op_user else ""
        )
        action = "添加到" if event_name == "GROUP_ADD_ROM" else "移出"
        return BroadcastInfo(
            event_type="notice",
            event_sub_type="group_bot_change",
            message=[
                {
                    "type": "text",
                    "data": {"text": f"机器人被{action}群 {legacy}"},
                }
            ],
            raw=data,
            server=self.server,
            logger=self.logger,
            _source=Source(self.connector.source),
            source_id=str(legacy),
            sender="",
            sender_id=user_legacy,
        )

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #

    def _is_valid_group(self, legacy_group: str) -> bool:
        """群是否在监听列表（真实群号或已绑定 openid）。"""
        config = self.connector.config
        group_ids = config.get_keys(["connector", "QQ", "permissions", "group_ids"], [])
        admin_groups = config.get_keys(
            ["connector", "QQ", "permissions", "admin_group_ids"], []
        )
        valid = {str(g) for g in (group_ids + admin_groups) if g}
        return str(legacy_group) in valid

    def _is_admin_group(self, legacy_group: str) -> bool:
        config = self.connector.config
        admin_groups = config.get_keys(
            ["connector", "QQ", "permissions", "admin_group_ids"], []
        )
        return str(legacy_group) in [str(g) for g in admin_groups if g]

    def _build_segments(self, content: str, attachments: Optional[list]) -> list:
        """把官方 content + attachments 合成 OneBot array 消息段。

        图片/视频/语音附件：官方 CDN url 有时效（约 30 分钟），这里立即
        下载到插件数据目录缓存（除非配置关闭），转成本地 file:// 路径，
        供 ChatImage/ImagePreview 稳定渲染；同时保留 url 便于点击预览。
        """
        segments = []
        if content:
            segments.append({"type": "text", "data": {"text": content}})

        for att in attachments or []:
            if not isinstance(att, dict):
                continue
            content_type = att.get("content_type", "") or ""
            url = att.get("url", "") or att.get("url_https", "") or ""
            filename = att.get("filename", "附件")
            summary = (att.get("summary", "") or "").strip().strip("[]")
            file_data = {"url": url, "file": filename}
            if summary:
                file_data["summary"] = summary

            # 下载附件到本地缓存（ChatImage 用本地文件最稳）
            local = self._cache_attachment(url, filename, content_type)
            if local:
                file_data["file"] = local

            if content_type.startswith("image"):
                segments.append({"type": "image", "data": file_data})
            elif content_type.startswith("video"):
                segments.append({"type": "video", "data": file_data})
            elif content_type.startswith("audio"):
                segments.append({"type": "record", "data": file_data})
            # 其他附件忽略（官方 file 类型需要额外 file_info 拉取）

        if not segments:
            segments.append({"type": "text", "data": {"text": ""}})
        return segments

    def _cache_attachment(self, url: str, filename: str, content_type: str) -> Optional[str]:
        """下载附件到插件数据目录，返回本地绝对路径（失败返回 None）。"""
        if not url:
            return None
        cache_enabled = self.connector.config.get_keys(
            ["connector", "QQ", "connection", "official", "cache_attachments"], True
        )
        if not cache_enabled:
            return None
        try:
            import hashlib
            import mimetypes
            from pathlib import Path
            import requests as _requests

            data_dir = Path(self.connector.server.get_data_folder()) / "attachments"
            data_dir.mkdir(parents=True, exist_ok=True)

            digest = hashlib.md5(url.encode()).hexdigest()[:12]
            # 扩展名：优先按 content_type 推断，兜底用文件名
            ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
            if not ext and "." in filename:
                ext = "." + filename.rsplit(".", 1)[-1]
            if not ext:
                ext = ".bin"
            local = data_dir / f"{digest}{ext}"

            if not local.exists():
                resp = _requests.get(url, timeout=20)
                resp.raise_for_status()
                local.write_bytes(resp.content)

            return str(local)
        except Exception as e:
            if self.logger:
                self.logger.debug(f"[QQ官方] 附件缓存失败（将直接用 url）: {e}")
            return None
