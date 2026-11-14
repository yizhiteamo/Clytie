# 一次 Windows 握手诊断

用户批准固定候选的一次 Windows 定向 CI 诊断。原功能失败仍为根因未定位；本机原测试单次通过和故障注入不能关闭它。

固定受测提交 `003a001d7078513d360d19e1a400c52d990451e2`；产品 src、plugins、schemas、pyproject.toml、uv.lock 与 `e4dfacca7e3493918ded15a82239139f8394e763` 一致。仅增加测试侧观测，不改生产超时、重试、停止、协议或断言。环境补采另用固定工具，两个运行分别记账。

入口 `gh workflow run startup-diagnostic.yml --ref main --repo yizhiteamo/Clytie`，需该最小 PR 先合入 main。仅 Windows，首次运行、首次 attempt；没有 push/PR/定时触发，没有矩阵、循环或自动重试。合并前不执行，合并后先登记并核对没有既有运行，响应不明查运行而不重发。

环境：windows-latest、Python 3.10、uv 0.9.5、固定提交的 uv.lock。命令在 workflow 中完整固定：加载 tests.startup_diagnostic_plugin，用 pytest -q -s --tb=no 运行原维护启动参数化用例，以及原状态读拒绝、其他读 I/O 错误、真实 Windows 共享拒绝后释放用例，预计六项。未执行全量或循环同一用例。

白名单观测字段：event、stage、elapsed_seconds、exception、errno、winerror、child_state、exit_code。阶段仅固定枚举；异常类别仅已知枚举，其他记 Other；不包含异常正文、文件路径、PID、账号或环境转储。错误码以固定两组四位十六进制表示，去掉冒号即为三十二位值，负数用补码；未知为 null，不推测为零。通过测试侧 sys.settrace 观察当前线程调用和异常，原调用、期限与清理保持不变；不将观测开销用于性能验收。

每个用例最多一千零二十四条观测，超出会明确使诊断不完整，不截断后宣称通过。子进程只查询退出状态，不延长等待或替其收尾。当前观测不进入子进程内部，因此如仍缺子进程内部异常，保留缺口，不猜根因。

观测仅以固定 JSON 行进入该诊断作业日志，本工作流没有 artifact 上传步骤。不是环境 runtime 采集，不输出另一份环境包，也不上传明文运行目录或凭据。现有失败日志、本次结果和候选均须保存，若未复现，结论是本次未复现、历史根因仍未知；工具失败不补跑。

本机离线通道验证：四项白名单/异常观测测试通过；固定提交的同一组真实与模拟启动路径六项通过、观测行字段检查通过。不是 Windows CI 已通过，也不是原失败已修复。正式审查三轮已用，本 PR 不启动新审查、不合并 PR #71、不触发正式测量。
