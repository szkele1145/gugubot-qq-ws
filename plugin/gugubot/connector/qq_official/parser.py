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

import asyncio
import hashlib
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Iterable, Optional

import requests

from gugubot.parser.basic_parser import BasicParser
from gugubot.utils.types import BroadcastInfo, Source

# <@!xxxx> 官方 at 用户/机器人标记（捕获 openid）
_AT_USER_PATTERN = re.compile(r"<@!\s*([A-Za-z0-9_-]+)\s*>")
# 兼容旧名
_AT_BOT_PATTERN = _AT_USER_PATTERN
# <faceType=...,faceId=...,ext=...> QQ 原生小表情标记（清理为空，避免乱码文本）
_FACE_PATTERN = re.compile(r"<faceType=\d+[^>]*>")
# 保留 [CICode,...]] 图片标签（AtomChat/ChatImage 识别）


def _clean_official_content(text: str) -> str:
    """清理官方消息 content 里的平台标记（@机器人、face 表情）。"""
    text = _AT_USER_PATTERN.sub("", text)
    text = _FACE_PATTERN.sub("", text)
    return text


def _id_in(sender_id, ids: Optional[Iterable]) -> bool:
    """配置号列表的成员判断（不预建临时列表，逐项短路比较）。

    等价于 ``str(sender_id) in [str(i) for i in ids if i]``，但省掉了每次
    调用都新建一个列表的开销。列表通常只有个位数，短路比较比 set 更快。
    """
    if not ids:
        return False
    target = str(sender_id)
    for i in ids:
        if i and str(i) == target:
            return True
    return False


def _has_ids(ids: Optional[Iterable]) -> bool:
    """配置号列表里是否存在有效（非空）项。"""
    if not ids:
        return False
    for i in ids:
        if i:
            return True
    return False


class QQOfficialParser(BasicParser):
    """QQ 官方平台事件解析器。"""

    def _is_admin(self, sender_id) -> bool:
        """检查是否是管理员（sender_id 已是虚拟号）。"""
        config = self.connector.config
        admin_ids = config.get_keys(["connector", "QQ", "permissions", "admin_ids"], [])
        if _id_in(sender_id, admin_ids):
            return True
        # 官方平台没有"管理员"概念，管理群内所有人可用
        source_id = getattr(self, "_current_source_id", None)
        if source_id:
            admin_groups = config.get_keys(
                ["connector", "QQ", "permissions", "admin_group_ids"], []
            )
            if _id_in(source_id, admin_groups):
                return True
        return False

    async def _should_exclude_user(self, user_id: str) -> bool:
        """官方平台没有真实 QQ 号，排除列表按虚拟号匹配。"""
        exclude_ids = self.connector.config.get_keys(
            ["system", "bound_notice", "exclude_ids"], []
        )
        return _id_in(user_id, exclude_ids)

    async def parse(self, raw_message) -> Optional[BroadcastInfo]:
        """解析官方 WS 推送的 Dispatch 事件（connector 包装后的任务 dict）。

        Parameters
        ----------
        raw_message : dict
            形如 ``{"_event_name": 事件类型, "_data": 事件体 d, "_event_id": 外层事件id}``。
            兼容直接传事件体 d（缺 ``_event_name`` 时按 None 处理）。

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

            if event_name in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
                # GROUP_MESSAGE_CREATE = 全量群消息（机器人开启"接收所有消息"后，
                # 不用@机器人也能收到；字段与 GROUP_AT_MESSAGE_CREATE 完全一致）
                return await self._parse_group_message(
                    data, event_id=raw_message.get("_event_id", "")
                )
            elif event_name == "C2C_MESSAGE_CREATE":
                return await self._parse_c2c_message(
                    data, event_id=raw_message.get("_event_id", "")
                )
            elif event_name in ("FRIEND_ADD",):
                return self._parse_friend_add(data)
            elif event_name in ("GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT", "GROUP_ADD_ROM", "GROUP_DEL_ROM"):
                return self._parse_group_bot_change(data, event_name)
            return None
        except Exception as e:
            if self.logger:
                import traceback
                self.logger.error(
                    f"QQ官方消息解析失败: {e}\n{traceback.format_exc()}"
                )
            raise

    def _expand_content_segments(
        self,
        content: str,
        id_mapper,
        mention_names: Optional[dict] = None,
        bot_official_id: str = "",
    ) -> list:
        """把官方 content 的文本/@标记流转成消息段序列。

        - `<@!openid>` 原位展开为 at 段（@位置保真）
        - `<@!机器人自身id>` 剔除
        - face 表情等平台标记清理
        """
        mention_names = mention_names or {}
        out = []
        pos = 0
        for m in _AT_USER_PATTERN.finditer(content):
            if m.start() > pos:
                out.append(("text", content[pos:m.start()]))
            openid = m.group(1)
            if not (bot_official_id and openid == bot_official_id):
                out.append(("at", openid))
            pos = m.end()
        if pos < len(content):
            out.append(("text", content[pos:]))

        segments = []
        for kind, value in out:
            if kind == "text":
                cleaned = _clean_official_content(value).strip()
                if cleaned:
                    segments.append({"type": "text", "data": {"text": cleaned}})
            else:
                segments.append({
                    "type": "at",
                    "data": {
                        "qq": id_mapper.to_legacy_user(value),
                        "name": mention_names.get(value, ""),
                    },
                })
        return segments

    # ------------------------------------------------------------------ #
    # 群消息
    # ------------------------------------------------------------------ #

    async def _parse_group_message(
        self, data: dict, event_id: str = ""
    ) -> Optional[BroadcastInfo]:
        connector = self.connector
        id_mapper = connector.id_mapper

        group_openid = data.get("group_openid", "")
        author = data.get("author", {}) or {}
        member_openid = author.get("member_openid", "")

        # 先入回复池（Gensokyo 式绕限额的关键：收到即可回复；附带外层
        # event_id 作为 msg_id 通道用尽后的备用被动回复通道）
        msg_id = data.get("id", "")
        if msg_id:
            connector.reply_pool.add(group_openid, msg_id, event_id=event_id)

        # 新群首次出现：尝试绑定到配置中的真实群号槽位
        # （用户在 permissions.group_ids 里填了真实群号，此群第一条
        #   消息到达时按登记顺序自动绑定，实现旧配置完全兼容）
        if not id_mapper.legacy_group_display_id(group_openid):
            bound = id_mapper.bind_group_openid(group_openid)
            if bound is not None:
                # 绑定成功：清除 connector 的"未绑定警告"去重标记
                # （若该群此前广播被跳过并 warning 过，恢复提示资格）
                warned = getattr(connector, "_warned_unbound", None)
                if warned is not None:
                    warned.discard(str(bound))
            elif self.logger:
                # 无可绑定槽位：该群用虚拟号继续（仅当配置含虚拟号或
                # 后续绑定时才会转发），打 info 便于排查
                display = id_mapper.group_legacy_display(group_openid)
                self.logger.info(
                    f"[QQ官方] 群 {group_openid} 没有可用配置槽位，"
                    f"当前显示号 {display or '(未分配)'}；"
                    f"如需转发请在 group_ids 配置其群号或等待自动绑定"
                )

        # openid → 虚拟号 / 绑定真实群号
        legacy_group = id_mapper.to_legacy_group(group_openid)
        legacy_user = id_mapper.to_legacy_user(member_openid)

        # 廉价校验先行：附件下载带 20s 超时、at 展开也有开销，而机器人
        # 消息 / 排除用户 / 未监听群 在旧实现里会先下载全部图片落盘、构建
        # 完消息段才丢弃，白耗带宽与缓存。这里先把过滤全部做完。
        username = (author.get("username", "") or "").strip()

        # 机器人消息过滤（官方事件 author.bot 字段明确标注机器人身份）：
        # 拦掉 Q群管家等系统机器人的消息，防止转发回声循环
        if author.get("bot", False):
            if self.logger:
                self.logger.debug(f"[QQ官方] 拦截机器人消息（{username}），不转发")
            return None

        if await self._should_exclude_user(legacy_user):
            if self.logger:
                self.logger.debug(f"已在connector层拦截排除用户 {legacy_user} 的消息")
            return None

        # 权限校验：该群是否在监听列表（含绑定真实群号）
        if not self._is_valid_group(legacy_group):
            if self.logger:
                self.logger.info(
                    f"[QQ官方] 群 {legacy_group}（openid {group_openid}）"
                    f"不在监听列表（group_ids/admin_group_ids），消息已忽略"
                )
            return None

        # 供 _is_admin 识别「当前群」（管理群判定依赖它）
        self._current_source_id = legacy_group

        content = data.get("content", "") or ""

        # 富媒体附件 → image/record/video 段
        segments = await self._build_segments("", data.get("attachments"))

        # @用户标记原位展开为 at 段（保留消息中的位置语义）；
        # @机器人自身的标记剔除。mentions 提供用户名映射。
        bot_official_id = str(getattr(self.connector.bot, "self_id", "") or "")
        mention_names = {
            str(m.get("id", "")): (m.get("username", "") or "").strip()
            for m in (data.get("mentions") or [])
            if isinstance(m, dict) and m.get("id")
        }
        segments.extend(
            self._expand_content_segments(
                content, id_mapper, mention_names, bot_official_id
            )
        )

        # 显示名：官方下发真实昵称 username（新平台能力）；没有则回退虚拟号
        sender_name = username or f"成员{legacy_user[-4:]}"

        # 群内角色（新平台能力）：owner/admin 视为管理员
        member_role = (author.get("member_role", "") or "").strip().lower()
        role_is_admin = member_role in ("owner", "admin")

        # 空消息保底（无文本/附件/@时跳过广播，不产出空段列表）
        if not segments:
            return None

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
            is_admin=role_is_admin
            or self._is_admin(legacy_user)
            or self._is_admin_group(legacy_group),
        )

    # ------------------------------------------------------------------ #
    # C2C 单聊
    # ------------------------------------------------------------------ #

    async def _parse_c2c_message(
        self, data: dict, event_id: str = ""
    ) -> Optional[BroadcastInfo]:
        connector = self.connector
        id_mapper = connector.id_mapper

        user_openid = data.get("author", {}).get("user_openid", "") or data.get(
            "user_openid", ""
        )
        legacy_user = id_mapper.to_legacy_user(user_openid)

        # 先入回复池（与群消息同理：先占用被动回复额度；后续过滤即便
        # 丢弃本条消息，已入池的 msg_id 仍可供其他广播做被动回复）
        msg_id = data.get("id", "")
        if msg_id:
            connector.reply_pool.add(f"c2c:{user_openid}", msg_id, event_id=event_id)

        # 廉价校验先行（与群消息同序：避免为将被丢弃的消息下载附件）
        # 机器人消息过滤（与群消息一致：author.bot 拦截防回声）
        author = data.get("author", {}) or {}
        username = (author.get("username", "") or "").strip()
        if author.get("bot", False):
            if self.logger:
                self.logger.debug(
                    f"[QQ官方] 拦截机器人单聊消息（{username}），不转发"
                )
            return None

        if await self._should_exclude_user(legacy_user):
            return None

        content = (data.get("content", "") or "").strip()

        # @标记原位展开为 at 段（与群消息一致）
        bot_official_id = str(getattr(self.connector.bot, "self_id", "") or "")
        segments = await self._build_segments("", data.get("attachments"))
        segments.extend(
            self._expand_content_segments(
                content, id_mapper, {}, bot_official_id
            )
        )

        # 官方单聊默认不放开权限（无 friend_is_admin 概念），保持与管理员列表匹配
        is_admin = self._is_admin(legacy_user)

        # 空消息保底
        if not segments:
            return None

        return BroadcastInfo(
            event_type="message",
            event_sub_type="private",
            message=segments,
            raw=data,
            server=self.server,
            logger=self.logger,
            _source=Source(self.connector.source),
            source_id=str(legacy_user),
            sender=username or f"用户{legacy_user[-4:]}",
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
        # 注意：官方真实事件名是 GROUP_ADD_ROBOT / GROUP_DEL_ROBOT；
        # GROUP_ADD_ROM / GROUP_DEL_ROM 是早期文档笔误留下的兼容别名。
        # 两种「添加」写法都要算添加，否则机器人被拉进群时会误报成「移出」。
        action = (
            "添加到"
            if event_name in ("GROUP_ADD_ROBOT", "GROUP_ADD_ROM")
            else "移出"
        )
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
        """群是否在监听列表。

        规则：
        - 配置了 group_ids/admin_group_ids：只监听列出的群号（含绑定后的真实群号）
        - 未配置任何群号：监听所有「已绑定/已分配」的群（宽松模式）
        """
        config = self.connector.config
        group_ids = config.get_keys(["connector", "QQ", "permissions", "group_ids"], [])
        admin_groups = config.get_keys(
            ["connector", "QQ", "permissions", "admin_group_ids"], []
        )
        if not _has_ids(group_ids) and not _has_ids(admin_groups):
            # 宽松模式：没配置群号时，凡是有映射（真实绑定或虚拟分配）的群都放行
            return bool(legacy_group)
        return _id_in(legacy_group, group_ids) or _id_in(legacy_group, admin_groups)

    def _is_admin_group(self, legacy_group: str) -> bool:
        config = self.connector.config
        admin_groups = config.get_keys(
            ["connector", "QQ", "permissions", "admin_group_ids"], []
        )
        return _id_in(legacy_group, admin_groups)

    async def _build_segments(self, content: str, attachments: Optional[list]) -> list:
        """把官方 content + attachments 合成 OneBot array 消息段。

        图片/视频/语音附件：官方 CDN url 有时效（约 30 分钟），这里立即
        下载到插件数据目录缓存（除非配置关闭），转成本地 file:// 路径，
        供 ChatImage/ImagePreview 稳定渲染；同时保留 url 便于点击预览。
        下载经 to_thread 执行——CDN 下载 20s 超时不阻塞事件循环。
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

            # 下载附件到本地缓存（ChatImage 用本地文件最稳；线程池执行）
            local = await asyncio.to_thread(
                self._cache_attachment, url, filename, content_type
            )
            if local:
                file_data["file"] = local

            if content_type.startswith("image"):
                segments.append({"type": "image", "data": file_data})
            elif content_type.startswith("video"):
                segments.append({"type": "video", "data": file_data})
            elif content_type.startswith("audio"):
                segments.append({"type": "record", "data": file_data})
            # 其他附件忽略（官方 file 类型需要额外 file_info 拉取）

        return segments

    @staticmethod
    def _mentions_to_segments(mentions: Optional[list]) -> list:
        """官方 mentions（@用户列表，含 username/member_openid）→ at 段。

        .. deprecated::
            保留作兼容参考；群消息的 @ 已改为 content 原位展开
            （见 ``_parse_group_message``），不再整体追加到尾部。
        """
        result = []
        for m in mentions or []:
            if not isinstance(m, dict):
                continue
            openid = m.get("id", "") or m.get("member_openid", "") or ""
            name = (m.get("username", "") or "").strip()
            if openid or name:
                result.append({
                    "type": "at",
                    "data": {"qq": openid or name, "name": name},
                })
        return result

    # 附件缓存上限（默认 512MB；0 = 不缓存，-1 = 不清理）
    _CACHE_MAX_BYTES = 512 * 1024 * 1024
    _CACHE_CLEAN_STATE = {"last_check": 0.0}

    def _cache_attachment(self, url: str, filename: str, content_type: str) -> Optional[str]:
        """下载附件到插件数据目录，返回本地绝对路径（失败返回 None）。

        缓存目录按 LRU 策略限制总大小（默认 512MB，超限删最旧文件）——
        官方 CDN url 约 30 分钟过期，缓存的价值窗口很短，无需永久保留。
        每次下载前最多做一次大小检查（10 分钟节流，避免高频消息时
        反复扫描目录）。
        """
        if not url:
            return None
        cache_enabled = self.connector.config.get_keys(
            ["connector", "QQ", "connection", "official", "cache_attachments"], True
        )
        if not cache_enabled:
            return None
        try:
            data_dir = Path(self.connector.server.get_data_folder()) / "attachments"
            data_dir.mkdir(parents=True, exist_ok=True)

            # 大小检查节流（10 分钟一次）
            now = time.time()
            if now - self._CACHE_CLEAN_STATE["last_check"] > 600:
                self._CACHE_CLEAN_STATE["last_check"] = now
                self._cleanup_attachment_cache(data_dir)

            digest = hashlib.md5(url.encode()).hexdigest()[:12]
            # 扩展名：优先按 content_type 推断，兜底用文件名
            ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
            if not ext and "." in filename:
                ext = "." + filename.rsplit(".", 1)[-1]
            if not ext:
                ext = ".bin"
            local = data_dir / f"{digest}{ext}"

            if not local.exists():
                resp = requests.get(url, timeout=20)
                resp.raise_for_status()
                local.write_bytes(resp.content)
                # 触碰 mtime：LRU 依据（重复出现的图保持"最新"）
                os.utime(local, None)

            return str(local)
        except Exception as e:
            if self.logger:
                self.logger.debug(f"[QQ官方] 附件缓存失败（将直接用 url）: {e}")
            return None

    @classmethod
    def _cleanup_attachment_cache(cls, data_dir) -> None:
        """缓存目录超过上限时按 mtime 从旧到新删除，直到回到上限内。

        用 ``os.scandir`` 遍历：Windows 下 DirEntry 自带的 stat 信息无需
        额外系统调用，且每个文件只取一次 stat（旧实现每文件调了两次
        ``f.stat()``）。
        """
        try:
            max_bytes = cls._CACHE_MAX_BYTES
            if max_bytes <= 0:
                return
            files = []
            with os.scandir(data_dir) as it:
                for entry in it:
                    try:
                        if not entry.is_file():
                            continue
                        st = entry.stat()
                    except OSError:
                        continue
                    files.append((st.st_mtime, st.st_size, entry.path))
            total = sum(s for _, s, _ in files)
            if total <= max_bytes:
                return
            # 最旧优先删除
            files.sort(key=lambda x: x[0])
            for _, size, path in files:
                if total <= max_bytes:
                    break
                try:
                    os.unlink(path)
                    total -= size
                except OSError:
                    pass
        except Exception:
            pass
