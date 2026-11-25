"""检查全部分镜后才允许进行第一笔画图调用。"""

import hashlib
import json

from clytie.sdk.context import ServiceUnavailable
from clytie.sdk.immutable import thaw
from clytie.sdk.types import AssetRef, Denial, ErrorInfo, Plan, ToolResult

from .imports import character_key
from .references import page_references

DEFAULT_STYLE = "Q 版二次元，浅色、干净线稿、平涂，搭配表情符号和效果线。"
LAYOUT = (
    "竖版约 2:3。按本页实际格数分格，通常每页 6–9 格，每行 1–2 格，宽窄错落，高潮可用全宽格或特写。"
    "日常小剧场，由铺垫发展到反转或吐槽。同场景保持背景一致，角色标志保持一致。"
    "画出指定分格、对话框和准确台词，短句一般不超过两行，每行约六至八字。拟声词画在画面里。"
    "不画真人长相，新闻中的真人由角色转述，必要时画不露脸的剪影。"
)


def nonempty(text):
    return isinstance(text, str) and bool(text.strip())


def check_story(args, config):
    if not nonempty(args.get("title")) or ("style" in args and not nonempty(args["style"])):
        raise ValueError("标题和画风须为非空文字。")
    pages = args.get("pages")
    if not isinstance(pages, list) or not pages:
        raise ValueError("至少需要第 1 页分镜。")
    limit = config.get("max_pages", 10)
    if len(pages) > limit:
        raise ValueError(f"第 {limit + 1} 页超出 {limit} 页上限。")
    for n, page in enumerate(pages, 1):
        prefix = f"第 {n} 页"
        panels = page.get("panels") if isinstance(page, dict) else None
        if not isinstance(panels, list) or not panels:
            raise ValueError(prefix + "至少需要一格。")
        if len(panels) > config.get("max_panels", 10):
            raise ValueError(prefix + "分格数超出上限。")
        for i, panel in enumerate(panels, 1):
            where = f"{prefix}第 {i} 格"
            if not isinstance(panel, dict) or not nonempty(panel.get("scene")):
                raise ValueError(where + "需要场景和动作。")
            cast = panel.get("characters")
            if not isinstance(cast, list) or not all(nonempty(role) for role in cast):
                raise ValueError(where + "需要角色 ID 列表。")
            if "sfx" in panel and not nonempty(panel["sfx"]):
                raise ValueError(where + "拟声词须为非空文字。")
            if not isinstance(panel.get("dialogue"), list):
                raise ValueError(where + "需要台词列表。")
            for line in panel["dialogue"]:
                if not isinstance(line, dict) or not nonempty(line.get("text")):
                    raise ValueError(where + "需要台词文字。")
                if len(line["text"]) > config.get("max_dialogue_chars", 30):
                    raise ValueError(where + "台词过长。")
                if not nonempty(line.get("speaker")):
                    raise ValueError(where + "每句台词都需要说话人。")


def visible(role, conversation_id):
    scopes = role.get("conversations")
    return scopes is None or conversation_id in scopes


def cast_of(page):
    return list(dict.fromkeys(role for panel in page["panels"] for role in panel["characters"]))


def render_prompt(story, n, cast, descriptions, style, labels):
    return "\n".join(
        [
            "画出一整页漫画。",
            LAYOUT,
            f"画风：{style}",
            f"漫画标题：{story['title']}（第 {n} 页）",
            "角色资料：" + json.dumps({r: descriptions[r] for r in cast}, ensure_ascii=False),
            "所附参考图依次为：" + "、".join(labels),
            "逐格分镜（含说话人及台词原文）："
            + json.dumps(story["pages"][n - 1]["panels"], ensure_ascii=False),
        ]
    )


class ComicTool:
    def __init__(self, plugin):
        self.plugin = plugin

    def idempotency_key(self, call, inv):
        return hashlib.sha256(
            f"{inv.companion_id}:{inv.trace_id}:{call.call_id}".encode()
        ).hexdigest()

    async def prepare(self, call, inv):
        await self.plugin.ctx.clock.sleep(0)
        args = thaw(call.arguments)
        try:
            check_story(args, self.plugin.ctx.config)
        except ValueError as exc:
            return Denial(reason_code="invalid_request", message_for_model=str(exc))
        return Plan(
            summary=f"绘制 {len(args['pages'])} 页漫画",
            heavy=True,
            data={"call_id": call.call_id, "story": args},
        )

    async def load_cast(self, pages, inv):
        ctx = self.plugin.ctx
        descriptions, portraits, names = {}, {}, {}
        for n, page in enumerate(pages, 1):
            for role in cast_of(page):
                if role in portraits:
                    continue
                if role == "self":
                    appearance = await ctx.appearance.current()
                    if not (appearance.description.strip() or appearance.references):
                        raise ValueError(f"第 {n} 页使用 self，但伴侣还没有形象资料。")
                    descriptions[role] = appearance.description
                    portraits[role] = list(appearance.references)
                    names[role] = "self"
                else:
                    entry = await ctx.storage.get(character_key(role))
                    if entry is None:
                        raise ValueError(f"第 {n} 页使用了未知角色 {role}。")
                    if not visible(entry, inv.conversation.conversation_id):
                        raise ValueError(f"第 {n} 页角色 {role} 不允许在当前会话使用。")
                    descriptions[role] = entry["name"] + "：" + entry["description"]
                    portraits[role] = [AssetRef.model_validate(r) for r in entry["references"]]
                    names[role] = entry["name"]
        return descriptions, portraits, names

    async def execute(self, plan, inv, cancel):
        ctx = self.plugin.ctx
        cancel.raise_if_cancelled()
        story = thaw(plan.data["story"])
        call_id = plan.data["call_id"]
        try:
            descriptions, portraits, names = await self.load_cast(story["pages"], inv)
        except ValueError as exc:
            return failure(call_id, "invalid_request", str(exc))
        service = ctx.services.get("image.render")
        if service is None:
            return failure(call_id, "unavailable", "画图服务不可用。")
        style = story.get("style", ctx.config.get("style", DEFAULT_STYLE))
        result_pages, summaries = [], []
        previous = None
        for n, page in enumerate(story["pages"], 1):
            cancel.raise_if_cancelled()
            cast = cast_of(page)
            selected, missing = page_references(page["panels"], portraits, previous)
            labels = ["上一页（画风）"] if previous else []
            for ref in selected[len(labels) :]:
                labels.append(" / ".join(r for r in cast if ref in portraits[r]))
            prompt = render_prompt(story, n, cast, descriptions, style, labels)
            try:
                result = await service.call(
                    "render",
                    {
                        "prompt": prompt,
                        "size": "1024x1536",
                        "references": [r.model_dump(mode="json") for r in selected],
                    },
                )
            except ServiceUnavailable:
                return failure(call_id, "unavailable", f"第 {n} 页画图服务不可用。")
            cancel.raise_if_cancelled()
            if "error" in result:
                code = result["error"]
                return failure(call_id, code, f"第 {n} 页绘制失败（{code}）。")
            try:
                previous = AssetRef.model_validate(thaw(result["asset"]))
                result_pages.append(await self.plugin.decorate(previous, story["title"], n))
            except (KeyError, ValueError, OSError):
                return failure(call_id, "invalid_page", f"第 {n} 页的图片无法读取或排版。")
            line = f"第 {n} 页：" + ("、".join(names[r] for r in cast) or "无角色")
            if missing:
                line += "；未附参考图：" + "、".join(missing)
            summaries.append(line)
        cancel.raise_if_cancelled()
        return ToolResult(
            call_id=call_id,
            status="ok",
            assets=result_pages,
            data={},
            content=f"共 {len(result_pages)} 页。\n" + "\n".join(summaries),
        )


def failure(call_id, code, message):
    return ToolResult(
        call_id=call_id,
        status="denied" if code == "denied" else "error",
        content=message,
        data={},
        error=ErrorInfo(code=code, message=message),
    )
