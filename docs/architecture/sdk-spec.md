# SDK 规格 v0.1

本文件是 `sdk.md` 的实现规格：数据类型字段、取消与时钟协议、扩展点签名、指令合并语义。配置与 Schema 见 `config-spec.md`。

实现库（dataclasses / pydantic 等）由实现者选择并写 ADR，但字段名、类型、是否必填、取值范围以本文件为准。

## 0. 通用约定

- 版本：SDK 版本从 `0.1.0` 开始。`0.x` 阶段次版本可以有不兼容变更（写入 `CHANGELOG.md`）；架构阶段结束时发布 `1.0.0`，之后按 `sdk.md` 的版本策略。
- 所有数据类型不可变（frozen），可以无损地序列化为 JSON 并还原。
- 时间：带时区的 `datetime`，序列化为 ISO 8601，内部一律 UTC。
- 标识符一律为字符串：
  - `trace_id`：32 位小写十六进制（uuid4）
  - `task_id`：8 位小写十六进制，由账本查重分配（A3）
  - `event_id`：`"{channel_id}:{platform_message_id}"`，用作入站去重键
  - `actor_id`：逻辑身份，来自实例配置（如 `owner`）；未登记的人为 `"anon:{channel_id}:{sha256(平台账号)[:16]}"`
  - `asset_id`：内容的 SHA-256（64 位十六进制）
- 枚举用小写字符串。
- 可选字段缺省为 `None`；列表缺省为空列表，不用 `None`。
- 平台的原始数据不进入 SDK 类型，只保留一个不透明的 `raw_ref`（字符串，平台插件自己能解析）。

## 1. 数据类型（`sdk.types`）

### 1.1 身份与会话

**Actor**
| 字段 | 类型 | 说明 |
|---|---|---|
| `actor_id` | str | 必填 |
| `role` | `owner` \| `peer` \| `member` \| `stranger` | 必填；`peer` = 同部署的其他伴侣 |
| `display_name` | str? | 平台上的昵称（不可信，不用于判断身份） |
| `channel_id` | str | 来源平台插件实例 |

**Conversation**
| 字段 | 类型 | 说明 |
|---|---|---|
| `conversation_id` | str | 逻辑名（实例配置中登记的）或 `"anon:{channel_id}:{hash}"` |
| `kind` | `private` \| `group` | |
| `scene` | str | 场景名，来自实例配置（如 `owner_private`、`family_group`、`public_group`）；未登记的群为 `public_group`，未登记的私聊为 `stranger_private` |
| `channel_id` | str | |

### 1.2 消息

**MessagePart**（带 `type` 字段的联合类型）
| type | 字段 |
|---|---|
| `text` | `text: str` |
| `mention` | `actor_id: str`，`is_self: bool` |
| `image` / `audio` / `video` / `file` | `asset: AssetRef?`，`raw_ref: str?`（尚未下载时） |
| `link` | `url: str`，`title: str?` |
| `quote` | `message_ref: MessageRef` |
| `sticker` | `sticker_id: str`，`asset: AssetRef?` |
| `card` | `title: str`，`body: str`（发送用：阅读卡片） |
| `unknown` | `raw_type: str` |

**MessageRef**：`channel_id`、`conversation_id`、`platform_message_id`。

**Message**：`ref: MessageRef`、`sender: Actor`、`parts: list[MessagePart]`、`sent_at: datetime`、`reply_to: MessageRef?`。

### 1.3 事件

**EventEnvelope**
| 字段 | 类型 | 说明 |
|---|---|---|
| `event_id` | str | 去重键 |
| `type` | str | `conversation.message`、`conversation.notice`、`conversation.request`（好友/入群申请）；`content.*` 预留 |
| `companion_id` | str | 接收这条事件的伴侣 |
| `channel_id` | str | |
| `conversation` | Conversation | |
| `message` | Message? | `conversation.message` 时必填 |
| `notice` | dict? | 其他类型的结构化内容（键名由 SDK 列出） |
| `received_at` | datetime | 来自 `ctx.clock` |
| `raw_ref` | str? | |

### 1.4 交付

**DeliveryIntent**
| 字段 | 类型 | 说明 |
|---|---|---|
| `intent_id` | str | 32 位十六进制 |
| `companion_id` / `conversation_id` / `channel_id` | str | |
| `kind` | `reply` \| `proactive` \| `react` | |
| `parts` | list[MessagePart] | `react` 时为空 |
| `emoji` | str? | `react` 时必填 |
| `target` | MessageRef? | 引用或表情回应的目标 |
| `task_id` | str? | |
| `trace_id` | str | |
| `split` | `{group_id: str, index: int, total: int}`? | 拆分发送 |
| `not_before` | datetime? | 拆分间隔用 |

**Receipt**
| 字段 | 类型 | 说明 |
|---|---|---|
| `intent_id` | str | |
| `status` | `sent` \| `failed` \| `unknown` | `unknown` = 无法确认是否送达；**本体对 `unknown` 永不重试** |
| `platform_message_id` | str? | |
| `at` | datetime | |
| `error` | `{code: str, message: str}`? | |

平台插件只有在确定"没有发出"时才能返回 `failed`；超时、断线等不确定情况必须返回 `unknown`。

### 1.5 模型

**ChatMessage**：`role: system|user|assistant|tool`、`parts: list[MessagePart]`、`tool_calls: list[ToolCall]`（仅 assistant）、`tool_call_id: str?`（仅 tool）。

**ToolSpec**：`name`、`description`、`when_not_to_use`、`examples: list[{input: str, call: bool, note: str?}]`、`parameters: dict`（JSON Schema）。

**ToolCall**：`call_id: str`、`tool: str`、`arguments: dict`。

**ModelRequest**：`messages: list[ChatMessage]`、`tools: list[ToolSpec]`、`budget: Budget`、`trace_id`。

**Budget**：`deadline: datetime`（必填）、`max_output_tokens: int?`、`max_tool_rounds: int`（默认 4）。

**ModelTurn**
| 字段 | 类型 |
|---|---|
| `text` | str? |
| `tool_calls` | list[ToolCall] |
| `finish` | `stop` \| `tool_calls` \| `cancelled` \| `error` \| `deadline` |
| `usage` | `{input_tokens: int?, output_tokens: int?}` |
| `quota` | QuotaSnapshot? | 本次调用顺带得到的额度信息 |
| `error` | `{code, message}`? |

**QuotaSnapshot**：`windows: list[{name: str, remaining_ratio: float(0–1), resets_at: datetime?}]`、`observed_at`、`source: str`。拿不到时返回常量 `QUOTA_UNKNOWN`。

### 1.6 能力

**Invocation**（能力执行时的调用信息）：`companion_id`、`actor: Actor`、`conversation: Conversation`、`trace_id`、`task_id: str?`。

**Plan**：`summary: str`（写入账本）、`heavy: bool`、`data: dict`（插件自用）。

**Denial**：`reason_code: str`、`message_for_model: str`（告诉模型为什么不能做）。

**ToolResult**
| 字段 | 类型 | 说明 |
|---|---|---|
| `call_id` | str | |
| `status` | `ok` \| `error` \| `denied` \| `cancelled` | |
| `content` | str | 给模型看的结果摘要 |
| `assets` | list[AssetRef] | |
| `data` | dict | 结构化结果（写入账本） |
| `error` | `{code, message}`? | |

**AssetRef**：`asset_id`、`media_type`、`size: int`、`origin: str`（来源说明）。

### 1.7 记忆

**MemoryRecord**
| 字段 | 类型 | 说明 |
|---|---|---|
| `record_id` | str | 32 位十六进制 |
| `revision` | int | 从 1 开始 |
| `kind` | `fact` \| `preference` \| `event` \| `episode` \| `profile` \| `jargon` | |
| `content` | str | |
| `subject` | str? | 相关的 `actor_id` |
| `visibility` | `private` \| `public` \| `relationship` | |
| `write_mode` | `auto` \| `confirmed` | |
| `source_type` | `private` \| `group` \| `owner_group_interaction` | |
| `source_ref` | str? | 账本追踪号 |
| `scope` | str? | 黑话等按群隔离的记录所属的 `conversation_id` |
| `created_at` / `updated_at` | datetime | |
| `decay` | `{weight: float(0–1), frozen: bool}` | |
| `tags` | list[str] | |
| `retracted` | bool | 撤销后保留记录 |

**MemoryQuery**：`text`、`scene`、`allowed_visibility: list`（由本体填写，插件不能改）、`scope: str?`、`limit: int`。

**MemoryHit**：`record_id`、`score: float`、`source: str`（哪个检索器）。

### 1.8 其他

**BusEvent**：`name`、`payload: dict`、`at`、`source`（发布者：`core` 或插件 id）。
**Notice**（通知）：`level: info|warn|error`、`title`、`body`、`dedupe_key`。
**Tick**：`schedule_id`、`scheduled_at`、`now`、`missed: int`。

## 2. 取消协议

`CancelToken`：
- `cancelled: bool`、`reason: str?`
- `raise_if_cancelled()`：已取消时抛出 `Cancelled`
- `await wait()`：等待取消发生
- `child()`：派生子令牌（父取消时子也取消）

规则：
- 所有可能耗时的接口都接受 `cancel: CancelToken`，插件在耗时步骤之间检查它。
- 插件**不能吞掉** `asyncio.CancelledError` 和 `Cancelled`。
- 本体发出取消后，等待一个宽限期（默认 5 秒，可配置）；插件仍未返回时，本体取消该协程，结果记为 `cancelled`。
- 被取消的能力返回 `ToolResult(status="cancelled")`；已产生的部分产物可以放进 `assets`，但不会被交付。
- `Budget.deadline` 到达时，本体触发同一个取消令牌，原因为 `deadline`。

## 3. 时钟协议

`Clock`：
- `now() -> datetime`（UTC，带时区）
- `monotonic() -> float`
- `await sleep(seconds)`、`await sleep_until(dt)`
- `local_tz`：实例配置中的时区，用于作息等本地时间计算

`MockClock`（`sdk.testing`）：
- `set(dt)`、`advance(seconds)`：推进时间，并按时间顺序唤醒到期的 `sleep`；同一时刻的唤醒按注册顺序
- 推进是确定的：同样的操作序列，结果完全相同

规则：本体和插件代码中禁止直接使用 `time.time`、`time.sleep`、`datetime.now`、`asyncio.sleep`、`random` 的全局实例（随机数通过 `ctx.random` 获取，可设种子）。用 ruff 的禁用 API 规则或检查脚本强制执行。

## 4. 插件上下文 `PluginContext`

始终存在：`plugin_id`、`companion_id`、`config`（合并并校验后的只读配置）、`log`、`clock`、`random`、`storage`（插件私有目录 `plugin-data/<id>/` 下的文件访问 + 一个键值存储）、`events`（只能发布以自己 id 为前缀的事件）。

按权限出现（未授权时属性**不存在**，访问抛 `AttributeError`）：

| 属性 | 权限 |
|---|---|
| `memory` | `memory.read.public` / `memory.read.private` / `memory.write` |
| `assets` | `assets.write`（读取始终允许） |
| `services` | 声明了依赖的服务时出现，只能查到已声明的服务 |
| `peer` | `peer` |
| `model` | `model.invoke` |
| `http` | `network` |
| `proactive` | `message.proactive`：提交主动消息意图（仍经过边界和出站检查） |
| `subprocess` | `subprocess` |

## 5. 扩展点签名

以下为异步协议（Protocol）。`ctx` 在插件启动时注入，不在每次调用时传入。

### 5.1 channel
```
async connect() -> None
async close() -> None
events() -> AsyncIterator[EventEnvelope]      # 本体消费入站事件
async send(intent: DeliveryIntent, cancel) -> Receipt
async react(intent: DeliveryIntent, cancel) -> Receipt
features() -> ChannelFeatures
```
`ChannelFeatures`：`max_text_chars`、`supports_react`、`supports_quote`、`supports_card`、`media_limits: dict`。
平台插件负责把平台账号映射为 `Actor` 和 `Conversation`：映射表由本体通过 `ctx` 提供（来自实例配置），插件不自己判断身份。

### 5.2 model
```
async generate(req: ModelRequest, cancel) -> ModelTurn
features() -> ModelFeatures
async quota() -> QuotaSnapshot | QUOTA_UNKNOWN     # 不得为此调用模型
```
`ModelFeatures`：`supports_tools`、`supports_images`、`context_chars`、`agent_type: bool`（Agent 类模型自己跑工具循环，本体通过 MCP 提供工具）。

### 5.3 embedder
```
dimension: int
async embed(texts: list[str], cancel) -> list[list[float]]
```

### 5.4 tool
工具描述（ToolSpec）写在清单的 `contributes.tools` 中。
```
idempotency_key(call: ToolCall, inv: Invocation) -> str
async prepare(call: ToolCall, inv: Invocation) -> Plan | Denial
async execute(plan: Plan, inv: Invocation, cancel) -> ToolResult
```
本体的权限、额度检查在 `prepare` 之前；`prepare` 可以做插件自己的检查（如参数不合理）。

### 5.5 behavior 环节
每个环节一个函数：`async handler(input) -> Directive | None`。返回 `None` 表示没有意见。

| 环节 | 输入 | 可返回的指令 |
|---|---|---|
| `message_received` | EventEnvelope | 不返回（只观察） |
| `before_attention` | EventEnvelope + `CompanionState` | `Ignore` / `ReactOnly(emoji)` / `Mode(name, params)` / `Normal` |
| `attention` | EventEnvelope + 待处理的消息批次 | `Skip` / `Defer(seconds)` / `Respond` |
| `after_generation` | `ReplyDraft` | 修改后的 `ReplyDraft` |
| `before_send` | list[DeliveryIntent] | 修改后的 list[DeliveryIntent] |
| `task_completed` | 任务摘要 | 不返回 |

`ReplyDraft`：`parts`、`conversation`、`task_id?`、`trace_id`、`form: plain|split|card`、`quote: MessageRef?`。
`CompanionState`：`companion_id`、各插件通过服务公布的状态摘要（只读）。

### 5.6 其他扩展点
| 扩展点 | 签名 |
|---|---|
| `context_section` | `async contribute(req: ContextRequest) -> ContextSection?`；`ContextRequest`：`scene`、`actor`、`budget_chars`；`ContextSection`：`title`、`text`、`priority: int` |
| `schedule` | 清单声明 `{id, every: ISO 8601 时长}` 或 `{id, at: "HH:MM", days: [...]}`；处理函数 `async on_tick(tick: Tick)` |
| `service` | 清单声明 `{name, version, methods: [...]}`；`async call(method: str, args: dict) -> dict`。使用方通过 `ctx.services.get(name).call(...)` 调用，参数和返回值都必须可序列化 |
| `event_subscriber` | 清单声明事件名（可用 `前缀.*`）；`async on_event(e: BusEvent)` |
| `memory.retriever` | `async search(q: MemoryQuery, candidates: CandidateReader, cancel) -> list[MemoryHit]`；`CandidateReader` 只返回 `q.allowed_visibility` 内的记录 |
| `memory.importer` | `detect(source: Path) -> bool`；`read(source: Path, cancel) -> AsyncIterator[MemoryRecord]`；本体负责快照、预览、合并 |
| `memory.consolidator` | `async run(window_start, window_end, reader, cancel) -> list[MemoryRecord]`（kind=`episode`，由本体写入） |
| `notifier` | `async notify(n: Notice, cancel) -> Receipt` |
| `cli_command` | 清单声明 `{name, help, args: JSON Schema}`；`async run(args: dict, api: ApiClient) -> int`（只能通过 API 操作） |
| `api_route` | 清单声明 `{method, path}`，路径固定在 `/plugins/<id>/` 下；非 GET 必须声明 `audit: true` |
| `webui_panel` | 声明式：`{id, title, kind: kv|table|text, data_route}`，不允许插件自带脚本 |

A1 只需要定义这些签名、数据类型和契约测试；实现由 A2、A3 完成。

## 6. 指令合并规则

同一环节有多个插件返回意见时：

**排序**：按清单中的 `priority`（整数，越小越先，默认 100），相同时按插件 id 字母序。

**before_attention**：取"强度"最高的指令，强度：`Ignore`(4) > `ReactOnly`(3) > `Mode`(2) > `Normal`(1)。强度相同时取排序最靠前的插件。
**本体保护**：主人私聊的消息永远不会被完全忽略——`Ignore` 自动降级为 `ReactOnly`（表情来自配置，默认 💤）。

**attention**：`Skip` > `Defer` > `Respond`。多个 `Defer` 取最长的秒数；本体限制最长延迟（默认 120 秒）。没有任何插件给出意见时：私聊和被 @ 的消息为 `Respond`，其余为 `Skip`。主人私聊不能被 `Skip`（降级为 `Respond`）。

**after_generation / before_send**：按排序依次传递（流水线），每个插件拿到上一个的结果。约束：
- 不能改变文字内容，只能改变形式（拆分、卡片、引用、发送时间）。本体比较处理前后去掉空白后的全部文字，不一致则丢弃该插件的修改并记录警告。
- `before_send` 不能新增或删除交付意图，不能修改 `parts`。

**context_section**：全部收集，按 `priority` 排序，超出总预算时从优先级最低的开始舍弃，单段超过自身预算时截断。

插件出错或超时（默认 2 秒，可在清单中声明更长，上限 10 秒）时视为返回 `None`，并采用清单中的 `failure_default`。
