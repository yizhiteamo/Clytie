# 离线假平台

入口 `plugin:Plugin` 无参数构造，`start(ctx)` 后通过清单的
`channel.factory = create_channel` 创建 `clytie.sdk.testing.FakeChannel`，使用手写虚构
事件；发送和表情回应只把意图追加到内存列表并返回虚构收据，不连接真实平台。
收据时间来自上下文时钟，耗时步骤支持令牌取消和协程取消。

从仓库根目录运行 `python -m pytest plugins/fake-channel/tests` 验证平台契约。

测试通过 `plugin_from_manifest` 执行读清单、构造、启动、factory 和停止，
确认平台上下文含有身份解析接口。生产环境的发现和依赖装配属于 A2。
