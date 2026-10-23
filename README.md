# GUGUBot（增强改造版）

一个 MCDReforged 插件，实现 **Minecraft 服务器 ↔ QQ 群** 双向互通。

本仓库是上游 [PF-GUGUBot](https://github.com/PFingan-Code/PF-GUGUBot) 的**增强改造版**，
在 v2.2.4 基础上迭代到 **v2.2.10**，新增两大能力：

1. **QQ 官方开放平台原生 WS 直连** —— 填 AppID / AppSecret 即可直连官方机器人，
   无需 NapCat / Lagrange / Gensokyo 等中间层；内置 Gensokyo 式
   `msg_id` + `event_id` 双通道被动回复池，绕开官方主动消息限额。
2. **系统消息通道注入** —— QQ→MC 消息以 `[QQ转发]名字:消息` 的纯文本形式经原版
   `/tellraw`（系统消息包）送入聊天框。系统消息**不做聊天签名验证**，
   离线 / 未签名服务器不会再出现 `聊天验证错误`。**无需安装任何 Mod。**

旧 OneBot 配置**完全兼容**：`connection.type` 不填或填 `onebot` 时行为与原版一致；
填 `official` 切换官方直连。

---

## 快速开始

1. 下载最新的 `.mcdr` 包（见 [Releases](https://github.com/szkele1145/gugubot-qq-ws/releases)），
   放进 MCDR 的 `plugins/` 目录。
2. 启动服务器，插件会自动生成 `config.yml`，按注释填写 QQ 连接方式。
3. 群内发送 `#帮助` 查看可用命令。

> Mod 侧：使用系统消息通道时**无需安装 Mod**。
> 若服务器已装 GuguChat（1.21.1 NeoForge）也可继续使用，其
> `/guguchat status` 仍可查询状态。

---

## 功能特性

| 分类 | 能力 |
| --- | --- |
| 聊天 | MC ↔ QQ 双向转发，图片 / 表情 / 媒体，`@` 原位展开，多服互联 |
| 绑定 | QQ ↔ 游戏 ID 绑定（Java / 基岩版），退群自动解绑，绑定上限 |
| 权限 | 管理员判定、群白名单、远程执行 MC / MCDR 命令 |
| 管控 | 白名单管理、违禁词过滤（自动撤回）、关键词自动回复 |
| 运维 | 在线玩家查询、不活跃玩家检查、未绑定用户检查、待办管理 |
| 官方模式 | 虚拟号映射（用户 `100000xxx` / 群 `200000xxx`）、双通道被动回复池、`#状态` 余量展示 |

---

## 仓库结构

```
plugin/                插件本体（打包成 .mcdr 的内容）
  gugubot/connector/   连接器：OneBot / 官方 QQ / MC / 桥接 / 测试
  gugubot/connector/qq_official/
                       官方直连：ws_client / api_client / parser /
                       id_map（虚拟号映射）/ reply_pool（双通道额度池）/
                       token_manager
  gugubot/logic/       系统逻辑：绑定 / 回声 / 投票 / 待办 / 违禁词 …
  gugubot/logic/plugins/  插件式系统：不活跃检查 / 未绑定检查 / 活跃白名单
  gugubot/parser/      消息解析器
  gugubot/ws/          WebSocket 服务端
  lang/                多语言（zh_cn / en_us）
tests/                 pytest 用例（当前 491 项）
guguchat-mod/          GuguChat Mod 源码（可选，非必需）
改造说明.md            逐条缺陷修复与设计决策记录
修复模式索引.md        按成因归类的修复模式（87 个修复归纳成十几种模式）
交接文档.md            全量交接文档（零上下文接手用）
通宵战报.md            迭代战报
```

---

## 开发与测试

```bash
# 安装依赖
pip install -r plugin/requirements.txt

# 跑全部用例（约 97 秒）
py -m pytest tests/ -q

# 语法自检
py -m compileall -q plugin/gugubot
```

测试覆盖 OneBot 兼容层、官方 WS 协议、虚拟号映射、回复池额度语义、
并发（线程 / 事件循环）边界、配置与持久化等。

---

## 文档

- 详细功能与配置说明 → [`plugin/README.md`](plugin/README.md)
- 缺陷修复与设计决策 → [改造说明.md](改造说明.md)
- **修复模式索引（推荐先读）→ [修复模式索引.md](修复模式索引.md)**
- 迭代记录 → [`通宵战报.md`](通宵战报.md)

---

## 致谢

- 上游：[PF-GUGUBot](https://github.com/PFingan-Code/PF-GUGUBot)（原作者 XueK__ / LoosePrince）
- [MCDReforged](https://github.com/MCDReforged/MCDReforged)
- 官方 QQ 机器人开放平台
