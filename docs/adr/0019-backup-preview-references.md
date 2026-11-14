# ADR 0019：恢复预览生命周期与源引用

状态：实施中，尚未验收。依据用户批准的 backup-spec §8.5、§8.6、§13.16a（Issue #66）；不改变普通记忆导入规则。

`supervisor/backup-previews.json` 使用 schema_version=1。顶层为 previews（plan_id 索引）与 references（owner_id 索引）；同目录稳定 `.lock` 文件上的 OS 锁协调所有进程。原子替换 JSON 同时提交预览状态和引用转移。调用者的准入/索引锁先于引用锁；引用锁内不能再次调用目录方法。API 使用工作线程调用同步方法，锁等待最多 1001 次、每次 OS Event 等待 10ms，并用注入时钟检查 10 秒截止；业务期限完全来自注入 Clock。

预览记录 kind、plan_id、source、target、fingerprint、state、created_at、expires_at、operation_id、reference_id、history。kind 仅 instance/memory；memory 的服务端适配器仅登记备份源计划。时间采用 UTC ISO 8601，24 小时从完成验证及计划文件持久发布后的目录提交开始。history 每次原子追加状态、时间和操作编号，不改变过去生成操作的成功事实。ready 可转 expired/cancelled/stale/consumed；终态不复活。查询、重复发布、重启不改截止时间。

引用记录 source（规范化绝对路径，仅实例私有状态）、owner_id、kind、executor（PID 和 OS 创建时间）、created_at、recovery_required。生成、预览、读取、复制、执行引用独立持有。删除必须在 `deletion(source)` 持锁上下文内重新核对文件身份与保留规则并逐个删除；只取得 holders 快照不能授权删除。无法解析目录即拒绝删除。读者被强杀后的持久引用保留，不能因 PID 退出、预览过期或操作终态自动回收；需要核验读者结束且没有恢复依赖。

生成在首次读取前持有 generation 引用。先登记计划发布意图和内容指纹，再原子写计划，最后在同一目录事务中增加 preview 引用并删除 generation 引用。之后才提交生成操作成功。若在计划写入与目录提交之间崩溃，generation 引用继续保护；恢复器只检查已登记的计划和工作材料，核对完整指纹后补齐目录发布，不重做原请求。已发布目录重复发布保持原状态和时间。

apply 在引用锁内核对 ready、时间、源/密钥/目标指纹，新增 execution 引用并提交 consumed 和唯一 operation_id。完整实例恢复在交付执行者前完成这一步。记忆恢复复用已有管理工作进程：父进程固定请求编号并跟踪，首请求等待业务结果，超时只表示请求仍待查询；子进程在持有记忆管理锁并核对数据库、配置与源后原子接受执行。父请求登记不等于恢复执行已被接受，只有 consumed 表示已经完成交接，不能把此前的请求处理中改称恢复已接受。若在目录与操作日志之间崩溃，只可重建同一个已接受编号，不能接受新编号。已有操作记录直接返回原结果。恢复失败且可能有依赖时保留引用；完整目标通过检查持久提交 ready_for_switch 后才释放本执行引用。预览到期和取消不能中断已接受执行。

错误映射：未知或 kind 不符为 `plan_not_found`（404）；`plan_expired`、`plan_cancelled`、`plan_stale`、`plan_in_use`、`reference_busy`、`reference_recovery_blocked` 为 409。`plan_in_use` 附 operation_id。原源/密钥/目标变化错误仍沿用对应恢复冲突码，同时持久标记 stale。取消复用正常鉴权、Cookie CSRF 和确认，不加管理口令；原 apply 保护不变。元数据对外投影只提供逻辑编号、状态、期限、操作编号，不返回 source、target 指纹或进程信息。

验收证据由 `tests/test_backup_previews.py` 的 MockClock 边界、真实双进程 apply/到期/删除竞争和读者强杀重建，以及既有恢复工作进程发布/强杀测试提供；Windows/Linux 结果分别记录，不将当前平台结果推断为另一平台通过。

## 跨 home 边界的历史诊断（Issue #67）

最初目录仅协调同一管理 home。独立 maintenance home 引用另一仍运行实例所管理的源包时，两份目录不互见，不能由上述机制推断已阻止源轮转。两个虚构目录已复现该边界，未实际删除文件。

用户已确认源仓库权威登记、显式身份绑定及保护元数据写入授权，现行方案见 backup-spec §8.7 和 ADR 0020；维护 home 的目录不再是跨 home 删除资格的权威。不自动扫描旧实例、不擅自写未绑定目录，也不将额外副本或提前停源作为默认回退。以上历史失败证据保留，后续组合验收不能以同 home 局部回归替代。
