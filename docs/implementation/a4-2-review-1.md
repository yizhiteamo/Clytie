# A4.2 第 1 轮独立手动审查：修复证据

被审提交：`553e3308e7d4bf37f1f8dd5fb5b82d6406c0b094`。
来源：用户转交的独立手动审查结论及修复指令。结论为“需要修改”，正式预算已用 1/3，剩余两轮；自动入口实际正式运行仍为零，自动化隔离保持 `review_incomplete`。设计审查会话对源码和规格的复核不算新增正式轮次。

本文件是开发修复证据，不是原始审查报告或独立通过结论。完整原报告、审查会话标识及覆盖表尚待转交；不能用本文件替代，也不能声称已逐项回答尚未收到的覆盖表。

## 分项证据

| 问题 | 原因与修复 | 直接回归与已执行结果 |
|---|---|---|
| 1 关联快照 | 原恢复只看 SQLite 提交记录。现在在发布已校验快照前，持久记录路径和内容哈希；恢复核验数据库及快照，已提交事实与恢复受阻分开。已完成操作不重验正常轮转后的历史快照 | `test_import_apply_uses_real_commit_evidence` 的正常、缺失、损坏三种情况；原实现缺失/损坏两例失败，修复后三例通过。管理日志与记忆导入关联回归 35 passed；正常轮转、仅受影响伴侣阻止及不重复导入有直接断言 |
| 2 公钥、文件与审计 | 原代码在审计前标终态。现在预先记录必要审计，保留发布阶段至审计成功；启动核验只补齐对应操作的必要审计，不重做导出或轮换，不持久化恢复码 | `test_memory_api_published_file_audit_failure_is_recoverable` 原实现两例失败；`test_real_memory_api_file_kill_recovers_audit_without_reexecution` 用真实子进程调用 MemoryAPI，在公钥替换、审计前、审计后响应前强杀；`test_audited_rotation_never_overwrites_a_later_completed_public_key`。相关回归 42 passed |
| 3 在线转交与停止 | 原发送前拒绝留下 accepted 日志，且执行者仍是守护进程。现在用内部异常区分明确未发送；发送前记录目标进程创建身份，交付不确定保留日志用于核验 | `test_online_management_stopping_before_dispatch_releases_admission` 原实现失败；`test_online_management_uncertain_send_keeps_evidence_and_no_second_writer` 覆盖已提交/未提交两个窗口，断言无第二写入者、重复编号不执行、停止后核验与重新启动。关联管理/守护回归 60 passed |
| 4 单伴侣强杀 | 原超时分支没有全局停止路径的审计、通知问题与结果保存。补齐 force_stop、stop_killed 和内存/健康文件结果 | `test_single_force_stop_persists_result_audit_and_notification` 原实现 cancel/wait 两例失败；修复后检查等待升级、原因、通知去重、其他伴侣不受影响以及持久查询。相关回归 47 passed |
| 5 证据参数数组 | 生成器的原始参数数组未统一脱敏；自动化仍暂停，此轮不修入口、不启动预检 | 已补入 Issue #59。已发布旧证据没有继承目录逐项参数，后续预检报告使用哈希占位；本次核对未在这些位置发现原始目录标识符。这不代表本地原始 evidence.json 可直接分享；恢复使用前仍需虚构标识符回归 |

各组数字有重叠，不相加为测试总数。完整测试命令、最终提交的双平台 CI 和扫描在 PR #58 的后续交接中固定。没有修改产品规格来适配实现。

## 证据范围与限制

- api-spec §10 既有逐项测试及管理强杀、停止边界的索引仍在 `a4-2.md`。本轮新增的真实进程故障注入位于 `tests/management_file_crash_fixture.py`，只运行临时虚构实例中的 MemoryAPI，不连接平台或模型。
- 时间行为使用 MockClock；真实子进程等待使用既有 SystemClock 进程测试边界。通知仅用假后端。
- 第 1 轮原始覆盖表缺失，待收到后按每个观察项分别注明已有证据、补充验证或未覆盖限制，不把全量通过视作填补全部观察项。
- Issue #56、#57 仍待 main 合并后的逐项证据，不能提前关闭。Issue #59 的自动化缺口不因本轮业务修复而视作完成。
- 不自行启动第 2 轮或自动审查；最终候选交用户后重新冻结，用户决定是否交回原独立审查会话。
