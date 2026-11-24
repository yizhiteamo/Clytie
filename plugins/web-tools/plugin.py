"""Search and read capabilities run within the invoking chat record."""

from clytie.sdk import PluginUnavailable
from clytie.sdk.types import Plan, ToolResult

from .pages import WebError, fetch
from .search import OpenAISearch


class WebTool:
    def __init__(self, plugin, search):
        self.plugin, self.search = plugin, search

    def idempotency_key(self, call, inv):
        return call.call_id

    async def prepare(self, call, inv):
        return Plan(
            summary="搜索资料" if self.search else "阅读网页",
            heavy=False,
            data={"call_id": call.call_id, **call.arguments},
        )

    async def execute(self, plan, inv, cancel):
        cancel.raise_if_cancelled()
        args = plan.data
        try:
            if self.search:
                material = await self.plugin.backend.search(
                    args["query"], args.get("recent_days"), args.get("max_results", 5)
                )
            else:
                material = await fetch(self.plugin.ctx, args["url"])
        except WebError as exc:
            return ToolResult(
                call_id=args["call_id"],
                status="error",
                content=exc.message,
                data={},
                error={"code": exc.code, "message": exc.message},
            )
        cancel.raise_if_cancelled()
        return ToolResult(
            call_id=args["call_id"], status="ok", content=material.text, material=material, data={}
        )


class Plugin:
    async def start(self, ctx):
        self.ctx = ctx
        if ctx.config.get("backend", "openai") != "openai":
            raise PluginUnavailable("invalid_configuration")
        self.backend = OpenAISearch(ctx)

    async def stop(self):
        pass

    def create_read(self):
        return WebTool(self, False)

    def create_search(self):
        return WebTool(self, True)
