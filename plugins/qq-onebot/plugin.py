"""QQ OneBot 平台单元；账号身份来自宿主绑定。"""

from clytie.sdk import PluginUnavailable

from .capture import identifier
from .capture_cli import capture
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

    def create_smoke_driver(self, clock):
        from .smoke_driver import Driver

        return Driver(clock)

    async def capture_cli(self, args, api):
        return await capture(args, api)

    async def stop(self):
        if self.channel is not None:
            await self.channel.close()

    async def capture_start(self, request):
        if self.channel is None:
            return {"status": 409, "json": {"code": "channel_unavailable"}}
        try:
            return {
                "status": 200,
                "json": self.channel.capture.start(request["json"].get("seconds", 180)),
            }
        except ValueError as exc:
            return {"status": 409, "json": {"code": str(exc)}}

    def _capture_status(self, session):
        if self.channel is None:
            return {"status": 409, "json": {"code": "channel_unavailable"}}
        value = self.channel.capture.status()
        if not session or session != value["session"]:
            return {"status": 409, "json": {"code": "capture_stale"}}
        return {"status": 200, "json": value}

    async def capture_status(self, request):
        query = request.get("query", {})
        session = query.get("session")
        result = self._capture_status(session)
        if query.get("wait") == "1" and result["json"].get("state") == "collecting":
            await self.channel.ctx.clock.sleep(0.25)
            return self._capture_status(session)
        return result

    async def config_proposal(self, request):
        result = self._capture_status(request.get("query", {}).get("session"))
        if result["status"] != 200:
            return result
        value = result["json"]
        ids = value["ids"]
        if (
            value["state"] != "complete"
            or len(ids) != 4
            or not all(isinstance(item, str) and identifier(item) for item in ids)
        ):
            return {"status": 409, "json": {"code": "capture_incomplete"}}
        return {"status": 200, "json": {"changes": {"emoji_ids": dict(zip("👌⏳✅❌", ids))}}}

    async def capture_cancel(self, request):
        if self.channel is None:
            return {"status": 409, "json": {"code": "channel_unavailable"}}
        if request["json"].get("session") != self.channel.capture.session:
            return {"status": 409, "json": {"code": "capture_stale"}}
        return {"status": 200, "json": self.channel.capture.cancel()}
