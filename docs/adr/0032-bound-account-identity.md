# ADR 0032：平台插件读取自己绑定的账号

状态：用户已确认（2026-11-15，Issue #89）。

## 问题

QQ 平台插件（B2）连上协议端后，要核对"协议端登录的账号就是配置里绑定的账号"，还要判断哪些消息是自己发的、哪个 @ 是在叫自己。现有 `ctx.identity` 只能映射 `people` 和 `conversations`；`account_secret()` 只返回令牌。插件拿不到"我应该是哪个账号"。

不能用的办法：
- 信任协议端自己报告的 `self_id`：要核对的正是它，拿它当标准就等于没核对。
- 把账号再写一遍到插件配置：同一个事实两处配置，改了一处就会不一致；plugin-host-spec 也规定平台登录账号不放进 `ctx.config`。
- 读实例文件或宿主内部属性：越过插件边界。

## 决定

1. `ctx.identity` 增加 `self_account() -> str | None`。
2. 返回值是 `accounts.<ref>.account`，`ref` 是伴侣配置 `channels[].account` 为本插件单元绑定的那个引用。由宿主按绑定提供，插件不能传入引用名，也读不到别的账号。
3. 只在实现了 channel 扩展点的插件单元上出现（与 `identity` 现有规则相同）。同一插件装在两个伴侣上时，各自只看到自己的绑定。
4. 没有绑定账号时返回 `None`。需要账号的插件自行决定：`qq-onebot` 在 `start` 中抛出 `PluginUnavailable("auth_invalid")`，宿主按现有规则不重启。
5. 不做 I/O，在单元的生命周期内不变。改绑定要重启插件，和现有配置重载一致。
6. 只返回账号，不返回令牌；令牌仍然只能通过 `ctx.credentials.account_secret()` 读取。
7. 平台账号属于个人信息：插件不把它写进日志、`ToolResult` 和错误信息；状态里只显示引用名。

## 影响

- sdk-spec §7.5 的 `identity` 一行增加 `self_account()`；SDK 次版本号升级。
- `clytie.sdk.testing.FakeIdentity` 增加 `self_account` 参数，默认 `None`。
- 契约测试覆盖：已绑定时返回绑定账号；未绑定时为 `None`；两个伴侣的同一平台插件互相隔离；非平台插件没有 `identity`。
