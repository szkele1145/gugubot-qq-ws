import re
import traceback
from typing import Optional

from mcdreforged.api.types import Info, PluginServerInterface

from gugubot.parser.basic_parser import BasicParser
from gugubot.utils.types import BroadcastInfo, Source


class MCParser(BasicParser):
    """Minecraft服务器消息解析器。

    处理来自Minecraft服务器的消息，包括玩家聊天、系统消息等。
    """

    async def parse(
        self, raw_message: Info, server: PluginServerInterface
    ) -> Optional[BroadcastInfo]:
        """解析Minecraft消息。

        Parameters
        ----------
        raw_message : Any
            从Minecraft服务器接收到的原始消息

        Returns
        -------
        Optional[BroadcastInfo]
            解析后的广播信息对象
        """
        try:
            player = raw_message.player
            content = raw_message.content

            if self._ignore_message(content):
                return None

            self.logger.debug(
                f"[GUGUBot]收到Minecraft消息 - " f"玩家: {player}, " f"内容: {content}"
            )

            broadcast_info = BroadcastInfo(
                event_type="message",
                event_sub_type="group",
                # **MC 聊天是纯文本，不做 CQ 解析**。
                #
                # CQ 码是 QQ 侧的表示法，只应出现在**来自 QQ** 的消息里
                # （qq_parser 解析它是对的）。旧实现把玩家打的字直接交给
                # CQHandler.parse，于是任何能在 MC 里说话的人打一句
                # ``[CQ:at,qq=all] 都来看`` 就能在绑定的 QQ 群里触发
                # **真正的 @全体成员**；``[CQ:image,file=<任意URL>]`` 还能
                # 往群里发任意图片（可用于钓鱼）。属于把用户输入当指令执行。
                message=[{"type": "text", "data": {"text": content}}],
                raw=content,
                server=server,
                logger=self.logger,
                _source=Source(self.connector.source),  # 使用 Source 类追踪来源链
                source_id=None,
                sender=raw_message.player,
                sender_id=raw_message.player,
                is_admin=await self.connector._is_admin(raw_message.player),
            )

            return broadcast_info

        except Exception as e:
            error_msg = str(e) + "\n" + traceback.format_exc()
            self.logger.error(f"MC消息解析失败: {error_msg}")
            raise

    def _ignore_message(self, content: str) -> bool:
        """检查消息是否需要忽略"""
        config = self.connector.config
        # !!qq 用于跨平台广播到 QQ，不忽略
        qq_cmd = config.get_keys(["system", "cross_broadcast", "qq_command"], "!!qq")
        qq_cmd_prefix = qq_cmd.split()[0] if qq_cmd.split() else qq_cmd
        if content.strip().startswith(qq_cmd_prefix):
            return False

        ignore_mc_command_patterns = config.get_keys(
            ["connector", "minecraft", "ignore_mc_command_patterns"], []
        )

        for pattern in ignore_mc_command_patterns:
            if re.match(pattern, content):
                return True
        return False
