# -*- coding: utf-8 -*-
"""被动回复额度池（reply_pool）双通道不变量测试。

覆盖三条真实缺陷的回归：
1. drop() 曾是死代码 —— 被官方拒绝的废 msg_id 永不淘汰；
2. event_id 备用通道没有计数 —— 会反复交出已耗尽的 event_id；
3. cleanup()/take() 会把 event_id 仍有额度的条目的提前回收。
"""
import sys, types, asyncio, time, importlib.abc, importlib.machinery


class _A:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return _A()
    def __call__(self, *a, **k): return _A()


class _SM(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        v = _A(); setattr(self, name, v); return v


class _L(importlib.abc.Loader):
    def create_module(self, s): return _SM(s.name)
    def exec_module(self, m): pass


class _F:
    def find_spec(self, f, p=None, t=None):
        if f.startswith('mcdreforged') or f.startswith('packaging'):
            return importlib.machinery.ModuleSpec(f, _L(), is_package=True)


sys.meta_path.insert(0, _F())
sys.path.insert(0, r'F:\vibe coding工程\gugubot-qq-ws\plugin')

from gugubot.connector.qq_official.reply_pool import (
    QQReplyPool, _MAX_SEQ, _POOL_LIMIT,
)
from gugubot.connector.qq_official.connector import QQOfficialConnector
from gugubot.connector.qq_official.api_client import QQOfficialAPIError

G = "GROUP_OPENID"


# ---------------------------------------------------------------- 基础行为
def test_seq_increments_then_exhausts():
    p = QQReplyPool(None)
    p.add(G, "MID1")
    seqs = [p.take(G)[1] for _ in range(_MAX_SEQ)]
    assert seqs == list(range(_MAX_SEQ)), seqs
    # 无 event_id：两条通道都算耗尽 → take 返回 None 并回收条目
    assert p.take(G) is None
    assert p.size(G) == 0
    print("PASS: msg_seq 0..4 递增，用尽后条目被回收")


def test_latest_first():
    p = QQReplyPool(None)
    p.add(G, "OLD")
    p.add(G, "NEW")
    assert p.take(G)[0] == "NEW"
    print("PASS: 优先取最新的 msg_id")


def test_event_channel_has_own_counter():
    p = QQReplyPool(None)
    p.add(G, "MID1", event_id="EV1")
    eids = [p.take_event(G) for _ in range(_MAX_SEQ)]
    assert eids == ["EV1"] * _MAX_SEQ, eids
    # 第 6 次必须交出 None，而不是反复给同一个已耗尽的 event_id
    assert p.take_event(G) is None
    print("PASS: event_id 独立计数，耗尽后不再重复交出")


def test_entry_kept_while_event_quota_remains():
    """msg_id 用尽但 event_id 仍有额度 → 条目必须保留给 take_event。"""
    p = QQReplyPool(None)
    p.add(G, "MID1", event_id="EV1")
    for _ in range(_MAX_SEQ):
        p.take(G)
    assert p.take(G) is None, "msg 通道应已无货"
    assert p.size(G) == 1, "event_id 仍有额度时不得回收条目"
    assert p.take_event(G) == "EV1"
    print("PASS: msg_seq 用尽但 event 有额度时条目被保留")


def test_drop_removes_entry():
    p = QQReplyPool(None)
    p.add(G, "MID1")
    p.drop(G, "MID1")
    assert p.size(G) == 0
    # 桶空后整个 key 移除，防止小对象累积
    assert G not in p._pools
    p.drop(G, "NOT_EXIST")  # 幂等，不得抛异常
    print("PASS: drop() 生效且幂等，空桶被移除")


def test_drop_event_keeps_msg_quota():
    """event_id 被判废时只摘该字段，不得连带牺牲 msg_id 额度。"""
    p = QQReplyPool(None)
    p.add(G, "MID1", event_id="EV1")
    p.drop_event(G, "EV1")
    assert p.take_event(G) is None, "已判废的 event_id 不得再被交出"
    taken = p.take(G)
    assert taken is not None and taken[0] == "MID1", "msg_id 额度必须保留"
    print("PASS: drop_event() 只摘 event 通道，保留 msg_id 额度")


def test_expired_entries_swept():
    p = QQReplyPool(None)
    p.add(G, "MID1", event_id="EV1")
    p._pools[G]["MID1"]["ts"] = time.time() - 10_000
    assert p.take(G) is None
    assert p.size(G) == 0
    print("PASS: 过期条目被回收")


def test_take_ttl_deletes_expired_newest_entry():
    """take() 扫描从最新条目开始；若最新条目已过期，必须把该条目本身
    及其之前的条目一并清理（keys[:idx+1]，而非误写成 keys[:idx] 或 keys[idx:]）。

    回归：slice 边界错一会导致过期条目残留，或把尚未过期的旧条目连带误删。
    """
    p = QQReplyPool(None)
    old = time.time() - 10_000
    p.add(G, "OLDER")     # index 0，未过期
    p.add(G, "EXPIRED")   # index 1，最新，已过期
    p._pools[G]["EXPIRED"]["ts"] = old
    assert p.take(G) is None
    assert p.size(G) == 0, f"过期条目及其之前条目应全部清理，还剩 {p.size(G)}"
    print("PASS: take() 遇到过期条目时连同更早条目一并清理")


def test_cleanup_keeps_event_only_entries():
    """cleanup 的淘汰判据必须与 take 一致：两条通道都耗尽才删。"""
    p = QQReplyPool(None)
    p.add(G, "MID1", event_id="EV1")
    for _ in range(_MAX_SEQ):
        p.take(G)
    removed = p.cleanup()
    assert removed == 0, f"event_id 仍有额度，不应清理（removed={removed}）"
    assert p.size(G) == 1
    # 两条通道都耗尽后才允许清理
    for _ in range(_MAX_SEQ):
        p.take_event(G)
    assert p.cleanup() == 1
    assert p.size(G) == 0
    print("PASS: cleanup() 与 take() 淘汰判据一致")


def test_pool_limit_evicts_oldest():
    p = QQReplyPool(None)
    for i in range(_POOL_LIMIT + 10):
        p.add(G, f"MID{i}")
    assert p.size(G) == _POOL_LIMIT
    assert p.take(G)[0] == f"MID{_POOL_LIMIT + 9}"
    print("PASS: 池上限生效，淘汰最旧条目")


def test_add_backfills_event_id():
    """事件先到、外层 id 后到时，add 必须回填 event_id 而非新建条目。"""
    p = QQReplyPool(None)
    p.add(G, "MID1")
    p.add(G, "MID1", event_id="EV1")
    assert p.size(G) == 1
    for _ in range(_MAX_SEQ):
        p.take(G)
    assert p.take_event(G) == "EV1"
    print("PASS: 重复 add 回填 event_id 且不新建条目")


# ------------------------------------------------- 发送链路：失效引用淘汰
class _FakePool:
    def __init__(self, items, events=None):
        self.items = list(items)
        self.events = list(events or [])
        self.takes = 0
        self.take_events = 0
        self.dropped = []
        self.dropped_events = []

    def take(self, g):
        self.takes += 1
        return self.items.pop(0) if self.items else None

    def take_event(self, g):
        self.take_events += 1
        return self.events.pop(0) if self.events else None

    def drop(self, g, mid):
        self.dropped.append(mid)

    def drop_event(self, g, eid):
        self.dropped_events.append(eid)


class _FakeAPI:
    """第一次用被动 id 返回「引用失效」，之后成功。"""

    def __init__(self, err):
        self.err = err
        self.calls = []

    def send_group_message(self, g, content, msg_id=None, msg_seq=None):
        self.calls.append(("group", msg_id, msg_seq))
        if len(self.calls) == 1:
            raise self.err
        return {"ok": True}

    def send_c2c_message(self, u, content, msg_id=None, msg_seq=None):
        self.calls.append(("c2c", msg_id, msg_seq))
        if len(self.calls) == 1:
            raise self.err
        return {"ok": True}


def _connector(pool, api):
    c = QQOfficialConnector.__new__(QQOfficialConnector)
    c.reply_pool = pool
    c.api_client = api
    return c


def test_stale_msg_id_is_dropped_and_replaced():
    err = QQOfficialAPIError(400, 22009, "msg_id 已过期")
    pool = _FakePool([("MID1", 0), ("MID2", 0)])
    api = _FakeAPI(err)
    c = _connector(pool, api)
    r = asyncio.new_event_loop().run_until_complete(c._send_group(G, "hi"))
    assert r == {"ok": True}
    assert pool.dropped == ["MID1"], pool.dropped
    assert api.calls == [("group", "MID1", 0), ("group", "MID2", 0)], api.calls
    print("PASS: 失效 msg_id 被丢弃并换用下一条")


def test_rate_limit_reuses_same_quota():
    """限频不是失效：必须复用同一额度，不得误丢。"""
    err = QQOfficialAPIError(429, 11244, "rate limited")
    pool = _FakePool([("MID1", 0), ("MID2", 0)])
    api = _FakeAPI(err)
    c = _connector(pool, api)
    r = asyncio.new_event_loop().run_until_complete(c._send_group(G, "hi"))
    assert r == {"ok": True}
    assert pool.dropped == [], pool.dropped
    assert api.calls == [("group", "MID1", 0), ("group", "MID1", 0)], api.calls
    print("PASS: 限频错误复用同一额度，不误丢")


def test_stale_event_id_is_dropped_and_replaced():
    err = QQOfficialAPIError(400, 22009, "event_id 无效")
    pool = _FakePool([], events=["EV1", "EV2"])
    api = _FakeAPI(QQOfficialAPIError(500, 500, "unused"))
    c = _connector(pool, api)

    # 注意：connector 用 asyncio.to_thread 调它，因此必须是同步函数
    def fake_event(g, content, eid):
        api.calls.append(("EVENT", eid))
        if len(api.calls) == 1:
            raise err
        return {"ok": True}

    c._request_group_with_event = fake_event
    r = asyncio.new_event_loop().run_until_complete(c._send_group(G, "hi"))
    assert r == {"ok": True}
    assert pool.dropped_events == ["EV1"], pool.dropped_events
    assert api.calls == [("EVENT", "EV1"), ("EVENT", "EV2")], api.calls
    print("PASS: 失效 event_id 被摘下并换用下一条")


def test_c2c_event_fallback_present():
    """C2C 路径此前完全没有 event_id 回退，此测试锁死该行为。"""
    pool = _FakePool([], events=["EV1"])
    api = _FakeAPI(QQOfficialAPIError(500, 500, "unused"))
    c = _connector(pool, api)
    seen = {}

    # 同上：必须同步函数（走 to_thread）
    def fake_event(u, content, eid):
        seen["eid"] = eid
        return {"ok": True}

    c._request_c2c_with_event = fake_event
    r = asyncio.new_event_loop().run_until_complete(c._send_c2c("U1", "hi"))
    assert r == {"ok": True}
    assert seen.get("eid") == "EV1", seen
    assert pool.takes == 1 and pool.take_events == 1
    print("PASS: C2C 路径具备 event_id 回退")


def test_api_client_payload_carries_passive_refs():
    """api_client 构造的 body 必须带 msg_seq / event_id。"""
    from gugubot.connector.qq_official.api_client import QQOfficialClient
    sent = []

    client = QQOfficialClient.__new__(QQOfficialClient)
    client._pool = _FakePool([("MID1", 3)], events=["EV9"])
    client._token = None
    client.logger = None
    client._seq_lock = None
    client._request = lambda m, p, payload=None, **k: (sent.append((p, payload)), {})[1]

    client.send_group_message(G, "t")
    assert sent[-1][1]["msg_id"] == "MID1" and sent[-1][1]["msg_seq"] == 3

    # msg 通道空 → 落到 event_id
    client._pool = _FakePool([], events=["EV9"])
    client.send_group_message(G, "t")
    assert sent[-1][1].get("event_id") == "EV9"

    # C2C 文本
    client._pool = _FakePool([], events=["EV9"])
    client.send_c2c_message("U1", "t")
    assert sent[-1][1].get("event_id") == "EV9"
    print("PASS: api_client 群/C2C 均带 msg_seq 与 event_id")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nREPLY POOL TEST PASSED")
