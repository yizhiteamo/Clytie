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

**context_section**：全部收集，按 `priority` 升序排列（数字越小越重要，与插件 `priority` 的约定一致），先把每段截断到自身预算，超出总预算时从排在最后的段落开始整段舍弃。

插件出错或超时（默认 2 秒，可在清单中声明更长，上限 10 秒）时视为返回 `None`，并采用清单中的 `failure_default`。

## 7. v0.2 补充（A1 验收时补齐，对应 Issue #1）

### 7.1 插件入口与生命周期

```
class Plugin(Protocol):
    def __init__(self) -> None            # 无参数构造
    async def start(self, ctx: PluginContext) -> None
    async def stop(self) -> None
```

- 清单 `entry` 指向插件类。宿主构造实例 → `await start(ctx)`（超时 10 秒）→ 按清单装配扩展点 → 运行 → `await stop()`（超时 10 秒）。
- 清单中的 `handler` 和 `factory` 都是**插件实例上的属性名**：
  - `handler`：一个异步方法（行为环节、上下文段落、定时、事件、服务、CLI、API 路由）。
  - `factory`：一个无参数方法，在 `start` 之后调用，返回实现对应协议的对象（channel、model、embedder、tool、memory.*、notifier）。
- `start` 失败：该插件在这个伴侣上标记为"启动失败"，不装配任何扩展点，报告原因；依赖它的插件按缺少依赖处理。`stop` 的异常只记录日志。

### 7.2 CandidateReader（检索器读取候选记录）

```
async iter(*, kinds: list[str] | None = None, scope: str | None = None,
           updated_after: datetime | None = None, batch: int = 200)
    -> AsyncIterator[list[MemoryRecord]]
async get(record_ids: list[str]) -> list[MemoryRecord]
async count() -> int
```

只返回：可见等级在 `allowed_visibility` 内、未撤销、未冻结的记录。`get` 对不在范围内的 ID 静默跳过（不报错，避免泄露"这条记录存在"）。

### 7.3 ApiClient（CLI 扩展使用）

```
async request(method: str, path: str, *, json: JsonObject | None = None,
              query: dict[str, str] | None = None) -> ApiResponse
```
`ApiResponse`：`status: int`、`json: JsonValue | None`。路径以 `/api/v1/` 开头；具体路由清单在 A4 定义。A1 只需要协议和一个假实现。

### 7.4 通知与事件负载

**`EventEnvelope.notice`** 的结构按事件类型：

| type | 字段 |
|---|---|
| `conversation.notice` | `kind: recall \| member_join \| member_leave \| poke \| other`、`actor_id?`、`target?: MessageRef`、`detail?: str` |
| `conversation.request` | `kind: friend \| group_invite`、`actor_id`、`comment?: str`、`request_ref: str` |

**本体事件负载**（都定义为 SDK 数据类型；负载中不包含消息正文）：

| 事件 | 负载 |
|---|---|
| `message.received` | `event_id`、`trace_id`、`conversation_id`、`scene`、`actor_id` |
| `reply.sent` | `trace_id`、`conversation_id`、`intent_ids: list`、`task_id?` |
| `task.started` | `task_id`、`trace_id`、`tool`、`actor_id`、`conversation_id` |
| `task.completed` / `task.failed` | `TaskSummary`（下表） |
| `companion.state_changed` | `companion_id`、`plugin_id`、`state: JsonObject` |
| `quota.changed` | `model_plugin`、`snapshot: QuotaSnapshot?`、`protection_mode: bool` |
| `plugin.enabled` / `plugin.disabled` | `plugin_id`、`companion_id`、`reason?` |

**TaskSummary**：`task_id`、`trace_id`、`tool`、`status: ok|error|denied|cancelled`、`actor_id`、`conversation_id`、`scene`、`started_at`、`finished_at`、`asset_ids: list`、`error_code?`。`task_completed` 环节的输入就是 `TaskSummary`。

**CompanionState**：`companion_id`、`states: dict[plugin_id, JsonObject]`。`states` 的内容来自各插件通过 `ctx.state.set()` 公布的最新状态（见 7.5），只读。

### 7.5 插件上下文的方法

始终存在：

| 属性 | 方法 |
|---|---|
| `log` | `debug/info/warning/error(msg: str, **fields)`；`fields` 必须可序列化。约定：不记录消息正文 |
| `storage` | `path: Path`（插件私有目录，已创建）；`async get(key) -> JsonValue \| None`、`async set(key, value)`、`async delete(key)`、`async keys(prefix="") -> list[str]`。键 ≤200 字符，单个值序列化后 ≤1 MB |
| `events` | `async publish(name, payload: JsonObject)`；`name` 不以 `<plugin_id>.` 开头时抛 `ValueError` |
| `state` | `async set(payload: JsonObject)`：更新本插件在 `CompanionState.states` 中的状态，并发布 `companion.state_changed` |
| `assets` | **只读**：`async info(asset_id) -> AssetRef`、`async read(asset_id) -> bytes`、`async path(asset_id) -> Path`（返回的文件只读） |

按权限出现（未授权时属性不存在）：

| 属性 | 权限 | 方法 |
|---|---|---|
| `asset_writer` | `assets.write` | `async put(data: bytes, media_type, origin) -> AssetRef` |
| `memory` | 任一 `memory.*` | 有 `memory.read.public` 或 `memory.read.private` 时：`async search(text, *, limit=10) -> list[MemoryRecord]`、`async get(record_id) -> MemoryRecord \| None`。有 `memory.write` 时：`async write(draft: MemoryDraft) -> MemoryRecord`；同时具有写权限和读权限时另有 `async revise(record_id, expected_revision, changes: JsonObject) -> MemoryRecord`、`async retract(record_id, expected_revision, reason)`。没有的方法在对象上不存在 |
| `services` | 声明了服务依赖 | `get(name) -> ServiceProxy \| None`（可选依赖不可用时为 `None`）；`ServiceProxy.call(method, args) -> JsonObject`，不可用时抛 `ServiceUnavailable` |
| `peer` | `peer` | `async send(companion_id, kind, payload) -> str`、`async request(companion_id, kind, payload, timeout_s) -> JsonObject` |
| `model` | `model.invoke` | `async generate(req: ModelRequest, cancel, *, purpose: str) -> ModelTurn`（`purpose` 写入日志） |
| `http` | `network` | `async request(method, url, *, headers=None, body=None, timeout_s=30, max_bytes=20 MB) -> HttpResponse`（`status`、`headers`、`body: bytes`） |
| `proactive` | `message.proactive` | `async submit(conversation_id, parts, *, reason: str) -> str`（返回 `intent_id`；仍经过边界和出站检查，可能被拒绝） |
| `subprocess` | `subprocess` | `async run(args: list[str], *, timeout_s, env=None, max_output_bytes=10 MB) -> ProcessResult`（`returncode: int`、`stdout: bytes`、`stderr: bytes`、`truncated: bool`）；不经过 shell，工作目录固定为插件私有目录；输出超过上限时截断并置 `truncated` |
| `identity` | 实现了 `channel` 扩展点的插件自动获得 | `resolve_actor(account) -> Actor`、`resolve_conversation(kind, platform_id) -> Conversation`、`account_for(actor_id) -> str \| None`、`platform_id_for(conversation_id) -> str \| None`；映射来自实例配置 |

- `bytes` 类型的字段（`HttpResponse.body`、`ProcessResult.stdout/stderr` 等）序列化为 JSON 时使用标准 Base64 字符串，反序列化时还原为 `bytes`。解码为文本由插件自己负责（子进程和网页的编码不一定是 UTF-8，也可能是二进制）。
- `gpu` 权限没有对应属性，只用于告诉宿主和用户"这个插件会使用 GPU"。
- **记忆权限规则**（宿主负责裁剪，插件无法改变）：
  1. 只有 `memory.write`：只能写入新记录，不能 `search`、`get`、`revise`、`retract`。`revise` / `retract` 只能作用于当前场景下读得到的记录。
  2. 场景**由宿主绑定**，插件不能传入或修改。宿主在调用环节、工具、上下文段落等处理某条事件时，把该事件的场景绑定到这次调用；`search`、`get`、`revise`、`retract` 都按绑定的场景过滤。
  3. 没有绑定场景的调用（定时任务、事件订阅、其他插件的服务调用、启动和停止）一律按 `public_group` 处理：只能读到 `public` 和 `relationship` 等级。
  4. 私密记录只在**同时满足**以下条件时可读：插件有 `memory.read.private`，并且绑定的场景在伴侣配置 `memory.private_scenes` 中（默认只有 `owner_private`）。可以加入哪些场景、有什么限制，见 `config-spec.md` 第 3 节。
  5. 只有 `memory.read.public` 的插件，在任何场景都读不到私密记录。
  6. `get` 对当前场景读不到的记录返回 `None`，和"记录不存在"无法区分。
- **MemoryDraft**：`kind`、`content`、`subject?`、`visibility`、`source_type`、`source_ref?`、`scope?`、`tags`。插件写入的记录 `write_mode` 一律为 `auto`。
- `revise` / `retract` 的 `expected_revision` 与当前修订号不一致时抛 `RevisionConflict`。

### 7.6 取消的责任划分

- **插件内部**：收到取消后，要么抛出 `Cancelled` / `asyncio.CancelledError`，要么尽快返回 `ToolResult(status="cancelled")`（适用于需要附带部分产物的情况）。不允许捕获取消后继续执行。
- **宿主**：当它持有的令牌已被取消、而插件抛出了取消异常时，宿主把它转换为 `ToolResult(status="cancelled")`；令牌原因为 `deadline` 时，`error.code = "deadline"`。宿主自己的令牌**没有**被取消、却收到 `asyncio.CancelledError` 时（例如进程正在关闭），不转换，继续向上传播。
- 宽限期、截止时间触发、写入账本都是宿主的职责（A3 实现）。
- 契约测试：对一个已取消的令牌调用 `execute`，插件必须在宽限期内抛出取消异常或返回 `cancelled`；两种都算通过。

### 7.7 清单中的扩展点编码

```json
"contributes": {
  "channel":  {"factory": "create_channel", "platform": "onebot-v11"},
  "model":    {"factory": "create_model"},
  "embedder": {"factory": "create_embedder", "dimension": 512},
  "tools": [{"factory": "create_draw", "name": "draw", "description": "…", "when_not_to_use": "…",
             "examples": [], "parameters": {}, "audience": ["owner", "member"],
             "heavy": true, "risk": "low", "delivery": "image"}],
  "memory": {
    "retriever":    {"factory": "create_retriever"},
    "importer":     {"factory": "create_importer", "formats": ["…"]},
    "consolidator": {"factory": "create_consolidator"}
  },
  "notifier": {"factory": "create_notifier"},
  "behavior": {"before_attention": {"handler": "on_before_attention", "timeout_s": 2}},
  "context_section": {"handler": "contribute", "budget_chars": 300},
  "schedule": [{"id": "tick", "every": "PT1M", "handler": "on_tick"},
               {"id": "morning", "at": "09:00", "days": [1, 2, 3, 4, 5], "handler": "on_morning"}],
  "services": [{"name": "example.state", "version": "1.0", "methods": ["get"], "handler": "on_service"}],
  "events": {"subscribe": ["task.completed"], "publish": ["example.changed"], "handler": "on_event"},
  "cli_commands": [{"name": "example", "help": "…", "args": {}, "handler": "on_cli"}],
  "api_routes": [{"method": "GET", "path": "/plugins/example/status", "handler": "on_status", "audit": false}],
  "webui_panels": [{"id": "status", "title": "…", "kind": "kv", "data_route": "/plugins/example/status"}]
}
```

- `channel`、`model`、`embedder`、`notifier` 每个插件最多一个；`tools` 可以有多个。
- `audience` 取值 `owner`、`peer`、`member`、`stranger`；`risk` 取值 `low`、`medium`、`high`；`delivery` 取值 `text`、`image`、`audio`、`video`、`file`、`card`。
- `schedule.days`：ISO 星期编号，1 = 星期一 … 7 = 星期日；省略表示每天。`at` 按实例配置的时区解释。节假日判断不属于调度器，由需要的插件自己处理。
- `services[].handler` 签名：`async (method: str, args: JsonObject) -> JsonObject`。
- `api_routes` 和 `webui_panels` 在 A1–A3 只做声明和校验，A4 才真正提供服务。
- 假插件的清单要改为使用上述编码（`fake-model` 声明 `model`，`fake-channel` 声明 `channel`），契约测试通过宿主同样的装配路径（读清单 → 构造 → `start` → `factory`）获得实现对象。
