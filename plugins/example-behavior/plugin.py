"""多扩展点参考插件；状态只通过 ctx.storage/state/services 交互。"""

from clytie.sdk.behavior import Ignore, Normal, Respond
from clytie.sdk.types import ContextSection


class Plugin:
    async def start(self, ctx):
        self.ctx = ctx
        self.ticks = await ctx.storage.get("ticks") or 0

    async def stop(self):
        await self.ctx.clock.sleep(0)
        self.ctx = None

    async def before_attention(self, input):
        await self.ctx.clock.sleep(0)
        return Ignore() if self.ctx.config.get("quiet", False) else Normal()

    async def attention(self, input):
        await self.ctx.clock.sleep(0)
        return Respond()

    async def contribute(self, request):
        await self.ctx.clock.sleep(0)
        return ContextSection(
            title="example_state", text=self.ctx.config.get("text", "示例状态"), priority=100
        )

    async def on_tick(self, tick):
        self.ticks += 1
        await self.ctx.storage.set("ticks", self.ticks)
        await self.ctx.state.set({"ticks": self.ticks})
        await self.ctx.events.publish("example-behavior.ticked", {"ticks": self.ticks})

    async def service(self, method, args):
        await self.ctx.clock.sleep(0)
        if method != "get":
            raise ValueError("unsupported method")
        return {"ticks": self.ticks}
