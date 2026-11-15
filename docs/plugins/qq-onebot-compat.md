# qq-onebot 兼容契约（B2）

状态：用户已确认（2026-11-15，Issue #91）。

OneBot v11 标准没有规定表情回应、`file` 和 `mface` 消息段，也没有规定限流或临时失败的返回码。本文固定 B2 的最小做法：标准里有的按标准做；标准里没有的，插件只做不依赖未知字段的处理，可重试的返回码只能由用户配置，插件不猜测。

## 1. 参考资料范围

- 只依据 OneBot v11 标准仓库（`botuniverse/onebot-11`）的公开文档：`api/`、`message/`、`event/`、`communication/`。
- 标准没有覆盖的部分以本文为准。实现时不读取任何协议端的源码、文档或示例代码，也不根据错误文本推断含义。
- 本文中标准之外的字段，在真实链路测试（用户当次同意）中核对。核对结果与本文不一致时，先改本文，再改代码。

## 2. 表情回应 `set_msg_emoji_like`（标准之外）

- 请求：`{"action": "set_msg_emoji_like", "params": {"message_id": <原消息的 message_id>, "emoji_id": <配置的编号>}, "echo": ...}`。不发送其他参数。
- 四个表情 👌⏳✅❌ 的编号都来自配置，没有默认值；缺任何一个，`features().supports_react = false`，不发送这个动作。
- `status = "ok"`：回应成功。
- `status = "failed"` 且 `retcode = 1404`（标准规定的"接口不存在"）：判定协议端不支持。本次连接内 `supports_react = false`，记一条告警；重连后重新按配置启用。
- 其他失败、超时或断线：只记日志（动作名、`retcode`），不重试，不降级，不影响消息收据和任务状态。

## 3. 标准之外的入站消息段

- `file` 映射为 file 类型，`mface` 映射为 sticker 类型。只按 `type` 判断，不要求 `data` 里有任何字段。
- `raw_ref` 保存这个段的 `data` 原样内容，按 JSON 序列化后不超过 4 KB；超过或不是对象时，`raw_ref` 为空对象，并记一次畸形段计数。
- 文字说明只在 `mface` 的 `data.summary` 是字符串时使用，长度截到 100 字；否则不给说明。
- 出站不发送 `file` 和 `mface`。

## 4. 出站图片

图片用标准的 `image` 段发送，`file` 字段用标准支持的 `base64://` 格式。超出 `media_limits` 的产物不发送，收据为 `failed`。

## 5. 收据与返回码

判定只看 `status`、`retcode` 和 `data.message_id`，不读 `msg`、`wording` 等错误文字。

| 协议端回复 | 收据 |
|---|---|
| `status = "ok"`，带 `message_id` | `sent` |
| `status = "ok"`，没有 `message_id` | `unknown` |
| `status = "async"` | `unknown`（没有 `message_id`，无法确认） |
| `status = "failed"`，`retcode` 在配置 `retcode_classes` 中 | `failed`，错误码取配置的 `rate_limited` 或 `transient` |
| `status = "failed"`，其他 `retcode`（包括 1400、1404） | `failed`，永久错误 |
| 超时、断线、`echo` 对不上、迟到的回复、格式不对的回复 | `unknown`，不重发 |

- `retcode_classes` 是插件配置：`{返回码: rate_limited | transient}`，**默认为空**，也就是默认所有失败都按永久错误处理。永久错误不会重发，不会造成重复发送。
- 用户在真实测试中看到某个返回码确实是限流或临时失败后，再加进配置。插件不内置任何非标准返回码。
- 日志只记录动作名、`status` 和 `retcode`。

## 6. 测试

- 表情回应：成功；1404 降级且本连接内不再发送；其他失败只记日志；配置缺编号时不发送。
- `file`、`mface`：有数据、没有数据、`data` 不是对象、超过 4 KB。
- 收据：上表每一行一个测试；`retcode_classes` 配置为限流和临时失败各一个；未配置的码为永久错误。
