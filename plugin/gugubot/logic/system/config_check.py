# -*- coding: utf-8 -*-
"""配置热校验系统。

群内 ``#配置检验`` / ``#config check``（管理员）对当前运行配置做语法 + 语义
逐项检查，把“配置对不对、能不能连上”问题在运行期暴露出来，而不只是
启动时的 YAML/JSON 语法报错（BotConfig.validate() 仅做语法检查）。

OneBot 模式下此系统仍注册，但只做通用语义校验（admin_ids / group_ids 等）；
QQ 官方直连相关项（appid/secret/token/intents）仅在 official 连接器存在时校验。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List, Optional

from ruamel.yaml import YAMLError

from gugubot.builder import MessageBuilder
from gugubot.config import BotConfig, yaml
from gugubot.logic.system.basic_system import BasicSystem
from gugubot.utils.types import BroadcastInfo


class ConfigCheckSystem(BasicSystem):
    """``#配置检验`` / ``#config check``：配置语法 + 语义热校验。"""

    def __init__(
        self,
        connector_manager=None,
        config: Optional[BotConfig] = None,
    ) -> None:
        BasicSystem.__init__(self, "config_check", enable=True, config=config)
        self._connector_manager = connector_manager

    def initialize(self) -> None:
        if self.logger:
            self.logger.debug("配置热校验系统已初始化")

    # ------------------------------------------------------------------ #
    # 命令入口
    # ------------------------------------------------------------------ #

    async def process_broadcast_info(self, broadcast_info: BroadcastInfo) -> bool:
        if not self.is_command(broadcast_info):
            return False

        message = broadcast_info.message
        if not message:
            return False
        content = "".join(
            seg.get("data", {}).get("text", "")
            for seg in message
            if seg.get("type") == "text"
        )
        prefix = self.config.get_keys(["GUGUBot", "command_prefix"], "#")
        rest = content[len(prefix):].strip() if content.startswith(prefix) else ""

        if rest in ("配置检验", "config check", "config_check"):
            return await self._handle_check(broadcast_info)

        return False

    # ------------------------------------------------------------------ #
    # 检查主体
    # ------------------------------------------------------------------ #

    async def _handle_check(self, broadcast_info: BroadcastInfo) -> bool:
        if not broadcast_info.is_admin:
            await self.reply_to_source(
                broadcast_info,
                [MessageBuilder.text("仅管理员可执行配置检验")],
            )
            return True

        lines: List[str] = ["== GUGUBot 配置检验 =="]
        ok = True

        # ---- 1) 配置文件语法 ----
        syntax_ok, syntax_msg = self._check_syntax()
        lines.append(f"配置语法: {'正常' if syntax_ok else '异常'}")
        if not syntax_ok:
            ok = False
            lines.append(f"  详情: {syntax_msg}")

        # ---- 2) 通用语义项 ----
        general = self._check_general()
        lines.extend(general["lines"])
        if not general["ok"]:
            ok = False

        # ---- 3) QQ 连接器专项 ----
        qq = self._check_qq_connector()
        lines.extend(qq["lines"])
        if not qq["ok"]:
            ok = False

        lines.append("")
        lines.append("结论: " + ("全部通过 ✅" if ok else "存在问题 ❌，请按上方详情修正"))

        await self.reply_to_source(
            broadcast_info, [MessageBuilder.text("\n".join(lines))]
        )
        return True

    # ------------------------------------------------------------------ #
    # 单项检查
    # ------------------------------------------------------------------ #

    def _check_syntax(self) -> tuple[bool, str]:
        """复用 BotConfig.validate 的语法检查逻辑，返回 (ok, msg)。"""
        cfg = self.config
        path: Path | None = getattr(cfg, "path", None)
        if not path or not path.exists():
            return True, "配置文件不存在，将使用默认配置"
        try:
            with path.open("r", encoding="UTF-8") as f:
                if cfg.yaml_format:
                    yaml.load(f)
                else:
                    json.load(f)
            return True, ""
        except (YAMLError, json.JSONDecodeError) as e:
            return False, str(e)

    def _check_general(self) -> dict[str, Any]:
        lines: List[str] = []
        ok = True

        admin_ids = self.config.get_keys(
            ["connector", "QQ", "permissions", "admin_ids"], []
        ) or []
        if not admin_ids:
            ok = False
            lines.append("admin_ids: 未配置 ⚠️ （建议至少填一个管理员 QQ 号）")
        else:
            lines.append(f"admin_ids: 已配置 {len(admin_ids)} 个 ✅")

        group_ids = self.config.get_keys(
            ["connector", "QQ", "permissions", "group_ids"], []
        ) or []
        if not group_ids:
            ok = False
            lines.append("group_ids: 未配置 ⚠️ （未指定监听群，QQ 消息不会转发到 MC）")
        else:
            lines.append(f"group_ids: 已配置 {len(group_ids)} 个 ✅")

        return {"ok": ok, "lines": lines}

    def _check_qq_connector(self) -> dict[str, Any]:
        lines: List[str] = []
        ok = True

        conn = self.config.get_keys(["connector", "QQ", "connection"], {}) or {}
        ctype = conn.get("type", "onebot")

        if ctype != "official":
            lines.append(f"QQ 连接类型: {ctype}（非官方直连，跳过官方专项检查）")
            return {"ok": ok, "lines": lines}

        lines.append("QQ 连接类型: official（官方直连）")

        official = conn.get("official", {}) or {}
        appid = official.get("appid", "")
        secret = official.get("secret", "")

        if not appid:
            ok = False
            lines.append("official.appid: 未配置 ❌（官方直连必填）")
        else:
            lines.append(f"official.appid: 已配置 ✅")

        if not secret:
            ok = False
            lines.append("official.secret: 未配置 ❌（官方直连必填）")
        else:
            lines.append(f"official.secret: 已配置 ✅（长度 {len(secret)}）")

        # intents
        intents = official.get("intents", {}) or {}
        enabled_intents = [k for k, v in intents.items() if v]
        if not enabled_intents:
            ok = False
            lines.append("official.intents: 全部未开启 ⚠️ （可能收不到任何事件）")
        else:
            lines.append(f"official.intents: {', '.join(enabled_intents)} ✅")

        # max_message_length：<=0 曾会导致发送分片死循环（挂死事件循环），
        # 运行期已钳制回默认值；配置检查阶段给出明确提示
        mml = self.config.get_keys(
            ["connector", "QQ", "max_message_length"], 2000
        )
        try:
            mml_val = int(mml)
        except (TypeError, ValueError):
            ok = False
            lines.append(f"max_message_length: {mml!r} 不是数字 ❌（将回退默认 2000）")
        else:
            if mml_val <= 0:
                ok = False
                lines.append(
                    f"max_message_length: {mml_val} ≤ 0 ❌"
                    "（会导致分片死循环，运行期已强制回退默认 2000，请修正配置）"
                )
            else:
                lines.append(f"max_message_length: {mml_val} ✅")

        # 运行期连接器是否存在（官方模式下应当有 id_mapper + ws_client）
        official_connector = None
        if self._connector_manager:
            for c in getattr(self._connector_manager, "connectors", []) or []:
                if hasattr(c, "id_mapper") and hasattr(c, "ws_client"):
                    official_connector = c
                    break
        if official_connector is None:
            ok = False
            lines.append("运行期官方连接器: 未启动 ❌（WS 未连接，检查日志）")
        else:
            ws = getattr(official_connector, "ws_client", None)
            ws_state = "已连接" if (ws and getattr(ws, "is_connected", lambda: False)()) else "未连接"
            lines.append(f"运行期官方连接器: 存在（WS {ws_state}） ✅")

        # ---- 官方模式专属：ID 形态与依赖成员列表的系统 ----
        mapper = getattr(official_connector, "id_mapper", None)
        if mapper is not None:
            # 1) admin_ids / admin_group_ids / group_ids 应为虚拟号（官方
            #    openid 非数字且不可发送为普通目标；虚拟号为 12 位数字）
            def _check_virtual(key_path: list, label: str) -> None:
                nonlocal ok
                ids = self.config.get_keys(key_path, []) or []
                bad = [
                    i for i in ids
                    if i and not (
                        str(i).isdigit()
                        and len(str(i)) == 12
                        and str(i)[:6] in ("100000", "200000")
                    )
                ]
                if bad:
                    ok = False
                    lines.append(
                        f"{label}: 含疑似非虚拟号条目 {bad[:3]}"
                        f"{'...' if len(bad) > 3 else ''} ❌（官方模式请填 #我是谁 "
                        f"查到的 100000xxx/200000xxx 虚拟号，而非 QQ 号/openid）"
                    )

            _check_virtual(
                ["connector", "QQ", "permissions", "admin_ids"], "admin_ids"
            )
            _check_virtual(
                ["connector", "QQ", "permissions", "admin_group_ids"],
                "admin_group_ids",
            )
            _check_virtual(
                ["connector", "QQ", "permissions", "group_ids"], "group_ids"
            )

            # 2) 依赖群成员列表的系统在官方模式必失效（官方无成员列表接口）
            for sys_key, sys_label in (
                ("unbound_check", "未绑定检查"),
                ("inactive_check", "不活跃检查"),
            ):
                if self.config.get_keys(
                    ["system", sys_key, "enable"], False
                ):
                    lines.append(
                        f"{sys_label}: 已启用 ⚠️ （官方平台无群成员列表接口，"
                        f"该系统无法获取成员，将不生效——建议关闭或换 OneBot 模式）"
                    )

        return {"ok": ok, "lines": lines}
