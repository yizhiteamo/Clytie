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

## 准备核验与取消回归的追加时点

ba5ec3bbd9d5812871235a25936eff818c073358 的两平台准备密文已下载，认证解密、原件哈希与本机私有扫描完成；通用数字误报按本轮有限例外记录，不称双扫描全部通过。平台实际接受十四天保留期，本机另存交接清单。首次 Linux 核验因本机工具换行字节与固定 Git 对象不同而拒绝，原失败保留；核对固定对象及仅换行差异后，对同一密文重新核验，没有重跑准备或改写原件。准备仅提供合法几何及环境证据，不是正式机制或性能通过。

同候选常规 Linux CI 为 3048 passed、7 skipped；Windows 为 3054 passed、1 failed。Windows 失败在 test_cancel_keeps_cancelled_manifest_and_waits_for_worker_exit 的提前结束分支，旧断言没有记录 capture 信号及任务实际结果，无法从原日志确定根因。原日志本机保留，通用扫描未过、私有扫描通过，不发布原文。父候选成功不替代该次失败，也不归为环境波动。

1c50c19 只调整测试同步和安全诊断：capture 入口显式等测试交接；取消后先释放测试门闩，再等待实际关闭桥接，仍断言 cancelled 原件及真实工作进程退出，不延长窗口或退出期限。模拟提前结束、异常和已进入捕获仍提前结束都保持失败；诊断只含白名单代码与状态，不输出任意异常消息。本机相应工具及真实临时子进程回归 18 passed。该证据证明同步和诊断行为，不冒充原远端故障根因复现。

容量工具在前后文件清单一致时完成 969 项测量工具回归；随后总量追加防线及真实加解密定向回归 45 passed。文件格式六仅扩大存储事件流的工程容量，历史格式、其他流、总认证包界限及原字节要求保留；超限使整场无效，不丢弃样本。正式大规模内存峰值及固定窗口仍待实际验证，tiny 或外推不能证明足够。

以上均为工程验证。正式机制、可比旧基线和三臂矩阵尚未开始；不增加运行预算，正式审查仍已用一轮。
