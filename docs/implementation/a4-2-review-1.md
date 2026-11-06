# A4.2 第 1 轮独立手动审查：修复证据

被审提交：`553e3308e7d4bf37f1f8dd5fb5b82d6406c0b094`。
来源：用户转交的独立手动审查结论及修复指令。结论为“需要修改”，正式预算已用 1/3，剩余两轮；自动入口实际正式运行仍为零，自动化隔离保持 `review_incomplete`。设计审查会话对源码和规格的复核不算新增正式轮次。

本文件是开发修复证据，不是原始审查报告或独立通过结论。完整原报告和覆盖表已由用户转交，逐字保存于本地轮次目录并通过双扫描后发布到 PR #58，评论标题为“第 1 轮手动正式审查原报告”。来源注明用户转交的独立 Codex 审查会话；未虚构额外会话标识。原报告中的阻塞、不利结论和验证限制全部保留。

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
- 第 1 轮覆盖表的逐项回应见下表；测试通过不等于独立审查通过，未覆盖组合场景仍明确保留。
- Issue #56、#57 仍待 main 合并后的逐项证据，不能提前关闭。Issue #59 的自动化缺口不因本轮业务修复而视作完成。
- 不自行启动第 2 轮或自动审查；最终候选交用户后重新冻结，用户决定是否交回原独立审查会话。

## 原审查覆盖表逐项回应

| api-spec §10 | 已有/新增证据 | 尚未覆盖或证据边界 |
|---|---|---|
| 1 本机访问 | `test_loopback_auth_host_origin_csrf_audit`，真实回环 Host/Origin 与认证拒绝 | 不测试非本机服务或真实平台 |
| 2 通行证 | `test_restart_token_and_manual_password_removal` 及认证测试中的权限断言 | Linux 文件权限由 Linux CI 验证，不用 Windows 结果代替 |
| 3 口令与会话 | `tests/test_api_auth.py` 的 MockClock 会话、锁定、修改及重启回归 | 不使用真实口令 |
| 4 确认与口令 | 扩展 `test_all_confirmation_routes_and_password_policies`：表中每条路由补未设置/已设置口令的确认成功路径，保留缺确认、危险操作缺口令及网页重认证拒绝 | 该参数化测试使用业务响应桩验证统一安全边界，不声称每条路由所有业务参数组合都端到端覆盖 |
| 5 审计 | 问题 2 的生产 MemoryAPI 强杀与审计失败、问题 4 的 force_stop 直接断言；既有 HTTP 审计敏感字段检查 | 不以无异常退出当作审计成功 |
| 6 守护操作 | 问题 4 的取消/等待升级强杀结果、通知、去重、健康文件与其他伴侣隔离 | 通知只用假后端 |
| 7 执行位置 | 既有 `test_restart_verification_opens_database_only_in_worker`；问题 3 增加真实回环 HTTP 接受后、发送前停止竞争 | 竞争测试使用假伴侣进程；不冒充真实伴侣进程和管理接受同时争锁的完整组合测试 |
| 7a Issue #56 | 既有 plan_kind、running_undo_and_restore、disabled_and_uninstalled 测试 | U1–U4 保持原规则 |
| 8 插件与配置 | 新增 `test_applied_not_saved_status_persists_until_save_or_restart`，验证写回失败提示持续存在，仅成功保存或重启清除；既有字节保留、还原、跨进程竞争 | 新测试的写回失败由假文件后端注入，实际 YAML 写回另有直接回归 |
| 9 插件路由 | 扩展 `test_real_plugin_route_mount_panel_and_unavailable`：真实回环 HTTP 经 Server、Management、companion_operation 到实际装配的虚构插件，并检查审计 | 进程转交在该测试中用内存桥接，不声称含真实 IPC；协议/进程转交另有冒烟 |
| 10 CLI | `test_management_cli_uses_client`、`test_yes_skips_confirmation_not_password`、离线拒绝及响应丢失回归 | 尚未穷举每个命令所有参数和输出组合；保留观察，不扩大命令功能 |
| 11 后台 | `tests/test_api_smoke.py` 双平台真实进程 detach、失败清理与停止 | 假进程启动超时不冒充真实超时强杀 |
| 12 转交 | 问题 3 的明确未发送/交付不确定、已提交/未提交、无第二写入者、重复编号不重放 | 交付后故障使用确定性传输注入；跨进程日志和锁另由真实进程测试覆盖 |
| 13 端口 | 新增 `test_real_foreground_survives_all_default_ports_occupied`：十个默认端口占满，真实前台与假伴侣继续运行、API 不可用提示，前端消失后安全退出 | 不连接占用端口上可能已有的服务 |
| 14 脚本 | `test_launch_scripts_only_start_foreground` | 静态命令检查，不创建服务、自启或快捷方式 |
| 15 管理恢复 | 问题 1 真实 MemoryAPI 导入后快照故障；问题 2 六种真实子进程强杀；问题 3 接受/停止竞争；既有父进程/树强杀、OS 锁、进程创建身份、取消阈值 | 既有事务强杀仍以人工事务为夹具；真实导入提交丢失另用生产 API 注入。尚未把同步导入器阻塞、完整 HTTP 停止及多个真实伴侣同时停止组合成一项端到端测试；`test_ignoring_management_cancel_does_not_delay_other_companion_stops` 是分段编排证据，不能冒充该组合已覆盖 |

上述限制交第 2 轮独立审查复核，不删除原报告的观察项，也不将全量通过解释为所有组合故障都已验证。
