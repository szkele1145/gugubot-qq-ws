# -*- coding: utf-8 -*-
"""QQ 官方开放平台连接器。

把官方 WS + REST 封装成 GUGUBot 的 BasicConnector，并对外提供
OneBot 风格的 ``bot`` 对象（``send_group_msg`` / ``send_private_msg`` /
``send_temp_msg`` / ``set_group_card`` / ``get_msg`` / ``get_group_member_list``
……），使 bound / inactive_check / unbound_check / cross_broadcast 等
既有系统零改动兼容。

配置在原 ``connector.QQ.connection`` 下新增 ``official`` 段（与旧
OneBot 配置并存，type 字段切换）：

.. code-block:: yaml

    connector:
      QQ:
        connection:
          type: official          # onebot(默认) | official
          official:
            appid: "123456789"
            secret: "xxxxxxxx"
            sandbox: false
            intents:              # 可选，默认群+C2C
              group_at_messages: true
              c2c_messages: true
              direct_message: false
              guild_messages: false
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
import traceback
from typing import Any, List, Optional

from gugubot.config import BotConfig
from gugubot.connector.basic_connector import BasicConnector
from gugubot.connector.qq_official.api_client import (
    QQOfficialAPIError,
    QQOfficialClient,
)
from gugubot.connector.qq_official.id_map import QQIDMapper
from gugubot.connector.qq_official.parser import QQOfficialParser
from gugubot.connector.qq_official.reply_pool import QQReplyPool
from gugubot.connector.qq_official.token_manager import QQTokenManager
from gugubot.connector.qq_official.ws_client import (
    INTENT_DIRECT_MESSAGE,
    INTENT_GROUP_AND_C2C_EVENT,
    INTENT_INTERACTION,
    INTENT_PUBLIC_GUILD_MESSAGES,
    QQOfficialWSClient,
)
from gugubot.utils.message import extract_images, segments_to_plain_text
from gugubot.utils.types import ProcessedInfo

# 模块级预编译（与 OneBot connector 的正则/flag 完全一致，语义不变）
_COLOR_CODE_PATTERN = re.compile(r"§[0-9a-fk-or]", re.IGNORECASE)

# ---------------------------------------------------------------------------
# 官方 OpenAPI 错误码表
#
# 取自官方文档（2026-09 版）：
#   · 《发送群聊消息 · 错误码》
#   · 《API 调用指南 · OpenAPI 错误码》
#
# 注意：官方失败响应的**业务错误码在响应体里**，HTTP 状态码可能仍是 200，
# 字段名是 ``err_code``（不是 ``code``）——见 api_client._request 的处理。
# ---------------------------------------------------------------------------

# 被动回复引用（msg_id / event_id / msg_seq）失效 → 必须丢弃该额度换下一条，
# 重试同一个 id 必然继续失败。
_STALE_REPLY_REF_CODES = frozenset({
    304026,    # MSG_ID 回复的消息 id 错误
    304027,    # MSG_EXPIRE 回复的消息过期
    304103,    # 消息ID已过期，不能回复
    40034005,  # 回复消息 msg_id 已过期
    40034024,  # 请求参数 msg_id 无效或越权
    40034025,  # 请求参数 event_id 无效
    40034026,  # 请求参数 event_id 已过期
    40034027,  # 该事件不支持回复消息
    40034128,  # 被动回复时间或次数超限
    40054005,  # 消息被去重（相同 msg_id + msg_seq 重复发送）
})

# 频控 / 主动消息超额 → 退避后复用同一条额度重试。
_RATE_LIMIT_CODES = frozenset({
    11244,     # （旧版）主动消息频控
    20028,     # ChannelHitWriteRateLimit
    304045,    # 子频道主动消息数限频
    304047,    # 主动消息推送超过限制的子频道数
    304049,    # 私信主动消息数限频
    304050,    # 私信主动消息总量限频
    304052,    # 发消息设置引导超频
    40034100,  # 主动消息发送超过频控限制
    1100100,   # 安全打击：消息被限频
    1100308,   # 触发频道内限频
})


def strip_minecraft_color_codes(text: str) -> str:
    """去掉 MC 颜色代码（与 OneBot connector 保持一致）。

    使用模块级预编译正则，省掉每次调用时 ``re.sub`` 的模式缓存查找。
    """
    return _COLOR_CODE_PATTERN.sub("", text)


class QQOfficialBot:
    """OneBot 风格 API 包装（供既有系统调用，接口签名与 OneBot Bot 一致）。

    所有 group_id / user_id 参数接收「下游号码」（真实绑定群号或虚拟号），
    内部经 :class:`QQIDMapper` 翻译回 openid 后调官方 API。
    """

    def __init__(self, connector: "QQOfficialConnector") -> None:
        self._c = connector
        self.self_id = None  # 官方无真实 QQ 号；READY 后填开放平台 bot id

    # ------------------------------------------------------------------ #
    # 发消息
    # ------------------------------------------------------------------ #

    @staticmethod
    def _filter_images(segments, sent_data_ids=None) -> list:
        """滤掉已成功作为富媒体发送的图片段。

        仅去掉发送成功的段：文本里不再出现 [图片:xx] 占位重复；
        跳过/失败的段保留占位，让 QQ 端知道此处有图。sent_data_ids 为
        ``extract_images`` 返回的、已成功发送的 data 对象 id 集合。
        """
        if not isinstance(segments, list):
            return segments
        ids = sent_data_ids or set()
        return [
            seg
            for seg in segments
            if not (
                isinstance(seg, dict)
                and seg.get("type") == "image"
                and id(seg.get("data", {})) in ids
            )
        ]

    async def _send_media_by_url(
        self, media_url: str, file_type: int, *, is_group: bool, openid: str
    ) -> None:
        """上传并发送一条富媒体（公网 URL）。失败抛 QQOfficialAPIError。

        媒体强制主动消息（use_reply_pool=False）：多图连发不消耗宝贵的
        被动回复 seq，把额度留给随后的文本消息。
        """
        api = self._c.api_client
        # requests 是同步阻塞库——移到线程池执行，避免多图上传时
        # 串行阻塞 MCDR 事件循环（每张图一次完整 REST 往返）
        if is_group:
            await asyncio.to_thread(
                api.send_group_media_message,
                openid, file_type, media_url, use_reply_pool=False
            )
        else:
            await asyncio.to_thread(
                api.send_c2c_media_message,
                openid, file_type, media_url, use_reply_pool=False
            )

    async def _send_medias(self, message, openid: str, *, is_group: bool) -> set:
        """图片段（及可选视频段）→ 官方富媒体。

        官方富媒体 API 只接受公网 URL；本地文件无法上传，跳过并记
        warning。单张上传抛 QQOfficialAPIError 时只警告继续下一张，
        不让整个 send 失败（文本照发）。

        Returns
        -------
        set
            已成功发送的图片 data 对象 id 集合（供过滤文本占位）。
        """
        log = self._c.logger
        prefix = self._c.log_prefix
        sent_data_ids: set = set()

        # 图片段 → file_type=1
        for data in extract_images(message):
            url = (data.get("url") or "").strip()
            file_ = (data.get("file") or "").strip()
            summary = data.get("summary", "")
            # 公网地址优先 url，兜底 file（两者都可能是 http）
            if url.lower().startswith(("http://", "https://")):
                src = url
            elif file_.lower().startswith(("http://", "https://")):
                src = file_
            else:
                if log:
                    # 一次性提示（本地图片反复出现时不刷屏）；本连接器
                    # 生命周期内只提醒一次"官方平台不支持本地图片"
                    warned = getattr(self._c, "_warned_local_media", False)
                    if not warned:
                        self._c._warned_local_media = True
                        log.warning(
                            f"{prefix} 官方平台不支持本地图片，此类图片将跳过"
                            "（如需转发图片请配置公网图床；本提示仅显示一次）"
                        )
                continue
            try:
                await self._send_media_by_url(src, 1, is_group=is_group, openid=openid)
            except QQOfficialAPIError as e:
                if log:
                    log.warning(f"{prefix} 官方平台图片上传/发送失败，跳过该图: {e}")
                continue
            sent_data_ids.add(id(data))

        # 可选：视频段（url 为公网 http）→ file_type=2
        if isinstance(message, list):
            for seg in message:
                if not (isinstance(seg, dict) and seg.get("type") == "video"):
                    continue
                vurl = (((seg.get("data") or {}).get("url") or "")).strip()
                if not vurl.lower().startswith(("http://", "https://")):
                    continue
                try:
                    await self._send_media_by_url(
                        vurl, 2, is_group=is_group, openid=openid
                    )
                except QQOfficialAPIError as e:
                    if log:
                        log.warning(f"{prefix} 官方平台视频上传/发送失败，跳过该视频: {e}")

        return sent_data_ids

    async def send_group_msg(self, group_id, message, **kwargs) -> dict:
        openid = self._c.id_mapper.to_official_group(group_id)

        # 图片/视频 → 官方富媒体（仅公网 URL；本地文件跳过并警告）
        sent_ids = await self._send_medias(message, openid, is_group=True)

        # 已成功发出的图片不再以 [图片:xx] 占位重复出现在文本里
        text_segments = self._filter_images(message, sent_ids)
        content = segments_to_plain_text(text_segments)
        if content:
            return await self._c._send_group(openid, content)
        return {"status": "ok", "retcode": 0, "data": None}

    async def send_private_msg(self, user_id, message, **kwargs) -> dict:
        openid = self._c.id_mapper.to_official_user(user_id)

        # 图片/视频 → 官方富媒体
        sent_ids = await self._send_medias(message, openid, is_group=False)

        # 已成功发出的图片不再以占位重复出现在文本里
        text_segments = self._filter_images(message, sent_ids)
        content = segments_to_plain_text(text_segments)
        if content:
            return await self._c._send_c2c(openid, content)
        return {"status": "ok", "retcode": 0, "data": None}

    async def send_temp_msg(self, group_id, user_id, message, **kwargs) -> dict:
        """官方无临时会话；降级为 C2C 单聊。"""
        return await self.send_private_msg(user_id, message)

    # ------------------------------------------------------------------ #
    # 查询类（官方 API 能力有限，尽量模拟 OneBot 返回结构）
    # ------------------------------------------------------------------ #

    async def get_msg(self, message_id, **kwargs) -> Optional[dict]:
        """官方无「按 id 查消息」公开接口 → 返回 None（调用方已有 None 兜底）。"""
        return None

    async def get_group_member_list(self, group_id, **kwargs) -> Optional[dict]:
        """官方无成员列表接口 → 返回「不支持」标记。

        官方平台不提供群成员列表接口，调用方（如 bound 系统的「多余绑定
        检查/移除」）依赖成员列表判断绑定者是否还在群里。若这里返回空
        列表，调用方会把**所有**绑定误判为「不在群里」——检查命令恒报
        全部多余，移除命令则会把全部绑定数据清空（数据丢失级）。

        故此处用 ``data: None`` 明确表达「无法获取」而非「没有成员」，
        由调用方识别后跳过相关操作。
        """
        return {"status": "ok", "retcode": 0, "data": None, "unsupported": True}

    async def get_group_info(self, group_id, **kwargs) -> Optional[dict]:
        """官方无群信息接口 → 返回基本结构。"""
        return {
            "status": "ok",
            "retcode": 0,
            "data": {
                "group_id": int(group_id) if str(group_id).isdigit() else 0,
                "group_name": f"群{group_id}",
            },
        }

    async def set_group_card(self, group_id, user_id, card, **kwargs) -> None:
        """官方 API 不支持改群名片 → 记 debug 日志跳过。"""
        if self._c.logger:
            self._c.logger.debug(
                f"[QQ官方] 官方平台不支持设置群名片，跳过 ({user_id} → {card})"
            )
        return None

    async def get_login_info(self, **kwargs) -> dict:
        return {
            "status": "ok",
            "retcode": 0,
            "data": {"user_id": self.self_id or "", "nickname": "GUGUBot"},
        }

    async def get_stranger_info(self, user_id, **kwargs) -> dict:
        legacy = str(user_id)
        return {
            "status": "ok",
            "retcode": 0,
            "data": {
                "user_id": legacy if legacy.isdigit() else 0,
                "nickname": f"用户{legacy[-4:] if len(legacy) >= 4 else legacy}",
            },
        }

    async def delete_msg(self, message_id, **kwargs) -> None:
        """官方无撤回接口（群机器人权限不足）→ 跳过。"""
        return None

    async def get_version_info(self, **kwargs) -> dict:
        return {
            "status": "ok",
            "retcode": 0,
            "data": {"app_name": "gugubot-official", "app_version": self._plugin_version()},
        }

    def _plugin_version(self) -> str:
        """插件真实版本号（优先 MCDR 元数据，其次打包内 json，最后 unknown）。

        不硬编码：bump 版本后 ``#状态`` 显示的 app_version 必须自动跟随，
        否则会报出与实际打包版本不符的旧号。
        """
        server = getattr(self._c, "server", None)
        try:
            meta = server.get_self_metadata()
            version = str(getattr(meta, "version", "") or "").strip()
            if version:
                return version
        except Exception:
            pass
        try:
            with server.open_bundled_file("mcdreforged.plugin.json") as f:
                data = json.loads(f.read().decode("utf-8"))
            version = str(data.get("version", "") or "").strip()
            if version:
                return version
        except Exception:
            pass
        return "unknown"

    # 兼容 OneBot Bot 的请求跟踪字段（BasicSystem 某些路径会读 function_return）
    function_return: dict = {}
    pending_requests: dict = {}


class QQOfficialConnector(BasicConnector):
    """QQ 官方开放平台直连连接器。"""

    def __init__(self, server, config: Optional[BotConfig] = None):
        source_name = config.get_keys(["connector", "QQ", "source_name"], "QQ")
        super().__init__(
            source=source_name, parser=QQOfficialParser, server=server, config=config
        )

        connector_basic_name = self.server.tr("gugubot.connector.name")
        self.log_prefix = f"[{connector_basic_name}{self.source}-官方]"

        self.id_mapper = QQIDMapper(
            data_file=self._resolve_data_file(server), logger=self.logger
        )
        self.reply_pool = QQReplyPool(logger=self.logger)
        self.token_manager = QQTokenManager(
            appid=config.get_keys(["connector", "QQ", "connection", "official", "appid"], ""),
            secret=config.get_keys(["connector", "QQ", "connection", "official", "secret"], ""),
            logger=self.logger,
        )
        self.api_client = QQOfficialClient(
            self.token_manager, self.reply_pool, logger=self.logger
        )

        # 登记配置中的真实群号，供 openid 自动绑定
        group_ids = config.get_keys(["connector", "QQ", "permissions", "group_ids"], [])
        forward_ids = config.get_keys(
            ["connector", "QQ", "permissions", "forward_group_ids"], []
        )
        admin_groups = config.get_keys(
            ["connector", "QQ", "permissions", "admin_group_ids"], []
        )
        notice_groups = config.get_keys(
            ["connector", "QQ", "permissions", "notice_forward_groups"], []
        )
        # !!qq 跨平台广播的自定义转发目标群也参与绑定
        cross_groups = config.get_keys(
            ["system", "cross_broadcast", "qq_forward_group_ids"], []
        )
        if self.logger:
            self.logger.info(
                f"{self.log_prefix} 监听配置读取结果：group_ids={group_ids!r} "
                f"forward_group_ids={forward_ids!r} admin_group_ids={admin_groups!r} "
                f"notice_forward_groups={notice_groups!r} "
                f"cross_qq_forward={cross_groups!r}"
                + ("（提示：均为空 = 宽松模式监听所有已绑定群）"
                   if not any([group_ids, forward_ids, admin_groups, notice_groups, cross_groups]) else "")
            )
        # 通知群/跨平台转发群也进绑定槽位（通知与 !!qq 需要向这些群发送）
        self.id_mapper.register_legacy_groups(
            group_ids + forward_ids + admin_groups + notice_groups + cross_groups
        )

        # intents
        self._intents = self._build_intents(config)

        self.ws_client: Optional[QQOfficialWSClient] = None
        self.bot = QQOfficialBot(self)

    def reload_config(self) -> list:
        """配置热重载后重新登记监听群与刷新收发开关。

        - 调 :meth:`BasicConnector.reload_config` 刷新 enable/enable_receive/
          enable_send 三个构造期缓存；
        - **重新登记监听群**：配置里新增的群号/openid 会进入 id_mapper 的
          绑定槽位（在群里 @机器人 说句话即可自动绑定），已绑定关系保持
          不变——这是官方模式热重载里最有价值的部分：加监听群免重启。
        - appid/secret/intents 属连接级参数，无法热生效（连接器已在运行），
          由 ConfigReloadSystem 的固定提示兜底说明。
        """
        changed = super().reload_config()

        config = self.config
        group_ids = config.get_keys(["connector", "QQ", "permissions", "group_ids"], [])
        forward_ids = config.get_keys(
            ["connector", "QQ", "permissions", "forward_group_ids"], []
        )
        admin_groups = config.get_keys(
            ["connector", "QQ", "permissions", "admin_group_ids"], []
        )
        notice_groups = config.get_keys(
            ["connector", "QQ", "permissions", "notice_forward_groups"], []
        )
        cross_groups = config.get_keys(
            ["system", "cross_broadcast", "qq_forward_group_ids"], []
        )
        try:
            self.id_mapper.register_legacy_groups(
                group_ids + forward_ids + admin_groups + notice_groups + cross_groups
            )
            changed.append("官方直连监听群已重新登记（新群需在群内 @机器人 完成绑定）")
        except Exception as e:
            if self.logger:
                self.logger.error(f"{self.log_prefix} 热重载重新登记监听群失败: {e}")
            changed.append(f"⚠️ 监听群重新登记失败: {e}")
        return changed

    # ------------------------------------------------------------------ #
    # 配置辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _resolve_data_file(server) -> Optional[Any]:
        try:
            from pathlib import Path
            data_folder = Path(server.get_data_folder())
            return data_folder / "qq_official_ids.json"
        except Exception:
            return None

    def _build_intents(self, config: BotConfig) -> int:
        """按官方 intent 表构建订阅位。

        群/单聊事件全部属于 GROUP_AND_C2C_EVENT (1<<25)——只要开通了
        「QQ群」或「QQ单聊」任一能力就订阅该位；群全量消息
        （GROUP_MESSAGE_CREATE，无需@机器人）由同一 intent 位下发，
        受机器人「接收所有消息」开关控制。
        """
        intents_cfg = config.get_keys(
            ["connector", "QQ", "connection", "official", "intents"], {}
        )
        intents = 0
        # 群@消息 / 全量群消息 / C2C —— 同一位 (1<<25)
        group_at = intents_cfg.get("group_at_messages", True)
        c2c = intents_cfg.get("c2c_messages", True)
        if group_at or c2c:
            intents |= INTENT_GROUP_AND_C2C_EVENT
        # 频道类（默认关，避免无权限位导致 4014）
        if intents_cfg.get("direct_message", False):
            intents |= INTENT_DIRECT_MESSAGE
        if intents_cfg.get("guild_messages", False):
            intents |= INTENT_PUBLIC_GUILD_MESSAGES
        if intents_cfg.get("interaction", False):
            intents |= INTENT_INTERACTION
        return intents

    # ------------------------------------------------------------------ #
    # 连接生命周期
    # ------------------------------------------------------------------ #

    async def connect(self) -> None:
        if not self.token_manager.configured:
            raise RuntimeError(
                "QQ 官方平台未配置：请在 connector.QQ.connection.official 下填写 appid / secret"
            )

        self.logger.info(
            f"{self.log_prefix} {self.server.tr('gugubot.connector.QQ.try_connect', url='wss://api.sgroup.qq.com/websocket')}"
        )

        def on_ready(payload: dict) -> None:
            user = payload.get("user") or {}
            self.bot.self_id = user.get("id")
            self.logger.info(
                f"{self.log_prefix} {self.server.tr('gugubot.connector.QQ.connect_success')}"
            )

        self.ws_client = QQOfficialWSClient(
            api_client=self.api_client,
            token_manager=self.token_manager,
            intents=self._intents,
            logger=self.logger,
            on_ready=on_ready,
            on_dispatch=self._on_ws_dispatch,
            reply_pool=self.reply_pool,
        )
        self.ws_client.connect()

    async def disconnect(self) -> None:
        try:
            if self.ws_client:
                self.ws_client.disconnect()
            # 关闭解析器的附件下载线程池（不等待，避免阻塞卸载）
            try:
                parser = getattr(self, "parser", None)
                if parser is not None and hasattr(parser, "shutdown"):
                    parser.shutdown()
            except Exception:
                pass
            # 确保映射表落盘
            try:
                self.id_mapper.flush()
            except Exception:
                pass
            self.logger.info(
                f"{self.log_prefix} {self.server.tr('gugubot.connector.QQ.close_info')}"
            )
        except Exception as e:
            self.logger.warning(
                f"{self.log_prefix} 关闭连接时出错: {e}\n{traceback.format_exc()}"
            )
            raise

    def _on_ws_dispatch(self, event_type: str, seq: int, data: dict, event_id: str = "") -> None:
        """WS Dispatch 事件 → 丢给 parser（MCDR 事件循环）。"""
        if not self.enable:
            return
        # 事件到达日志（debug 级：高频，避免 info 刷屏；排查时开 debug）
        if self.logger:
            show_msg = str(data.get("content", ""))[:40] if isinstance(data, dict) else ""
            self.logger.debug(
                f"{self.log_prefix} 收到官方事件 {event_type} (seq={seq}){('：' + show_msg) if show_msg else ''}"
            )
        try:
            task = {"_event_name": event_type, "_data": data, "_event_id": event_id}
            # process_message 是协程函数：必须包装成可调用对象再交给
            # schedule_task，否则直接调用会传入一个协程对象（不可被调度），
            # 抛 TypeError 被外层 try/except 吞掉，导致全部 WS 事件丢失。
            self.server.schedule_task(lambda: self.parser.process_message(task))
        except Exception as e:
            self.logger.error(
                f"{self.log_prefix} 消息处理失败: {e}\n{traceback.format_exc()}"
            )

    # ------------------------------------------------------------------ #
    # BasicConnector 接口
    # ------------------------------------------------------------------ #

    async def on_message(self, raw: Any) -> None:
        """on_message 是 BasicConnector 的抽象接口；事件入口在 _on_ws_dispatch。"""
        if isinstance(raw, dict) and "_event_name" in raw:
            self._on_ws_dispatch(
                raw["_event_name"], raw.get("seq", 0), raw.get("_data", raw),
                raw.get("_event_id", ""),
            )

    async def send_message(self, processed_info: ProcessedInfo) -> None:
        """广播发送：把 ProcessedInfo 投递到各目标群/C2C。"""
        if not self.enable or not self.enable_send:
            return

        forward_group_ids = self.config.get_keys(
            ["connector", "QQ", "permissions", "forward_group_ids"], []
        )
        if not forward_group_ids or not any(forward_group_ids):
            forward_group_ids = self.config.get_keys(
                ["connector", "QQ", "permissions", "group_ids"], []
            )
        forward_group_target = {
            str(g): "group" for g in forward_group_ids if g
        }
        target = processed_info.target or forward_group_target

        message = processed_info.processed_message
        source = processed_info.source

        message = self._strip_color_codes_from_message(message)

        # 非 QQ 来源 → 加 [{来源}] {发送者}: 前缀（与 OneBot connector 一致；
        # source_name 用户可自定义，不能硬编码 "QQ"）
        if not source.is_from(self.source) and source.origin and processed_info.sender:
            chat_templates = self.config.get_keys(
                ["connector", "QQ", "chat_templates"], []
            )
            formatted_text = self._pick_template_text(chat_templates)
            if not formatted_text:
                formatted_text = f"[{source.origin}] {processed_info.sender}: "
            else:
                formatted_text = formatted_text.format(
                    display_name=source.origin, sender=processed_info.sender
                )

            from gugubot.builder import CQHandler
            source_message = CQHandler.parse(formatted_text)
            message = source_message + message

        elif (
            not source.is_from(self.source)
            and source.origin
            and processed_info.sender == ""
        ):
            from gugubot.builder import CQHandler
            message = CQHandler.parse(f"[{source.origin}] ") + message

        max_message_length = self.config.get_keys(
            ["connector", "QQ", "max_message_length"], 2000
        )
        message_parts = self._split_message(message, max_length=max_message_length)

        # 节流参数循环外一次读取
        # 用户可能把配置写成非数字字符串（如 "0.5 秒"）→ float() 抛 ValueError
        # 会让整条消息发送失败；此处兜底回默认值
        try:
            t_min = float(self.config.get_keys(
                ["connector", "QQ", "connection", "official", "send_throttle_min"], 0.5
            ))
        except (TypeError, ValueError):
            t_min = 0.5
        try:
            t_max = float(self.config.get_keys(
                ["connector", "QQ", "connection", "official", "send_throttle_max"], 1.5
            ))
        except (TypeError, ValueError):
            t_max = 1.5
        if t_max < t_min:
            t_min, t_max = t_max, t_min
        multi_part_throttle = len(message_parts) > 1

        async def _send_one(target_id: str, target_type: str) -> None:
            """向单个目标发送全部分片；跨目标相互独立、互不阻塞。"""
            if not target_id:
                return
            # 官方模式防护：目标群还没绑定 openid 时跳过并提示。
            # 放行三类：①已绑定的真实/虚拟群号 ②openid 原文（用户直接填
            # openid 进配置的场景）③映射表能翻译的号码。
            if target_type == "group":
                t = str(target_id)
                mapped = self.id_mapper.to_official_group(t)
                bound = (
                    self.id_mapper.is_legacy_group(t)
                    or (mapped != t)       # 能映射出不同的 openid
                    or not t.isdigit()      # openid 原文（非纯数字）
                )
                if not bound:
                    # 每个未绑定群只 warning 一次（MC 每条聊天都广播，
                    # 不去重会刷屏）；成功绑定后从去重集移除
                    warned = getattr(self, "_warned_unbound", None)
                    if warned is None:
                        warned = self._warned_unbound = set()
                    if t not in warned:
                        warned.add(t)
                        self.logger.warning(
                            f"{self.log_prefix} 群 {target_id} 尚未绑定 openid"
                            f"（群内 @机器人 发条消息即可自动绑定），后续将静默跳过"
                        )
                    return

            for part in message_parts:
                try:
                    if target_type == "group":
                        await self.bot.send_group_msg(group_id=target_id, message=part)
                    elif target_type == "private":
                        await self.bot.send_private_msg(
                            user_id=target_id, message=part
                        )
                    else:
                        await self.bot.send_temp_msg(
                            group_id=target_type, user_id=target_id, message=part
                        )
                except QQOfficialAPIError as e:
                    self.logger.warning(
                        f"{self.log_prefix} 发送到 {target_id} 失败: {e}"
                    )
                    # 主动消息超限（errcode 1300xx / 240xxx 等）时提前终止该群
                    if self._is_rate_limited(e):
                        break

                # 分片间节流（参数循环外已读取）
                if multi_part_throttle:
                    await asyncio.sleep(random.uniform(t_min, t_max))

        # 多目标群并行发送（跨群 REST 独立限额、互不阻塞），
        # 单群内部分片顺序与节流保持不变；任一目标失败不影响其余目标。
        await asyncio.gather(
            *(_send_one(tid, ttype) for tid, ttype in target.items()),
            return_exceptions=True,
        )

    # ------------------------------------------------------------------ #
    # 内部发送
    # ------------------------------------------------------------------ #

    async def _send_group(self, group_openid: str, content: str) -> dict:
        """发群消息；对限频错误做短重试。

        首次调用**先取池**把结果（含 None）显式传递，杜绝 send_group_message
        内部二次取池；重试全程复用同一额度——绝不重复取池（每条仅 5 次）。
        REST 为同步 requests——统一 to_thread 执行，不阻塞事件循环。
        """
        last_err: Optional[Exception] = None
        # 先取被动额度：有 → 显式传参；无 → event_id 通道（也只取一次）
        taken = self.reply_pool.take(group_openid)
        event_id = None
        if taken is None:
            event_id = self.reply_pool.take_event(group_openid)
        attempt = 0
        # 「被动引用失效换额度」的次数上限。独立于 attempt 计数：
        # 换到的是全新额度，必须给一次被真正使用的机会（不能因轮次
        # 已用尽而白白浪费）；但池里若全是失效额度也不能无限换下去。
        swaps = 0
        _MAX_SWAPS = 3
        while attempt < 3:
            try:
                if taken:
                    mid, seq = taken
                    return await asyncio.to_thread(
                        self.api_client.send_group_message,
                        group_openid, content, msg_id=mid, msg_seq=seq
                    )
                if event_id:
                    return await asyncio.to_thread(
                        self._request_group_with_event, group_openid, content, event_id
                    )
                # msg_id 与 event_id 均无货：纯主动消息（显式请求，不取池）
                return await asyncio.to_thread(
                    self.api_client._request,
                    "POST",
                    f"/v2/groups/{group_openid}/messages",
                    {"msg_type": 0, "content": content},
                )
            except QQOfficialAPIError as e:
                last_err = e
                if e.status in (401, 403):
                    raise
                # 被动额度被官方拒绝（msg_id/event_id 过期、msg_seq 非法或
                # 已用尽）：该条目已成废条目，丢弃并换下一条，否则每次发送
                # 都撞同一个失效 id，白白退避重试 3 次。
                if self._is_stale_reply_ref(e) and swaps < _MAX_SWAPS:
                    if taken:
                        self.reply_pool.drop(group_openid, taken[0])
                    elif event_id:
                        self.reply_pool.drop_event(group_openid, event_id)
                    taken = self.reply_pool.take(group_openid)
                    event_id = None
                    if taken is None:
                        event_id = self.reply_pool.take_event(group_openid)
                    swaps += 1
                    # 换到了全新额度：立即重试，退避对失效 id 毫无意义。
                    # 没换到（池已空）说明本就是纯主动消息，无需空转重试。
                    # **不消耗 attempt**：新额度必须有一次被真正使用的机会。
                    if taken or event_id:
                        continue
                if self._is_rate_limited(e) and attempt < 2:
                    attempt += 1
                    await asyncio.sleep(1 + attempt)
                    continue
                attempt += 1
                await asyncio.sleep(0.5 * attempt)
        raise last_err or RuntimeError("未知发送错误")

    def _request_group_with_event(self, group_openid: str, content: str, event_id: str) -> dict:
        """用 event_id 备用通道发群消息（额度已显式持有，不再取池）。"""
        payload = {"msg_type": 0, "content": content, "event_id": event_id}
        return self.api_client._request(
            "POST", f"/v2/groups/{group_openid}/messages", payload
        )

    def _request_c2c_with_event(self, user_openid: str, content: str, event_id: str) -> dict:
        """用 event_id 备用通道发 C2C 消息（额度已显式持有，不再取池）。"""
        payload = {"msg_type": 0, "content": content, "event_id": event_id}
        return self.api_client._request(
            "POST", f"/v2/users/{user_openid}/messages", payload
        )

    async def _send_c2c(self, user_openid: str, content: str) -> dict:
        """发 C2C 消息；限频/网络错误短重试（首取额度后全程复用，无二次取池）。"""
        last_err: Optional[Exception] = None
        pool_key = f"c2c:{user_openid}"
        # 与群聊路径对称：msg_id 池 → event_id 通道 → 纯主动消息
        taken = self.reply_pool.take(pool_key)
        event_id = None
        if taken is None:
            event_id = self.reply_pool.take_event(pool_key)
        attempt = 0
        # 「被动引用失效换额度」次数上限（独立于 attempt，见 _send_group 注释）
        swaps = 0
        _MAX_SWAPS = 3
        while attempt < 3:
            try:
                if taken:
                    mid, seq = taken
                    return await asyncio.to_thread(
                        self.api_client.send_c2c_message,
                        user_openid, content, msg_id=mid, msg_seq=seq
                    )
                if event_id:
                    return await asyncio.to_thread(
                        self._request_c2c_with_event, user_openid, content, event_id
                    )
                # 池空：纯主动消息（显式请求，不取池；to_thread 不阻塞循环）
                return await asyncio.to_thread(
                    self.api_client._request,
                    "POST",
                    f"/v2/users/{user_openid}/messages",
                    {"msg_type": 0, "content": content},
                )
            except QQOfficialAPIError as e:
                last_err = e
                if e.status in (401, 403):
                    raise
                # 被动引用失效：丢弃并换下一条，避免每次撞同一个废 id
                if self._is_stale_reply_ref(e) and swaps < _MAX_SWAPS:
                    if taken:
                        self.reply_pool.drop(pool_key, taken[0])
                    elif event_id:
                        self.reply_pool.drop_event(pool_key, event_id)
                    taken = self.reply_pool.take(pool_key)
                    event_id = None
                    if taken is None:
                        event_id = self.reply_pool.take_event(pool_key)
                    swaps += 1
                    # 换到新额度才立即重试；池已空则是纯主动模式，不空转。
                    # 不消耗 attempt：新额度必须有一次被真正使用的机会。
                    if taken or event_id:
                        continue
                if self._is_rate_limited(e) and attempt < 2:
                    attempt += 1
                    await asyncio.sleep(1 + attempt)
                    continue
                attempt += 1
                await asyncio.sleep(0.5 * attempt)
        raise last_err or RuntimeError("未知发送错误")

    @staticmethod
    def _is_stale_reply_ref(e: QQOfficialAPIError) -> bool:
        """被动回复引用（msg_id / event_id / msg_seq）失效类错误。

        与「限频」严格区分：这类错误重试同一个 id 必然继续失败，必须
        丢弃该额度换下一条；限频则应当退避后复用同一条。

        错误码取自官方文档《发送群聊消息 · 错误码》与《API 调用指南 ·
        OpenAPI 错误码》（2026-09 版）：

        - ``304103``  消息ID已过期，不能回复
        - ``40034005`` 回复消息 msg_id 已过期
        - ``40034024`` 请求参数 msg_id 无效或越权
        - ``40034025`` 请求参数 event_id 无效
        - ``40034026`` 请求参数 event_id 已过期
        - ``40034027`` 该事件不支持回复消息
        - ``40034128`` 被动回复时间或次数超限
        - ``40054005`` 消息被去重（msg_id + msg_seq 重复）
        - ``304026``  MSG_ID 回复的消息 id 错误
        - ``304027``  MSG_EXPIRE 回复的消息过期
        """
        if e.code in _STALE_REPLY_REF_CODES:
            return True
        # 文案兜底：官方未列出的错误码时按 message 判断。
        # 注意**不能限定 HTTP 状态码**——官方业务错误常以 HTTP 200 返回
        # （err_code 在 body 里），限定 400/404 会让未收录的失效码漏判，
        # 结果是每次发送都撞同一个废 id、白白退避重试 3 次。
        # 官方文档提醒 message 可能调整，这里只作辅助手段。
        text = (getattr(e, "message", "") or "").lower()
        if not any(k in text for k in ("msg_id", "msgid", "event_id", "msg_seq")):
            return False
        return any(
            k in text
            for k in ("过期", "expired", "invalid", "无效", "非法", "不可用", "已用", "used")
        )

    @staticmethod
    def _is_rate_limited(e: QQOfficialAPIError) -> bool:
        """官方主动消息超限/频控错误码段。"""
        if e.status == 429:
            return True
        if e.code in _RATE_LIMIT_CODES:
            return True
        # 130000~130999 段（旧版频道相关限频码）
        return 130000 <= e.code <= 130999

    # ------------------------------------------------------------------ #
    # 消息转换
    # ------------------------------------------------------------------ #
    # 说明：段→纯文本的转换已统一到 gugubot.utils.message.segments_to_plain_text

    def _pick_template_text(self, chat_templates) -> Optional[str]:
        """从模板配置中按权重随机选一个模板字符串。"""
        if not chat_templates or not isinstance(chat_templates, list):
            return None
        if isinstance(chat_templates[0], dict):
            templates, weights = [], []
            for item in chat_templates:
                for t, w in item.items():
                    templates.append(t)
                    weights.append(w if isinstance(w, (int, float)) else 1)
            return random.choices(templates, weights=weights, k=1)[0]
        return random.choice(chat_templates)

    def _strip_color_codes_from_message(self, message: list) -> list:
        result = []
        for item in message:
            if isinstance(item, dict) and item.get("type") == "text":
                new_item = item.copy()
                new_data = item.get("data", {}).copy()
                if "text" in new_data:
                    new_data["text"] = strip_minecraft_color_codes(new_data["text"])
                new_item["data"] = new_data
                result.append(new_item)
            else:
                result.append(item)
        return result

    # 与 OneBot connector 相同的分片逻辑（官方单条 ≤ 2000 字符无硬限制，
    # 但保守分片防止风控）
    _TYPE_LENGTHS = {"image": 100, "default": 20}

    def _get_item_length(self, item: dict) -> int:
        if item.get("type") == "text":
            return len(item.get("data", {}).get("text", ""))
        return self._TYPE_LENGTHS.get(item.get("type"), self._TYPE_LENGTHS["default"])

    def _split_message(self, message: list, max_length: int = 2000) -> list:
        # max_length 来自配置（connector.QQ.max_message_length），用户可能填 0
        # 或负数：此时 space=0 → chunk 恒为空串 → text 不缩短 → 死循环，
        # 会直接挂死 MCDR 事件循环（不是抛异常）。此处钳制为正整数。
        try:
            max_length = int(max_length)
        except (TypeError, ValueError):
            max_length = 2000
        if max_length <= 0:
            max_length = 2000

        total = sum(self._get_item_length(m) for m in message if isinstance(m, dict))
        if total <= max_length:
            return [message]

        result, current_part, current_len = [], [], 0

        def flush():
            nonlocal current_part, current_len
            if current_part:
                result.append(current_part)
                current_part, current_len = [], 0

        for item in message:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "text":
                item_len = self._get_item_length(item)
                if current_len + item_len > max_length:
                    flush()
                current_part.append(item)
                current_len += item_len
                continue

            text = item.get("data", {}).get("text", "")
            while text:
                space = max_length - current_len
                if space <= 0:
                    flush()
                    space = max_length
                if len(text) <= space:
                    current_part.append({"type": "text", "data": {"text": text}})
                    current_len += len(text)
                    break
                chunk = text[:space]
                if (pos := chunk.rfind("\n")) > space // 2:
                    chunk = text[: pos + 1]
                current_part.append({"type": "text", "data": {"text": chunk}})
                current_len += len(chunk)
                text = text[len(chunk):]
        flush()
        return result
