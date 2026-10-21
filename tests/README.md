# 测试套件

GUGUBot 官方直连改造版的自动化测试（全部独立可跑，无需真实 QQ 账号）。

运行方式（Windows，Python 3.11+ / 3.14 验证通过，需 `pip install ruamel.yaml websocket-client requests mcstatus pytest`）：

```powershell
$env:PYTHONIOENCODING = "utf-8"

# 一次性跑全部 pytest 用例（当前 285 项，约 50s；含 11 个独立脚本的自动执行）
python -m pytest tests/ -q

# 也可以直接 `python -m pytest`（仓库根有 pytest.ini，testpaths=tests）
# —— 否则 pytest 会去收集 plugin/ 下的 test_connector.py，它 import
#    mcdreforged 失败 → 收集阶段直接中断，整轮测试一条都不跑。

# 独立脚本（不由 pytest 收集，需单独执行；现已被 test_standalone_scripts.py 自动纳入回归网）
python tests\test_imports.py       # 全插件导入回归（OneBot+官方双路径）
python tests\test_config_check.py  # 配置校验器独立校验
python tests\test_lang_consistency.py  # 语言包键一致性（zh_cn ↔ en_us ↔ 代码引用）
python tests\test_lang_orphans.py       # 语言包孤儿键 / 单边漏译反向检查
```

### pytest 用例（python -m pytest tests/ -q）

| 测试 | 覆盖内容 |
|---|---|
| test_behavior.py | openid↔虚拟号映射、真实群号自动绑定、持久化重载（flush 语义）；回复池（最新优先/seq 递增/用尽回退/TTL/自动清理/**过期即停**/**event_id 备用通道**）；媒体额度守卫（use_reply_pool=False 不取池）；图片占位过滤（_filter_images）；群消息解析（@bot 剥离、群号绑定、入回复池、bot 消息拦截）；未监听群拒绝；C2C 解析；消息段转文本与颜色码剥离 |
| test_ws.py | 官方 WS 协议：op:10 Hello → op:2 Identify（QQBot token 前缀、intents=1<<25、shard）→ READY（session_id）→ op:1 心跳 → op:11 ACK（本地起最小 WS 服务器全真握手，**随机端口 + 条件等待**，不依赖固定端口与固定 sleep） |
| test_ws_close_codes.py | **WS 关闭码语义 + Resume 位点分离（v2.2.10 新增，18 项）**：官方错误码 4006/4007/4008/4009/4900-4913 命中即清空 `session_id`/`last_seq`/`_acked_seq`（否则带废 session 重连 → 服务端再拒 → **永久离线**）；4914（已下架）/4915（封禁）必须停止重连（否则每 3s 刷屏）；4010-4013 停止重连；4014 保持重试（等用户开通能力）；`_on_open` 清 `_last_close_code`（曾遇 4014 的连接后续不再白等 60s）；**Resume 的 seq 用处理完成的 `_acked_seq` 而非收到的 `last_seq`**（官方要求「处理过事件之后记录 s」→ 处理失败的事件不得被跳过，否则永久丢失）；回调异常重抛但不影响 WS 读循环 |
| test_source_name_custom.py | **自定义 `connector.QQ.source_name` 的全链路适配（v2.2.10 新增，11 项）**：绑定写入 PlayerManager 的平台键是 `source.origin`（= 用户自定义的 source_name，默认 "QQ"），而早期实现多处硬编码 `"QQ"` → 用户改名后**解绑无效、查询查不到、多余绑定检查形同虚设、群名片静默失效**；`echo.py` 硬编码 `is_from("QQ")` → **QQ 私聊被广播到 MC**；两个 QQ 连接器硬编码 `is_from("QQ")` → **转发消息多出 `[来源] 发送者:` 前缀**。现统一走 `_qq_platform_keys()`（当前键 + 兼容历史 "QQ" 键）；另删除 `parser._mentions_to_segments` 死代码（把 openid 原文塞进 at 段，与原位展开的虚拟号不一致 → 同一用户两条绑定） |
| test_vote_eligible.py | **投票资格名单的两处缺陷（v2.2.10 新增，9 项）**：遍历在线玩家时只要有人**没绑定过**（服务器上的路人）就 `return set()` → **整个资格名单作废**，任何人发起投票被拒、管理员绕过发起后**所有人投票仍被拒**（docstring 明写的是「在线 **且** 已绑定」）；另只查当前 source_name 单键 → 历史 `"QQ"` 键的绑定在投票资格里消失。含 QQ 侧/MC 侧投票者 ID 解析回归 |
| test_basic_config_atomic.py | **BasicConfig 原子写（v2.2.10 新增，6 项）**：`BasicConfig` 是**所有**持久化系统的基类（玩家绑定/待办/违禁词/关键词/白名单/未绑定检查…），旧 `save()` 直接 `open("w")` 覆盖写 → 写入中途崩溃/断电/磁盘满 → **半截文件且旧数据已被截断，无法恢复**。现改为「先写 .tmp 再 `os.replace`」，同文件系统上原子替换 → 要么旧文件完整、要么新文件完整 |
| test_player_manager_source_name.py | **PlayerManager 对自定义 source_name 的适配（v2.2.10 新增，11 项）**：`player.accounts` 的平台键是 `connector.QQ.source_name`（用户可自定义，默认 "QQ"）。① `get_player(..., platform=)` 按键**精确匹配** → 改名前的旧绑定（历史 "QQ" 键）查不到 → 系统误判「没绑过」→ **允许重复绑定**；② `is_admin()` 遍历 `connectors.items()`，其键是**配置节名**（恒为 "QQ"），旧实现却拿它与 source_name 比较 → 改名后恒为 False → **QQ 管理员分支整个被跳过、管理员判定失效**。现 QQ 平台的查询/判定都遍历「source_name 当前键 + 历史 "QQ" 键」，非 QQ 平台语义不变 |
| test_bound_limit_source_name.py | **绑定上限与不活跃检查的单键查询（v2.2.10 新增，7 项）**：`bound.py` 绑定上限只查当前键 → 旧绑定不计入 → **可借改名绕过绑定上限**；`inactive_check.py` 只查当前键 → 历史键下的玩家**被漏检**（不活跃却不被清理/通知）。现两处都遍历「当前 source_name + 历史 "QQ" 键」 |
| test_bound_unbind_source_name.py | **解绑权限/重复绑定/退群清理的单键查询（v2.2.10 新增，13 项）**：① 解绑权限检查只查当前键 → 改名前的旧绑定查不到 → **绑定者自己无法解绑**（非管理员被卡住）；② 重复绑定判重旧代码拿 **MC 玩家名**去比 **QQ 账号列表**（不同命名空间，几乎恒为 True）且只查当前键 → 改名前绑过的 QQ 号被**再绑一次**（两个键各存一份，解绑只能删一半）；③ 退群清理判断"其他平台绑定"时硬编码 `platform != "QQ"` → 改名后 QQ 绑定被误判成"其他平台"→ 残留**僵尸绑定**（玩家名留着、QQ 号没了）。现抽出 `_get_player_qq_accounts` / `_can_unbind` / `_is_account_bound` / `_has_other_platform_accounts` 四个方法 |
| test_bound_empty_command.py | **bound 在官方 @机器人 消息形态下的回归（v2.2.10 新增，9 项）**：QQ 官方直连里 @机器人 时消息段是 `[at 段, text 段]`（长度 2），OneBot 是单段 → 旧代码用 `len(broadcast_info.message) == 1` 判「空命令」在官方模式下恒为 False → 空命令绕过 `_handle_command` 的帮助分支、掉进 `_handle_bind`，最终回的是 `bind_instruction`（用法提示）；而 OneBot 模式回的是 `user_help_msg`（系统帮助）→ **两种平台给用户的提示不一致**。现空命令判定只看「拼接后的命令文本是否为空」，两平台统一回系统帮助；并覆盖 @别人 时绑定到被 @ 的人、只有 at 段无文本时静默忽略、普通聊天不误触发 |
| test_qq_official_send_resilience.py | **官方发送链路的边界加固（v2.2.10 新增，16 项）**：① `_send_group`/`_send_c2c` 用 `for attempt in range(3)` → **第 3 轮才遇到「被动引用失效」时，换到的新额度因轮次耗尽而根本没被用过**（本次发送直接失败）；改为 `while` + 独立的换额度计数（换额度不消耗轮次，但有上限防死循环）；② `reply_pool.take()` 的过期回收用 `keys.index()` 线性查找 → 池满 256 条时退化成 O(n²)，改为一次 `enumerate` 遍历；③ `reply_pool.take_event()` 清理条目用 `del`（键不存在会 KeyError）→ 改 `pop(..., None)`；④ `api_client` 鉴权重试没透传 `_net_attempt` → 网络重试次数被重置、最坏请求数翻倍；⑤ `api_client._seq_lock` 是**从未使用的死代码**却误导读者以为 msg_seq 分配有锁保护（实际由 reply_pool 的 RLock 保护）→ 删除 |
| test_qq_official_parser_hardening.py | **官方 parser 的日志去重与附件下载加固（v2.2.10 续，8 项）**：① 「本群没有可用配置槽位」和「该群不在监听列表」两条 **info** 日志在「条件持续成立」时**每条消息都会打一遍**（群里正常聊天频率 → 每条消息刷一行，生产环境直接写盘）→ 改为**每群只提示一次**（绑定成功后解除去重）；② 附件 URL 取 `url`（**明文 http**）优先于 `url_https` → 下载可被中间人劫持/篡改 → 改为优先 https；③ 附件用 `requests.get()` + `resp.content` **一次性把整个文件读进内存** → 异常大文件打爆内存（缓存上限是**下次**下载才清理，救不了当次）→ 改为 `stream=True` + 分块写入 + Content-Length 预检 + 单文件 32MB 上限 + `.part` 半成品清理；④ 下载失败时若仍返回本地路径 → 下游拿到**不存在的路径**必然 404 → 改为返回 None（只给 url） |
| test_id_map_repair.py | **ID 映射脏数据自愈（v2.2.10 续，8 项）**：旧版 `_load` 在文件缺 `next_user`/`next_group` 时会让计数器回落到起始号，而映射表里早已占用这些号 → 后续新 openid 被分到**重复虚拟号**。项目此前只加了「按已有最大号 +1 兜底」防止**将来**再撞号，但**已经落盘的脏数据**从未修复——`self._user_rev = {v: k for k, v in self._user_map.items()}` 是「后写覆盖先写」，冲突时反向表只保留**最后一个** openid → 其余 openid 的虚拟号指向**别人** → 下游按虚拟号解析真实 openid 时**消息发给错的人**，全程无报错无日志。现加载时先修复冲突（先到者保留原号、后到者重新分配）**再**建反向表，并把修复结果立即落盘（否则每次启动重跑、期间消息仍会发错人） |
| test_qq_official_token_credential.py | **官方 token 凭证判定与 C2C 富媒体兼容（v2.2.10 续，14 项）**：① 「凭证错误不重试」的判定靠 `str(err)` 子串匹配 `401`/`403`/`invalid` → **漏判**（官方 4xx 的中文原因如「appid不合法」不含这些词 → 白重试一次，多耗 1s + 一次网络往返 + 限流风险）且**误判**（URL/参数里恰好带 "401" 子串 → 本该重试的网络抖动被当成凭证错误直接放弃）→ 改为优先读 `err.response.status_code`，取不到才退回关键词（429 明确排除；401/403 要求独立出现，不被 URL 片段命中）；② `raise_for_status()` **丢掉响应体** → 只剩一句 `400 Client Error: Bad Request for url: ...`，排障时看不出是凭证错了还是接口变了 → 改为把官方 body 一并带进异常；③ C2C 富媒体与群聊富媒体的 `file_info` 判定**不一致**（群聊兼容「返回体整个就是 file_info 包装」，C2C 只取 `data["file_info"]`）→ 同一份返回在群聊能发、单聊报「富媒体上传返回异常」；现抽成共用的 `_extract_file_info()`，并把「空串 / 空 dict」一律判为无效（否则会拼出 `media.file_info` 为空的富媒体消息） |
| test_qq_official_ws_session.py | **官方 WS 会话失效与回调签名探测（v2.2.10 续，18 项）**：① op:9（INVALID_SESSION）的 `d` 判定写的是 `d_val is False` → 只认**恰好是布尔 False** 的对象，官方若下发字符串 `"false"` / 数字 `0` 则**漏判** → 走「保留 session 等下个 Hello 重试 Resume」→ 拿已被官方判死的 session 反复 Resume → 死循环；现按「显式假值」统一判定；② op:9 后**只清字段不重连** → Identify 永远发不出去（Identify 只在新连接的 Hello 之后发）→ 连接看似还在、实则已死，只能等心跳失活兜底（官方心跳间隔最长 300s → 最坏离线近 10 分钟）；现 op:9 主动触发重连；③ 回调签名探测靠「先试 4 参、捕获 TypeError 再退回 3 参」→ 回调**内部**的业务 TypeError（文案含 "argument"/"positional"）会被误判成旧签名 → **同一事件被处理两次**；现改用 `inspect.signature` 静态求值，不依赖任何异常 |
| test_native.py | native_chat 分支：走原版 `/tellraw @a <JSON>` 系统消息通道（`ClientboundSystemChatPacket`，不触发聊天验证错误）；`[群名] [QQ转发]名字:` 拼接、引号/反斜杠/换行转义、`show_group_prefix` 只控制可选的 `[群名] ` 前缀（头部无条件保留）、`prefix_color` 上色 |
| test_e2e.py | 完整数据流：官方 GROUP_MESSAGE_CREATE（带图片附件）→ 群号绑定 → 图片下载缓存（本地 http server）→ BroadcastInfo → MCConnector native_chat → `tellraw @a` 系统消息命令（CICode 公网 url 优先）+ C2C 链路 |
| test_migration.py | 配置兼容实证：v2.2.4 老配置（token/群号/管理员/自定义群名/模板）迁移后用户值原样保留，新键（connection.type / official / native_chat）自动补全且默认不改变旧行为 |
| test_status.py | `#状态`（WS/token/池/绑定/event_id 余量全展示）与 `#我是谁`（虚拟号/绑定/群角色）：官方模式响应、权限校验、OneBot 模式自动跳过（duck typing 判定） |
| test_send_quota.py | 额度守卫：限频重试全程复用首取的 msg_id/msg_seq（3 次尝试仅 1 次取池，不浪费被动回复额度） |
| test_reply_pool.py | 被动额度池不变量：msg_seq 0..4 递增后用尽回收、最新优先、event_id 独立计数、msg 用尽但 event 有额度时条目必须保留、drop/drop_event 语义、TTL 与 cleanup 判据一致性、池上限淘汰、add 回填 event_id；发送链路：失效 msg_id/event_id 被丢弃并换下一条、限频不误丢、C2C event_id 回退存在性、api_client 载荷带 msg_seq/event_id |
| test_rate_limit.py | **官方 REST 客户端错误语义（v2.2.10 重写）**：429 恰好 1 次请求（不重试）、不调用 token.invalidate()（限流≠鉴权失效）、429 经 `_is_rate_limited` 判为限频且不被 `_is_stale_reply_ref` 误判为失效额度；401/403 与**业务鉴权码**（11241/11242/11243，常以 HTTP 200 返回）均刷新 token 并重试一次；**HTTP 200 里的 `err_code` 业务错误必须抛异常**（官方规定字段名是 `err_code` 而非 `code`，且失败时 HTTP 状态码可能仍是 200——此前全部被当成发送成功）；`err_code=0` 视为成功；无 err_code 的成功体原样返回；官方失效码/频控码分类正确；**未收录的失效码在 HTTP 200 下仍能靠文案兜底识别**；超时可配置（默认 15s，可覆盖）<br>⚠️ 此文件此前**从未真正执行过**（无模块级 `test_*` → pytest 收集不到；桩挂在 `requests.request` 而实现走 `self._session.request` → 桩未生效；`FakeToken.invalidate` 签名不符 → 直接运行 TypeError），已重写为标准 pytest 用例 |
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
| test_standalone_scripts.py | **把独立脚本纳入 pytest 回归网**（v2.2.10 新增）：用 `subprocess` 逐个执行 11 个独立脚本，断言退出码为 0 且打印各自的成功标志；并检查「仓库里新增的 `test_*.py` 若既没有模块级 `def test_*`、也没登记到 `STANDALONE_SCRIPTS`」→ 直接失败（防止再出现「写了个测试文件但永远不会被执行」） |

### 独立脚本（不由 pytest 收集，但已被 `test_standalone_scripts.py` 自动执行）

| 测试 | 覆盖内容 |
|---|---|
| test_imports.py | 主入口及全部子模块导入回归（stub mcdreforged），验证 OneBot 旧路径与官方新路径共存无冲突 |
| test_config_check.py | 配置校验器的独立校验入口（语法 + 语义各项） |
| test_bound_subcommand_i18n.py | 绑定系统管理员子命令的 i18n：命令字必须来自语言包（`get_tr`）而非硬编码；zh_cn / en_us 的值与旧硬编码**逐一比对一致**（行为不变）；源码不再出现中英双语命令字列表 |
| test_lang_consistency.py | 语言包键一致性：静态扫描源码里所有 `get_tr("...")` 与 `BasicSystem.__init__` 注册名，逐一确认 zh_cn / en_us 都有对应条目，并比对两个语言文件的 system 段键集合完全一致。key 收集覆盖**动态形式**（变量赋值式 / 三元式 / 元组式 / 嵌套调用）。**防回归重点**——MCDR 的 `server.tr()` 找不到 key 时返回 key 字符串本身（不报错），键名错位会让命令前缀变成一串 key 路径 → 命令**静默失效**且无任何日志 |
| test_lang_orphans.py | 语言包孤儿键反向检查：找出「两边都有但代码从不引用」的死键，并互查两个语言包的单边漏译（zh 有 en 无 / en 有 zh 无） |
| diagnose_config.py | 配置问题诊断辅助脚本（人工排查用，需传配置路径） |

> ⚠️ **两类测试文件必须分清**：有模块级 `def test_*` 的被 pytest 直接收集；写成「顶层脚本 + `print("PASS")`」的**永远不会被 `pytest tests/` 执行**——历史上 `test_rate_limit.py` 就是这样长期「假通过」的（详见上表）。现在所有独立脚本已登记进 `test_standalone_scripts.py`，新增时请同步登记。

> **路径约定**：`tests/` 下的脚本用 `sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'plugin'))` 引入插件源码（基于 `__file__` 推导）。此前 25 个文件硬编码了 `F:\vibe coding工程\gugubot-qq-ws\plugin`，换机器/换目录会全部 ImportError，已全部改掉。
