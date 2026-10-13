# 测试套件

GUGUBot 官方直连改造版的自动化测试（全部独立可跑，无需真实 QQ 账号）。

运行方式（Windows，Python 3.12+，需 `pip install ruamel.yaml websocket-client requests mcstatus`）：

```powershell
$env:PYTHONIOENCODING = "utf-8"
python tests\test_behavior.py   # id_map/reply_pool/parser/消息转换
python tests\test_ws.py         # WS 协议状态机（本地 mock 官方服务器）
python tests\test_native.py     # native_chat 注入命令生成与转义
python tests\test_e2e.py        # 端到端：官方事件→解析→图片缓存→MC注入
python tests\test_imports.py    # 全插件导入回归（OneBot+官方双路径）
python tests\test_migration.py # 旧配置迁移：v2.2.4 用户值保留 + 新键自动补全
python tests\test_status.py     # #状态 命令：官方模式响应/非管理员拒绝/OneBot跳过
```

| 测试 | 覆盖内容 |
|---|---|
| test_behavior.py | openid↔虚拟号映射、真实群号自动绑定、持久化重载；回复池（最新优先/seq 递增/用尽回退/TTL/自动清理）；群消息解析（@bot 剥离、群号绑定、入回复池、bot 消息拦截）；未监听群拒绝；C2C 解析；消息段转文本与颜色码剥离 |
| test_ws.py | 官方 WS 协议：op:10 Hello → op:2 Identify（QQBot token 前缀、intents=1<<25、shard）→ READY（session_id）→ op:1 心跳 → op:11 ACK（本地起最小 WS 服务器全真握手） |
| test_native.py | native_chat 分支：brigadier 转义（引号/反斜杠）、`[QQ转发]玩家名:` 格式、CICode 生成 |
| test_e2e.py | 完整数据流：官方 GROUP_MESSAGE_CREATE（带图片附件）→ 群号绑定 → 图片下载缓存（本地 http server）→ BroadcastInfo → MCConnector native_chat → `guguchat inject` 命令（CICode 公网 url 优先）+ C2C 链路 |
| test_imports.py | 主入口及全部子模块导入回归（stub mcdreforged），验证 OneBot 旧路径与官方新路径共存无冲突 |
| test_migration.py | 配置兼容实证：v2.2.4 老配置（token/群号/管理员/自定义群名/模板）迁移后用户值原样保留，新键（connection.type / official / native_chat）自动补全且默认不改变旧行为 |
| test_status.py | `#状态` 命令：官方模式管理员响应（WS/token/池/绑定全展示）、非管理员拒绝、OneBot 模式自动跳过（duck typing 判定） |
