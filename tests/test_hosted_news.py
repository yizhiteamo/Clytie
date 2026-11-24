"""Exercise both official plugins with a real host, a fictional feed, model and channel."""

from clytie.plugin_host.http import HttpBackend
from clytie.sdk.types import HttpResponse, Tick

from .host_helpers import enable
from .test_agent import finish, make_agent
from .test_agent_model_usage import usages


async def test_hosted_news_judgment_to_owner_delivery(tmp_path, monkeypatch):
    endpoint = "https://publisher.example/feed"
    calls = []

    async def fake_http(self, method, url, **kwargs):
        assert self._public_only and (method, url) == ("GET", endpoint)
        calls.append(url)
        return HttpResponse(
            status=200,
            headers={},
            body=b"<rss><channel><item><title>Fictional AI release</title>"
            b"<link>https://publisher.example/ai</link>"
            b"<description>A fictional laboratory announced a language model.</description>"
            b"</item></channel></rss>",
        )

    monkeypatch.setattr(HttpBackend, "request", fake_http)
    config = {
        "news-feeds": {
            "enabled": True,
            "config": {
                "sources": [{"id": "fictional-lab", "type": "rss", "url": endpoint, "major": True}]
            },
        },
        "proactive-chat": {
            "enabled": True,
            "config": {
                "conversation": "owner_private",
                "topics": [],
                "breaking_at_night": "always",
            },
        },
    }
    agent, channel, model = await make_agent(tmp_path, {"text": "yes", "finish": "stop"})

    async def submit(pid, cid, **options):
        return await agent.submit(cid, plugin_id=pid, **options)

    try:
        host = agent.host
        host.proactive_backends[agent.cid] = submit, agent.proactive_result
        host.configure(agent.cid, plugins=config)
        proactive = await enable(host, "proactive-chat")
        feeds = await enable(host, "news-feeds")
        now = agent.clock.now()
        tick = Tick(schedule_id="poll", scheduled_at=now, now=now, missed=0)
        await feeds.plugin.on_tick(tick)
        await proactive.plugin.on_tick(tick.model_copy(update={"schedule_id": "tick"}))
        await agent.drain()
        await agent.kernel.bus.drain()
        assert calls == [endpoint]
        assert [i.conversation_id for i in channel.sent] == ["owner_private"]
        assert channel.sent[0].kind == "proactive"
        assert not channel.reacts
        assert len(model.requests) == 2
        judgment = model.requests[0]
        public_request = judgment.model_dump_json()
        assert "Fictional AI release" in public_request
        assert "fictional private detail" not in public_request
        assert not judgment.tools
        assert [
            (r["plugin"], r["plugin_purpose"]) for r in usages(agent) if r["purpose"] == "plugin"
        ] == [("news-feeds", "news_judgment")]
    finally:
        await finish(agent)
