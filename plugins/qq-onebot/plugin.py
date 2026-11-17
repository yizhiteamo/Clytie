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

    async def capture_start(self, request):
        try:
            return {
                "status": 200,
                "json": self.channel.capture.start(request["json"].get("seconds", 180)),
            }
        except ValueError as exc:
            return {"status": 409, "json": {"code": str(exc)}}

    async def capture_status(self, request):
        value = self.channel.capture.status()
        if request["query"].get("session") != value["session"]:
            return {"status": 409, "json": {"code": "capture_stale"}}
        return {"status": 200, "json": value}

    async def capture_cancel(self, request):
        if request["json"].get("session") != self.channel.capture.session:
            return {"status": 409, "json": {"code": "capture_stale"}}
        return {"status": 200, "json": self.channel.capture.cancel()}
