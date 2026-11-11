# ADR 0013：在线备份的一致性组与固定读视图

状态：A4.3 已批准规格的内部实现；运行集成与完整验收仍待完成。

依据：backup-spec §4、§9、§13。本 ADR 不增设 SDK 或放宽完成条件。

## 数据组与锁顺序

每个伴侣的 memory（含 recollect_progress）和 ledger 共用提交门闩；回想写入和进度仍是 memory 的同一事务。数据持有进程处理在线捕获，离线 worker 只能在原运行/迁移锁已取得后捕获。门闩路径由装配层注入，不能让调用方选择另一组的门闩。

锁顺序为既有生命周期/迁移锁、按 canonical path 排序的捕获门闩、SQLite 事务。业务逻辑若同时提交多个数据库，必须以同一门闩覆盖整个逻辑操作，不逐库释放。单库事务通过可注入的协调器接入；没有接入的运行时不能声称支持在线完整捕获。

插件状态与清单/配置/版本、管理提交证据与数据、资产引用与文件内容分别需由装配层固定稳定版本并提供关联证明；SQLite 捕获器不为这些组自动声明一致。迁移或受阻恢复只能返回缺项，不能通过恢复入口获取锁。

## 捕获与物化

所有源连接在门闩外以 mode=ro、timeout=0 打开，检查 WAL；在门闩内 BEGIN 并实际读取各库 sqlite_schema 建立读视图。只 BEGIN 不足以固定 SQLite 视图。组内所有读视图建立后立即释放门闩。默认总等待 2 秒、持有 250 毫秒，均用注入 Clock；超时释放全部已建立视图并报告 capture_busy。

保持连接和读事务直到分批 sqlite backup 完成；复制不能重新打开源或先提交源事务。物化在后台线程执行，每批检查取消和资源限制回调，不在事件循环复制大数据库。rollback journal 源返回 snapshot_unsupported；运行账本需在启动阶段设置 WAL，不能在捕获时修改活动库。

账本仅在持有原运行锁、确认 schema 无需迁移的可写启动路径设置 WAL，已有 rollback journal 库同样转换；只读连接不转换，旧 schema 仍先交原迁移路径。默认无协调器保持原构造调用兼容；上线装配必须对组内所有提交注入相同门闩。同步事务只在原同步边界有界等待 OS 锁，统计最大等待，不添加异步任务或改变生命周期。250 毫秒是检测并放弃的工程上限，不宣称不可抢占 SQLite 单步或 OS I/O 的硬实时保证；单步超时后立即释放，不产出完整结果。

OS 文件锁不依赖 PID 标记判活，进程退出自动释放；锁文件不删除。失败、取消和超时关闭视图。每次捕获失败不重放业务操作、不杀插件、不延长界限。后台线程取消须等待其协作退出再释放连接，不能留下未知执行者。

生产装配在 LedgerRuntime、PluginHost.memory_store、MemoryAPI.open 注入同一伴侣路径的门闩。同线程同步嵌套共享重入状态；commit 上下文禁止跨 await。runtime_capture 只使用已有在线持有者，并在非阻塞管理操作锁内固定读视图；活跃操作、受阻记录或缺少持有者返回 unavailable，不启动或恢复任何组件。物化后的结果仅声明 memory_ledger_progress 的 group_complete，明确列出仍需 configuration/plugins/assets/management 组证明；这不是 instance complete，也不替代恢复端的进度边界/裁剪证据核验。

## 内部结果与错误

捕获对象含 epoch、按逻辑名索引的持续只读连接、捕获时刻；物化结果是逻辑名到新数据库路径的映射。epoch 是本次关联标签，不替代数据库/资产引用校验。输出目录由本次独占工作区提供，不覆盖现存文件。

`capture_busy` 表示门闩等待或持有超限；`snapshot_unsupported` 表示非 WAL 源；`snapshot_inconsistent` 表示读视图或物化校验失败。API 映射由管理入口实现，捕获器不返回任意 SQL 或文件错误原文。完整包仍需上层验证 companion UID、回想边界、裁剪证据与其他组关联；不满足时不得标 complete。

## 插件提供者与受限输出

ProviderCapture 按 prepare（最多 10 秒）、capture（最多 250 毫秒）、materialize、release（最多 2 秒）分阶段调度，全部时限来自注入时钟；materialize 的额外预算由上层任务决定。capture 对 epoch/data_version/返回类型校验，并在不可抢占的调用返回后再检查实际耗时。共同门闩只包 capture；capture 失败也必须先退出门闩再调用 release，不在捕获窗口等待释放。

超时和取消立即使 sink 失效并取消子 token，同时保留未退出任务的引用。task.cancel 不代表退出；迟到 prepare 产生的 ticket 仍安排一次释放。release 幂等，失败和迟到任务分别保留 errors 与 resource_pending；不能因为输出存在就算成功。提供者自行取消、错误返回和异常只使该插件 partial。上层仍须保留执行器直到 resource_pending 消失，不能因请求返回丢失跟踪。

NamespaceSink 仅写本次专属插件目录，拒绝链接/重解析点、穿越、绝对路径、Windows 特殊路径、重复成员、跨平台大小写与父目录冲突，以及显式凭据路径。单 chunk 默认不超过 1 MiB，后台有界写入并计量 SHA-256；只有完整写入才登记 entry。任意写入失败永久使 sink 无效，即使插件吞掉异常也不能宣称完整。已开始的后台写须收尾后关闭句柄；失效后新写入与迟到 chunk 一律拒绝。该约束是宿主协议边界，不宣称剥夺插件进程的任意文件系统能力；完整持久布局和直接恢复格式仍由 provider 契约、宿主关联核验与离线测试保证。

## 配置写回与捕获竞争

API 的插件配置写回与配置捕获使用同一 config-commit.lock。写回复用 CaptureCoordinator 的既有有界提交等待和注入时钟，短捕获释放后继续保存；超时映射 config_not_writable，由已生效的插件操作路径报告 applied_not_saved，不把内部锁竞争报为请求格式错误。回滚写回同样协调，读写失败不吞掉。真实跨进程回归核对短占用成功与持续占用不改原配置；本地 HTTP 故障注入记录与 CI 观察分开保存。
