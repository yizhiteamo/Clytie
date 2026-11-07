# ADR 0016：持久备份调度

状态：A4.3 实施技术编码；产品依据 backup-spec §3、§5、§9。

## 接口与存储

`BackupScheduler(home, clock, timezone, config)` 使用注入 Clock；`mark_dirty` 只登记已提交的重要记忆修订，`poll` 原子认领一个请求，`finish` 接收经过核验的结果和覆盖版本。`manual` 登记用户稳定操作号，`shutdown` 使用调用者提供的本次停止编号防重。此模块不调用模型、平台、OS 定时任务，不创建执行进程、不发送通知。

状态为 `supervisor/backup-scheduler.json`，独立 `supervisor/backup-scheduler.lock` 复用稳定文件 OS 锁，每次读改写均在锁内重新读取，再原子替换并 fsync。锁冲突向调用者返回原 OperationBusy，不能使用缓存状态覆盖其他进程。当前执行操作仅一项；进程重启保留 active，外层核验后 finish 或 recover_interrupted，不能自动重放。

请求含 operation_id、trigger_id、scope、companion_id、manual、attempt、requested_versions。自动操作号由持久触发标识和尝试号确定；同类待执行触发合并，已经认领的请求内容不改写。完成请求和用户操作号保留以防重复登记；未来压缩账本须保留防重证据。

## 时间与脏版本

重要修改类型：fact、revision、retraction、import、manual_restore。调用者提供单伴侣单调递增的已提交版本；纯检索、派生向量和淡化维护不登记。首笔未覆盖修改固定 deadline，后续修改只提高版本，不推迟 deadline。完成结果只用明确已验证覆盖版本消除相应脏标记；仍有更新时继续待执行。

日/周计划使用实例时区、本地计划日期防重。重复时刻取第一次；缺失分钟向后寻找同日第一个有效分钟。首次启动无历史时只判断当日到期；已有观察日期的停机间隔按最近一个到期计划补当前快照一次，不伪造历史包。维护模式与未 ready 状态不认领、不推进计划观测；关闭自动备份不删除记录。catch_up=false 时恢复后跳过已错过的计划，只运行新的未来触发。

shutdown 只登记当时有未备份记忆或到期完整备份的待办，停止编号持久防重；shutdown 完成不再递归登记。调用者在业务安全停止后认领最终请求，模块不替代守护停止协议。

## 已确认失败的有限重试（Issue #65，用户选择 1）

同一自动触发首次尝试后最多重试三次，共四次。间隔依次为前次已确认失败时间之后 10、30、60 分钟；持久化 trigger_id、attempt、每次结果和下一 due。成功终止；第四次失败为 exhausted，等待新的计划、实际新修改或人工请求。重启和重复结果投递不清零计数、不推迟 due，也不追赶多个历史重试；到期只认领一个当前快照，后续间隔从其确认失败重新计算。

取消与关窗不记为失败、不重试。未知结果保留 active 与 verification=pending，外层核验前不能创建另一请求。已提交产物即便保留清理或副本失败也不能重建；finish 的 committed 标识可表达已提交事实。recover_interrupted 仅供外层完成执行者与产物核验后终结旧请求，不重放该请求。人工操作保持调用者操作编号，失败不悄悄换编号重发；用户可明确发起新的人工请求。
