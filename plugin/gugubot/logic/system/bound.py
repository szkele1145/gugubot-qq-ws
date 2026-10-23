# -*- coding: utf-8 -*-
import re
from typing import List, Optional, Union

from mcdreforged.api.types import PluginServerInterface

from gugubot.builder import MessageBuilder
from gugubot.config import BotConfig
from gugubot.logic.system.basic_system import BasicSystem
from gugubot.logic.system.whitelist import WhitelistSystem
from gugubot.utils.player_manager import Player, PlayerManager
from gugubot.utils.types import BroadcastInfo


class BoundSystem(BasicSystem):
    """绑定系统，用于管理QQ账号与游戏玩家的绑定关系。

    提供绑定、解绑、查询绑定信息等功能。
    """

    def __init__(
        self, server: PluginServerInterface, config: Optional[BotConfig] = None
    ) -> None:
        """初始化绑定系统。"""
        super().__init__("bound", enable=False, config=config)
        self.server = server
        self.player_manager = PlayerManager.get_instance(server, self)
        self.whitelist: Optional[WhitelistSystem] = None

    def initialize(self) -> None:
        """初始化系统，加载配置等"""
        self.player_manager.load()
        self.logger.debug(
            f"已加载 {len(self.player_manager.get_all_players())} 个玩家绑定信息"
        )

    def set_whitelist_system(self, whitelist: WhitelistSystem) -> None:
        """设置白名单系统引用"""
        self.whitelist = whitelist

    def _qq_platform_keys(self) -> List[str]:
        """QQ 账号在 PlayerManager 里可能使用的平台键（按优先级）。

        绑定时写入的键是 ``broadcast_info.source.origin``——也就是用户
        自定义的 ``connector.QQ.source_name``（默认 "QQ"）。历史数据里
        也可能存在默认键 "QQ"。

        早期实现多处硬编码 ``"QQ"``：一旦用户把 source_name 改成别的
        名字（如 "我的QQ"），绑定会写进 ``accounts["我的QQ"]``，而解绑 /
        查询仍去动 ``accounts["QQ"]`` → **解绑无效、查询查不到**。
        因此这里返回全部候选键（当前键优先 + 兼容历史 "QQ"），由调用方
        遍历处理。
        """
        current = self.config.get_keys(
            ["connector", "QQ", "source_name"], "QQ"
        ) or "QQ"
        keys = [str(current)]
        if "QQ" not in keys:
            keys.append("QQ")
        return keys

    def _qq_source_name(self) -> str:
        """QQ 连接器的**注册名**（= ``connector.QQ.source_name``）。

        与 :meth:`_qq_platform_keys` 的区别：那个返回的是「绑定数据里可能
        用到的平台键」（当前 + 历史 "QQ"）；这个返回的是**当前生效的连接器
        注册名**——``ConnectorManager.get_connector()`` 是按
        ``connector.source`` 匹配的，而 ``connector.source`` 就是
        ``source_name``（默认 "QQ"，可自定义）。

        早期实现多处写成 ``get_connector("QQ")``：用户改过 source_name 后
        取到 None → 后续 ``qq_connector.bot`` 抛 AttributeError 被外层
        except 吞掉 → **绑定其实已写盘，用户却看到"绑定失败"**；另一些
        地方取到 None 后静默 return → 功能（群名片 / 退群通知 / 成员归属
        检查 / 未绑定检查）静默失效。
        """
        return (
            self.config.get_keys(["connector", "QQ", "source_name"], "QQ") or "QQ"
        )

    def _get_player_qq_accounts(self, player) -> list:
        """取玩家在 QQ 平台上的全部绑定账号（含历史键）。

        平台键要遍历全部候选键（当前 source_name + 历史 "QQ"）——只查
        当前键的话，用户改名前的旧绑定查不到 → **绑定者自己无法解绑**
        （只提示"无绑定"，非管理员被卡住）。
        """
        accounts = []
        for pk in self._qq_platform_keys():
            accounts.extend(player.accounts.get(pk, []))
        return accounts

    def _can_unbind(self, player, sender_id: str, is_admin: bool) -> bool:
        """判断发起解绑的人是否有权限解绑该玩家。

        管理员一律可以；普通用户必须是**绑定者本人**——判定要看全部候选
        平台键（当前 source_name + 历史 "QQ"），只查当前键的话，用户
        改名前的旧绑定查不到 → **绑定者自己无法解绑**（只提示"无绑定"，
        非管理员被卡住）。
        """
        if is_admin:
            return True
        return str(sender_id) in [
            str(a) for a in self._get_player_qq_accounts(player)
        ]

    def _has_other_platform_accounts(self, player) -> bool:
        """判断玩家是否还绑定了 QQ 以外平台的账号。

        绑定键是用户自定义的 source_name（默认 "QQ"），硬编码 ``"QQ"``
        会在改名后把 QQ 绑定误判成"其他平台的绑定"→ 退群清理时残留
        **僵尸绑定**（玩家名留着、QQ 号没了）。
        """
        qq_keys = self._qq_platform_keys()
        for platform, accounts in player.accounts.items():
            if platform not in qq_keys and accounts:
                return True
        return False

    def _is_account_bound(self, player, source: str, account_id: str) -> bool:
        """判断玩家是否已在该平台绑定过指定账号。

        QQ 平台要遍历全部候选键（当前 source_name + 历史 "QQ"）——只查
        当前键的话，用户改名前绑过的账号会被判成"没绑过"→ 重复绑定
        （同一 QQ 号在两个键下各存一份，解绑时只能删掉一半）。
        """
        if source in self._qq_platform_keys():
            keys = self._qq_platform_keys()
        else:
            keys = [source]
        for pk in keys:
            if str(account_id) in [str(a) for a in player.accounts.get(pk, [])]:
                return True
        return False

    def _platform_account_count(self, player, source: str) -> int:
        """统计玩家在某平台上的绑定数量。

        QQ 平台要遍历全部候选键（当前 source_name + 历史 "QQ"）——只查
        当前键的话，改名前的旧绑定不计入上限 → 可借改名绕过绑定上限。
        其他平台直接查该平台键。
        """
        if source in self._qq_platform_keys():
            return sum(
                len(player.accounts.get(pk, []))
                for pk in self._qq_platform_keys()
            )
        return len(player.accounts.get(source, []))

    async def process_broadcast_info(self, broadcast_info: BroadcastInfo) -> bool:
        """处理接收到的命令。

        Parameters
        ----------
        broadcast_info: BroadcastInfo
            广播信息，包含消息内容
        """
        # 先检查是否是开启/关闭命令
        if await self.handle_enable_disable(broadcast_info):
            return True

        if not self.enable:
            return False

        # 处理退群事件
        if (
            broadcast_info.event_type == "notice"
            and broadcast_info.event_sub_type == "group_decrease"
        ):
            if broadcast_info.source.is_from(
                self.config.get_keys(["connector", "QQ", "source_name"], "QQ")
            ):
                return await self._handle_quit_member(broadcast_info)
            return False

        # 处理消息类型事件
        if broadcast_info.event_type != "message":
            return False

        message = broadcast_info.message

        if not message:
            return False

        # QQ 官方 @机器人 时消息段首位可能是 at 段，text 段不必然在 index 0；
        # 须拼接全部 type=="text" 段后再做前缀/命令解析（同 player_list.py 已修）。
        content = "".join(
            seg.get("data", {}).get("text", "")
            for seg in message
            if seg.get("type") == "text"
        ).strip()
        if not content:
            return False

        return await self._handle_msg(broadcast_info)

    async def _handle_msg(self, broadcast_info: BroadcastInfo) -> bool:
        """处理消息"""
        if self.is_command(broadcast_info):
            return await self._handle_command(broadcast_info)

        return False

    @staticmethod
    def _starts_with_command(text: str, cmd: str) -> bool:
        """判断 text 是否以 cmd 作为完整命令前缀开头。

        确保 cmd 后面紧跟空格或到达字符串末尾，
        避免 "绑定提醒" 被 "绑定" 错误匹配。
        """
        if not text.startswith(cmd):
            return False
        remaining = text[len(cmd) :]
        return not remaining or remaining[0] == " "

    async def _handle_command(self, broadcast_info: BroadcastInfo) -> bool:
        """处理绑定相关命令"""
        command = "".join(
            seg.get("data", {}).get("text", "")
            for seg in broadcast_info.message
            if seg.get("type") == "text"
        )
        command_prefix = self.config.get("GUGUBot", {}).get("command_prefix", "#")
        system_name = self.get_tr("name")

        command = command.replace(command_prefix, "", 1).strip()

        valid_commands = [
            system_name,
            self.get_tr("bind"),
            self.get_tr("unbind"),
            self.get_tr("search"),
        ]
        if not any(self._starts_with_command(command, i) for i in valid_commands):
            return False

        command = command.replace(system_name, "", 1).strip()

        # 空命令 → 显示绑定系统帮助。
        # 判定只看「拼接后的命令文本是否为空」，**不能**用
        # ``len(broadcast_info.message) == 1``：QQ 官方直连里 @机器人 时
        # 消息段是 [at 段, text 段]（长度 2），该条件在官方模式下恒为
        # False → 空命令会绕过这里、掉进 _handle_bind，最后回的是
        # bind_instruction（用法提示）——与 OneBot 单段模式回 user_help_msg
        # （系统帮助）**不一致**。这里统一：两种平台都回系统帮助。
        if command.strip() == "":
            return await self._handle_help(broadcast_info)
        elif command.startswith(self.get_tr("unbind")):
            return await self._handle_unbind(broadcast_info)
        elif command.startswith(self.get_tr("search")):
            return await self._handle_search(broadcast_info)
        elif command.startswith(self.get_tr("list")):
            return await self._handle_list(broadcast_info)
        elif broadcast_info.is_admin:
            # 管理员专属命令。命令字一律从语言包取（get_tr），不再硬编码
            # 中英双语列表——硬编码会让语言包里的同名条目变成死键，且新增
            # 语言时必须同步改 Python 源码。
            admin_subcommands = (
                ("whitelist_check", self._handle_check_whitelist),
                ("remove_unbound_whitelist", self._handle_remove_unbound_whitelist),
                ("extra_bound_check", self._handle_check_extra_bound),
                ("remove_extra_bound", self._handle_remove_extra_bound),
            )
            for key, handler in admin_subcommands:
                if command == self.get_tr(key):
                    return await handler(broadcast_info)

        return await self._handle_bind(broadcast_info)

    async def _handle_bind(self, broadcast_info: BroadcastInfo) -> bool:
        """处理绑定命令"""
        command = "".join(
            seg.get("data", {}).get("text", "")
            for seg in broadcast_info.message
            if seg.get("type") == "text"
        )
        command_prefix = self.config.get("GUGUBot", {}).get("command_prefix", "#")
        system_name = self.get_tr("name")
        bind_cmd = self.get_tr("bind")

        for i in [command_prefix, system_name]:
            command = command.replace(i, "", 1).strip()

        # 空内容 → 回绑定用法提示（直接调用本方法时的兜底；正常命令流
        # 里空命令已在 _handle_command 被拦下并回系统帮助）。
        # 判定同样不看消息段数（官方 @机器人 时消息段为 2）。
        if not command:
            await self.reply(
                broadcast_info,
                [
                    MessageBuilder.text(
                        self.get_tr(
                            "bind_instruction",
                            command_prefix=command_prefix,
                            name=system_name,
                            bind=bind_cmd,
                        )
                    )
                ],
            )
            return True

        # 解析消息段
        source = broadcast_info.source.origin  # 获取原始来源作为平台标识
        target_id = broadcast_info.sender_id  # 默认绑定到发送者
        player_name = ""
        is_bedrock = False

        # 检查是否有@消息段，如果有就更新要绑定的平台账户
        for message_segment in broadcast_info.message:
            if message_segment.get("type") == "at":
                target_id = str(
                    message_segment.get("data", {}).get(
                        "qq", broadcast_info.sender_id
                    )
                )
                break

        # 使用最后一个消息段来处理玩家名和基岩版标识
        last_text_message = [
            message
            for message in broadcast_info.message
            if message.get("type") == "text"
        ][-1]
        last_text = last_text_message.get("data", {}).get("text", "")
        for i in [command_prefix, system_name]:
            last_text = last_text.replace(i, "", 1).strip()

        bedrock_modifiers = {"bedrock", "基岩", "be"}
        offline_modifiers = {"offline", "离线", "off"}
        online_modifiers = {"online", "在线", "on"}
        type_modifiers = bedrock_modifiers | offline_modifiers | online_modifiers
        is_bedrock = is_offline = is_online = False
        parts = last_text.rsplit(maxsplit=1)
        if len(parts) > 1 and parts[-1].lower() in type_modifiers:
            player_name = parts[0].strip()
            modifier = parts[-1].lower()
            is_bedrock = modifier in bedrock_modifiers
            is_offline = modifier in offline_modifiers
            is_online = modifier in online_modifiers
        elif last_text.strip():
            player_name = last_text.strip()

        if not player_name:
            await self.reply(
                broadcast_info,
                [
                    MessageBuilder.text(
                        self.get_tr(
                            "bind_instruction",
                            command_prefix=command_prefix,
                            name=system_name,
                            bind=bind_cmd,
                        )
                    )
                ],
            )
            return True

        # 验证玩家名是否符合正则表达式
        # 基岩版优先使用专属正则，若未配置则回退到通用正则
        bound_config = self.config.get("system", {}).get("bound", {})
        if is_bedrock:
            player_name_pattern = bound_config.get(
                "bedrock_player_name_pattern", ""
            ) or bound_config.get("player_name_pattern", "")
        else:
            player_name_pattern = bound_config.get("player_name_pattern", "")
        if player_name_pattern:
            try:
                if not re.match(player_name_pattern, player_name):
                    await self.reply(
                        broadcast_info,
                        [
                            MessageBuilder.text(
                                self.get_tr(
                                    "bind_invalid_name",
                                    player_name=player_name,
                                    pattern=player_name_pattern,
                                )
                            )
                        ],
                    )
                    return True
            except re.error as e:
                self.logger.error(f"正则表达式 '{player_name_pattern}' 格式错误: {e}")
                await self.reply(
                    broadcast_info,
                    [MessageBuilder.text(self.get_tr("bind_pattern_error"))],
                )
                return True

        # 检查是否达到绑定上限
        player = self.player_manager.get_player(
            target_id or player_name, platform=source
        )

        exceeded_platform_bound = exceeded_bedrock_bound = exceeded_java_bound = False
        if player:
            # 平台绑定数：QQ 平台要遍历全部候选键（当前 source_name +
            # 历史 "QQ"），只查当前键的话改名前的旧绑定不计入上限 →
            # 可借改名绕过绑定上限。
            exceeded_platform_bound = self._platform_account_count(
                player, source
            ) >= self.config.get("system", {}).get("bound", {}).get(
                "max_platform_bound", 1
            )
            exceeded_bedrock_bound = len(player.bedrock_name) >= self.config.get(
                "system", {}
            ).get("bound", {}).get("max_bedrock_bound", 1)
            exceeded_java_bound = len(player.java_name) >= self.config.get(
                "system", {}
            ).get("bound", {}).get("max_java_bound", 1)

        # 到达所有绑定上限
        if (
            player
            and exceeded_platform_bound
            and exceeded_bedrock_bound
            and exceeded_java_bound
        ):
            await self.reply(
                broadcast_info, [MessageBuilder.text(self.get_tr("max_bound_reached"))]
            )
            return True

        elif (not is_bedrock and exceeded_platform_bound and exceeded_java_bound) or (
            is_bedrock and exceeded_platform_bound and exceeded_bedrock_bound
        ):
            await self.reply(
                broadcast_info, [MessageBuilder.text(self.get_tr("max_bound_reached"))]
            )
            return True

        # 直接执行绑定
        bound_result = await self._bind_player(
            broadcast_info,
            player_name,
            source,
            is_bedrock,
            is_offline,
            is_online,
            target_id,
        )
        if bound_result:
            await self.reply(
                broadcast_info, [MessageBuilder.text(self.get_tr("bind_success"))]
            )
        else:
            await self.reply(
                broadcast_info, [MessageBuilder.text(self.get_tr("bind_existed"))]
            )

        return True

    async def _bind_player(
        self,
        broadcast_info: BroadcastInfo,
        player_name: str,
        platform: str,
        is_bedrock: bool = False,
        is_offline: bool = False,
        is_online: bool = False,
        target_id: str = None,
    ) -> bool:
        """执行玩家绑定"""
        try:
            # 如果用户未指定任何选项，将选择权交给whitelist

            # 检查玩家名是否已被其他用户绑定
            if self.player_manager.is_name_bound_by_other_user(
                player_name, target_id, platform
            ):
                return False

            def _bound_whitelist(
                player_name: str, is_offline: bool, is_online: bool, is_bedrock: bool
            ):
                if (
                    self.config.get("system", {})
                    .get("bound", {})
                    .get("whitelist_add_with_bound", False)
                    and self.whitelist
                ):
                    return self.whitelist.add_player(  # 返回白名单添加状态
                        player_name,
                        force_offline=is_offline,
                        force_online=is_online,
                        force_bedrock=is_bedrock,
                    )
                return True  # 如未开启绑定时添加白名单，返回True

            async def _set_group_card_if_qq(
                player_name: str,
                platform: str,
                broadcast_info: BroadcastInfo,
                target_id: str,
            ):
                """如果是QQ来源，则设置群名片"""
                # platform 是 broadcast_info.source.origin（即用户自定义的
                # connector.QQ.source_name），不能硬编码 "QQ"——用户改名后
                # 群名片功能会静默失效。
                if platform in self._qq_platform_keys() and self.config.get_keys(
                    ["connector", "QQ", "others", "change_group_card"], True
                ):
                    qq_connector = self.system_manager.connector_manager.get_connector(
                        self._qq_source_name()
                    )
                    # 官方直连连接器带 id_mapper（虚拟号→openid）；OneBot 链路
                    # 没有该属性，此处必须 getattr 兜底，否则 OneBot 模式下
                    # 每次绑定成功后会抛 AttributeError，被外层 except 吞掉并
                    # 返回 False —— 绑定其实已写盘，用户却看到"绑定失败"。
                    mapper = getattr(qq_connector, "id_mapper", None)
                    user_id = (
                        mapper.to_official_user(target_id) if mapper else target_id
                    )
                    await qq_connector.bot.set_group_card(
                        group_id=broadcast_info.source_id,
                        user_id=user_id,
                        card=player_name,
                    )

            # 检查是否已存在该玩家
            existing_player = self.player_manager.get_player(
                target_id, platform=platform
            )
            if existing_player:
                # 将列表中的元素转换为字符串进行比较
                bedrock_names_str = [str(name) for name in existing_player.bedrock_name]
                java_names_str = [str(name) for name in existing_player.java_name]
                if (is_bedrock and player_name not in bedrock_names_str) or (
                    not is_bedrock and player_name not in java_names_str
                ):
                    existing_player.add_name(player_name, is_bedrock)
                    self.player_manager.save()
                    success = _bound_whitelist(
                        player_name, is_offline, is_online, is_bedrock
                    )
                    await _set_group_card_if_qq(
                        player_name, platform, broadcast_info, target_id
                    )
                    return success

                elif not self._is_account_bound(existing_player, platform, target_id):
                    existing_player.add_account(platform, target_id)
                    self.player_manager.save()
                    success = _bound_whitelist(
                        player_name, is_offline, is_online, is_bedrock
                    )
                    await _set_group_card_if_qq(
                        player_name, platform, broadcast_info, target_id
                    )
                    return success

                return False

            # 添加新玩家绑定
            self.player_manager.add_player_account(
                player_name,
                platform=platform,
                account_id=target_id,
                is_bedrock=is_bedrock,
            )
            self.player_manager.save()
            success = _bound_whitelist(player_name, is_offline, is_online, is_bedrock)
            await _set_group_card_if_qq(
                player_name, platform, broadcast_info, target_id
            )

            return success
        except Exception as e:
            self.logger.error(f"绑定玩家失败: {e}")
            return False

    async def _handle_unbind(self, broadcast_info: BroadcastInfo) -> bool:
        """处理解绑命令"""
        command = "".join(
            seg.get("data", {}).get("text", "")
            for seg in broadcast_info.message
            if seg.get("type") == "text"
        )
        command_prefix = self.config.get("GUGUBot", {}).get("command_prefix", "#")
        system_name = self.get_tr("name")
        unbind_command = self.get_tr("unbind")

        for i in [command_prefix, system_name, unbind_command]:
            command = command.replace(i, "", 1).strip()

        if not command:
            # 解绑当前用户的所有绑定
            return await self._unbind_current_user(broadcast_info)
        else:
            # 检查是否是基岩版独立命令
            if command.lower() in ["bedrock", "基岩", "be"]:
                return await self._unbind_bedrock_only(broadcast_info)

            # 解析玩家名和基岩版参数，从右侧拆分以支持带空格的玩家名
            bedrock_modifiers = {"bedrock", "基岩", "be"}
            parts = command.rsplit(maxsplit=1)
            if len(parts) > 1 and parts[-1].lower() in bedrock_modifiers:
                player_name = parts[0].strip()
                is_bedrock = True
            else:
                player_name = command.strip()
                is_bedrock = False

            # 解绑指定玩家
            return await self._unbind_specific_player(
                broadcast_info, player_name, is_bedrock
            )

    async def _unbind_current_user(self, broadcast_info: BroadcastInfo) -> bool:
        """解绑当前用户的Java版玩家名（默认行为）"""
        player = self.player_manager.get_player(
            str(broadcast_info.sender_id), platform=broadcast_info.source.origin
        )
        if not player:
            await self.reply(
                broadcast_info, [MessageBuilder.text(self.get_tr("no_bindings"))]
            )
            return True

        # 检查是否有Java版玩家名
        if not player.java_name:
            await self.reply(
                broadcast_info, [MessageBuilder.text(self.get_tr("no_bindings"))]
            )
            return True

        # 从白名单中删除玩家名（转换为字符串）
        self._remove_from_whitelist([str(name) for name in player.java_name])

        # 清空Java版玩家名列表
        player.java_name.clear()
        self.player_manager.save()
        await self._clean_bound(player, self.player_manager)
        await self.reply(
            broadcast_info, [MessageBuilder.text(self.get_tr("unbind_success"))]
        )
        return True

    async def _unbind_bedrock_only(self, broadcast_info: BroadcastInfo) -> bool:
        """解绑当前用户的所有基岩版玩家名"""
        player = self.player_manager.get_player(
            broadcast_info.sender_id, platform=broadcast_info.source.origin
        )
        if not player:
            await self.reply(
                broadcast_info, [MessageBuilder.text(self.get_tr("no_bindings"))]
            )
            return True

        # 检查是否有基岩版玩家名
        if not player.bedrock_name:
            await self.reply(
                broadcast_info, [MessageBuilder.text(self.get_tr("no_bindings"))]
            )
            return True

        # 从白名单中删除玩家名（转换为字符串）
        self._remove_from_whitelist([str(name) for name in player.bedrock_name])

        # 清空基岩版玩家名列表
        player.bedrock_name.clear()
        self.player_manager.save()
        await self._clean_bound(player, self.player_manager)
        await self.reply(
            broadcast_info, [MessageBuilder.text(self.get_tr("unbind_success"))]
        )
        return True

    async def _unbind_specific_player(
        self, broadcast_info: BroadcastInfo, player_name: str, is_bedrock: bool = False
    ) -> bool:
        """解绑指定玩家"""
        player = self.player_manager.get_player(player_name)
        if not player:
            await self.reply(
                broadcast_info, [MessageBuilder.text(self.get_tr("player_not_found"))]
            )
            return True

        is_admin = broadcast_info.is_admin
        # 检查是否是绑定玩家
        if not self._can_unbind(
            player, broadcast_info.sender_id, is_admin
        ):
            await self.reply(
                broadcast_info, [MessageBuilder.text(self.get_tr("no_bindings"))]
            )
            return True

        # 根据基岩版参数选择性解绑
        if is_bedrock:
            # 只解绑基岩版玩家名（转换为字符串进行比较）
            bedrock_names_str = [str(name) for name in player.bedrock_name]
            if player_name in bedrock_names_str:
                # 从白名单中删除玩家名
                self._remove_from_whitelist(player_name)

                player.bedrock_name.remove(player_name)
                await self._clean_bound(player, self.player_manager)
                await self.reply(
                    broadcast_info, [MessageBuilder.text(self.get_tr("unbind_success"))]
                )
                return True
            else:
                await self.reply(
                    broadcast_info,
                    [MessageBuilder.text(self.get_tr("player_not_found"))],
                )
                return True
        else:
            # 只解绑Java版玩家名（转换为字符串进行比较）
            java_names_str = [str(name) for name in player.java_name]
            if player_name in java_names_str:
                # 从白名单中删除玩家名
                self._remove_from_whitelist(player_name)

                player.java_name.remove(player_name)
                await self._clean_bound(player, self.player_manager)
                await self.reply(
                    broadcast_info, [MessageBuilder.text(self.get_tr("unbind_success"))]
                )
                return True
            else:
                await self.reply(
                    broadcast_info,
                    [MessageBuilder.text(self.get_tr("player_not_found"))],
                )
                return True

    def _remove_from_whitelist(self, player_names: Union[str, List[str]]) -> None:
        """从白名单中删除玩家名的辅助函数

        Parameters
        ----------
        player_names : Union[str, List[str]]
            要删除的玩家名，可以是单个字符串或字符串列表
        """
        if (
            not self.config.get("system", {})
            .get("bound", {})
            .get("whitelist_remove_with_leave", False)
            or not self.whitelist
        ):
            return

        # 统一转换为列表处理
        if isinstance(player_names, str):
            player_names = [player_names]

        for name in player_names:
            self.whitelist.remove_player(name)

    async def _get_member_in_all_groups(self) -> Optional[set]:
        """获取所有群（包括普通群和管理群）中的成员ID集合

        Returns
        -------
        Optional[set]
            所有群成员的QQ ID集合；**None 表示"无法获取成员列表"**
            （如 QQ 官方直连模式不提供该接口），调用方必须据此跳过
            依赖成员列表的操作——把「无法获取」当成「没有成员」会把
            所有绑定误判为多余，移除命令将清空全部绑定数据。
        """
        member_id_set = set()

        # 获取配置中的群ID列表
        group_ids = self.config.get_keys(
            ["connector", "QQ", "permissions", "group_ids"], []
        )
        admin_group_ids = self.config.get_keys(
            ["connector", "QQ", "permissions", "admin_group_ids"], []
        )

        # 合并所有群ID
        all_group_ids = {gid for gid in (group_ids + admin_group_ids) if gid}

        if not all_group_ids:
            # 宽松模式（未配置监听群，机器人收到的所有群都转发）下根本
            # 无从得知有哪些群、成员是谁——这与「群里没有成员」完全不同。
            # 当成空集会让「多余绑定检查」把全部绑定报为多余。
            self.logger.info(
                "[绑定系统] 未配置监听群（宽松模式），无法获取成员列表，"
                "成员归属类检查不可用"
            )
            return None

        # 获取QQ连接器
        try:
            # 连接器注册名 = source_name（用户可自定义），不能硬编码 "QQ"
            # ——改名后取到 None → 成员归属类检查永远不可用
            qq_connector = self.system_manager.connector_manager.get_connector(
                self._qq_source_name()
            )
            if not qq_connector or not qq_connector.bot:
                # 连接器未就绪：查询结果不可信，不能当空集
                self.logger.info(
                    "[绑定系统] QQ 连接器未就绪，无法获取成员列表，"
                    "成员归属类检查不可用"
                )
                return None

            # 遍历所有群，获取成员列表
            queried = 0  # 至少拿到一次真实列表的群数
            for group_id in all_group_ids:
                if not group_id:
                    continue
                try:
                    member_list_result = await qq_connector.bot.get_group_member_list(
                        group_id=group_id
                    )
                    if not member_list_result:
                        continue
                    # 官方直连等平台不支持成员列表 → data 为 None
                    if member_list_result.get("unsupported"):
                        return None
                    if member_list_result.get("status") == "ok":
                        members = member_list_result.get("data", [])
                        # data 为 None 同样表示无法获取（不是"没有成员"）
                        if members is None:
                            return None
                        queried += 1
                        for member in members:
                            user_id = str(member.get("user_id", ""))
                            if user_id:
                                member_id_set.add(user_id)
                except Exception as e:
                    self.logger.error(f"获取群 {group_id} 成员列表失败: {e}")
                    continue

            # 所有群都查询失败（如 OneBot WS 断连）：结果集合不可信。
            # 若把空集交给调用方，全部绑定都会被误判为「不在群里」。
            if queried == 0:
                self.logger.warning(
                    "[绑定系统] 所有群的成员列表均查询失败，"
                    "无法判断成员归属，本次检查不可用"
                )
                return None
        except Exception as e:
            self.logger.error(f"获取群成员列表失败: {e}")
            return None

        return member_id_set

    async def _send_quit_notification_to_admin_groups(
        self, user_id: str, group_id: str, player: Optional[Player] = None
    ) -> None:
        """向管理群发送退群通知

        Parameters
        ----------
        user_id : str
            退群用户的QQ号
        group_id : str
            退群的群号
        player : Player, optional
            退群用户的绑定信息，如果为None则不包含绑定信息
        """
        try:
            # 获取管理群ID列表
            admin_group_ids = self.config.get_keys(
                ["connector", "QQ", "permissions", "admin_group_ids"], []
            )
            if not admin_group_ids:
                return

            # 构建通知消息
            notification_parts = [f"群 {group_id} 有成员退群"]
            notification_parts.append(f"QQ号: {user_id}")

            if player:
                # 获取所有绑定的玩家名，并区分 Java 与基岩
                java_names = [str(name) for name in player.java_name]
                bedrock_names = [str(name) for name in player.bedrock_name]
                if java_names or bedrock_names:
                    if java_names:
                        java_str = "Java: " + ", ".join(java_names)
                        notification_parts.append(f"绑定玩家({java_str})")
                    if bedrock_names:
                        bedrock_str = "基岩: " + ", ".join(bedrock_names)
                        notification_parts.append(f"绑定玩家({bedrock_str})")
                else:
                    notification_parts.append("未绑定玩家")
            else:
                notification_parts.append("未绑定玩家")

            notification_msg = "\n".join(notification_parts)

            # 获取QQ连接器（注册名 = source_name，不能硬编码 "QQ"——改名后
            # 取到 None → 退群通知静默不发送）
            qq_connector = self.system_manager.connector_manager.get_connector(
                self._qq_source_name()
            )
            if not qq_connector or not qq_connector.bot:
                return

            # 向所有管理群发送通知
            for admin_group_id in admin_group_ids:
                if not admin_group_id:
                    continue
                try:
                    await qq_connector.bot.send_group_msg(
                        group_id=admin_group_id,
                        message=[MessageBuilder.text(notification_msg)],
                    )
                except Exception as e:
                    self.logger.error(
                        f"发送退群通知到管理群 {admin_group_id} 失败: {e}"
                    )
        except Exception as e:
            self.logger.error(f"发送退群通知失败: {e}")

    async def _handle_quit_member(self, broadcast_info: BroadcastInfo) -> bool:
        """处理退群事件

        Parameters
        ----------
        broadcast_info : BroadcastInfo
            退群事件信息

        Returns
        -------
        bool
            是否处理了该事件
        """
        try:
            # 从 raw 中获取退群用户信息
            raw_data = broadcast_info.raw
            if not isinstance(raw_data, dict):
                return False

            user_id = str(raw_data.get("user_id", ""))
            group_id = str(raw_data.get("group_id", ""))

            if not user_id or not group_id:
                return False

            # 判断退群的群是否是普通群（不是管理群）
            group_ids = self.config.get_keys(
                ["connector", "QQ", "permissions", "group_ids"], []
            )
            admin_group_ids = self.config.get_keys(
                ["connector", "QQ", "permissions", "admin_group_ids"], []
            )

            # 官方直连模式下，raw["group_id"] 是**虚拟群号**（12 位
            # 200000xxx），而配置里填的是**真实群号** → 直接字符串比较
            # 永不相等 → is_normal_group 恒 False → 退群通知永远不发。
            # 与 echo.py / bound_notice.py 同一修法：有 id_mapper 时把
            # 双方统一翻成 group_openid 再比对；OneBot 链路没有
            # id_mapper，group_id 本身就是真实群号，直接按字符串比。
            mapper = None
            qq_connector = self.system_manager.connector_manager.get_connector(
                self._qq_source_name()
            )
            if qq_connector:
                mapper = getattr(qq_connector, "id_mapper", None)

            def _as_openid(gid: str) -> str:
                if mapper is None:
                    return str(gid)
                try:
                    return str(mapper.to_official_group(str(gid)))
                except Exception:
                    return str(gid)

            event_group_key = _as_openid(group_id)
            is_normal_group = event_group_key in {_as_openid(g) for g in group_ids if g}
            is_admin_group = event_group_key in {
                _as_openid(g) for g in admin_group_ids if g
            }

            # 获取该用户的绑定信息（兼容自定义 source_name：可能在
            # accounts["我的QQ"]，也可能在历史键 accounts["QQ"]）
            player = None
            for pk in self._qq_platform_keys():
                player = self.player_manager.get_player(user_id, platform=pk)
                if player:
                    break

            # 如果退群的是普通群，发送通知到管理群
            if is_normal_group and not is_admin_group:
                await self._send_quit_notification_to_admin_groups(
                    user_id, group_id, player
                )

            # 检查该用户是否还在其他群中
            all_member_ids = await self._get_member_in_all_groups()

            # 成员列表不可用（平台不支持 / 未配置监听群 / 查询全部失败）→
            # 无法判断，跳过清理：否则空集会让条件恒成立，把所有绑定都清掉
            if all_member_ids is None:
                self.logger.info(
                    f"[绑定系统] 无法获取群成员列表，跳过用户 {user_id} 的退群清理"
                )
                return True

            # 如果用户不在任何群中，执行清理
            if user_id not in all_member_ids and player:
                # 获取所有玩家名（转换为字符串）
                all_player_names = [str(name) for name in player.java_name] + [
                    str(name) for name in player.bedrock_name
                ]

                # 从白名单中移除玩家名
                if all_player_names:
                    self._remove_from_whitelist(all_player_names)

                # 检查该用户是否还有其他平台的账户绑定
                # 注意：绑定键是用户自定义的 source_name（默认 "QQ"），
                # 硬编码 "QQ" 会在改名后把 QQ 绑定误判成"其他平台的绑定"
                # → 残留僵尸绑定（玩家名留着、QQ 号没了）
                has_other_platform_accounts = self._has_other_platform_accounts(
                    player
                )

                # 从绑定数据中删除该用户的QQ账户绑定（遍历全部候选键：
                # 自定义 source_name 键 + 历史 "QQ" 键）
                for pk in self._qq_platform_keys():
                    if pk in player.accounts and user_id in player.accounts[pk]:
                        player.accounts[pk].remove(user_id)
                        # 如果QQ账户列表为空，删除该平台
                        if not player.accounts[pk]:
                            del player.accounts[pk]

                # 如果该用户没有其他平台的账户绑定，删除整个 Player（包括所有玩家名）
                # 参考 bound_system.py 的逻辑：退群时删除整个绑定
                if not has_other_platform_accounts:
                    self.player_manager.remove_player(player.name)
                else:
                    # 如果还有其他平台的账户，只删除QQ账户绑定，保留玩家名和其他平台绑定
                    self.player_manager.save()

                self.logger.info(f"已清理退群用户 {user_id} 的绑定信息")
                return True

            return False
        except Exception as e:
            self.logger.error(f"处理退群事件失败: {e}")
            return False

    async def _clean_bound(self, player: Player, player_manager: PlayerManager) -> bool:
        """清理绑定"""
        if not player.java_name and not player.bedrock_name:
            player_manager.remove_player(player.name)

    async def _handle_list(self, broadcast_info: BroadcastInfo) -> bool:
        """处理显示绑定列表命令"""
        # 管理员查看所有人的绑定列表
        if broadcast_info.is_admin:
            return await self._handle_list_all(broadcast_info)

        player = self.player_manager.get_player(broadcast_info.sender_id)
        if not player:
            await self.reply(
                broadcast_info, [MessageBuilder.text(self.get_tr("no_bindings"))]
            )
            return True

        # 获取所有绑定的玩家名（排除 QQ 平台：兼容自定义 source_name 与
        # 历史 "QQ" 键——这里是要「跳过」QQ 平台，所以要排除全部候选键）
        qq_keys = {k.lower() for k in self._qq_platform_keys()}
        bound_players = []
        for platform, accounts in player.accounts.items():
            if str(platform).lower() in qq_keys:
                continue
            bound_players.extend(accounts)

        if not bound_players:
            await self.reply(
                broadcast_info, [MessageBuilder.text(self.get_tr("no_bindings"))]
            )
            return True

        player_list = "\n".join(
            f"{i + 1}. {name}" for i, name in enumerate(bound_players)
        )
        await self.reply(
            broadcast_info,
            [MessageBuilder.text(self.get_tr("bind_list", player_list=player_list))],
        )
        return True

    async def _handle_list_all(self, broadcast_info: BroadcastInfo) -> bool:
        """管理员查看所有玩家的绑定列表"""
        all_players = self.player_manager.get_all_players()
        if not all_players:
            await self.reply(
                broadcast_info, [MessageBuilder.text("当前没有任何绑定信息")]
            )
            return True

        reply_lines = [f"绑定列表 (共{len(all_players)}人):"]
        for i, player in enumerate(all_players, 1):
            # 收集玩家名
            java_names = [str(name) for name in player.java_name]
            bedrock_names = [str(name) for name in player.bedrock_name]

            # 收集平台账号
            account_parts = []
            for platform, accounts in player.accounts.items():
                account_ids = ", ".join(str(a) for a in accounts)
                account_parts.append(f"{platform}: {account_ids}")

            # 拼接每行信息
            line = f"{i}. "
            name_parts = []
            if java_names:
                name_parts.append("Java: " + ", ".join(java_names))
            if bedrock_names:
                name_parts.append("基岩: " + ", ".join(bedrock_names))
            line += " | ".join(name_parts) if name_parts else "无游戏名"

            if account_parts:
                line += f" [{' | '.join(account_parts)}]"

            reply_lines.append(line)

        await self.reply(broadcast_info, [MessageBuilder.text("\n".join(reply_lines))])
        return True

    async def _handle_help(self, broadcast_info: BroadcastInfo) -> bool:
        """绑定指令帮助"""
        command_prefix = self.config.get("GUGUBot", {}).get("command_prefix", "#")
        system_name = self.get_tr("name")
        bind_cmd = self.get_tr("bind")
        unbind_cmd = self.get_tr("unbind")
        list_cmd = self.get_tr("list")
        search_cmd = self.get_tr("search")

        # 根据用户权限选择不同的帮助信息
        if broadcast_info.is_admin:
            enable_cmd = self.get_tr("gugubot.enable", global_key=True)
            disable_cmd = self.get_tr("gugubot.disable", global_key=True)
            help_msg = self.get_tr(
                "help_msg",
                command_prefix=command_prefix,
                name=system_name,
                enable=enable_cmd,
                disable=disable_cmd,
                bind=bind_cmd,
                unbind=unbind_cmd,
                list=list_cmd,
                search=search_cmd,
            )
        else:
            help_msg = self.get_tr(
                "user_help_msg",
                command_prefix=command_prefix,
                name=system_name,
                bind=bind_cmd,
                unbind=unbind_cmd,
                list=list_cmd,
                search=search_cmd,
            )
        await self.reply(broadcast_info, [MessageBuilder.text(help_msg)])
        return True

    def get_player_by_qq_id(self, qq_id: str) -> Optional[Player]:
        """通过QQ ID获取玩家信息"""
        return self.player_manager.get_player(qq_id)

    async def _handle_search(self, broadcast_info: BroadcastInfo) -> bool:
        """处理绑定查询命令。

        - 无参数：查询发送者自己的绑定
        - 带参数（玩家名或 QQ/虚拟号）：仅管理员可查任意玩家，
          普通用户带参数会被拒绝（防止借查询枚举他人绑定）
        """
        command = "".join(
            seg.get("data", {}).get("text", "")
            for seg in broadcast_info.message
            if seg.get("type") == "text"
        )
        command_prefix = self.config.get("GUGUBot", {}).get("command_prefix", "#")
        system_name = self.get_tr("name")
        search_cmd = self.get_tr("search")

        for i in [command_prefix, system_name]:
            command = command.replace(i, "", 1).strip()
        # 去掉子命令词，剩下的就是查询目标
        # （用切片而非 replace：查询目标名里若含"查询"二字不会被误删）
        if command.startswith(search_cmd):
            command = command[len(search_cmd):].strip()

        # 无参数 → 查自己（与 #list 个人视角一致，但明确报"未绑定"）
        if not command:
            # 不加 platform 限定：与 _handle_list 保持一致，避免平台名
            # 大小写/别名差异导致查不到自己的绑定
            player = self.player_manager.get_player(broadcast_info.sender_id)
            if not player:
                await self.reply(
                    broadcast_info,
                    [MessageBuilder.text(self.get_tr("no_bindings"))],
                )
                return True
            await self.reply(
                broadcast_info,
                [
                    MessageBuilder.text(
                        self.get_tr(
                            "search_result", info=self._format_player_info(player)
                        )
                    )
                ],
            )
            return True

        # 带参数：仅管理员可查任意目标
        if not broadcast_info.is_admin:
            await self.reply(
                broadcast_info,
                [MessageBuilder.text(self.get_tr("search_no_permission"))],
            )
            return True

        player = self.player_manager.get_player(command)
        if not player:
            await self.reply(
                broadcast_info,
                [
                    MessageBuilder.text(
                        self.get_tr("no_bindings_for_player", player_name=command)
                    )
                ],
            )
            return True
        await self.reply(
            broadcast_info,
            [
                MessageBuilder.text(
                    self.get_tr("search_result", info=self._format_player_info(player))
                )
            ],
        )
        return True

    def _format_player_info(self, player) -> str:
        """把 Player 渲染为人类可读的绑定信息文本。"""
        name_parts = []
        java_names = [str(n) for n in player.java_name]
        bedrock_names = [str(n) for n in player.bedrock_name]
        if java_names:
            name_parts.append("Java: " + ", ".join(java_names))
        if bedrock_names:
            name_parts.append("基岩: " + ", ".join(bedrock_names))

        account_parts = []
        for platform, accounts in player.accounts.items():
            account_parts.append(
                f"{platform}: " + ", ".join(str(a) for a in accounts)
            )

        lines = []
        if name_parts:
            lines.append(" | ".join(name_parts))
        else:
            lines.append("无游戏名")
        if account_parts:
            lines.append(" | ".join(account_parts))
        return "\n".join(lines)

    def get_player_by_name(self, player_name: str) -> Optional[Player]:
        """通过玩家名获取玩家信息"""
        return self.player_manager.get_player(player_name)

    ############################################################## 白名单检查 ##############################################################

    def _get_unbound_whitelist(self) -> List[tuple]:
        """获取白名单中未绑定的玩家

        Returns
        -------
        List[tuple]
            未绑定的玩家列表，元组格式: (uuid, player_name)
        """
        if not self.whitelist:
            return []

        result = []

        # 获取所有已绑定的玩家名（转小写以便比较）
        all_bound_player_names = set()
        for player in self.player_manager.get_all_players():
            all_bound_player_names.update(
                [str(name).lower() for name in player.java_name]
            )
            all_bound_player_names.update(
                [str(name).lower() for name in player.bedrock_name]
            )

        # 检查白名单中的玩家是否在绑定列表中
        for whitelist_player in self.whitelist._api.get_whitelist():
            if whitelist_player.name.lower() not in all_bound_player_names:
                result.append((whitelist_player.uuid, whitelist_player.name))

        return result

    async def _handle_check_whitelist(self, broadcast_info: BroadcastInfo) -> bool:
        """处理白名单检查命令

        检查白名单中有哪些玩家未绑定
        """
        if not self.whitelist:
            await self.reply(broadcast_info, [MessageBuilder.text("白名单系统未启用")])
            return True

        result = self._get_unbound_whitelist()

        if not result:
            await self.reply(
                broadcast_info, [MessageBuilder.text("白名单检查: 所有玩家都已绑定~")]
            )
            return True

        reply_msg = ["白名单检查:"]
        for uuid, player_name in result:
            reply_msg.append(f"{player_name}({uuid}) 未绑定")

        await self.reply(broadcast_info, [MessageBuilder.text("\n".join(reply_msg))])
        return True

    async def _handle_remove_unbound_whitelist(
        self, broadcast_info: BroadcastInfo
    ) -> bool:
        """处理移除未绑定白名单命令

        从白名单中移除所有未绑定的玩家
        """
        if not self.whitelist:
            await self.reply(broadcast_info, [MessageBuilder.text("白名单系统未启用")])
            return True

        result = self._get_unbound_whitelist()

        if not result:
            await self.reply(
                broadcast_info, [MessageBuilder.text("没有未绑定的白名单成员~")]
            )
            return True

        reply_msg = ["已将未绑定的白名单成员移除:"]
        for uuid, player_name in result:
            self.whitelist.remove_player(player_name)
            reply_msg.append(f"{player_name}({uuid}) 已从白名单中移除")

        await self.reply(broadcast_info, [MessageBuilder.text("\n".join(reply_msg))])
        return True

    ############################################################## 多余绑定检查 ##############################################################

    async def _get_extra_bound_member(self) -> Optional[List[tuple]]:
        """获取不在群里的多余绑定成员

        Returns
        -------
        Optional[List[tuple]]
            多余绑定成员列表，元组格式: (qq_id, player_names)；
            **None 表示成员列表不可用**（平台不支持），调用方必须跳过
            而不是把它当成"没有多余绑定"或"全部都是多余绑定"。
        """
        extra_bound_members = []

        # 获取所有群成员ID
        all_member_ids = await self._get_member_in_all_groups()

        # 成员列表不可用（如 QQ 官方直连无该接口）→ 无法判断
        if all_member_ids is None:
            return None

        # 检查每个绑定的QQ账号是否还在群中（遍历全部候选平台键：
        # 自定义 source_name 键 + 历史 "QQ" 键）
        qq_keys = self._qq_platform_keys()
        for player in self.player_manager.get_all_players():
            qq_accounts = []
            for pk in qq_keys:
                qq_accounts.extend(player.accounts.get(pk, []))
            for qq_id in qq_accounts:
                if qq_id not in all_member_ids:
                    # 获取该QQ绑定的所有玩家名（转换为字符串）
                    player_names = [str(name) for name in player.java_name] + [
                        str(name) for name in player.bedrock_name
                    ]
                    extra_bound_members.append((qq_id, player_names))

        return extra_bound_members

    async def _handle_check_extra_bound(self, broadcast_info: BroadcastInfo) -> bool:
        """处理多余绑定检查命令

        检查有哪些绑定的用户不在群里
        """
        result = await self._get_extra_bound_member()

        # 成员列表不可用（平台不支持 / 未配置监听群 / 查询全部失败）→
        # 无法判断，绝不能当成"全部多余"或"没有多余"
        if result is None:
            await self.reply(
                broadcast_info,
                [MessageBuilder.text(
                    "当前无法获取 QQ 群成员列表（平台不支持、未配置监听群，"
                    "或查询全部失败），无法执行多余绑定检查。"
                    "如需清理，请手动在配置文件中核对绑定。"
                )],
            )
            return True

        if not result:
            await self.reply(
                broadcast_info, [MessageBuilder.text("多余绑定检查: 绑定成员都在群里~")]
            )
            return True

        reply_msg = ["多余绑定检查:"]
        for qq_id, player_names in result:
            player_name_str = ", ".join(player_names)
            reply_msg.append(f"{qq_id}({player_name_str}) 未在群里")

        await self.reply(broadcast_info, [MessageBuilder.text("\n".join(reply_msg))])
        return True

    async def _handle_remove_extra_bound(self, broadcast_info: BroadcastInfo) -> bool:
        """处理移除多余绑定命令

        移除所有不在群里的绑定成员
        """
        result = await self._get_extra_bound_member()

        # 成员列表不可用（平台不支持 / 未配置监听群 / 查询全部失败）→
        # 拒绝执行。若把它当成空集，命令会判定"全部绑定都是多余"并逐个
        # 删除——这是数据丢失级的严重后果。
        if result is None:
            await self.reply(
                broadcast_info,
                [MessageBuilder.text(
                    "当前无法获取 QQ 群成员列表（平台不支持、未配置监听群，"
                    "或查询全部失败），无法判定多余绑定，"
                    "已跳过移除操作（避免误删全部绑定）。"
                    "如需清理，请手动在配置文件中核对绑定。"
                )],
            )
            return True

        if not result:
            await self.reply(broadcast_info, [MessageBuilder.text("绑定成员都在群里~")])
            return True

        reply_msg = ["已将多余已绑定成员移除:"]
        for qq_id, player_names in result:
            # 从白名单中移除玩家名
            if player_names:
                self._remove_from_whitelist(player_names)

            # 从绑定数据中移除该QQ账号
            # 由于新架构中是以玩家为中心，需要找到对应的玩家并移除QQ账号绑定
            # 遍历全部候选平台键（自定义 source_name + 历史 "QQ"）
            for player in self.player_manager.get_all_players():
                removed = False
                for pk in self._qq_platform_keys():
                    if pk in player.accounts and qq_id in player.accounts[pk]:
                        player.accounts[pk].remove(qq_id)
                        # 如果QQ账户列表为空，删除该平台
                        if not player.accounts[pk]:
                            del player.accounts[pk]
                        removed = True
                if removed:
                    # 如果该玩家没有任何平台绑定了，删除整个玩家
                    has_any_account = any(
                        accounts for accounts in player.accounts.values() if accounts
                    )
                    if not has_any_account:
                        self.player_manager.remove_player(player.name)
                    break

            self.player_manager.save()

            player_name_str = ", ".join(player_names)
            reply_msg.append(f"{qq_id}({player_name_str}) 已从绑定中移除")

        await self.reply(broadcast_info, [MessageBuilder.text("\n".join(reply_msg))])
        return True
