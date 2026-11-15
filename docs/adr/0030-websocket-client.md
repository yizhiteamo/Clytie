# ADR 0030：插件的 WebSocket 客户端

状态：用户已确认（2026-11-15，阶段二 B2）。

## 问题

QQ 平台插件（B2）需要和协议端保持一条长连接。现有 `ctx.http` 只能发一次性请求。插件又只能 import `clytie.sdk`，不能自己引入网络库，否则会绕过权限控制。

## 决定

1. 在 `ctx` 上增加 `websocket` 属性，需要 `network` 权限；未授权时属性不存在。
2. 只提供客户端，插件不能监听端口。理由：插件不开放入站端口，攻击面更小，权限含义也和 `network` 一致。QQ 插件因此使用 OneBot 的正向 WebSocket，由 Clytie 主动连接协议端。
3. 接口：
   ```
   async connect(url, *, headers=None, timeout_s=10, max_message_bytes=16 MB) -> WebSocketConnection
   WebSocketConnection:
     async send_text(text: str) -> None
     async receive(cancel) -> str
     async close() -> None
     closed: bool
     close_code: int | None
   ```
   - `url` 只接受 `ws://` 和 `wss://`。
   - 只收发文本帧。收到二进制帧，或者消息超过 `max_message_bytes` 时，关闭连接并抛出 `WebSocketClosed`。
   - 连接关闭后，再调用 `send_text` 或 `receive` 都抛出 `WebSocketClosed`（带 `close_code`）。
   - `receive` 遵守 sdk-spec §2 的取消协议。
   - 协议层的 ping/pong 由宿主处理；业务心跳由插件自己判断。
4. 插件停止或被停用时，宿主关闭它打开的所有连接。
5. 日志只记录地址的主机和端口、连接时长和关闭码，不记录消息内容、请求头和令牌。
6. 宿主用现有的 aiohttp 实现，不增加新依赖。
7. `clytie.sdk.testing` 提供假连接，插件的单元测试不需要真实网络。端到端测试可以在 `127.0.0.1` 上启动测试用的服务端。

## 影响

- sdk-spec §7.5 的"按权限出现"表增加 `websocket` 一行；SDK 次版本号升级。
- 将来插件改为在独立进程中运行时，连接对象由宿主代理，接口不变。
- 以后真的需要反向连接（协议端主动连 Clytie）时，另写 ADR，不扩大本接口。
