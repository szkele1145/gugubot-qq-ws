# -*- coding: utf-8 -*-
"""通用帮助系统模块。

该模块提供了通用帮助功能，响应 # 或 #帮助 命令。
"""

from typing import Optional

from mcdreforged.api.types import PluginServerInterface

from gugubot.builder import MessageBuilder
from gugubot.config import BotConfig
from gugubot.logic.system.basic_system import BasicSystem
from gugubot.utils.types import BroadcastInfo


class GeneralHelpSystem(BasicSystem):
    """通用帮助系统，响应帮助命令。

    当用户发送 # 或 #帮助 时，显示通用帮助信息。

    Attributes
    ----------
    name : str
        系统名称
    enable : bool
        系统是否启用
    """

    def __init__(
            self, server: PluginServerInterface, config: Optional[BotConfig] = None
    ) -> None:
        """初始化通用帮助系统。"""
        super().__init__("general_help", enable=True, config=config)
        self.server = server

    def initialize(self) -> None:
        """初始化系统，加载配置等"""
        return

    async def process_broadcast_info(self, broadcast_info: BroadcastInfo) -> bool:
        """处理接收到的命令。

        Parameters
        ----------
        broadcast_info: BroadcastInfo
            广播信息，包含消息内容

        Returns
        -------
        bool
            是否处理了该消息
        """
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

        command_prefix = self.config.get("GUGUBot", {}).get("command_prefix", "#")

        # 检查是否是帮助命令
        help_command = self.get_tr("help_command")

        if content == command_prefix or content == f"{command_prefix}{help_command}":
            return await self._handle_help(broadcast_info)

        return False

    async def _handle_help(self, broadcast_info: BroadcastInfo) -> bool:
        """处理帮助命令"""
        command_prefix = self.config.get("GUGUBot", {}).get("command_prefix", "#")
        is_admin = broadcast_info.is_admin

        # 获取快捷指令的翻译名称
        player_list_cmd = self.server.tr("gugubot.system.list.list")
        tps_cmd = self.server.tr("gugubot.system.tps.tps")
        key_word_add_cmd = self.server.tr("gugubot.system.key_words.add")
        key_word_remove_cmd = self.server.tr("gugubot.system.key_words.remove")
        key_word_list_cmd = self.server.tr("gugubot.system.key_words.list")

        # 获取各系统的翻译名称
        key_words_name = self.server.tr("gugubot.system.key_words.name")
        ban_words_name = self.server.tr("gugubot.system.ban_words.name")
        bound_name = self.server.tr("gugubot.system.bound.name")
        bound_notice_name = self.server.tr("gugubot.system.bound_notice.name")
        whitelist_name = self.server.tr("gugubot.system.whitelist.name")
        active_whitelist_name = self.server.tr("gugubot.system.active_whitelist.name")
        startup_command_name = self.server.tr("gugubot.system.startup_command.name")
        execute_name = self.server.tr("gugubot.system.execute.name")
        execute_execute_cmd = self.server.tr("gugubot.system.execute.execute")
        execute_mcdr_cmd = self.server.tr("gugubot.system.execute.mcdr")
        style_name = self.server.tr("gugubot.system.style.name")
        todo_name = self.server.tr("gugubot.system.todo.name")
        unbound_check_name = self.server.tr("gugubot.system.unbound_check.name")
        inactive_check_name = self.server.tr("gugubot.system.inactive_check.name")
        vote_name = self.server.tr("gugubot.system.vote.name")

        # 官方直连专用命令：仅作为 get_tr 的可选参数传入，供自定义语言模板
        # 以 {official_status} / {whoami} 占位符引用；内置 zh_cn/en_us 模板
        # 已直接写死命令名，因此这两个参数缺省不影响显示。
        official_status_cmd = "#状态 / #status"
        whoami_cmd = "#我是谁 / #whoami"

        # 根据是否是管理员显示不同的帮助信息
        if is_admin:
            help_msg = self.get_tr(
                "admin_help_msg",
                command_prefix=command_prefix,
                player_list=player_list_cmd,
                tps=tps_cmd,
                key_word_add=key_word_add_cmd,
                key_word_remove=key_word_remove_cmd,
                key_word_list=key_word_list_cmd,
                key_words=key_words_name,
                ban_words=ban_words_name,
                bound=bound_name,
                bound_notice=bound_notice_name,
                whitelist=whitelist_name,
                active_whitelist=active_whitelist_name,
                startup_command=startup_command_name,
                execute=execute_name,
                execute_execute=execute_execute_cmd,
                execute_mcdr=execute_mcdr_cmd,
                style=style_name,
                todo=todo_name,
                vote=vote_name,
                unbound_check=unbound_check_name,
                inactive_check=inactive_check_name,
                official_status=official_status_cmd,
                whoami=whoami_cmd,
            )
        else:
            help_msg = self.get_tr(
                "help_msg",
                command_prefix=command_prefix,
                player_list=player_list_cmd,
                tps=tps_cmd,
                key_word_add=key_word_add_cmd,
                key_word_remove=key_word_remove_cmd,
                key_word_list=key_word_list_cmd,
                key_words=key_words_name,
                bound=bound_name,
                style=style_name,
                todo=todo_name,
                vote=vote_name,
                whoami=whoami_cmd,
                # 官方状态占位符也一并传入：内置模板不用它，但**自定义
                # 语言模板**若在 help_msg 里引用了 {official_status}，
                # 缺参数会让 str.format 抛 KeyError → 用户收到异常而非
                # 帮助文本。显示命令名本身无害（#状态 仍只对管理员生效）。
                official_status=official_status_cmd,
                # 同理补齐其余系统名占位符：自定义语言模板在 help_msg
                # 里引用 {ban_words} / {whitelist} 等时，缺参数会抛
                # KeyError。非管理员看到这些名字没有副作用（对应命令
                # 本身仍受权限校验）。
                ban_words=ban_words_name,
                bound_notice=bound_notice_name,
                whitelist=whitelist_name,
                active_whitelist=active_whitelist_name,
                startup_command=startup_command_name,
                execute=execute_name,
                execute_execute=execute_execute_cmd,
                execute_mcdr=execute_mcdr_cmd,
                unbound_check=unbound_check_name,
                inactive_check=inactive_check_name,
            )

        await self.reply(broadcast_info, [MessageBuilder.text(help_msg)])
        return True
