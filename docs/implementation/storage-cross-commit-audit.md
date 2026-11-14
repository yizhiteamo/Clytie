# Issue #72：跨库及文件提交清单

核对基于规格同步提交 5c0b6c008a8447fa772fccb734377776172023db。只读工程清单，不是正式审查；没有访问实例数据或执行故障注入。以下正常边界与强杀语义必须分开验收。

| 链路 | 现有提交及证据 | 所有者接线要求 |
|---|---|---|
| core_remember / core_forget / core_recall 强化 | memory/context.py 修改记忆后独立写 ledger core.tool；memory/store.py 同库保存 revisions、trace 和 memory_revision。两次提交间可能缺账本审计，不能重跑工具 | 一条完整 Owner 命令的共同门闩覆盖记忆修改及 core.tool；仍是两次数据库提交。changed 通知移到事件循环，不能在存储线程调异步通知 |
| 回想窗口 | memory/scheduler.py 从 ledger 读取窗口、高水位，登记 memory attempt，再调用模型；模型 usage 单独记账本；recollection.py 的最终记忆修订、recollect_progress、memory.recollect 同事务 | 关联读取及 attempt 是一次命令，模型在外，最终提交再校验。不能锁住跨模型等待的整个窗口，不能把 usage 独立审计当成记忆进度缺事务 |
| 边界计数与解除 | agent/policy.py report 先 ledger 审计再原子替换 state.json；release 先替换 state 再 ledger 审计。正常路径同门闩；启动只读取 state，没有两侧自动补偿协议 | 各自一条同步持久命令，通知调度移到事件循环。保留分阶段事实；共同捕获门闩不等于崩溃原子性，不自行增加补偿日志或重做操作 |
| 导入、restore、undo、thaw | transfer.py 先校验快照、journal登记哈希、发布，再修改记忆；imports、memory_import 和 management_committed 与业务同事务。management_journal.py 按库完整性、提交标记及快照校验恢复 | 保留原操作编号、运行锁及恢复标记；外部 importer 与最终存储命令分离。不能把整个 async apply 搬入线程，也不能因为后续清理失败否认已提交事实 |
| 恢复公钥与加密导出 | package.py 先登记目标及哈希，再替换公钥/发布文件、提交 memory 审计，最后 journal committed；management_journal.py 仅按登记证据补必要审计 | 保留文件日志及条件核验。公钥变化不因响应丢失重复生成，不恢复秘密原文；加密与大文件 I/O 不占业务队列 |
| 资产与账本引用 | assets/store.py 校验并发布内容，再在伴侣门闩及 registry 锁内登记，最后返回 AssetRef；账本引用在后续业务事务。无引用内容可能合法保留，无自动 GC | 大文件发布在门闩外，归属登记及固定资产证明在同捕获边界；不得重跑工具来补账本引用。不能把整个工具生命周期持锁 |
| 工具与发送 | service.py / delivery.py 先登记，再外部执行，再写结果/收据；ledger.restart 将旧任务标 interrupted、发送中标 unknown，不重放 | 收尾凭证覆盖所有阶段、迟到结果及审计，不能第一次写入后释放；外部调用前必须取得凭证 |
| 新目标完整恢复 | backup/restore.py 先 incomplete、解包后 verifying，全部核验后 ready，再释放来源引用；恢复失败保留 blocked | 不接入在线写入队列替代原恢复状态机，不自动重放解包 |
| 记忆迁移 | memory/migration.py 先校验预迁移快照，再同事务更新模式；随后独立写 receipt。receipt失败保留不确定快照，不否认已提交模式 | 保持启动前核验及迁移保护；未核验前不能发布在线所有者 |

本体工具与 Boundary 的两侧提交间隙是已观察到的历史事实；当前没有找到规定“任意强杀点自动补齐另一侧”的完整恢复协议。不能在本次改造中声称已经获得该保证，也不能偷偷增加通用跨库事务框架。如果生产接线或故障验证发现批准的恢复保证确实依赖补齐这些间隙，须按 storage-spec §3 单独报告，由用户与设计审查会话决定。

完整事务、跨场景隔离、真实强杀和生产捕获的验证仍待实施；此清单不能替代对应回归。
