# ADR 0021：常规备份显式选择清理

状态：实施中，尚未验收。依据用户批准的 backup-spec §6.4、§13 Issue #68 补充验收；不改变自动轮转策略、旧实例清理或外部源绑定权限。

## 固定选择与后果

`CleanupService.plan("regular", backup_ids=[...])` 仅接受非空、不重复的逻辑编号列表。编号限定 ASCII 字母、数字、下划线和连字符，最多 128 字符；不接受路径、通配符或自动推导的删除范围。只从当前管理 home 的 `BackupStorage` 索引取得文件，恢复源绑定不参与授权。

计划保存于 `supervisor/backup-cleanup/<plan_id>.json`，字段包括 `id/category/created_at/state/backup_ids/repository_id/eligible_ids/items/estimated_freed_bytes/groups_after`。每项公开后果包括 `backup_id/scope/group/completed_at/size/completeness/manual/retention_layers/lost_restore_point/eligible/reason/state`；内部 `record` 固定索引记录、文件身份、大小及哈希，管理 API 不公开它。未知编号只显示编号、不可执行状态及原因，不猜类型或位置。`groups_after` 按 scope/group 列出 `valid_before/valid_after`，partial 和未核验文件不计入有效份数。

手动包以及日/周/月层可以显式选择；保留层只展示后果，不成为自动删除授权。日/周/月分桶与自动策略共用 `retention.calendar_bucket`，按实例顶层 timezone 展示。每组最近三份经物理身份、大小、哈希和索引核验的 complete 包受最低保护，不足三份全部保护；manual complete 可以构成有效恢复点，partial 和其他组不能替代。源权威登记中的所有引用（含失联/未知持有者）均阻止删除。受保护项不进入 `eligible_ids`。

## 锁、回执与恢复

锁顺序为清理计划锁 → 当前仓库索引锁 → 源权威引用锁，与轮转及源引用协议兼容。每次删除重新核验仓库 ID、完整索引记录、物理身份/哈希、当前最低保护集合和权威引用，在持有索引与引用锁期间执行单个明确路径的 unlink。不增补、不替换候选，不递归、不改 ACL，不要求先生成新包。

逐文件先持久 `deleting` 意图及源索引删除回执，再 unlink，随后提交源 `deleted` 回执并移除索引记录，最后保存计划的 `deleted`。回执含 backup_id/path/state/operation_id/at。不存在跨文件原子提交承诺；进程中断留下 `deleting` 时，恢复标记该项 `failed/cleanup_result_unknown` 和整体 `recovery_blocked`，不根据文件消失推断成功，不重删，也不推进剩余项。

计划整体状态为 `ready/applying/completed/partial/blocked/stopped/recovery_blocked`。每项结果为 `deleted/blocked/failed/not_executed`，`deleting` 只用于进行中的持久意图。受保护、身份变化、缺失、失败及未执行分别保留；存在未成功项不报告全部成功。取消保存 `stopped/cancelled`，保留先前删除事实及未执行项。终态 apply/recover 返回已存结果；同 plan_id 的生成重试只有相同 category 和 backup_ids 可返回原计划，否则冲突，不能覆盖历史后果或扩大授权。

worker 清理回执验证使用不可变选择指纹（计划 ID、类别、创建时间、请求编号集合、仓库 ID、合格集合及每项原始记录/资格），动态进度更新不会使正常恢复误报篡改。旧实例计划继续使用既有独立路径，不把常规编号变成旧目录删除授权。

错误包括 `cleanup_selection_invalid/cleanup_category_invalid/cleanup_plan_conflict`；逐项阻止原因包括 `backup_not_owned/backup_evidence_mismatch/minimum_restore_points/backup_referenced/source_repository_identity_mismatch`；未知删除结果为 `cleanup_result_unknown`，其他执行失败只公开 `cleanup_failed`，不泄漏原始异常或路径。

## 验证范围

`tests/test_backup_explicit_cleanup.py` 使用手写虚构包数据，覆盖手动/长期层、最低保护、组隔离、partial/未核验、计划后新增跨 home 引用、文件替换、逐项失败与重复请求、unlink 后中断、外部恢复绑定不授权删除、worker 不可变指纹。真实子进程同时竞争显式清理、源引用登记和自动轮转，核对最低三份及已成功登记的引用不会被穿透。旧实例清理、轮转和 API 的组合回归另行提供实际命令及平台结果；未执行的平台和未完成的全链路验收不据此声称通过。
