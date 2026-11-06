# Issue #61：后台启动状态文件读取

范围仅为 `api-spec.md` §6.2、§10 第 11 项已经批准的后台启动行为，不涉及 A4.3。修复与流程文档 PR #60 分开提交、审查。

## 事实与诊断边界

- 已观察：PR #60 的 Windows CI 在 `test_real_two_companion_enable_uninstall_race` 的后台启动阶段，读取 `run/api.json` 时抛出 `PermissionError`。尚未开始该测试的插件并发操作。Linux 同轮通过。
- 已核对：`_await_start` 的状态文件读取原本位于异常处理外，读取拒绝直接逃逸；外层 `finally` 仍关闭启动管道。不能把这个事实描述为“原先没有请求安全停止”。
- 定向复现：修复前新增的八项测试失败，包含 Windows 真实句柄独占读取后发生的 `EACCES`，其余为明确的故障注入。修复后这些测试通过。
- 推断限制：真实句柄实验说明一种可能触发条件，并未识别原 CI 中拒绝读取的来源、持有者或持续时间；不认定原 CI 一定是临时共享冲突，也不以重新运行取绿作为诊断。

## 处理边界

仅在启动等待中的 `api.json` 读取点处理拒绝访问。读取被拒绝时不宣告就绪，继续使用原来的同一个 30 秒截止时间；持续不可读则返回 `startup_timeout`，明确提示状态文件不可读。读取恢复后，HTTP 就绪请求只使用剩余时间。其他文件 I/O 错误立即返回 `startup_failed`，不重试、不宣告成功，不输出原始异常中的路径或内容。

进程退出检查、取消传播、启动确认消息、启动管道关闭与既有 EOF 安全停止逻辑保持不变。没有改变 ACL、全局配置、通用文件读取器或状态文件发布方式。

## 针对性证据

`tests/test_api_startup_read.py`：

| 检查 | 回归测试 |
|---|---|
| 一次拒绝后读取恢复，只发一次确认 | `test_read_denial_then_readiness_sends_one_ack` |
| 持续拒绝不重置期限、无确认、提示原因 | `test_persistent_read_denial_times_out_without_reset_or_ack` |
| 文件读取与 HTTP 共用截止时间 | `test_read_denial_does_not_extend_remaining_http_budget` |
| 其他 I/O 错误明确失败，不泛化重试 | `test_other_read_io_errors_fail_explicitly_without_retry` |
| 进程退出和调用方取消仍清理启动管道 | `test_read_denial_preserves_exit_and_cancellation_cleanup` |
| Windows 真实文件句柄拒绝与释放，不改权限 | `test_real_windows_read_denial_then_release_without_permission_changes` |
| 真实守护进程的读取失败、超时及取消，关闭管道后安全退出 | `test_real_read_failure_closes_pipe_and_safely_exits` |

业务期限使用 MockClock；真实进程启动、退出使用有上限的 OS 等待。真实后台进程只加载临时虚构实例及 fake-channel/fake-model。Windows 句柄实验在 Linux 明确跳过，其他回归均在两平台运行。真实进程测试同时加入 CI 的 API 冒烟选择；冒烟与全量存在重复，不累加计数。

首个修复候选的 Windows 冒烟通过，但全量在新增取消用例观察真实进程退出时超时；本机全量通过。后续有界本地诊断覆盖发布后的多个取消时点，未复现该退出超时，不能据此断言环境原因。原 CI 失败证据保留，未重跑该流水线。

因此补强真实退出测试：临时实例明确使用规格允许的 10 秒停止预算，保持原 30 秒 OS 观察上限；除退出成功外，必须有 `stop` 审计、不得有 `force_stop`。失败输出仅来自该临时虚构实例的健康状态与进程日志，便于区分停止阶段和进程退出。此配置不改变产品默认值，也不允许以强杀或放宽等待掩盖退出问题。新候选的 CI 结果不能倒推旧失败的原因。

最终提交、全量结果、双平台 CI、扫描与独立审查报告以修复 PR 的固定提交证据为准。开发验证不替代独立审查，Issue #61 等合并 main 后按证据关闭。
