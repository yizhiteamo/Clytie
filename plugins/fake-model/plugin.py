"""使用清单装配的离线参考模型。"""

from clytie.sdk.testing import FakeModel


class Plugin:
    def __init__(self):
        self.ctx = None

    async def start(self, ctx):
        await ctx.clock.sleep(0)
        self.ctx = ctx

    def create_model(self):
        if self.ctx is None:
            raise RuntimeError("plugin has not started")
        return FakeModel(self.ctx)

    async def stop(self):
        if self.ctx is not None:
            await self.ctx.clock.sleep(0)
        self.ctx = None
