# Clytie

Clytie 是可复用的 AI 伴侣框架：本体提供事件、账本、记忆、边界、插件宿主和守护进程；模型、平台、能力和行为通过插件接入。每个伴侣使用独立的配置、人设与记忆。

项目当前处于 A1（工程与 SDK）的准备阶段。此初始化提交包含通用设计和提交检查，尚不提供运行时服务。范围与验收标准见 [路线图](docs/roadmap.md)，架构入口见 [架构总览](docs/architecture/overview.md)。

## 开发约定

开始前阅读 [AGENTS.md](AGENTS.md)，以及实例目录中存在的 `AGENTS.local.md`。实例目录由 `CLYTIE_HOME` 指定，未设置时为仓库同级的 `Clytie-home/`。部署配置、真实身份、凭据、人设、聊天和记忆都留在实例目录。

测试只使用手写的虚构数据、假模型、假平台和模拟时钟。实现按里程碑分支提交 PR；由设计验收和用户合并完成交付。

## 提交检查

本机 Python 虚拟环境中安装固定版本的检查工具。以下为 PowerShell 示例（其他系统使用对应的虚拟环境激活命令）：

```text
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install pre-commit==4.3.0
python -m pre_commit install --install-hooks
python -m pre_commit run --all-files
```

安装依赖需要网络；扫描本身离线运行。首次提交前必须准备实例目录中的 `private-denylist.txt`，每行一个禁止外传的真实值；名单不能进入公开仓库。名单缺失或为空时，本机检查失败。提交钩子同时检查文件和提交说明，报错不回显命中的私有值。

通用检查覆盖疑似密钥、账号与邮箱、禁止类别的文件、二进制文件和单文件大小。规则和限制见 [公开仓库的内容规则](docs/architecture/repository.md)。CI 仅运行通用规则，不能代替本机私有检查。

示例账号预留 `1000000000` 至 `1000000099`，只用于手写虚构数据；真实配置即使使用同样的格式，也必须留在实例目录。通用规则允许保留的示例邮箱域和 GitHub noreply 邮箱，私有名单仍会独立检查这些内容。

## 许可证

[MIT](LICENSE)。
