# -*- coding: utf-8 -*-
"""配置热重载系统。

群内 ``#重载配置`` / ``#reload config``（管理员）重新从磁盘读取配置文件，
**无需重启服务器、无需 ``!!MCDR reload plugin``**。

设计要点
--------
1. **原子性**：解析失败（YAML 语法错等）时旧配置原封不动继续生效，
   不会出现"配置被清空"的中间态——实现见 ``BotConfig.reload()``。
2. **共享实例**：所有系统共用同一个 ``BotConfig`` 对象（见
   ``SystemManager.register_system``），``get_keys`` 每次实时读 dict，
   因此重载后各系统立即读到新值，无需重建系统实例。
3. **enable 状态同步**：``system.<name>.enable`` 存在配置文件里，
   重载后需要同步到各系统实例的 ``self.enable``，否则"关掉某个系统"
   不会立即生效。
4. **连接器参数**：QQ 连接器的 appid/secret/intents/监听群等是
   **构造期**参数，热重载无法原地改变连接——这类改动需要重启插件，
   因此重载结果里会明确提示。

命令
----
``#重载配置`` / ``#reload config`` —— 管理员专用。
"""

from __future__ import annotations

from typing import List, Optional

from gugubot.builder import MessageBuilder
from gugubot.config import BotConfig
from gugubot.logic.system.basic_system import BasicSystem
from gugubot.utils.types import BroadcastInfo


class ConfigReloadSystem(BasicSystem):
    """``#重载配置`` / ``#reload config``：运行期热重载配置文件。"""

    # 命令别名（中英文各一）
    _COMMANDS = ("重载配置", "reload config", "reload_config", "config reload")

    def __init__(
        self,
        connector_manager=None,
        config: Optional[BotConfig] = None,
    ) -> None:
        BasicSystem.__init__(self, "config_reload", enable=True, config=config)
        self._connector_manager = connector_manager

    def initialize(self) -> None:
        if self.logger:
            self.logger.debug("配置热重载系统已初始化")

    # ------------------------------------------------------------------ #
    # 命令入口
    # ------------------------------------------------------------------ #

    async def process_broadcast_info(self, broadcast_info: BroadcastInfo) -> bool:
        if not self.is_command(broadcast_info):
            return False

        message = broadcast_info.message
        if not message:
            return False

        # QQ 官方 @机器人时消息段首位可能是 at 段，text 段不必然在 index 0；
        # 须拼接全部 type=="text" 段后再做前缀/命令解析。
        content = "".join(
            seg.get("data", {}).get("text", "")
            for seg in message
            if seg.get("type") == "text"
        ).strip()
        prefix = self.config.get_keys(["GUGUBot", "command_prefix"], "#")
        rest = content[len(prefix):].strip() if content.startswith(prefix) else ""

        if rest.lower() in [c.lower() for c in self._COMMANDS]:
            return await self._handle_reload(broadcast_info)

        return False

    # ------------------------------------------------------------------ #
    # 重载主体
    # ------------------------------------------------------------------ #

    async def _handle_reload(self, broadcast_info: BroadcastInfo) -> bool:
        if not broadcast_info.is_admin:
            await self.reply_to_source(
                broadcast_info,
                [MessageBuilder.text("仅管理员可执行配置重载")],
            )
            return True

        if self.config is None:
            await self.reply_to_source(
                broadcast_info,
                [MessageBuilder.text("配置重载失败：未找到配置对象")],
            )
            return True

        ok, msg = self.config.reload()

        if not ok:
            if self.logger:
                self.logger.error(f"[GUGUBot] 配置热重载失败: {msg}")
            await self.reply_to_source(
                broadcast_info,
                [
                    MessageBuilder.text(
                        f"配置重载失败 ❌\n{msg}\n\n"
                        "旧配置仍在生效，请修正配置文件后重试。"
                    )
                ],
            )
            return True

        lines: List[str] = [f"配置重载成功 ✅\n{msg}"]
        lines.extend(self._sync_enable_states())
        lines.extend(self._reload_system_caches())
        lines.extend(self._connector_notes())

        if self.logger:
            self.logger.info(f"[GUGUBot] 配置热重载完成: {msg}")

        await self.reply_to_source(
            broadcast_info, [MessageBuilder.text("\n".join(lines))]
        )
        return True

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #

    def _sync_enable_states(self) -> List[str]:
        """把配置里的 ``system.<name>.enable`` 同步到各系统实例。

        共享的 ``BotConfig`` 换内容后，``get_keys`` 立刻能读到新值，
        但 ``BasicSystem.enable`` 是构造期算好的属性，不会自动更新——
        需显式同步，否则"在配置里关掉某个系统"不会立即生效。
        """
        notes: List[str] = []
        manager = getattr(self, "system_manager", None)
        systems = getattr(manager, "systems", None) if manager else None
        if not systems:
            return notes

        changed: List[str] = []
        for system in systems:
            if system.name in ("config_reload",):
                continue
            if "system" not in self.config:
                continue
            raw = self.config.get_keys(["system", system.name], None)
            if not isinstance(raw, dict) or "enable" not in raw:
                continue
            new_enable = bool(raw.get("enable"))
            if getattr(system, "enable", None) != new_enable:
                system.enable = new_enable
                changed.append(f"{system.name}={'开' if new_enable else '关'}")

        if changed:
            notes.append("系统开关已同步: " + "、".join(changed))
        return notes

    def _reload_system_caches(self) -> List[str]:
        """调用各系统的 ``reload_config()`` 钩子，重新读取被缓存的配置值。

        大多数系统在运行时才读配置，热重载自动生效；但把配置缓存成实例
        属性的系统（如投票系统的关键词/投票类型注册）必须显式重建，
        否则改了配置也不生效。钩子由 :meth:`BasicSystem.reload_config`
        提供默认空实现，只有需要重建的系统才覆写。
        """
        notes: List[str] = []
        manager = getattr(self, "system_manager", None)
        systems = getattr(manager, "systems", None) if manager else None
        if not systems:
            return notes

        for system in systems:
            if system.name == "config_reload":
                continue
            hook = getattr(system, "reload_config", None)
            if hook is None:
                continue
            try:
                changed = hook()
            except Exception as e:
                if self.logger:
                    self.logger.error(
                        f"[GUGUBot] 系统 {system.name} 重载配置失败: {e}"
                    )
                notes.append(f"⚠️ {system.name} 重载失败: {e}")
                continue
            if changed:
                notes.append(
                    f"{system.name} 已重新加载: " + "、".join(str(c) for c in changed)
                )
        return notes

    def _connector_notes(self) -> List[str]:
        """提示哪些配置改动需要重启插件才能生效。"""
        notes: List[str] = []
        cm = self._connector_manager
        connectors = getattr(cm, "connectors", None) if cm else None
        if not connectors:
            return notes

        for c in connectors:
            name = getattr(c, "source", "?")
            # 官方直连连接器：appid/secret/intents/监听群均为构造期参数
            if hasattr(c, "id_mapper") and hasattr(c, "ws_client"):
                notes.append(
                    f"⚠️ QQ 官方直连连接器的 appid/secret/intents/监听群"
                    f"属于启动期参数，改动这些需要 ``!!MCDR reload plugin``；"
                    f"普通系统开关与转发规则已即时生效。"
                )
                break

        return notes
