# -*- coding: utf-8 -*-
"""Basic configuration class for loading and saving JSON/YAML files with auto-saving."""

import json
import os
from pathlib import Path
from typing import Any, List, Optional

from ruamel.yaml import YAML

yaml = YAML()
yaml.preserve_quotes = True


class BasicConfig(dict):
    """
    Basic configuration class for loading and saving JSON/YAML files with auto-saving.
    Be careful, changing the mutable value will not be saved automatically.
    """

    def __init__(
        self,
        path: str = "./config.json",
        default_content: dict = None,
        yaml_format: bool = False,
    ) -> None:
        super().__init__()
        self.yaml_format = yaml_format
        self.path = Path(path).with_suffix(".yml" if yaml_format else ".json")
        self.default_content = default_content or {}
        self.load()

    def load(self) -> None:
        """Load data from file."""
        if self.path.is_file() and self.path.stat().st_size > 0:
            with self.path.open("r", encoding="UTF-8") as f:
                self.update(yaml.load(f) if self.yaml_format else json.load(f))
        else:
            self.update(self.default_content)
            self.save()

    def save(self) -> None:
        """Save data to file（原子写）。

        先写临时文件再 ``os.replace``：直接 ``open("w")`` 覆盖写的话，
        进程在写入中途崩溃/断电会留下**半截文件**——玩家绑定、待办、
        白名单等所有基于 BasicConfig 的持久化数据都会损坏（旧文件已被
        截断，无法恢复）。``os.replace`` 在同一文件系统上是原子操作，
        要么旧文件完整保留、要么新文件完整生效。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp_path.open("w", encoding="UTF-8") as f:
            if self.yaml_format:
                yaml.dump(dict(self), f)
            else:
                json.dump(dict(self), f, ensure_ascii=False)
        os.replace(tmp_path, self.path)

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.save()

    def __delitem__(self, key):
        super().__delitem__(key)
        self.save()

    def get_keys(self, key: List[str], default: Optional[Any] = None) -> Any:
        """
        Get value from config by key path.

        Parameters
        ----------
        key : List[str]
            A list of keys to get the value from config
        default : Any, optional
            Default value if the key is not found, default None

        Returns
        -------
        Any
            Value from config by key path

        Examples
        --------
        >>> config = BasicConfig()
        >>> config.get_keys(["connector", "QQ", "permissions", "admin_ids"])
        [123456, 654321]
        >>> config.get_keys(["key", "not", "exists"], [123, 321])
        [123, 321]
        """
        result = self
        for k in key[:-1]:
            result = result.get(k, {})
        return result.get(key[-1], default)
