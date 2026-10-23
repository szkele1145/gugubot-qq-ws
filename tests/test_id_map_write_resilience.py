# -*- coding: utf-8 -*-
"""QQIDMapper 的落盘必须抗「文件被占用」（v2.2.10 续，76）。

问题
----
``connector/qq_official/id_map.py`` 的 ``_write_file`` 用固定 tmp 名
``<data>.json.tmp`` + ``tmp.replace(target)``，失败时**只打一行 warning
就放弃**，没有任何重试：

.. code-block:: python

    tmp = self._data_file.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ...)
    tmp.replace(self._data_file)      # ← Windows 撞读者 → PermissionError
    except Exception as e:
        logger.warning(...)           # ← 吞掉，不重试

**Windows 的文件共享模型**：只要还有进程/线程开着这个文件（编辑器、
备份脚本、杀软扫描、另一个插件实例），``os.replace`` 就会抛
``PermissionError``。Linux 上不存在这个问题，所以本地开发很难发现。

**后果链**（比"少写一个文件"严重得多）：

1. 这次映射变更**没落盘**，但内存里的 ``_next_user`` 已经递增了；
2. 重启后从旧文件读回**旧的** ``_next_user``；
3. 新 openid 被分到**已经分配过的**虚拟号 → ``_repair_duplicate_ids_locked``
   自愈时把后来者改成新号；
4. 下游（白名单 / 绑定 / 投票 / 待办）里存的还是**旧虚拟号** →
   这些人的绑定全部指向"另一个人"或直接失效，且**只在下次重启后
   才暴露**，日志里只有一行 warning。

修复
----
1. ``_write_file`` 加**递增退避重试**（Windows 上撞读者是瞬时的）；
2. 临时文件名**唯一化**（pid + 线程 id + 自增序号）—— 固定名在多个
   实例/进程并发写时会互相截断（与 #72 同一类缺陷）；
3. 重试仍失败时**保留「待写盘」标记**，让后续 ``_save()`` 还能再试，
   而不是静默丢弃。

测试手法
--------
把 ``Path.replace`` 打桩成「第一次抛 PermissionError，之后正常」——
这精确复现 Windows 的瞬时占用。判定看**磁盘上是否真的出现了新数据**，
不看日志、不看返回值（日志说什么不重要，数据在不在才重要）。
"""
import importlib.util
import json
import pathlib
import sys

import pytest

_MOD_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "plugin" / "gugubot" / "connector" / "qq_official" / "id_map.py"
)


def _load_id_map_module():
    """按文件路径加载 id_map，绕开 ``gugubot.connector`` 包初始化。

    直接 ``import gugubot.connector.qq_official.id_map`` 会执行
    ``gugubot/__init__.py``（MCDR 插件入口），需要 mcdreforged，会把整个
    测试环境拖进桩里。id_map 只依赖标准库，按路径加载最干净。
    """
    spec = importlib.util.spec_from_file_location("_id_map_under_test", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_id_map_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


ID_MAP = _load_id_map_module()
QQIDMapper = ID_MAP.QQIDMapper


# ==================================================== 1. 瞬时占用必须重试成功
def test_write_file_retries_transient_permission_error(tmp_path, monkeypatch):
    """``replace`` 第一次抛 PermissionError 时，重试必须让它最终落盘。

    Windows 上文件被别的进程短暂打开是**常态**（编辑器/备份/杀软）。
    旧实现一次失败就放弃 → 映射丢失（连带 ``_next_user`` 回退）。
    """
    data_file = tmp_path / "qq_official_ids.json"
    mapper = QQIDMapper(data_file=data_file)
    mapper.user_legacy_id("openid-first")          # 建立初始文件
    mapper.flush()   # _save() 是 0.2s 去抖，读盘前必须先 flush

    real_replace = pathlib.Path.replace
    state = {"n": 0}

    def flaky_replace(self, target):
        state["n"] += 1
        if state["n"] == 1:
            raise PermissionError("[WinError 32] 文件正被另一进程使用")
        return real_replace(self, target)

    monkeypatch.setattr(pathlib.Path, "replace", flaky_replace)

    mapper.user_legacy_id("openid-second")
    mapper.flush()

    assert state["n"] >= 2, (
        "replace 只被调用了一次 —— 没有重试，遇到 Windows 的文件占用就"
        "直接放弃了落盘"
    )

    on_disk = json.loads(data_file.read_text(encoding="utf-8"))
    assert "openid-second" in on_disk["user_map"], (
        "重试没有生效：新映射没落盘 → 重启后 _next_user 回退 → 新 openid "
        "会复用已分配的虚拟号 → 下游绑定失效"
    )


# ==================================================== 2. 彻底失败不损坏旧文件
def test_write_failure_does_not_corrupt_existing_file(tmp_path, monkeypatch):
    """重试全部失败时，**旧文件必须完好**（不能变成半截 / 空文件）。

    原子写的核心价值：宁可这次保存失败，也不能让读者看到半截内容。
    """
    data_file = tmp_path / "qq_official_ids.json"
    mapper = QQIDMapper(data_file=data_file)
    legacy_before = mapper.user_legacy_id("openid-keep")
    mapper.flush()   # _save() 是 0.2s 去抖，读盘前必须先 flush
    before = json.loads(data_file.read_text(encoding="utf-8"))

    def always_fail(self, target):
        raise PermissionError("[WinError 32] 文件正被另一进程使用")

    monkeypatch.setattr(pathlib.Path, "replace", always_fail)

    mapper.user_legacy_id("openid-lost")     # 这次落盘会一直失败
    mapper.flush()

    after = json.loads(data_file.read_text(encoding="utf-8"))
    assert after == before, (
        "写盘失败时旧文件被改坏了 —— 原子写的意义就是「本次失败，旧数据完好」"
    )
    assert after["user_map"]["openid-keep"] == legacy_before

    # 半成品 tmp 不能残留（否则下次启动的 _load 会试图清理它，且占空间）
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert not leftovers, f"失败后残留了临时文件：{leftovers}"


# ==================================================== 3. 失败后还能再写
def test_pending_flush_survives_failed_write(tmp_path, monkeypatch):
    """一次写盘失败后，**后续**的 ``_save()`` 必须仍然能落盘。

    旧实现在 ``_delayed_write`` 里先把 ``_flush_pending`` 置 False 再写；
    写失败被吞掉后，没有任何机制再试 → 数据要等到**下一次**映射变更
    才可能被写出去（而且那次也可能是失败）。
    """
    data_file = tmp_path / "qq_official_ids.json"
    mapper = QQIDMapper(data_file=data_file)
    mapper.user_legacy_id("openid-a")

    def always_fail(self, target):
        raise PermissionError("[WinError 32] 被占用")

    monkeypatch.setattr(pathlib.Path, "replace", always_fail)
    mapper.user_legacy_id("openid-b")        # 这次必失败
    mapper.flush()

    # 占用解除
    monkeypatch.undo()

    # 再触发一次保存 —— 必须能把**此前累积的**改动一起写出去
    mapper.user_legacy_id("openid-c")
    mapper.flush()

    on_disk = json.loads(data_file.read_text(encoding="utf-8"))
    assert "openid-b" in on_disk["user_map"], (
        "上次失败的改动永久丢失了 —— 失败后必须保留「待写盘」状态"
    )
    assert "openid-c" in on_disk["user_map"]


# ==================================================== 4. 静态：tmp 名唯一
def test_tmp_name_is_unique_per_call():
    """临时文件名必须含「每次调用不同」的成分。

    固定 tmp 名（``<data>.json.tmp``）在两个实例 / 两个进程并发写时会
    **互相截断** → ``replace`` 搬走半截文件（与 #72 完全同类）。
    """
    src = _MOD_PATH.read_text(encoding="utf-8")
    assert "_write_file" in src
    # 必须有唯一化成分
    marks = ("getpid", "get_ident", "monotonic", "uuid", "counter", "time_ns")
    assert any(m in src for m in marks), (
        "id_map 的写盘没有唯一临时文件名成分（getpid/get_ident/uuid/…）—— "
        "并发写时两个实例会共用同一个 tmp 并互相截断"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
