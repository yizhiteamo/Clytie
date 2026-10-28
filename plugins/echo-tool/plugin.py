"""最小回显能力：只返回结果，不发送任何消息。"""

import hashlib

from clytie.sdk.types import Denial, Plan, ToolResult


class EchoTool:
    def __init__(self, ctx):
        self.ctx = ctx

    def idempotency_key(self, call, inv):
        return hashlib.sha256(
            f"{inv.companion_id}:{inv.trace_id}:{call.call_id}".encode()
        ).hexdigest()

    async def prepare(self, call, inv):
        await self.ctx.clock.sleep(0)
        text = call.arguments.get("text")
        if not isinstance(text, str):
            return Denial(
                reason_code="invalid_arguments", message_for_model="text must be a string"
            )
        return Plan(summary="echo text", heavy=False, data={"call_id": call.call_id, "text": text})

    async def execute(self, plan, inv, cancel):
        cancel.raise_if_cancelled()
        await self.ctx.clock.sleep(0)
        cancel.raise_if_cancelled()
        return ToolResult(
            call_id=plan.data["call_id"], status="ok", content=plan.data["text"], data={}
        )


class Plugin:
    async def start(self, ctx):
        self.ctx = ctx
        await ctx.clock.sleep(0)

    def create_echo(self):
        return EchoTool(self.ctx)

    async def stop(self):
        await self.ctx.clock.sleep(0)
        self.ctx = None
