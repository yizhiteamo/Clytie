# 聊天行为插件

| 插件 | 用途 |
|---|---|
| [chat-timing](chat-timing/README.md) | 合并消息、确认称呼，根据群里的发言比例和回应情况决定是否接话。 |
| [reply-form](reply-form/README.md) | 拆分较长的聊天回复，在有人插话时引用目标消息，给分条发送留出间隔。 |
| [proactive-chat](proactive-chat/README.md) | 按作息、节日和最近聊天时间，在已登记的主人私聊中提出话题。每日一报可在画漫画的能力可用且有当前形象时采用漫画配文。 |

完整官方清单见 [catalog.json](catalog.json)，其他规划见[预设的官方插件](../docs/plugins/catalog.md)。

## 漫画能力

[comic-maker](comic-maker/README.md) 按分镜生成多页漫画，可使用伴侣当前形象和会话允许的角色。独立场景可并行绘制，连续场景参考上一页，整本按页码交付。

[qq-onebot](qq-onebot/README.md) 默认每条消息最多发送 4 张图片。多页漫画按渠道的张数和字节上限分组，页序不变。

## 资料能力

[web-tools](web-tools/README.md) 提供公开来源搜索和网页正文阅读，两项工具都按轻量能力执行。

[news-feeds](news-feeds/README.md) 从公开订阅源整理近七天的科技新闻。`recent_news` 在当前对话中返回带来源的资料；启用主动私聊插件后，重要新闻还可进入突发提醒或早间简报。
