# -*- coding: utf-8 -*-
"""Connector for a Minecraft server via MCDR."""

import logging
import traceback
from typing import Any, Optional

from gugubot.builder import McMessageBuilder
from gugubot.config import BotConfig
from gugubot.connector.basic_connector import BasicConnector
from gugubot.parser.mc_parser import MCParser
from gugubot.utils.message import segments_to_plain_text, strip_minecraft_color_codes
from gugubot.utils.types import ProcessedInfo


class MCConnector(BasicConnector):
    """Connector for a Minecraft server via MCDR.

    Attributes
    ----------
    server : Any
        MCDR server instance.
    logger : logging.Logger
        Logger instance.
    """

    def __init__(
        self,
        server: Any,
        config: Optional[BotConfig] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """Initialize the Minecraft connector.

        Parameters
        ----------
        server : Any
            MCDR server instance.
        logger : logging.Logger, optional
            Logger instance.  Falls back to ``server.logger`` when omitted.
        """
        source_name = config.get_keys(
            ["connector", "minecraft", "source_name"], "Minecraft"
        )
        super().__init__(
            source=source_name,
            parser=MCParser,
            builder=McMessageBuilder,
            server=server,
            logger=logger or server.logger,
            config=config,
        )

        connector_basic_name = self.server.tr("gugubot.connector.name")
        self.log_prefix = f"[{connector_basic_name}{self.source}]"

    def _is_remote_mc_origin(self, origin: str, qq_source: str) -> bool:
        """Return True if origin is another MC server that should not appear in local chat.

        Allow: self (local MC), QQ, and any locally-registered non-MC connector (Bridge, Test, …).
        Block: unregistered names (remote MC) or another locally-registered MCConnector.
        """
        if not origin or origin in (self.source, qq_source):
            return False
        registered = self.connector_manager.get_connector(origin)
        return registered is None or isinstance(registered, MCConnector)

    def _map_custom_group_name(
        self,
        custom_group_name: dict,
        source_id: str,
        source: str,
        qq_source: str,
    ) -> str:
        """按群号查自定义群名（兼容官方模式的虚实号两套 ID）。

        官方直连模式下，消息的 ``source_id`` 是**虚拟群号**（12 位
        200000xxx），而用户在 ``custom_group_name`` 里填的键是**真实
        QQ 群号**（旧配置）→ 直接查表**永远匹配不上** → 自定义群名
        静默失效，游戏里显示的一直是「QQ」。

        （「改造说明.md」的「群号绑定流程」承诺过「custom_group_name
        等按群号配置的功能照常生效」，这条承诺在官方模式下此前
        并没有真正兑现。）

        这里按两种键都查一遍：
        1. 直接用 ``source_id`` 查（OneBot 模式 / 用户直接填虚拟号）；
        2. 官方模式下把 ``source_id`` 翻成 ``group_openid``，再把配置
           里的每个键（真实群号 / 虚拟号 / openid 原文）也翻成
           ``group_openid`` 比对 —— 与 ``echo.py`` / ``bound_notice.py``
           同一套「统一翻 openid 再比对」的修法。
        """
        if not custom_group_name:
            return source
        key = str(source_id)
        if key in custom_group_name:
            return custom_group_name[key]

        # 官方模式：虚拟号 ↔ 真实群号的对应关系在 id_mapper 里
        qq_connector = self.connector_manager.get_connector(qq_source)
        mapper = getattr(qq_connector, "id_mapper", None) if qq_connector else None
        if mapper is None:
            return source
        try:
            target_openid = str(mapper.to_official_group(key))
        except Exception:
            return source
        for cfg_key, name in custom_group_name.items():
            try:
                cfg_openid = str(mapper.to_official_group(str(cfg_key)))
            except Exception:
                continue
            if cfg_openid and cfg_openid == target_openid:
                return name
        return source

    async def connect(self) -> None:
        """Establish the connection (no-op since MCDR manages the lifecycle)."""
        self.logger.info(f"{self.log_prefix} 就绪 ~")
        # native_chat 探测：延迟到服务器运行后再测（见 _check_native_chat_available）
        self._native_chat_checked = False
        self._native_chat_available = False

    def _check_native_chat_available(self) -> bool:
        """探测 GuguChat mod 是否在位（缓存一次结果）。

        MCDR 的 execute_command 异步执行不抛异常，命令不存在时只打日志。
        所以发送前主动发送一条探测命令，从服务器输出确认（有 mod 会回
        `Unknown command` 缺失/正常注册两种输出）。这里用简单策略：
        配置开启 + 未确认失败 → 尝试注入；连续 N 次注入后无任何
        GuguChat 日志输出则判定不可用（保守做法：信任配置，只在
        execute_command 抛异常时回退 say —— mod 在但服务器没跑完启动
        时命令会 Unknown，MCDR 日志可见，用户可自查）。
        """
        if not self._native_chat_checked:
            self._native_chat_checked = True
            self._native_chat_available = bool(
                self.config.get_keys(
                    ["connector", "minecraft", "native_chat", "enable"], False
                )
            )
            if self._native_chat_available:
                self.logger.info(
                    f"{self.log_prefix} native_chat 已启用（请确保 mods/ 内有 GuguChat mod）"
                )
        return self._native_chat_available

    def _inject_system_chat(
        self,
        display_name: str,
        plain: str,
        show_group_prefix: bool = True,
        group_name: str = "",
        prefix_color: str = "",
    ) -> bool:
        """用 /tellraw 把消息送进系统聊天通道，成功返回 True。

        为什么用 tellraw 而不是 mod 注入的 PlayerChat 包：
          - tellraw 由服务端生成 ClientboundSystemChatPacket —— 系统消息
            通道不做聊天签名验证，未签名/offline 服务器不会再出现
            "聊天验证错误"
          - 渲染的是纯文本本身（所见即所得），不经过聊天头 /
            玩家档案解析，因此不依赖 UUID、头像等
          - 无需安装 mod，原版服务器即可用

        渲染结果（[QQ转发]名字: 永远存在）：
            [群名] [QQ转发]名字:消息内容
            show_group_prefix 只控制开头的 [群名]。

        注意：名字与正文之间、正文内部不做任何二次加工 ——
        tellraw 的 text 是字面量，所见即所得。
        """
        import json

        try:
            payload = []

            # 可选的群名前缀：[群名] （与 [QQ转发] 无关，各自独立）
            if show_group_prefix and group_name:
                payload.append({"text": f"[{group_name}] "})

            # [QQ转发]名字: 是消息的固定开头，不受任何开关影响
            head = f"[QQ转发]{display_name}:"
            if prefix_color:
                payload.append({"text": head, "color": prefix_color})
            else:
                payload.append({"text": head})

            payload.append({"text": plain})

            # json.dumps + ensure_ascii=False：中文原样输出；控制字符
            # 由 dumps 自动转义为 \\uXXXX，不会破坏命令解析
            raw = json.dumps(payload, ensure_ascii=False)
            # 换行会在服务器命令行里截断命令，统一转义掉
            raw = raw.replace("\n", "\\n").replace("\r", "\\r")

            # execute() = 服务端 stdin（原版命令）；execute_command() 是
            # MCDR 自己的 !! 命令系统，走不到服务端
            self.server.execute(f"tellraw @a {raw}")
            return True
        except Exception as e:
            self.logger.warning(
                f"{self.log_prefix} 系统消息注入失败，回退 server.say: {e}"
            )
            return False

    async def disconnect(self) -> None:
        """Tear down the connection (no-op since MCDR manages the lifecycle)."""
        self.logger.info(f"{self.log_prefix} 已断开 ~")

    async def send_message(self, processed_info: ProcessedInfo) -> None:
        """Build and broadcast a message to the Minecraft server chat.

        Parameters
        ----------
        processed_info : ProcessedInfo
            The processed message to send.
        """
        if not self.enable:
            return

        if not self.server.is_server_running():
            return

        self.builder: McMessageBuilder

        message = processed_info.processed_message
        source = processed_info.source.origin
        source_id = processed_info.source_id
        sender = processed_info.sender
        sender_id = processed_info.sender_id
        receiver = getattr(processed_info, "receiver", None)

        use_chat_image = self.config.get_keys(
            ["connector", "minecraft", "chat_image"], False
        )
        use_image_previewer = self.config.get_keys(
            ["connector", "minecraft", "image_previewer"], False
        )

        qq_source = self.config.get_keys(["connector", "QQ", "source_name"], "QQ")

        # When the bridge has show_in_game disabled, suppress cross-server MC chat:
        # drop if the last hop is an unregistered connector (direct remote-MC message),
        # or if the origin is another MC server (relayed through a known Bridge hop).
        bridge_source = self.config.get_keys(
            ["connector", "minecraft_bridge", "source_name"], "Bridge"
        )
        bridge = self.connector_manager.get_connector(bridge_source)
        if bridge is not None and not getattr(bridge, "show_in_game", True):
            cur = processed_info.source.current
            if (
                cur and self.connector_manager.get_connector(cur) is None
            ) or self._is_remote_mc_origin(processed_info.source.origin, qq_source):
                return

        try:
            try:
                game_version = self.server.get_server_information().version or ""
                game_version = game_version.lower() if game_version else ""
            except Exception:
                game_version = ""
            is_low_version = self.builder.is_low_game_version(game_version)

            bound_system = self.connector_manager.system_manager.get_system("bound")
            player_manager = getattr(bound_system, "player_manager", None)

            # Retrieve the bot's QQ ID so @-mentions targeting the bot can be filtered
            bot_id = None
            if qq_connector := self.connector_manager.get_connector(qq_source):
                bot_id = getattr(getattr(qq_connector, "bot", None), "self_id", None)

            rtext_content = self.builder.array_to_rtext(
                message,
                low_game_version=is_low_version,
                chat_image=use_chat_image,
                image_previewer=use_image_previewer,
                player_manager=player_manager,
                bot_id=bot_id,
            )

            if player_manager:
                sender_player = player_manager.get_player(str(sender_id))
                if sender_player:
                    # Prefer the first Java name, then Bedrock name, then display name
                    sender = (
                        sender_player.java_name[0]
                        if sender_player.java_name
                        else (
                            sender_player.bedrock_name[0]
                            if sender_player.bedrock_name
                            else sender_player.name
                        )
                    ) or sender

                if receiver:
                    receiver_player = player_manager.get_player(str(receiver))
                    if receiver_player:
                        receiver = (
                            receiver_player.java_name[0]
                            if receiver_player.java_name
                            else (
                                receiver_player.bedrock_name[0]
                                if receiver_player.bedrock_name
                                else receiver_player.name
                            )
                        ) or receiver

            custom_group_name = self.config.get_keys(
                ["connector", "QQ", "permissions", "custom_group_name"], {}
            ) or {}
            source = self._map_custom_group_name(
                custom_group_name, source_id, source, qq_source
            )

            # ----- native_chat 模式：系统消息通道注入 -----
            # 注入格式（整行纯文本，所见即所得）：
            #     [QQ转发]名字:我是玩家说的话
            #   - 名字 = 绑定的游戏名（有绑定）或 QQ 侧昵称（无绑定）
            #   - 走 ClientboundSystemChatPacket（tellraw 同款系统消息通道）：
            #     * 无聊天头、无玩家档案查询 —— 不依赖 UUID/头像
            #     * 不做聊天签名验证 —— 彻底规避 "聊天验证错误"
            if self._check_native_chat_available():
                show_group_prefix = self.config.get_keys(
                    ["connector", "minecraft", "native_chat", "show_group_prefix"], True
                )
                prefix_color = self.config.get_keys(
                    ["connector", "minecraft", "native_chat", "prefix_color"], ""
                )
                plain = segments_to_plain_text(message, image_as="cicode")
                plain = plain.replace("\n", " ").replace("\r", " ").strip()
                if not plain:
                    plain = " "

                # sender：优先绑定游戏名（player_manager 已解析），
                # 未绑定的 QQ 用户显示原始昵称
                display_name = (sender or "QQ用户").strip()
                # **必须剥 MC 颜色码**：``§`` 是**可打印字符**，
                # isprintable() 拦不住它。用户把 QQ 群昵称改成
                # ``§c§l管理员`` 就能在 MC 里把这行显示成红色粗体的
                # 「[QQ转发]管理员:」→ 伪造格式、冒充管理发言。
                # 正文走 segments_to_plain_text 时已经脱敏过，昵称这里
                # 是唯一漏网的位置（另一条发送路径也做了脱敏）。
                display_name = strip_minecraft_color_codes(display_name)
                # 系统消息不走 brigadier，无需替换空格；仅剔除控制字符
                display_name = "".join(
                    ch for ch in display_name if ch.isprintable()
                ) or "QQ用户"

                # source 已是 custom_group_name 映射后的群名（见上方）
                if self._inject_system_chat(
                    display_name,
                    plain,
                    show_group_prefix=show_group_prefix,
                    group_name=str(source) if source else "",
                    prefix_color=prefix_color,
                ):
                    return
            # ----- native_chat 结束（失败则继续走下方 say 逻辑） -----

            main_content = self.builder.build(
                rtext_content,
                group_name=source,
                group_id=source_id,
                sender=sender,
                sender_id=sender_id,
                receiver=receiver,
            )

            self.server.say(main_content)

        except Exception as e:
            error_msg = str(e) + "\n" + traceback.format_exc()
            self.logger.error(f"{self.log_prefix} 发送消息失败: {error_msg}")
            raise

    async def on_message(self, raw: Any) -> None:
        """Handle an incoming Minecraft chat message.

        Parameters
        ----------
        raw : Info
            The MCDR ``Info`` object for the received message.
        """
        try:
            if not self.enable:
                return

            if not raw.is_player:
                return

            if self.parser is not None:
                await self.parser.process_message(raw, server=self.server)

        except Exception as e:
            self.logger.error(f"{self.log_prefix} 处理消息失败: {e}")
            raise

    async def _is_admin(self, sender_id) -> bool:
        """Return whether *sender_id* is an admin."""
        bound_system = self.connector_manager.system_manager.get_system("bound")

        if not bound_system:
            return False

        player_manager = bound_system.player_manager
        return await player_manager.is_admin(sender_id)
