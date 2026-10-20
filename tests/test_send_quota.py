# -*- coding: utf-8 -*-
import sys
import pathlib, types, asyncio

class _A:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return _A()
    def __call__(self, *a, **k): return _A()
class _SM(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith('__'): raise AttributeError(name)
        v = _A(); setattr(self, name, v); return v
import importlib.abc, importlib.machinery
class _L(importlib.abc.Loader):
    def create_module(self, s): return _SM(s.name)
    def exec_module(self, m): pass
class _F:
    def find_spec(self, f, p=None, t=None):
        if f.startswith('mcdreforged') or f.startswith('packaging'):
            return importlib.machinery.ModuleSpec(f, _L(), is_package=True)
sys.meta_path.insert(0, _F())
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'plugin'))
from gugubot.connector.qq_official.connector import QQOfficialConnector
from gugubot.connector.qq_official.api_client import QQOfficialAPIError

takes = {'n': 0}
sends = []
class FakePool:
    def take(self, g):
        takes['n'] += 1
        return ('MID1', takes['n'] - 1)
    def take_event(self, g): return None
class FakeAPI:
    def __init__(self): self.calls = 0
    def send_group_message(self, g, content, msg_id=None, msg_seq=None):
        self.calls += 1
        sends.append((msg_id, msg_seq))
        if self.calls < 3:
            raise QQOfficialAPIError(429, 11244, 'rate limited')
        return {'ok': True}

c = QQOfficialConnector.__new__(QQOfficialConnector)
c.reply_pool = FakePool()
c.api_client = FakeAPI()
c._is_rate_limited = staticmethod(QQOfficialConnector._is_rate_limited)
r = asyncio.new_event_loop().run_until_complete(c._send_group('G', 'hello'))
assert r == {'ok': True}
assert takes['n'] == 1, takes  # 全程复用：只取池一次
assert sends == [('MID1', 0)] * 3, sends  # 三次尝试同一额度
print('PASS: _send_group 重试全程复用首取额度（3次尝试仅1次取池）')
print("SEND QUOTA TEST PASSED")
