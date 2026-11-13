# Issue #72：机制证据的覆盖边界

本表把已批准要求对应到生产调用点、测量观察及真实离线回归，不新增规则或测量场次。它是待执行核验的清单，不是验收通过声明。正式原件、同一候选的测试/CI、人工源码核对和本机解密扫描必须分别保存；历史候选的通过不代替新候选。正式审查仍已用一轮。

## 生产调用点与证据

| 要求 / 实际入口 | 原件观察 | 组合验证入口（tests/） | 必须保留的限制 |
|---|---|---|---|
| source._copy/_entry、container.pack/unpack、资产捕获、插件 Sink、副本复制 | measurement_io_observation 的实际宿主文件调用；measurement_batch_observation 关联预留与真实 client 请求 | test_backup_io_files.py；test_backup_assets_integration.py | 预留不等于失败调用的实际字节；插件绕过宿主的内部 I/O 不可见 |
| capture._SnapshotWorker._copy_checked、sqlite_io.copy_database | 实际 SQLite progress、固定 view/数据库几何、读写预留关联 | test_backup_io_snapshot.py；test_measurement_pressure_observation.py | 复制逻辑页不等于物理读页；不能推断原生校验内部字节 |
| native_sqlite.native_checks/validation_stage；创建、复制后及恢复校验 | 原调用开始、驱动返回、连接关闭与实际 stage/收尾入口 | test_backup_native_validation.py；test_measurement_native_context.py | finished 观察可能晚于释放，要求的是实际执行与连接关闭先于释放；内部读字节保持未知 |
| 管理 prepare → file executor → snapshot → StorageOwner → 必要恢复 | 实际控制请求、同一 capability；正式备份 create 不自动涵盖全部管理 apply 分支 | test_management_operation_slot.py；test_backup_memory_io_channel.py | 常驻线程/伴侣不必退出才能结束一次借用；不以 context 离开代替真实工作结束 |
| IoControl/Responsibilities 的登记、授予、释放、重新核验 | 相同 instance/controller/session/operation/generation/execution 的显式关联 | test_backup_io_control.py；test_backup_io_admission_control.py；test_backup_io_restart_wiring.py | 业务终态、EOF、PID 或心跳不足以证明释放；失败登记不能升级为成功 |
| 真实 daemon 重启与遗留执行者 | 单场原件不能证明跨重启互斥 | test_backup_io_restart_http.py::test_real_daemon_restart_blocks_orphan_io_but_other_companion_chats；test_backup_io_responsibility.py | 真进程组合与注入的 PID 复用分开列；不能以原件 metadata 替代跨进程锁及退出证据 |
| StorageOwner 压力 → 控制器 → shared budget | 实际 owner 样本、收取、refill/charge 前后状态及已有产品时钟 | test_backup_io_budget.py；test_measurement_pressure_observation.py；test_measurement_pressure_validation.py | 未绑定或不完整成员集合不是零 owner；观察时间不能替代产品 monotonic |
| ViewLimits/WalGrowth | 已记录的代次、保留状态及累计增长；视图/线程实际结束 | test_backup_view_limits.py；test_backup_io_snapshot.py；test_measurement_views.py | 增长公式不证明全部采样存在，也不单独证明触限停止；小限额故障注入不是实际大 WAL 压力测试 |
| 取消源、受理、底层结束、引用及槽位释放 | 各 acquire/start 与对应 actual close/exit 必须连接，不只枚举已有 release | test_measurement_cancel_trial.py；test_management_operation_slot.py；test_backup_worker_cleanup.py | 已提交事实保持；不可抢占调用尚未返回时不能报告资源已释放 |

上述测试名是核验入口，不是本次执行结果。每次正式机制核验需记录实际完整 head、命令、结果、平台、跳过原因及来源文件哈希；没有运行或没有覆盖的分支继续标明未验证。常规 CI 的冒烟是全量子集，不相加。

## 原件判定与外层核验

- 聚合器的 verified 仅表示明确命名的已观察谓词；顶层 not_evaluated 不得改写成机制通过。缺失是 unverified，真实矛盾是 failed，后续成功不能抹去失败。
- 通过完整冻结请求计划核对所有到达、排队、完成、原生阶段及子进程退出。负载必须覆盖完整窗口；不延长已冻结窗口、不剔除慢样本、不以 tiny 工具用例替代正式规模。
- borrower 串行、跨控制器重启、队列边界及未在正式 normal/cancel 场景触发的故障语义，使用上述同候选真实组合测试和源码核对；不为消除聚合器的未知而扩大正式负载。
- 原件覆盖的调用链逐项核对；初始化、必要收尾与普通业务分别按实际入口判断，不能用空 stage 或关闭收费字段作通用豁免。
- 真实机制运行还须固定产品 C、工具 T、计划 P、请求 Q、准入 B 及实际 CI 身份，核对原字节、运行预算、artifact 期限及本机认证解密/哈希/私有扫描。单独哈希、模拟扫描或已上传密文都不等于完成核验。
- 保留原生内部字节未知、插件内部旁路未观察、文件系统缓存和不可抢占 I/O 的限制。完整机制通过前不进入矩阵；全部功能、性能和证据齐全前不进入第 2 轮正式审查。
