# web-tools

`search_web` 查询公开来源，`read_url` 阅读网页正文。两个工具随当前回复执行。按伴侣现有的 `permissions` 和 `tool_scopes` 授权，私聊和群聊沿用各自的准入规则。

```yaml
plugins:
  web-tools:
    enabled: true
    config:
      backend: openai
      model: fictional-search-model
      credential: search_key
```

`model` 填支持联网搜索的模型，`credential` 填实例中已授权给此插件的凭据引用。查询通过 Responses API 的 `web_search` 工具发送。指定 `recent_days` 后，只保留能核实发布时间的结果。搜索结果页的元数据核实也受公网限制。

`read_url` 支持 HTML、XHTML 和纯文本。读取仅允许公网 HTTP 或 HTTPS 的 80、443 端口，最多跟随三次重定向。下载、内容类型、登录和正文提取错误会返回给模型。同一会话可在最近 24 小时内再次参考已读资料。
