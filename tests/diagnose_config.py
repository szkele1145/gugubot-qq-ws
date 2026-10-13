# -*- coding: utf-8 -*-
"""独立诊断脚本（无插件源码依赖）：打印 config.yml 里 QQ 相关关键配置。

用法：python diagnose_config.py <config.yml 路径>
需要：pip install ruamel.yaml （MCDR 环境自带）
"""
import sys, pathlib

try:
    from ruamel.yaml import YAML
except ImportError:
    print("缺少 ruamel.yaml，请用 MCDR 自带的 python 跑（它已安装该依赖）")
    sys.exit(1)

if len(sys.argv) < 2:
    print("用法: python diagnose_config.py <config.yml 路径>")
    sys.exit(1)

config_path = pathlib.Path(sys.argv[1])
if not config_path.exists():
    print(f"找不到配置文件: {config_path}")
    sys.exit(1)

yaml = YAML(typ="safe")
with config_path.open("r", encoding="UTF-8") as f:
    data = yaml.load(f)

def dig(d, *keys):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur

print("=" * 60)
print(f"配置文件: {config_path}")
print("顶层键:", list(data.keys()) if isinstance(data, dict) else type(data))
print("=" * 60)

qq = dig(data, "connector", "QQ") or {}
print("connector.QQ 的键:", list(qq.keys()))
conn = qq.get("connection") or {}
print()
print("connection.type      =", repr(conn.get("type")))
print("connection.official  =", repr(conn.get("official")))
print("connection 其余键    =", [k for k in conn.keys() if k not in ("type", "official")])
print()
perm = qq.get("permissions") or {}
print("permissions 的键     :", list(perm.keys()))
print("  admin_ids          =", repr(perm.get("admin_ids")))
print("  admin_group_ids    =", repr(perm.get("admin_group_ids")))
print("  group_ids          =", repr(perm.get("group_ids")))
print("  forward_group_ids  =", repr(perm.get("forward_group_ids")))
print("  notice_forward_gr. =", repr(perm.get("notice_forward_groups")))
print()
print("=" * 60)
print("parser 判定模拟：")
group_ids = perm.get("group_ids") or []
admin_groups = perm.get("admin_group_ids") or []
valid = {str(g) for g in (list(group_ids) + list(admin_groups)) if g}
print("  valid 集合 =", valid)
for probe in ("1103662998", "CD83E33E21A0FB69CCAD64FCDE6C2B5D"):
    print(f"  {probe!r} in valid ? ->", probe in valid)
print("=" * 60)
