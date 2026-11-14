# 四臂矩阵的本机数值分析

`evaluate_storage_performance.py` 不能直接读取新 format 6 原件。旧入口读取 conditions.json、summary.json 与各 repeat/result.json；它要求旧两臂交替顺序、控制注入样本、persisted_counter、旧组件时长及线程字段。新矩阵是固定四臂顺序与五份原件，不能靠填默认值把缺失旧字段变成证明。

新增 `scripts/measurement_matrix_analysis.py` 是限定的离线输入适配，复用旧分析器的 nearest-rank 百分位函数和数值公式，不修改旧入口。它不运行负载、不访问网络、不读取密钥，也不重新解密。只能在已有认证解密、结构验证和本机私有扫描完成后，读取既有输出目录中的 state.json 与 raw。

## 输入和命令

先按既有 transport 的 download/verify 流程处理每臂密文。保留每份密文、收据、下载来源及本机核验输出。创建本机 `verified-index.json`，内容为九十六个核验目录路径构成的 JSON 数组，每个目录包含 state.json 和 raw 子目录。它只枚举显式路径，不自动递归发现数据；不要提交包含本机路径的索引。

使用实际已冻结 matrix 计划和已核验 workflow run 身份执行：

```powershell
.venv/Scripts/python.exe -m scripts.measurement_matrix_analysis --plan .cache/frozen-matrix.json --verified-index .cache/verified-index.json --workflow-run-id $VerifiedWorkflowRunId --output .cache/matrix-numeric-analysis
```

示例路径是调用者明确选择的本机证据位置；工具不会生成正式计划或补齐缺失证据。输出目录必须是新目录且不在任何输入核验目录内。输出为 numeric-analysis.json；数值不通过返回一，数值通过仍返回二表示整体未判定，永远不返回代表完整性能通过的零。无效、缺失、重复、结果非 pass 或内容被修改时拒绝，不跳过失败臂继续产出通过结果；已存在原件不改写。

## 核对及字段映射

| 新原件字段 | 分析用途 |
|---|---|
| manifest.execution_id / platform / scale / group / arm | 与冻结计划全部九十六个槽位一一对应，不重复、不缺组 |
| manifest 的固定身份、load、execution、环境与工具字段 | 由冻结计划生成 expected manifest 后逐项核对；workflow run 由独立调用参数提供，job 名由固定组派生 |
| manifest.metrics.logical / ledger_rows | 与冻结合法几何核对，不用摘要替代数据规模 |
| requests 中 business 的 id / scheduled_s / started_s / finished_s / outcome | 核对连续请求、总数、预定到达间隔、时间顺序及成功结果；响应毫秒为 finished 减 scheduled，包含排队与积压 |
| resources 的 rss_bytes / wal_bytes / temp_bytes / queue / views | 完整序列最大值，仅报告，不新增资源阈值 |
| requests 中 backup 起止 | 有备份臂必须有唯一备份请求，业务覆盖其起止；不代替真实资源退出核验 |
| state.files 的五份原件散列和大小 | 逐个重新读取实际 bytes 校验；JSONL 逐行 schema 核验，清单读前读后身份一致 |

认证的信任边界仍是既有本机 verify 产物：state.json 不是密码学签名，分析器不能从一个调用者伪造的 accepted 标志证明曾经解密。必须保留并核对原有下载、密文和认证解密证据。此入口重新核对实际五文件内容、来源绑定、完整负载和数值，不冒称重新认证密文。测试中的手写核验状态只模拟这个边界，不证明真实档案认证通过。

同平台同规模的三组旧基线 P99 全部保留，参考值采用三者最低值，与旧 evaluator 完全一致；不改为较宽松的同组值、中位数或最佳新结果。无备份门槛是旧参考乘一点二与旧参考加二十毫秒的较大者。有备份门槛是同组 none P99 乘二与其加一百毫秒的较大者。原备份 original 的完整指标保留供对照，不增设未经批准的 original 相对门槛。判断用未舍入数值，nearest-rank 排名不插值，不去掉尾部样本或把窗口之后完成的请求删掉。

## 不覆盖的验收

输出同时有 `numeric_status` 和固定 `status=not_evaluated`；前者 passed **不等于性能或里程碑通过**。该适配不把普通周期 control 行冒充受控慢 I/O 状态/取消响应实验，不伪造持久末值、实际资源退出、固定视图期限、原生调用链正确性或异常停顿解释。机制通过、资源与正确性核验、明显业务停顿说明及其他既定证据仍须独立结合。缺项不能由数值门槛通过抵消。

本轮仅运行虚构离线测试，覆盖旧基线最低值、nearest-rank 与积压计时、重复/缺臂拒绝、实际原件变更拒绝、外部 workflow 身份及到达计划核对；没有实际矩阵测量或性能结果。
