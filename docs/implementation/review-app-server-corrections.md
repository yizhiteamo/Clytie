# app-server 预检器修正

本次只修正审查接入和离线回归。正式审查仍为 **0/3**，没有授权调用模型补齐探针。
先前的[预检报告](review-app-server-preflight.md)和 JSON 保留原样，表示当时运行的事实，不代表当前入口通过。

## 配置与权限门槛

- 在已有禁用项之外，显式关闭 plugins、computer_use、in_app_local_automation、remote_plugin、plugin_discovery_prefetch、skill_mcp_dependency_install，并设置空 notify。
- 无线程、无模型的诊断进程只提取有效配置及各来源层的 MCP、插件标识符；为每个标识符生成 `enabled=false` 的 TOML 带引号键，通过参数数组传递。特殊字符不会被当作 TOML 路径或 shell 语法执行。
- 诊断进程不提供通过证明。之后的独立实例重新读取自己的 config/read、configRequirements/read，重新核对所有门槛；有新继承项、管理策略冲突或初始化错误时停止。
- 安全要求清单与启动参数独立。移除启动开关不会移除对应的验收要求。插件入口门槛现在依据有效功能状态、逐项禁用状态、插件默认状态、技能加载状态和 notify 判断，不再使用无条件拒绝占位项。
- 文件系统只将本版协议中的 `glob_scan_max_depth:null` 视为无效力的可选字段；不忽略非空值、未知字段、extends、额外路径规则或 workspace roots。仍要求根目录拒绝、最小运行文件读取、唯一快照根只读、网络禁用。
- 线程 runtime 清单继续要求完整分页、显式 disabled、空 tools、无 toolsError，以及应用不可调用。未初始化、初始化失败、发现失败、未知额外入口或漏掉已配置 MCP 均不算通过。
- 配置在同实例检查前后发生变化，或快照内容变化，都会使检查失效。没有增加全局配置写入、模型调用或命令探针入口。

## 快照证据

旧 JSON 中的 `snapshot_sha256` 实际是路径字符串的哈希，不能用于证明内容完整性；它作为历史证据保留。
新字段名为 `snapshot_path_sha256`。准备新候选时另写 `snapshot-manifest.json`，逐文件记录相对路径、长度及内容哈希，并把清单哈希绑定到候选 metadata。
预检启动前、元数据核对后均核对清单，拒绝内容变化和链接。CLI 路径哈希、可执行文件哈希与版本也绑定到候选。
候选仍由固定提交的 git archive 生成，必要 CI、扫描和 PR head 必须对应同一提交；未来正式候选必须从最新待审提交重新准备，不能复用旧快照证明。

## 无模型协议检查的边界

本机带 `--experimental` 导出的协议进一步确认：

- `command/exec` 是独立命令，没有 threadId，不能替代线程工具证据。
- `thread/shellCommand` 虽然有 threadId，却明确以完整权限执行，不继承线程沙箱，禁止用作探针。
- 两个入口均不在预检器的 RPC 白名单中；没有调用它们，也没有调用模型。
- 正式审查必须沿用完成全部预检的同一进程、同一 ephemeral 线程。元数据通过最多到达“线程工具执行绑定未证明”，仍为 review_incomplete；实例退出后不能把输出 JSON 作为新实例的授权。

## Windows 子进程回归的根因

此前失败时子进程退出码为零。生产路径在账本打开之前设置 `migrating`，账本打开之后先检查停止请求，再进入记忆打开。因此两个 `migrating` 心跳不代表已经进入测试所阻塞的同步记忆打开函数。EOF 若在此之前到达，正常退出是已确认的停止规则。

`test_real_sync_migration_heartbeats_and_eof_waits_without_plugin_start` 现在以真实子进程内的显式文件信号确定阻塞位置，并参数化两种时序：

- `before_memory`：故意在账本打开后、记忆打开前发出两个阶段心跳。EOF 后解除账本返回阻塞，进程正常退出，记忆函数没有被调用。
- `memory_blocked`：等到同步记忆打开函数写出进入信号才关闭 stdin。保留原来的等待时长和进程存活断言；释放阻塞后确认完成信号、正常退出、没有 ready、没有插件版本记录。

修复只改变测试同步，不修改生产停止语义、不延长等待、不删除断言。所有目录和文件都是测试生成的虚构实例。

## 离线验收方法

`tests/test_review_app_server.py` 包含完整合法配置与 runtime 正例，再逐项改变 MCP、插件、初始化状态、工具发现、应用 callable、额外入口、分页、路径权限或网络条件验证拒绝。另覆盖 TOML 特殊标识符、要求与参数分离、管理策略冲突、快照内容哈希、新实例重新核验及同实例配置变化失效。

`tests/test_review_protocol.py` 继续覆盖报告解析、提交一致性、双扫描拦截、发布去重、三轮限制及旧 exec 入口拒绝。
最新实测结果与脱敏证据单独记录；这些离线测试不是实际隔离通过的证明。
