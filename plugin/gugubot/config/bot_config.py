# -*- coding: utf-8 -*-
"""Bot configuration class for GUGUBot."""

import json
from pathlib import Path

from ruamel.yaml import YAMLError

from gugubot.config.basic_config import BasicConfig, yaml


class BotConfig(BasicConfig):
    """
    Bot configuration class for GUGUBot.
    It is used to store the configuration of this plugin.
    """

    def __init__(
        self, path="./config.yml", default_content=None, yaml_format=True, logger=None
    ):
        self.logger = logger
        # 记录初始化时配置文件是否真实存在：load() 在文件缺失时会写默认
        # 内容落盘，reload() 需要区分"用户的配置"与"插件生成的默认壳"
        self._file_existed_at_init = Path(path).with_suffix(
            ".yml" if yaml_format else ".json"
        ).is_file()
        super().__init__(path, default_content, yaml_format)

    def load(self):
        self.validate()
        super().load()
        # Additional checks for the plugin configuration
        self.plugin_check()

    def reload(self) -> tuple[bool, str]:
        """原子重载配置文件（供运行期热重载使用）。

        与 :meth:`load` 的区别：

        1. **不写回默认配置**——文件不存在或为空时直接失败，绝不把默认内容
           落盘覆盖用户手写的配置；
        2. **原子性**——先解析到临时 dict，只有解析成功才替换当前内容；
           YAML 语法错误时旧配置原封不动继续生效（不会出现"配置被清空"
           的中间态）；
        3. **不跑 plugin_check**——该方法会把 None/空值规整后**写回文件**，
           热重载路径不应擅自改写用户的配置文件。

        Returns
        -------
        tuple[bool, str]
            ``(是否成功, 说明信息)``。失败时说明信息含错误详情。
        """
        if not self.path.is_file():
            return False, f"配置文件不存在: {self.path}"
        if self.path.stat().st_size == 0:
            return False, f"配置文件为空: {self.path}"

        # 初始化时文件不存在 → 现在的文件是 load() 落盘的默认壳（用户从未
        # 配置过），重载它没有意义，且会把内存里的运行配置清成默认模板
        if not self._file_existed_at_init:
            return False, (
                f"配置文件 {self.path.name} 是插件启动时自动生成的默认模板，"
                "尚未被用户配置过，无需重载"
            )

        try:
            with self._open_for_read() as f:
                data = yaml.load(f) if self.yaml_format else json.load(f)
        except (YAMLError, json.JSONDecodeError) as e:
            return False, f"配置文件语法错误（已保留原配置继续生效）: {e}"
        except Exception as e:
            return False, f"配置文件读取失败（已保留原配置继续生效）: {e}"

        if data is None:
            data = {}
        if not isinstance(data, dict):
            return False, (
                f"配置文件根节点应为映射/字典，实际为 {type(data).__name__}"
                "（已保留原配置继续生效）"
            )

        # 解析成功 → 原子替换
        self.clear()
        self.update(data)
        return True, f"已重载 {self.path.name}（{len(data)} 个顶层配置项）"

    def add_new_config(self, server):
        """Add new configs from latest version to current config"""
        # read the latest config file from MCDR package
        with server.open_bundled_file(
            "gugubot/config/defaults/default_config.yml"
        ) as file_handler:
            message = file_handler.read()
        message_unicode = message.decode("utf-8").replace("\r\n", "\n")
        yaml_data = yaml.load(message_unicode)

        # Update the config with the latest version
        def _update_config(old_yaml_data, new_yaml_data):
            for key, value in old_yaml_data.items():
                if isinstance(value, dict):
                    _update_config(value, new_yaml_data[key])
                else:
                    new_yaml_data[key] = value
            return new_yaml_data

        self.update(_update_config(self, yaml_data))
        self.save()

    def plugin_check(self):
        """Avoid None or empty value in config"""

        # Not exist/None -> error
        # not list -> [value]
        # extra empty value -> remove empty value
        list_type_configs = ["admin_id", "group_id"]

        for config_name in list_type_configs:
            if not self.get(config_name):
                if self.logger:
                    self.logger.error(f"请设置 {config_name}")
                continue

            value = self[config_name]
            if not isinstance(value, list):
                self[config_name] = [value]
                continue

            if any(value):
                self[config_name] = [i for i in value if i]

        # prevent None value/not list type
        if "admin_group_id" in self:
            if not self["admin_group_id"]:
                self["admin_group_id"] = []

            elif not isinstance(self["admin_group_id"], list):
                self["admin_group_id"] = [self["admin_group_id"]]

        self.save()

    def validate(self):
        """
        Validate config file and prompt user where is wrong,
            including YAML/JSON syntax errors.
        """
        if not self.path.exists():
            return

        # Check YAML or JSON syntax
        try:
            with open(self.path, "r", encoding="UTF-8") as f:
                if self.yaml_format:
                    yaml.load(f)
                else:
                    json.load(f)
        except (YAMLError, json.JSONDecodeError) as e:
            if self.yaml_format:
                mark = getattr(e, "problem_mark", None)
                if mark:
                    error_detail = f"详细信息: {e}"
                    location_info = (
                        f"出错位置：第 {mark.line + 1} 行，第 {mark.column + 1} 列"
                    )
                    msg = (
                        f"YAML 配置文件语法错误: {location_info}" + "\n" + error_detail
                    )
                else:
                    error_detail = f"YAML 配置文件语法错误: {e}"
                    check_hint = "请检查 YAML 文件的缩进和冒号(:)是否正确。"
                    msg = error_detail + "\n" + check_hint
            else:
                lineno = getattr(e, "lineno", None)
                colno = getattr(e, "colno", None)
                if lineno and colno:
                    error_detail = f"详细信息: {e}"
                    location_info = f"出错位置：第 {lineno} 行，第 {colno} 列"
                    msg = (
                        f"JSON 配置文件语法错误: {location_info}" + "\n" + error_detail
                    )
                else:
                    error_detail = f"JSON 配置文件语法错误: {e}"
                    check_hint = "请检查 JSON 文件的格式是否正确。"
                    msg = error_detail + "\n" + check_hint

                if self.logger:
                    self.logger.error(msg)
                else:
                    print(msg)

            return False
