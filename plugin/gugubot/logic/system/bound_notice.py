# -*- coding: utf-8 -*-

from typing import Optional

from gugubot.builder import MessageBuilder
from gugubot.config import BotConfig
from gugubot.logic.system.basic_system import BasicSystem
from gugubot.utils.types import BroadcastInfo


class BoundNoticeSystem(BasicSystem):
    """绑定提醒系统，用于提醒未绑定的玩家进行账号绑定。

    当玩家发送消息时，如果该玩家未在玩家管理器中绑定账号，
    则提醒其进行绑定。不会拦截消息，让其他系统继续处理。
    """

    def __init__(self, config: Optional[BotConfig] = None) -> None:
        """初始化绑定提醒系统。"""
        super().__init__("bound_notice", enable=False, config=config)
        self.bound_system = None

    def initialize(self) -> None:
        """初始化系统，加载配置等"""
        self.logger.debug("绑定提醒系统已初始化")

    def set_bound_system(self, bound_system) -> None:
        """设置绑定系统引用，用于访问玩家管理器"""
        self.bound_system = bound_system

    async def process_broadcast_info(self, broadcast_info: BroadcastInfo) -> bool:
        """处理接收到的消息。

        Parameters
        ----------
        broadcast_info: BroadcastInfo
            广播信息，包含消息内容
        """
        if broadcast_info.event_type != "message":
            return False

        message = broadcast_info.message

        if not message:
            return False

        # 先检查是否是开启/关闭命令
        if await self.handle_enable_disable(broadcast_info):
            return True

        # 如果系统未启用或没有绑定系统引用，不处理
        if not self.enable or not self.bound_system:
            return False

        return await self._check_and_notify(broadcast_info)

    async def _check_and_notify(self, broadcast_info: BroadcastInfo) -> bool:
        """检查玩家是否已绑定，如果未绑定则发送提醒"""
        # 只在有发送者ID的情况下检查
        if not broadcast_info.sender_id:
            return False

        # 排除管理员
        if broadcast_info.is_admin:
            return False

        # 只处理 QQ 来源的消息，避免对 Minecraft 等来源广播未绑定提示
        qq_source = self.config.get_keys(["connector", "QQ", "source_name"], "QQ")
        if broadcast_info.source.origin != qq_source:
            return False

        # 排除管理群消息
        if broadcast_info.source_id:
            admin_group_ids = self.config.get_keys(
                ["connector", "QQ", "permissions", "admin_group_ids"], []
            )
            # 连接器的注册名（connector.source）就是 source_name，不是配置
            # 节名 "QQ"——用户改过 source_name 后硬编码 "QQ" 会取到 None →
            # 落进下面的 OneBot 兜底分支 → 拿虚拟群号和真实群号配置比对 →
            # 永远不相等 → 管理群里的未绑定玩家照样收到「请先绑定」提醒
            # （设计意图是管理群不发这类提醒）。
            qq_connector = self.system_manager.connector_manager.get_connector(
                qq_source
            )
            # getattr 兜底：OneBot 链路连接器无 id_mapper 属性
            id_mapper = getattr(qq_connector, "id_mapper", None) if qq_connector else None
            if id_mapper:
                # source_id 是虚拟群号，admin_group_ids 是配置里的真实群号；
                # 统一翻成 group_openid 后再比对，避免虚实号混叠
                source_openid = id_mapper.to_official_group(broadcast_info.source_id)
                admin_openids = [
                    id_mapper.to_official_group(str(gid))
                    for gid in admin_group_ids if gid
                ]
                if source_openid in admin_openids:
                    return False
            else:
                # OneBot 链路没有 id_mapper：source_id 本身就是真实群号，
                # 直接按字符串比对即可。
                # （若在这里静默跳过，管理群里的未绑定玩家就会收到
                #   「请先绑定」提醒——而设计意图是管理群不发这类提醒。）
                admin_group_strs = {str(gid) for gid in admin_group_ids if gid}
                if str(broadcast_info.source_id) in admin_group_strs:
                    return False

        # 检查玩家是否在玩家管理器中
        player = self.bound_system.player_manager.get_player(
            broadcast_info.sender_id, platform=broadcast_info.source.origin
        )

        # 如果玩家未绑定，发送提醒消息
        if not player:
            command_prefix = self.config.get("GUGUBot", {}).get("command_prefix", "#")
            # 获取绑定系统的名称
            bound_name = self.bound_system.get_tr("name")

            notice_msg = self.get_tr(
                "notice_message", command_prefix=command_prefix, bound_name=bound_name
            )
            await self.reply(
                broadcast_info,
                [
                    MessageBuilder.at(broadcast_info.sender_id),
                    MessageBuilder.text(notice_msg),
                ],
            )

        # 不拦截消息，让其他系统继续处理
        return False
