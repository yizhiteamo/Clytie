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

## 本次离线证据与交接

入口实现提交：`2c674f5619c7d1b99abd082e2ea00ccdec4e502e`；方案提交：`043d931`。当前没有实际采集记录、环境 artifact 或本机解密结果。将来执行若改变入口授权或增加工作流，工具版本须重新固定，不能仍声称执行此提交原字节。

Windows 项目环境执行 `python -m pytest tests/test_environment_evidence.py tests/test_measurement_preparation_evidence.py tests/test_measurement_transport.py tests/test_measurement_preparation_transport.py -q`：一百四十一项通过，包含二十三项新环境回归；不能与单独二十三项相加。唯一警告为拒绝测试主动构造重复 ZIP 成员。新文件 ruff 检查通过；文件及两次提交说明的通用、私有扫描通过。测试使用虚构 runtime 和临时密钥，不读取真实运行环境用于证据采集。

方案未新增活动工作流；实际采集与上传仍未授权。现有 preparation 保持手动停用；正式机制工作流仍只识别固定请求文件，本次不创建或修改请求。环境验证不代表正式性能验收或独立审查通过，正式审查计数仍为三轮已用。

其他执行准备仍未闭合：旧基线与三臂矩阵的公开场次接线、最终环境依据及冻结计划，不由本次环境入口回归替代。正式机制八场、旧基线每平台十二场、三臂矩阵每平台三十六场预算不变；本次未消费其中任何场次。

## 用户批准执行后的入口准备（后续时点）

用户已批准一次 Windows/Linux 各一个环境作业，以及仅 environment.age、environment-receipt.json 的公开 artifact 上传，知悉公开副本不能撤回。上文“待授权”保留为前一时点，不代表本次仍缺该授权。明文、私钥、私有名单和其他产物仍禁止上传。

实际入口须先通过独立最小工具 PR 合入 main，因为 workflow_dispatch 要求工作流存在于默认分支；不能为此合并 PR #71。入口固定产品、工具完整提交和原公钥指纹；只允许 main 上的手动触发。GitHub run_number 必须为一且 run_attempt 必须为一，Windows/Linux 作业名固定，工作流级 concurrency 不取消已有运行。不同 dispatch、重跑及不同分支均拒绝采集。作业在采集前排他写入运行绑定，重复调用不重采；该登记单独保存在临时声明目录，不在附件白名单。

实际触发前由实现会话保存一次性本地 dispatch 登记；响应不明时查询既有运行，不再次触发。GitHub 持久运行编号用于跨 runner 拒绝后续运行；不删除、改名或重建工作流重置编号。若首次运行失败、编号不符或平台历史无法核对，保留结果并报告，不自动重试。

已核对 e4dfacca7e3493918ded15a82239139f8394e763 与当前工具候选的 src、plugins、schemas、pyproject.toml、uv.lock 无差异；入口运行前重新核对。公钥指纹固定为 `09d84206c57a4adb3efd462881c0801b7cd9fcadcf103d176ff3b6915fc31eaf`。新工具提交由独立入口 PR 固定，不使用可变 m4-3 HEAD。

本次需要用户合并入口 PR，依授权停在该具体前置条件；未实际触发采集。主产品候选、正式测量计划、预算和审查轮次不因此变化。

## 首次失败后批准的第二次补采与诊断

前述入口已通过 PR #74 合入 main。实际第一次采集 Windows/Linux 均在封装前因 a43-environment 父目录缺失失败，没有 artifact，也没有可核验的 runtime。失败原日志、来源和散列保存在本地，不能用新运行替代。第一次机会保持已用。

用户另批准第二次两平台各一个环境作业及相同两文件公开上传。修复提交 35c39a2 仅在已核对的临时根内准备安全父目录，目标仍独占新建；回归工具提交为 `3b1efd82136c451cd283836e0e6662efd7827c63`。新入口只允许 run_number 为二、attempt 为一，不能改名、重建工作流或放开计数。主产品仍固定 e4dfacca7e3493918ded15a82239139f8394e763，已逐文件核对产品、Schema 与依赖未改变。

另一个独立授权是一次 Windows 定向诊断，受测提交 `003a001d7078513d360d19e1a400c52d990451e2`。只增加 tests/startup_diagnostic_plugin.py 观测渠道与其测试，没有改变产品超时、重试、停止、协议或原断言。已知 Windows 功能失败仍标为根因未定位。通道只输出阶段、相对时间、白名单异常类别、错误码和退出状态，不记录路径、PID、凭据或异常正文。

本机验证：环境、封装传输及观测白名单回归合计一百五十七项通过，唯一警告来自主动构造重复 ZIP 成员的拒绝测试；其中三十五项为环境相关测试，四项为观测白名单测试，不能重复相加。固定诊断提交另运行预先列出的六项启动用例，全部通过，四百一十九条观测经过字段核验；这不是诊断 CI 已执行或历史根因已消除。新入口的静态回归两项通过，ruff、架构及提交双扫描通过。

两个 main 手动入口更新在独立最小 PR 中，分别提交，需用户合并后才可执行。第二次环境采集与一次 Windows 定向 CI 均尚未启动。新计划仍缺核验后的环境依据，旧基线/矩阵接线仍不算完成；正式测量未启动，审查三轮已用。本次没有需要改变产品行为或放宽指标的新决定。

## 第二次环境已核验后的计划输入与诊断入口修正

上段是执行前的历史时点。PR #75 已合并，第二次环境采集两平台成功且完成本机认证解密、来源及哈希核对、私有扫描。通用扫描的运行编号误报按批准例外记录，不称为双扫描全部无命中。两次环境采集机会均已使用，第一次失败保留。本次用户批准采用新的四 CPU 环境，不新增环境采集。

产品仍为 `e4dfacca7e3493918ded15a82239139f8394e763`。新诊断工具为 `7b865241f95be973530eda5106bacd620c6e1b1d`，与产品的 src、plugins、schemas、pyproject.toml、uv.lock 无差异。原几何来源为 `ba5ec3bbd9d5812871235a25936eff818c073358`；夹具和几何工具未改变，产品差异限于调度准入错误处理及 restore_apply 的受控摘要/交接，不改变造数调用链和 MEMORY_TABLES。几何可作为原来源的输入继续核对，不用旧环境或旧耗时作为新环境通过证据。

| 新环境 | Python | SQLite | runtime 原字节 SHA-256 |
|---|---|---|---|
| Windows，四 CPU | 3.10.11 | 3.40.1 | `1b3538dfebacfd0bd5a129293901a7e578a6b4bdf1a96b22b1147ee0e880795a` |
| Linux，四 CPU | 3.10.22 | 3.46.1 | `e1af9d70ab02b85e7775670f0d62c58777f58966d6962586acbdd7f66c06e90d` |

本地 `.cache/plan-four-cpu-01/` 保存完整 runtime 值、原文件/核验记录/认证清单散列、来源绑定、固定产品快照清单、工具清单及兼容性核对。机制八场、含旧基线的对照九十六场已生成新草案，仍明确 unfrozen：成本 profile 与统一窗口未闭合，矩阵还需要机制通过证据，生产矩阵准入/启动接线仍待完成。没有创建活动请求或授权执行文件。历史成本估算不是已测上界，不能据新 CPU 数自行换算窗口或宣称可冻结。正式运行仍须逐字段核对当场 runtime；旧两 CPU 材料保持不变。

首次 Windows 定向诊断在加载 `tests.startup_diagnostic_plugin` 时失败，六项用例未执行；该机会保持已用。干净隔离副本中以 uv.lock 建立独立环境、移除进程 PYTHONPATH 后，原 console 入口同样退出为四；模块入口收集恰好六项，完整执行六项通过，四百零九条白名单观测。此次本机 Python 3.10.6、SQLite 3.37.2，与 CI 不同，不外推成远端运行或历史根因证明。原失败、锁定安装日志、复现、收集、执行日志和散列保存在 `.cache/startup-diagnostic-approved/`，没有覆盖原 CI 日志。

修复只引入固定目标的 `scripts.startup_diagnostic`，通过当前解释器 `-m pytest` 在插件加载前建立仓库模块路径；不注入 PYTHONPATH、不安装第三方 tests、不修改产品或原断言。入口拒绝错误 cwd，透传失败码且只运行一次。工具/观测回归七项通过，最小入口静态回归一项通过。用户另批准的第二次远端诊断需先合入 PR #76：只允许第二个 workflow run 的首次 attempt，固定上述工具。没有新 dispatch；成功但不复现仍须报告历史握手失败根因未知。

正式机制、旧基线和矩阵均未启动，预算不变；审查三轮已用。本次不合并产品 PR、不新增审查、不打标签。上述草案和工具回归不代表性能或最终验收通过。

后续普通 CI 在新增回归直接 import scripts 时发现 console pytest 的同类路径问题；提交 `23b0c773529c8f0957a0b5c818bfe0cdcf8a42dc` 改用既有 tests.measurement_tools 显式加载，console pytest 定向七项通过。第二次远端诊断入口随之固定此提交；诊断脚本和产品字节均未变化。上述本地计划草案仍绑定其生成时的 7b86524，不覆盖旧绑定。PR #76 的首次普通 Windows CI 在原 test_real_detach_api_stop_and_duplicate 中再次报告握手失败，原日志已保存，不推断历史根因，不自动重跑。Linux 普通 CI 通过。
