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
| `sticker` | `sticker_id: str`，`asset: AssetRef?`，`raw_ref: str?`，`summary: str?`（最多 100 字，ADR 0033） |
| `card` | `title: str`，`body: str`（发送用：阅读卡片） |
| `unknown` | `raw_type: str` |

**MessageRef**：`channel_id`、`conversation_id`、`platform_message_id`。

**Message**：`ref: MessageRef`、`sender: Actor`、`parts: list[MessagePart]`、`sent_at: datetime`、`reply_to: MessageRef?`。

### 1.3 事件

**EventEnvelope**
| 字段 | 类型 | 说明 |
|---|---|---|
| `event_id` | str | 去重键 |
| `dedupable` | bool | 默认 `true`；无法合成稳定 ID 时为 `false`，只能观察，不能触发任务或回复（见 `kernel-spec.md` 第 3 节） |
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
| `replaces_emoji` | str? | `kind = react` 时使用，填上一个确定发出的状态表情；平台支持撤掉表情时先撤旧的再加新的，不支持时只加新的（kernel-spec §13.5） |
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

临时性失败的 `error.code` 约定为 `rate_limited` / `transient`（有限重发规则见 `kernel-spec.md` 第 6 节）。

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
| `write_mode` | `auto` \| `confirmed` | confirmed 当且仅当 basis 为 owner_confirmed |
| `basis` | `owner_confirmed` \| `owner_stated` \| `observed` | 本体填写；旧格式缺失时按 write_mode 推导：confirmed 对应 owner_confirmed，否则 observed |
| `origin_conversation_id` | str? | 本体填写的可信来源会话，旧格式缺失为 null；随修订保存、导出 |
| `source_type` | `private` \| `group` \| `owner_group_interaction` | |
| `source_ref` | str? | 账本追踪号 |
| `scope` | str? | 黑话等按群隔离的记录所属的 `conversation_id` |
| `created_at` / `updated_at` | datetime | |
| `decay` | `{weight: float(0–1), frozen: bool}` | |
| `tags` | list[str] | |
| `retracted` | bool | 撤销后保留记录 |

**MemoryQuery**：`text`、`scene`、`allowed_visibility: list`（由本体填写，插件不能改）、`scope: str?`、`limit: int`。

**MemoryHit**：`record_id`、`score: float`、`source: str`（哪个检索器）。

**A3.4 回想类型**（memory-recall-spec §2.2）：
- `RecollectWindow`：会话场景、按顺序编号的消息（`[消息N] 说话人称呼：原文`）、她在这段里已经用 `core_remember` 记下的记忆，以及本段撤销的条数；撤销的原文和编号不交给扩展或模型（Issue #35 U2）。
- `RecollectItem`：`kind`、`content`、`visibility`、`evidence: list[消息编号]`，以及可选的 `about`（印象对应的说话人）、`merges`（建议合并的 `[记忆N]`）。
- 保留标签 `core:` 前缀只能由本体写入；插件和模型提交的 `core:` 标签一律丢弃。出处、合并与保留标签的核对按 memory-recall-spec §2.4–§2.5、§9。

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

生产时钟由 `src/clytie/clock.py` 的 `SystemClock` 提供，是唯一允许调用系统时钟的文件（仅 `datetime.now`、`time.monotonic`、`asyncio.sleep`）；本体其他代码通过注入获得时钟，插件通过 `ctx.clock` 获得。此适配边界经 Claude 只读技术确认，不改变业务时钟规则。

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
| `assets` | `assets.write`（读取仅限当前调用方获授权的资产，不另要求写权限） |
| `services` | 声明了依赖的服务时出现，只能查到已声明的服务 |
| `peer` | `peer` |
| `model` | `model.invoke` |
| `http` | `network` |
| `proactive` | `message.proactive`：提交主动话题与材料，文字由模型生成；直接发送只允许表情和媒体（见 §7.5 与 agent-spec §15） |
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

SDK 0.14 的 `supports_media_fetch` 默认 `False`。声明支持的平台提供
`async fetch_media(part: MediaPart, cancel: CancelToken) -> MediaContent`，
返回 `data: bytes` 和 `media_type: str`。宿主按文件头核验格式。
`ctx.http.request` 接受关键字参数 `follow_redirects: bool = True`；
为 `False` 时直接返回首次响应，取图必须使用该模式并拒绝 3xx。
另有 `supports_react_remove: bool`，默认 `false`（kernel-spec §13.5）。
B4b 新增 `supports_membership: bool = False`；支持时提供可选 `async is_member(conversation_id, actor_id) -> bool | None`，参数为逻辑 ID，`None` 表示未知（admission-spec §5）。成员变化沿用 `conversation.notice` 的 `member_join` / `member_leave`，本体在准入前清除对应缓存。
`media_limits` 的键：`{image|audio|video|file: {max_bytes: int, formats: [media_type]}}`；没有列出的类型视为不支持。
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
| `context_section` | `async contribute(req: ContextRequest) -> ContextSection?`；`ContextRequest`：`scene`、`actor: Actor?`（默认 `None`）、`budget_chars`；主动消息时 actor 为空，普通回复显式传入发送者；`ContextSection`：`title`、`text`、`priority: int` |
| `schedule` | 清单声明 `{id, every: ISO 8601 固定时长}` 或 `{id, at: "HH:MM", days: [...]}`；处理函数 `async on_tick(tick: Tick)`；`every` 的范围见 §7.7，按 kernel-spec §9 执行 |
| `service` | 清单声明 `{name, version, methods: [...]}`；`async call(method: str, args: dict) -> dict`。使用方通过 `ctx.services.get(name).call(...)` 调用，参数和返回值都必须可序列化 |
| `event_subscriber` | 清单声明事件名（可用 `前缀.*`）；`async on_event(e: BusEvent)` |
| `memory.retriever` | `async search(q: MemoryQuery, candidates: CandidateReader, cancel) -> list[MemoryHit]`；`CandidateReader` 只返回 `q.allowed_visibility` 内的记录 |
| `memory.importer` | `detect(source: Path) -> bool`；`read(source: Path, cancel)` 异步产出 `ImportCandidate`（具体字段编码见 ADR 0008），包含平台账号/会话引用及可信程度提议，不能直接填写 subject/origin_conversation_id/basis。本体负责配置映射、计划确认、快照和写入，范围见 memory-spec §9.4 |
| `memory.consolidator` | `async run(window: RecollectWindow, reader: CandidateReader, cancel) -> list[RecollectItem]`；本体核对、写入，见 memory-recall-spec §2 |
| `notifier` | `async notify(n: Notice, cancel) -> Receipt` |
| `cli_command` | 清单声明 `{name, help, args: JSON Schema}`；`async run(args: dict, api: ApiClient) -> int`（只能通过 API 操作）。SDK 0.15 起确认由本体负责，见下方说明（Issue #137） |
| `smoke_driver`（SDK 0.13） | 平台插件在 `contributes.smoke_driver.factory` 声明工厂。冒烟 CLI 用注入的时钟构造测试方驱动；伴侣启动不装配此扩展点。 |
| `api_route` | 清单声明 `{method, path}`，路径固定在 `/plugins/<id>/` 下；实际挂载 `/api/v1/companions/<cid>/plugins/<pid>/`；非 GET 必须声明 `audit: true`；handler 与边界见 §7.3 |
| `webui_panel` | 声明式：`{id, title, kind: kv|table|text, data_route}`，不允许插件自带脚本 |

冒烟驱动契约位于 `clytie.sdk.smoke`。本体按测试会话的 `channel` 解析目标，核对其与
`driver.plugin` 一致。CLI 只把 `driver.config` 和本次测试凭据读取能力交给
`connect(config, secrets)`。每次发送前调用 `authorize(target)` 检查成员与测试账号，
返回成员数和测试账号在配置中的序号。`send(case)` 返回测试方消息编号；
`events()` 产生通用回复或表情观察，`close()` 释放连接。
平台原始事件、账号、消息编号和凭据均不写入报告。会话服务、能力间调用及能力包的后续规则见平台能力规格 §2–§4。

插件命令的确认（SDK 0.15，Issue #137，用户已确认 2026-11-20）：
- 交给插件命令的 `ApiClient` 不接受 `confirm` 字段。请求体带 `confirm` 时直接返回固定错误 `confirm_not_allowed`，不发出请求。
- 路由返回 428 `confirmation_required` 时，由本体 CLI 显示服务器给出的固定说明，按与本体命令相同的规则询问用户（确认与口令规则见 api-spec §2.3）。用户同意后，本体代为重新提交一次并带上 `confirm: true`；用户拒绝或没有交互终端时，插件收到 `confirmation_declined`，不写入。
- 插件只能看到最终结果，不能自己生成确认提示，也不能代替用户确认。

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

### 7.2a memory.reranker

`async rerank(query: str, records: list[MemoryRecord], cancel) -> list[float]`：返回与 records 等长的分数。只接收本体按 memory-spec §3 投影后的记录；采用 §7.1 的 factory 编码。不可用时跳过重排（memory-spec §4）。

### 7.3 ApiClient（CLI 扩展使用）

SDK 0.13 的插件配置提议由已启用的插件路由产生。CLI 在用户确认后向
`POST /api/v1/companions/<cid>/plugins/<pid>/config/apply` 提交提议的会话引用。
插件的 `GET /plugins/<pid>/config/proposal` 处理器按 `session` 返回 `{"changes": {键: 新值}}`。
宿主从同一伴侣的同一插件取回提议，核对运行时绑定、清单顶层 `config_writable` 键清单和配置 Schema。
客户端只提交会话引用和确认；保存目标由宿主选择。无法唯一确定绑定时拒绝写入。
没有声明可写键的插件不能通过此路由写配置。成功保存后保留配置历史；新配置在重启后生效。

```
async request(method: str, path: str, *, json: JsonObject | None = None,
              query: dict[str, str] | None = None) -> ApiResponse
```
`ApiResponse`：`status: int`、`json: JsonValue | None`。路径以 `/api/v1/` 开头；具体路由清单见 `api-spec.md` 第 4、5 节。CLI 扩展只拿到绑定 CLI 通行证的 `ApiClient`，不提供 `ctx`。

插件路由 handler 为 `async (request: {method, subpath, query, json}) -> {status, json}`，宿主限制 10 秒超时和 1 MB 响应。清单的 `/plugins/<pid>/<子路径>` 实际挂在 `/api/v1/companions/<cid>/plugins/<pid>/<子路径>`，请求转交伴侣子进程执行。登录、防伪和审计由本体统一处理；插件非 GET 路由不能作为确认或口令级操作。

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
| `credentials` | 实例配置 `credentials.<ref>.plugins` 列出本插件 | `async read(ref) -> str`；失败抛 `CredentialUnavailable(code)`。平台插件另有 `account_secret()`。见 runtime-services-spec §1 和 ADR 0031 |
| `websocket` | `network` | 只有客户端：`async connect(url, *, headers=None, timeout_s=10, max_message_bytes=16 MB) -> WebSocketConnection`（`send_text`、`receive(cancel)`、`close`、`closed`、`close_code`）。只收发文本帧，插件停止时宿主关闭全部连接。详见 ADR 0030（阶段二 B2，用户已确认） |
| `proactive` | `message.proactive` | `async submit(conversation_id, *, topic: str \| None = None, material: JsonObject \| None = None, parts: list[MessagePart] \| None = None, reason: str) -> str`（返回 `trace_id`；话题与直接发送二选一，仍经过边界和出站检查，见下） |
| `subprocess` | `subprocess` | `async run(args: list[str], *, timeout_s, env=None, max_output_bytes=10 MB) -> ProcessResult`（`returncode: int`、`stdout: bytes`、`stderr: bytes`、`truncated: bool`）；不经过 shell，工作目录固定为插件私有目录；输出超过上限时截断并置 `truncated` |
| `identity` | 实现了 `channel` 扩展点的插件自动获得 | `resolve_actor(account) -> Actor`、`resolve_conversation(kind, platform_id) -> Conversation`、`account_for(actor_id) -> str \| None`、`platform_id_for(conversation_id) -> str \| None`、`self_account() -> str \| None`（本单元绑定的平台账号，未绑定为 `None`，见 ADR 0032）；映射来自实例配置 |

- `bytes` 类型的字段（`HttpResponse.body`、`ProcessResult.stdout/stderr` 等）序列化为 JSON 时使用标准 Base64 字符串，反序列化时还原为 `bytes`。解码为文本由插件自己负责（子进程和网页的编码不一定是 UTF-8，也可能是二进制）。
- `gpu` 权限没有对应属性，只用于告诉宿主和用户"这个插件会使用 GPU"。
- `proactive.submit`（agent-spec §15，用户已确认）：`topic` 与 `parts` 同时给出或都不给时抛 `ValueError`。话题方式可附 `material`，文字由模型结合上下文生成，可以选择不发；不提供能力。直接发送只允许 `sticker`、`image`、`audio`、`video`、`file`，含 `text`、`card`、`link` 等文字类部分时抛 `ValueError`，提示改用话题方式。`reason` 写入日志和审计，不交给模型。目标会话必须在实例配置登记，未登记或陌生人私聊抛 `ValueError`，不创建记录。返回这次主动消息的 `trace_id`，插件不能读取账本。
- **记忆权限规则**（宿主负责裁剪，插件无法改变）：
  1. 只有 `memory.write`：只能写入新记录，不能 `search`、`get`、`revise`、`retract`。`revise` / `retract` 只能作用于当前场景下读得到的记录。
  2. 场景**由宿主绑定**，插件不能传入或修改。宿主在调用环节、工具、上下文段落等处理某条事件时，把该事件的场景绑定到这次调用；`search`、`get`、`revise`、`retract` 都按绑定的场景过滤。
  3. 没有绑定场景的调用（定时任务、事件订阅、其他插件的服务调用、启动和停止）一律按 `public_group` 处理：只能读到 `public` 和 `relationship` 等级。
  4. 私密记录只在**同时满足**以下条件时可读：插件有 `memory.read.private`，并且绑定的场景在伴侣配置 `memory.private_scenes` 中（默认只有 `owner_private`）。可以加入哪些场景、有什么限制，见 `config-spec.md` 第 3 节。
  5. 只有 `memory.read.public` 的插件，在任何场景都读不到私密记录。
  6. `get` 对当前场景读不到的记录返回 `None`，和"记录不存在"无法区分。
- A3.3 的记忆视图由本体存储支持，所有读取按 `memory-spec.md` §3 同时裁剪可见等级和来源会话；宿主绑定场景与会话，插件不能指定可信来源。
- 写入入口忽略外部传入的 basis、write_mode、origin_conversation_id；插件写入 basis 一律为 observed，source_type 由宿主填写。插件 source_ref/scope 仅作说明，投影不信任。新建、去重与可信程度只升不降按 memory-spec §1.3；插件只能修订或撤销 basis=observed 的记录；owner_confirmed、owner_stated 只读，修订或撤销返回与找不到记录相同的错误。聊天和插件操作不降低可信程度；用户确认的导入 restore、撤销导入、冲突选择及导入器再次导入可按目标版本恢复 basis，降低项在计划中单独列出。已校验本系统导出包是忽略外部可信字段的唯一例外，由本体保留或规范化，导入器不能自称可信包。
- **MemoryDraft**：`kind`、`content`、`subject?`、`visibility`、`source_type`、`source_ref?`、`scope?`、`tags`。插件写入的记录 `write_mode` 一律为 `auto`。
- `revise` / `retract` 的 `expected_revision` 与当前修订号不一致时抛 `RevisionConflict`。

### 7.6 取消的责任划分

- **插件内部**：收到取消后，要么抛出 `Cancelled` / `asyncio.CancelledError`，要么尽快返回 `ToolResult(status="cancelled")`（适用于需要附带部分产物的情况）。不允许捕获取消后继续执行。
- **宿主**：当它持有的令牌已被取消、而插件抛出了取消异常时，宿主把它转换为 `ToolResult(status="cancelled")`；令牌原因为 `deadline` 时，`error.code = "deadline"`。宿主自己的令牌**没有**被取消、却收到 `asyncio.CancelledError` 时（例如进程正在关闭），不转换，继续向上传播。
- 宽限期、截止时间触发、写入账本都是宿主的职责（A3 实现）。
- 契约测试：对一个已取消的令牌调用 `execute`，插件必须在宽限期内抛出取消异常或返回 `cancelled`；两种都算通过。

### 7.7 清单中的扩展点编码

工具名不能以 `core_` 开头，该前缀保留给本体工具；清单校验拒绝此前缀（`agent-spec.md` §3.3）。

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
- 工具清单新增可选字段 `timeout_s`（正整数，秒）；截止时间计算与实例上限见 `kernel-spec.md` 第 8 节。
- 工具参数 Schema 支持 `"x-clytie-sensitive": true` 注解：带注解的属性写入账本前替换为 `"[redacted]"`；适用于任意层级，包括 `properties`、`items` 和嵌套对象；`oneOf` / `anyOf` / `allOf` 的任一分支把某属性标为敏感，就按敏感处理（见 `kernel-spec.md` 第 13.2 节）。
- `audience` 取值 `owner`、`peer`、`member`、`stranger`；`risk` 取值 `low`、`medium`、`high`；`delivery` 取值 `text`、`image`、`audio`、`video`、`file`、`card`。
- `schedule.every`：只接受能换算成固定秒数的 ISO 8601 时长，即周、日、时、分、秒（`W`、`D`、`H`、`M`（时间部分）、`S`）。一天严格按 24 小时、一周按 7 天，在 UTC 上换算，不随夏令时变化。带年（`Y`）或月（日期部分的 `M`）的一律拒绝，包括混合形式（如 `P1M1D`）和小数形式（如 `P0.5Y`）；总时长为零（如 `PT0S`）的一律拒绝。清单校验和 SDK 类型校验结果必须一致（kernel-spec §9，用户已确认）。
- `schedule.days`：ISO 星期编号，1 = 星期一 … 7 = 星期日；只属于 `at`。`every` 与 `days` 同时出现（包括 `days: []`）时，清单校验和 SDK 校验都拒绝。`at` 省略 `days` 或给出空列表都表示每天，按实例配置的时区解释。节假日判断不属于调度器，由需要的插件自己处理。
- `services[].handler` 签名：`async (method: str, args: JsonObject) -> JsonObject`。
- `api_routes` 和 `webui_panels` 在 A1–A3 只做声明和校验，A4 才真正提供服务。
- 假插件的清单要改为使用上述编码（`fake-model` 声明 `model`，`fake-channel` 声明 `channel`），契约测试通过宿主同样的装配路径（读清单 → 构造 → `start` → `factory`）获得实现对象。


Issue #36（用户已确认）：窗口聊天原文保持原样，不从撤销记录派生内容或编号；回想窗口只给撤销条数。只有 memory.embedder 指定的向量模型可为本次比对接收撤销原文：优先复用同一 embedder_id 缓存，没有时临时计算，不落库、不缓存、不记录原文。未配置、出错、超时或候选向量失败时，整段只做规范化相同比对，照常写入并推进进度；status 只显示计数提醒，WARN 每小时最多一次，不使用其他模型身份的向量。


### Issue #39 用户确认的同步

- 印象与黑话按普通检索命中后注入，共用 recall_limit / recall_chars，不自动全量前置、不增加预算。群聊不在场的印象，普通检索和 core_recall 都不返回；对内印象按普通检索。
- owner_confirmed / owner_stated 的印象与黑话不淡化，但核心记忆排除 profile / jargon；修订升级规则不变，使用范围不变。core:with_owner 只用于群聊 episode，主人参与的黑话不因此免淡化。
- 来源会话按 memory-recall-spec §4.6 的会话登记表处理：本体写入时按配置、导入时按包清单登记；只以 channel + kind + platform_id 在当前配置对应，逻辑 ID 和群名称不参与身份判断。不改 origin_conversation_id，不产生修订、不进内容指纹。未对应时仅对内可见、按登记类别淡化；旧包和无法得知身份的非印象来源不淡化。再次导出保留登记；更换 channel 不自动对应。本地与导入同逻辑 ID、不同平台身份的记录须分别引用自己的登记。
- 停用回想立即取消进行中的工作且不写入；重新启用时所有会话进度移到现在，停用期间和未完成窗口都跳过，status 只显示跳过段数。

## A4.1 连接状态与通知补充

按 `supervisor-spec.md` §3.3、§9：

- `Channel` 新增可选方法 `state() -> ChannelState`；`ChannelState` 字段为 `status`（`online`、`connecting`、`offline`、`auth_invalid`、`unknown`）、`since` 和可选 `code`。未实现时为 `unknown`，现有插件不需要修改。
- 新增 `PluginUnavailable(code, message="")`，插件可在 `start(ctx)`、`factory` 和 `connect()` 中抛出；`ChannelUnavailable(code)` 是它的子类，供 `connect()` 使用。宿主只传递 `code`，不解析异常消息；`message` 只写日志（supervisor-spec §3.3，Issue #47 D1）。模型的登录状态只取最近一次正常调用的结果，`ModelFeatures` 不增加状态字段。
- 错误码增加 `auth_invalid`（凭证或登录态失效）和 `quota_exhausted`（额度用完），可以出现在 `ModelTurn.error.code`、`Receipt.error.code` 和 `PluginUnavailable(code)`。原有 `rate_limited` / `transient` 不变。
- notifier 清单增加可选 `outward: bool`，默认 `false`。静默期间暂停 `outward: true` 的实例通知插件；其他照常。
- 契约测试覆盖未实现 `state()` 时为 `unknown`，以及 start、factory 或 connect 抛出 auth_invalid 时不重启。

## A4.3 在线备份扩展（待实现）

以下为已批准的 A4.3 契约，尚未实现、未经验收；完整依据为 `backup-spec.md`，不代表历史里程碑已提供这些能力。

这是在线备份要求下的新公共契约，须先同步 sdk-spec、清单 Schema 和 CHANGELOG，再实现。插件仍只 import clytie.sdk。
清单新增可选 `backup`：`mode` 为 `stateless | snapshot | unsupported`；省略按 unsupported。snapshot 模式需在 `contributes` 声明唯一 `backup.snapshot` factory，按现有 factory 约定返回提供者；其他模式不允许声明。stateless 表示没有需恢复的业务持久状态，宿主发现 plugin-data 内非声明缓存数据时拒绝该结论并报缺项。缓存不能由插件临时声明来掩盖已有业务数据。
已停止且无活跃执行者/恢复日志的插件，可在取得原生命周期及数据锁后做离线一致复制；无需为备份启动插件。运行中的 unsupported 插件有状态时为缺项。支持状态在列表、启用计划及 status 展示，旧插件仍能正常启用，不把新增字段变成隐式停用。

公共协议（以下 ticket/handle 是进程内对象，不持久化或跨 IPC 序列化）：

```
async prepare(request: BackupRequest, cancel: CancelToken) -> BackupTicket
async capture(ticket: BackupTicket, epoch: str, cancel: CancelToken) -> BackupView
async materialize(view: BackupView, sink: BackupSink, cancel: CancelToken) -> None
async release(ticket: BackupTicket) -> None
```

- `BackupRequest`：operation_id、companion_id、plugin_id、目标 data_version、准备 deadline；这些由宿主填充。提供者不自行选择其他实例/伴侣。
- prepare 在捕获门闩外准备资源，可在私有临时区域工作，不停止插件、发送消息、调用模型或改变业务事实；默认 10 秒。capture 在组捕获窗口内固定该 epoch 的稳定视图，不能在这里做大文件复制。准备期间业务改变时须重新验证，不允许返回旧快照却标上新 epoch。
- `BackupView`：epoch、data_version、revision（提供者的稳定版本标识）以及提供者私有只读句柄。提供者保证 materialize 对应 capture 版本；宿主验证 epoch/版本、完整成员和关联，无法证明则不能算完整。实现允许不可变版本、数据库读视图等方式，不要求所有插件采用同一种存储。
- `BackupSink` 只允许 `async write(relative_path: str, chunks: AsyncIterator[bytes]) -> BackupEntry`。宿主计算字节数与哈希；返回相对路径、size、sha256。路径限定当前插件备份命名空间，拒绝重复/穿越/链接，不把任意宿主文件句柄交给插件。sink 拒绝写入声明为凭据的内容；内容完整性仍由插件契约和测试保证，不以扫描代替。
- materialize 在门闩释放后运行，只读稳定视图；取消/超时后不再接受其迟到写入。release 在成功、失败、取消时均执行且应幂等，默认 2 秒；记录释放失败，不吞掉它导致的资源泄漏。迟到资源继续被跟踪，不能把失去跟踪的 provider 说成已结束。
- 该接口提供备份快照，不授予通用恢复执行脚本权限。每个 sink 相对路径精确映射到恢复目标 companions/<cid>/plugin-data/<pid>/ 下的同一相对路径；成员必须是同一 data_version 可直接加载的完整持久布局。数据库须输出可直接打开的一致数据库文件，不接受需要执行 SQL 导入脚本、反序列化可执行对象或运行转换程序才能还原的逻辑导出。需要转换的 provider 本阶段视为 unsupported，结果 partial。宿主只做安全文件重建和格式校验，插件只在用户后来正常启动时按原生命周期加载。

## A4.3 最小资产后端（Issue #69，用户已确认）

assets.info/read/path 不增加写权限要求，但始终核验当前可信调用上下文的资产归属/共享授权；asset_id 是内容哈希，不是访问凭证。asset_writer.put 的 origin 不能设置归属或提升权限，新产物默认归当前伴侣，发布内容和登记成功后才返回 AssetRef。生产后端、捕获与完整恢复遵守 backup-spec §2.2a；不增加看图、转换、自动共享或单独记忆包资产导入。

## A4.3 BackupView 资产依赖（Issue #70，用户已确认，方案 1）

- BackupView 在已有字段后追加 `assets: tuple[AssetRef, ...] = ()`，默认空集合，兼容已有无资产提供者的构造方式。元素必须为有效 AssetRef；集合不可变，且必须固定元素值，不能只冻结外层而允许提供者随后修改元素。按既有 SDK 不可变类型契约校验或生成不可变快照，不改变现有方法签名。
- assets 在 capture 返回时与 epoch、data_version、revision、handle 所代表的视图一起固定。materialize 不得追加/替换依赖；需要不同依赖必须重新捕获，不能给旧视图改标版本。handle 始终为提供者私有不透明对象，宿主不得解析它发现资产，不扫描输出文本或文件名猜测依赖。
- AssetRef 是依赖声明，不是权限或事实凭证。宿主按 BackupRequest 中可信 companion_id/plugin_id 核验登记及访问权限，并核对媒体类型、长度与该归属登记的来源信息；哈希内容校验仍由本体负责。不符、缺失、跨伴侣未授权均不能算完整快照。插件不能用 origin 或同哈希提升权限。
- 空集合只表示该视图没有外部本体资产依赖；有依赖的提供者必须完整声明。插件快照自行包含的普通文件仍由 BackupSink 的原命名空间契约处理，不强制改为资产。对可验证的不完整声明拒绝/列缺项；宿主不能保证识别提供者故意漏报的全部依赖，不以默认空值证明语义完整。
- 本体汇总配置注解、账本结构化引用与 assets，在各自可信归属下核验，再按内容去重。BackupSink/BackupEntry 不新增资产声明职责，不开放新的资产权限或插件回调。字段及版本变化记录 CHANGELOG 和 ADR；不得将本期内部视图序列化或解析句柄作为替代。

### Issue #72：快照重入与本体存储（用户已确认）

本体记忆/账本由 storage-spec.md 的 StorageOwner 执行；SDK 不暴露连接、游标、任意 SQL 或命令执行器。权限及场景投影不变，异步结果是独立快照。

backup.snapshot 的 capture 不得重入被捕获屏障暂停的本体存储队列；宿主检测跨 await、子任务和转交的异步调用链，不能只看线程编号。重入立即拒绝并列提供者缺项，不排队形成死锁。所需读取在 prepare 完成，capture 校验版本，变化时拒绝旧视图；prepare/materialize 的原权限不因此扩大。
