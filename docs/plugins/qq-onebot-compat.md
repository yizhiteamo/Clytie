# qq-onebot 兼容契约（B2）

状态：用户已确认（2026-11-15，Issue #91）。

OneBot v11 标准没有规定表情回应、`file` 和 `mface` 消息段，也没有规定限流或临时失败的返回码。本文固定 B2 的最小做法：标准里有的按标准做；标准里没有的，插件只做不依赖未知字段的处理，可重试的返回码只能由用户配置，插件不猜测。

## 1. 参考资料范围

- 只依据 OneBot v11 标准仓库（`botuniverse/onebot-11`）的公开文档：`api/`、`message/`、`event/`、`communication/`。
- 标准没有覆盖的部分以本文为准。可以阅读协议端公开的接口文档和源码作为参考（用户 2026-11-27 授权；原先两者都不读）。不把协议端的代码、示例或定义原文复制进本仓库：NapCat 的许可证禁止未经许可复制和修改。不根据错误文本推断含义。
- 本文中标准之外的字段，在真实链路测试（用户当次同意）中核对。核对结果与本文不一致时，先改本文，再改代码。

## 2. 表情回应 `set_msg_emoji_like`（标准之外）

- 请求：`{"action": "set_msg_emoji_like", "params": {"message_id": <原消息的 message_id>, "emoji_id": <配置的编号>}, "echo": ...}`。不发送其他参数。
- 四个表情 👌⏳✅❌ 的编号都来自配置，没有默认值；缺任何一个，`features().supports_react = false`，不发送这个动作。
- `status = "ok"`：回应成功。
- `status = "failed"` 且 `retcode = 1404`（标准规定的"接口不存在"）：判定协议端不支持。本次连接内 `supports_react = false`，记一条告警；重连后重新按配置启用。
- 其他失败、超时或断线：只记日志（动作名、`retcode`），不重试，不降级，不影响消息收据和任务状态。

### 2.1 撤掉表情（B2b，草案 2026-11-17）

- 插件配置 `react_remove`（默认 `false`）。为 `true` 并且表情回应已启用时，`features().supports_react_remove = true`，本体按 kernel-spec §13.5 先撤旧表情再加新表情，消息上始终只有一个状态表情。
- 撤销请求与添加相同，只多一个参数：`{"message_id": ..., "emoji_id": <旧表情编号>, "set": false}`。
- 依据：2026-11-17 在测试群实测（用户当次同意），对同一条消息依次添加、`set: false` 撤销、再添加另一个表情，三次都返回 `status = "ok"`、`retcode = 0`；用户目视确认消息上最后只剩新表情，撤销有效。
- 撤销返回 1404：本次连接内 `supports_react_remove = false`，记一条告警，之后只添加不撤销；重连后按配置重新启用。
- 撤销的其他失败、超时或断线：只记日志，仍然发送新表情，不重试，不影响任务状态。

### 2.2 表情编号的采集（标准之外的通知，草案 2026-11-17）

- 协议端对别人加的表情回应会推送 `notice` 事件，`notice_type` 为 `group_msg_emoji_like`，其中 `likes` 是 `[{emoji_id, count}]`。2026-11-17 实测：在 QQ 中依次添加四个表情，收到的 `emoji_id` 与 QQ 机器人官方表情列表中能查到的编号一致。
- 这个通知**只用于表情编号采集**（roadmap B2b），不转换成交给本体的事件，不进入账本和记忆。
- 采集时只读取 `likes[].emoji_id`，只接受 owner 账号加的回应；不读取消息内容，不记录群号和账号。
- 加表情的人：通知顶层的 `user_id`。依据：2026-11-17 实测（用户当次同意），owner 给伴侣发的消息加表情，`user_id` 是整数，等于 owner 账号，不等于 `self_id`；通知里没有 `operator_id`。所以 `user_id` 是加表情的人，不是消息作者。
- 判定：`user_id` 必须是整数或纯数字字符串，只在内存中使用，交给宿主的身份映射（`identity.resolve_actor`）；映射结果是 owner 才采集。不能用消息作者或 `self_id` 代替。
- 通知里有 `is_add` 字段。只有 `is_add` 为 `true` 时采集；为 `false`（撤销）或缺失时不采集。
- 以下情况拒绝这条通知，只累加一个拒绝计数，不记录账号、群号和内容：`user_id` 缺失或格式不对，映射不到 owner，`is_add` 不是 `true`，`likes` 不是列表，或其中没有可用的 `emoji_id`。
- 协议端不推送这类通知时，采集如实报告"不支持"，部署只能手动填写编号。

## 3. 标准之外的入站消息段

- `file` 映射为 `FilePart`，`mface` 映射为 `StickerPart`（ADR 0033）。只按 `type` 判断，不要求 `data` 里有任何字段。
- `raw_ref` 是这个段的 `data` 按 JSON 序列化得到的字符串（键排序、UTF-8），不超过 4 KB。超过 4 KB 或 `data` 不是对象时，`raw_ref` 为 `None`，并记一次畸形段计数。
- `mface` 的 `sticker_id`：有 `raw_ref` 时为 `"mface:"` 加 `raw_ref` 的 SHA-256 前 16 位十六进制；没有时为 `"mface:unknown"`。插件不从 `data` 中挑选字段当编号。
- `summary` 只在 `mface` 的 `data.summary` 是字符串时填写，截到 100 字；否则为 `None`。
- 标准消息段 `face` 映射为 `StickerPart`，`sticker_id` 为 `"face:"` 加 `data.id`；`data.id` 缺失或不是数字/数字字符串时，映射为 unknown 段并计数。
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
- 表情采集：owner 的通知被采集；非 owner、`user_id` 缺失、`user_id` 格式不对、`is_add` 为 `false` 或缺失时都被拒绝，只累加计数。
- 撤掉表情：`react_remove` 关闭时 `supports_react_remove = false`；打开时撤销请求带 `set: false`；撤销 1404 降级为只添加；撤销失败仍发送新表情。
- `file`、`mface`：有数据、没有数据、`data` 不是对象、超过 4 KB；同一份 `mface` 数据得到相同的 `sticker_id`；`summary` 截断。
- `face`：正常编号、缺失编号。
- 收据：上表每一行一个测试；`retcode_classes` 配置为限流和临时失败各一个；未配置的码为永久错误。

## 7. 读取历史消息（标准之外，B11 草案 2026-11-27）

供本体补收掉线期间的消息（catch-up-spec §8–§9）。下面的动作和字段在标准之外，按第 1 节在真实核对中确认；不一致时先改本节，再改代码。

- 插件配置 `history`，默认 `false`。为 `false` 时 `supports_history = false`，不发送下面的动作。
- 依据（2026-11-27 查阅）：NapCat 公开文档（napneko.pages.dev 的"接口"和"完整接口定义"页），以及 NapCat 源码中这两个动作和消息编号的实现。下面写的是读到的行为，真实核对时确认。
- 私聊：`{"action": "get_friend_msg_history", "params": {"user_id": "<对方账号>", "message_seq": "<位置>", "count": <条数>, "reverse_order": true}, "echo": ...}`；群：`{"action": "get_group_msg_history", "params": {"group_id": "<群号>", "message_seq": "<位置>", "count": <条数>, "reverse_order": true}, "echo": ...}`。两个动作的参数相同，账号和群号按字符串发送。`reverse_order` 是新名字，`reverseOrder` 是旧版兼容名，只发新名字。
- 第一次请求的 `message_seq` 为 `"0"`：协议端返回这个会话最新的 `count` 条。带位置时，协议端从这条消息开始取，结果**包含这条消息本身**；方向由 `reverse_order` 决定：`false` 取这条和它**之后**的消息，`true` 取这条和它**之前**的消息。往前翻页用 `true`，第一页这个参数不起作用，也一样发 `true`（2026-11-28 真实核对：`false` 返回的是之后的消息，和原先的写法相反，已改正；`true` 的方向待核对确认）。
- 返回：`status = "ok"` 时，`data.messages` 是列表，每一项是标准的私聊或群消息事件格式，按时间从早到晚排列。每项的 `message_id`、`message_seq`、`real_id` 是同一个数，`real_seq` 是平台的消息序号。`data.messages` 不是列表时，这次读取按失败处理。会话中没有符合条件的消息时，协议端返回失败（"消息不存在"），不是空列表。
- 每项按实时事件的映射处理（同一个函数）：自己发的消息跳过；缺少 `self_id` 时用本连接的账号；畸形的项跳过并计数。
- 分页：第一次请求取最新的 `count` 条（`history_page_size`，默认 50）。需要更早的消息时，下一次请求的 `message_seq` 取本页最早一项的 `message_id`；结果中这一项会重复出现，按 `message_id` 去重。插件仍然自己按 `time` 排序，不依赖返回顺序。翻页请求失败时停止翻页，用已经取到的部分；只有第一页失败才算这次读取失败。翻到发送时间不晚于 `after`、或者取满 `limit` 时停止。两次请求之间至少间隔 1 秒。
- `status = "failed"` 且 `retcode = 1404`：本连接内按不支持处理，不再发送；重连后按配置重新启用。其他失败、超时、断线：这次读取失败，只记动作名、`status` 和 `retcode`。
- 真实核对的第一项：同一条消息，实时推送和历史读取给出的 `message_id` 必须相同（kernel-spec §3）。按源码，两边都用"平台消息编号 + 会话类型 + 会话"算出同一个散列值，重启协议端后也不变；仍以核对为准。2026-11-28 在测试群核对：`message_id` 一致；翻页的位置能用 `message_id`，结果包含位置项；`reverse_order: false` 取的是位置之后的消息。还要核对：`reverse_order: true` 取的是位置和它之前的消息。
- 测试：私聊和群的请求格式，每一页的 `reverse_order` 都是 `true`；分页、去掉重复的位置项和停止条件；翻页失败时用已取到的部分；第一页失败算读取失败；自己的消息被跳过；`data.messages` 不是列表；1404 后本连接内不再请求；配置为 `false` 时不请求。
