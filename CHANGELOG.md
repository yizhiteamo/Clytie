# 变更记录

## SDK 0.x 次版本变更（A3.1 规格，尚未实现或发布）

- **SDK 不兼容变更（Issue #17，用户确认）**：`schedule.every` 仅接受周、日、时、分、秒的固定时长，拒绝月、年（包括混合和小数形式）以及总时长为零的值。一天按 24 小时、一周按 7 天在 UTC 上换算。`every` 与 `days` 同时出现时一律拒绝（包括空列表）；`at` 省略 `days` 或给出空列表都表示每天。清单和 SDK 校验保持一致，依据 kernel-spec §9。
- `EventEnvelope` 新增 `dedupable: bool`，默认 `true`；不可防重事件只能观察，不触发任务或回复。
- 工具清单新增可选正整数秒 `timeout_s`，参数 Schema 支持 `x-clytie-sensitive` 注解。
- 明确 `ChannelFeatures.media_limits` 的媒体类型、`max_bytes`、`formats` 编码，未列出的类型视为不支持。
- 本次仅记录 `kernel-spec.md` 第 16 节的 SDK 0.x 次版本变更；SDK 类型、清单 Schema 和契约测试在 A3.1 实现时同步，不在本规格 PR 中变更运行代码。

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
