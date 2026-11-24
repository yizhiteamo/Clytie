"""Read the seven-day feed cache from the invoking chat record."""

from clytie.sdk.types import Plan, ToolResult


class RecentNews:
    def __init__(self, plugin):
        self.plugin = plugin

    def idempotency_key(self, call, inv):
        return call.call_id

    async def prepare(self, call, inv):
        return Plan(summary="查看近期科技新闻", heavy=False, data={"call_id": call.call_id})

    async def execute(self, plan, inv, cancel):
        cancel.raise_if_cancelled()
        material = await self.plugin.material()
        return ToolResult(
            call_id=plan.data["call_id"],
            status="ok",
            data={},
            material=material,
            content=material.text if material else "近七天还没有可提供的订阅资料。",
        )
