# 一次环境采集的最小手动入口

用户批准 Windows/Linux 各一个作业，只读取既有 runtime 字段，并允许本次 environment.age、environment-receipt.json 保存到公开 Actions artifact。公开读者可能长期保留副本；十四天保留期不撤回下载副本。没有产品测试、造数、正式机制或矩阵，不恢复 preparation，不合并 PR #71。

本 PR 仅加入手动工作流及其静态回归，不把 A4.3 产品实现合入 main。GitHub 要求 workflow_dispatch 文件存在于默认分支，因此须用户先合并本入口 PR。提交、推送及 PR 创建均不会触发环境采集；合并本身也不会触发。

## 固定版本与字段

- 产品：`e4dfacca7e3493918ded15a82239139f8394e763`。
- 工具：`5025b999b2eb7b0cc444192fb5d1be2adc0e13fb`，checkout 固定提交，不取可变分支；其 src、plugins、schemas、pyproject.toml、uv.lock 与产品提交一致。
- 公钥指纹：`09d84206c57a4adb3efd462881c0801b7cd9fcadcf103d176ff3b6915fc31eaf`；沿用专用 age 公钥，pyrage 1.4.0。私钥和私有名单留在本机。
- Python 3.10、uv 0.9.5、uv.lock；runner 使用 windows-latest / ubuntu-latest，不挑选或循环重试匹配环境。
- runtime 只有 cpu_count、os、release_sha256、machine_sha256、python、sqlite。摘要分别来自 platform.release 与 platform.machine；后者是处理器架构，不是主机名。不读账号、网络标识或环境转储。具体 Schema 和封装核验位于上述工具提交的 scripts/environment_evidence.py。

## 一次性执行与附件

合并后实际入口为 `gh workflow run environment.yml --ref main --repo yizhiteamo/Clytie`，无用户数据或路径输入。执行前核对 main 工作流字节、固定工具、公钥、产品一致性以及没有既有运行；在本机独占保存 dispatch 登记，触发响应不明先查询，不再 dispatch。

GitHub 持久 run_number 仅允许一，run_attempt 仅允许一；其他编号全部拒绝。只允许 main 手动触发，concurrency 不取消先前运行；每个作业在读取 runtime 前排他登记。失败不清除登记，不重命名、删除重建工作流或换入口绕过计数。一次 Windows 和一次 Linux 可以并行，任一失败不自动重试，也不将另一平台成功当作整体成功。

两个作业上传各自 RUNNER_TEMP/a43-environment/Windows 或 Linux 下精确的 environment.age 与 environment-receipt.json；没有通配符、明文、私钥或环境转储。上传仅在封装成功后执行，不用 always()。artifact 名为 a43-environment-运行编号-平台，请求保留十四天，实际到期时间须执行后核对。

执行后在本机 `.cache/a43-environment-evidence/运行编号/平台/` 下载并核对认证、来源、散列、通用及私有扫描。核验前为加密待核验，不能用于冻结计划。采集不消耗正式测量预算，不替代正式测量当场 runtime 检查，差异保留并停止。

当前尚未触发。此 PR 的出现不代表环境核验完成、A4.3 最终验收通过或增加审查轮次。相关 Issue #72 保持开启；不关闭 #59 或 #63。
