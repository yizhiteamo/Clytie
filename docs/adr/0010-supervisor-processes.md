# ADR 0010：守护进程、通信与停止流程

状态：A4.1 实现前技术约定。需求以 supervisor-spec 和用户确认的 Issue #47 为准；本文件不宣称实现已经完成。

## 进程与锁

前端窗口负责日志显示和停止操作，真正的守护进程按 Issue #47 以脱离控制台方式启动：Windows DETACHED_PROCESS，Linux setsid。Ctrl+C、普通关闭窗口和前端通道 EOF 触发取消模式安全停止；第二次 Ctrl+C 发 escalate。Windows 伴侣子进程纳入守护进程持有的 kill-on-job-close Job Object，子进程用独立进程组；所有平台的伴侣子进程在标准输入 EOF 后按规格取消、不等交付，5 秒内退出。

守护进程运行期间持有 run/supervisor.lock。子进程“同样的锁”指 kernel-spec §14 已有的 companions/<cid>/ledger/.lock，不重复获取父进程持有的 supervisor.lock。Windows msvcrt 字节锁和 Linux flock 沿用插件宿主的稳定文件锁实现，以操作系统锁为存活判据，记录 pid 和进程创建时间，不通过删除锁文件抢锁。

Issue #48 用户确认：注销/关机仅尽力停止，收到通知后立即停止开始新的发送、写 session_ending 审计，再按取消模式停止；不阻止或推迟关机。被系统中断时沿用 unknown 不重发、interrupted 不重放的账本恢复规则。普通关窗、Ctrl+C 和前端 EOF 仍保证取消模式的完整安全停止。

## 通信与健康

父子标准输入/标准输出使用规格已规定的逐行 JSON，每条消息带 type 和 seq；子进程日志使用 stderr 和日志文件。通信只传状态、计数、原因码，不传聊天、记忆、凭据或平台账号；未知 type 只记 WARN。

Channel.state 是与 features 相同的同步缓存查询，不做网络探测。模型状态只取正常业务调用结果，尚无结果为 unknown，不通过 features 推断登录态，不发诊断模型调用。PluginUnavailable 的 code 由宿主原样传递；不解析异常消息来猜原因，message 不进入通知与心跳。

## 启动时迁移

先按 kernel-spec §14 处理账本迁移日志，再判定是否 migration_blocked。已提交而残留的 migrating 日志可以清理、审计后正常启动；处理后仍被阻止、恢复未完成或缺少迁移步骤时报告 migration_blocked。

插件恢复阻止只在必需的平台或模型插件受影响时阻止整个伴侣；其他插件沿用现有隔离规则，不加载该插件并报告状态。

## 安全停止中的交付窗口

停止步骤二只处理当时已经存在的 intent：尚未开始的标为 shutdown_before_send，已开始的组按既定规则完成。等待重发的 intent 沿用 kernel-spec §6，以 failed 结束并保留最后错误码。

步骤三期间交付层照常处理执行中任务自然完成的产物；所有任务到达终态后，再开始一次 delivery_wait_seconds 等待。到时还没开始的 intent 标为 shutdown_before_send，已开始的组按 kernel-spec §7 处理；全过程受守护进程总超时限制。取消模式下，被取消任务不能登记新的 intent，任务状态表情遵循已有例外。

## 未送达通知

在 supervisor/notify-state.json 中按通知 key 保存 state（cooldown 或 failed）与 at，不保存正文。failed 不进入 6 小时冷却，只用于限制同一 key 的重试间隔为至少 10 分钟；送达后转为 cooldown。重启后仍遵守重试间隔，只在问题继续存在时重试；问题已消失时移除对应 failed 状态。

健康汇总中的未送达数量按 failed key 计数。这样 supervisor status 只读取 health.json 就能显示数量，不需要读取通知正文或连接其他进程。

## 验证计划

用假启动器、假插件、假平台、假模型、假通知后端和 MockClock 覆盖 supervisor-spec §10；真实子进程冒烟仅使用临时实例和 fake-channel/fake-model，在 Windows 与 Linux CI 中运行。不创建系统服务、自启或计划任务，不弹出真实通知。
