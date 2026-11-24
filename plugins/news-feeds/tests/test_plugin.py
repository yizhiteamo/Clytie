import json
import random
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from clytie.sdk.testing import MockClock

from .test_events import NOW, events
from .test_polling import HTTP, response

plugin = import_module(events.__name__ + ".plugin")


def rss(title="Fictional AI chip", url="https://lab.example/news"):
    return f"<rss><channel><item><title>{title}</title><link>{url}</link><description>Public summary</description></item></channel></rss>".encode()


def context(directory, sources, http, *, offers=None, **config):
    ctx = SimpleNamespace(
        config={"sources": sources, **config},
        clock=MockClock(NOW),
        http=http,
        random=random.Random(1),
        storage=SimpleNamespace(path=directory),
        model=SimpleNamespace(
            generate=AsyncMock(return_value=SimpleNamespace(finish="stop", text="yes"))
        ),
    )
    if offers is not None:
        ctx.services = {"proactive.news": SimpleNamespace(call=offers)}
    return ctx


async def test_optional_service_recent_tool_and_status_survive_restart(tmp_path):
    sources = [dict(id="first", type="rss", url="https://lab.example/rss", major=True)]
    ctx = context(tmp_path, sources, HTTP(response(rss(), ETag='"demo"')))
    feeds = plugin.Plugin()
    await feeds.start(ctx)
    await feeds.on_tick(None)
    await feeds.stop()
    restored = plugin.Plugin()
    await restored.start(ctx)
    await restored.on_tick(None)
    data = (await restored.status(None))["json"]
    assert data["events_today"] == 1 and data["sources"]["first"]["last_success"]
    assert "Public summary" not in str(data)
    assert len(ctx.http.calls) == 1
    ctx.model.generate.assert_not_awaited()
    assert (await restored.material()).sources[0].url == "https://lab.example/news"
    ctx.clock.advance(8 * 24 * 3600)
    assert await restored.material() is None
    assert (await restored.status(None))["json"]["events_today"] == 0


async def test_old_major_on_first_poll_and_undated_major_after_restart(tmp_path):
    source = dict(id="publisher", type="rss", url="https://lab.example/rss", major=True)
    dated = rss().replace(b"</item>", b"<pubDate>Sun, 01 Mar 2026 08:00:00 GMT</pubDate></item>")
    undated = rss("Fictional robot release", "https://lab.example/robot")
    offer = AsyncMock(return_value={"status": "unavailable"})
    ctx = context(
        tmp_path,
        [source],
        HTTP(response(dated), response(undated), response(undated)),
        offers=offer,
    )
    feed = plugin.Plugin()
    await feed.start(ctx)
    await feed.on_tick(None)
    ctx.model.generate.assert_not_awaited()
    assert all(call.args[1]["tier"] != "breaking" for call in offer.await_args_list)
    ctx.clock.advance(600)
    await feed.on_tick(None)
    assert offer.await_args.args[1]["tier"] == "breaking"
    await feed.stop()

    ctx.clock.advance(2 * 24 * 3600)
    offer.reset_mock()
    restored = plugin.Plugin()
    await restored.start(ctx)
    await restored.on_tick(None)
    ctx.model.generate.assert_awaited_once()
    assert all(call.args[1]["tier"] != "breaking" for call in offer.await_args_list)
    assert len((await restored.material()).sources) == 2


async def test_event_offers_upgrade_with_public_fields_and_restart_dedup(tmp_path):
    sources = [
        dict(id="configured-alias", type="hackernews", url="https://stories.example/v0/front.json"),
        dict(id="local-label", type="rss", url="https://publisher.example/rss", major=True),
    ]
    http = HTTP(
        response(b"[3]"),
        response(
            json.dumps(
                dict(
                    id=3,
                    type="story",
                    title="Fictional AI chip",
                    url="https://lab.example/news",
                    score=3,
                )
            ).encode()
        ),
        response(b"<rss/>"),
        response(b"[]"),
        response(rss()),
    )
    offer = AsyncMock(
        side_effect=[{"status": "accepted"}, {"status": "unavailable"}, {"status": "accepted"}]
    )
    ctx = context(tmp_path, sources, http, offers=offer)
    feeds = plugin.Plugin()
    await feeds.start(ctx)
    await feeds.on_tick(None)
    ctx.clock.advance(600)
    await feeds.on_tick(None)
    await feeds.stop()
    restored = plugin.Plugin()
    await restored.start(ctx)
    await restored.on_tick(None)
    await restored.on_tick(None)
    payloads = [c.args[1] for c in offer.await_args_list]
    assert [p["tier"] for p in payloads] == ["brief", "breaking", "breaking"]
    assert len({p["key"] for p in payloads}) == 1
    assert len(payloads[1]["sources"]) == 2
    assert "configured-alias" not in str(payloads) and "local-label" not in str(payloads)
    ctx.model.generate.assert_awaited_once()
    request = ctx.model.generate.await_args.args[0]
    assert not request.tools
    public = json.loads(request.messages[1].parts[0].text)
    assert all(set(r) == {"title", "source", "summary"} for r in public)
    assert {r["source"] for r in public} == {"stories.example", "publisher.example"}


async def test_filters_apply_to_cache_and_model(tmp_path):
    src = dict(id="feed", type="rss", url="https://lab.example/rss", major=True)
    config = dict(interests=["robot"], exclude=["spoiler"])
    ctx = context(
        tmp_path,
        [src],
        HTTP(
            response(rss(title="Fictional robot")),
            response(rss(title="Robot spoiler")),
            response(rss(title="Routine update")),
        ),
        offers=AsyncMock(return_value={"status": "accepted"}),
        **config,
    )
    feed = plugin.Plugin()
    await feed.start(ctx)
    for _ in range(3):
        await feed.on_tick(None)
        ctx.clock.advance(600)
    assert len(feed.book.events) == 1
    assert ctx.model.generate.await_count == 1
    ctx.config["exclude"] = ["robot"]
    restarted = plugin.Plugin()
    await restarted.start(ctx)
    assert not restarted.book.events and await restarted.material() is None


@pytest.mark.parametrize(
    "target, allowed",
    [
        ("https://api.github.com/search/repositories", True),
        ("http://api.github.com/search/repositories", False),
        ("https://other.example/search/repositories", False),
        ("https://api.github.com/user", False),
    ],
)
async def test_credential_stays_on_official_https_search_without_redirects(
    tmp_path, target, allowed
):
    src = dict(id="projects", type="github_rising", url=target)
    http = HTTP(response(b'{"items": []}'))
    ctx = context(tmp_path, [src], http, github_credential="fictional_ref")
    marker = "fictional_test_marker"
    ctx.credentials = SimpleNamespace(read=AsyncMock(return_value=marker))
    feed = plugin.Plugin()
    await feed.start(ctx)
    await feed.on_tick(None)
    if allowed:
        ctx.credentials.read.assert_awaited_once_with("fictional_ref")
        args = http.calls[0][2]
        assert (
            not args["follow_redirects"] and args["headers"]["Authorization"] == "Bearer " + marker
        )
    else:
        assert not http.calls
        ctx.credentials.read.assert_not_awaited()
        assert feed.poller.cache["projects"]["error"] == "invalid_credential_target"
    assert marker.encode() not in feed.cache.path.read_bytes()
    assert marker not in str(await feed.status(None))


async def test_burst_judgments_fall_back_to_brief_without_retry(tmp_path):
    import hashlib

    rows = "".join(
        f"<item><title>{hashlib.sha256(str(i).encode()).hexdigest()[:20]}</title><link>https://lab.example/{i}</link></item>"
        for i in range(12)
    )
    src = dict(id="major", type="rss", url="https://lab.example/rss", major=True)
    ctx = context(
        tmp_path,
        [src],
        HTTP(response(f"<rss>{rows}</rss>".encode())),
        offers=AsyncMock(return_value={"status": "accepted"}),
    )
    feed = plugin.Plugin()
    await feed.start(ctx)
    await feed.on_tick(None)
    ctx.clock.advance(61)
    await feed.on_tick(None)
    assert ctx.model.generate.await_count == 10
    assert (await feed.status(None))["json"]["tiers_today"] == dict(breaking=10, brief=2, passive=0)
