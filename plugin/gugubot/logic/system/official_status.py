# -*- coding: utf-8 -*-
"""官方直连运行状态系统。

群内 ``#状态``（管理员）显示 QQ 官方直连的健康度：
WS 连接、access_token、回复池深度、群/用户绑定数、被动回复通道余量。

OneBot 模式下此系统自动不注册（仅 official 连接器存在时启用）。
"""

from __future__ import annotations

from typing import Optional

from gugubot.builder import MessageBuilder
from gugubot.config import BotConfig
from gugubot.logic.system.basic_system import BasicSystem
from gugubot.utils.types import BroadcastInfo


class OfficialStatusSystem(BasicSystem):
    """``#状态``：查看 QQ 官方直连运行状态。"""

    def __init__(
        self,
        connector_manager=None,
        config: Optional[BotConfig] = None,
    ) -> None:
        BasicSystem.__init__(self, "official_status", enable=True, config=config)
        self._connector_manager = connector_manager

    def initialize(self) -> None:
        if self.logger:
            self.logger.debug("官方直连状态系统已初始化")

    def _get_official_connector(self):
        """拿官方连接器（None = OneBot 模式，本系统不处理）。

        duck typing 判定：官方连接器有 id_mapper + ws_client 两个属性，
        OneBot 连接器没有——避免 import 循环与类名硬匹配。
        """
        if not self._connector_manager:
            return None
        for c in self._connector_manager.connectors:
            if hasattr(c, "id_mapper") and hasattr(c, "ws_client"):
                return c
        return None

    async def process_broadcast_info(self, broadcast_info: BroadcastInfo) -> bool:
        connector = self._get_official_connector()
        if connector is None:
            return False  # OneBot 模式：不拦截

        if not self.is_command(broadcast_info):
            return False

        message = broadcast_info.message
        if not message:
            return False
        content = message[0].get("data", {}).get("text", "")
        prefix = self.config.get_keys(["GUGUBot", "command_prefix"], "#")
        rest = content[len(prefix):].strip() if content.startswith(prefix) else ""

        # ---- #状态 / #status：官方直连健康度 ----
        if rest in ("状态", "status", "official_status"):
            return await self._handle_status(broadcast_info, connector)

        # ---- #我是谁 / #whoami：自助查询虚拟号与绑定 ----
        if rest in ("我是谁", "whoami"):
            return await self._handle_whoami(broadcast_info, connector)

        return False

    # ------------------------------------------------------------------ #
    # #状态
    # ------------------------------------------------------------------ #

    async def _handle_status(self, broadcast_info: BroadcastInfo, connector) -> bool:
        if not broadcast_info.is_admin:
            await self.reply_to_source(
                broadcast_info,
                [MessageBuilder.text("仅管理员可查看官方直连状态")],
            )
            return True

        # ---- 采集状态 ----
        lines = ["== GUGUBot 官方直连状态 =="]
        try:
            ws = getattr(connector, "ws_client", None)
            ws_state = "已连接" if (ws and ws.is_connected()) else "未连接"
            session = getattr(ws, "session_id", None) or "-"
            lines.append(f"WS: {ws_state}  会话: {session[:8]}…")
        except Exception as e:
            lines.append(f"WS: 读取失败({e})")

        try:
            tm = getattr(connector, "token_manager", None)
            if tm and tm.configured:
                import time as _t
                remain = int(max(0, (getattr(tm, "_expires_at", 0) - _t.time())))
                lines.append(f"access_token: 有效，剩余 {remain // 60} 分钟")
            else:
                lines.append("access_token: 未配置")
        except Exception as e:
            lines.append(f"access_token: 读取失败({e})")

        try:
            pool = getattr(connector, "reply_pool", None)
            if pool:
                total = sum(pool.size(k) for k in _known_group_keys(connector))
                lines.append(f"被动回复池: {total} 条 msg_id 待用")
        except Exception:
            pass

        try:
            mapper = getattr(connector, "id_mapper", None)
            if mapper:
                groups = mapper.known_legacy_groups()
                users = len(getattr(mapper, "_user_map", {}) or {})
                lines.append(f"已映射: {len(groups)} 个群, {users} 个用户")
                # 配置群号绑定详情
                bound = getattr(mapper, "_openid_group_legacy", {}) or {}
                pending = getattr(mapper, "_pending_legacy_groups", None) or []
                if bound:
                    detail = ", ".join(f"{v}" for v in list(bound.values())[:5])
                    lines.append(f"  群号绑定: {detail}")
                if pending:
                    lines.append(f"  待绑定配置槽位: {len(pending)} 个")
        except Exception:
            pass

        await self.reply_to_source(
            broadcast_info, [MessageBuilder.text("\n".join(lines))]
        )
        return True

    # ------------------------------------------------------------------ #
    # #我是谁
    # ------------------------------------------------------------------ #

    async def _handle_whoami(self, broadcast_info: BroadcastInfo, connector) -> bool:
        """自助查询：我的虚拟号 / 绑定游戏名 / 群角色。

        官方模式下用户看不到自己的真实 QQ 号，此命令让他能拿到
        虚拟号（填 admin_ids 用）和绑定信息。
        """
        sender_id = str(broadcast_info.sender_id or "")
        if not sender_id:
            await self.reply_to_source(
                broadcast_info, [MessageBuilder.text("无法识别你的身份")]
            )
            return True

        lines = [f"你的虚拟号: {sender_id}"]

        # 绑定游戏名（bound 系统的 player_manager）
        try:
            bound_system = self.system_manager.get_system("bound")
            if bound_system:
                player = bound_system.player_manager.get_player(sender_id)
                if player:
                    names = list(player.java_name or []) + list(player.bedrock_name or [])
                    if names:
                        lines.append(f"绑定的游戏名: {', '.join(names)}")
                    else:
                        lines.append("尚未绑定游戏名（用绑定命令绑定后此处显示）")
                else:
                    lines.append("尚未绑定游戏名")
        except Exception:
            pass

        # 群内角色（官方事件 member_role）
        raw = broadcast_info.raw if isinstance(broadcast_info.raw, dict) else {}
        author = raw.get("author", {}) if isinstance(raw.get("author"), dict) else {}
        role = (author.get("member_role") or "").strip()
        if role:
            role_map = {"owner": "群主", "admin": "管理员", "member": "普通成员"}
            lines.append(f"群内角色: {role_map.get(role, role)}")

        lines.append(f"管理员设置: 把虚拟号 {sender_id} 填进 admin_ids 即可")

        await self.reply_to_source(
            broadcast_info, [MessageBuilder.text('\n'.join(lines))]
        )
        return True


def _known_group_keys(connector):
    """回复池里所有群的 openid（读内部结构，异常即空）。"""
    try:
        pool = connector.reply_pool
        return list(pool._pools.keys())
    except Exception:
        return []
