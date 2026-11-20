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
| `limits.max_task_seconds` | 否 | 任务截止时间上限，整数，默认 3600 秒；范围 60–86,400（见 `kernel-spec.md` 第 16 节） |

平台账号（`account`、`platform_id`）在这里是真实值，这个文件只存在于实例目录。

### A4.2 本机管理接口

全局 `api` 字段均可省略（`api-spec.md` §8）：

| 字段 | 类型（默认值） |
|---|---|
| `api.port` | 整数，1024 起的有效 TCP 端口号（8765） |
| `api.session_idle_days` | 整数 1–365（30） |
| `api.reauth_minutes` | 整数 1–1440（15） |
| `api.login_lockout.failures` | 整数 3–20（5） |
| `api.login_lockout.seconds` | 整数 10–3600（60） |

只绑定 127.0.0.1，不提供监听地址配置。未明确配置端口时，从默认端口起依次尝试后面的 9 个端口；明确配置时只使用该端口。实际端口写入 run/api.json 并在 status 显示。

## 3. 伴侣配置 `companion.yaml`

| 字段 | 必填 | 说明 |
|---|---|---|
| `schema_version` | 是 | `1` |
| `id` | 是 | 与目录名一致 |
| `names` | 是 | `{internal: str, external: {scene: str}?, internal_scenes: [scene]?}`：对内名字、各场景的对外显示名；对内场景默认 `[owner_private]` |
| `forbidden_names` | 否 | 对外场景中不允许出现的名字（出站检查使用），默认包含 `internal` |
| `model` | 是 | `{plugin: id, config: {...}}` |
| `channels` | 是 | `[{plugin: id, account: 账号引用, config: {...}}]` |
| `persona` | 是 | `{files: {layer: 相对 persona/ 的路径}, scenes: {scene: [layer, ...]}}`；每个已声明的场景都必须有映射 |
| `permissions` | 是 | `{role: [tool 名称]}`：每种身份能用的能力；`owner` 可以写 `"*"` |
| `plugins` | 否 | `{plugin_id: {enabled: bool, priority?: int, config: {...}}}` |
| `task_card` | 否 | 任务单模板，按场景：`{internal: str, external: str}`；另有 `label` 子字段（见下表）；可用变量 `{label} {id} {name} {helper} {duration}` |
| `boundary` | 否 | 关系边界设置，子字段见下 |
| `context` | 否 | 上下文预算，子字段见下 |
| `memory` | 否 | 记忆设置，子字段见下 |
| `delivery` | 否 | 交付重发设置，子字段见下 |
| `tasks` | 否 | 任务取消、截止时间与并发设置，子字段见下 |
| `ledger` | 否 | 账本内容保留设置，子字段见下 |
| `task_reactions` | 否 | 任务状态回应设置（kernel-spec §13.5），子字段见下 |

**A3.2 新配置**（按 `agent-spec.md` 第 14 节同步）：

| 字段 | 类型 | 默认值 | 范围 |
|---|---|---|---|
| `names.internal_scenes` | 场景名列表 | `[owner_private]` | 见下方校验规则 |
| `agent.max_tool_rounds` | 整数 | 4 | 1–8 |
| `agent.model_call_seconds` | 整数 | 120 | 10–600 |
| `replies.fallback_private` | 字符串 | `"刚刚没接上，可以再说一次吗？"` | 0–200 字；空字符串表示不发 |
| `tasks.cancel_words` | 字符串列表 | `["取消", "停", "停止", "停下", "别做了"]` | 1–20 项，每项 1–10 字 |
| `boundary.pause_hours` | 整数 | 24 | 1–168 |
| `boundary.input_filter.words` | 字符串列表 | 空 | 每项 1–100 字 |
| `boundary.input_filter.patterns` | 正则列表 | 空 | 每项不超过 200 字符，必须能编译 |
| `outbound.style_words` | 字符串列表 | 空 | 每项 1–50 字；追加到本体内置清单 |
| `protection.enter_ratio` | 数字 | 0.2 | 0–1 |
| `protection.exit_ratio` | 数字 | 0.3 | 0–1，必须大于 `enter_ratio` |

`names.internal_scenes` 只决定名字和称呼。不在其中的场景都是对外场景。校验规则与 `memory.private_scenes` 相同：`public_group`、`stranger_private` 不能加入；加入的其他场景，映射到它的每个会话都必须声明 `members_expected`，且成员只能是主人和本部署的伴侣。`memory.private_scenes` 必须是 `internal_scenes` 的子集，否则配置无效。私密记忆、人设私密段落、账本内容的可见范围仍由 `memory.private_scenes` 决定，不受这个开关影响（`agent-spec.md` §9.1）。

**A3.1 新配置**（按 `kernel-spec.md` 第 16 节同步）：

| 字段 | 类型 | 默认值 | 范围 |
|---|---|---|---|
| `delivery.retry.max_attempts` | 整数 | 3 | 1–5，包括首发 |
| `delivery.retry.backoff_seconds` | 数字列表 | `[2, 5]` | 每项 0.5–60；长度 0–4；比重发次数短时用最后一项；为空时立即重发 |
| `delivery.retry.window_seconds` | 整数 | 60 | 10–600 |
| `delivery.group_max_seconds` | 整数 | 60 | 10–300 |
| `tasks.cancel_grace_seconds` | 整数 | 5 | 1–60 |
| `tasks.default_deadline_seconds` | 整数 | 600 | 10 到 `limits.max_task_seconds`；超过上限时配置校验失败 |
| `tasks.max_concurrent` | 整数 | 2 | 1–8 |
| `ledger.content_retention_days` | 整数或 `null` | `null`（不限期） | 不小于 1 |
| `task_card.label` | `{internal: str, external: str}` | 都是 `"任务"` | 非空 |
| `task_reactions.enabled` | 布尔 | `true` | 设为 `false` 时全部关闭 |
| `task_reactions.audience` | `all` / `owner` | `all` | 所有有权限发起任务的人 / 只有主人的委托 |
| `task_reactions.emojis` | `{accepted, running, done, failed}` | 👌、⏳、✅、❌ | 各为非空字符串，由平台插件映射为平台表情 |
| `task_reactions.min_interval_seconds` | 整数 | 2 | 0–30 |

实例 `limits.max_task_seconds` 见第 2 节。

**`channels[].account`**：实例配置 `accounts` 中的一个键。校验：键必须存在；对应条目的 `channel` 必须等于 `channels[].plugin`；同一个 `account_ref` 不能被两个伴侣同时使用。`secret` 只检查文件是否存在，不读取内容。

**`boundary`**（全部可选，括号内为默认值）：
| 字段 | 类型 |
|---|---|
| `refusal_threshold` | 整数 1–100（5） |
| `window_hours` | 整数 1–168（24） |
| `categories` | `{romance, persona_change, scripted_intimacy, intimate_images, owner_privacy}`，各为布尔值（全部 `true`） |
| `never_publish` | 字符串列表（空）：对外场景中不允许出现的词句 |
| `notify_owner` | 布尔值（`true`） |
| `admission` | `test_only` / `registered` / `owner_present`（`registered`）：哪些会话可以处理，见 admission-spec |

**`memory`**（全部可选，括号内为默认值）：
| 字段 | 类型 |
|---|---|
| `private_scenes` | 场景名列表（`[owner_private]`）：哪些场景可以读取私密记忆 |
| `core_chars` | 整数 200–50_000（4000） |
| `recall_chars` | 整数 200–50_000（3000） |
| `recall_limit` | 整数 1–50（10） |
| `semantic_min_similarity` | 数值 -1–1（0.5）；语义路仅接受余弦相似度严格大于此值的候选；离线技术默认值，真实模型选型时校准 |
| `rerank_candidates` | 整数 5–200（30） |
| `max_writes_per_record` | 整数 1–20（5） |
| `leak_min_chars` | 整数 4–50（8） |
| `embedder` | 插件 id 或 null（null：只用关键词） |
| `reranker` | 插件 id 或 null（null） |

**A3.4 `memory` 新增字段**（memory-recall-spec §8，全部可选）：

| 字段 | 类型（默认值） |
|---|---|
| `recollect.enabled` | 布尔（`true`） |
| `recollect.idle_minutes` | 整数 5–1440（30） |
| `recollect.daily_limit` | 整数 1–100（12） |
| `recollect.window_chars` | 整数 2000–100_000（12_000） |
| `recollect.max_items` | 整数 1–30（8） |
| `recollect.model` | 与伴侣 `model` 同结构的模型配置，或 `null`（`null`：跟随当前聊天模型） |
| `recollect.retracted_similarity` | 数值 0–1（0.85）；本体内部拦截与本窗口撤销内容的余弦相似度严格大于此值的候选，真实模型选型时校准 |
| `recollect.group_visibility` | `private` \| `relationship` \| `public`（`relationship`） |
| `recollect.consolidator` | 插件 id（内置默认实现） |
| `fade.half_life_days` | 数值 1–3650（30） |
| `fade.reinforce` | 数值 1–5（1.5） |
| `fade.max_half_life_days` | 数值 1–36_500（365） |
| `fade.freeze_below` | 数值 0–0.5（0.1） |

`embedder`、`reranker` 指向的插件必须已启用并实现对应扩展点，否则配置无效（memory-spec §10）。

`private_scenes` 的校验规则（不满足时配置无效）：
- 必须是 `names.internal_scenes` 的子集（`agent-spec.md` §9.1）。
- `public_group`、`stranger_private` 永远不能加入。
- 加入的其他场景，实例配置中映射到该场景的每个会话都必须声明 `members_expected`，并且成员只能是主人和本部署的伴侣。也就是说，只有"在场的都是自己人"的场景才能开启私密记忆。
- 这个设置只放宽"能读到私密记忆"，不影响出站检查：对外场景仍然拦截私密内容。

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


### Issue #39 用户确认的同步

- 印象与黑话按普通检索命中后注入，共用 recall_limit / recall_chars，不自动全量前置、不增加预算。群聊不在场的印象，普通检索和 core_recall 都不返回；对内印象按普通检索。
- owner_confirmed / owner_stated 的印象与黑话不淡化，但核心记忆排除 profile / jargon；修订升级规则不变，使用范围不变。core:with_owner 只用于群聊 episode，主人参与的黑话不因此免淡化。
- 来源会话按 memory-recall-spec §4.6 的会话登记表处理：本体写入时按配置、导入时按包清单登记；只以 channel + kind + platform_id 在当前配置对应，逻辑 ID 和群名称不参与身份判断。不改 origin_conversation_id，不产生修订、不进内容指纹。未对应时仅对内可见、按登记类别淡化；旧包和无法得知身份的非印象来源不淡化。再次导出保留登记；更换 channel 不自动对应。本地与导入同逻辑 ID、不同平台身份的记录须分别引用自己的登记。
- 停用回想立即取消进行中的工作且不写入；重新启用时所有会话进度移到现在，停用期间和未完成窗口都跳过，status 只显示跳过段数。


Issue #41（用户已确认）：conversations 中 channel、kind、platform_id 三项完全相同的重复会话校验失败，错误指出配置文件与重复会话字段；群聊与私聊 platform_id 相同时可按 kind 分别对应。逻辑 ID 和平台群名称不参与身份。

## A4.1 守护进程与通知配置

全局配置 `clytie.local.yaml` 新增以下字段，全部可选；未知字段报错，与现有规则相同：

| 字段 | 类型（默认值） |
|---|---|
| `supervisor.heartbeat_seconds` | 整数 1–60（5） |
| `supervisor.startup_seconds` | 整数 30–3600（300）；只计 starting 阶段，不计迁移 |
| `supervisor.hang_seconds` | 整数 10–600（30），必须大于心跳间隔的 2 倍 |
| `supervisor.restart.backoff_seconds` | 正整数列表，1–10 项（`[10, 30, 60, 120, 300]`） |
| `supervisor.restart.max_failures` | 整数 1–100（5） |
| `supervisor.restart.stable_seconds` | 整数 60–86,400（600） |
| `supervisor.stop.delivery_wait_seconds` | 整数 0–600（30） |
| `supervisor.stop.task_mode` | `cancel` \| `wait`（`cancel`） |
| `supervisor.stop.timeout_seconds` | 整数 10–3600（120） |
| `notify.plugins` | 插件 id 列表（空）；只能是声明了 `notifier` 的插件 |
| `notify.cooldown_hours` | 整数 1–168（6） |

插件清单的 `notifier` 新增可选字段 `outward: bool`（`false`）。

## A4.3 实例备份配置（待实现）

以下为已批准的 A4.3 契约，尚未实现、未经验收；完整依据为 `backup-spec.md`，不代表历史里程碑已提供这些能力。

| 字段 | 默认及校验 |
|---|---|
| enabled | true；关闭不删除已有备份 |
| memory.enabled / memory.debounce_seconds | true / 300，整数 1–3600 |
| schedule.at / schedule.days | 03:00 / []；HH:MM，星期范围沿用 at，空为每日；实例 timezone |
| schedule.catch_up | true |
| retention.recent_memory | 12，整数且不小于最低保留数 |
| retention.daily_days / weekly_weeks / monthly_months | 30 / 52 / null；正整数或相应层 0 禁用，monthly null 为不按月龄到期；容量仍可淘汰 |
| retention.minimum_per_group | 3，不小于 3；降低保护底线不作为普通配置开放 |
| retention.target_gib | 20，正数或 null；允许小于单包，显示不可满足而非截断 |
| disk.reserve_gib | 1，正数 |
| mirror.enabled / mirror.path | false / null；启用必须明确目录并通过仓库确认 |
| mirror.recent / mirror.monthly_months / mirror.minimum | 7 / 12 / 3；minimum 至少 3，其余不得小于保护底线的有效含义 |
| old_instances.warn_count / warn_gib | 3 / 20，正数，只影响提醒 |
| logs.rotate_mib / archives | 10 / 10，正整数，普通日志，不覆盖审计 |

配置计划显示下次执行时间、保留影响、预算范围与超额语义；调整预算不当场删文件，下一次成功自动备份按新版本计算。降低保留期限/关闭备份/修改副本目录需要确认，其他更改也审计；Schema 未知字段报错。A4.3 提供 API/CLI，WebUI 以后复用。

### A4.3 恢复校验与正常启动校验（Issue #64，用户已确认 1A）

完整备份排除凭据后，账号 secret 引用可能指向尚未补齐的文件，声明式内联凭据也可能合法缺失。恢复校验可以依据经过认证并绑定目标的清单、字段位置与 Schema 声明识别这些预期缺项，并单列待配置；仅此不妨碍数据恢复成为 ready_for_switch。不得填假值、按错误消息猜测或忽略其他配置错误。
普通 `load_home` 与启动的校验规则不变：secret 路径仍须合法且文件存在，原全局错误仍为全局错误，不引入只对恢复实例生效的局部运行例外。维护模式只展示诊断，不将无效业务配置标为有效。补配后完整重新校验；文件存在不证明账号认证有效。具体排除边界、状态区分与验收见 backup-spec §8.3a 和 §13 第 15a 项。

## A4.3 必要资产配置注解（Issue #70，用户已确认，方案 1）

- 配置 Schema 用 `x-clytie-asset: true` 标注一个 asset_id 字符串；类型及内容遵守 SDK AssetId（内容 SHA-256），不接受文件路径、URL 或完整 AssetRef。长度、媒体类型和归属从本体登记取得。标注值只能为布尔值；false/未标注不声明资产依赖。
- 注解放在具体字符串值的 Schema 节点。数组在 items 的字符串节点标注，嵌套对象在对应 properties 节点标注，不在数组或对象整体标注。可选属性缺省时无依赖；属性存在则必须是合法 asset_id，null/空串不能冒充缺省。配置合并完成后解析实际生效配置，不收集被覆盖的旧层值。
- 注解校验与配置 Schema 校验使用同一套已解析 Schema。仅沿 Schema 对实际配置值的适用节点收集，不扫描未声明的普通文本。allOf 合并适用声明，oneOf 使用唯一有效分支，anyOf 使用所有有效分支声明的并集；某有效分支未标注不能抵消另一有效分支的资产声明。重复标识在同一可信归属范围内合并，保留字段来源。Schema 无有效分支或类型解释冲突按配置校验失败处理。
- 使用既有受控本地 Schema 引用解析，不为资产解析访问网络或读取任意外部文件；无法解析或不支持的声明结构必须明确报错，不能静默忽略资产注解。嵌套数组/对象及受支持的引用、条件 Schema 均须遵循验证器判定的适用节点；未知注解位置和不确定解释在清单/Schema 校验阶段拒绝。
- 同一字段或受凭据排除覆盖的子字段不能同时声明为必要资产；包括有效组合分支中出现的冲突。凭据/敏感排除与资产注解冲突须在清单或配置校验时拒绝，不在备份时选一边。普通隐私资产仍可在认证加密包中保存，资产注解本身不赋予公开可见性。
- 配置来源必须能由宿主确定所属伴侣或明确共享范围；插件/客户端不能通过配置值自授归属。纯配置校验只检查 Schema、编号与结构，不为此打开资产数据库；备份捕获时才按可信上下文核验登记、归属和内容，缺失或无权访问按 backup-spec 的缺项规则处理。

### 形象资料自检

伴侣配置可设 `appearance.self_check`（布尔值，默认 `true`）。伴侣在启动时读取此项；没有形象资料时调用次数为零。关闭时仍可在对话中使用形象资料。触发、结果和用量审计见 appearance-spec §6。
