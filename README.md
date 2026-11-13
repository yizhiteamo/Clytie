# Clytie

Clytie 是可复用的 AI 伴侣框架：本体提供事件、账本、记忆、边界、插件宿主和守护进程；模型、平台、能力和行为通过插件接入。每个伴侣使用独立的配置、人设与记忆。

主线已合入 A1 至 A4.2：SDK、插件宿主、账本与交付、对话、记忆与回想、守护进程、本机 API 和 CLI。当前 A4.3「备份与维护」已有实现候选，最终验收未完成。范围与验收标准见 [路线图](docs/roadmap.md)，架构入口见 [架构总览](docs/architecture/overview.md)，当前证据与限制见 [A4.3 检查点](docs/implementation/a4-3-limited-validation.md)。真实平台和产品模型接入、部署切换仍需后续阶段及用户授权。

## 安装与离线验证

要求 Python 3.10 或更高版本。本机与 CI 使用相同的 `uv.lock`；首次安装依赖需要网络。

```text
python -m pip install uv==0.9.5
uv sync --locked
uv run --no-sync python -m clytie status --home examples/home
uv run --no-sync python -m clytie status --home examples/home --json
uv run --no-sync pytest
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync python scripts/check_architecture.py
uv run --no-sync python scripts/build_catalog.py --check
```

`status` 离线校验配置、插件清单和人设文件存在状态，并读取已有存储状态以汇总记忆、账本及恢复提醒；不启动运行时服务、不导入插件、不读取人设正文、不连接平台或模型。退出码为：有效 `0`，配置错误 `1`，实例目录不存在 `2`。实例目录优先级为 `--home`、`CLYTIE_HOME`、仓库同级目录。

SDK 规格见 [sdk-spec.md](docs/architecture/sdk-spec.md)，配置格式见 [config-spec.md](docs/architecture/config-spec.md)。插件作者可使用 `clytie.sdk.testing` 中的假实现、`MockClock` 和 `assert_*_contract` 函数；完整样例见 [fake-model](plugins/fake-model/README.md) 与 [fake-channel](plugins/fake-channel/README.md)。

## 开发约定

### 命令行使用须知

管理命令通过本机 API 操作实例；先用 `python -m clytie supervisor start` 启动。加上 `--detach` 可在 API 就绪后返回，停止使用 `python -m clytie supervisor stop`。启动脚本只打开前台窗口，不安装服务或开机自启。

管理通行证只允许本机调用，不能代替对当前系统用户的信任。建议用 `python -m clytie admin set-password` 设置管理口令。设置后，CLI 的最危险操作也要求口令；`--yes` 只跳过确认，不能跳过口令。修改口令需要旧口令；忘记时由用户手动删除实例内的 `secrets/admin.json`，程序不会代删。

确认前请阅读目标编号和影响说明。普通关窗会请求安全停止；已接受的记忆管理操作可能仍在安全收尾。请求断线、超时或进程退出不代表操作成功，也不能据此重新执行；保留操作编号并查询状态。全部静默时 CLI 会显示提示。

开始前阅读 [AGENTS.md](AGENTS.md)，以及实例目录中存在的 `AGENTS.local.md`。实例目录由 `CLYTIE_HOME` 指定，未设置时为仓库同级的 `Clytie-home/`。部署配置、真实身份、凭据、人设、聊天和记忆都留在实例目录。

测试只使用手写的虚构数据、假模型、假平台和模拟时钟。实现按里程碑分支提交 PR；由设计验收和用户合并完成交付。

## 提交检查

完成依赖安装后，必须安装文件和提交说明检查：

```text
uv run --no-sync pre-commit install --install-hooks
uv run --no-sync pre-commit run --all-files
uv run --no-sync python scripts/audit_history.py
```

安装依赖需要网络；扫描本身离线运行。首次提交前必须准备实例目录中的 `private-denylist.txt`，每行一个禁止外传的真实值；名单不能进入公开仓库。名单缺失或为空时，本机检查失败。提交钩子同时检查文件和提交说明，报错不回显命中的私有值。

通用检查覆盖疑似密钥、账号与邮箱、禁止类别的文件、二进制文件和单文件大小。规则和限制见 [公开仓库的内容规则](docs/architecture/repository.md)。CI 仅运行通用规则，不能代替本机私有检查。

示例账号预留 `1000000000` 至 `1000000099`，只用于手写虚构数据；真实配置即使使用同样的格式，也必须留在实例目录。通用规则允许保留的示例邮箱域和 GitHub noreply 邮箱，私有名单仍会独立检查这些内容。

## 实现边界

SDK 契约与四层配置见 [A1 实现说明](docs/implementation/a1.md)。A2 宿主使用显式注入的时钟和后端，只在测试临时目录验证状态与升级；不会自行连接真实平台、调用模型或弹出通知。参考插件包括 echo-tool、example-behavior、keyword-retriever、desktop-notifier，验收方法见 [A2 实现说明](docs/implementation/a2.md)。

持久记忆、账本、边界流程、调度器、守护进程及本机管理 API/CLI 已有实现。A4.3 候选进一步接入在线捕获、备份恢复、StorageOwner 与重 I/O 协调，但功能测试不代替性能和独立验收。官方插件同进程运行，ctx 是能力接口裁剪，不能作为运行任意不可信 Python 代码的安全沙箱。WebUI、看图与形象识别尚不在当前交付范围；单独记忆包的资产导入继续预留。

## 许可证

[MIT](LICENSE)。

## A4.3 备份与维护（已有实现候选，最终验收未完成）

实现依据见 [backup-spec.md](docs/architecture/backup-spec.md)。在线备份不停止聊天，允许有界写入协调，不承诺零资源影响。实例 backup 配置默认开启，记忆重要修改合并窗口为 5 分钟，每日实例备份默认当地时间 03:00。

常规自动备份目标默认 20 GiB，不是硬磁盘上限：开始前未超额时允许新包使其超额，下次成功生成并校验后才删除最早的可删除旧包。高频记忆最近十二份与日周月层取并集，最低有效点及恢复引用始终受保护；手动包、特殊快照、失败现场和旧实例不被常规预算顺手删除。配置字段、范围及完整示例语义以规格 §6、§12 为准。

实例备份密钥须另外保管，不进入主包或副本；确认保管不等于验证外部副本。完整恢复到新空目标，经校验、明确源停用和用户切换后才允许正常启动，不自动上线或重放任务。旧实例持续登记，默认不删，清理须计划、确认并核验独有数据、密钥和引用。

候选中的维护与备份入口已接线，但尚不可作为已验收的正式备份保障。Windows 本机功能验证、Linux 验证、GitHub 双平台 CI、正式性能和独立审查分别记录；不能把本机通过写成 CI 或里程碑通过。Issue #72、性能证据及后续审查仍待闭合，当前不启动真实部署。
