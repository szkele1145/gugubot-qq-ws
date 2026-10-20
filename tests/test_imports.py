# -*- coding: utf-8 -*-
"""全插件导入回归：验证 OneBot + 官方双路径全部模块可导入。"""
import sys
import pathlib, types

class _Any:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return _Any()
    def __call__(self, *a, **k): return _Any()
class _StubModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"): raise AttributeError(name)
        v = _Any(); setattr(self, name, v); return v
import importlib.abc, importlib.machinery
class _Loader(importlib.abc.Loader):
    def create_module(self, spec): return _StubModule(spec.name)
    def exec_module(self, m): pass
class _StubFinder:
    def find_spec(self, f, p=None, t=None):
        if f.startswith("mcdreforged") or f.startswith("packaging"):
            return importlib.machinery.ModuleSpec(f, _Loader(), is_package=True)
sys.meta_path.insert(0, _StubFinder())
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'plugin'))

import importlib
mods = [
    'gugubot',  # 主入口（依赖最全）
    'gugubot.connector.qq_connector',
    'gugubot.connector.bridge_connector',
    'gugubot.connector.mc_connector',
    'gugubot.connector.test_connector',
    'gugubot.parser.qq_parser',
    'gugubot.logic.system',
    'gugubot.logic.plugins',
    'gugubot.api',
]
for m in mods:
    importlib.import_module(m)
    print('OK  ', m)
print('FULL PLUGIN IMPORT PASSED')
