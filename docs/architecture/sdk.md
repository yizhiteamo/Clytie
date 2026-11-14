# 插件 SDK `clytie.sdk`

SDK 是本体和插件之间唯一的契约。插件只能 import `clytie.sdk` 及其子模块。

## 1. 内容

| 子模块 | 内容 |
|---|---|
| `sdk.types` | 数据类型：`EventEnvelope`、`Actor`、`Conversation`、`Message`、`DeliveryIntent`、`Receipt`、`ModelTurn`、`ToolCall`、`ToolResult`、`AssetRef`、`MemoryRecord`、`Visibility` 等 |
| `sdk.plugin` | 插件入口基类、注册接口（声明扩展点的实现） |
| `sdk.context` | 插件运行时上下文：只暴露已授权的接口（日志、配置、私有存储、时钟、记忆视图、资产、事件、服务查找） |
| `sdk.channel` | 平台接口 |
| `sdk.model` | 模型接口 |
| `sdk.capability` | 能力（工具）接口 |
| `sdk.behavior` | 行为扩展点及其返回的指令类型 |
| `sdk.memory` | 记忆扩展接口（检索器、嵌入、导入器、整理器） |
| `sdk.events` | 事件名称和负载类型 |
| `sdk.testing` | 契约测试套件、假实现、模拟时钟（插件作者用来测试自己的插件） |

## 2. 核心接口

- **Channel**：`connect()`、`close()`、`normalize(raw) -> EventEnvelope`、`send(DeliveryIntent) -> Receipt`、`react(target, emoji) -> Receipt`、`features() -> ChannelFeatures`。事件类型分为 `conversation` 和 `content`（发帖、评论等，后续平台使用）。
- **Model**：`generate(messages, tools, budget, cancel) -> ModelTurn`、`features()`、`quota() -> QuotaSnapshot | unknown`。不为查询额度调用模型。
- **Capability**：`prepare(call, ctx) -> Plan`、`execute(plan, ctx, cancel) -> ToolResult`、`idempotency_key(call) -> str`。不自己发消息，只返回结果或资产引用。
- **Behavior**：在扩展点上返回结构化指令（见 `plugins.md`），不直接发消息、不直接写账本。
- **Memory 扩展**：`Retriever.search(query, scope) -> hits`、`Embedder.embed(texts)`、`Importer.read(source) -> records`、`Consolidator.run(window) -> episodes`。可见等级过滤由本体在扩展之外完成。

## 3. 设计约束

- 所有接口都是异步的，都接受取消信号。
- 所有与时间有关的逻辑都从 `ctx.clock` 取时间，测试中替换为模拟时钟。
- 接口参数和返回值都是 SDK 中定义的数据类型，可以序列化（便于以后把插件放到独立进程）。
- 插件拿到的 `ctx` 只包含清单中声明且已授权的接口；没有授权的接口在 `ctx` 上根本不存在。

## 4. 版本策略

- SDK 使用语义化版本，独立于 Clytie 本体版本。插件在清单中声明 `requires.sdk` 范围。
- 次版本只能新增（新类型、新扩展点、新可选字段）；删除或改变含义必须升主版本。
- 废弃的接口至少保留一个次版本，并在加载时给出警告。
- 每个扩展点都有契约测试，放在 `sdk.testing` 中；改动 SDK 必须同时更新契约测试。

## A4.3 备份契约（待实现）

以下为已批准的 A4.3 契约，尚未实现、未经验收；完整依据为 `backup-spec.md`，不代表历史里程碑已提供这些能力。

新增 `backup.snapshot`：prepare → capture → materialize → release，签名、类型和支持状态见 sdk-spec 的 A4.3 节。清单省略 backup 时为 unsupported，旧插件仍正常启用；stateless 与非缓存持久数据矛盾时为缺项，不自动停插件。

此接口的 ticket/handle 是进程内对象，不持久化、不跨 IPC 序列化，是第 3 节通用序列化约束的限定例外。sink 仅接受当前插件命名空间的异步字节流，宿主核验路径、长度、哈希及迟到写入；快照必须能按同一 data_version 直接恢复，不执行插件恢复脚本。
