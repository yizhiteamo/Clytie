# A4.3 公开测量密文：执行前准备

状态：仅准备；未启动正式机制、旧基线或矩阵。用户批准公开读者下载并长期保留限定密文，保留期限不能撤回副本。正式审查已用三轮，不追加。最终验收未完成。

## 版本和范围

- 原产品候选：e4dfacca7e3493918ded15a82239139f8394e763。
- 主规格提交：d5ffe63；关联文档：28a83e3；工具：085f299；回归：79d6f3c。
- 可用于后续重新核验的工具固定提交：79d6f3c5abaad107c80da621b211bd31c33603c9。
- 工具清单摘要：01681cf53fa1439c38fa0796a7b54f3f03f9d3756688ce10237fd502c2b31844。
- 旧无备份基线：05ec93c7c4ae972b7475911b0aec9db57bb00390；原备份：b40bbbc31cfb2ff012a151d0e0e9c79acf07d903。

相对原产品候选，src、plugins、依赖、工作流和请求文件逐字节不变。只修改证据规格、关联说明、三份测量工具和四份测试文件；本清单为另一个文档提交。不修改审查工具、参数、门槛、负载、预算或产品行为。第三轮报告仍绑定原被审提交，不声称审查过本次工具变更。

## 白名单与准入

公开上传仅返回 `measurements.age`、`receipt.json` 两条精确路径。回执固定为格式一，含待核验状态、格式、接收者指纹、密文摘要、大小和固定文件名。五个明文输入仅在加密范围内：requests.jsonl、storage-phases.jsonl、resources.jsonl、release-events.jsonl、run-manifest.json。

正式机制路径须通过原有固定计划、实际运行、场次顺序、独占派发、产品及工具字节、真实退出与原件检查，成功封装后再核对同一仓库和密文集合。公开可见性不单独授予资格。通用 upload-gate、ci-seal 和 preparation 仍拒绝 PUBLIC，不能借本次批准上传任意产物。未知身份、字段、来源或扫描结果拒绝；CI 不取得私钥或私有名单，不上传明文与完整日志。

## 离线验证

Windows 本机 Python 3.10.6，项目锁定环境。命令分别为 `python -m pytest tests/test_measurement_transport.py tests/test_measurement_plan_ingress.py tests/test_measurement_mechanism_workflow.py -vv -x -o faulthandler_timeout=30`（80 项通过），以及 `python -m pytest tests/test_measurement_transport.py tests/test_measurement_preparation_transport.py tests/test_measurement_preparation_request.py -q`（113 项通过）。两集合交叠，不相加。

覆盖公开同仓准入、错误运行/提交/attempt/仓库拒绝、预算不重置、固定字节、四槽顺序、失败退出及封装拦截、精确密文路径、公开下载与安全提取、preparation 原限制。网络和正式场次使用假执行器，Git/age 临时材料真实执行；不是实际远端上传或正式性能证明。Ruff 通过；各提交文件与说明通用、私有扫描通过。

首次组合选择出现测试辅助加载器缺少 measurement_launch 依赖及后续级联失败，补齐测试依赖后验证通过。两次重定向日志的无输出运行已中止，未计为通过；不据此推断产品故障。历史失败和未核验材料不覆盖。

## 计划、环境及剩余事项

仓库尚无正式 mechanism-plan、verification、request、admission 文件，本机正式 campaign 准入目录也未创建。旧测试临时目录中的计划不是正式计划。本次没有消费执行预算：机制两平台各四场，共八场；额外旧无备份两平台各十二场；三臂矩阵两平台各三十六场。启动前仍须核对完整执行历史，不能以新目录重置计数。

旧准备材料记录 Windows Python 3.10.11 / SQLite 3.40.1，Linux Python 3.10.22 / SQLite 3.46.1。最新功能 CI 使用 windows-2025-vs2026 和 ubuntu-26.04，Python 分别为 3.10.11 和 3.10.22；功能 CI 未提供 SQLite/完整测量环境摘要，不能因此认定与旧准备材料等价。

尚需：将原产品候选与新工具固定提交、合法几何、成本依据及实际 runner 环境重新绑定并完成本机双扫描；形成正式计划及执行授权。缺少环境匹配时拒绝，不能静默改参数或增加准备/正式次数。后续矩阵仍缺完整公开场次准入接线，不以机制入口替代，也不在本次新增执行框架。

真实上传、平台接受的十四天期限、本机下载解密及私有扫描尚无本轮新证据。普通功能 CI 保持启用，preparation 停用；正式测量未启动。此次不推送触发请求、不合并、不打标签。等待用户明确授权启动正式机制验证及后续矩阵；任何新增环境采集或运行次数仍须遵守既有预算和授权。
