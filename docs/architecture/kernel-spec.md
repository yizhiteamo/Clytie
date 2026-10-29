# 本体主干实现规格（A3.1：账本与交付）

回答 Issue #12 的 14 组问题。设计依据是 `design/kernel.md`，本文件只补实现必须的契约。标注"用户已确认"的条目是需求或隐私决定，改动前必须经用户确认；其余是技术约定，按 AGENTS.md 第 4 节处理。

物理存储（数据库选型、表与索引）另写 ADR，但不能改变本文件的逻辑结构和原子操作。

---

## 1. 账本的逻辑结构与原子操作

每个伴侣一本账本，位置 `companions/<cid>/ledger/`，只有本体能读写。采用分开定义的实体（Issue 选项 A）：

| 实体 | 主键 | 关键字段 |
|---|---|---|
| `inbound` 入站消息 | `event_id` | `companion_id`、`channel_id`、`platform_message_id`、`conversation_id`、`sender`（逻辑身份）、`scene`、`received_at`、`parts`（原文，见第 13 节）、`dedupable` |
| `record` 处理记录 | `trace_id` | `source_event_id?`（主动消息、定时任务为空）、`conversation_id`、`requester?`、`kind: chat\|task`、`task_id?`（任务才有）、`parent_trace_id?`、`scene`、`exec_status`、`delivery_status`、各阶段耗时、`created_at` |
| `invocation` 能力调用 | `invocation_id` | `trace_id`、`plugin_id`、`tool`、`idem_key`、`args`、`status`、`result`、`error?`、`asset_ids`、开始/结束时间 |
| `intent` 待交付 | `intent_id` | `trace_id`、`kind`、`parts`、`target?`、`split?`、`not_before?`、`status` |
| `attempt` 发送尝试 | `(intent_id, n)` | `status: sending\|sent\|failed\|unknown`、`started_at`、`receipt?` |
| `audit` 审计 | 自增 | 迟到结果、迟到收据、冲突收据、取消请求、状态修正 |

一条入站消息最多对应一条 `record`；一条 `record` 可以有多个 `invocation` 和多个 `intent`。

**原子操作**（每个都是一次事务）：
- `claim_inbound(event)`：按第 3 节的防重键插入；已存在时返回已有的 `event_id` 和对应的 `trace_id`，不新建记录。
- `claim_invocation(key, …)`：按第 4 节的幂等键插入；已存在时返回已有调用的状态和结果。
- `register_intents([...])`：一次登记一组待交付消息，状态为 `pending`。
- `begin_attempt(intent_id)`：在调用平台插件**之前**写入 `sending`。
- `record_receipt(intent_id, n, receipt)`：写回收据，并更新 intent 和 record 的交付状态。

账本带模式版本号，只能向前迁移；迁移前自动快照（与插件私有状态相同的规则）。代码版本低于账本版本时拒绝打开，报 `schema_too_new`。

## 2. 状态（两个轴分开）

**执行状态** `exec_status`：`pending` → `running` → `ok | error | denied | cancelled | interrupted`
**交付状态** `delivery_status`：`none | pending | partial | sent | failed | unknown`

- 终态不可改写，先到的终态生效；之后到达的结果写进 `audit`。
- **迟到收据**是例外：`unknown` 的尝试之后收到 `intent_id` 和尝试序号都匹配的真实收据时，可以改为 `sent` 或 `failed`，同时写审计。这只是补全信息，**不触发任何发送**。
- `interrupted` 只在账本中使用，不加入 SDK 枚举。对外的 `TaskSummary` 中映射为 `status: error`、`error_code: interrupted`。
- 记录的 `delivery_status` 由各个 intent 汇总：全部 `sent` 为 `sent`；有 `unknown` 为 `unknown`；有发出也有未发出为 `partial`；全部确定未发出为 `failed`。

**重启时**（伴侣启动、打开账本之后、接收新消息之前）：
- `pending` / `running` 的执行 → `interrupted`。
- `sending` 的尝试 → `unknown`。
- 还没开始尝试的 `pending` intent → `failed`，`error.code = not_sent_before_restart`。它们确实没有发出，但**不会补发**。
- 这些修正都写审计，任何旧任务都不重放。

**事件**（负载以 sdk-spec §7.4 为准）：
- 执行到达终态时发布：`ok` 发 `task.completed`；其他终态发 `task.failed`。事件表示执行结束，与交付结果无关。
- 交付结果通过 `reply.sent` 和账本查询获得。

## 3. 入站防重

- 防重键：`(companion_id, channel_id, platform_message_id)`。`channel_id` 是逻辑 `account_ref`，所以每个平台账号自成一个命名空间；不同伴侣各自独立。`event_id` 的格式保持 SDK 已定的 `channel_id:platform_message_id`。
- 平台插件必须保证 `platform_message_id` 在本账号内唯一并且稳定。
- 平台本身没有消息 ID 的事件（通知、撤回等）：插件用平台提供的稳定字段合成 ID。无法合成时 `dedupable = false`，这类事件只能观察，**不能触发任务或回复**。
- 重复事件：不处理，计数，DEBUG 日志。重启后再次收到旧事件同样按重复处理。
- 防重记录随 `inbound` 保留（见第 13 节保留期），至少保留 7 天。

## 4. 能力幂等键

- 完整的幂等键：`(companion_id, plugin_id, tool, root_event, plugin_key)`。
  - `root_event` 是触发这次调用的入站事件；定时或主动任务用 `trace_id`。
  - `plugin_key` 是插件 `prepare` 返回的键。
- 因为键里带了触发事件，用户再发一条消息、用同样参数提出请求时，键自然不同，不会被误合并；同一事件被重复处理时，键相同，只查询已有结果。
- 在 `prepare` 成功之后、执行之前领取。`prepare` 失败不占用键。
- 已存在时：仍在执行就返回 `in_progress` 和已有的 `task_id`，不新开执行；已到终态就返回已有结果（包括 `interrupted`）。
- 幂等记录随 `invocation` 保留。

## 5. 发送提交点

固定顺序：`register_intents` → `begin_attempt` 提交 → 调用平台插件 → `record_receipt`。

- 写入 `sending` 之后、收据写回之前崩溃 → 重启时标为 `unknown`（第 2 节），永不重发。
- 收据的 `intent_id` 与本次尝试不一致 → 拒绝这份收据，本次尝试标为 `unknown`，记 WARN 和审计。
- 同一尝试重复收到收据：内容相同就忽略；内容冲突就保留第一份，其余写审计。
- 平台插件抛出异常（而不是返回收据）→ 按 `unknown` 处理，因为无法证明没有发出。
- A3.1 不提供"向平台查询是否已送达"的接口。

## 6. 发送失败的有限重发（用户已确认）

- 只对 `status = failed` 且 `error.code` 属于临时性错误（`rate_limited`、`transient`）的收据重发。其他 `failed` 视为永久失败，不重发。`unknown` 永不重发。
  - 这是对 Receipt 已有 `error.code` 字段的约定，在 sdk-spec §1.4 补一句说明，不修改类型。
- 每个 intent 最多尝试 3 次（首发加 2 次重发）。每次重发都是同一 intent 的新尝试（`n` 加一），用注入时钟等待：第一次重发前等 2 秒，第二次前等 5 秒。
- 超过任务截止时间就不再重发。
- 记录在 `cancelled` 之后不再开始新的重发，但已开始的拆分组例外（第 7 节）。
- 配置：伴侣配置 `delivery.retry.max_attempts`（默认 3，范围 1–5）和 `delivery.retry.backoff_seconds`（默认 `[2, 5]`）。

## 7. 拆分发送与取消（用户已确认：发完这一组）

- `split.index` 从 0 开始，`total` 是组内条数；组内按 `index` 顺序发送，并遵守各条的 `not_before`。
- 第一条开始尝试后，整组视为"已开始"：普通取消不打断这一组，取消只影响之后的任务和交付。
- 已开始的组仍会在以下情况停止，剩余各条标为 `failed`，`error.code = skipped_after_unknown` 或 `skipped_after_failure`：
  - 某一条的结果为 `unknown`；
  - 某一条永久失败，或重发次数用完。
- 组还没开始时取消，整组都不发，标为 `failed`，`error.code = cancelled_before_send`。
- 安全停止时等待已开始的组发完（有超时，A4 实现）。重启后剩余各条按第 2 节处理，不补发。

## 8. 取消（用户已确认：请求人和主人）

- A3.1 提供内部入口 `cancel(companion_id, actor, task_id=None, conversation_id=None)`：
  - 带 `task_id`：取消这个任务。
  - 不带 `task_id`：取消 `actor` 在当前会话中最近一个还没结束的任务。
- **权限**：`actor` 是主人，或者是该任务的 `requester`。其他人的请求返回 `denied` 并写审计，不改变任务状态。
- 排队中的任务直接标为 `cancelled`，不执行。执行中的任务按 SDK 取消协议（sdk-spec §2、§7.6）处理，宽限期过后仍未结束就标为 `cancelled`，之后才到达的结果写审计。
- 取消之后不再登记新的 intent；已开始的拆分组按第 7 节处理。
- 文本识别（"取消""停"）在 A3.2 接入，规则在 A3.2 规格中另写。
- 配置：伴侣配置 `tasks.cancel_grace_seconds`（默认 5）和 `tasks.default_deadline_seconds`（默认 600）。插件清单中工具声明的截止时间优先，但不能超过实例配置 `limits.max_task_seconds`（默认 3600）。

## 9. 调度器

- **固定频率**：计划时间是"锚点 + k × 周期"。`every` 的锚点是插件启动时刻；`at` / `days` 按伴侣配置的时区计算。
- **漏点**：时钟一次越过多个计划时间时，只触发一次，`Tick.missed` 等于跳过的次数。
- **不重叠**：上一次还在执行时，本次跳过，计入下一次的 `missed`。
- **同一时刻**按 `(plugin_id, schedule_name)` 排序依次触发。
- **重启后不补触发**：从启动后的下一个计划时间开始，停机期间的计划时间不计入 `missed`。
- **夏令时**：不存在的本地时刻顺延到之后第一个有效时刻，只触发一次；重复出现的本地时刻只在第一次出现时触发。
- 插件停用或卸载时取消它的全部计划，正在执行的那次按取消协议处理。
- **测试**：MockClock 的 `advance` 只唤醒已有的等待者。测试辅助要提供"推进到某一时刻，并且每次唤醒后让事件循环把新注册的等待处理完"的方法。测试必须覆盖：一次推进跨越多个周期、同一时刻多个任务、执行中被取消、卸载、夏令时边界。

## 10. 事件总线

- 每个伴侣一条有序队列，按发布顺序分发。尽力投递一次，**不持久化，重启不回放**。
- 本体事件在账本事务提交**之后**发布。
- 每个订阅者有独立的超时（默认 2 秒，与行为扩展相同）；订阅者异常或超时只记日志，不影响其他订阅者，也不影响发布方。
- 处理事件时再发布的事件放到队列末尾，不嵌套执行；同一条事件链的深度上限为 8，超过就丢弃并记 WARN。
- 每个伴侣的队列上限 1000 条，满了丢弃新事件，计数并记 WARN。

## 11. A3.1 的离线入口、队列和资产

- **入口**：只在测试中装配的入口，直接构造假的 `Invocation` / `DeliveryIntent`，驱动账本、执行和交付服务（Issue 选项 A）。不提供任何真实发送的路径。
- **队列**（本阶段实现）：
  - 同一会话串行；每个伴侣同时执行的任务数由 `tasks.max_concurrent` 配置（默认 2）。
  - 主人的请求优先。非主人的请求等待超过 60 秒后提升到与主人同级，避免一直排不上。
- **权限**：A3.1 提供一个默认拒绝的权限检查接口，测试中显式授权；完整的边界检查在 A3.2 接入。
- **资产**：交付前按平台插件的 `ChannelFeatures` 检查格式和大小。不符合时这条 intent 标为 `failed`，`error.code = asset_rejected`（确定没有发出）。A3.1 不做格式转换。

## 12. `task` 命令

```
python -m clytie task <task_id> [--companion <cid>] [--json] [--full]
```

- 调用进程内的 API 层（A4 的 HTTP 接口以后复用同一组函数），不直接查询数据库。A3.1 不开 HTTP 服务。
- 不带 `--companion` 时在所有伴侣中查找。多个伴侣有同一编号时，列出匹配的伴侣并退出，退出码 2，不自己猜。
- 以只读方式打开账本：账本不存在时报 `ledger_missing`，**不创建空库**；不修改任何运行状态。
- 默认输出摘要：任务编号、伴侣、类型、能力、请求人、会话、执行状态、交付状态、各阶段耗时、父子任务、每条交付的尝试与收据摘要、错误摘要、产物引用。
- `--full` 加上消息原文、参数和结果。这是本机主人使用的工具，按主人私聊的可见范围显示。
- 错误码：`not_found`、`ambiguous`、`ledger_missing`、`ledger_corrupt`、`schema_too_new`。`--json` 时输出 `{ok, error?, task?}`。

## 13. 账本内容、任务单与表情例外

**账本内容（用户已确认：完整保存，按场景过滤）**
- 保存入站消息原文、出站消息原文、能力参数和结果（`ToolResult.data`）、产物引用。
- 每条内容带来源场景。读取时按场景投影，规则与记忆的可见等级相同：群聊场景读不到私聊内容，插件和模型拿不到超出当前场景的账本内容。
- 插件清单的参数 Schema 中标为 `sensitive` 的字段，写入账本前替换为 `[redacted]`。
- 保留期：伴侣配置 `ledger.content_retention_days`，默认不限期。到期只清除原文、参数和结果，骨架（状态、耗时、收据、编号）保留，账本始终可查。
- 日志仍然**不记录正文**（kernel.md §7 不变）。

**任务单**：`label`、`name`、`helper` 来自伴侣配置中的显示名，按场景（对内 / 对外）选择。没有协助方时省略整个"协助"段。伴侣配置中没有模板时，使用 kernel.md §4 中的示例模板。

**表情例外**：
- 主人私聊中 `Ignore` 降级为 `ReactOnly` 是本体保护（sdk-spec 合并规则），是**唯一**的本体例外，在 kernel.md §5 中写明，不需要插件声明。
- 插件返回 `ReactOnly`，必须在清单中声明了 `message.react` 权限；没有声明时，这条指令按插件出错处理（使用安全默认值）并记 WARN。
- 除上述两种情况外，只有任务（`record.kind = task`）可以加表情回应。

## 14. 版本短锁与事件循环（#8）

采用 Issue 选项 B：`Operations.metadata` 改为异步获取，两次尝试之间用注入时钟异步等待，不阻塞事件循环；上限和超时异常（`MetadataLockTimeout`）不变。不使用工作线程。

保持不变：跨进程互斥、只改本插件版本项、不伪报启动或恢复错误、提交点前后崩溃时的一致性、取消协议（等待中被取消时不留下部分写入）。

验收：另一个进程持有 `plugin-versions.lock` 期间，另一个伴侣的任务、Tick 和取消照常推进（用 MockClock 量化延迟）；超时后版本项不丢失、提交点行为不变。
