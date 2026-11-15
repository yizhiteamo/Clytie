# ADR 0031：插件凭据

状态：用户已确认（2026-11-15，Issue #83）。

## 问题

能力插件和模型插件要调用付费接口，需要 API 密钥。现有的 `accounts.*.secret` 只服务平台账号，而且宿主只检查文件是否存在，不提供读取接口。插件配置里虽然可以用 `x-clytie-secret` 声明内联凭据，但这样密钥会写进 YAML，配置报错时还可能带出实际值。

## 决定

1. 实例配置新增 `credentials: {ref: {secret, plugins}}`。密钥只放在 `secrets/` 下的文件里，不写进任何 YAML。
2. 插件通过 `ctx.credentials.read(ref)` 读取。能不能读，只看 `credentials.<ref>.plugins` 是否列出了这个插件。不新增权限名。
3. 每次调用都重新读取文件，换密钥不用重启。
4. 错误统一抛 `CredentialUnavailable(code)`，信息里不含密钥内容。
5. 备份时密钥文件和 `accounts` 的 secret 一样被排除，恢复后列为待配置。
6. 平台插件读取自己绑定账号的 secret，用 `ctx.credentials.account_secret()`（B2 实现）。

细节见 `architecture/runtime-services-spec.md` 第 1 节。

## 不选的方案

- **插件配置内联 `x-clytie-secret`**：密钥会进入 YAML 和配置报错信息。这个机制保留给已有用途，新的 API 密钥不使用它。
- **复用 `accounts`**：`accounts` 的语义是"伴侣自己使用的平台账号"，要求填 `channel`，而且一个账号不能被两个伴侣同时使用。API 密钥可能由多个伴侣共用，硬塞进去会改变现有校验规则。

## 影响

- `schemas/instance.schema.json` 增加 `credentials`；`schemas/companion.schema.json` 增加 `tool_quotas`。
- SDK 次版本号升级：新增 `ctx.credentials` 和 `CredentialUnavailable`。
- `status` 和本地 API 只显示引用名、允许的插件和文件是否存在。
