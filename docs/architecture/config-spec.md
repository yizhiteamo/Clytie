# 配置与 Schema 规格 v0.1

## 1. 文件

| 文件 | Schema | 位置 |
|---|---|---|
| 实例配置 | `schemas/instance.schema.json` | `CLYTIE_HOME/clytie.local.yaml` |
| 伴侣配置 | `schemas/companion.schema.json` | `CLYTIE_HOME/companions/<id>/companion.yaml` |
| 插件清单 | `schemas/plugin.schema.json` | `plugins/<id>/plugin.json` |

- Schema 使用 JSON Schema draft 2020-12。
- 每个文件顶层必须有 `schema_version`（当前为 `1`）。版本不认识时拒绝加载并提示。
- 未知字段报错（`additionalProperties: false`），防止拼写错误被静默忽略；以 `x-` 开头的字段允许，留给用户写注释。

## 2. 实例配置 `clytie.local.yaml`

| 字段 | 必填 | 说明 |
|---|---|---|
| `schema_version` | 是 | `1` |
| `timezone` | 是 | IANA 时区，如 `Asia/Shanghai` |
| `people` | 是 | `{actor_id: {role: owner|member, accounts: [{channel, account}]}}`；必须恰好有一个 `owner` |
| `accounts` | 否 | 伴侣自己使用的平台账号：`{account_ref: {channel: 插件 id, account: 平台账号, secret?: 相对 secrets/ 的路径}}`；伴侣配置的 `channels[].account` 引用这里的键 |
| `conversations` | 否 | `{conversation_id: {channel, platform_id, kind, scene, members_expected?: [actor_id]}}`；`members_expected` 用于测试会话的成员检查 |
| `scenes` | 是 | 场景名列表，必须包含 `owner_private`、`public_group`、`stranger_private` |
| `test_conversation` | 否 | 一个 `conversation_id`；只有它可以接收真实测试 |
| `companions` | 是 | 伴侣 id 列表；每个 id 对应 `companions/<id>/companion.yaml` |
| `plugins` | 否 | `{plugin_id: {config: {...}}}`：实例级的插件配置（所有伴侣共用的默认值） |
| `api` | 否 | `{port: int}`；只监听 127.0.0.1，不提供修改地址的选项 |
| `paths` | 否 | `{name: 相对或绝对路径}`：导入器等需要的外部路径（例如旧系统数据位置） |

平台账号（`account`、`platform_id`）在这里是真实值，这个文件只存在于实例目录。

## 3. 伴侣配置 `companion.yaml`

| 字段 | 必填 | 说明 |
|---|---|---|
| `schema_version` | 是 | `1` |
| `id` | 是 | 与目录名一致 |
| `names` | 是 | `{internal: str, external: {scene: str}?}`：对内名字、各场景的对外显示名 |
| `forbidden_names` | 否 | 对外场景中不允许出现的名字（出站检查使用），默认包含 `internal` |
| `model` | 是 | `{plugin: id, config: {...}}` |
| `channels` | 是 | `[{plugin: id, account: 账号引用, config: {...}}]` |
| `persona` | 是 | `{files: {layer: 相对 persona/ 的路径}, scenes: {scene: [layer, ...]}}`；每个已声明的场景都必须有映射 |
| `permissions` | 是 | `{role: [tool 名称]}`：每种身份能用的能力；`owner` 可以写 `"*"` |
| `plugins` | 否 | `{plugin_id: {enabled: bool, priority?: int, config: {...}}}` |
| `task_card` | 否 | 任务单模板，按场景：`{internal: str, external: str}`；可用变量 `{label} {id} {name} {helper} {duration}` |
| `boundary` | 否 | 关系边界设置，子字段见下 |
| `context` | 否 | 上下文预算，子字段见下 |

**`channels[].account`**：实例配置 `accounts` 中的一个键。校验：键必须存在；对应条目的 `channel` 必须等于 `channels[].plugin`；同一个 `account_ref` 不能被两个伴侣同时使用。`secret` 只检查文件是否存在，不读取内容。

**`boundary`**（全部可选，括号内为默认值）：
| 字段 | 类型 |
|---|---|
| `refusal_threshold` | 整数 1–100（5） |
| `window_hours` | 整数 1–168（24） |
| `categories` | `{romance, persona_change, scripted_intimacy, intimate_images, owner_privacy}`，各为布尔值（全部 `true`） |
| `never_publish` | 字符串列表（空）：对外场景中不允许出现的词句 |
| `notify_owner` | 布尔值（`true`） |

**`context`**（全部可选，括号内为默认值）：
| 字段 | 类型 |
|---|---|
| `max_messages` | 整数 1–1000（100） |
| `max_chars` | 整数 1,000–200,000（20,000） |
| `max_single_message_chars` | 整数 100–20,000（2000） |
| `material_full_minutes` | 整数 0–1440（15） |
| `material_budget_chars` | 整数 0–100,000（6,000） |

## 4. 插件清单 `plugin.json`

字段含义见 `plugins.md` 第 4 节。结构：

```json
{
  "schema_version": 1,
  "id": "example-behavior",
  "name": "示例行为",
  "summary": "……",
  "category": "behavior",
  "version": "0.1.0",
  "requires": {"sdk": ">=0.1,<0.2"},
  "dependencies": {"other-plugin": {"version": ">=1.0", "optional": false}},
  "conflicts": [],
  "entry": "plugin:Plugin",
  "priority": 100,
  "contributes": {
    "tools": [],
    "behavior": {"before_attention": {"handler": "on_before_attention", "timeout_s": 2}},
    "context_section": {"handler": "contribute", "budget_chars": 300},
    "schedule": [{"id": "tick", "every": "PT1M", "handler": "on_tick"}],
    "services": [{"name": "example.state", "version": "1.0", "methods": ["get"]}],
    "events": {"subscribe": ["task.completed"], "publish": ["example.changed"]}
  },
  "permissions": [],
  "config": {"type": "object", "properties": {}, "x-ui": {}},
  "data": {"version": 1, "on_uninstall": "keep"},
  "failure_default": {"before_attention": null}
}
```

- 版本范围使用 PEP 440 写法。
- `entry` 为 `模块:类`，模块相对插件目录。
- `config` 是插件配置的 JSON Schema，`default` 值即插件默认配置；`x-ui` 提供标签、单位、分组等界面提示。

## 5. 配置分层与合并

优先级从低到高：

1. 插件清单 `config` 中的 `default`
2. 实例配置 `plugins.<id>.config`
3. 伴侣配置 `plugins.<id>.config`

作为模型或平台使用的插件多一层（最高）：

4. 伴侣配置 `model.config`（模型插件）或 `channels[i].config`（平台插件，每个账号一份）

也就是：清单默认 < 实例 `plugins.<id>.config` < 伴侣 `plugins.<id>.config` < `model.config` / `channels[i].config`。

被 `model` 或 `channels` 引用的插件视为已启用；如果同时在 `plugins.<id>` 中写了 `enabled: false`，报错。

同一个插件 id 同时出现在仓库 `plugins/` 和 `CLYTIE_HOME/plugins/` 中时报错（不允许用安装的插件覆盖官方插件）。

规则：
- 对象逐键深度合并；列表和标量整体替换（高层覆盖低层）。
- 不支持用 `null` 删除低层的值；需要"关闭"时用明确的字段（如 `enabled: false`）。
- 合并后用插件的 `config` Schema 校验。
- 合并结果只读，插件不能修改。

## 6. 错误报告

所有配置错误都要给出：文件路径、JSON 指针（如 `/plugins/sleep/config/bedtime`）、期望值、实际值。一次报告所有错误，不在第一个错误处停止。

## 7. A1 的 `python -m clytie status`

用途：离线检查实例目录是否可用。

```
python -m clytie status [--home PATH] [--json]
```

- 实例目录：`--home` > 环境变量 `CLYTIE_HOME` > 仓库同级的 `Clytie-home/`。
- 检查内容：实例配置和每个伴侣配置的 Schema 校验；伴侣引用的插件是否能找到（仓库 `plugins/` 和 `CLYTIE_HOME/plugins/`）及其清单是否有效；人设文件是否存在；场景映射是否完整。
- 输出：每个伴侣一段：配置是否有效、模型插件、平台插件、启用的插件（找到 / 缺失 / 清单无效）、人设文件状态；最后列出全部错误。`--json` 输出同样的内容。
- 退出码：`0` 全部有效；`1` 存在错误；`2` 找不到实例目录。
- **边界**：不连接任何平台、不启动任何进程、不调用模型、不访问网络；不打开或创建数据库；对 `secrets/` 只检查文件是否存在，不读取内容；不修改实例目录中的任何文件。依赖解析和版本冲突检查属于 A2，A1 不做。
