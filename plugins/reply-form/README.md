# reply-form

在伴侣配置中启用后，较长的聊天回复可按空行或句末标点拆分。群里有人插话时引用原消息。任务单和产物单独交付。

在插件配置的 `tuning` 中可以设置 `min_chars`（默认 60）、`max_parts_group`（2）、`max_parts_private`（3）和 `interval_seconds`（默认 `[1, 2]`，单位为秒）。

主动消息的分段上限 `max_parts_proactive` 默认为 6，可调低到 1。使用能力的主动消息也按这个上限拆分。条间间隔由 `interval_seconds_proactive` 设置，默认 2–4 秒。整组发送仍受 `delivery.group_max_seconds` 限制。

拆分时，句末紧随的右引号和右括号留在前一条，网址保持完整，每条去掉首尾空白。群聊需要引用时，仅第一条带引用。
