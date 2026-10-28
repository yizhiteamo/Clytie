# Clytie

Clytie 是可复用的 AI 伴侣框架：本体提供事件、账本、记忆、边界、插件宿主和守护进程；模型、平台、能力和行为通过插件接入。每个伴侣使用独立的配置、人设与记忆。

项目当前实现 A1（工程与 SDK）：不可变 SDK 数据、取消和时钟协议、可复用契约测试、假模型与假平台、配置校验和离线状态命令。范围与验收标准见 [路线图](docs/roadmap.md)，架构入口见 [架构总览](docs/architecture/overview.md)。本体执行、真实平台与模型接入属于后续里程碑。

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
```

`status` 只读配置、插件清单和人设文件存在状态，不导入插件、不读取人设内容、不连接平台或模型。退出码为：有效 `0`，配置错误 `1`，实例目录不存在 `2`。实例目录优先级为 `--home`、`CLYTIE_HOME`、仓库同级目录。

SDK 规格见 [sdk-spec.md](docs/architecture/sdk-spec.md)，配置格式见 [config-spec.md](docs/architecture/config-spec.md)。插件作者可使用 `clytie.sdk.testing` 中的假实现、`MockClock` 和 `assert_*_contract` 函数；完整样例见 [fake-model](plugins/fake-model/README.md) 与 [fake-channel](plugins/fake-channel/README.md)。

## 开发约定

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

## A1 实现边界

已定义协议的契约测试与假实现可离线运行。插件宿主、真实权限裁剪、隐私投影、数据库、网络管理 API 尚未实现。规格仍未明确的注册方法、事件负载及部分配置字段没有自行补设计，详见 [A1 实现说明](docs/implementation/a1.md)。

## 许可证

[MIT](LICENSE)。
