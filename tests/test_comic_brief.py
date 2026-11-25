"""真实插件提交每日一报，脚本模型查看本体给出的工具、角色表和绘图结果。"""

import pytest

from clytie.agent.policy import text_parts
from clytie.plugin_host.http import HttpBackend
from clytie.sdk.types import HttpResponse, Tick

from .host_helpers import enable
from .test_agent import finish, make_agent
from .test_appearance_requests import set_description
from .test_image_gen_integration import CREDENTIAL_FILE


@pytest.mark.parametrize("condition", ["available", "no_appearance", "limit", "unlisted"])
async def test_daily_comic_brief_model_context_and_text_fallback(tmp_path, monkeypatch, condition):
    if condition != "no_appearance":
        set_description(tmp_path, "虚构的纸花播报员。")
    allowed = {} if condition == "unlisted" else {"draw_comic": {"daily": 1}}
    final_text = "花园报报道了虚构花展。https://garden.example/show"
    final = {"finish": "stop", "text": final_text}
    draw = {
        "finish": "tool_calls",
        "tool_calls": [
            {
                "tool": "draw_comic",
                "call_id": "news-comic",
                "arguments": {
                    "title": "虚构花展",
                    "pages": [
                        {"panels": [{"scene": "播报花展", "characters": ["self"], "dialogue": []}]}
                    ],
                },
            }
        ],
    }
    agent, channel, model = await make_agent(
        tmp_path,
        changes={
            "proactive": {"tools": allowed},
            "appearance": {"self_check": False},
            "memory": {"recollect": {"enabled": False}},
        },
    )
    renders = []

    async def unavailable(self, method, url, **kwargs):
        renders.append((method, url))
        return HttpResponse(status=429, headers={}, body=b"{}")

    async def submit(pid, cid, **options):
        return await agent.submit(cid, plugin_id=pid, **options)

    monkeypatch.setattr(HttpBackend, "request", unavailable)
    host = agent.host
    try:
        (tmp_path / "secrets").mkdir()
        (tmp_path / "secrets/image.txt").write_text("fictional-marker", encoding="utf-8")
        host.credentials = {"image_test": {CREDENTIAL_FILE: "image.txt", "plugins": ["image-gen"]}}
        settings = {
            "image-gen": {
                "config": {"backends": [{"model": "fictional-image", "api_key_ref": "image_test"}]}
            },
            "comic-maker": {},
            "proactive-chat": {"config": {"conversation": "owner_private", "topics": []}},
        }
        host.proactive_backends[agent.cid] = submit, agent.proactive_result
        host.configure(agent.cid, plugins=settings)
        await enable(host, "comic-maker")
        proactive = (await enable(host, "proactive-chat")).plugin
        agent.config["plugins"].update(dict.fromkeys(settings, {"enabled": True}))
        if condition == "limit":
            # 同一天早一点的另一次主动开口，实际调用一次工具，用完今日额度。
            model.turns.extend([draw, {"finish": "stop", "text": "虚构的早安。"}])
            await agent.submit("owner_private", topic="早安", reason="good_morning", tools=True)
            await agent.drain()
        now = agent.clock.now().replace(hour=12, minute=30)
        agent.clock.set(now)
        offer = await proactive.service(
            "offer",
            {
                "key": "garden-show",
                "tier": "brief",
                "title": "虚构花展",
                "summary": "花园开放了。",
                "sources": [{"name": "花园报", "url": "https://garden.example/show"}],
                "time": now.isoformat(),
            },
        )
        assert offer["status"] == "accepted"
        first = len(model.requests)
        if condition == "available":
            model.turns.append(draw)
        model.turns.append(final)
        await proactive.on_tick(Tick(schedule_id="tick", scheduled_at=now, now=now, missed=0))
        await agent.drain()

        request = model.requests[first]
        messages = [text_parts(m.parts) for m in request.messages]
        assert any(
            "如果能用 draw_comic" in text and "否则照常只发文字" in text for text in messages
        )
        assert ("draw_comic" in {tool.name for tool in request.tools}) == (
            condition in {"available", "no_appearance"}
        )
        sections = [text for text in messages if text.startswith("漫画角色表")]
        if condition == "available":
            assert len(sections) == 1 and "self 可用" in sections[0]
            assert any(
                "绘制失败" in text_parts(m.parts)
                for m in model.requests[-1].messages
                if m.role == "tool"
            )
        elif condition == "no_appearance":
            assert len(sections) == 1 and "self 无形象资料" in sections[0]
        else:
            assert not sections
        assert any("draw_comic: proactive_limit" in text for text in messages) == (
            condition == "limit"
        )
        assert len(renders) == int(condition in {"available", "limit"})
        delivered = [i for i in channel.sent if i.trace_id == request.trace_id]
        assert len(delivered) == 1 and delivered[0].kind == "proactive"
        assert [(p.type, p.text) for p in delivered[0].parts] == [("text", final_text)]
    finally:
        await finish(agent)
