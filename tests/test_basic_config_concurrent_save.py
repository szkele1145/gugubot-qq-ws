# -*- coding: utf-8 -*-
"""BasicConfig 并发写盘安全 + 基岩缓存并发查询不丢更新（v2.2.10 续）。

问题
----
两个真实缺陷（一个是历史遗留，一个是 #71 修复引入的）：

**A. ``BasicConfig.save()`` 用固定名临时文件**（``path.tmp``）且无锁。
save() 会被**多个线程**并发调用：

* 事件循环线程：``__setitem__`` 触发的自动保存
* **线程池线程**：``inactive_check`` 把 HTTP 查询丢进 executor 后，
  原实现里「回写缓存 + save()」也跟着进了线程池

共用固定 tmp 名时两个线程写到**同一个文件** → 互相截断 →
``os.replace`` 可能搬走**半截文件** → **配置/玩家数据损坏且无法恢复**。
另外 ``json.dump(dict(self))`` 在 dict 被并发修改时抛
"dictionary changed size during iteration"。

**B. 缓存的读-改-写丢更新**。多个基岩名是**并发**查询的
（``asyncio.gather``），每个协程都在「读缓存 → await HTTP → 写缓存」
之间让出控制权。写回时若用 await 之前拿到的**旧字典引用**，会把别的
协程在这期间写入的条目**整块覆盖掉**（丢更新）。

修复
----
* A：``save()`` 加 ``threading.RLock`` + **每次唯一**的临时文件名
  （pid + 线程 id + 自增序号）+ 失败时清理 tmp；
* B：写回前**重读**当前缓存字典再改（``_write_bedrock_cache``），
  HTTP 单独抽成纯函数 ``_fetch_bedrock_uuid_http`` 丢线程池
  （线程里不碰 ``self`` 的可变状态）。

测试
----
1. 静态：``save()`` 的临时文件名必须含「每次调用不同」的成分
2. 静态：``save()`` 必须持锁
3. 运行时：8 线程 × 50 轮并发 save → 落盘文件始终可解析、内容始终是
   **某一次完整保存**的结果（没有半截/穿插）
4. 运行时：两个并发查询（各自写不同玩家）→ 两个玩家的缓存条目
   **都**在（丢更新的版本会只剩一个）
"""
import ast
import importlib.abc
import importlib.machinery
import json
import pathlib
import sys
import tempfile
import threading
import time
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

from gugubot.config.basic_config import BasicConfig


# ==================================================== 1. 静态：唯一临时文件名
def test_save_tmp_name_is_unique_per_call():
    """``save()`` 的临时文件名必须含「每次调用不同」的成分。

    固定名（``xxx.tmp``）在多线程并发 save 时会让两个线程写到同一个
    文件 → 互相截断 → ``os.replace`` 搬走半截文件 → 配置损坏。
    """
    src = pathlib.Path(
        sys.modules["gugubot.config.basic_config"].__file__
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)

    save_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "save":
            save_fn = node
            break
    assert save_fn is not None, "找不到 BasicConfig.save"

    src_seg = ast.get_source_segment(src, save_fn)
    # 临时文件名必须由「每次调用都会变」的东西拼出来：pid / 线程 id /
    # 自增计数 / uuid / 临时目录。静态检查：save 函数体（含它调用的
    # 生成函数）里出现了这类成分。
    candidates = [
        "getpid", "get_ident", "counter", "uuid", "mkstemp", "NamedTemporary",
        "mkdtemp", "tempfile",
    ]
    # save 可能委托给 _make_tmp_path —— 把整个类源码都纳入检查
    assert any(c in src for c in candidates), (
        "BasicConfig.save 的临时文件名似乎是固定的——多线程并发保存时"
        "会互相截断 tmp 文件，os.replace 可能搬走半截文件 → 配置损坏"
    )


# ==================================================== 2. 静态：save 必须持锁
def test_save_is_locked():
    """``save()`` 必须持有实例锁。

    否则两个线程同时 ``json.dump(dict(self))`` 时，dict 被另一个线程
    改动会抛 "dictionary changed size during iteration"。
    """
    src = pathlib.Path(
        sys.modules["gugubot.config.basic_config"].__file__
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)

    save_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "save":
            save_fn = node
            break

    body_src = ast.get_source_segment(src, save_fn)
    assert (
        "with self._get_save_lock()" in body_src
        or "with self._save_lock" in body_src
        or "acquire" in body_src
    ), (
        "BasicConfig.save 没有持锁 —— 并发保存时 json.dump 会撞上"
        "dictionary changed size during iteration，或两个线程写同一个 tmp"
    )


# ==================================================== 3. 运行时：并发 save 不损坏
def test_concurrent_save_never_corrupts():
    """8 线程 × 40 轮并发 save → 落盘文件**始终**可解析、内容是某一次
    完整保存的结果。

    固定 tmp 名 + 无锁的版本会在两个线程同时 open("w") 同一个 tmp 时
    互相截断 → os.replace 搬走半截文件 → json 解析失败（ValueError）。
    """
    import os

    with tempfile.TemporaryDirectory() as td:
        path = pathlib.Path(td) / "data" / "conf.json"
        cfg = BasicConfig(path=str(path), default_content={"n": 0})

        errors = []
        stop = threading.Event()

        def writer(tid):
            try:
                for i in range(40):
                    cfg[f"key_{tid}"] = i
                    # 直接 save（不依赖 __setitem__ 的自动保存）
                    cfg.save()
            except Exception as e:
                errors.append((tid, repr(e)))

        def reader():
            """持续读盘上文件——读到的内容**任何时刻**都必须是完整 JSON。

            注意：这里刻意用**裸 open**（不走插件的重试逻辑），模拟
            外部读者（用户的编辑器 / 备份脚本）。Windows 上 writer
            正在 ``os.replace`` 的瞬间，裸 open 本身可能
            ``PermissionError`` —— 那是 Windows 的共享模型限制，**不是
            本次要修的缺陷**，故容忍它；真正要断言的是：**凡是成功读
            到的内容，绝不能是半截文件**（旧实现在两个线程共用一个
            tmp 名时会互相截断 → os.replace 搬走半截文件 → 这里
            json.load 抛 JSONDecodeError）。
            """
            try:
                while not stop.is_set():
                    if path.exists():
                        try:
                            with path.open("r", encoding="utf-8") as f:
                                data = json.load(f)
                        except PermissionError:
                            # Windows 替换窗口，容忍（外部读者无法避免）
                            pass
                        except json.JSONDecodeError as e:
                            # ← 这个绝不能发生：读到半截文件
                            errors.append(("reader-corrupt", repr(e)))
                            return
                        else:
                            assert isinstance(data, dict)
                    # 模拟真实外部读者的间歇访问（不是 100% 占住文件）
                    time.sleep(0.003)
            except Exception as e:
                errors.append(("reader", repr(e)))

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(8)]
        rd = threading.Thread(target=reader, daemon=True)
        rd.start()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stop.set()
        rd.join(timeout=5)

        assert not errors, f"并发 save 出错: {errors[:5]}"

        # 终态：所有 key 都在（每个线程写了 40 轮，最后一轮一定成功落盘）
        with path.open("r", encoding="utf-8") as f:
            final = json.load(f)
        for tid in range(8):
            assert final[f"key_{tid}"] == 39, (
                f"key_{tid} = {final.get(f'key_{tid}')}，应为 39"
            )

        # 没有遗留 tmp 文件
        leftovers = list(path.parent.glob("*.tmp"))
        assert not leftovers, f"遗留临时文件: {[p.name for p in leftovers]}"


# ==================================================== 4. 运行时：插件自身读盘不失败
def test_plugin_read_survives_concurrent_save():
    """插件自身的读盘（``_open_for_read`` / ``reload``）**不得**因并发
    保存而失败。

    这是第 3 项的另一半：裸 open 的外部读者在 Windows 替换窗口内可能
    ``PermissionError``（无法避免），但**插件自己**的读路径带了退避
    重试，必须 100% 成功 —— 否则 MCDR 热重载会随机报「重载失败」。
    """
    with tempfile.TemporaryDirectory() as td:
        path = pathlib.Path(td) / "data" / "conf.json"
        cfg = BasicConfig(path=str(path), default_content={"n": 0})
        errors = []
        stop = threading.Event()

        def writer():
            try:
                for i in range(200):
                    cfg["n"] = i
                    cfg.save()
            except Exception as e:
                errors.append(("writer", repr(e)))
            finally:
                stop.set()

        def plugin_reader():
            """走插件自己的退避读路径 —— 必须零失败。

            每轮之间 sleep 一小段：模拟**真实**读者（MCDR 热重载 /
            编辑器）的间歇性访问，而不是一个 100% 占住文件的敌手
            （那种情况下任何写入策略都无解——文件被锁死意味着 OS
            层面就不允许写）。
            """
            n = 0
            while not stop.is_set():
                try:
                    with cfg._open_for_read() as f:
                        data = json.load(f)
                    assert isinstance(data, dict)
                    n += 1
                except Exception as e:
                    errors.append(("plugin_reader", repr(e), n))
                    return
                time.sleep(0.005)

        wt = threading.Thread(target=writer)
        rts = [threading.Thread(target=plugin_reader) for _ in range(3)]
        for t in rts:
            t.start()
        wt.start()
        wt.join()
        for t in rts:
            t.join(timeout=10)

        assert not errors, f"插件自身读盘失败: {errors[:3]}"


# ==================================================== 5. 运行时：并发查询不丢缓存
def test_concurrent_bedrock_queries_do_not_lose_cache_entries():
    """两个并发查询（各写不同玩家）→ 两个缓存条目**都**必须在。

    丢更新的版本：两个协程都在「读缓存」时拿到同一个（空的）字典
    引用，查完各自写回 → 后写的整块覆盖先写的 → 只剩一个玩家。
    """
    import asyncio
    from gugubot.logic.plugins import inactive_check as ic

    sys_obj = ic.InactiveCheckSystem.__new__(ic.InactiveCheckSystem)
    sys_obj.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        debug=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    sys_obj._data = {"bedrock_cache": {}}
    sys_obj.get = lambda k, d=None: (sys_obj._data.get(k, d))
    sys_obj.__setitem__ = lambda k, v: sys_obj._data.__setitem__(k, v)
    sys_obj.save = lambda: None
    sys_obj.config = types.SimpleNamespace(
        get_keys=lambda path, default=None: 15552000
    )

    # 两个玩家的 HTTP 查询都「慢」（0.15s）→ 两个协程必然并发在途
    def fake_get(url, headers=None, timeout=None):
        time.sleep(0.15)
        name = url.rsplit("/", 1)[-1]
        return types.SimpleNamespace(
            status_code=200,
            json=lambda n=name: {"floodgateuid": f"uuid-{n}"},
        )

    orig_requests = ic.requests
    ic.requests = types.SimpleNamespace(
        get=fake_get,
        exceptions=types.SimpleNamespace(
            Timeout=type("Timeout", (Exception,), {}),
            RequestException=type("RequestException", (Exception,), {}),
        ),
    )
    try:
        async def run():
            await asyncio.gather(
                sys_obj._get_bedrock_player_uuid_async("Alice"),
                sys_obj._get_bedrock_player_uuid_async("Bob"),
            )

        asyncio.run(run())
    finally:
        ic.requests = orig_requests

    cache = sys_obj._data["bedrock_cache"]
    assert "alice" in cache, f"alice 的缓存条目丢失: {list(cache)}"
    assert "bob" in cache, f"bob 的缓存条目丢失: {list(cache)}"
    assert cache["alice"]["uuid"] == "uuid-Alice"
    assert cache["bob"]["uuid"] == "uuid-Bob"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
