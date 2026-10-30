# Clytie · 协作约定

Clytie 是一个可复用的 AI 伴侣框架：**本体 + 插件**。本体提供事件、账本、记忆、边界、插件宿主和守护进程；能力、行为、平台、模型等一切可替换的功能都以插件形式接入。同一套代码可以运行多个伴侣，每个伴侣有自己的人设、记忆、模型和插件组合。

本仓库是**公开的通用框架**，不包含任何具体部署的信息。本文件只写现行规则和文档索引；设计细节在 `docs/`，决策记录在 `docs/adr/`。

---

## 0. 部署专属规则

如果环境变量 `CLYTIE_HOME` 指向的目录中存在 `AGENTS.local.md`，**开始工作前必须先读它**。它包含具体部署的伴侣身份、测试授权、旧系统迁移等私有规则，优先级与本文件相同；两者冲突时停下来报告，不要自行取舍。

私有文件中的任何内容（名字、号码、称呼、聊天、人设）**都不能写进本仓库**，包括代码注释、测试、文档、提交说明和 PR 描述。

## 1. 分工

| 角色 | 职责 |
|---|---|
| 用户 | 决定需求、优先级和所有上线操作；合并 PR |
| Claude | 架构设计、设计文档、验收标准；验收每个里程碑；不改代码 |
| GPT | 按文档实现代码、测试和实现说明；发现文档与现实冲突时先报告，不自行改设计 |

设计文档有歧义或缺失时，GPT 在 PR 描述中列出问题，不要猜。

## 2. 开始工作前要读

1. 本文件，以及 `$CLYTIE_HOME/AGENTS.local.md`（如果存在）
2. `docs/roadmap.md`：当前里程碑的范围和验收标准
3. `docs/architecture/` 下的全部文档
4. 与任务相关的设计文档（见第 6 节）

## 3. 硬性规则

### 3.1 公开仓库的内容
- 仓库只放通用代码、测试、文档和示例。规则见 `docs/architecture/repository.md`。
- 代码、测试、文档中只用逻辑名称（`owner`、`companion_a`、`test_group`）和明显虚构的数据。
- 测试数据只能手写假数据；不能把真实聊天、事件、记忆"脱敏"后放进仓库。
- 提交前必须通过 pre-commit 检查；CI 中再检查一次。提交使用 GitHub noreply 邮箱。
- 发现误提交时立即停止推送并报告用户；不自行改写历史或强制推送。

### 3.2 线上操作
- 未经用户本次明确同意，不连接真实平台账号、不发送消息、不调用线上模型账号。
- 真实测试只发到部署配置中指定的测试会话，并且必须先通过配置中的成员检查；检查失败就不发。
- 不在线上模型账号上跑批量基准或诊断；不为确认"是否空闲"而调用模型。
- 不悄悄更换模型、降低质量或改变费用方案。后端不可用时如实报告。

### 3.3 数据安全
- 禁止批量删除。不使用 `rm -rf`、`Remove-Item -Recurse`、`rd /s`、通配符删除；需要删除时一次删除一个明确路径，大量删除交给用户手动完成。
- 不能用空库或种子数据覆盖记忆库。导入、恢复、迁移之前必须先自动快照。
- 账本：保留收据、防重、取消和恢复；**不重放旧任务**；发送结果未知的消息不补发。

### 3.4 架构边界
- 插件只能 import `clytie.sdk`，不能 import 本体内部模块或其他插件；插件之间只通过声明的服务和事件交互。
- 边界保护、隐私投影、出站检查属于本体，不能做成插件，不能被停用。
- 新功能优先通过已有扩展点实现；确实需要新扩展点时，先修改架构文档并经验收，再改 SDK。
- WebUI 和 CLI 只调用 API；业务逻辑不写在界面里。
- 借鉴 MaiBot（GPL-3.0）只借鉴思路：不打开它的源码，不复制代码或提示词原文。

### 3.5 人设底线
人设可以沉浸，不必主动承认是 AI；但**任何时候都不声称自己是真人**，有人认真需要确认时如实说明。这条由本体保证，人设文件不能覆盖。

## 4. 工作流程

- 每个里程碑一个分支（如 `m1-sdk`），通过 PR 合并到 `main`。`main` 受保护：只能经 PR 合并、CI 必须通过；Claude 验收后由用户合并，合并后打 tag。
- PR 描述按 `docs/roadmap.md` 的验收清单逐项给出证据；没做到的如实写明。
- 问题登记到 GitHub Issues（不含私人信息；私有问题记在 `$CLYTIE_HOME` 的私有文档中）。
- 设计变更：先改文档（Claude 起草、用户确认），再改代码。重要决定写 ADR。

### 自动验收

- 每次推送 PR 更新、且 CI 通过后，由 GPT 将 `gh pr view <编号> --json body,title,commits,headRefOid`、`gh pr diff <编号>`、`gh pr checks <编号>` 的输出分别保存到 `.cache/auto-review/pr-<编号>/view.json`、`diff.patch`、`checks.txt`，再运行 `claude -p` 验收（命令模板见下）。每轮刷新这些文件，确认对应本轮提交；Claude 只读这些快照，不使用命令行工具。
- Claude 只把验收结论输出到命令行，不直接在 GitHub 上发布任何内容。由 GPT 先用通用扫描和私有名单扫描检查输出内容，全部通过后再用 `gh pr comment` 发到 PR；扫描命中时不发布，报告用户。
- 默认不给 Claude 实例目录的访问权限。自动验收是第 0 节的例外：验收时默认不读取 AGENTS.local.md。确实需要部署专属规则时，必须先报告用户；用户同意后才加上 `--add-dir`，并且输出同样要先扫描再发布。
- 结论为“需要修改”时，按意见修改后再次验收。同一个 PR 最多自动验收 3 轮；第 3 轮仍未通过就停下来，报告用户。
- 结论为“需要用户决定”时，立即停下来报告用户，不要自行选择。
- 实现中遇到规格没写清的技术细节，也可以用 `claude -p` 询问（同样只读）。只有不改变任何已定规则的技术细节，才能按 Claude 的答复补进规格，并在 PR 描述中列出。这是第 1 节、第 4 节中“设计变更须用户确认”的例外，仅限不改变已定规则的技术细节。涉及需求、已定规则、隐私、线上操作的问题，必须报告用户。
- 合并、打 tag、修改仓库可见性和设置，只能由用户完成（用户明确指示时可代为打 tag）。Claude 的“通过”不等于可以合并。
- 仓库转为公开之前，除了提交历史，还要扫描所有 PR 和 Issue 的正文和评论，把结果交给用户确认。

命令模板（只读，不能发布、编辑、提交、推送、合并）：

```shell
claude -p "<验收指令>" --allowedTools "Read(./**)" --disallowedTools "Bash" --permission-mode dontAsk --permission-prompts none --safe-mode --strict-mcp-config --no-session-persistence --output-format stream-json --verbose
```

验收指令模板：

```text
验收 Clytie 的 PR #<编号>。先读 AGENTS.md、docs/roadmap.md 中对应里程碑的验收清单，以及 docs/architecture/ 下的规格。从 .cache/auto-review/pr-<编号>/ 的 view.json、diff.patch、checks.txt 读取 PR 信息，不使用命令行。只读，不修改任何文件。不访问仓库目录以外的文件；被 git 忽略的文件仅允许读取本轮上述 PR 信息快照，不读取其他忽略文件；不需要部署专属规则。搜索和读取都只限于仓库目录内。逐项判断验收清单，最后给出结论：通过 / 需要修改（列出具体问题）/ 需要用户决定（列出问题）。
```

不对应里程碑的 PR 按 `AGENTS.md` 的现行规则检查。

## 5. 技术约定

- Python（版本以本机实际可用的为准，在 `pyproject.toml` 中固定下限），src 布局，包名 `clytie`。
- pytest 只扫描 `tests/` 和 `plugins/*/tests/`；代码检查用 ruff。
- 离线测试使用假模型、假平台和模拟时钟，不访问网络。
- 文档用中文；代码标识符、文件名、日志字段用英文。
- 路径一律相对于仓库根目录或 `CLYTIE_HOME`，不写死绝对路径。

## 6. 文档索引

| 文档 | 内容 |
|---|---|
| `docs/roadmap.md` | 阶段、里程碑、验收清单 |
| `docs/architecture/overview.md` | 设计原则、分层、本体模块、消息流程、仓库目录 |
| `docs/architecture/sdk.md` | 插件 SDK：接口、数据类型、事件、稳定性与版本策略 |
| `docs/architecture/sdk-spec.md` | SDK 实现规格：字段、取消与时钟协议、扩展点签名、合并规则 |
| `docs/architecture/config-spec.md` | 配置文件、Schema、分层合并、错误报告、A1 的 status 命令 |
| `docs/architecture/plugins.md` | 插件包、清单、扩展点、权限、依赖、生命周期、创意工坊格式 |
| `docs/architecture/plugin-host-spec.md` | A2 宿主内部计划、装配、持久化和索引技术约定 |
| `docs/architecture/kernel-spec.md` | A3.1 账本与交付：状态、防重、重发、取消、调度、查询与场景过滤 |
| `docs/architecture/agent-spec.md` | A3.2 对话主干：工具循环、上下文、能力状态、边界、出站检查 |
| `docs/architecture/memory-spec.md` | A3.3 记忆核心：当场记、投影、检索、加密导出与搬家导入 |
| `docs/architecture/memory-recall-spec.md` | A3.4 回想与印象：回想窗口、出处核对、群友印象、软遗忘与黑话 |
| `docs/architecture/data-and-backup.md` | 实例目录、记忆导入导出、备份与保留策略 |
| `docs/architecture/repository.md` | 公开仓库的内容规则与自动检查 |
| `docs/design/kernel.md` | 统一账本、防重、交付、任务编号、表情回应规则、日志 |
| `docs/design/memory.md` | 记忆模型、可见等级、检索、经历、档案、软遗忘 |
| `docs/design/conversation.md` | 上下文预算、人设分层、群聊行为、多伴侣协作 |
| `docs/design/boundary.md` | 权限、关系边界保护、保障模式、过滤与出站检查 |
| `docs/design/supervisor.md` | 守护进程 |
| `docs/design/webui.md` | WebUI |
| `docs/design/affect.md` | 情感模块 |
| `docs/design/lessons.md` | 必须防止复发的设计问题 |
| `docs/plugins/catalog.md` | 预设的官方插件（可调整） |
| `docs/plugins/sleep.md` | 睡眠作息插件（行为插件的参考设计） |
| `docs/adr/` | 决策记录 |
