# 测试套件

GUGUBot 官方直连改造版的自动化测试（全部独立可跑，无需真实 QQ 账号）。

运行方式（Windows，Python 3.11+ / 3.14 验证通过，需 `pip install ruamel.yaml websocket-client requests mcstatus pytest`）：

```powershell
$env:PYTHONIOENCODING = "utf-8"

# 一次性跑全部 pytest 用例（当前 108 项，约 15s）
python -m pytest tests/ -q

# 独立脚本（不由 pytest 收集，需单独执行）
python tests\test_imports.py       # 全插件导入回归（OneBot+官方双路径）
python tests\test_config_check.py  # 配置校验器独立校验
python tests\test_lang_consistency.py  # 语言包键一致性（zh_cn ↔ en_us ↔ 代码引用）
python tests\test_lang_orphans.py       # 语言包孤儿键 / 单边漏译反向检查
```

### pytest 用例（python -m pytest tests/ -q）

| 测试 | 覆盖内容 |
|---|---|
| test_behavior.py | openid↔虚拟号映射、真实群号自动绑定、持久化重载（flush 语义）；回复池（最新优先/seq 递增/用尽回退/TTL/自动清理/**过期即停**/**event_id 备用通道**）；媒体额度守卫（use_reply_pool=False 不取池）；图片占位过滤（_filter_images）；群消息解析（@bot 剥离、群号绑定、入回复池、bot 消息拦截）；未监听群拒绝；C2C 解析；消息段转文本与颜色码剥离 |
| test_ws.py | 官方 WS 协议：op:10 Hello → op:2 Identify（QQBot token 前缀、intents=1<<25、shard）→ READY（session_id）→ op:1 心跳 → op:11 ACK（本地起最小 WS 服务器全真握手） |
| test_native.py | native_chat 分支：走原版 `/tellraw @a <JSON>` 系统消息通道（`ClientboundSystemChatPacket`，不触发聊天验证错误）；`[群名] [QQ转发]名字:` 拼接、引号/反斜杠/换行转义、`show_group_prefix` 只控制可选的 `[群名] ` 前缀（头部无条件保留）、`prefix_color` 上色 |
| test_e2e.py | 完整数据流：官方 GROUP_MESSAGE_CREATE（带图片附件）→ 群号绑定 → 图片下载缓存（本地 http server）→ BroadcastInfo → MCConnector native_chat → `tellraw @a` 系统消息命令（CICode 公网 url 优先）+ C2C 链路 |
| test_migration.py | 配置兼容实证：v2.2.4 老配置（token/群号/管理员/自定义群名/模板）迁移后用户值原样保留，新键（connection.type / official / native_chat）自动补全且默认不改变旧行为 |
| test_status.py | `#状态`（WS/token/池/绑定/event_id 余量全展示）与 `#我是谁`（虚拟号/绑定/群角色）：官方模式响应、权限校验、OneBot 模式自动跳过（duck typing 判定） |
| test_send_quota.py | 额度守卫：限频重试全程复用首取的 msg_id/msg_seq（3 次尝试仅 1 次取池，不浪费被动回复额度） |
| test_reply_pool.py | 被动额度池不变量：msg_seq 0..4 递增后用尽回收、最新优先、event_id 独立计数、msg 用尽但 event 有额度时条目必须保留、drop/drop_event 语义、TTL 与 cleanup 判据一致性、池上限淘汰、add 回填 event_id；发送链路：失效 msg_id/event_id 被丢弃并换下一条、限频不误丢、C2C event_id 回退存在性、api_client 载荷带 msg_seq/event_id |
| test_rate_limit.py | REST 客户端 v2.2.9 回归：429 恰好 1 次请求（不重试）、不调用 token.invalidate()（限流≠鉴权失效）、429 错误经 `_is_rate_limited` 判为限频且不被 `_is_stale_reply_ref` 误判为失效额度；401/403 仍刷新 token 并重试一次（回归保护）；超时可配置（默认 15s，可覆盖） |
| test_split_message.py | 长消息分片：按 `max_message_length` 切分、不丢字不重叠、空文本/单字符边界、超长单段不被吞掉 |
| test_config_numeric.py | 数值型配置的入口钳制：`max_message_length` ≤0 被夹为正整数（避免分片死循环）、限频/超时等字段的取值范围与类型兜底 |
| test_config_reload.py | `#重载配置` 热重载：系统/连接器 `reload_config()` 钩子被调用、`enable` 状态重同步、钩子返回值并入反馈、重载后自动跑一遍语义校验 |
| test_member_list_unsupported.py | 官方模式无 `get_group_member_list`：平台返回 `unsupported` 标记、`data is None` 与空集严格区分、宽松模式（未配置监听群）/连接器未就绪/全部查询失败时返回不可用、局部失败仍返回已收集集合、清理类命令在不可用时拒绝执行 |
| test_is_admin_cache.py | 管理员判定缓存：命中缓存只发一次请求、TTL 过期后重新拉取、失败结果也缓存（避免 WS 掉线时刷屏）、`unsupported` 平台不缓存 |
| test_id_map_slots.py | 真实群号槽位不被重复占用：已绑定群号重启后不再入待绑定队列、已有绑定不被新群覆盖、部分绑定场景、重启后映射保持、配置直填 openid 的自映射不进反向表 |
| test_id_map_concurrency.py | `to_legacy_group` 并发不变量（同群不得拿到两个 legacy 号）——**注**：受 GIL 与窄窗口影响，无法稳定复现该竞态，属防御性加固 |
| test_id_map_known_groups.py | 群集合语义：用户直填 openid 的群计入 `known_legacy_groups()`/`known_group_openids()`、不占用真实群号槽位；`#状态` 回显「直填 openid 群」且群数含该项 |
| test_reply_routing.py | 回复路由：虚拟号与 openid 原文均可作为回复 target key、空值不可路由、回复目标用 openid、空值回退到 origin |
| test_echo_admin_group.py | echo「管理群消息不广播」：OneBot 链路（无 id_mapper）按真实群号字符串比对、管理群消息被拦下、普通群照常广播；官方链路的虚拟号/真实群号两种形态均被拦下 |
| test_bound_notice_admin_group.py | bound_notice「管理群不发未绑定提醒」：与 echo 同源缺陷的第二个实例，OneBot 链路管理群拦截、普通群正常提醒、官方链路两种群号形态均拦截 |
| test_general_help_placeholders.py | `#帮助` 的模板占位符：管理员与非管理员两分支传入的占位符集合完全一致（非管理员分支曾缺 11 个）、含 `official_status`/`whoami`、含全部系统名占位符 |
| test_official_admin_scope.py | 官方直连管理群上下文**不得泄漏到 C2C 单聊**（越权误判）：单聊显式 `source_id=None`、`admin_ids` 列表用户单聊仍是管理员、管理群内用户群消息仍是管理员、非管理群用户不是管理员、不再使用粘性群上下文字段 |
| test_token_manager_concurrency.py | `QQTokenManager` 并发：401 触发的 `invalidate()` 只作废「本次实际使用的那枚」token（不误伤别处刚刷新的）、8 并发只触发 1 次刷新（单飞）、刷新进行中 `invalidate()` 不被阻塞（不持锁做 I/O） |

### 独立脚本（不由 pytest 收集）

| 测试 | 覆盖内容 |
|---|---|
| test_imports.py | 主入口及全部子模块导入回归（stub mcdreforged），验证 OneBot 旧路径与官方新路径共存无冲突 |
| test_config_check.py | 配置校验器的独立校验入口（语法 + 语义各项） |
| test_bound_subcommand_i18n.py | 绑定系统管理员子命令的 i18n：命令字必须来自语言包（`get_tr`）而非硬编码；zh_cn / en_us 的值与旧硬编码**逐一比对一致**（行为不变）；源码不再出现中英双语命令字列表 |
| test_lang_consistency.py | 语言包键一致性：静态扫描源码里所有 `get_tr("...")` 与 `BasicSystem.__init__` 注册名，逐一确认 zh_cn / en_us 都有对应条目，并比对两个语言文件的 system 段键集合完全一致。key 收集覆盖**动态形式**（变量赋值式 / 三元式 / 元组式 / 嵌套调用）。**防回归重点**——MCDR 的 `server.tr()` 找不到 key 时返回 key 字符串本身（不报错），键名错位会让命令前缀变成一串 key 路径 → 命令**静默失效**且无任何日志 |
| test_lang_orphans.py | 语言包孤儿键反向检查：找出「两边都有但代码从不引用」的死键，并互查两个语言包的单边漏译（zh 有 en 无 / en 有 zh 无） |
| diagnose_config.py | 配置问题诊断辅助脚本（人工排查用） |
