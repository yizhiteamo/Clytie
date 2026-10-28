# 离线假模型

入口 `plugin:Plugin` 无参数构造，通过 `start(ctx)` 接收上下文，再由清单中的
`model.factory = create_model` 创建 `clytie.sdk.testing.FakeModel`，返回
固定的虚构文本。所有等待都经过上下文时钟，支持令牌取消和协程取消。
`quota()` 返回 `QUOTA_UNKNOWN`，查询额度不会触发生成调用。

从仓库根目录运行 `python -m pytest plugins/fake-model/tests` 验证模型契约。
测试无需平台账号、模型账号或网络。

测试通过 `plugin_from_manifest` 执行读清单、构造、启动、factory 和停止。
配置通过伴侣的 `model.plugin` 引用此插件；生产环境的发现、依赖装配和故障隔离属于 A2。
