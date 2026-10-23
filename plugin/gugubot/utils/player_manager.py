# -*- coding: utf-8 -*-
# +----------------------------------------------------------------------+
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from mcdreforged.api.types import PluginServerInterface

from gugubot.config import BasicConfig, BotConfig


# +----------------------------------------------------------------------+


@dataclass
class Player:
    """玩家数据类"""

    name: str  # 玩家主名称
    java_name: List[str] = field(default_factory=list)  # Java版名称列表
    bedrock_name: List[str] = field(default_factory=list)  # 基岩版名称列表
    accounts: Dict[str, List[str]] = field(default_factory=dict)  # 账号映射表
    properties: Dict[str, Any] = field(default_factory=dict)  # 自定义属性

    def add_name(self, player_name: str, is_bedrock: bool = False) -> None:
        """添加名称"""
        if is_bedrock and player_name not in self.bedrock_name:
            self.bedrock_name.append(player_name)
        elif player_name not in self.java_name:
            self.java_name.append(player_name)

    def remove_name(self, player_name: str, is_bedrock: bool = False) -> None:
        """移除名称（回滚用）"""
        if is_bedrock:
            if player_name in self.bedrock_name:
                self.bedrock_name.remove(player_name)
        else:
            if player_name in self.java_name:
                self.java_name.remove(player_name)

    def add_account(self, platform: str, account_id: str) -> None:
        """添加关联账号"""
        account_id = str(account_id)
        if platform not in self.accounts:
            self.accounts[platform] = []
        if account_id not in self.accounts[platform]:
            self.accounts[platform].append(account_id)

    def remove_account(self, platform: str, account_id: str) -> None:
        """移除关联账号（回滚用）"""
        account_id = str(account_id)
        if platform in self.accounts and account_id in self.accounts[platform]:
            self.accounts[platform].remove(account_id)
            if not self.accounts[platform]:
                del self.accounts[platform]

    def set_property(self, key: str, value: Any) -> None:
        """设置自定义属性"""
        self.properties[key] = value

    def get_property(self, key: str, default: Any = None) -> Any:
        """获取自定义属性"""
        return self.properties.get(key, default)


class PlayerManager(BasicConfig):
    """玩家管理器。

    **同一路径只允许存在一个实例**（按解析后的绝对路径做单例）——
    ``bound.py`` 与 ``vote.py`` 都会构造它，而它们指向**同一个**
    ``players.json``。两个独立实例 → 两份内存副本**分裂**：

    * ``bound.py`` 刚给玩家绑上 QQ（内存已改、已落盘）
    * ``vote.py`` 的实例内存仍是**旧快照** → 发起投票时用它算
      「资格名单」→ 用户明明刚绑定，却被判「**无投票资格**」

    旧实现靠「每次发起投票都 ``load()`` 一次」来绕开分裂，但那只能
    保证「磁盘状态」，看不到**另一个实例内存里尚未落盘**的修改，而且
    两个实例的 load 时刻不同步 → 资格名单依然可能是旧的。

    单例后：绑定改动对所有消费者**立即可见**，且 ``load()`` 的调用
    也可以收敛（``vote.py`` 里那两处 ``load()`` 变成纯保险）。
    """

    # 路径 -> 实例（进程内共享）
    _instances: Dict[str, "PlayerManager"] = {}
    _instances_lock = threading.Lock()

    @classmethod
    def get_instance(
        cls, server: PluginServerInterface, bound_system
    ) -> "PlayerManager":
        """取（并按需创建）该数据目录对应的**唯一** PlayerManager。

        同一路径只允许存在一个实例 —— ``bound.py`` 与 ``vote.py`` 都要用
        玩家绑定数据，而它们指向**同一个** ``players.json``。两个独立
        实例 → 两份内存副本**分裂**：

        * ``bound.py`` 刚给玩家绑上 QQ（内存已改、已落盘）
        * ``vote.py`` 的实例内存仍是**旧快照** → 发起投票时用它算
          「资格名单」→ 用户明明刚绑定，却被判「**无投票资格**」

        旧实现靠「每次发起投票都 ``load()`` 一次」来绕开分裂，但那只能
        保证「磁盘状态」，看不到**另一个实例内存里尚未落盘**的修改，而且
        两个实例的 load 时刻不同步 → 资格名单依然可能是旧的。

        单例后：绑定改动对所有消费者**立即可见**。
        """
        data_path = Path(server.get_data_folder()) / "system" / "players.json"
        key = str(data_path.absolute())
        with cls._instances_lock:
            inst = cls._instances.get(key)
            if inst is None:
                inst = cls(server, bound_system)
                cls._instances[key] = inst
            else:
                # 已存在 → 复用，但**保留第一个构造者**的 bound_system：
                # 语义上它才是「绑定系统」（vote 只是借用来读全局 config）。
                # 覆盖会让 vote 顶替 bound，将来若 bound_system 上新增了
                # 只有 bound 才有的东西（如 system_manager 的特定用法），
                # 就会静默 AttributeError。
                #
                # 只刷新 server / logger（热重载时 server 对象可能换新）。
                inst.server = server
                inst.logger = server.logger
            return inst

    def __init__(self, server: PluginServerInterface, bound_system):
        self.server = server
        self.logger = server.logger
        self.bound_system = bound_system
        self._players: Dict[str, Player] = {}  # 玩家数据字典
        # 管理群成员列表缓存：(时间戳, 成员ID集合)
        # is_admin() 每次调用都要遍历所有管理群拉成员列表——MC 侧每条聊天
        # 消息都可能触发一次，放大后是 N 条消息 × M 个管理群次请求。
        # 成员列表在短时间内不会变，缓存一小段时间即可。
        self._admin_member_cache: Optional[tuple] = None
        data_path = Path(server.get_data_folder()) / "system" / "players.json"
        super().__init__(path=str(data_path), default_content={}, yaml_format=True)

    def load(self) -> None:
        """加载玩家数据（**整份替换**，不是合并）。

        旧实现用 ``super().load()``（``update()`` 语义、**不 clear**）
        + ``_players`` 只增不减 → **刷不干净**：

        * 磁盘上已 ``remove_player()`` 删掉的玩家**残留在内存** →
          ``get_player()`` 依然能找到他 → **已解绑用户仍在投票资格
          名单里**；
        * 内存里**尚未落盘**的修改会被磁盘旧值**覆盖**（丢更新）；
        * ``_players`` 里攒下磁盘上早已不存在的**僵尸 Player** →
          ``get_all_players()`` 会把它们算进去 → 未绑定检查 / 不活跃
          检查会遍历到不存在的玩家。

        现在改成：先把 dict 层**整体替换**成磁盘内容，再**重建**
        ``_players`` 快照（不多不少）。
        """
        # 先把旧内容清掉：BasicConfig.load() 是 update() 语义，会把磁盘
        # 上已删除的键留在内存里
        self.clear()
        super().load()
        # 重建 Player 快照（先清空，避免僵尸条目残留）
        self._players.clear()
        for name, data in self.items():
            if not isinstance(data, dict):
                continue
            self._players[name] = Player(
                name=name,
                java_name=data.get("java_name", []),
                bedrock_name=data.get("bedrock_name", []),
                accounts=data.get("accounts", {}),
                properties=data.get("properties", {}),
            )

    def save(self) -> None:
        """保存玩家数据"""
        temp = {}
        for name, player in self._players.items():
            temp[name] = {
                "java_name": player.java_name,
                "bedrock_name": player.bedrock_name,
                "accounts": player.accounts,
                "properties": player.properties,
            }
        self.clear()
        self.update(temp)
        super().save()

    def add_player(
        self, name: str, player_name: str = None, is_bedrock: bool = False
    ) -> Player:
        """添加新玩家

        Parameters
        ----------
        name : str
            玩家名称
        player_name : str
            玩家名称
        is_bedrock : bool
            是否为基岩版玩家

        Returns
        -------
        Player
            这个玩家对应的 Player 实例
        """
        if name not in self._players:
            self._players[name] = Player(name=name)
        if player_name:
            self._players[name].add_name(player_name, is_bedrock)
        self.save()
        return self._players[name]

    def remove_player(self, name: str) -> bool:
        """删除玩家"""
        if name in self._players:
            del self._players[name]
            self.save()
            return True
        return False

    def get_player(
        self, identifier: str, platform: str = None, name_only: bool = False
    ) -> Optional[Player]:
        """
        通用的玩家查找函数

        Parameters
        ----------
        identifier : str
            玩家标识符（可以是名称、UUID或账号ID）
        platform : str, optional
            要查找的平台, 不填或填 ``None`` 在全平台查找
        name_only : bool, optional
            是否只用名称查找, 默认 ``False``
        Returns
        -------
        Optional[Player]
            找到的玩家对象，如果未找到则返回 None

        Examples
        --------
        >>> # 通过名称查找
        >>> player = manager.get_player("Steve")

        >>> # 通过UUID查找（Java版或基岩版）
        >>> player = manager.get_player("uuid-xxxxx")

        >>> # 通过关联账号查找
        >>> player = manager.get_player("discord-id")
        """
        # 1. 通过名称查找
        if identifier in self._players:
            return self._players[identifier]

        if name_only:
            return None

        # platform 候选键：QQ 平台的绑定键是用户自定义的 source_name，
        # 历史数据可能存在默认 "QQ" 键下——只精确匹配 platform 的话，
        # 用户改名后查不到自己改名前的绑定（系统误判「没绑过」，
        # 允许重复绑定、绑定上限算错、管理员判定失效）。
        candidate_platforms = self._platform_candidates(platform)

        for player in self._players.values():
            # 2. 通过UUID查找
            if identifier in player.java_name or identifier in player.bedrock_name:
                return player

            # 3. 通过关联账号查找
            for platform_name, account_ids in player.accounts.items():
                if candidate_platforms and platform_name not in candidate_platforms:
                    continue
                if identifier in account_ids or str(identifier) in [str(a) for a in account_ids]:
                    return player

        return None

    def _platform_candidates(self, platform: str) -> Set[str]:
        """给定平台键，返回实际可能存放绑定数据的全部候选键。

        QQ 连接器的绑定键是 ``connector.QQ.source_name``（用户可自定义，
        默认 "QQ"），而配置节名恒为 "QQ"。用户改过 source_name 之后，
        旧绑定仍留在 "QQ" 键下 → 查询时必须两个键都查，否则会误判
        「该玩家没有绑定」。其他平台只有单一键。调用方传 "QQ"（节名）
        还是 source_name，都返回同一组候选键。
        """
        if not platform:
            return set()
        keys = {str(platform)}
        try:
            source_name = self.bound_system.config.get_keys(
                ["connector", "QQ", "source_name"], "QQ"
            )
            if source_name:
                source_name = str(source_name)
                # 传入的 platform 是 QQ 的两个候选键之一 → 返回两个键
                if platform == "QQ" or platform == source_name:
                    keys.add("QQ")
                    keys.add(source_name)
        except Exception:
            pass
        return keys

    def get_all_players(self) -> List[Player]:
        """获取所有玩家列表"""
        return list(self._players.values())

    def add_player_account(
        self, identifier: str, platform: str, account_id: str, is_bedrock: bool = False
    ) -> bool:
        """添加玩家关联账号"""
        player = self.get_player(identifier) or self.get_player(
            account_id, platform=platform
        )
        if not player:
            player = self.add_player(
                identifier, player_name=identifier, is_bedrock=is_bedrock
            )
        player.add_account(platform, account_id)
        self.save()
        return True

    def is_name_bound_by_other_user(
        self, player_name: str, current_user_id: str, source: str
    ) -> bool:
        """检查玩家名是否已被其他用户绑定

        Parameters
        ----------
        player_name : str
            要检查的玩家名
        current_user_id : str
            当前用户ID
        source : str
            当前用户来源

        Returns
        -------
        bool
            如果已被其他用户绑定则返回True
        """
        for player in self._players.values():
            player_in_name = (
                player_name in player.java_name or player_name in player.bedrock_name
            )
            not_current_user = current_user_id not in player.accounts.get(source, [])
            platform_not_bound = source in player.accounts

            if player_in_name and not_current_user and platform_not_bound:
                return True

        return False

    # 管理群成员列表缓存有效期（秒）。管理群进出人不会高频变化，
    # 30s 内的短暂陈旧完全可接受（最坏情形：新管理员加群后 30s 内
    # 权限未生效）。
    _ADMIN_MEMBER_CACHE_TTL = 30.0

    async def is_admin(self, sender_id: str) -> bool:
        """检查是否是管理员"""
        player = self.get_player(sender_id)

        if not player:
            return False

        config: BotConfig = self.bound_system.config
        connectors = config.get_keys(["connector"], {})

        import time as _time

        now = _time.monotonic()
        cached = self._admin_member_cache
        cache_valid = (
            cached is not None
            and (now - cached[0]) < self._ADMIN_MEMBER_CACHE_TTL
        )

        for source, connector_config in connectors.items():
            permissions = connector_config.get("permissions", {})
            admin_ids = permissions.get("admin_ids", [])
            admin_group_ids = permissions.get("admin_group_ids", [])

            # 注意：source 是 connector 下的**配置节名**（如 "QQ"，代码固定，
            # 用户改不了），而绑定数据 player.accounts 的平台键是
            # **source_name**（用户可自定义，默认也是 "QQ"）。两者只在
            # source_name 未改名时相等——旧实现拿 source 与 source_name 比较，
            # 改名后恒为 False，QQ 管理员分支整个被跳过 → 管理员判定失效
            # （改回默认名才恢复）。
            if source == "QQ":
                # 连接器注册名 = source_name（用户可自定义，默认 "QQ"），
                # 不是配置节名 "QQ"——硬编码会在改名后取到 None →
                # None.bot 抛 AttributeError（且 try 从下一行才开始，
                # 这一行不在保护范围内）→ 管理员判定整个抛异常。
                qq_source_name = (
                    self.bound_system.config.get_keys(
                        ["connector", "QQ", "source_name"], "QQ"
                    )
                    or "QQ"
                )
                qq_connector = self.bound_system.system_manager.connector_manager.get_connector(
                    qq_source_name
                )
                if qq_connector is None:
                    # 连接器缺失（改名后注册名对不上 / 未启动）→ 跳过「管理群
                    # 成员」这一路判定，但**不能**直接 return False：配置里
                    # 显式写的 admin_ids 仍应生效，否则管理员权限静默失效。
                    admin_group_ids = []
                    admin_group_member_ids = set()
                    member_list_supported = True
                elif cache_valid:
                    # 命中缓存：免掉对每个管理群的成员列表请求
                    admin_group_member_ids = cached[1]
                    member_list_supported = True
                else:
                    admin_group_member_ids = set()
                    member_list_supported = True
                    for admin_group_id in admin_group_ids:
                        if not admin_group_id:
                            continue
                        try:
                            admin_group_members = await qq_connector.bot.get_group_member_list(
                                group_id=admin_group_id
                            )

                            if (
                                admin_group_members
                                and admin_group_members.get("status") == "ok"
                            ):
                                # 平台不支持成员列表（QQ 官方直连）→ data 为
                                # None，与"该群没有成员"含义完全不同：此时无法
                                # 识别管理群成员，保留空集合而不是把 None 当
                                # 列表迭代
                                members = admin_group_members.get("data")
                                if members is None:
                                    member_list_supported = False
                                    continue
                                admin_group_member_ids.update(
                                    [
                                        str(member.get("user_id", ""))
                                        for member in members
                                    ]
                                )
                        except Exception as e:
                            self.logger.error(
                                f"获取管理群 {admin_group_id} 成员列表失败: {e}"
                            )
                            continue
                    if not member_list_supported:
                        self.logger.warning(
                            "[绑定系统] 当前 QQ 平台不支持获取群成员列表，"
                            "管理群成员将无法被识别为管理员"
                        )
                    # 仅在平台确实支持时缓存（官方直连模式下列表恒不可得，
                    # 缓存与否无意义）；失败结果（空集）同样缓存——避免
                    # WS 断连期间每条消息都打一遍 error 刷屏
                    if member_list_supported:
                        self._admin_member_cache = (now, admin_group_member_ids)
                admin_group_ids = list(admin_group_member_ids)

            # 绑定数据的平台键是 source_name（当前配置键），历史数据可能在
            # "QQ" 键下（用户改名前的旧绑定）——两个候选键都要查，
            # 与 BoundSystem._qq_platform_keys() 同源。
            candidate_keys = [source]
            if source == "QQ":
                qq_source_name = config.get_keys(
                    ["connector", "QQ", "source_name"], "QQ"
                )
                if qq_source_name and str(qq_source_name) not in candidate_keys:
                    candidate_keys.insert(0, str(qq_source_name))
            platform_accounts = []
            for pk in candidate_keys:
                platform_accounts.extend(player.accounts.get(pk, []))
            for account_id in platform_accounts:
                if str(account_id) in [str(i) for i in admin_ids + admin_group_ids]:
                    return True

        return False
