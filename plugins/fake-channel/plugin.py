"""使用清单装配的离线参考平台。"""

from clytie.sdk.testing import FakeChannel
from clytie.sdk.testing.samples import sample_event


class Plugin:
    def __init__(self):
        self.ctx = None

    async def start(self, ctx):
        await ctx.clock.sleep(0)
        self.ctx = ctx

    def create_channel(self):
        if self.ctx is None:
            raise RuntimeError("plugin has not started")
        return FakeChannel(self.ctx, incoming=(sample_event(self.ctx.clock),))

    async def stop(self):
        if self.ctx is not None:
            await self.ctx.clock.sleep(0)
        self.ctx = None
