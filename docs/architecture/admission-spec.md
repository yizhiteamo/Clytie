# 会话准入规格（B4a / B4b）

状态：用户已确认（2026-11-15）。

伴侣在一个会话里是否处理消息、是否发言，由本体统一决定。没有准入的会话完全静默。这是边界保护的一部分，属于本体，不能做成插件，也不能被停用（AGENTS.md 3.4）。

## 1. 原则

- **只用通用概念**：会话类型（`private` / `group`）、是否登记、发言人身份（`owner` / `member` / `stranger`）、成员查询。本体不按平台写分支。
- **平台差异由平台插件翻译**：平台插件把自己的会话映射成通用概念（已有的 `ctx.identity`）。需要成员信息的规则，由平台插件按可选能力回答。
- **不确定就静默**：查不到、不支持、类型不认识，一律按"不准入"处理。
- **平台信息照常传递**：`channel_id` 和 `scene` 仍随消息传给插件和配置，用于定制风格；准入规则本身不使用它们。

## 2. 配置

伴侣配置 `boundary.admission`（可选，默认 `registered`）：

| 值 | 群聊 | 私聊 |
|---|---|---|
| `test_only` | 只处理实例配置的 `test_conversation` | 同左：只有它是私聊会话时才处理 |
| `registered` | 只处理实例配置 `conversations` 中登记的群 | 处理 owner 和登记过的成员；陌生人静默 |
| `owner_present`（B4b） | 登记的群，加上 owner 当前在其中的未登记群 | 同 `registered` |

- 任何模式下，**陌生人私聊都静默**（用户决定 2026-11-15）。
- `test_only` 而实例没有 `test_conversation` 时，配置无效。
- `test_only` 下还要求发言人在 `test_conversation` 的 `members_expected` 中，不在的消息静默。
- 本体不认识的会话类型，任何模式下都静默。以后需要"公开场合"（如推特时间线）时另行设计，不扩大现有模式。
- 配置重载后立即生效。

## 3. 静默的含义

没有准入的消息：
- 在交给对话主干之前丢弃，不写入账本的入站记录，不写记忆，不触发回应、工具、表情或任务；
- 只累加计数，按原因分类：`not_test_conversation`、`not_registered`、`stranger_private`、`sender_not_expected`、`unknown_kind`、`owner_absent`（B4b）、`membership_unknown`（B4b）；
- 计数出现在伴侣的状态和本地 API 中。日志和状态不记录平台账号、平台会话号和消息内容。

## 4. 两道检查

1. **入站**：本体在 `Agent.receive` 接受事件之前检查准入（在认领入站记录之前）。
2. **出站**：本体交付层在调用平台插件的 `send` 或 `react` 之前，再按当前模式检查目标会话。不准入时不调用平台插件，收据为 `failed`，错误码 `not_admitted`（永久错误，不重发）。这一道检查覆盖回复、主动消息和任何以后新增的发送路径。

已经在等待发送的消息，如果配置改成不再准入这个会话，按出站检查拒绝。

## 5. 成员查询（B4b）

- `ChannelFeatures` 增加 `supports_membership: bool = False`。
- 支持的平台插件实现可选方法 `async is_member(conversation_id, actor_id) -> bool | None`。`None` 表示不知道。
- 本体缓存结果（默认 10 分钟）；平台插件收到成员变动时可以发出通知，让本体清除对应缓存。
- `owner_present` 模式下：结果为 `True` 才准入；`False` 记 `owner_absent`，`None`、超时或出错记 `membership_unknown`，都静默。
- 平台不支持成员查询时，配置检查给出提示：该平台账号上 `owner_present` 只对登记的群生效。
- qq-onebot 用 OneBot v11 标准接口 `get_group_member_info` 实现查询，用标准通知 `group_increase` / `group_decrease` 清除缓存。

## 6. 不做

- 自动拉取机器人所在的群列表、群成员列表和网页勾选界面（属于 WebUI）。
- 公开场合类型（如推特时间线、评论区）。
- 按工具限制使用范围：在 B4b 中另行设计 `tool_scopes`（私聊只有 owner 能用、群里可排除个别人），与准入分开。

## 7. 验收

**B4a**
- 三种模式的准入表（`owner_present` 除外）每一格都有测试，包括陌生人私聊、发言人不在 `members_expected`、未知类型。
- 静默的消息不产生入站记录、记忆、回应和模型调用；计数按原因增加；计数和日志里没有平台账号和内容。
- 出站检查：对不准入会话的回复和主动消息都不调用平台插件，收据为 `failed` / `not_admitted`；配置重载后已排队的消息也被拒绝。
- `test_only` 缺少 `test_conversation` 时配置无效。
- 端到端：假协议端在测试群和另一个群各发一条画图请求，只有测试群得到回复。

**B4b**
- 成员查询的 `True` / `False` / `None` / 超时各有测试；缓存和成员变动清除各有测试。
- 不支持成员查询的平台插件，`owner_present` 只对登记的群生效。
- `tool_scopes` 的验收在其规格中给出。
