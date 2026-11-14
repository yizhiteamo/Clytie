# A4.3 务实收尾执行记录

用户已批准修订范围，起点 1881e4579fc30f1a3670b30a9d3e7376ab0ca927，工作区干净。主规格提交 2a1c65b6e5ba13485fdcd9b49eefc8390c69bb9a；关联同步 2baf0b162429f071a15415021ad0bc8cf23345c7。旧计划、失败、三轮审查及原始证据保留；本页不改写旧结论。

## 执行与预算

- 正式基本性能：Windows/Linux 各大数据库、大资产一次，四场尚未执行；失败停止后续，不自动补场。
- 旧机制八场不启动；完整旧基线/三臂比较延期，不宣称已验证。
- 原审查三轮已用；用户额外批准的一次限定收尾复审尚未调用。完成必要证据后使用原生 gpt-6-astra / high 新上下文，仅此一次。
- 不扩通用工具，不改产品范围、期限、参数或权限；只用虚构实例、假后端。公开附件仍限 measurements.age、receipt.json，本机认证解密及私有扫描前不是可用验收证据。

## 要求与证据映射（执行中）

| 要求 | 测试/证据入口 | 候选与结果 | 缺口 |
|---|---|---|---|
| 全量功能、静态及构建 | 普通 CI；历史 execution-admission-final-01 | 起点双平台成功，Windows 3389；Linux 3382、七项跳过 | 产品修复后最终候选需受影响验证及双平台全量 |
| 三项历史失败 | 原 CI 日志、startup_read、api_smoke、backup_bridge | 原候选及日志保留，当前核查 | 握手根因未知、健康读拒绝、关停期限未完成须分别核对 |
| 备份与新目录恢复、资产与配置 | test_backup_restore、test_backup_assets 等生产链路 | 复用前需核对源码适用性 | 本页后续填写确切测试名，不以文件存在算通过 |
| 取消/强杀/不重放与互斥 | backup_bridge、管理与责任真实进程用例 | 待逐项映射 | 当前旧失败不能以新全量成功覆盖 |
| 源保护、最低保留、清理授权 | backup_source、preview、retention、cleanup 用例 | 待逐项映射 | 核对最终候选影响 |
| 基本性能与真实释放 | 既有合法夹具、生产测量入口 | 四场未执行 | 固定可比阶段、窗口、版本与当场 runtime |
| 受控慢 I/O 控制响应 | 既有存储所有者定向证据 | 待核对绑定与适用性 | 不与四场实负载互相替代 |
| 限定收尾复审 | 额外一次原生审查 | 尚未调用 | 必要证据齐全后执行 |

## 当前不可合并结论

验收尚未完成，不能提出合并通过。工具回归和历史 CI 不替代基本性能、资源或最终候选验证。失败原因、已证安全边界及仍需用户接受的限制分别记录；未知安全后果继续阻塞。全部必要项通过后按用户指定措辞报告并暂停，不合并、打标签、替换审查或推进后续功能。


## 第一检查点：失败调查与限定修复

完整矩阵延期已登记 https://github.com/yizhiteamo/Clytie/issues/77 ，不关闭 #72。代码修复仅区分 `_await_start` 的状态请求 OSError 与确认帧写失败；不改变 deadline、轮询、确认次数或 EOF 取消。真实 Client→status→health 读拒绝与真实 daemon 确认失败退出回归位于 `tests/test_api_startup_read.py`，本机 Windows 锁定项目环境十三项通过；不证明历史故障根因。

| 要求/现象 | 确切证据 | 绑定与结果 | 仍缺 |
|---|---|---|---|
| 状态错误不冒充握手错误 | test_real_client_health_read_denial_is_not_confirmation_failure | 本轮工作树红后绿，完整 diff 与清单保留 | 修复后双平台候选 CI |
| 确认失败不错误成功且安全退出 | test_real_read_failure_closes_pipe_and_safely_exits[confirm_write] | 真 daemon/假插件、原期限、退出与锁释放通过 | 原历史日志没有同样的内部阶段证据，不能反推 |
| 健康快照拒绝 | 原 Windows 日志；严格持有读取句柄的虚构实验 | 既有 atomic_read 复用仍遇 WinError，未采用补丁及失败保留 | 历史 errno 不足以区分共享/权限/外部占用，未解决 |
| 关停后最后一次备份 | test_controller_shutdown_creates_once_after_worker_cancel_drain 单次 | 未复现超时，但 partial 断言失败；任务/进程/槽位已结束，dirty 保留 | 原超时等待链缺失，不能由本轮退出覆盖 |
| 本轮 partial 分类 | 同一虚构数据短/同深度捕获；SQLite 两长度提交对照 | 短路径成功；同深度复制 OperationalError；数据库路径二百五十四字符时 journal 二百六十二字符失败，一百五十字符对照成功 | 这是本次诊断路径问题，不解释历史 CI 超时；不修改全局路径设置 |
| 资产/配置/数据恢复 | test_production_tool_ledger_online_encrypted_restore_authorization；test_real_configuration_credentials_and_plugin_versions | 起点普通 CI 已通过，对应源码未改 | 不冒充本次产品修复后的最终验证 |
| 强杀后不重放与预览/清理保护 | test_real_kill_recovery_never_replays_unpack；test_real_worker_cancel_after_unlink_preserves_progress；test_manual_and_references_and_each_group_protected | 起点证据保留，来源文件不变 | 最终候选与剩余 §13 分支须结合原完整映射核对 |

本机证据目录为 `.cache/closeout-startup-01`、`.cache/closeout-shutdown-01`，原 CI 与实际合并预览、散列在 `.cache/execution-admission-final-01`。缓存路径只作本机索引，原始日志与失败补丁不上传。没有以多集合数量相加作为总验证数。

### 四场入口的实际障碍与人工替代

旧 plan、准入、workflow 和接受器分别固定八场或完整矩阵，不能删除 JSON 行或用 tiny 夹具冒充四场。全面改造这组框架可能超过半个工作日，因此停止该方向。最小替代是仅为此次固定四场清单复用生产 trial、版本核对与既有加密封装，人工核对原始请求分期与资源；不扩通用准入、报告或调度器。无备份期须在冻结前明确样本与边界，不能沿用原很短的备份启动延迟宣称可比。当前正确性未知项仍阻塞，未冻结或执行四场，未消费额外复审。

本检查点结论仍为不可合并。关停操作已接受业务，历史日志没有证明其超时后的内部状态；不得自行把它降为“未接受业务的可用性限制”。四场尚未执行，后续不得用性能通过覆盖该未知安全边界。
