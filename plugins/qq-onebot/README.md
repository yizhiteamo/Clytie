# qq-onebot

B2 正向 WebSocket 平台插件。按 OneBot v11 标准与仓库 `docs/plugins/qq-onebot-compat.md` 工作；不管理协议端登录或进程，不提供反向连接。

## 配置

平台单元绑定 `accounts` 中的账号。预期账号由 `ctx.identity.self_account()` 提供；令牌由 `ctx.credentials.account_secret()` 每次连接时读取。插件配置不存账号和令牌；登录账号不符时进入 auth_invalid，重启前不再重连。

- `url` 必填：协议端同时支持事件和 API 的正向 ws/wss 地址。
- `connect_timeout_s` 默认 10 秒，`request_timeout_s` 默认 30 秒；`heartbeat_timeout_s` 默认 60 秒。
- `reconnect_delays` 至少一项，默认按 1、2、4、8、16、30 秒重连，耗尽后持续按最后一项间隔重试；成功核验连接后下一次断线重新按这组间隔处理。auth_invalid 或插件停止时退出重连。
- `queue_limit` 默认 128，溢出丢弃并记录告警和状态计数；不无限积压。
- `max_message_bytes` 默认 16 MiB；`max_text_chars` 默认 4000。
- `emoji_ids` 没有默认编号，四项 👌⏳✅❌ 都有编号才启用回应；接口不存在时仅当前连接降级。
- `retcode_classes` 默认空对象。只允许把明确的返回码配置成 rate_limited 或 transient；其他明确失败永久失败，不按错误文字猜测。
- `media_limits` 默认空对象，不声明媒体发送能力。需要发图时配置 image 的 `max_bytes` 与 MIME `formats`，例如 image/png。audio 和 video 使用对应配置；超限或不支持的类型明确失败。

图片、语音和视频出站从本体授权资产读取，校验登记、字节数和摘要，编码为标准 base64 文件参数。不接受任意本机路径。入站媒体只保留引用，不下载。file 和 mface 只接收入站，不发送；未能反查身份的提及、未知段及 card/link 出站明确拒绝，不静默省略。

## 收据与限制

发送仅在对应 echo 的成功响应带 message_id 时判为 sent。超时、断线、异步结果、畸形或无法对应的响应均 unknown，不自动重发；迟到响应不能改变已返回收据。回应失败不影响普通发送和任务状态。

状态包含连接状态、账号不符事件数、入站溢出数与畸形段数；日志不记录令牌、头、正文或账号。没有真实协议端验证；扩展字段只按已批准兼容契约实现，真实测试须另经用户授权。


### B2b 状态回应与编号采集

`react_remove` 默认关闭。经用户确认协议端支持后设为 `true`，状态切换时先撤销上一个确定发出的表情，再添加新表情。撤销失败不重试，继续添加；撤销接口返回 1404 时，本连接只停用撤销，重连重新按配置启用。

`clytie emoji capture companion_a --seconds 180` 通过本机 API 在正在运行的 qq-onebot 中开始采集。默认三分钟；只接受宿主识别为 owner 的 `user_id`、`is_add: true` 通知中的编号。按接单、执行中、完成、失败顺序添加；重复编号不重复计入。通知不进入聊天事件、账本或记忆。

四个编号齐全后显示并要求确认，`--yes` 不跳过这次编号确认。超时、不足四个、未收到支持的通知或用户不确认都不写配置。保存保留配置历史，重启伴侣后生效；不会自动重启。一个伴侣存在多个 qq-onebot 配置或无法安全保留 YAML 时拒绝写入，需要手动配置。
