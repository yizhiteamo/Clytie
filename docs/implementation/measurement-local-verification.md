# 机制原件的本机核验

本页只说明已批准八场机制运行的保存与核验，不授权启动运行、重跑、矩阵、CI 或正式审查。没有实际原件时只能准备模板，状态为 `pending_actual_evidence`。本机工具回归中的假扫描器、假 GitHub 元数据和微型事件都不构成正式机制通过。

## 入口与保存位置

`scripts/measurement_mechanism_acceptance.py` 提供两种本机操作。以下变量由操作者明确设置；目录使用仓库忽略的本机证据目录。不得指向部署实例、真实记忆或人设。无需复制整个实例。

```powershell
# frozen-plan.json 是 P 中已经冻结并核验的原生计划对象，不能重新估算或冻结。
python scripts/measurement_mechanism_acceptance.py --prepare-plan $frozenPlan --target $bundle

# 补齐 record.json 引用后，使用已有专用身份；不生成、不轮换密钥。
python scripts/measurement_mechanism_acceptance.py --record $record --identity $identity --target $verification
```

prepare 只创建计划副本、缺值模板和明确未通过的 checkpoint，不读取密钥、不联网、不采集环境、不产生准入。verify 的目标必须是全新目录。成功和失败的目录都保留，禁止删除旧结果后重试取绿。Python API `verify_record(record_path, identity=已加载身份, target=全新目录)` 省略 target 时在记录旁边创建唯一持久目录，也不自动清理。

成功目录含每场认证解密结果、原始五文件、原样聚合报告、取消检查、辅助扫描结果、最小 receipt 和 checkpoint。聚合器的 `not_evaluated`、所有未知原因及限制不被改写。外层成功表示在明确的本机人工信任边界内证据齐全，不是性能矩阵通过或验收完成。

## 八场实际 artifact

逐场使用已有 `measurement_transport.py download`，每个 artifact 只下载到新目录：

```powershell
python scripts/measurement_transport.py download --artifact-id $artifactId --run-id $runId --name $artifactName --target $slotDirectory
```

名称必须是实际固定运行、首次 attempt、平台及平台内序号对应的 `a43-encrypted-<run>-1-<platform>-slot<ordinal>`。不是凭名称查找任意文件。download 生成 `artifact.zip`、`download.json`、`sealed/measurements.age` 和 `sealed/receipt.json`；另保存同一 artifact 元数据 API 的完整响应 `artifact-metadata.json`。只读取得的元数据必须核对运行、artifact 数字标识及来源；不得手写一个看似通过的响应。过期前下载，本机仍保存实际创建/到期时间及平台接受期限，不假称无限保留。

verify 重新读取这些原件：实际 ZIP 的两条内容必须逐字节匹配密文及回执，重新核对下载摘要、运行、名称和期限，再调用既有 `open_bundle` 做真实 age 认证、完整身份/字段/原字节核验和本机私有扫描。已有 `state.json` 或自填 accepted 不能替代这次认证。正常场必须是冻结的 pass；固定取消场必须是 cancelled 且通过既有取消来源、时序、负载及资源释放检查。原解密状态中的 `ineligible` 不改写，外层单独使用已批准的 planned cancellation 证据。其他失败不作取消豁免。

## record.json 格式一

prepare 生成完整字段模板。所有一般文件引用都是 `{"path":"相对路径","sha256":"实际文件原字节 SHA-256"}`，相对于 record 所在目录；路径穿越、符号链接和硬链接拒绝。记录、元数据与 CI 原日志全部留在本机，不直接提交仓库。

| 字段 | 实际来源 |
|---|---|
| format | 整数 1 |
| plan | P 的原生冻结计划副本；仍重新调用 `validate_plan` |
| repository | 含 C/T/P/Q/B 固定 Git 对象的本地公开代码仓库目录 |
| version_directory | 已有 `measurement_version` 导出的 C 目录，含 source 与 source-inventory.json |
| tool_root | T 对应的实际工具文件树；逐文件核对 T 的 Git blob |
| admission_commit | 完整 B；工具重新核对 B→Q→P、准入运行、候选及工具提交 |
| ci_run | 正式机制运行的完整 Actions run 元数据 |
| runs | 恰好八个冻结 execution_id，不能补场、漏场、换 attempt |
| tests | Windows 和 Linux 的真实功能组合证据，见下节 |
| coverage | 人工源码/覆盖核对记录，见下节 |

三个材料目录可以是明确的本机绝对目录，也可以位于 bundle 内；它们只允许指向公开代码，不允许指向实例。C 的 source-inventory 不仅核对自填摘要，还重新对照实际 Git archive。CI 头晚于 C 时，实际比较 src、plugins、schemas、pyproject.toml 和 uv.lock 树；只有全部相同才能使用该 CI，不能把工具提交说成被测产品提交。

每个 runs 项含 runtime、ciphertext、receipt、download、execution、artifact_metadata、downloaded_zip。runtime 引用既有已核验准备证据中的环境、Python 和 SQLite 三字段对象，必须与冻结计划的平台环境摘要一致；认证解密时还会逐项核对密文 run-manifest 的实际值。不得以本机当前环境覆盖，不依赖 CI 临时 expected.json。

execution 严格包含 job（实际平台 job 元数据引用）、log（该 slot 完整步骤原日志引用）、archive（原运行日志 ZIP 引用）、entry（确切 ZIP 条目名）。来源取得方式同下文 Offline tests 原日志；实际步骤名为 `Execute frozen platform slot <平台内序号加一> once and seal stable output`。工具核对该 job 的运行、Q、平台逻辑名称、成功终态、步骤 number/name/终态，原 ZIP 条目必须与 log 全字节匹配。步骤内实际 `run-slot --slot` 参数、成功行及既有 `driver_exit_confirmed=true` 白名单整行必须精确匹配且各只有一次。它只证明外层驱动确认退出；原件中子进程、线程及资源真实退出仍由聚合器独立核对。不能自填 handoff accepted，也不依赖从未上传的 CI 临时 handoff.json。

其余文件来自上一节既有下载输出。人工传递外层 API 元数据与日志不是密码学来源证明，属于下文明确的本机操作者信任边界；完整日志只在本机保存，不新增公开 artifact。

填引用时先读取原文件并计算 SHA-256，再保存引用，不手抄或改写原件。SHA-256 只是完整性绑定，不自动证明来源。补齐模板后运行核验仍会重读全部实际文件，任何修改使旧摘要失效。

## 同产品真实组合测试

tests 的两平台项分别包含：candidate（C）、environment（实际测试环境 JSON，至少 platform）、command（实际参数数组）、results（原结果文件）、result_format、result_source、ci（实际测试运行元数据）、job（实际平台 job 元数据）、sources（测试源码路径到实际 CI head 的 Git blob SHA-256）。environment 是测试执行环境，不能拿正式测量环境替代。ci 必须完成且成功；job 必须属于该 ci、对应实际 head，且 Offline tests 步骤成功。所有被引用的测试源码从实际 CI head 重新读取。

现有 CI 的正式结果来源为 `pytest_verbose`，command 必须是 `['uv','run','--no-sync','pytest','-v']`。使用 Actions 原始日志 ZIP 中 **完整 Offline tests 单一步骤** 的原字节，不使用人为摘取的 PASS 行，也不直接使用带 job/step 多列前缀的 `gh run view --log` 汇总输出。

通过只读 API 下载该实际 run 的日志 ZIP 到本机，保存其完整原件；查阅 ZIP 条目及 job 元数据的 Offline tests 步骤 number，按条目原样提取该文件（不要转换换行或删行）。result_source 为 `{"archive":文件引用,"entry":"ZIP 中的确切条目名"}`；条目 basename 必须是 `<number>_Offline tests.txt`。工具核对唯一条目、实际长度和全部原字节，因此换步、截断或删掉失败行不能通过。日志带 ISO UTC 时间戳可以识别；最终 pytest 汇总必须与逐个测试的状态计数一致，失败、错误、重复、缺行、缺汇总或意外多个汇总都拒绝。

不要上传明文日志 ZIP、完整 metadata、环境或 JUnit。本地取日志不新增公开原件 artifact。旧 CI 未输出逐用例结果时继续标为缺证据，不补造 JUnit。`junit` 格式仅用于确实有实际 JUnit 原件的已有运行，result_source 为 null；解析所有 testcase/failure/error/skipped，不能仅读顶层 tests 数字。此兼容格式不是要求 CI 新增 artifact。

必须有覆盖表所列的真实组合测试模块及源码摘要，尤其真实 daemon 重启组合测试必须实际通过；常规 CI 冒烟不能加算为另一套全量覆盖。跳过项保留，不能满足人工映射。两平台有结果不意味着每个分支都被自动证明。

## 人工源码与未观察要求

coverage JSON 严格包含 candidate、trust_boundary、reviewer、requirements。trust_boundary 固定为 `local_operator_ci_metadata_and_human_source_coverage_not_machine_proof`。reviewer 使用通用逻辑角色，不放私人姓名。

requirements 的 key 是聚合结果中每个 unverified 的完整路径，例如 `requirements.borrower_seriality` 或 `pressure_and_accounting.<子项>`。不同场的同名 reasons 取并集；不能遗漏或凭空删除。每项包含：

- reasons：原报告中的完整原因集合；单数 reason 同样按单元素集合处理。没有原因字段的具名未知使用固定标记 `named_aggregate_predicate_requires_external_evidence`。
- judgment：`covered`，只表示核对者认为该项已有足够额外证据，不改变原 unknown。
- rationale：说明真实调用链、该测试如何覆盖具体缺口、哪些固有限制仍保留。
- tests：Windows 和 Linux 各自的实际通过 testcase 全名列表；不能用 skipped 或不存在的名称。
- sources：C 中源码的 path、sha256、lines（起止行号）；实际字节及行界必须匹配。

核对者必须检查相应断言和生产调用链，不能用任意通过用例凑数。工具只校验引用、实际测试结果、源码字节和行界，**不能机械判断 rationale 的语义、日志获取者的身份或覆盖论证是否成立**。上述判断必须由可信本机外层完成。原生内部字节未知、插件绕过宿主未观察、缓存和不可抢占 I/O 的限制继续保留；不得把这些事实写成已测得。任何层级的 failed 都直接拒绝，人工记录无权覆盖。

## 扫描与矩阵入站

认证原件仍沿用既有严格 Schema 与有限数字例外。辅助 metadata/原日志实际扫描并保留分类，命中材料只留本机，不扩大原件数字例外、不宣称通用扫描全过。人工 coverage 和拟发布最小 receipt 使用未放宽的通用扫描，所有辅助明文及 receipt 都须实际私有扫描通过。扫描器不可用或失败不能完成机制核验。

receipt 只含 format、accepted、candidate、fixture_sha256、tool_sha256、plan_sha256、results_sha256 和明确 trust_boundary。results_sha256 绑定全部原聚合报告、人工核对、扫描分类和实际辅助文件摘要。无日志、路径、私有名单或密钥进入 receipt。

本机矩阵入站调用 `verify_record` 重新执行完整核验；CI 仅通过固定的已扫描 P/Q/B 发布链读取最小 receipt，并调用 `verify_public_record` 校验结构与摘要绑定。后者只证明记录形状与提交一致，不能独立证明私有扫描或人工核对发生，不能被当作密码签名授权。机制失败或缺证据时不进入矩阵。
