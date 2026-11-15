"""QQ OneBot 平台单元；账号身份来自宿主绑定。"""

from clytie.sdk import PluginUnavailable

from .channel import OneBotChannel


class Plugin:
    def __init__(self):
        self.channel = None

    async def start(self, ctx):
        if ctx.identity.self_account() is None:
            raise PluginUnavailable("auth_invalid")
        self.channel = OneBotChannel(ctx)

    def create_channel(self):
        if self.channel is None:
            raise RuntimeError("plugin is not started")
        return self.channel

    async def stop(self):
        if self.channel is not None:
            await self.channel.close()
