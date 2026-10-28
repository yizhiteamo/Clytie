# 离线假模型

入口 `plugin:Plugin` 复用 `clytie.sdk.testing.FakeModel`，接收测试上下文后返回
固定的虚构文本。所有等待都经过上下文时钟，支持令牌取消和协程取消。
`quota()` 返回 `QUOTA_UNKNOWN`，查询额度不会触发生成调用。

从仓库根目录运行 `python -m pytest plugins/fake-model/tests` 验证模型契约。
测试无需平台账号、模型账号或网络。

A1 尚未规定模型扩展点在 `contributes` 中的 JSON 编码，因此清单保留空对象；
配置通过伴侣的 `model.plugin` 引用此插件，运行中的发现和注册属于 A2。
