# 变更记录

## 0.7.0（A3.4，待验收）

- Issue #35：撤销内容仅由本体内部比对，窗口只给撤销条数；新增 recollect.retracted_similarity（0–1，默认 0.85）。private_scenes 既有隐私边界不变；永不淡化优先，跨群印象仍会淡。

- 官方插件 SDK 范围同步为 `>=0.7,<0.8`。
- SDK 0.x 次版本不兼容变更：consolidator 替换旧的四参数签名为 `run(window: RecollectWindow, reader: CandidateReader, cancel) -> list[RecollectItem]`，新增窗口和候选类型，出处由本体核对；core: 标签保留给本体。
- 记忆库模式版本加一，新增回想进度与淡化状态表，并提供 A3.3 库迁移及旧快照回归。
- 导出格式版本加一，增加加密的淡化状态，继续支持旧包；回想进度不导出。具体表结构与版本编码在 ADR 记录。
- 回想、印象、软遗忘、黑话、读取降级与重复合并遵循 memory-recall-spec；只处理新对话，不回溯账本。

## 0.6.0（A3.3，待验收）

- SDK 0.x 次版本不兼容变更：导入器返回 `ImportCandidate`，替代可携带可信字段的 `MemoryRecord`；候选字段和账号引用编码见 ADR 0008。官方插件的 SDK 范围同步为 `>=0.6,<0.7`。

- 第一条记录或首次导入确定 companion_uid，之后不变；空库无 UID。导入仅支持搬回自己的记忆，不支持外来伴侣；空库 new 需确认并沿用包的 UID。
- 从零开始，无预置记忆；投影为空时注入固定说明，空库不能导出。
- memory.importer 改为输出平台账号/会话引用和可信程度提议，由本体映射、用户确认可信提议；不能直接填写可信字段。仅支持空库和同一来源未被写入的试导入例外（memory-spec §9.4）。
- 用户确认的 restore、撤销导入、冲突选择及导入器再次导入可恢复目标可信程度，计划单列降低项；聊天和插件操作只升不降。

- MemoryRecord 新增本体填写的 basis、origin_conversation_id；旧格式读取按 write_mode 推导可信程度，未知来源为 null。写入忽略外部可信字段，插件只能修改或撤销 observed 记录；owner_confirmed、owner_stated 只读。
- 新增兼容扩展 memory.reranker，候选由本体投影后传入。
- 语义路新增 `memory.semantic_min_similarity`（默认 0.5）；向量缓存身份包含版本、维度与配置指纹；一次回复内记忆编号稳定，搬家保留修订来源元数据。
- 新增 memory 的核心/检索预算、召回条数、重排候选数、写入次数、泄漏片段阈值、embedder/reranker 配置。契约见 memory-spec §10。
- A3.3 覆盖当场记、存储与投影、检索、加密导入导出；回想、群友印象和软遗忘等拆至 A3.4。

## 0.5.0（A3.2，待验收）

- 新增对话编排、串行工具循环、人设与能力上下文、边界暂停、出站改写检查及主动话题；接入统一账本、交付和取消，全部使用离线假后端验收。

- `ContextRequest.actor` 改为可空，主动话题传 `None`；上下文插件需处理没有请求人的情况。普通回复仍传入发送者。场景与记忆权限规则不变（只读技术澄清，agent-spec §15.4）。

- **SDK 不兼容变更（Issue #22，用户确认）**：`proactive.submit` 改为仅关键字的 `topic` / `material` 或 `parts` 两种提交方式，返回 `trace_id`。文字由模型生成，可选择不发；直接发送只允许表情和媒体，暂不提供能力。详见 agent-spec §15。

- **SDK 不兼容变更**：工具名不能以 `core_` 开头，该前缀保留给本体工具；清单校验拒绝此前缀（`agent-spec.md` §3.3 / §14）。

## 0.4.0（A3.1，待验收）

- **SDK 不兼容变更（Issue #17，用户确认）**：`schedule.every` 仅接受周、日、时、分、秒的固定时长，拒绝月、年（包括混合和小数形式）以及总时长为零的值。一天按 24 小时、一周按 7 天在 UTC 上换算。`every` 与 `days` 同时出现时一律拒绝（包括空列表）；`at` 省略 `days` 或给出空列表都表示每天。清单和 SDK 校验保持一致，依据 kernel-spec §9。
- `EventEnvelope` 新增 `dedupable: bool`，默认 `true`；不可防重事件只能观察，不触发任务或回复。
- 工具清单新增可选正整数秒 `timeout_s`，参数 Schema 支持 `x-clytie-sensitive` 注解。
- 明确 `ChannelFeatures.media_limits` 的媒体类型、`max_bytes`、`formats` 编码，未列出的类型视为不支持。
- `DeliveryIntent.replaces_emoji` 和 `ChannelFeatures.supports_react_remove` 支持任务状态表情替换。
- 新增统一账本、有限交付重试、取消、事件总线、模拟时钟调度、只读任务查询及加密迁移失败现场；插件版本短锁改为注入时钟异步等待。
- SDK 类型、清单 Schema、参考插件兼容范围与契约测试同步到 0.4。

## 0.3.0（A2）

- 插件宿主、四个参考插件、确定的官方索引、加密升级快照。
- 就地数据迁移采用只读计划与确认；宿主不安装或交换代码。失败保留故障目录、恢复私有状态并保持停用，status 提醒和确认清理可用。
- 修复调用生命周期、平台迁移第四层配置、普通插件回滚配置冻结、ctx 内部引用泄漏，以及取消时跳过恢复的问题。
- **SDK 不兼容变更（用户确认）**：MemoryView.search 删除 scene 参数，场景由宿主绑定。新增仅写入的 MemoryWriter 协议；只有同时具备读写权限时才提供 revise/retract。私密读取遵循 memory.private_scenes。
- SDK.testing 的假上下文和契约同步更新；参考插件声明兼容 `>=0.3,<0.4`。

## SDK 0.2.0

以下为已由用户合并验收的 A1 规格补齐记录。

- 插件改为无参数构造及 `start(ctx)` / `stop()`；通过清单中实例方法的 factory/handler 装配。参考插件和测试同步迁移。
- 补全 CandidateReader、ApiClient、上下文服务协议及离线假实现、契约检查。资产读取始终存在，写入改为按权限出现的 `asset_writer`。
- 通知、请求、本体事件和任务摘要使用明确的数据模型；`task_completed` 改为接收 `TaskSummary`。定时星期使用 ISO 编号；声明包含 handler。
- 新增二进制 HTTP/进程结果，JSON 使用标准 Base64。进程输出受总字节数上限约束，截断标志显式记录。
- 能力取消契约接受取消异常或 `ToolResult(status="cancelled")`；宿主转换、宽限与截止执行仍属于 A3。
- 配置新增账号引用、边界及上下文默认值；模型和每个平台账号的内联配置作为第四层覆盖，并报告错误来源。

不兼容点：旧插件入口、无结构通知/任务摘要、未编码的模型与平台贡献点，以及用原始账号替代 `channels[].account` 引用的配置需要更新。示例已同步。


Issue #36（用户已确认）：窗口聊天原文保持原样，不从撤销记录派生内容或编号；回想窗口只给撤销条数。只有 memory.embedder 指定的向量模型可为本次比对接收撤销原文：优先复用同一 embedder_id 缓存，没有时临时计算，不落库、不缓存、不记录原文。未配置、出错、超时或候选向量失败时，整段只做规范化相同比对，照常写入并推进进度；status 只显示计数提醒，WARN 每小时最多一次，不使用其他模型身份的向量。
