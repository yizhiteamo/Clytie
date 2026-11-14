# 一次双平台环境采集：待授权方案

状态：用户批准准备入口；未批准实际采集或公开上传。Windows/Linux 各一个作业，仅环境，不造数、不运行产品测试、机制或矩阵。独立记账，失败保留，不自动重试。

## 字段与匹配

明文唯一业务文件为 runtime.json，严格沿用既有字段：environment（cpu_count、os、release_sha256、machine_sha256）、python、sqlite。来源分别为 os.cpu_count()、platform.system()、platform.release() 的 UTF-8 SHA-256、platform.machine() 的 UTF-8 SHA-256、platform.python_version()、sqlite3.sqlite_version。machine 是 CPU 架构字符串，不是机器名或主机编号；不读取 platform.node、hostname、MAC、网络、账号、路径或环境转储。摘要仅针对上述明确输入。

现有计划按整个 runtime 的规范化摘要精确匹配，因此这些字段都需一致；本次不改变版本或资源匹配规则。不会新增临时 runner 名称/身份匹配。实际作业编号、候选、工具及采集次数是来源绑定，不是必须与将来测量作业相同的机器条件。正式测量仍读取当场 runtime，差异立即停止并保留，不轮换 runner 碰匹配。

## 容器和来源

拟公开外层仅 `environment.age` 与 `environment-receipt.json`，不得冒充 measurements.age。age v1/pyrage 1.4.0，复用现有专用公钥和本机私钥，不新增密钥。认证加密内部为 runtime.json 与 archive-manifest.json；后者绑定产品和工具完整提交、实际 run/job/attempt、文件散列、扫描结论。外层只含格式、用途、待核验状态、公钥指纹、密文散列、字节数和固定文件名。

拟上传为本仓库 Actions artifact `a43-environment-<run>-<platform>`，请求十四天；runner 暂存仅 RUNNER_TEMP 下独占新目录，本机下载到忽略目录 `.cache/a43-environment-evidence/<run>/<platform>/`，核对保留期、认证、绑定及双扫描。公开读者长期保存风险与新的容器白名单需用户另行批准；当前不上传、不声称已本机归档。

## 触发和停止

专用手动工作流方案存放于 docs 而非活动 workflows；没有 push、PR、定时或可执行 dispatch 入口。本次只提供库级封装/核验及严格来源校验，测试注入虚构 runtime 与假 CI 身份。用户核对后另行批准一次双平台采集及密文上传，届时固定工具提交、产品提交、公钥指纹、请求及实际运行；首次 attempt 之外拒绝，不修改 preparation 或通用上传入口。

实际采集失败不自动重试，CI 未成功加密不上传任何产物；未知字段、来源不符、私有扫描失败保持未核验。此授权不消耗机制八场或矩阵预算。正式性能、审查、合并和标签均不授权。

## 固定 Schema

所有对象拒绝未知字段；不接受任意字符串扩展或附加文件。校验实现为 `scripts/environment_evidence.py::validate`。

| 对象 | 字段及约束 |
|---|---|
| runtime | environment 对象；python、sqlite 为三段数字版本 |
| environment | cpu_count 为正整数；os 仅 Windows/Linux；release_sha256、machine_sha256 为完整 SHA-256 |
| binding | product_commit、tool_commit 为完整提交；platform 对应 runtime；run_id 为正整数；job_id 固定 environment_windows/environment_linux；attempt 只能为一 |
| 内层 manifest | 固定格式及用途、binding、公钥指纹、runtime 实际字节散列、通用数字误报类别、CI 未运行私有扫描状态 |
| 外层 receipt | 固定格式及用途、加密待核验状态、公钥指纹、密文散列及字节数、固定密文文件名 |

内层 ZIP 只允许两个无压缩普通文件，不允许目录、链接、额外字段、注释或重复成员；解密明文上限六十四 KiB。目标目录必须新建，不覆盖旧证据。失败核验返回未接受，不输出异常原文。

## 待授权的作业方案

本段是方案，不是活动工作流。未来若获授权，入口仅 `workflow_dispatch`，Windows/Linux 各一个固定作业，不使用可扩展矩阵；分别采用普通 CI 的 windows-latest、ubuntu-latest、Python 3.10 和锁定依赖。准备依赖不是产品测试，不运行任何造数命令。最小权限为 contents: read。

执行顺序：核对用户批准的产品、工具完整提交与唯一运行登记 → checkout 固定工具提交（不保存认证）→ 锁定依赖 → 两个作业分别执行 `python -m scripts.environment_capture --product <完整产品提交> --tool <完整工具提交>` → 仅在成功封装及核对后上传精确两文件。当前入口的 `COLLECTION_AUTHORIZED=False` 会在读取运行环境前拒绝，尚无活动工作流或上传步骤。

未来上传路径精确为 `RUNNER_TEMP/a43-environment/Windows/environment.age` 和同目录 `environment-receipt.json`，Linux 将路径中 Windows 换为 Linux。不得使用目录通配符。失败和取消不得改为上传明文或整个目录。唯一运行登记必须绑定实际 run，attempt 检查不能单独防止第二次 dispatch；此登记与上传接线须在执行授权时核对，不将当前禁用入口称为已具备运行准入。

本次产品绑定仍为 `e4dfacca7e3493918ded15a82239139f8394e763`。工具绑定应取包含本次入口的最终完整提交，不能使用旧工具版本冒充；实际采集的 run、runtime 及计划环境摘要目前不存在，不填写推测值。现有旧环境材料只作为历史依据；普通 CI 的 runner 标签或 Python 版本不足以证明完整 runtime 摘要匹配。采集之后仍需核验，才能形成新的计划依据。
