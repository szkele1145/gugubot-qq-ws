# -*- coding: utf-8 -*-
"""PlayerManager.load() 必须真正刷新（v2.2.10 续）。

问题
----
``utils/player_manager.py`` 的 ``load()`` 用来「从磁盘重新加载最新绑定
数据」——``vote.py`` L1092 发起投票前就是这么用的（注释原文：「发起投票
前从磁盘重新加载最新绑定数据，确保资格名单是最新的」）。

但旧实现根本**刷不干净**：

.. code-block:: python

    def load(self) -> None:
        super().load()                     # ← self.update(磁盘数据)：只 update，不 clear
        for name, data in self.items():
            self._players[name] = Player(...)   # ← 只增不减

两个真实缺陷：

* **A. 磁盘上已删除的玩家残留在内存**。``super().load()`` 用 ``update()``
  **不 clear** → 磁盘上被 ``remove_player()`` 删掉的玩家**仍然留在**
  ``self`` 里；``self._players`` 也只增不减 → ``get_player()`` 依然能
  找到他。
* **B. 未保存的内存修改被磁盘旧数据覆盖**。``update()`` 是「用磁盘值
  覆盖同名键」→ 内存里刚改、还没 ``save()`` 的绑定会被**磁盘旧值覆盖**
  → 丢更新。

A 的实际后果：玩家解绑了（``remove_player`` → 落盘），但投票资格名单
里**还有他** → 已解绑用户仍能投票。vote.py 的注释与实际行为相反。

修复
----
``load()`` 改成**先清空再整体替换**（``clear()`` + ``update()``），
``_players`` 同步**重建**（而不是只增不减）。
"""
import importlib.abc
import importlib.machinery
import pathlib
import sys
import tempfile
import types

import pytest


class _A:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return _A()
    def __call__(self, *a, **k): return _A()


class _SM(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        v = _A()
        setattr(self, name, v)
        return v


class _L(importlib.abc.Loader):
    def create_module(self, s): return _SM(s.name)
    def exec_module(self, m): pass


class _F:
    def find_spec(self, f, p=None, t=None):
        if f.startswith("mcdreforged") or f.startswith("packaging"):
            return importlib.machinery.ModuleSpec(f, _L(), is_package=True)


sys.meta_path.insert(0, _F())
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "plugin"))

from gugubot.utils.player_manager import PlayerManager


def _make_pm(tmpdir):
    """造一个能真正落盘的 PlayerManager（不依赖 MCDR server）。"""
    path = pathlib.Path(tmpdir) / "system" / "players.json"

    class _Logger:
        def __getattr__(self, n):
            return lambda *a, **k: None

    class _Server:
        logger = _Logger()

        def get_data_folder(self):
            return str(tmpdir)

    class _BoundSystem:
        config = types.SimpleNamespace(
            get_keys=lambda path, default=None: default
        )

    pm = PlayerManager.get_instance(_Server(), _BoundSystem())
    return pm


# ==================================================== 1. 已删除的玩家不能残留
def test_removed_player_does_not_survive_load():
    """磁盘上删掉的玩家，load() 之后内存里也不能再有。

    旧实现：``super().load()`` 只 ``update()`` 不 ``clear()``，
    ``_players`` 也只增不减 → 已解绑玩家**残留** → 投票资格名单里
    还有他 → 已解绑用户仍能投票。
    """
    with tempfile.TemporaryDirectory() as td:
        pm = _make_pm(td)

        pm.add_player("Steve", player_name="Steve")
        pm.add_player_account("Alex", platform="QQ", account_id="10001")
        assert pm.get_player("Alex") is not None

        # 解绑（从磁盘删除）
        assert pm.remove_player("Alex") is True
        # 磁盘上确实没了
        assert "Alex" not in pm

        # 重新加载（vote.py 发起投票前就是这么做的）
        pm.load()

        assert "Alex" not in pm, (
            "磁盘上已删除的玩家在 load() 后仍残留在 dict 层 —— "
            "super().load() 用的是 update()（不 clear）"
        )
        assert pm.get_player("Alex") is None, (
            "磁盘上已删除的玩家在 load() 后仍能被 get_player() 找到 —— "
            "_players 只增不减 → 已解绑用户仍在投票资格名单里"
        )


# ==================================================== 2. load 之后与磁盘一致
def test_load_rebuilds_players_snapshot():
    """load() 之后 ``_players`` 必须与磁盘内容**一致**（不多不少）。

    旧实现只增不减 → 内存里会攒下磁盘上早已不存在的僵尸 Player 对象
    （``get_all_players()`` 会把它们算进去 → 未绑定检查 / 不活跃检查
    都会遍历到不存在的玩家）。
    """
    with tempfile.TemporaryDirectory() as td:
        pm = _make_pm(td)

        for n in ("A", "B", "C"):
            pm.add_player(n, player_name=n)

        pm.remove_player("B")
        pm.load()

        names = {p.name for p in pm.get_all_players()}
        assert names == {"A", "C"}, (
            f"load() 后的玩家集合是 {names}，应为 {{'A', 'C'}} —— "
            "_players 里攒下了磁盘上已不存在的僵尸条目"
        )


# ==================================================== 3. 关键：单例（不分裂）
def test_player_manager_is_shared_singleton():
    """**同一路径**的 PlayerManager 必须是**同一个实例**。

    旧实现里 ``bound.py`` 与 ``vote.py`` 各自 ``PlayerManager(server,
    self)`` → **两个独立实例**指向同一个 ``players.json`` → 内存副本
    **分裂**：

    * ``bound.py`` 刚给玩家绑上 QQ（内存已改、已落盘）
    * ``vote.py`` 的实例内存仍是**旧快照** → 发起投票时用它算
      「资格名单」→ 用户明明刚绑定，却被判「**无投票资格**」

    这正是 vote.py 里那句注释的反面：

        # 发起投票前从磁盘重新加载最新绑定数据，确保资格名单是最新的。
        self.player_manager.load()

    —— 靠「每次发起投票都重新 load」来绕开分裂，但 load 只能保证
    「磁盘状态」，仍看不到**另一个实例内存里尚未落盘**的修改；而两个
    实例的 load 时刻不同步，资格名单依然可能是旧的。
    """
    with tempfile.TemporaryDirectory() as td:
        pm1 = _make_pm(td)
        pm2 = _make_pm(td)
        assert pm1 is pm2, (
            "同一路径的 PlayerManager 是两个不同实例 → 内存副本分裂 → "
            "bound.py 的绑定改动对 vote.py 的资格名单不可见"
        )


# ==================================================== 4. 单例后修改立即可见
def test_binding_change_visible_to_other_consumer():
    """绑定改动后，**另一个消费者**立刻能看见（无需各自 load）。

    这是 #73 的核心断言：bound 系统加完绑定，vote 系统算资格名单时
    立即可见 —— 不再依赖「各自 load 的时刻是否同步」。

    旧实现（两个独立实例）下：bound 的实例内存已改、vote 的实例内存
    还是旧快照 → vote 查不到刚绑定的玩家 → 用户明明刚绑定却被判
    「无投票资格」。
    """
    with tempfile.TemporaryDirectory() as td:
        # 两个「消费者」：bound 系统与 vote 系统
        bound_pm = _make_pm(td)
        vote_pm = _make_pm(td)

        # bound 系统：玩家绑定 QQ
        bound_pm.add_player_account("Alex", platform="QQ", account_id="10001")

        # vote 系统算资格名单：查这个 QQ 号绑没绑定
        player = vote_pm.get_player("10001", platform="QQ")
        assert player is not None, (
            "绑定刚做完，另一个消费者却查不到 → 内存副本分裂 → "
            "用户明明刚绑定却被判「无投票资格」"
        )
        assert "Alex" in player.java_name


# ==================================================== 5. load 不得清掉未落盘的改动
def test_load_preserves_unsaved_binding_changes():
    """``load()``（整份替换）不得把**尚未落盘**的绑定改动清掉。

    单例后 ``load()`` 是「把内存重置成磁盘内容」—— 如果磁盘上还是旧的
    （另一个进程/上一次运行留下的），就会把内存里刚做的绑定**丢掉**。

    因此：绑定流程必须**改完立刻 save**（内存与磁盘保持一致），
    且 ``load()`` 只能在「确保磁盘是最新的」之后调用。
    """
    with tempfile.TemporaryDirectory() as td:
        pm = _make_pm(td)
        pm.add_player("Steve", player_name="Steve")

        # 模拟「刚绑定、已落盘」
        pm.add_player_account("Alex", platform="QQ", account_id="10001")

        # 磁盘与内存一致 → load 之后数据仍在
        pm.load()
        player = pm.get_player("10001", platform="QQ")
        assert player is not None, "load() 把已落盘的绑定弄丢了"
        assert "Alex" in player.java_name


# ==================================================== 6. 单例不顶替 bound_system
def test_singleton_keeps_first_bound_system():
    """后构造者（vote）**不得**顶替先构造者（bound）的 ``bound_system``。

    ``PlayerManager`` 用 ``self.bound_system.config`` 读全局配置
    （``connector.QQ.source_name`` 等）。两个系统的 ``config`` 恰好是
    同一个 BotConfig，所以顶替**当前**无害 —— 但语义上 ``bound_system``
    就该是绑定系统；一旦将来它上面多了只有 bound 才有的东西，顶替就会
    变成静默 AttributeError。

    这条测试把「保留第一个」的约定钉住。
    """
    with tempfile.TemporaryDirectory() as td:

        class _Logger:
            def __getattr__(self, n):
                return lambda *a, **k: None

        class _Server:
            logger = _Logger()

            def get_data_folder(self):
                return str(td)

        bound_marker = object()
        vote_marker = object()

        first = PlayerManager.get_instance(_Server(), bound_marker)
        second = PlayerManager.get_instance(_Server(), vote_marker)

        assert first is second, "同一路径必须返回同一实例"
        assert second.bound_system is bound_marker, (
            "后构造者顶替了 bound_system —— 应保留第一个（真正的绑定系统）"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
