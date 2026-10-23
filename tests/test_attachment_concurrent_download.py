# -*- coding: utf-8 -*-
"""同一 URL 并发下载必须各用各的临时文件（v2.2.10 续，88）。

问题
----
``connector/qq_official/parser.py`` 的 ``_download_attachment`` 用
**固定的**半成品文件名：

.. code-block:: python

    tmp = local.with_suffix(local.suffix + ".part")      # <digest>.png.part
    try:
        with requests.get(url, timeout=20, stream=True) as resp:
            with open(tmp, "wb") as fh:                  # "wb" 会截断
                for chunk in resp.iter_content(...):
                    fh.write(chunk)
        tmp.replace(local)
    finally:
        if tmp.exists():
            tmp.unlink()                                # ← 删共享临时文件

而 ``_cache_attachment`` 跑在**专用下载线程池**里（``_DOWNLOAD_WORKERS = 4``）。
同一个 URL 被两次并发请求时（同一人连发两张相同的图、官方 Resume 补发
导致同一事件被处理两次、两个群同时收到同一个表情包……），两个线程**共用
同一个 .part**：

* 一个线程 ``"wb"`` 会把另一个线程刚写入的内容截断。**但内容相同、偏移
  相同，所以字节结果碰巧仍然正确** —— 这一点我最初判断成"图片损坏"，
  是错的，写在这里免得下次又慌；
* **真正的伤害在 ``finally``**：先失败/先结束的那个线程会 ``unlink`` 这个
  共享临时文件。此时另一个线程还在写 —— Linux 上它的句柄指向已 unlink 的
  inode，最后 ``replace`` 找不到源文件 → 失败 → ``_download_attachment``
  返回 ``False`` → 这条消息的附件**不被缓存**（退回原始 url，而官方 CDN
  url 约 30 分钟过期 → 稍后回看就是裂图）；
* 顺带：两个线程重复传输同一份数据。

结论：这不是「图片损坏」，而是**一个失败的下载会搞丢另一个成功的下载**，
外加无谓的重复传输。

修复
----
半成品文件名**每次唯一**（pid + 线程 id + 自增序号）：两个线程各写各的
临时文件，互不干扰，``finally`` 也只清自己的。

（与 #76 同一类：**固定临时文件名 + 并发**。）

测试手法
--------
不用耗时/竞态断言（那类断言在本项目已经假失败过一次，#71）。这里直接
**记录两个线程实际打开的半成品路径**：固定名 → 两条记录相同 → 失败；
唯一名 → 不同 → 通过。判定与调度顺序无关。
"""
import builtins
import importlib.abc
import importlib.machinery
import pathlib
import sys
import threading
import time
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


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
sys.path.insert(0, str(_ROOT / "plugin"))

_MOD = "gugubot.connector.qq_official.parser"
sys.modules.pop(_MOD, None)
from gugubot.connector.qq_official import parser as parser_mod  # noqa: E402

QQOfficialParser = parser_mod.QQOfficialParser

PAYLOAD = bytes(range(256)) * 8


class _Logger:
    def __getattr__(self, n):
        return lambda *a, **k: None


class _FakeResponse:
    status_code = 200

    def __init__(self):
        self.headers = {"Content-Length": str(len(PAYLOAD))}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=1024):
        # 故意慢一点：两个线程必须**同时处于下载中**才测得到并发共用
        # 临时文件的情形。若第一个瞬间完成，第二个会直接命中缓存
        # （local.exists()）而根本不下载 —— 那就测不到本次要测的东西。
        step = len(PAYLOAD) // 8
        for i in range(0, len(PAYLOAD), step):
            time.sleep(0.025)
            yield PAYLOAD[i:i + step]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _make_parser(tmpdir):
    inst = QQOfficialParser.__new__(QQOfficialParser)
    inst.logger = _Logger()
    inst.connector = types.SimpleNamespace(
        config=types.SimpleNamespace(get_keys=lambda path, default=None: default),
        server=types.SimpleNamespace(get_data_folder=lambda: str(tmpdir)),
    )
    # 让清理节流「刚跑过」，避免测试里触发目录扫描
    inst._cache_last_check = 1e18
    return inst


# ==================================================== 1. 并发必须各用各的临时文件
def test_concurrent_same_url_uses_distinct_temp_files(monkeypatch, tmp_path):
    """两个线程下载同一 URL 时，半成品路径必须**不同**。"""
    monkeypatch.setattr(parser_mod.requests, "get", lambda *a, **k: _FakeResponse())

    opened = []            # [(线程 id, 路径)]
    opened_lock = threading.Lock()
    real_open = builtins.open

    def spy_open(file, mode="r", *a, **k):
        try:
            path_str = str(file)
        except Exception:      # pragma: no cover
            path_str = ""
        if "w" in mode and path_str.endswith(".part"):
            with opened_lock:
                opened.append((threading.get_ident(), path_str))
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", spy_open)

    inst = _make_parser(tmp_path)
    results = []
    errors = []

    def worker():
        try:
            results.append(
                inst._cache_attachment(
                    "https://cdn.example.com/a.png", "a.png", "image/png"
                )
            )
        except Exception as e:      # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)

    assert not errors, f"下载线程抛异常: {errors}"
    assert len(opened) == 2, f"预期两次下载各打开一个 .part，实际记录: {opened}"

    paths = {p for _, p in opened}
    assert len(paths) == 2, (
        f"两个并发下载写了**同一个**半成品文件: {paths}。"
        f"先失败/先结束的那个线程会在 finally 里 unlink 这个共享文件 —— "
        f"另一个线程随后 replace 找不到源 → 下载被丢弃 → 这条消息的附件"
        f"不被缓存（退回会过期的 CDN url）。同时还会重复传输同一份数据"
    )


# ==================================================== 2. 单线程行为不变
def test_single_download_still_works(monkeypatch, tmp_path):
    """加固不能把正常路径弄坏。"""
    monkeypatch.setattr(parser_mod.requests, "get", lambda *a, **k: _FakeResponse())
    inst = _make_parser(tmp_path)

    result = inst._cache_attachment(
        "https://cdn.example.com/b.png", "b.png", "image/png"
    )
    assert result is not None, "单线程下载失败"
    cached = pathlib.Path(result)
    assert cached.is_file()
    assert cached.read_bytes() == PAYLOAD
    assert not [p.name for p in cached.parent.iterdir() if p.name.endswith(".part")], (
        "成功后残留半成品文件"
    )


# ==================================================== 3. 静态：临时名唯一成分
def test_part_filename_is_unique_per_attempt():
    src = (_ROOT / "plugin" / "gugubot" / "connector" / "qq_official" / "parser.py"
           ).read_text(encoding="utf-8-sig")

    assert ".part" in src, "没找到 .part 相关代码，测试需更新"
    marks = ("get_ident", "getpid", "uuid", "counter", "time_ns")
    assert any(m in src for m in marks), (
        "半成品文件名没有唯一化成分 —— 同一 URL 并发下载会共用同一个 "
        ".part（与 #76 同一类：固定临时文件名 + 并发）"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
