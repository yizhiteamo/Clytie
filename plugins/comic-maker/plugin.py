"""漫画工具、可见角色段落和本机导入命令。"""

import asyncio
import json

from clytie.sdk.immutable import thaw
from clytie.sdk.types import ContextSection

from .imports import import_command, process
from .media import typeset
from .storyboard import ComicTool, visible


class Plugin:
    async def start(self, ctx):
        self.ctx = ctx
        self.import_lock = asyncio.Lock()
        await ctx.clock.sleep(0)

    async def stop(self):
        await self.ctx.clock.sleep(0)

    def create_draw(self):
        return ComicTool(self)

    async def contribute(self, request):
        budget = min(300, request.budget_chars)
        if budget <= 0:
            return None
        cid = request.conversation.conversation_id if request.conversation else None
        full, brief = [], []
        for key in await self.ctx.storage.keys("character."):
            role = await self.ctx.storage.get(key)
            if role is None or not visible(role, cid):
                continue
            name = f"{role['name']}（{role['character_id']}）"
            brief.append(role["name"])
            full.append(name + ("：" + "、".join(role["keywords"]) if role["keywords"] else ""))
        status = "self 可用。" if request.has_appearance else "self 无形象资料。"
        if len(status + "；".join(full)) <= budget:
            text = status + "；".join(full)
        else:
            text = status
            for item in brief:
                line = item + "；"
                if len(text) + len(line) <= budget:
                    text += line
            text = text.rstrip("；")
        return ContextSection(title="漫画角色表", text=text[:budget], priority=100)

    async def decorate(self, asset, title, number):
        ctx = self.ctx
        if not ctx.config.get("header_footer", True):
            return asset
        title_info = await ctx.storage.get("font.title")
        caption_info = await ctx.storage.get("font.caption")
        if not (title_info and caption_info):
            return asset
        data = await ctx.assets.read(asset.asset_id)
        directory = ctx.storage.path / "fonts"
        date = ctx.clock.now().astimezone(ctx.clock.local_tz).date().isoformat()
        output = await asyncio.to_thread(
            typeset,
            data,
            title,
            number,
            date,
            ctx.config.get("footer_text", ""),
            directory / title_info["filename"],
            directory / caption_info["filename"],
        )
        return await ctx.asset_writer.put(output, "image/png", "comic-maker")

    async def import_preview(self, request):
        try:
            entry = json.loads(request["query"]["entry"])
            return await process(self.ctx, entry, commit=False)
        except (ValueError, KeyError, TypeError) as exc:
            return {"status": 400, "json": {"description": str(exc)}}

    async def import_commit(self, request):
        async with self.import_lock:
            try:
                return await process(self.ctx, thaw(request["json"]), commit=True)
            except (ValueError, KeyError, TypeError) as exc:
                return {"status": 400, "json": {"description": str(exc)}}

    async def character_import(self, args, api):
        return await import_command("character", args, api)

    async def character_set(self, args, api):
        return await import_command("character", args, api)

    async def font_import(self, args, api):
        return await import_command("font", args, api)
