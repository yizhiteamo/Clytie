# 离线假平台

入口 `plugin:Plugin` 复用 `clytie.sdk.testing.FakeChannel`。测试显式注入虚构
事件；发送和表情回应只把意图追加到内存列表并返回虚构收据，不连接真实平台。
收据时间来自上下文时钟，耗时步骤支持令牌取消和协程取消。

从仓库根目录运行 `python -m pytest plugins/fake-channel/tests` 验证平台契约。

A1 尚未规定平台扩展点在 `contributes` 中的 JSON 编码，因此清单保留空对象；
运行中的发现和注册属于 A2。
