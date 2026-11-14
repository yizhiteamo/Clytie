# A4.3 有限恢复检查点

后续进展见 [第 2 轮修复与验证](a4-3-review-2-followup.md)。本页保留有限恢复结束时的候选、限制和轮次，不代表后续修复提交的当前状态。

本页记录本次获准的工程核查和 Windows 本机验证，不是正式独立审查、性能验收或合并批准。PR：https://github.com/yizhiteamo/Clytie/pull/71 。Issue：https://github.com/yizhiteamo/Clytie/issues/72 。

受检产品提交为 `b4dcd0611184e92c7d2da9fb751edbb3c832967a`，base 为 `cb8734fbf77707f5c8bf62e7d97f517848973aa4`。本检查点之后的提交仅补充当前状态和证据说明；代码、测试、锁文件、工作流及冻结计划没有改动。不能将历史候选结果直接改标为新提交通过。

## 本次边界与实现状态

已有在线捕获、加密包、资产归属、保留/引用、恢复/维护、管理工作进程、StorageOwner 和重 I/O 准入实现候选。首轮 R1–R5 的修复入口及回归已存在；本次有限核查未发现需要改动产品实现的新缺陷。逐项证据见 [验收映射](a4-3-acceptance-map.md)。这不是不存在缺陷、全部子项覆盖或最终实现收敛的证明；Issue #72 的完整性能和最终验收仍未闭合。

源码核查区分实际调用链与测试入口：资产组合经过生产工具、账本、捕获、加密及恢复；跨 home 保护经过维护 API 和独立轮转进程；重启准入经过真实 daemon、HTTP、旧管理执行者及无关伴侣聊天。具体执行结果另见下表，不能用函数名称或测试数量替代证据。

## 固定本机验证

| 检查 | 实际结果 |
|---|---|
| 定向取消、容量、工具与所有者关闭 | 37 passed，35.30 秒 |
| 守护进程真实子进程 smoke | 9 passed，40.47 秒 |
| API/管理真实子进程 smoke | 24 passed、33 deselected，59.32 秒；选择器与 CI 一致 |
| 全量 pytest | 3070 passed、0 skipped、1 warning，1341.34 秒；exit 0 |
| 锁文件检查、通用/私有扫描、ruff/格式、架构/时钟、插件目录 | 全部 exit 0 |
| 虚构 home 离线检查、wheel/sdist | 全部 exit 0 |

唯一 warning 来自 `test_measurement_preparation_evidence.py::test_archive_structure_and_hash[zip_duplicate]` 故意写入重复 ZIP 成员，随后断言解密校验拒绝。不是线程关闭警告；本次日志没有报告 StorageOwner 线程或资源关闭警告，但这不删除或解释掉旧运行警告。所有测试都执行，零平台跳过。591 个受检文件在运行前、中、后保持一致。

完整 pytest 文本 SHA-256：`7e4c0f90777f03a8c968714e0957fc0a234afbab02a76d30c776bc0904580fe5`；JUnit SHA-256：`b8077e2c9c2fa6dd53338ced9384696e0aff4563bb92529ccb439359e5994392`。两者记录方式不同，pytest 终端与 JUnit 的统计边界不同，JUnit suite 时间为 1340.225 秒，不混作两次执行。


定向、冒烟与全量测试集合相交，不相加。历史 969、45、18 项同样不相加。全量期间受检文件冻结，运行前、运行中和运行后保存清单；完整日志、退出码、JUnit、包清单和产物留在本地 `.cache/limited-resume-20261114/`。原件未作为公开附件上传；本页只发布经过规定扫描的摘要。

执行入口复用 `.github/workflows/ci.yml`：`uv sync --locked`、`pre-commit run --all-files`（本机同时含私有扫描）、`ruff check .`、`ruff format --check .`、`python scripts/check_architecture.py`、`python scripts/build_catalog.py --check`、两组 smoke、`pytest`、`python -m clytie status --home examples/home --json`、`uv build --no-sources --no-build-isolation`。额外参数仅指定 JUnit 和构建产物目录，不修改超时、断言或跳过条件。完整实际参数保存在各步骤 started.json。

## 环境差异

本机使用独立项目级环境，保留原环境；`uv sync --locked --check --offline` 通过。uv 为 0.9.5，Python 3.10.6、SQLite 3.37.2、Windows 内核版本 10.0.26200、AMD64、12 个逻辑 CPU、NTFS。uv.lock SHA-256：`e84caf2b359ddf699259b6ee341d973b90f5ac3beec3fe25d86d9be37459466b`。实际安装包及版本见 environment.json，不修改全局 Python、权限或服务。

已归档的旧准备候选 `ba5ec3bbd9d5812871235a25936eff818c073358` 报告 Windows 为 Python 3.10.11 / SQLite 3.40.1，Linux 为 3.10.22 / 3.46.1，均为两个逻辑 CPU。这是旧准备元数据，不是当前 CI 测试结果；磁盘、内核及完整依赖环境的等价性没有证明。本机结果不得标为 GitHub Windows CI，通过 NTFS 也不能推断 Linux 行为。

## 历史与未闭合项

- 交接候选的普通 CI 与准备工作流均为计费门禁、零步骤；平台显示 failure，但产品测试没有执行，不能写成失败或通过。最终文档推送可能自动触发普通作业，另在 PR 追加实际状态，不人工重跑。
- 当前候选 Linux 未验证；不启动替代 Linux 环境，不把 Docker Desktop 内部发行版当作已授权环境。
- 正式机制、可比旧无备份基线、四规模三臂矩阵、完整窗口资源及性能门槛尚未闭合；此次未启动，参数、预算和冻结计划不变。微型测试和准备密文核验不是正式性能通过。
- 原异常基线、Linux EOF 失败、bridge 超时、线程关闭警告、诊断脚本收尾错误及此前原始数据缺口保留各自时点。新功能回归不能反推旧运行没有问题或补造旧原件。
- 首轮完整报告仍为需要修改，来源 `/root/a43_native_review_1`，被审 head `1679eb49aa1d1071fae33050874a219d2f096dc4`。正式预算已用 1/3，本次没有发起第 2 轮或等价整体验收。报告原件 SHA-256：`e9cf186c368ba7f4f57202ce40d3f05a267c39ea5453ab5e196f03f290017e72`。
- #59 保持 review_incomplete，#63 历史超时不因本轮通过关闭；#64–#72 等分支工作仍按 main 证据规则处理。旧审查入口保持停用。

## 公开准备

复用三份既有审计报告；仅整理增量边界，不重新无差别全量审计。报告及 SHA-256：

| 报告 | SHA-256 |
|---|---|
| Clytie-publication-audit.md | `0eaf6e07fa0fef255b2c72c0af6d3b72aca6c5f5115af314f21515a00a31900b` |
| Clytie-publication-supplement-v2.md | `0e2025fbf0c29f0ce4d0326a197a4686bb8474b3450ebf393755be6f6e0dd930` |
| Clytie-publication-authorization-summary-supplement.md | `8e1d7cb2dce67be18f6098963fc00407179559f7af86fa4737cd3f2dc790d94b` |

这些是 `.cache/public-audit-20261113-01/` 及两个 supplement 子目录内原报告，保持原件。原扫描告警经分类处理，不改写为零告警；旧摘要正文扫描输入为零，不能标扫描通过。228 个 Actions 独立步骤摘要仍未取得，动态加载失败不证明没有摘要。分支保护接口权限不足的旧观察也不等于未配置。

两份指定准备密文及绑定回执仅有条件性公开授权，不扩展到新产物、原始明文、日志或密钥。临时认证字段只读取脱敏类型、出现环节与暴露范围说明，没有读取、回显、验证或撤销原值；机制、权限及生命周期未得到核实，原始响应/工具输出仍为受限材料，不作为公开附件。

增量清单：新增文档与提交说明、更新 PR/Issue 正文或评论、平台自动产生的运行/注释、可取得的新日志/摘要及附件，分别保存对象、时点、哈希和扫描状态。没有新增材料时不虚构新核验；不可取得时标未知。公开前仍需用户决定摘要/平台历史剩余风险、临时字段处置及最终公开范围，并再次核对指定附件和时点变化。本次不修改可见性或账户设置。

## 检查点结论

| 维度 | 本检查点结论 |
|---|---|
| 实现收敛 | 有实现和功能回归候选；完整性能及所有验收保证未闭合，不能宣称最终收敛 |
| Windows 本地验证 | 已完成一次冻结候选的完整验证，后续文档差异另核对 |
| Linux 验证 | 当前候选未完成，未启动替代环境 |
| 独立审查 | 未完成；仍仅第 1/3 轮需要修改，本次不调用 |
| GitHub 双平台 / 正式性能 | 未完成；计费门禁不是产品测试结果，正式测量仍暂停 |
| 合并条件 | 不具备；等待用户批准后续补证 |

本次允许的本地验证及证据整理已完成，最终验收待补证。此结论不批准公开、部署或使用候选承担正式备份保障。

候选在交付后暂停修改。下一步需用户批准；即使计费恢复，也不自行启动正式测量、第 2 轮审查、合并、打标签或下一阶段。
