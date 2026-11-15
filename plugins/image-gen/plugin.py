"""模型显式调用的画图工具；发送、权限与任务状态交给本体。"""

import asyncio
import hashlib

from clytie.sdk.types import Denial, ErrorInfo, Plan, ToolResult

from .backend import BackendError, OpenAIBackend


class ImageTool:
    def __init__(self, plugin, edit=False):
        self.plugin, self.edit = plugin, edit

    def idempotency_key(self, call, inv):
        return hashlib.sha256(
            f"{inv.companion_id}:{inv.trace_id}:{call.call_id}".encode()
        ).hexdigest()

    async def prepare(self, call, inv):
        await self.plugin.ctx.clock.sleep(0)
        prompt = call.arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return Denial(reason_code="invalid_request", message_for_model="需要明确的图片描述。")
        source = None
        if self.edit:
            history = await self.plugin.ctx.storage.get(self.plugin.history_key(inv)) or []
            if not history:
                return Denial(
                    reason_code="invalid_request", message_for_model="本会话还没有生成的图片。"
                )
            source = history[-1]["asset_id"]
        return Plan(
            summary="修改本会话上一张图" if self.edit else "根据描述画图",
            heavy=True,
            data={"call_id": call.call_id, "prompt": prompt, "source": source},
        )

    async def execute(self, plan, inv, cancel):
        cancel.raise_if_cancelled()
        ctx = self.plugin.ctx
        async with self.plugin.lock:
            cancel.raise_if_cancelled()
            await ctx.clock.sleep(0)
            image, media_type = None, None
            if self.edit:
                history = await ctx.storage.get(self.plugin.history_key(inv)) or []
                if not history or history[-1]["asset_id"] != plan.data["source"]:
                    return self.error(plan, "invalid_request")
                try:
                    ref = await ctx.assets.info(plan.data["source"])
                    image = await ctx.assets.read(ref.asset_id)
                    media_type = ref.media_type
                except (OSError, ValueError, KeyError):
                    return self.error(plan, "invalid_request")
            for index, backend in enumerate(self.plugin.backends):
                try:
                    if self.edit:
                        data, media = await backend.edit(
                            plan.data["prompt"], image, media_type, cancel
                        )
                    else:
                        data, media = await backend.generate(plan.data["prompt"], cancel)
                except BackendError as exc:
                    if (
                        exc.code == "content_refused"
                        and self.plugin.fallback
                        and index + 1 < len(self.plugin.backends)
                    ):
                        cancel.raise_if_cancelled()
                        continue
                    return self.error(plan, exc.code)
                cancel.raise_if_cancelled()
                ref = await ctx.asset_writer.put(data, media, "image-gen")
                key = self.plugin.history_key(inv)
                history = await ctx.storage.get(key) or []
                await ctx.storage.set(key, [*history, ref.model_dump(mode="json")][-5:])
                return ToolResult(
                    call_id=plan.data["call_id"],
                    status="ok",
                    content="图片已生成。",
                    assets=[ref],
                    data={},
                )
            return self.error(plan, "invalid_request")

    @staticmethod
    def error(plan, code):
        return ToolResult(
            call_id=plan.data["call_id"],
            status="error",
            content=f"图片生成失败：{code}",
            data={},
            error=ErrorInfo(code=code, message=code),
        )


class Plugin:
    async def start(self, ctx):
        await ctx.clock.sleep(0)
        self.ctx = ctx
        self.lock = asyncio.Lock()
        self.fallback = ctx.config.get("fallback_on_refusal", False)
        self.backends = [OpenAIBackend(ctx, config) for config in ctx.config["backends"]]

    @staticmethod
    def history_key(inv):
        identity = (
            f"{inv.companion_id}:{inv.conversation.channel_id}:{inv.conversation.conversation_id}"
        )
        return "recent-" + hashlib.sha256(identity.encode()).hexdigest()

    def create_draw(self):
        return ImageTool(self)

    def create_edit(self):
        return ImageTool(self, edit=True)

    async def stop(self):
        await self.ctx.clock.sleep(0)
