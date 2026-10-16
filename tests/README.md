# 测试套件

GUGUBot 官方直连改造版的自动化测试（全部独立可跑，无需真实 QQ 账号）。

运行方式（Windows，Python 3.11+ / 3.14 验证通过，需 `pip install ruamel.yaml websocket-client requests mcstatus`）：

```powershell
$env:PYTHONIOENCODING = "utf-8"
python tests\test_behavior.py   # id_map/reply_pool/parser/消息转换
python tests\test_ws.py         # WS 协议状态机（本地 mock 官方服务器）
python tests\test_native.py     # native_chat 注入命令生成与转义（tellraw 系统消息通道）
python tests\test_e2e.py        # 端到端：官方事件→解析→图片缓存→MC 系统消息注入
python tests\test_imports.py    # 全插件导入回归（OneBot+官方双路径）
python tests\test_migration.py # 旧配置迁移：v2.2.4 用户值保留 + 新键自动补全
python tests\test_status.py     # #状态/#我是谁 命令：官方模式响应/权限/OneBot跳过
python tests\test_send_quota.py # 发送额度守卫：限频重试复用首取 seq
python tests\test_reply_pool.py # 被动额度池双通道不变量（msg_id/event_id/TTL/淘汰）
python tests\test_rate_limit.py # REST 429 语义（不刷新 token 不重试）+ 可配置超时
```

| 测试 | 覆盖内容 |
|---|---|
| test_behavior.py | openid↔虚拟号映射、真实群号自动绑定、持久化重载（flush 语义）；回复池（最新优先/seq 递增/用尽回退/TTL/自动清理/**过期即停**/**event_id 备用通道**）；媒体额度守卫（use_reply_pool=False 不取池）；图片占位过滤（_filter_images）；群消息解析（@bot 剥离、群号绑定、入回复池、bot 消息拦截）；未监听群拒绝；C2C 解析；消息段转文本与颜色码剥离 |
| test_ws.py | 官方 WS 协议：op:10 Hello → op:2 Identify（QQBot token 前缀、intents=1<<25、shard）→ READY（session_id）→ op:1 心跳 → op:11 ACK（本地起最小 WS 服务器全真握手） |
| test_native.py | native_chat 分支：走原版 `/tellraw @a <JSON>` 系统消息通道（`ClientboundSystemChatPacket`，不触发聊天验证错误）；`[群名] [QQ转发]名字:` 拼接、引号/反斜杠/换行转义、`show_group_prefix` 只控制可选的 `[群名] ` 前缀（头部无条件保留）、`prefix_color` 上色 |
| test_e2e.py | 完整数据流：官方 GROUP_MESSAGE_CREATE（带图片附件）→ 群号绑定 → 图片下载缓存（本地 http server）→ BroadcastInfo → MCConnector native_chat → `tellraw @a` 系统消息命令（CICode 公网 url 优先）+ C2C 链路 |
| test_imports.py | 主入口及全部子模块导入回归（stub mcdreforged），验证 OneBot 旧路径与官方新路径共存无冲突 |
| test_migration.py | 配置兼容实证：v2.2.4 老配置（token/群号/管理员/自定义群名/模板）迁移后用户值原样保留，新键（connection.type / official / native_chat）自动补全且默认不改变旧行为 |
| test_status.py | `#状态`（WS/token/池/绑定/event_id 余量全展示）与 `#我是谁`（虚拟号/绑定/群角色）：官方模式响应、权限校验、OneBot 模式自动跳过（duck typing 判定） |
| test_send_quota.py | 额度守卫：限频重试全程复用首取的 msg_id/msg_seq（3 次尝试仅 1 次取池，不浪费被动回复额度） |
| test_reply_pool.py | 被动额度池不变量：msg_seq 0..4 递增后用尽回收、最新优先、event_id 独立计数、msg 用尽但 event 有额度时条目必须保留、drop/drop_event 语义、TTL 与 cleanup 判据一致性、池上限淘汰、add 回填 event_id；发送链路：失效 msg_id/event_id 被丢弃并换下一条、限频不误丢、C2C event_id 回退存在性、api_client 载荷带 msg_seq/event_id |
| test_rate_limit.py | REST 客户端 v2.2.9 回归：429 恰好 1 次请求（不重试）、不调用 token.invalidate()（限流≠鉴权失效）、429 错误经 `_is_rate_limited` 判为限频且不被 `_is_stale_reply_ref` 误判为失效额度；401/403 仍刷新 token 并重试一次（回归保护）；超时可配置（默认 15s，可覆盖） |
