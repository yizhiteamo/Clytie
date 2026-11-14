# A4.3 矩阵入口与密文交接

本文是执行工具接口说明，不是测量结果、冻结计划或执行授权。尚未创建矩阵请求、准入或正式计划文件；真实执行仍须用户另行明确授权，并先完成机制八场的本机验收。

## 本机到 CI

`measurement_matrix_ingress` 复用现有计划入口的规范编码、通用及私有扫描、单控制文件提交核对和 Git 原件读取。矩阵专用入口只接受冻结的二十四组、每组四臂，共九十六场；基线、原备份、无备份与候选分别沿计划固定源码，不取最新产品。

本机 `record_local(source, target, root, source_directories, basis_files, mechanism_record=..., mechanism_identity=...)` 先调用 `measurement_mechanism_acceptance.verify_record`，使用实际密钥对象完成完整机制材料重新核验。其计算结果须与矩阵的候选、夹具、工具、机制计划及结果五项绑定相同；只给摘要或 accepted 声明不能替代核验。密钥、原始记录和私有材料不进入公开计划。随后核对真实源码、工具及依据文件，写出计划和本机核验记录，并执行双扫描。

通过 `request_bytes` 生成请求候选字节；计划提交 P、单文件请求提交 Q、单文件准入提交 B 均独立绑定。`authorize_run` 只能选择实际观察到的、对应 Q 的首次运行；固定 campaign 登记不随新计划、目录、请求或运行重置。扫描失败或部分写入均保留，不删除后重试。本机操作返回字节，不自动提交、推送或启动工作流。

公开文件名称限定为 `.github/a43-matrix-plan.json`、`.github/a43-matrix-verification.json`、`.github/a43-matrix-request.json`、`.github/a43-matrix-admission.json`。它们目前均未创建。矩阵工作流只在未来单独请求文件变更触发的 synchronize 事件上考虑准入；普通工具提交没有该路径变化，不执行测量。CI 读取本机双扫描并由 P/Q/B 固定的声明，不声称在 CI 重新验证了私有材料。

## 串行执行

`.github/workflows/measurement-matrix.yml` 固定二十四个作业，按 Windows、Linux、四个规模、三个组次排列；每个作业依赖前一作业成功。组内四臂在同一个 runner 上按计划既定次序执行，共用环境和窗口。首次 attempt、请求事件、实际 run、固定作业、产品、工具、源码归档及环境不匹配均拒绝。缺少 B、B 不是 Q 的单文件子提交、网络未知或等待耗尽也不会启动试验。

每臂执行前独占登记 started，子进程入口独占登记 entered。未知退出、失败和部分记录不回收场次。密文封装完成本身不能启动下一臂。

## 上传后交接

机制与矩阵两入口共用 `measurement_cipher_handoff`。`run-slot` 在来源、清单及封装检查后，只输出精确 `measurements.age` 和 `receipt.json` 两个路径，并保存本地两文件摘要。上传步骤沿原授权保留十四天；失败场次已封装的密文仍可保存，但不会继续。

上传动作成功后，`confirm-upload` 核对动作返回 artifact id/digest、远端元数据的名称/摘要/运行/有效保留时间、运行的请求 head/工作流/首次 attempt，以及本地精确两文件摘要未变。成功记录状态为 `upload_confirmed_not_authenticated`，`local_archive_verified` 为 false。没有 CI 下载、没有 Actions read 权限、没有远端字节真实性或验收通过声明。真正的下载、ZIP 白名单与字节核对、认证解密和私有扫描留在既有本机通道。

矩阵只有在该确认成功、驱动退出成功且 started/entered 均匹配后写 completed；机制下一槽也必须读取前槽确认记录。上传失败、响应未知或确认失败均不写成功交接，YAML 的 success 依赖阻止后续臂和后续组。手动重复命令也不能重用独占场次或覆盖交接记录。

两工作流仍只有 contents: read 权限。通用 transport 的私有仓库上传门禁及 preparation 门禁均未放宽；本轮限定公开密文路径在专用准入检查后才开放。

## 本机调用接口

本机 P/Q/B 准备提供 Python API，目前没有对应 CLI。使用 `from scripts import measurement_matrix_ingress as ingress`。以下方法不提交、不推送；调用返回的公开候选字节仍按既有批准流程处理。

- `validate_payload(raw)`：只读核对规范编码的计划 bytes，返回 payload；不检查外部材料、不授权执行。
- `record_local(source, target, root, source_directories, basis_files, mechanism_record=record_path, mechanism_identity=identity)`：source 是本机规范编码矩阵计划；target 是尚不存在的证据目录；root 是检出工具 T 的仓库；source_directories 恰含 candidate/legacy/original 三个实际归档目录；basis_files 是依据摘要到文件 Path 的完整映射；record_path 是完整机制核验记录；identity 是专用私钥加载得到的对象。成功输出 target/plan.json、target/verification.json、target/verified.json，返回 target。私钥不进输出。
- `pair(root, plan_commit)`：只读核对完整 P 中的固定计划及核验记录，返回 `(payload, plan_bytes, record_bytes)`；只做公开校验，不声称重做私有扫描。
- `request_bytes(root, plan_commit, local)`：local 是上述 target，重新核对 P 与本机记录并双扫描，独占写 local/request.json，返回其 bytes。
- `request_at(root, request_commit)`：只读核对完整 Q 的父提交和单请求文件变化，返回请求对象。
- `authorize_run(root, request_commit, local, run_id)`：查询对应 Q 的实际待执行首次 run，独占写 `.cache/measurement-matrix-ingress-campaign/admission.json` 并双扫描，返回 B 的准入 bytes；不 dispatch。

P 的仓库目标是 `.github/a43-matrix-plan.json` 与 `.github/a43-matrix-verification.json`，只复制通过双扫描的 target/plan.json 和 target/verification.json 精确字节。Q 的唯一目标是 `.github/a43-matrix-request.json`；B 的唯一目标是 `.github/a43-matrix-admission.json`。这些文件目前不存在，本文不执行创建。完整 P/Q/B、实际 run、每次调用及失败须留本机，不能重置 campaign 绕过预算。

CI CLI 是 `python -B -m scripts.measurement_matrix_workflow` 的 `wait`、`prepare --admission-commit <B>`、`run-slot --slot <0..3>`、`confirm-upload --slot <0..3>`，要求真实 Actions 上下文。artifact id/digest 只取相邻上传动作的输出环境变量；计划不能提供任意上传路径或新负载。每一步仍核对实际工具清单的全部 T 字节，不仅核对入口脚本。
