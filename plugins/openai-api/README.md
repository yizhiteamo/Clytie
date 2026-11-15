# openai-api

普通模型插件，使用 Chat Completions 格式。本体执行工具循环；插件不执行工具，不切换模型，不重试请求，不提供流式输出。

必须配置 `base_url`（服务的 API 根地址，插件追加 `/chat/completions`）、`model`、`api_key_ref` 和 `context_chars`。接口与模型没有默认值。`context_chars` 是该部署模型的上下文字符预算，需要按服务能力填写，不是费用上限。

`supports_tools` 和 `supports_images` 默认开启；服务或模型不支持时应显式关闭。密钥通过宿主 `credentials` 配置授权后读取，不写在插件配置里。`quota()` 返回未知，不调用模型探测余额；平台错误区分认证失败、额度耗尽、限流和临时故障。没有次数或 token 限额配置。

图片输入只读取宿主资产服务已授权的 AssetRef，以 data URL 发送；不下载或透传不透明 `raw_ref`。仅支持用户消息中的 PNG、JPEG、WebP、GIF，其他输入明确报错。接口返回截断、过滤或非法工具调用时不冒充完整成功。

请求遵守本体 deadline，取消后关闭当前 HTTP 等待；请求可能已经被平台处理，取消不表示未计费。用量由本体按 agent-spec §11.1 写账本，缺失的用量保持未知。没有经过线上模型验证。

接口依据：[Chat Completions 官方参考](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create)。测试只用假 HTTP、虚构凭据和假协议端。
