# ADR 0020：源仓库权威保护与跨管理目录回执

状态：实施中，尚未验收。依据用户批准的 backup-spec §8.6、§8.7、§13.16a/b（Issue #67）。不改变 Issue #66 的 24 小时期限、普通记忆导入或 apply 的口令保护；ADR 0019 中仅本地目录的历史实现说明保留。

## 身份与最小元数据

物理仓库 root 内保存 `.clytie-repository.json`（schema_version=1、repository_id、root、root_identity={device,inode}、owner_instance_id、migration_complete）、`.clytie-index.json`（schema_version=1、records、deletions）和 `.clytie-references.json`（schema_version=1、references、previews）。主仓库为当前 home 的 backups/regular；镜像仓库分别初始化自己的 repository_id。元数据只由已授权的自身仓库初始化/副本确认创建，外部绑定绝不创建仓库。

跨 home 请求 `source_repository={root,repository_id,backup_id}`。确认前只读用户明确选择的 root 的 marker、index 及 OS 文件身份。缺确认或缺预期 repository_id 返回 428 和核实后的绑定；错误预期 ID 直接冲突，不自动接受新 ID。确认说明：将在此备份仓库登记临时使用保护，防止恢复期间被自动清理；不会修改源实例的业务数据。密钥路径由用户另行显式提供，保护绑定不授权读取源实例的密钥。

运行时拒绝网络路径、链接/重解析点、多硬链接文件、目录/文件身份变化、路径变动及未知文件系统。Windows 接受 OS 确认的 fixed/removable 本地卷；Linux 从当前挂载表核对本地文件系统类型，其他平台或无法证明时拒绝。每次登记使用实际 OS 排他锁；无法取得锁、权限不足或元数据损坏不降级成本地保护。独立副本不能仅凭未发现标记得到豁免：没有明确物理归属证据的外部文件拒绝，当前没有自动转出入口。

管理 home 的 `supervisor/backup-holder.json` 保存随机 instance_id 和 home 物理身份。引用的内部主键为 holder_instance_id 与调用方稳定操作编号的组合；记录 owner_id、holder_operation_id、holder_instance_id、source_repository_id、backup_id、source_identity、source、kind、executor（PID 和 OS 创建时间）、created_at、recovery_required。不同 home 即使操作编号相同也不能互相释放。PID 仅供状态诊断，退出和复用均不授权删除。

## 两个持久位置与崩溃点

本地 `supervisor/backup-source-bindings.json` schema_version=1，包含 sources、plans、owners、pending_release。sources 保存核实后的 root/包身份及 holder；plans 保存真实 target、fingerprint、source_repository、kind 和阶段；owners 在源登记前保存稳定编号意图。源端仅保存删除保护必需的生命周期和哈希化 target/fingerprint，不保存目标配置、正文、密钥或凭据。本地业务恢复计划与操作日志仍属于管理 home。

1. 先保存本地登记意图，源锁内核实索引和物理文件，再提交 generation 引用；只有提交后才读取/解密。响应丢失按同一持有者和操作编号查询/重试登记，不重建新编号。源先删除则 source_missing。
2. 已校验的业务计划发布后，本地保存 publishing 回执，源事务中登记 ready、created_at/expires_at、preview 引用并移除 generation 引用，最后本地改为 published。源提交前崩溃保留 generation；提交后本地回执中断可读取源端状态补齐，不自动重做恢复。
3. apply 先保存 accepting 意图，源锁内重新核对 ready、期限和调用方指纹校验，登记 execution、唯一 operation_id 和 consumed，再去掉 preview 引用；之后才保存本地 accepted 并启动写入者。源提交后本地记录丢失，可通过绑定查询同一 operation_id；不能接受替代编号。
4. 到期由源事务决定，仅 ready 预览可释放；终态不复活。reader、generation、execution、copy 与恢复依赖不适用预览期限。管理 home 消失、源重启、失联执行者均保留引用。
5. 完整恢复先持久 ready_for_switch 和 source_release=pending，再释放本引用；成功后写 released。释放错误保留 pending 和源引用，不能将数据恢复改失败或重新执行。源与本地文件之间不宣称双文件原子提交。取消、失效或到期不影响独立读取/执行引用。

锁顺序：外层请求/计划准入与源索引锁先于源引用锁。绑定文件只用于短事务，退出后再进入源锁，源锁内不反向取得绑定文件锁。源读取期间使用持久 reader 引用，不持长时源登记锁。删除在同一源引用锁内重新核对索引、文件身份及正常保留规则并逐文件执行。旧目录清理在无引用时持久登记 repository_retirement，之后新读者/执行引用拒绝；只有证明清理已停止的同一操作才能解除该准入阻止。

## 旧记录迁移

自身主仓库初始化先设 migration_complete=false。保留旧 supervisor/backup-index.json 和 backup-previews.json，不删除原证据；在旧本地目录锁内将索引及匹配的引用/生命周期复制到源权威登记，然后写本地 binding/owner/plan 路由，最后提交 migration_complete=true。中断时保留两侧证据并按同一主键核对；不匹配、缺记录、无法证明或损坏时拒绝受影响仓库删除。不会自动读取其他 home 或包内历史路径来猜旧引用归属。

## 错误映射与公开投影

- `source_repository_confirmation_required`：428，附已验证的 source_repository 绑定说明；不取得保护、不读包内容。
- `source_repository_identity_mismatch`、`source_repository_ownership_mismatch`、`holder_identity_mismatch`、`reference_mismatch`：409 身份/归属冲突。
- `source_repository_unavailable`、`source_repository_unsupported`、`source_repository_index_invalid`、`source_repository_retiring`、`reference_busy`、`reference_recovery_blocked`、`source_missing`：409 源不可用或引用受阻；权限错误沿用管理 API 结构化受阻响应，不能返回 ready。
- `plan_not_found`：404；`plan_expired/cancelled/stale/in_use`：409；consumed 附原 operation_id。鉴权、CSRF 和确认沿用既有规则，不新增口令豁免。

公开日志/通知只给逻辑编号和原因；源路径/物理身份仅供已鉴权本机管理接口。源端可独立列出持有者、用途、创建时间、执行者 active/exited/unknown 与 recovery_unverified 原因；未知状态不能作为零引用。

测试位置：tests/test_backup_repository.py 的两个真实进程 apply/到期/实际 unlink 竞争、生成读者强杀及源重启、双 home 同操作编号隔离、响应丢失/发布/执行交接回执、缺登记、旧引用迁移、硬链接和 retirement；tests/test_backup_previews.py 保持同 home 生命周期与竞争回归。完整维护 API 与实际源轮转组合由单独集成测试核验，不以这些局部测试代替 §13.16b 全部验收。Windows 与 Linux 结果分别提供；未跑的平台不推断通过。
