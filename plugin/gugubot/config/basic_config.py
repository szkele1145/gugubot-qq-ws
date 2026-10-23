# -*- coding: utf-8 -*-
"""Basic configuration class for loading and saving JSON/YAML files with auto-saving."""

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, List, Optional

from ruamel.yaml import YAML

yaml = YAML()
yaml.preserve_quotes = True

# os.replace 撞上并发读者（Windows：open("r") 不允许并发 rename）时的
# 重试策略。读者（MCDR 热重载 / 外部编辑器 / 备份脚本）打开文件的窗口
# 通常只有几毫秒到几秒，退避累计约 10 秒的窗口足够覆盖。
#
# **重试到底仍失败时直接抛错，保留旧文件** —— 不降级到「先删目标再
# rename」（那一步在 rename 也失败时会把旧文件一起删掉，把「保存失败」
# 升级成「数据丢失」），也不降级到「直接写目标文件」（会牺牲原子性，
# 读者可能读到半截内容）。
_REPLACE_RETRIES = 12
_REPLACE_BACKOFF = 0.02
_REPLACE_BACKOFF_MAX = 0.5

# 读文件撞上并发写入（Windows：writer replace 期间 open 可能失败）时
# 的重试策略，理由同上。
_READ_RETRIES = 12
_READ_BACKOFF = 0.02
_READ_BACKOFF_MAX = 0.5


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
        # 写盘锁：save() 可能被**多个线程**并发调用（事件循环线程 + 线程池
        # 里的 executor 线程，如 inactive_check 的 HTTP 查询回写缓存）。
        # 不加锁时的具体故障：
        #   * 两个线程同时用**同一个**临时文件名 → A 正在写的内容被 B
        #     截断 → os.replace 搬走**半截文件** → 配置损坏（且不可恢复）
        #   * json.dump(dict(self)) 在 dict 被另一个线程修改时抛
        #     "dictionary changed size during iteration"
        #
        # 懒初始化（_get_save_lock）：有测试/调用方用 ``__new__`` 绕过
        # ``__init__`` 构造实例（如 test_config_reload 的桩对象），属性
        # 若只在 __init__ 里赋值，这类实例一 save 就 AttributeError。
        self._save_lock = threading.RLock()
        self.load()

    def _get_save_lock(self) -> threading.RLock:
        """取写盘锁；绕过 ``__init__`` 的实例也能懒创建（线程安全）。"""
        lock = self.__dict__.get("_save_lock")
        if lock is None:
            lock = threading.RLock()
            # 可能与其它线程竞争创建 —— RLock 覆盖谁创建的无所谓，
            # 只要「同一实例同一把锁」即可；用 setdefault 原子写入
            self.__dict__["_save_lock"] = lock
        return lock

    def load(self) -> None:
        """Load data from file."""
        if self.path.is_file() and self.path.stat().st_size > 0:
            with self._open_for_read() as f:
                self.update(yaml.load(f) if self.yaml_format else json.load(f))
        else:
            self.update(self.default_content)
            self.save()

    def _open_for_read(self):
        """打开文件用于读；撞上并发写入（Windows）时短暂退避重试。

        Windows 上 writer 执行 ``os.replace`` 的瞬间，reader 的
        ``open("r")`` 可能直接 ``PermissionError``（文件被换掉的那一
        刹那，旧文件已不可访问）。MCDR 热重载正好读的就是这个配置
        文件 → 撞上就报「重载失败」。
        """
        last_err = None
        delay = _READ_BACKOFF
        for _ in range(_READ_RETRIES):
            try:
                return self.path.open("r", encoding="UTF-8")
            except PermissionError as e:
                last_err = e
                time.sleep(delay)
                delay = min(delay * 2, _READ_BACKOFF_MAX)
        if last_err is not None:
            raise last_err
        raise OSError("unreachable")

    def save(self) -> None:
        """Save data to file（原子写 + 加锁 + 唯一临时文件名）。

        先写临时文件再 ``os.replace``：直接 ``open("w")`` 覆盖写的话，
        进程在写入中途崩溃/断电会留下**半截文件**——玩家绑定、待办、
        白名单等所有基于 BasicConfig 的持久化数据都会损坏（旧文件已被
        截断，无法恢复）。``os.replace`` 在同一文件系统上是原子操作，
        要么旧文件完整保留、要么新文件完整生效。

        临时文件名**必须唯一**：``save()`` 可能被多个线程并发调用
        （事件循环线程 + 线程池线程），共用固定名字时两个线程会写到
        同一个文件 → 互相截断 → ``os.replace`` 可能搬走**半截文件**
        → 配置损坏。文件名里带上 pid + 线程 id + 自增序号即可。

        Windows 上若目标文件正被外部读者（MCDR 热重载 / 编辑器 / 备份
        脚本）打开，``os.replace`` 会 ``PermissionError``。此时依次降级：
        退避重试 → 删除目标再 rename → 直接写目标文件。**保存必须成功**
        ——「有人正在读配置」不该导致绑定数据写不进去。
        """
        with self._get_save_lock():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._make_tmp_path()
            try:
                with tmp_path.open("w", encoding="UTF-8") as f:
                    if self.yaml_format:
                        yaml.dump(dict(self), f)
                    else:
                        json.dump(dict(self), f, ensure_ascii=False)
                self._install_tmp(tmp_path)
            except Exception:
                # 失败时别把临时文件留在盘上（下次 save 会再产生一个，
                # 攒一堆垃圾文件）
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except OSError:
                    pass
                raise

    def _install_tmp(self, tmp_path: Path) -> None:
        """把临时文件装到位（原子替换），Windows 被占用时退避重试。

        降级链：

        1. ``os.replace``（原子，优先）
        2. 退避重试（读者窗口通常只有几毫秒）

        **不做「先删目标文件再 rename」的降级**：那一步在 rename 仍失败
        时（tmp 被杀软锁住 / 跨卷 / 目标目录权限变化）会**把旧文件一起
        删掉** → 配置凭空消失，「保存失败」被升级成「数据丢失」。占用
        是瞬时的，退避重试足够；真失败就抛错并**保留旧文件**。

        **也不降级到「直接写目标文件」**：那会牺牲原子性 —— 目标文件
        写到一半时崩机会留下**半截文件**（原子替换最坏是保留旧文件），
        而读者恰好在写入窗口里读就会读到半截内容 → JSON/YAML 解析失败。
        **「保存失败」比「文件损坏」安全得多** —— 前者用户会重试并看到
        报错，后者数据静默丢失。
        """
        try:
            self._atomic_replace(tmp_path, self.path)
            return
        except (PermissionError, OSError) as e:
            raise OSError(
                f"保存 {self.path.name} 失败（目标文件可能被其他程序占用）: {e}"
            ) from e

    @staticmethod
    def _atomic_replace(src: Path, dst: Path) -> None:
        """``os.replace`` + Windows 并发读者兼容重试。

        Windows 上 ``open("r")`` 打开的文件**不允许**被 rename —— 若
        MCDR 热重载 / 外部编辑器 / 备份脚本恰好正在读配置，保存的
        ``os.replace`` 会直接 ``PermissionError`` 失败（Linux 无此问题，
        rename 与打开的 fd 并存）。读者打开文件的窗口通常只有几毫秒，
        短暂退避后重试即可成功；重试仍失败则把最后一次错误原样抛出。
        """
        last_err = None
        delay = _REPLACE_BACKOFF
        for _ in range(_REPLACE_RETRIES):
            try:
                os.replace(src, dst)
                return
            except PermissionError as e:
                last_err = e
                time.sleep(delay)
                delay = min(delay * 2, _REPLACE_BACKOFF_MAX)
        if last_err is not None:
            raise last_err

    def _make_tmp_path(self) -> Path:
        """生成一个**本次调用独占**的临时文件路径。"""
        self._tmp_counter = getattr(self, "_tmp_counter", 0) + 1
        tmp_name = (
            f"{self.path.name}.{os.getpid()}.{threading.get_ident()}"
            f".{self._tmp_counter}.tmp"
        )
        return self.path.with_name(tmp_name)

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
