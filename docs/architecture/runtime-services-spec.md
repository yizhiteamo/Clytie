# 阶段二运行时服务规格：凭据与生产网络

状态：用户已确认（2026-11-15，Issue #83，B1 前置）。

能力插件、平台插件和模型插件要接入真实服务，本体需要提供：读取凭据、真实的网络访问。本文定义它们的最小契约。第 2 节已撤回。B1 实现第 1、3 节；第 1.5 节和第 4 节由 B2 实现。

## 1. 插件凭据（ADR 0031）

### 1.1 配置

实例配置 `clytie.local.yaml` 增加可选的 `credentials`：

`credentials: {ref: {secret, plugins}}`

| 字段 | 说明 |
|---|---|
| `ref` | 凭据引用名，标识符格式，例如 `image_api_test` |
| `secret` | 密钥文件相对 `secrets/` 的路径 |
| `plugins` | 允许读取的插件 id 列表，至少一个，例如 `[image-gen]` |

- 路径校验与 `accounts.*.secret` 相同：必须位于 `secrets/` 之内，不能是符号链接，文件必须存在。启动校验只检查文件是否存在，不读取内容。
- `plugins` 中的 id 必须是已安装的插件。
- 插件在自己的配置里用普通字符串字段写引用名，例如 `api_key_ref: image_api_test`。本体不解析插件配置里的引用，是否允许读取只看 `credentials.<ref>.plugins`。
- 密钥文件是 UTF-8 文本，内容就是密钥本身。读取时去掉首尾空白。

### 1.2 SDK 接口

`ctx.credentials`：有任何凭据的 `plugins` 列出本插件时才出现。不新增权限名。

```
async read(ref: str) -> str
```

- 每次调用时读取文件，不缓存，所以更换密钥后不用重启。
- 失败时抛 `CredentialUnavailable(code)`，`code` 取 `unknown_ref`、`not_permitted`、`missing`、`empty`、`unreadable` 之一。异常信息只含引用名和错误码，不含文件路径和文件内容。
- `CredentialUnavailable` 从 `clytie.sdk` 导出。

### 1.3 不泄露

- 宿主不记录凭据内容，也不把它写进日志、账本、审计或 API 响应。
- 插件不得把凭据写进日志、`ToolResult`、错误信息、存储或资产。插件测试用一个虚构的标记值，断言它没有出现在日志、账本和返回结果中。
- 本地 API 和 `status` 显示凭据时，只显示引用名、允许的插件和"文件存在/不存在"。

### 1.4 备份与恢复

`credentials.*.secret` 和 `accounts.*.secret` 一样属于文件引用：密钥文件不进入备份包，恢复后列为"待配置"（backup-spec §8.3a 的同一套规则）。`backup/credentials.py` 的排除清单增加这一类，kind 为 `file_reference`。

### 1.5 平台账号的密钥（B2 实现）

平台插件读取自己绑定账号的 `accounts.<ref>.secret`：`ctx.credentials.account_secret() -> str`，只对实现了 channel 扩展点的插件出现，只能读取伴侣配置里 `channels[].account` 绑定的那个账号。错误码与 1.2 相同；未绑定账号时为 `unknown_ref`。

绑定的是哪个平台账号，用 `ctx.identity.self_account()` 读取（ADR 0032），不从协议端的报告或插件配置中获取。

## 2. 工具每日额度（已撤回，用户决定 2026-11-15）

本体不做工具次数计数，不提供 `tool_quotas`。原因：
- 费用上限由平台侧设置，比如 OpenAI 项目的预算上限。超出后平台直接拒绝，Clytie 出任何错误都绕不过去。
- 防止模型反复调用，靠对话主干已有的工具轮数限制。
- 使用 coding plan 的 `codex-cli` 应该能用满全部额度；本体只保留现有的保障模式。

插件收到平台的额度用完错误时，如实映射为 `quota_exceeded`，在 `ToolResult` 中告诉模型，由伴侣如实转告，不改用其他后端或模型（配置了回退的情况除外，见 roadmap B1）。

"伴侣知道自己还剩多少额度、快用完时主动提醒 owner"另立里程碑，见 roadmap「额度感知」。

## 3. 生产 HTTP 后端

### 3.1 接线

宿主默认的后端解析（`plugin_host/host.py` 的 `resolve_backends`）为声明了 `network` 权限的插件提供真实的 `http` 后端。测试仍可以通过 `backends` 参数注入假后端。现有 `context.Http` 的超时和大小限制保持不变，真实后端放在它下面。

### 3.2 行为

- 使用现有依赖 aiohttp，不增加新依赖。每个插件实例一个会话，插件停止时关闭。
- 只允许 `http` 和 `https`。校验 TLS 证书，不提供关闭校验的选项。
- 最多跟随 3 次重定向；不允许从 `https` 重定向到 `http`。
- 返回内容超过 `max_bytes` 时，边读边中止，不先完整读进内存。
- 不读取系统代理环境变量，本阶段不提供代理配置（用户确认当前环境不需要）。以后需要时另行补充。
- 日志只记录方法、主机名、状态码和耗时，不记录路径参数、请求头和正文。

### 3.3 测试

用本机 `127.0.0.1` 上的测试服务器覆盖：正常请求、超时、超大返回、重定向次数和协议降级。离线测试不访问外网。

## 4. 生产 WebSocket 后端（B2 实现）

ADR 0030 的 `ctx.websocket` 按第 3 节的同一套原则接线：默认后端由宿主提供；只允许 `ws` 和 `wss`；校验 TLS；插件停止时关闭全部连接；日志只记录主机名、连接时长和关闭码。

## 5. 验收（B1 部分）

- 凭据：读取成功、五种错误码、未列入 `plugins` 的插件看不到 `ctx.credentials`、虚构标记值不出现在日志和账本中、备份排除清单包含 `credentials` 的文件引用，各有测试。
- HTTP：第 3.3 节的各项都有测试；默认启动路径能为声明了 `network` 的插件装配出真实后端。
