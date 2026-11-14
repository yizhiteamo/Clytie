# 本体主干实现规格（A3.1：账本与交付）

回答 Issue #12 的 14 组问题，以及 PR #13 通读和第 1 轮验收中提出的缺口。设计依据是 `design/kernel.md`，本文件只补实现必须的契约。

- 标注"用户已确认"的条目是需求或隐私决定，改动前必须经用户确认；其余是技术约定，按 AGENTS.md 第 4 节处理。
- 本文件与其他规格冲突时，以本文件为准，并同步修改对方文档（第 16 节列出了需要同步的地方）。
- 物理存储（数据库选型、表与索引）另写 ADR，但不能改变本文件的逻辑结构和原子操作。

---

## 1. 账本的逻辑结构与原子操作

每个伴侣一本账本，位置 `companions/<cid>/ledger/`，只有本体能读写。采用分开定义的实体（Issue 选项 A）：

| 实体 | 主键 | 关键字段 |
|---|---|---|
| `inbound` 入站消息 | `event_id` | `companion_id`、`channel_id`、`platform_message_id`、`conversation_id`、`sender`（逻辑身份）、`scene`、`received_at`、`parts`（原文，见第 13 节）、`dedupable`、`state: received\|processing\|ignored\|merged\|dropped_on_restart`、`merged_into = trace_id`（消息合并，见 `agent-spec.md` §2）；`ignored` 原因字段取值包括 `boundary_paused`、`cancel_only` |
| `record` 处理记录 | `trace_id` | `source_event_id?`（主动消息、定时任务为空）、`conversation_id`、`requester?`、`kind: chat\|task`、`task_id?`、`tool?`、`parent_trace_id?`、`scene`、`exec_status`、`delivery_status`、`deadline`、各阶段耗时、`created_at` |
| `invocation` 能力调用 | `invocation_id` | `trace_id`、`plugin_id`、`tool`、`idem_key`、`args`、`status`、`result`、`error?`、`asset_ids`、开始/结束时间 |
| `intent` 待交付 | `intent_id` | `trace_id`、`kind`、`parts`、`target?`、`split?`、`not_before?`、`status`（第 6 节） |
| `attempt` 发送尝试 | `(intent_id, n)` | `status: sending\|sent\|failed\|unknown`、`started_at`、`receipt?` |
| `dedupe` 防重键 | `(companion_id, channel_id, platform_message_id)` | `event_id`、`first_seen_at` |
| `audit` 审计 | 自增 | 迟到结果、迟到收据、冲突收据、取消请求、状态修正 |

- 一条入站消息最多对应一条 `record`；一条 `record` 可以有多个 `invocation` 和多个 `intent`。
- **chat 与 task**：记录创建时为 `chat`；第一次领取能力调用（第 4 节）时，在同一事务中分配 `task_id`、写入 `tool`（这次调用的工具名），并改为 `task`。之后的调用不再改变 `tool`。

**原子操作**（每个都是一次事务）：
- `claim_inbound(event)`：写入 `dedupe` 和 `inbound`（`state = received`）。键已存在时返回已有的 `event_id`，不新建任何东西。
- `open_record(event_id, …)`：把 `inbound.state` 改为 `processing`，同时创建 `record`。经过注意力判断决定不处理的消息改为 `ignored`，不创建记录。
- `claim_invocation(key, …)`：按第 4 节的幂等键插入，必要时把记录升级为 task。
- `register_intents([...])`：一次登记一组待交付消息。
- `begin_attempt(intent_id)`：在调用平台插件**之前**写入 `sending`。
- `record_receipt(intent_id, n, receipt)`：写回收据，并更新 intent 和 record 的交付状态。

账本带模式版本号，迁移规则见第 14 节。代码版本低于账本版本时拒绝打开，报 `schema_too_new`。

## 2. 执行状态与事件

**执行状态** `exec_status`（记录级）：`pending` → `running` → `ok | error | denied | cancelled | interrupted`

- 记录的执行状态反映整条处理流程（A3.2 起是 agent 循环）是否正常结束，不是某一次工具调用的结果。工具调用各有自己的 `invocation.status`。
- 终态不可改写，先到的终态生效；之后到达的结果写进 `audit`。
- `interrupted` 只在账本中使用，不加入 SDK 枚举。对外的 `TaskSummary` 中映射为 `status: error`、`error_code: interrupted`。

**重启时**（伴侣启动、打开账本之后、接收新消息之前）：
- `pending` / `running` 的执行 → `interrupted`；其中 `pending` / `running` 的 invocation 也改为 `interrupted`。
- `sending` 的尝试 → `unknown`。
- 还没开始尝试的 intent（`pending` / `retry_wait`）→ `failed`，`error.code = not_sent_before_restart`。它们确实没有发出，但**不会补发**。
- `state = received` 的入站消息 → `dropped_on_restart`，不处理。
- 这些修正都只写审计，**不发布任何事件**（包括 `task.*`），也不调用 `task_completed` 环节；任何旧任务都不重放。

**执行封口**：执行到达终态，与这一轮的 `register_intents` 在同一个事务中提交。这样"执行已结束"时，这条记录要交付的 intent 已经全部登记，交付状态的汇总不会在中途被误判为终态。之后不再为这条记录登记 intent。

**事件与 `task_completed` 环节**（统一时机，与 plugins.md §3、overview.md §5 一致）：
- 只有 `kind = task` 的记录发布 `task.*` 事件，并调用 `task_completed` 环节；聊天记录不发。
- `task.started`：记录升级为 task 的事务（第 1 节）提交后发布。
- `task.completed` / `task.failed`：执行到达终态，并且交付状态到达终态（第 6 节）之后发布。没有任何交付时，执行封口后立即发布。
- `ok` 发 `task.completed`，其他终态发 `task.failed`。总线事件和环节使用同一份 `TaskSummary`。
- `TaskSummary` 的填法：`tool` 取记录的 `tool`；`started_at` 取第一次调用的开始时间，领取后还没开始执行就结束的，取领取时间；`finished_at` 取执行终态时间；`asset_ids` 取全部调用的产物。
- 排队中被取消、还没有任何调用的记录仍是 `chat`，不发 `task.*`。
- `reply.sent`：每个 intent 到达 `sent` 时发一次。部分成功时，只为发出的那几条各发一次。
- 迟到收据修正了状态时，**不补发、不修正已发布的事件**，只写审计。

## 3. 入站防重

- 防重键：`(companion_id, channel_id, platform_message_id)`。`channel_id` 是逻辑 `account_ref`，所以每个平台账号自成一个命名空间；不同伴侣各自独立。`event_id` 的格式保持 SDK 已定的 `channel_id:platform_message_id`。
- 平台插件必须保证 `platform_message_id` 在本账号内唯一并且稳定。
- 平台本身没有消息 ID 的事件（通知、撤回等）：插件用平台提供的稳定字段合成 ID，写进 `event_id`。
- 无法合成稳定 ID 时，插件把 `EventEnvelope.dedupable` 设为 `false`（SDK 新增字段，默认 `true`，见第 16 节）。这类事件照常经过身份解析和输入过滤（隐私过滤不能绕过），然后不经过 `claim_inbound`，不写入 `dedupe` 和 `inbound`，不创建记录，只调用行为环节 `message_received`。不发布总线事件 `message.received`（它的负载需要 `trace_id`），不新增 SDK 事件。这类事件**不能触发任务或回复**。
- 重复事件：不处理，计数，记 DEBUG 日志。重启后再次收到旧事件，同样按重复处理。
- **防重键永久保留（用户已确认）**：只有编号、没有内容，不随第 13 节的内容清理删除。

## 4. 能力幂等键

- 完整的幂等键：`(companion_id, plugin_id, tool, root_event, plugin_key)`。
  - `root_event` 是触发这次调用的入站事件；定时或主动任务用 `trace_id`。
  - `plugin_key` 是 SDK §5.4 `idempotency_key(call, inv)` 的返回值。`Plan` 不增加字段。
- 调用顺序：
  1. 本体做权限和额度检查；
  2. 计算完整的幂等键；
  3. 查询已有调用，有就直接返回，不调用 `prepare`；
  4. 调用 `prepare`；
  5. 返回 `Plan` 后原子领取（并发时没抢到的一方返回已有调用）；
  6. 执行。
- `prepare` 返回 `Denial` 或抛出异常时不占用键。
- 查到已有调用时：仍在执行就返回 `in_progress` 和已有的 `task_id`，不新开执行；已到终态就返回已有结果（包括 `interrupted`）。
- 同一触发事件里，同一工具、同一 `plugin_key` 的多次调用视为同一个请求。用户再发一条消息提出同样的请求时，`root_event` 不同，会正常执行。
- 幂等记录随 `invocation` 保留。

## 5. 发送提交点

固定顺序：`register_intents` → `begin_attempt` 提交 → 调用平台插件 → `record_receipt`。

- 写入 `sending` 之后、收据写回之前崩溃 → 重启时标为 `unknown`（第 2 节），永不重发。
- 收据的 `intent_id` 与本次尝试不一致 → 拒绝这份收据，本次尝试标为 `unknown`，记 WARN 和审计。
- 同一尝试重复收到收据：内容相同就忽略；内容冲突就保留第一份，其余写审计。
- 平台插件抛出异常（而不是返回收据）→ 按 `unknown` 处理，因为无法证明没有发出。
- **发送调用一直不返回**：截止点沿用已有时长，不新增配置项。拆分组内的发送用 `delivery.group_max_seconds`（从组的第一条开始计时），其他发送用 `delivery.retry.window_seconds`（从这个 intent 首次尝试开始计时）；安全停止属于 A4。到截止点后触发这次发送自己的取消令牌（组内用组的独立令牌），再等待 `tasks.cancel_grace_seconds`。仍没有收据，就把这次尝试记为 `unknown`，不得记为 `failed`，不重发。之后收到的收据按第 6.1 节迟到收据的规则处理。
- A3.1 不提供"向平台查询是否已送达"的接口。

## 6. 交付状态与有限重发

### 6.1 状态

**intent**：`pending` → `sending` →（`retry_wait` → `sending` …）→ `sent | failed | unknown`

- `retry_wait` 表示上一次尝试是临时性失败，正在等待重发；它不是终态。
- `sent`、`failed`、`unknown` 是终态，之后不再改写。例外只有一种：`unknown` 之后收到 `intent_id` 和尝试序号都匹配的真实收据，可以改为 `sent` 或 `failed`，同时写审计。这只是补全信息，**不触发任何发送**，即使那份收据是临时性失败也一样。

**记录的 `delivery_status`**，由各 intent 汇总：

| 情况 | 结果 | 是否终态 |
|---|---|---|
| 没有 intent | `none` | 是 |
| 有任何 intent 处于 `pending` / `sending` / `retry_wait` | `pending` | 否 |
| 全部是 `sent` | `sent` | 是 |
| 有 `unknown` | `unknown` | 是 |
| 有 `sent` 也有 `failed` | `partial` | 是 |
| 全部是 `failed` | `failed` | 是 |

迟到收据修正 intent 后，重新计算一次汇总。

### 6.2 有限重发（用户已确认）

- 只对 `status = failed` 且 `error.code` 属于临时性错误（`rate_limited`、`transient`）的收据重发。其他 `failed` 视为永久失败，不重发。`unknown` 永不重发。
- 每个 intent 最多尝试 `delivery.retry.max_attempts` 次（默认 3 次，包括首发）。第 k 次重发前等待 `backoff_seconds` 的第 k 项；列表比重发次数短时，用最后一项。等待使用注入时钟。
- 停止重发的条件（满足任一）：
  - 次数用完；
  - 距这个 intent 首次尝试超过 `delivery.retry.window_seconds`（默认 60 秒）。这个窗口适用于所有记录，包括聊天和主动消息；窗口用完视同次数用完，在拆分组中按第 7 节的 `skipped_after_failure` 处理；
  - task 记录超过它的 `deadline`，并且这个 intent 不属于已开始的拆分组（第 7 节）；
  - 记录被取消，并且这个 intent 不属于已开始的拆分组。
- 停止时，intent 的终态为 `failed`，保留最后一次的错误码。

## 7. 拆分发送与取消（用户已确认：发完这一组）

**拆分本身**（用户已确认）：
- 拆分插件可以在任何地方断开，包括为了玩笑效果，故意让前半句造成误会、后半句再翻转。
- 不要求每一段单独都看得懂。

**发送规则**：
- `split.index` 从 0 开始，`total` 是组内条数；组内按 `index` 顺序发送，并遵守各条的 `not_before`。
- 第一条开始尝试后，整组视为"已开始"。已开始的组使用自己的交付令牌，不受普通取消和任务截止时间影响；只在以下情况停止：
  - 某一条的结果为 `unknown`；
  - 某一条永久失败，或重发次数用完；
  - 从第一条开始尝试算起，超过 `delivery.group_max_seconds`（默认 60 秒，用户已确认）；
  - 进程安全停止的超时（A4）。
- 停止后**还没开始尝试**的各条标为 `failed`，`error.code` 分别为 `skipped_after_unknown`、`skipped_after_failure`、`group_timeout`、`shutdown`。
- 组超时和安全停止只阻止开始新的尝试。停止时正在发送的那一条，仍按第 5 节等待收据；等不到时记为 `unknown`，不能标为确定没发。
- 组还没开始时取消，整组都不发，标为 `failed`，`error.code = cancelled_before_send`。
- 重启后剩余各条按第 2 节处理，不补发。

**发了一半的组（用户已确认）**：从 A3.2 起，同一会话下一次组装上下文时，本体注入一条说明，让她可以自然地补上或解释。说明按**每条 intent 的实际状态**如实分三档，不使用记录级的汇总：
- 确定发出的：`发出了前 N 段`；
- 结果为 `unknown` 的：`第 N+1 段不确定是否发出`；
- 之后没有尝试的：`后面的没有发出`。

第一段就是 `unknown` 时，说明为"第 1 段不确定是否发出，后面的没有发出"。不能把不确定说成确定没发。A3.1 只需要保证账本能逐条查出每段的状态（`deliveries[]`）。

## 8. 取消（用户已确认）

内部入口：`cancel(companion_id, actor, conversation_id, task_id=None, reply_to=None)`。

**权限**（所有方式相同）：主人可以取消任何记录；其他人只能取消自己发起的（`requester` 是自己）。没有权限时返回 `denied` 并写审计，不改变状态。

**可见性先于权限（用户已确认，agent-spec §6.2）**：取消入口先按第 13.1 节判断目标记录在当前场景是否可见。看不到的记录一律返回 `not_found`，之后才检查取消权限（`denied`）和是否已结束（`already_finished`）。适用于快速路径、`core_cancel_task` 和引用取消。私聊中发起的任务，要回到能看到它的场景取消。

**选择目标**（按以下顺序，只用第一个适用的方式）：

| 方式 | 目标 |
|---|---|
| 带 `reply_to`（用户引用某条消息说"停"） | 被引用的消息对应的记录：可以是一条入站请求，也可以是她发出的某条回复（取它所属的记录）。排队中的记录也可以这样取消 |
| 带 `task_id` | 这个编号的任务 |
| 都不带 | 先找当前会话中最近一个还没结束的 `kind = task` 记录：主人不论发起人，其他人只找自己的。找不到时，只找 `actor` **自己**排队中、还没开始执行的记录 |

- 不带引用和编号时，**不会取消别人排队中的普通聊天**，主人也一样（用户已确认）。
- 正在执行、但还没有任何能力调用的 chat 记录，只能用 `reply_to` 取消。
- 无论哪种方式，都排除这条取消消息自己产生的记录。
- 引用的消息不在本伴侣账本中，或者对应的记录已经到终态，按下面的返回值处理。

**自然语言指定**（例如"把 XX 的那个画图停掉"）：在 A3.2 实现。由模型判断目标，调用本体提供的取消能力；权限仍按上面的规则由本体检查，模型无法越权。目标对不上或有多个可能时，她要反问，不能猜（用户已确认）。

**编号的显示时机不变**：`task_id` 在升级为 task 时分配，只写入账本，**不在开始时发给用户**。用户仍然只在任务结束时，从随结果附带的任务单中看到编号；执行中也不主动发进度（kernel.md §5）。

**返回值**：`cancelled`（成功）、`denied`、`not_found`（找不到符合条件的任务，包括当前伴侣账本中找不到该 `task_id`）、`already_finished`（执行和交付都已到终态）、`invalid_request`（仅用于参数缺失或 `task_id` 格式不合法）。

**跨伴侣编号（Issue #20，用户已确认）**：取消只查当前伴侣的账本，不跨伴侣查找。其他伴侣的任务编号与不存在的编号同样返回 `not_found`，避免在一个伴侣的对话中暴露另一个伴侣的任务。

**按任务所处阶段**：
- 排队中：直接标为 `cancelled`，不执行。
- 执行中：按 SDK 取消协议（sdk-spec §2、§7.6）处理；宽限期过后仍未结束就标为 `cancelled`，之后才到达的结果写审计。
- 执行已成功、交付还没结束：可以取消。执行状态保持 `ok`，不改写；还没开始的 intent 和还没开始的组标为 `failed`（`cancelled_before_send`）；已开始的组按第 7 节发完。这次取消写审计。
- 取消之后，不再为这条记录登记新的 intent。

文本识别（"取消""停"）在 A3.2 接入，规则见 `agent-spec.md` 第 6 节。A3.1 的内部入口已经支持 `reply_to`，平台插件把引用信息放在 `Message.reply_to` 中传入。

**截止时间**：
- task 记录：升级为 task 时计算一次，之后不变：`deadline = 升级时间 + 工具的 timeout_s`（不含之前的排队时间）。工具没有声明时，用 `tasks.default_deadline_seconds`。两者都不能超过实例配置的 `limits.max_task_seconds`：工具声明超过上限时截断到上限并记 WARN；伴侣配置超过上限属于配置错误，校验时拒绝。
- chat 记录和主动消息没有执行截止时间，交付只受第 6、7 节的窗口限制。

## 9. 调度器

- **时区**：`at` / `days` 使用实例配置的 `timezone`（与 sdk-spec §3、§7.7 一致）。伴侣配置不提供时区。
- **固定频率**：计划时间是"锚点 + k × 周期"。`every` 的锚点是插件启动时刻。
- **`every` 的取值范围（用户已确认）**：只接受能换算成固定秒数的 ISO 8601 时长，即周、日、时、分、秒（`W`、`D`、`H`、`M`（时间部分）、`S`）。
  - 一天严格按 24 小时、一周按 7 天，在 UTC 上换算，不随夏令时变化。
  - 带年（`Y`）或月（日期部分的 `M`）的时长一律拒绝，包括混合形式（如 `P1M1D`）和小数形式（如 `P0.5Y`）。按日历的月 / 年调度以后另行设计。
  - 总时长为零（如 `PT0S`）的一律拒绝。
  - 在清单校验和 SDK 类型校验两处都执行，两处结果必须一致。属于 SDK 0.x 次版本的不兼容变更，写入 CHANGELOG。
- `days` 只属于 `at`：`every` 与 `days` 同时出现（包括 `days: []`）时，清单校验和 SDK 校验都拒绝。`at` 省略 `days` 或给出空列表都表示每天。
- **漏点**：时钟一次越过多个计划时间时，只触发一次。`Tick.scheduled_at` 取被越过的计划时间中最晚的一个，`Tick.missed` 等于其余被跳过的个数。
- **不重叠**：上一次还在执行时，本次跳过，计入下一次触发的 `missed`；触发之后清零。
- **同一时刻**按 `(plugin_id, schedule id)` 排序依次触发。
- **重启后不补触发**：从启动后的下一个计划时间开始，停机期间的计划时间不计入 `missed`。
- **夏令时**：不存在的本地时刻顺延到之后第一个有效时刻，只触发一次；重复出现的本地时刻只在第一次出现时触发。
- 插件停用或卸载时，取消它的全部计划；正在执行的那次按取消协议处理。
- **测试**：MockClock 的 `advance` 只唤醒已有的等待者。测试辅助要提供"推进到某一时刻，并且每次唤醒后让事件循环把新注册的等待处理完"的方法。测试必须覆盖：一次推进跨越多个周期、同一时刻多个任务、执行中被取消、卸载、夏令时边界。

## 10. 事件总线

- 每个伴侣一条有序队列，按发布顺序分发。尽力投递一次，**不持久化，重启不回放**。
- 本体事件在账本事务提交**之后**发布。
- 每个订阅者有独立的超时（默认 2 秒，与行为扩展相同）。订阅者异常或超时只记日志，不影响其他订阅者，也不影响发布方。
- 处理事件时再发布的事件放到队列末尾，不嵌套执行；同一条事件链的深度上限为 8，超过就丢弃并记 WARN。
- 每个伴侣的队列上限 1000 条。满了丢弃新事件，计数并记 WARN。

## 11. A3.1 的离线入口、队列、权限、额度和资产

- **入口**：只在测试中装配的入口，直接构造假的 `Invocation` / `DeliveryIntent`，驱动账本、执行和交付服务（Issue 选项 A）。不提供任何真实发送的路径。
- **队列**（本阶段实现）：
  - 同一会话串行；每个伴侣同时执行的任务数由 `tasks.max_concurrent` 配置（默认 2）。
  - 主人的请求优先。非主人的请求等待超过 60 秒后，提升到与主人同级，避免一直排不上。
- **权限**：A3.1 提供一个默认拒绝的权限检查接口，测试中显式授权；完整的边界检查在 A3.2 接入。
- **额度**：A3.1 提供额度检查接口，测试中由假实现返回"允许"或"不足"；真实额度在 A3.2 接入模型插件后提供。额度不足时返回 `Denial(reason_code = "quota")`。
- **资产**：交付前按平台插件的 `ChannelFeatures.media_limits` 检查格式和大小。`media_limits` 的键见第 16 节。不符合时这条 intent 标为 `failed`，`error.code = asset_rejected`（确定没有发出）。
  - kernel.md §3 的"资产库统一处理"包括格式转换。A3.1 只做检查；格式转换在资产库实现时加入，原设计不变。

## 12. `task` 命令

```
python -m clytie task <task_id> [--companion <cid>] [--json] [--full]
```

- 调用进程内的 API 层（A4 的 HTTP 接口以后复用同一组函数），不直接查询数据库。A3.1 不开 HTTP 服务。
- 以只读方式打开账本：账本不存在时报 `ledger_missing`，**不创建空库**；不修改任何运行状态，也不执行第 2 节的重启修正，也不迁移。
- 存在迁移日志，或者账本模式版本低于代码版本时，报 `ledger_unavailable`，原因注明"迁移中或待迁移"，不读取账本内容。
- `unrecovered` 的判断：`ledger/.lock` 没有被活着的进程持有，而账本里还有非终态时，这些非终态按原值显示并标注 `unrecovered: true`。有进程持有锁时，非终态按正常的运行中显示，`unrecovered: false`。
- 不带 `--companion` 时，在所有伴侣中查找：
  - 只在一个伴侣中找到：正常返回。其他伴侣的账本缺失或损坏时，放进 `warnings`。
  - 多个伴侣有同一编号：列出匹配的伴侣，报 `ambiguous`，不自己猜。
  - 没有找到，并且有账本打不开：报 `ledger_unavailable`，列出打不开的伴侣，不能断言"不存在"。
  - 没有找到，并且账本都正常：报 `not_found`。
- **默认输出摘要**：任务编号、伴侣、类型、能力、请求人、会话、执行状态、交付状态、各阶段耗时、父子任务、每条交付的尝试与收据摘要、错误摘要、产物引用。
- `--full` 加上消息原文、参数和结果。这是本机主人使用的工具，按主人私聊的可见范围显示。

**`--json` 输出**：

```json
{"ok": true, "task": {...}, "warnings": []}
{"ok": false, "error": {"code": "ambiguous", "message": "…", "companions": ["companion_a", "companion_b"]}, "warnings": []}
```

`task` 对象的字段名与上面的摘要项一一对应（`task_id`、`companion_id`、`kind`、`tool`、`requester`、`conversation_id`、`exec_status`、`delivery_status`、`timings`、`parent_task_id`、`child_task_ids`、`deliveries[]`、`error`、`asset_ids`、`unrecovered`）；`--full` 时再加 `content`。

**退出码**：

| 码 | 情况 |
|---|---|
| 0 | 找到 |
| 1 | `not_found` |
| 2 | `ambiguous` |
| 3 | `ledger_missing`、`ledger_corrupt`、`ledger_unavailable`、`schema_too_new` |
| 4 | 参数错误 |

## 13. 账本内容、任务单与表情例外

### 13.1 内容与场景过滤（用户已确认）

- 保存入站消息原文、出站消息原文、能力参数和结果（`ToolResult.data`）、产物引用。
- 每条内容带来源场景和来源会话。读取时的可见范围：

| 读取场景 | 能看到的账本内容 |
|---|---|
| 主人私聊（`owner_private`） | 全部 |
| 伴侣配置 `memory.private_scenes` 中列出的场景（例如家庭群） | 本会话的内容，加上来源为主人私聊的内容。**与记忆共用这个开关**，默认不开 |
| 其他任何场景 | 只有本会话自己的内容 |

  不同陌生人的私聊之间、不同群之间互相隔离。
- 过滤同样适用于骨架和摘要：读取方看不到的记录，连"存在这条记录"也看不到。
- 插件不能读取账本：A3.1 没有账本读取扩展点。模型看到的内容由本体按上表投影后注入（A3.2）。

### 13.2 脱敏与保留期

- **脱敏**：工具参数 Schema 中带 `"x-clytie-sensitive": true` 的属性，写入账本前替换为 `"[redacted]"`。
  - 适用于任意层级，包括 `properties`、`items` 和嵌套对象。
  - `oneOf` / `anyOf` / `allOf` 的任一分支把某属性标为敏感，就按敏感处理。
- `ToolResult.data` 和收据 `error.message` 没有 Schema，不自动脱敏。插件开发规范要求不把凭据写进这两处（写进 plugins.md）。
- **保留期**：`ledger.content_retention_days`，默认不限期。到期时清除以下内容，替换为 `null` 并写一条审计：
  - 入站原文、出站原文；
  - 参数、结果；
  - 审计中迟到结果的内容、收据的 `error.message`。
- 清除后保留骨架：状态、耗时、收据状态与错误码、编号、`dedupe`。账本始终可查。
- 日志仍然**不记录正文**（kernel.md §7 不变）。

### 13.3 任务单

模板变量的来源：
- `label`：伴侣配置新增 `task_card.label`，格式 `{internal: str, external: str}`，默认都是 `"任务"`。
- `name`：对内用 `names.internal`；对外用 `names.external[scene]`，没有为这个场景配置时用 `names.internal`。如果 `names.internal` 在 `forbidden_names` 中，由出站检查拦截（A3.2）。
- `helper`：协助的伴侣在它自己配置中、相同规则下的名字。没有协助方时，省略模板中含 `{helper}` 的那一段（以 `·` 分隔）。
- `duration`：执行耗时，四舍五入到秒，超过 60 秒显示为分钟。

伴侣配置中没有模板时，使用 kernel.md §4 中的示例模板。

### 13.4 表情例外

- 主人私聊中 `Ignore` 降级为 `ReactOnly` 是本体保护（sdk-spec 合并规则），是**唯一**的本体例外，已在 kernel.md §5 写明，不需要插件声明。
- 插件返回 `ReactOnly`，必须在清单中声明了 `message.react` 权限。没有声明时，这条指令按插件出错处理（使用安全默认值）并记 WARN。
- 除上述两种情况外，只有任务（`record.kind = task`）可以加表情回应，即下面第 13.5 节的任务状态回应。

### 13.5 任务状态回应（用户已确认：沿用旧系统的四个状态，所有有权限的人）

在委托消息上用表情显示任务状态，**不发新消息**。这是挂在原消息上的状态标记，不属于 kernel.md §5 所说的"主动发进度"。

**四个状态与时机**：

| 状态 | 时机 | 默认表情 |
|---|---|---|
| `accepted` 接单 | 记录升级为 task 的事务提交后（与 `task.started` 同时） | 👌 |
| `running` 执行中 | 第一次能力调用开始 `execute` 时 | ⏳ |
| `done` 完成 | 发布 `task.completed` 时 | ✅ |
| `failed` 失败 | 发布 `task.failed` 时（包括 `error`、`denied`、`cancelled`） | ❌ |

- 状态变化时，用新表情替换上一个：DeliveryIntent 的 `replaces_emoji` 填上一个**确定发出**的状态表情。平台支持撤掉表情时（`ChannelFeatures.supports_react_remove`），插件先撤旧的再加新的；不支持时只加新的。
- 两次状态变化间隔小于 `task_reactions.min_interval_seconds`（默认 2 秒）时，合并为后一个状态。例如接单后马上开始执行，只显示"执行中"。终态（`done` / `failed`）总是发出，不会被合并掉。
- 平台不支持表情回应（`supports_react = false`）时，静默跳过。

**适用范围**：
- 只用于有来源入站消息的 task 记录，表情加在那条入站消息上。定时任务、主动任务没有来源消息，不加。
- `task_reactions.audience`：`all`（默认，所有有权限发起这个任务的人）或 `owner`（只有主人的委托）。
- `task_reactions.enabled`：默认 `true`；设为 `false` 时全部关闭。
- 状态回应由本体发出，不需要插件声明 `message.react`。插件自己的表情例外（例如睡眠插件的 💤）仍按第 13.4 节声明。

**与交付规则的关系**：
- 状态回应在账本中登记为 `kind = react` 的 intent，并标记 `status_reaction = true`。它们**不计入**记录的 `delivery_status` 汇总，不受执行封口（第 2 节）和"取消后不登记新 intent"（第 8 节）的限制，否则取消后无法显示"失败"。
- 状态回应**不重发**：任何 `failed` 或 `unknown` 都不重试，也绝不因为表情失败另发一条文字消息。
- 一个状态还没发出、下一个状态已经到来时，跳过还没发出的那个，直接发新的。

**重启时**：被重启修正为 `interrupted` 的 task 记录，如果最后一次确定发出的状态回应是 `accepted` 或 `running`，补发一次 `failed` 状态回应，避免原消息一直显示"执行中"。这是更新状态，不是重放任务：只发这一个表情，不执行能力、不发文字，结果不确定时也不再重试。其余重启修正仍按第 2 节只写审计。

## 14. 账本迁移

账本属于本体，不使用插件的升级快照和迁移日志（data-and-backup §4.2 不变），规则如下。

**锁与时机**
- 锁：`companions/<cid>/ledger/.lock`，跨进程有效，伴侣运行期间由伴侣进程持有。
- 迁移只能由**已经取得这把锁**的伴侣启动流程执行。取不到锁（另一个进程正在运行这个伴侣或正在迁移）时，这次启动失败并报告，不迁移。
- 伴侣启动、打开账本时自动迁移，不需要确认（这是本体的数据格式升级，不是插件数据）。
- `task` 命令的只读打开不取锁，也不迁移（第 12 节）。

**快照（用户已确认：加密，只留最近 2 份，服从内容保留期）**
- 迁移前生成加密快照：用实例备份密钥加密（与常规备份、插件升级快照相同，见 data-and-backup §4 和 ADR 0004），存放在 `backups/ledger/<cid>/<从版本>-<到版本>-<时间>/`。
- 快照不包含 `.lock` 和 `.migration.json`。
- 生成后解密到临时目录逐文件校验哈希，校验失败就不迁移。
- 保留：每个伴侣只保留最近 2 份账本迁移快照，更早的在下一次迁移成功后逐个文件删除。设置了 `ledger.content_retention_days` 时，生成时间超过保留期的快照也删除。删除只针对明确的快照目录，逐个文件进行，不批量删除。
- 账本迁移快照不参与 data-and-backup 常规备份的 30 天 / 每周 / 每月保留规则。
- **过期检查的时机**：只在伴侣进程取得锁、并且处理完迁移日志之后执行，与第 13.2 节的账本内容清理同时进行。只要存在迁移日志伴侣就不启动，所以清理不会删掉日志正在引用的恢复来源。伴侣被阻止期间，快照和账本过期内容都不清理。到期时间按快照生成时间计算。

**失败现场（用户已确认：加密另存）**
- 从快照恢复**之前**，先把当前账本文件（不含 `.lock` 和 `.migration.json`）用实例备份密钥加密，保存到 `backups/ledger/<cid>/failed-<从版本>-<到版本>-<时间>/`，满足 AGENTS.md 3.3"恢复之前先自动快照"。
- 失败现场解密校验通过后，才开始恢复。保存或校验失败时不恢复：日志保持当前阶段并记录原因，伴侣不启动，status 显示错误级提醒。
- 失败现场的路径写入迁移日志，并在 status 中显示。
- 不计入"最近 2 份"，不会挤掉有效的迁移前快照。
- 和迁移前快照一样服从 `ledger.content_retention_days`（按生成时间），执行时机同上。没有设置保留期时一直保留，由用户手动删除。

**迁移日志与提交点**
- 迁移日志：`companions/<cid>/ledger/.migration.json`，记录从版本、到版本、快照路径、失败现场路径、阶段、执行者、错误信息。
- 阶段：`migrating`（正在迁移）→ `restoring`（失败后正在保存失败现场并从快照恢复）→ `failed`（已恢复到旧版本，阻止自动重试）。
- 写入顺序：快照校验通过 → 原子写入日志（先写临时文件再替换，阶段 `migrating`）→ 第一个迁移事务。日志写失败就不迁移。
- 提交点：账本内的模式版本号，在最后一个迁移事务中写入。写入成功后删除日志。
- 运行中迁移失败或被取消时，只能进入下表已有的状态（`restoring` → 保存失败现场 → 恢复 → `failed`），不新增阶段。

**恢复时的保护**
- 恢复全程持有 `ledger/.lock`。
- 不整体改名或替换 `ledger/` 目录：锁文件在目录内，整体改名后，新进程可能拿到另一把锁。
- `.lock` 和 `.migration.json` 不移动、不覆盖、不删除。
- 按快照清单逐个文件写回。迁移新增的文件（快照中没有的），在已存入失败现场之后逐个删除，不批量删除。

**启动时发现迁移日志**

| 日志阶段 | 账本模式版本 | 处理 |
|---|---|---|
| `migrating` | 已是"到版本" | 迁移已提交，只删除残留日志并写审计，正常启动 |
| `migrating` | 仍是"从版本" | 视为迁移中途失败：日志先改为 `restoring`，从快照恢复，成功后改为 `failed`；伴侣不启动 |
| `migrating` | 其他值或读不出 | 无法判断：不动账本，日志保持 `migrating` 并记录原因，伴侣不启动，status 显示错误级提醒 |
| `restoring` | 任意 | 恢复中途中断：伴侣不启动，status 显示错误级提醒，不自动重试 |
| `failed` | 任意 | 上次迁移已失败并恢复：伴侣不启动，不自动重新迁移 |

- 所有不启动的情况，status 都显示：伴侣、阶段、原因、快照路径，以及处理方法。其他伴侣照常运行。
- A3.1 不提供解除入口。处理方法写在 status 中：修好代码，或手动用快照恢复账本后，由用户删除 `companions/<cid>/ledger/.migration.json` 这一个文件，再启动伴侣。`failed` 阶段删除日志后，下次启动会重新尝试迁移。

## 15. 版本短锁与事件循环（#8）

采用 Issue 选项 B：`Operations.metadata` 改为异步获取，两次尝试之间用注入时钟异步等待，不阻塞事件循环；上限和超时异常（`MetadataLockTimeout`）不变。不使用工作线程。

保持不变：
- 跨进程互斥；
- 只改本插件的版本项；
- 不伪报启动或恢复错误；
- 提交点前后崩溃时的一致性；
- 取消协议：等待中被取消时，不留下部分写入。

验收：另一个进程持有 `plugin-versions.lock` 期间，另一个伴侣的任务、Tick 和取消照常推进（用 MockClock 量化延迟）；超时后版本项不丢失，提交点行为不变。

## 16. 需要同步修改的其他文档

### 实现技术澄清（只读询问 Claude 后采用）

- 取消入口排除自身记录，通过本体的调用上下文绑定当前 trace；后台事件、调度和状态回应不继承这个绑定。不新增插件可控的取消参数。
- 版本短锁等待中取消：提交点之前的迁移按既有恢复流程完成清理，再传播取消；解除操作保留失败与选择审计，按原规则重新计划，不留下部分版本项。
- 只读任务查询直接探测系统锁状态，不尝试取得锁：Windows 只读第零字节的锁冲突，Linux 读取锁表并匹配文件身份；不能读取时报告查询失败，不推断任务不存在。锁文件首次创建时先放入固定第零字节，再取得锁。
- 内容保留期按每项内容实际入账时间（注入时钟）计算，迟到内容不沿用记录创建时间。持有运行锁、处理完迁移日志后启动时清理，运行期间定期清理；查询命令不清理。清理频率作为实现选型记录在 ADR 0005。

| 文档 | 修改 |
|---|---|
| sdk-spec §1.3 | `EventEnvelope` 新增 `dedupable: bool`，默认 `true`（第 3 节）。属于 0.x 次版本变更，写入 CHANGELOG |
| sdk-spec §7.7 / plugins.md §4 | 工具清单新增可选字段 `timeout_s`（正整数，秒）；参数 Schema 支持 `x-clytie-sensitive` 注解（第 8、13 节） |
| sdk-spec ChannelFeatures | `media_limits` 的键：`{image\|audio\|video\|file: {max_bytes: int, formats: [media_type]}}`；没有列出的类型视为不支持 |
| plugins.md 能力插件规范 | 不把凭据写进 `ToolResult.data` 和收据的 `error.message` |
| config-spec §3 | 新增或更新下表字段 |
| plugins.md §3 / sdk-spec §5.5 | `dedupable = false` 的事件经过过滤但不经过防重，可能重复到达，仅供 `message_received` 观察，不触发任务或回复 |
| data-and-backup §1 目录树 | 加入 `backups/ledger/<cid>/`（账本迁移快照）和 `companions/<cid>/ledger/.migration.json` |
| data-and-backup §4 | 写明账本迁移快照的加密、存放位置和保留规则（第 14 节），并注明它不参与常规备份的保留规则 |
| sdk-spec §1.4 DeliveryIntent | 新增可选字段 `replaces_emoji: str?`（`kind = react` 时使用，第 13.5 节） |
| sdk-spec §5.1 ChannelFeatures | 新增 `supports_react_remove: bool`，默认 `false`（第 13.5 节） |
| kernel.md §5 | 写明本体的任务状态回应（四个状态，替换上一个，不发新消息），并注明它不属于"主动发进度"（第 13.5 节） |
| config-spec §3 | 新增 `task_reactions`：`enabled`（布尔，默认 `true`）、`audience`（`all` / `owner`，默认 `all`）、`emojis`（`{accepted, running, done, failed}`，默认 👌 ⏳ ✅ ❌，各为非空字符串，由平台插件映射为平台表情）、`min_interval_seconds`（整数 0–30，默认 2） |
| data-and-backup §4 / §4.1 | 加入失败现场 `backups/ledger/<cid>/failed-…/` 的加密与保留（第 14 节）；§4.1 的密钥适用范围列出账本迁移快照和失败现场；删除 §4 中照抄带来的自引用"见 data-and-backup §4" |

**config-spec 字段**（替换现有的"未规定"）：

| 字段 | 类型 | 默认值 | 范围 |
|---|---|---|---|
| `delivery.retry.max_attempts` | 整数 | 3 | 1–5，包括首发 |
| `delivery.retry.backoff_seconds` | 数字列表 | `[2, 5]` | 每项 0.5–60；长度 0–4；比重发次数短时用最后一项；为空时立即重发 |
| `delivery.retry.window_seconds` | 整数 | 60 | 10–600 |
| `delivery.group_max_seconds` | 整数 | 60 | 10–300 |
| `tasks.cancel_grace_seconds` | 整数 | 5 | 1–60 |
| `tasks.default_deadline_seconds` | 整数 | 600 | 10 到 `limits.max_task_seconds`；超过上限时配置校验失败 |
| `tasks.max_concurrent` | 整数 | 2 | 1–8 |
| `ledger.content_retention_days` | 整数或 `null` | `null`（不限期） | 不小于 1 |
| `task_card.label` | `{internal: str, external: str}` | 都是 `"任务"` | 非空 |
| 实例 `limits.max_task_seconds` | 整数 | 3600 | 60–86,400 |

## A4.1 停止与静默同步

按 `supervisor-spec.md` §4.3、§6、§7，入站 ignored 增加原因 `shutting_down`、`silenced`、`model_auth_invalid`，照常写入 dedupe / inbound，不回复，重启或解除静默后不补回复。intent 增加错误码 `shutdown_before_send`、`silenced`。静默阻止新 intent 和表情；发送组在当前一条结束后停止，剩余标为 silenced；主动消息直接丢弃并审计。安全停止与正在执行任务的交付遵循该规格 §6。

## A4.3 在线账本捕获与恢复（待实现）

以下为已批准的 A4.3 契约，尚未实现、未经验收；完整依据为 `backup-spec.md`，不代表历史里程碑已提供这些能力。

账本、记忆和回想进度必须按 backup-spec §4.1、§9.1 建立共同捕获边界或可核验版本链，不直接复制活动 db/wal；损坏或来源错配不能降级为正常缺失。

保留周期配置，用户正常启用后继续未来触发，默认不补发停机错过的提醒；不重放旧 tick 队列。计划时区/防重沿用内核，备份本身的错过补当前快照是专用内部策略。
保留长期目标、步骤、检查点、已知外部结果，但 A4.3 不新增任意任务自动续跑引擎。原已开始 invocation 按账本恢复规则中断/未知，不直接把旧执行状态改为 queued。
具有后续专门恢复协议的任务可在校验外部结果/幂等凭据并获该协议授权后从安全检查点继续；本阶段没有该协议的任务显示待人工核验。结果未知的发送/上传/提交不自动重试，不能靠新任务编号绕过。不把“保留进度”或“用户切换确认”解释为执行全部旧任务的授权。

## Issue #72：账本连接所有权（用户已确认）

按 storage-spec.md，运行账本连接与记忆连接由该伴侣专用 StorageOwner 创建、使用和关闭；读取也经内部异步入口。完整写命令在协调及事务内读取校验，不在事件循环持有连接，不将事务拆成跨 await 的 SQL 片段。发送前登记、收据、防重、取消及恢复语义不变。

副作用前取得必要收尾容量；容量凭证覆盖本次操作的多次持久记录、重试及迟到结果责任，未就绪的预留不占 FIFO 队头。unknown 不重发、旧任务不重放。跨库串行不冒充跨库原子提交，既有提交证据和恢复顺序仍必需。

本文件 §15 的 plugin-versions.metadata 无工作线程规则完全保留；StorageOwner 不替换版本短锁实现。
