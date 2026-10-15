# -*- coding: utf-8 -*-
"""openid ↔ 虚拟号码 双向映射。

QQ 官方开放平台出于隐私只下发 openid（形如 ``XXXXXX`` 的不透明串），
而 GUGUBot 现有的群号/成员管理、白名单、投票、绑定等逻辑全部按
「数字 QQ 号」书写。为了让既有逻辑零改动地兼容官方平台：

- 每个官方 openid / group_openid 首次出现时分配一个**虚拟数字号码**
  （从 ``100000000000`` 起递增，永不复用）；
- ``to_legacy()`` 把 openid 翻译成虚拟号供下游使用；
- ``to_official()`` 在发送时把虚拟号翻译回 openid；
- 映射持久化到插件数据目录 ``qq_official_ids.json``，重启不丢失。

另外支持把配置文件里**预先填写的真实群号**（permissions.group_ids 等）
与官方 group_openid 绑定：首次收到该群消息时，若该群还没登记过 openid，
就用当前事件的 group_openid 建立绑定（见 ``bind_group_openid``）。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Dict, Optional

# 虚拟号码起始段（12 位数字，避免与真实 QQ 号/QQ 群号冲突）
_BASE_USER = 100_000_000_000
_BASE_GROUP = 200_000_000_000


class QQIDMapper:
    """openid ↔ 虚拟号码 双向映射器（线程安全，磁盘持久化）。"""

    def __init__(self, data_file: Optional[Path] = None, logger=None) -> None:
        self._logger = logger
        self._lock = threading.RLock()
        self._data_file = Path(data_file) if data_file is not None else None

        # openid -> 虚拟号
        self._user_map: Dict[str, str] = {}
        self._group_map: Dict[str, str] = {}
        # 虚拟号 -> openid
        self._user_rev: Dict[str, str] = {}
        self._group_rev: Dict[str, str] = {}

        # 用户配置的真实群号 -> 已绑定的 group_openid
        self._legacy_group_openid: Dict[str, str] = {}
        # group_openid -> 用户配置的真实群号（用于显示）
        self._openid_group_legacy: Dict[str, str] = {}

        self._next_user = _BASE_USER
        self._next_group = _BASE_GROUP

        # 合并写状态
        self._flush_pending = False

        # 运行期临时状态（不持久化，实例隔离）
        self._pending_legacy_groups: list = []
        self._legacy_group_order: list = []

        if self._data_file is not None:
            self._load()

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        # 清理上次崩溃可能残留的 tmp 文件（rename 原子写的中间产物）
        try:
            tmp = self._data_file.with_suffix(".json.tmp")
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        if not self._data_file or not self._data_file.exists():
            return
        try:
            with self._data_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
            with self._lock:
                self._user_map = dict(data.get("user_map", {}))
                self._group_map = dict(data.get("group_map", {}))
                self._user_rev = {v: k for k, v in self._user_map.items()}
                self._group_rev = {v: k for k, v in self._group_map.items()}
                self._legacy_group_openid = dict(data.get("legacy_group_openid", {}))
                self._openid_group_legacy = dict(data.get("openid_group_legacy", {}))
                self._next_user = int(data.get("next_user", _BASE_USER))
                self._next_group = int(data.get("next_group", _BASE_GROUP))
        except Exception as e:
            if self._logger:
                self._logger.warning(f"[QQ官方] 读取 ID 映射文件失败: {e}")

    def _save(self) -> None:
        """合并写：标记脏 + 0.2s 去抖统一落盘。

        场景：@ 原位展开会让一条消息触发多个新 openid 的映射注册
        （@ 三个新人 = 三次全量 JSON dump）。去抖窗口内合并为一次
        写盘；**flush()（插件卸载）与进程退出前已覆盖 _write_file
        保证最终落盘**。窗口极短（0.2s），崩溃丢失风险可忽略。
        """
        with self._lock:
            if self._flush_pending:
                return  # 已有待写盘任务
            self._flush_pending = True

        def _delayed_write():
            with self._lock:
                # 窗口内可能又有新映射，一并写入
                self._flush_pending = False
                self._write_file()

        t = threading.Timer(0.2, _delayed_write)
        t.daemon = True
        t.start()

    def flush(self) -> None:
        """立即落盘（插件卸载/重载时调用；兼并执行待写盘内容）。"""
        with self._lock:
            self._flush_pending = False
            self._write_file()

    def _write_file(self) -> None:
        """真正写盘（须持锁调用；tmp 文件原子替换）。"""
        if not self._data_file:
            return
        try:
            self._data_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "user_map": self._user_map,
                "group_map": self._group_map,
                "legacy_group_openid": self._legacy_group_openid,
                "openid_group_legacy": self._openid_group_legacy,
                "next_user": self._next_user,
                "next_group": self._next_group,
            }
            tmp = self._data_file.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            tmp.replace(self._data_file)
        except Exception as e:
            if self._logger:
                self._logger.warning(f"[QQ官方] 保存 ID 映射文件失败: {e}")

    # ------------------------------------------------------------------ #
    # 用户 openid
    # ------------------------------------------------------------------ #

    def user_legacy_id(self, openid: str) -> str:
        """openid → 虚拟用户号（不存在则新建）。"""
        if not openid:
            return ""
        with self._lock:
            if openid not in self._user_map:
                legacy = str(self._next_user)
                self._next_user += 1
                self._user_map[openid] = legacy
                self._user_rev[legacy] = openid
                self._save()
            return self._user_map[openid]

    def user_openid(self, legacy_id: str) -> str:
        """虚拟用户号 → openid（未知则原样返回）。"""
        with self._lock:
            return self._user_rev.get(str(legacy_id), str(legacy_id))

    def is_legacy_user(self, legacy_id: str) -> bool:
        """虚拟用户号是否已登记。"""
        with self._lock:
            return str(legacy_id) in self._user_rev

    # ------------------------------------------------------------------ #
    # 群 openid
    # ------------------------------------------------------------------ #

    def group_legacy_id(self, openid: str) -> str:
        """group_openid → 虚拟群号（不存在则新建）。"""
        if not openid:
            return ""
        with self._lock:
            if openid not in self._group_map:
                legacy = str(self._next_group)
                self._next_group += 1
                self._group_map[openid] = legacy
                self._group_rev[legacy] = openid
                self._save()
            return self._group_map[openid]

    def group_openid(self, legacy_id: str) -> str:
        """虚拟群号 → group_openid（未知则原样返回）。"""
        with self._lock:
            return self._group_rev.get(str(legacy_id), str(legacy_id))

    def is_legacy_group(self, legacy_id: str) -> bool:
        """虚拟群号是否已登记。"""
        with self._lock:
            return str(legacy_id) in self._group_rev

    # ------------------------------------------------------------------ #
    # 真实群号 ↔ group_openid 绑定（Gensokyo 式配置兼容）
    # ------------------------------------------------------------------ #
    # 官方平台拿不到真实群号，只能拿到 group_openid。为了让用户在配置里
    # 填写的 permissions.group_ids / forward_group_ids 继续可用，约定：
    #
    # 1. 启动时把配置里所有非空群号登记为「待绑定槽位」；
    # 2. 每当收到某群的第一条消息（或任意事件）时，若该群 openid 尚未
    #    与任何真实群号绑定，则按登记顺序把当前 openid 绑定到下一个
    #    未占用的真实群号上（每群只需在群里 @机器人 说句话即可）；
    # 3. 绑定关系持久化；此后 to_legacy()/to_official() 会优先返回
    #    用户配置里的真实群号，实现与旧配置完全兼容。

    def register_legacy_groups(self, group_ids) -> None:
        """登记配置文件中的目标（真实群号 或 openid 原文）。

        - 纯数字 → 视为真实群号，进入待绑定槽位（收到群消息时按序自动绑定 openid）
        - 非数字（如 32 位十六进制 openid）→ 直接自映射登记（填 openid 的场景）
        """
        seen = set()
        with self._lock:
            order = []
            for gid in group_ids or []:
                gid = str(gid).strip()
                if not gid or gid in seen:
                    continue
                seen.add(gid)
                if gid.isdigit():
                    order.append(gid)
                else:
                    # openid 原文：直接作为「自己映射自己」登记，发消息直接可用
                    self._group_map.setdefault(gid, gid)
                    self._group_rev.setdefault(gid, gid)
            # 保持已绑定关系不变，仅补充新槽位
            bound = set(self._legacy_group_openid.values())
            pending = [g for g in order if g not in bound]
            self._pending_legacy_groups = pending
            self._legacy_group_order = order
            self._save()

    def bind_group_openid(self, group_openid: str) -> Optional[str]:
        """把一个 group_openid 绑定到配置中的真实群号。

        允许给「只有虚拟号的旧群」补绑定：早期版本运行时该群可能只
        分配了虚拟号（_group_map 有记录但无真实绑定），只要它还没占
        用配置槽位且待绑定列表非空，即可按顺序补绑。

        Returns
        -------
        Optional[str]
            绑定到的真实群号；无待绑定槽位或已绑定返回 None。
        """
        if not group_openid:
            return None
        with self._lock:
            if group_openid in self._openid_group_legacy:
                # 已绑定真实群号
                return None
            pending = self._pending_legacy_groups or []
            if pending:
                legacy = pending.pop(0)
                self._legacy_group_openid[legacy] = group_openid
                self._openid_group_legacy[group_openid] = legacy
                self._pending_legacy_groups = pending
                self._save()
                if self._logger:
                    self._logger.info(
                        f"[QQ官方] 群 openid {group_openid} 已绑定到配置群号 {legacy}"
                    )
                return legacy
            return None

    def legacy_group_display_id(self, group_openid: str) -> Optional[str]:
        """查询该 openid 绑定的真实群号（仅显示用，不新建映射）。"""
        with self._lock:
            return self._openid_group_legacy.get(group_openid)

    # ------------------------------------------------------------------ #
    # 统一入口
    # ------------------------------------------------------------------ #

    def known_group_openids(self):
        """所有已知群 openid（真实绑定 + 虚拟分配）。"""
        with self._lock:
            return set(self._openid_group_legacy.keys()) | set(self._group_map.keys())

    def to_legacy_group(self, group_openid: str) -> str:
        """group_openid → 下游使用的群号（优先真实群号）。"""
        with self._lock:
            legacy = self._openid_group_legacy.get(group_openid)
            if legacy:
                return legacy
        return self.group_legacy_id(group_openid)

    def group_legacy_display(self, group_openid: str) -> str:
        """该群当前显示号（真实群号或虚拟号），未知返回空。"""
        with self._lock:
            legacy = self._openid_group_legacy.get(group_openid)
            if legacy:
                return legacy
            return self._group_map.get(group_openid, "")

    def to_official_group(self, legacy_id: str) -> str:
        """下游群号（真实/虚拟）→ group_openid。"""
        legacy = str(legacy_id)
        with self._lock:
            if legacy in self._legacy_group_openid:
                return self._legacy_group_openid[legacy]
            if legacy in self._group_rev:
                return self._group_rev[legacy]
        # 兜底：把原始值当 openid 用（用户可能直接填了 openid）
        return legacy

    def to_legacy_user(self, openid: str) -> str:
        """用户 openid → 虚拟用户号。"""
        return self.user_legacy_id(openid)

    def to_official_user(self, legacy_id: str) -> str:
        """虚拟用户号 → 用户 openid。"""
        return self.user_openid(legacy_id)

    def known_legacy_groups(self):
        """所有已知群号（真实绑定 + 虚拟分配）。"""
        with self._lock:
            return set(self._legacy_group_openid.keys()) | set(self._group_rev.keys())

    def clear_pending_slots(self) -> None:
        with self._lock:
            self._pending_legacy_groups = []
