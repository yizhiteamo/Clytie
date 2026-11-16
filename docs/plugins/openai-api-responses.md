# openai-api 的 Responses 模式（B3c）

状态：草案，待用户确认（2026-11-16）。

## 1. 为什么要做

第一次真实测试中，聊天模型通过 Chat Completions 接口带工具调用时返回 400，`error.param` 为 `reasoning_effort`（PR #108 的诊断字段）。离线核对和三次极小的手动调用确认：

- 同一个模型、同一个密钥，Chat Completions 不带工具时返回 200；
- Chat Completions 带工具时，默认推理设置返回 400；设为 `reasoning_effort: none` 后返回 200；
- Responses 接口同时带工具和推理（`medium`）时返回 200，并且正确调用了工具。

这是 Chat Completions 接口对这类推理模型的限制，与 Clytie 的请求内容无关。用户要求效果优先，不接受用关闭推理来换取可用（2026-11-16），所以 openai-api 增加 Responses 接口。

## 2. 范围

- 只修改 `plugins/openai-api`，不修改本体和 SDK，不新增扩展点。
- 新增配置 `api`：`chat_completions`（默认，行为不变）或 `responses`。
- 新增可选配置 `reasoning_effort`：1–16 个小写字母，原样发送；省略时不发送，由服务使用默认值。插件不维护取值列表，模型是否支持由服务端判断（与 image-gen 的 `size` 口径相同）。
  - Chat Completions 模式下发送为 `reasoning_effort`；Responses 模式下发送为 `reasoning.effort`。
- `responses` 模式请求 `base_url` 加 `/responses`，不使用流式输出。

## 3. 请求转换（`responses` 模式）

| 本体内容 | Responses 输入 |
|---|---|
| `system` 消息 | `role: developer` 的消息，顺序不变 |
| `user` 消息的文字 | `input_text` |
| `user` 消息的图片（已授权的 AssetRef） | `input_image`，data URL；格式和大小限制与 Chat Completions 模式相同 |
| `assistant` 消息的文字 | `role: assistant` 的消息 |
| `assistant` 消息的工具调用 | 每个调用一项 `function_call`（`call_id`、`name`、JSON 字符串 `arguments`） |
| `tool` 消息 | `function_call_output`（`call_id`、`output`） |
| 工具定义 | `type: function`，`name`、`description`、`parameters` 与 Chat Completions 模式相同，并显式发送 `strict: false` |

- 固定发送 `store: false`：对话内容不保存在服务端。
- 固定请求 `include: ["reasoning.encrypted_content"]`，用于第 4 节。
- 不使用 `previous_response_id`，每次请求都带完整输入。

## 4. 推理内容在工具循环中的连续性

模型调用工具前产生的推理内容，在拿到工具结果后的下一轮请求中带回，避免模型"想到一半就失忆"。

- 响应中的 `reasoning` 项（只保留 `type`、`id`、`encrypted_content`、`summary`）按紧随其后的 `function_call` 的 `call_id` 暂存。
- 下一轮请求转换到对应的 `function_call` 时，把暂存的推理项原样放在它前面。
- 暂存只在插件进程内存中，不写磁盘、日志、账本和资产库；最多 64 组，每组 10 分钟后丢弃，被带回一次后也丢弃。
- 找不到暂存内容时（例如重启后），只发送 `function_call`，不报错。
- 加密推理内容属于不透明数据，插件不解析、不记录，也不交给本体。

## 5. 响应转换

- `output` 中 `message` 项的 `output_text` 拼接为回复文字；`function_call` 项转换为 `ToolCall`，`arguments` 必须是 JSON 对象。
- 有工具调用时 `finish` 为 `tool_calls`，否则为 `stop`。
- `status` 不是 `completed`（例如 `incomplete`），或者出现拒绝内容（`refusal`）时，返回 `incomplete_response`，不冒充完整成功。
- 用量：`usage.input_tokens`、`usage.output_tokens` 映射到 `TokenUsage`；缺失时保持未知，不填 0（agent-spec §11.1）。
- 错误状态码的分类、PR #108 的诊断字段（`code`、`type`、`param`）与 Chat Completions 模式相同；仍然不记录 `error.message`。

## 6. 不做

- 流式输出、服务端会话（`previous_response_id`、`store: true`）、内置工具（网页搜索、文件搜索等）。
- 推理摘要展示给用户。
- 自动在两种接口之间切换，或在 400 时自动关闭推理重试。

## 7. 验收

全部离线，使用假 HTTP 和虚构凭据：

1. 默认 `chat_completions` 的现有测试全部不变并通过；`reasoning_effort` 在两种模式下的字段位置正确，省略时不发送，不合法的值导致配置无效。
2. 请求转换：第 3 节表格的每一行都有测试，包括多轮工具循环、并行的多个工具调用、带图片的用户消息；`store: false`、`include` 和 `strict: false` 固定出现。
3. 推理连续性：推理项在下一轮被放在对应的 `function_call` 之前；超过 64 组或 10 分钟后丢弃；带回一次后丢弃；找不到时正常发送；加密内容不出现在日志、账本和错误信息中。
4. 响应转换：文字、工具调用、两者同时出现、`incomplete`、`refusal`、非法 `arguments`、缺失用量各有测试。
5. 错误分类与诊断字段在 `responses` 模式下与现有测试等价。
6. 离线端到端：假协议端 → `openai-api`（`responses` 模式，假 HTTP）→ `image-gen`（假后端）→ 假协议端，跑通画图和改图两轮。
7. 双平台 CI 通过；线上模型调用次数为 0。

规模预估：1.5 个工作日。

## 8. 之后的真实验证（需要用户当次同意）

部署配置改为 `api: responses`、`reasoning_effort: medium`，在测试会话中按 roadmap 的真实链路测试进行。

接口依据：[Responses 官方参考](https://developers.openai.com/api/reference/resources/responses/methods/create)。
