# ADR 0018：备份工作进程与持久结果

状态：实施中，尚未验收。依据 backup-spec §5、§8、§12；复用 A4.2 的操作跟踪和协作停止边界。

每个操作以 UUID hex 编号登记在 supervisor/backup-operations，JSON schema_version=1。字段包含 id、operation、phase、created_at、updated_at、executor（PID/创建身份）、cancel_requested、arguments、artifacts、result。arguments 只保留服务器核对过的逻辑编号和明确路径，不保存密钥或口令；对外查询不返回 arguments、内部临时路径或异常原文。

工作进程在实例级 OS 操作锁内登记真实执行者。HTTP 返回 accepted 后，请求断线不取消后台任务。父进程通过专用控制管道转达取消；EOF 进入取消模式。复制阶段检查取消，发布阶段完成验证、原子发布和登记；退出本身不是提交证据。

公开结果分别保留 creation、verification、retention、mirror、restore_readiness，任何一轴失败不覆盖另一轴已完成事实。发布前记录临时文件、最终文件、认证包摘要及预期清单；恢复只核验这些登记材料，不再次捕获或执行原请求。复制前中断标失败，后续调度是新触发；发布边界依据文件身份、哈希和认证结果完成登记，否则 recovery_blocked。

备份与记忆管理使用组合停止器并行收尾；同一次停止的取消时间不被重复请求重置。普通伴侣超时不变。备份 120 秒是仍在收尾的提醒阈值，不对正在提交的进程强杀。shutdown 最多登记一次，普通业务停止完成后捕获稳定状态；不以后台副本失联无限拖住退出。

完整恢复仍使用 ADR 0015 的新目标锁与状态机。管理操作记录只指向本次目标计划；不将源操作执行者复制为活跃进程。来源包引用在计划/执行/核验期间保护，释放以明确终态和进程核验为依据。

所有查询和取消经原 Server/Auth；restore apply、源停用和清理的确认/口令由服务端判断。正式验收须包括真实子进程、父进程和树被强杀，以及 HTTP 断线、并行关闭和发布边界，不能由单个日志单元测试替代。

## 创建工作区与重连维护的实施补充

创建工作区 `run/backup-work/<operation_id>` 的明文文件只在捕获或解包完整返回后，按本次明确工作树登记身份、长度与 SHA-256；固定清单独立保存在 `supervisor/backup-temporary/<operation_id>.json`。主包认证、发布与索引登记成功后，逐个核对并删除登记文件，先持久写 deleting，再记 deleted；最后只对登记的空目录调用 rmdir。目录出现未知对象、链接、内容或身份变化即停止，状态为 cleanup_blocked。失败/未验证捕获保留 retained，状态统计单列；崩溃恢复不重新枚举目录取得删除权。已发布包由原 publishing 记录的文件身份、摘要和已认证清单确认后补登记，再核验既有包；不重新捕获、不重复创建主包。

临时工作区清理失败不改变主包已经提交的事实。暂存登记只证明本次已完成写入的文件归属；未完成捕获或解包的未知残留不自动删除，registered_bytes 不声称包含这些未验证材料的精确大小。

## 公开契约与内部证据字段

`schemas/backup-management.schema.json` 使用 JSON Schema 2020-12，根引用 `operation`；调用方按 `$defs` 选择其他管理入口。它描述现有 HTTP `{ok,result}` 中的 result 或请求体，不替代鉴权、CSRF、确认、口令、仓库锁、文件身份和业务准入。常规清理另见 `backup-cleanup.schema.json`。请求中的 `confirm/admin_password` 是 Server 消费的传输字段，不进入 worker arguments；`_source_repository_confirmed`、`_backup_config_confirmed` 是服务端上下文，客户端输入不能成为授权证据。

| 契约 | Schema 定义 | 当前字段及边界 |
|---|---|---|
| 创建/核验 | create_request、verify_request | scope 默认 instance；memory 必须指定现存 companion_id；backup_id 为逻辑编号，operation_id 是可选 UUID hex 防重编号 |
| 恢复 | restore_plan_request、restore_apply_request、restore_transition_request | source/key_path/target 是显式路径；apply 只指定既有 plan_id；切换指定 restore_id，不自动启动任何账号 |
| 外部源 | source_repository、source_repository_binding | 请求 root/backup_id 与可选 repository_id；首轮缺确认或预期 ID 返回 428 和核实后的 root/source 物理身份；绑定不授权删除 |
| 异步结果 | operation、operation_result、unreadable_operation | operation 含 id/operation/phase/created_at/updated_at/cancel_requested/result，可选过滤后的 reason；状态列表中损坏日志单独返回 id/recovery_blocked/journal_unreadable |
| 恢复预览 | preview | kind/plan_id/state/created_at/expires_at/operation_id；状态 ready/consumed/expired/cancelled/stale；查看不续期 |
| 管理状态 | status、key_status、space_estimates | key/space_estimates/storage_categories/audit/mirror/old_instances/protection/primary/temporary/operations/still_finishing，以及恢复和配置轴；key 仅给指纹、可用/保管声明/副本核验状态，不含密钥；不可用统计显式 unavailable，不假装零份/零引用 |
| 备份列表 | list_response、backup_record、missing_component | 当前鉴权本机列表保留受管路径和文件身份；missing 表按 component/reason 与可选 count 显示 partial 缺项，旧索引允许缺此表，missing_count 继续保留 |
| 失败封装 | error_response | ok=false，error.code 和 message；可选 result 携带确认说明、核实后的绑定或原 operation_id，不含异常原文 |

当前 create/verify/restore HTTP 实现会忽略未消费的额外顶层字段；Schema 如实保留这种兼容性，不用静态 Schema 声称服务端已拒绝它们。source_repository 和 regular cleanup 的额外字段实际拒绝。逻辑编号格式不证明对象存在；请求确认和管理口令仍由服务器根据真实操作判断，不能由 Schema 校验通过推断授权成功。

内部日志仍为 schema_version=1；除前文初始字段外，`cancel_at` 为首次取消时间或 null；`reason` 为持久阶段诊断码；`delivery` 为 waiting/received；`source_sha256` 为包摘要；`envelope` 为已核验容器证据。`executor` 保存 OS 核实的 PID/创建身份，不能用文件存在、PID 数值或进程退出推断提交成功。`artifacts` 是按 operation 固定的内部证据：backup_package、backup_verify、workspace、restore_plan_expected、restore_plan、restore_target、cleanup_plan；各自可包含内部路径、原索引 record、摘要或目标身份，只用于核验，均不投影至 operation 查询。以上字段不是客户端可提交的参数。

公开 result 的三个编号字段是 backup_id/restore_id/plan_id；数量字段为 missing_count/bytes；结果轴为 creation/verification/retention/mirror/restore_readiness。retention_reason/mirror_reason 分别记录主备份提交后维护失败的原因，不覆盖 creation 和 verification；失败主操作的阶段原因由顶层 reason 表达，不伪造一个已提交 result。Schema 的 public_reason enum 保存本版本 `backup.errors.PUBLIC_REASONS` 的完整固定集合；新增可公开诊断必须同时更新实现、Schema 和行为测试，不能接受任意异常字符串。

## 原因到结果的映射

下表按实际执行边界覆盖原因映射；源仓库生命周期各码细目见 ADR 0020，常规逐文件结果见 ADR 0021。错误码不依赖解析 message 或异常原文。

| 来源/条件 | 公开结果/HTTP | 原因选择 |
|---|---|---|
| Server 鉴权、Origin/Host、CSRF、确认、口令 | 沿用 A4.2 APIError 的 400/401/403/428；未进入业务 | 原 Server/Auth 固定码；confirmation_required 带 description |
| 请求字段或逻辑编号不合法 | 400 invalid_request | Management 的显式 APIError；传输层 JSON/类型错误同码 |
| 源绑定缺确认或缺预期 repository_id | 428 source_repository_confirmation_required | result.source_repository 是已核实绑定；尚未登记引用或读包内容 |
| 源绑定校验中的 RepositoryError | 409 exc.code | 身份/归属冲突、不可用、不支持、索引损坏、缺包等；OSError 固定 source_repository_unavailable |
| 异步准入中的 WorkerError/PreviewError/RepositoryError/RestoreError | 409 exc.code | backup_busy、supervisor_stopping、operation_mismatch、arguments_invalid 及相应源/计划/恢复码 |
| 异步准入其余 OSError/ValueError/KeyError/TypeError | 409 backup_request_unavailable | 不回传异常字符串 |
| 预览查询/取消 | plan_not_found 为 404，其他 PreviewError 为 409 | consumed/in_use 等冲突可带原 operation_id；不新建替代执行编号 |
| 配置计划/写入 | 409；confirmation_required 为 428 | OperationBusy→operation_in_progress；OSError→config_unavailable；已知 plan_stale/invalid_plan_id/config_invalid/config_not_writable/config_invalid_after_write/config_changed_during_validation 原码，否则 config_not_writable |
| 密钥管理 | 409 | KeyErrorCode 的 key_missing/key_invalid/key_changed/key_export_path_invalid/key_export_exists/key_export_verification_failed/key_mismatch；OSError→key_file_unavailable |
| 源停用/切换确认 | 409 | maintenance_required 或 RestoreError.code；restore_id 类型无效为 400 |
| 常规清理计划查询失败 | 409 cleanup_plan_unavailable | 仅内部查询错误，不从错误原文提取路径 |
| worker 协作取消/未交付 | phase=cancelled（核验模式未交付为 recovery_blocked） | cancelled/delivery_cancelled；已提交事实不回退 |
| worker 普通异常 | publishing 阶段为 recovery_blocked，其余 failed | 有 `.code` 且在 PUBLIC_REASONS 中时原码（如 capture_busy/snapshot_unsupported/space_insufficient/restore_incompatible）；未知类型、未知码或自由文本→worker_failed |
| worker 核验异常 | 同上 | 已知 `.code` 保留；否则 verification_failed；不重新捕获或重放原请求 |
| worker 未成功创建 | phase=failed | worker_not_created |
| 主包提交后轮转失败 | creation 保留，retention=failed | 已知 `.code`→retention_reason；否则 retention_failed；保护容量不足正常返回 retention_protected |
| 主包提交后副本失败 | creation 保留，mirror=failed | 已知 `.code`→mirror_reason；否则 mirror_failed；协作取消为 mirror=cancelled |
| 任意已存顶层 reason 的公开投影 | 白名单码或 worker_failed | 历史/损坏数据中的自由文本不能穿过 public() |
| 未捕获的 HTTP 异常 | 500 operation_failed | 固定消息，不输出 traceback/异常原文 |

`missing_component.reason` 是已捕获 partial 的组件缺项事实，与操作抛异常的 public_reason 分开：例如 asset_source_unverified、plugin_version_unverified、snapshot_unsupported、stateless_has_data、opaque_plugin_file、audit_source_unavailable、snapshot_inconsistent。它不把 partial 改为完整成功，也不授权启动插件或模型验证。

测试文件 `tests/test_backup_management_schema.py` 通过真实 BackupJournal.public、Management 的 list/status/preview 投影、源仓库 binding 和本机 HTTP 失败响应核对 Schema，并检查非法编号、缺必要字段、额外源授权字段、未识别公开原因与内部 arguments 泄漏均无法通过对应契约。测试不能替代端到端功能验收；实际运行结果与未覆盖平台另行记录。

`space_estimates` 正常为按操作编号列出的数值状态数组，每项含 operation_id、state（estimated/monitoring/blocked）、estimated=true、guaranteed=false、source_bytes、package_bytes、temporary_budget_bytes、wal_growth_budget_bytes、reserve_bytes、occupied、peak_temporary_bytes、peak_wal_growth_bytes、volumes 和 failure。occupied 仅含常规仓库/分类占用及旧实例已知字节和未知数量；volumes 仅含 device/free_bytes，不含 probe 路径。blocked 的 failure 固定 code=space_insufficient，dimension 为 preflight_free/reserve_free/wal_growth/temporary_budget；其他状态 failure=null。证据不可读取或损坏时整个字段为 `{state: unavailable, reason: space_evidence_unverified}`，不能以空数组或零容量代替未知。内部 work/wal/path/probe 不公开；估算不承诺压缩率、不授予读取资产或删除旧包权限。Schema 测试使用真实 SpaceGuard 持久结果，覆盖正常、预留空间受阻和损坏证据，检查内部路径注入与无效状态被拒绝。
