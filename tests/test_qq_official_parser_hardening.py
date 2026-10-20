# -*- coding: utf-8 -*-
"""官方 parser 的日志去重与附件下载加固（v2.2.10 续）。

背景
----
1. **每条消息刷一行日志**
   ① 「本群没有可用配置槽位」：该分支在「没有可用槽位」时**每条消息**
      都会走到（``legacy_group_display_id`` 仍为空 → 再次尝试绑定 →
      再次返回 None）→ 群里正常聊天频率下每条消息刷一行 info。
   ② 「该群不在监听列表」：同理，不在监听列表的群每条消息都打一行 info。
   两者都是 **info** 级别 → 生产环境直接写盘。

2. **附件走明文 http**
   官方同时下发 ``url``（明文 http）与 ``url_https``，旧实现
   ``att.get("url") or att.get("url_https")`` **优先取明文** → 附件
   下载可被中间人劫持/篡改。

3. **附件全量读进内存**
   ``requests.get(url)`` + ``resp.content`` 一次性读整个文件 → 异常大
   文件打爆内存；且缓存上限（512MB LRU）是**下次**下载时才清理，
   救不了当次。

4. **下载失败返回不存在的路径**
   旧实现 ``local.write_bytes(resp.content)`` 抛异常时被外层 except
   吞掉 → 返回 None ✓；但新实现若下载失败却仍 ``return str(local)``
   → 下游拿到一个**不存在的本地路径** → 渲染必然 404。

本文件覆盖上述四点。
"""
import asyncio
import sys
import pathlib
import types
import importlib.abc
import importlib.machinery

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

from gugubot.connector.qq_official import parser as pp


class FL:
    def __init__(self):
        self.infos = []
        self.debugs = []

    def debug(self, msg, *a, **k): self.debugs.append(str(msg))
    def info(self, msg, *a, **k): self.infos.append(str(msg))
    def warning(self, msg, *a, **k): pass
    def error(self, msg, *a, **k): pass


class FakeConfig:
    def __init__(self, data=None):
        self.data = data or {}

    def get_keys(self, path, default=None):
        cur = self.data
        for p in path:
            if not isinstance(cur, dict) or p not in cur:
                return default
            cur = cur[p]
        return cur


class FakeIdMapper:
    def __init__(self, display=None, bind_result=None, legacy="123456"):
        self._display = display
        self._bind_result = bind_result
        self._legacy = legacy
        self.bind_calls = 0

    def legacy_group_display_id(self, openid):
        return self._display

    def bind_group_openid(self, openid):
        self.bind_calls += 1
        return self._bind_result

    def group_legacy_display(self, openid):
        return self._legacy

    def to_legacy_group(self, openid):
        return self._legacy

    def to_legacy_user(self, openid):
        return "900"


class FakePool:
    def add(self, *a, **k): pass


class FakeConnector:
    def __init__(self, id_mapper=None, config=None, logger=None):
        self.id_mapper = id_mapper or FakeIdMapper()
        self.config = config or FakeConfig()
        self.reply_pool = FakePool()
        self.source = "QQ"
        self.logger = logger or FL()
        self._warned_unbound = set()
        self.server = type("S", (), {
            "get_data_folder": staticmethod(lambda: ".")
        })()

    class bot:
        self_id = "BOT-1"


def _mk_parser(connector):
    p = pp.QQOfficialParser.__new__(pp.QQOfficialParser)
    p.connector = connector
    p.logger = connector.logger
    p.server = connector.server
    p.system_manager = None
    p._no_slot_warned = set()
    p._not_listened_warned = set()
    return p


def _group_data(content="hello"):
    return {
        "id": "MSG1",
        "group_openid": "G-OPENID",
        "author": {"member_openid": "U-1", "username": "阿强"},
        "content": content,
        "attachments": [],
    }


# ================================================ 1. 日志去重
def test_no_slot_logged_once_per_group():
    """回归：「没有可用配置槽位」每个群只提示一次。"""
    logger = FL()
    conn = FakeConnector(
        id_mapper=FakeIdMapper(display=None, bind_result=None), logger=logger
    )
    p = _mk_parser(conn)
    # 该群不在监听列表 → 消息被丢弃，但仍会走到槽位检查分支
    conn.config = FakeConfig({"connector": {"QQ": {"permissions": {
        "group_ids": ["999"], "admin_group_ids": []}}}})
    for _ in range(5):
        asyncio.run(p._parse_group_message(_group_data()))
    slot_logs = [m for m in logger.infos if "没有可用配置槽位" in m]
    assert len(slot_logs) == 1, f"应只提示一次，实际 {len(slot_logs)} 条"


def test_not_listened_logged_once_per_group():
    """回归：「不在监听列表」每个群只提示一次。"""
    logger = FL()
    conn = FakeConnector(
        id_mapper=FakeIdMapper(display="123456", bind_result=None), logger=logger
    )
    conn.config = FakeConfig({"connector": {"QQ": {"permissions": {
        "group_ids": ["999"], "admin_group_ids": []}}}})
    p = _mk_parser(conn)
    for _ in range(5):
        asyncio.run(p._parse_group_message(_group_data()))
    listen_logs = [m for m in logger.infos if "不在监听列表" in m]
    assert len(listen_logs) == 1, f"应只提示一次，实际 {len(listen_logs)} 条"


def test_slot_log_repeated_after_bind_success():
    """绑定成功后应允许再次提示（用户补了配置 → 再次提示有意义）。"""
    logger = FL()
    conn = FakeConnector(
        id_mapper=FakeIdMapper(display=None, bind_result="123456"), logger=logger
    )
    p = _mk_parser(conn)
    asyncio.run(p._parse_group_message(_group_data()))
    # 第一次绑定成功 → 无「无槽位」日志
    assert not [m for m in logger.infos if "没有可用配置槽位" in m]
    # 再来一条：mapper 现在已有 display → 不再进入槽位分支
    conn.id_mapper._display = "123456"
    asyncio.run(p._parse_group_message(_group_data()))
    assert not [m for m in logger.infos if "没有可用配置槽位" in m]


# ================================================ 2. 附件优先 https
def test_attachment_prefers_https_url(tmp_path):
    """回归：附件下载必须优先用 url_https（明文 url 可被劫持）。"""
    captured = {}

    class FakeResp:
        headers = {"Content-Length": "3"}

        def raise_for_status(self): pass
        def iter_content(self, chunk_size=1):
            yield b"abc"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_get(url, **kw):
        captured["url"] = url
        captured["kw"] = kw
        return FakeResp()

    orig = pp.requests.get
    pp.requests.get = fake_get
    try:
        conn = FakeConnector(logger=FL())
        p = _mk_parser(conn)
        p.connector.config = FakeConfig({"connector": {"QQ": {"connection": {
            "official": {"cache_attachments": True}}}}})
        p.connector.server = type("S", (), {
            "get_data_folder": staticmethod(lambda: str(tmp_path))
        })()
        segments = asyncio.run(p._build_segments("", [{
            "content_type": "image/png",
            "url": "http://cdn.example.com/a.png",
            "url_https": "https://cdn.example.com/a.png",
            "filename": "a.png",
        }]))
    finally:
        pp.requests.get = orig
    assert captured["url"] == "https://cdn.example.com/a.png", captured["url"]
    assert captured["kw"].get("stream") is True, "必须流式下载"
    assert segments and segments[0]["type"] == "image"


# ================================================ 3. 大文件保护
def test_oversized_attachment_declared_length_is_skipped(tmp_path):
    """回归：Content-Length 超上限 → 放弃缓存（不下载、不写盘）。"""
    called = {"n": 0}

    class FakeResp:
        headers = {"Content-Length": str(pp.QQOfficialParser._ATTACHMENT_MAX_BYTES + 1)}

        def raise_for_status(self): pass
        def iter_content(self, chunk_size=1):
            called["n"] += 1
            yield b"x"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_get(url, **kw):
        return FakeResp()

    orig = pp.requests.get
    pp.requests.get = fake_get
    try:
        conn = FakeConnector(logger=FL())
        p = _mk_parser(conn)
        p.connector.config = FakeConfig({"connector": {"QQ": {"connection": {
            "official": {"cache_attachments": True}}}}})
        p.connector.server = type("S", (), {
            "get_data_folder": staticmethod(lambda: str(tmp_path))
        })()
        local = p._cache_attachment(
            "https://cdn.example.com/big.bin", "big.bin", "application/octet-stream"
        )
    finally:
        pp.requests.get = orig
    assert local is None, "超限应返回 None（只给 url）"
    assert called["n"] == 0, "超限应在读流之前就放弃"


def test_stream_overrun_is_aborted(tmp_path):
    """回归：实际字节数超上限 → 中止并删除半成品。

    桩同时提供 ``content``（旧实现走 ``resp.content`` 一次性读入）
    与 ``iter_content``（新实现走流式），这样旧实现会真的把 64MB
    读进内存并写盘 → 本用例在旧实现下**必须失败**。
    """
    class FakeResp:
        headers = {}  # 无 Content-Length

        def __init__(self):
            self._buf = bytearray()

        @property
        def content(self):
            # 旧实现路径：一次性读入全部内容
            return bytes(self._buf)

        def raise_for_status(self): pass
        def iter_content(self, chunk_size=1):
            chunk = b"x" * (1024 * 1024)
            for _ in range(64):  # 共 64MB > 32MB 上限
                self._buf.extend(chunk)
                yield chunk
        def __enter__(self): return self
        def __exit__(self, *a): return False

    orig = pp.requests.get
    pp.requests.get = lambda url, **kw: FakeResp()
    try:
        conn = FakeConnector(logger=FL())
        p = _mk_parser(conn)
        p.connector.config = FakeConfig({"connector": {"QQ": {"connection": {
            "official": {"cache_attachments": True}}}}})
        p.connector.server = type("S", (), {
            "get_data_folder": staticmethod(lambda: str(tmp_path))
        })()
        local = p._cache_attachment(
            "https://cdn.example.com/big2.bin", "big2.bin", "application/octet-stream"
        )
    finally:
        pp.requests.get = orig
    assert local is None, "超限应返回 None"
    leftovers = list((tmp_path / "attachments").glob("*.part"))
    assert not leftovers, f"半成品应被清理，实际 {leftovers}"


def test_download_failure_returns_none_not_fake_path(tmp_path):
    """回归：下载失败不得返回不存在的本地路径。"""
    def boom(url, **kw):
        raise OSError("network down")

    orig = pp.requests.get
    pp.requests.get = boom
    try:
        conn = FakeConnector(logger=FL())
        p = _mk_parser(conn)
        p.connector.config = FakeConfig({"connector": {"QQ": {"connection": {
            "official": {"cache_attachments": True}}}}})
        p.connector.server = type("S", (), {
            "get_data_folder": staticmethod(lambda: str(tmp_path))
        })()
        local = p._cache_attachment(
            "https://cdn.example.com/c.png", "c.png", "image/png"
        )
    finally:
        pp.requests.get = orig
    assert local is None, "下载失败应返回 None"


def test_cleanup_removes_part_files(tmp_path):
    """回归：LRU 清理应顺带删掉 .part 半成品。"""
    att_dir = tmp_path / "attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    (att_dir / "abc.png.part").write_bytes(b"x" * 10)
    (att_dir / "keep.png").write_bytes(b"y" * 10)
    pp.QQOfficialParser._cleanup_attachment_cache(att_dir)
    assert not (att_dir / "abc.png.part").exists(), ".part 半成品应被清理"
    assert (att_dir / "keep.png").exists(), "正常文件不应被误删"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
