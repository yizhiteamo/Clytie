import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from clytie.sdk.testing import MockClock
from clytie.sdk.types import HttpResponse


def module():
    directory = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "fixture_news_polling",
        directory / "polling.py",
        submodule_search_locations=[str(directory)],
    )
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


polling = module()
URL = "https://bulletin.example/feed"
RSS = b"<rss><channel><item><title>Model release</title><link>https://lab.example/post</link></item></channel></rss>"


def response(body=b"", status=200, **headers):
    return HttpResponse(body=body, status=status, headers=headers)


class HTTP:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


async def test_conditional_feed_and_source_schedule():
    clock = MockClock()
    http = HTTP(
        response(RSS, ETag='W/"fictional"', **{"Last-Modified": "Tue, 03 Feb 2026 14:00:00 GMT"}),
        response(status=304),
    )
    cache = {}
    src = dict(id="bulletin", type="rss", url=URL)
    poll = polling.Poller(SimpleNamespace(http=http, clock=clock), cache)
    assert len(await poll.read(src)) == 1
    assert not await poll.read(src)
    clock.advance(600)
    assert not await poll.read(src)
    assert http.calls[1][2]["headers"] == {
        "If-None-Match": 'W/"fictional"',
        "If-Modified-Since": "Tue, 03 Feb 2026 14:00:00 GMT",
    }
    assert cache["bulletin"]["last_success"] == clock.now().timestamp()


async def test_rate_limit_does_not_block_another_source():
    clock = MockClock()
    http = HTTP(response(status=429, **{"Retry-After": "700"}), response(RSS), response(RSS))
    cache = {}
    poll = polling.Poller(SimpleNamespace(http=http, clock=clock), cache)
    first = dict(id="one", type="rss", url=URL)
    second = dict(id="two", type="arxiv", url="https://archive.example/rss")
    assert not await poll.read(first)
    assert cache["one"]["error"] == "rate_limited"
    assert len(await poll.read(second)) == 1
    clock.advance(600)
    assert not await poll.read(first)
    clock.advance(100)
    assert len(await poll.read(first)) == 1


async def test_hn_unchanged_front_page_still_samples_story_heat():
    clock = MockClock()

    def story(score):
        return response(
            json.dumps(
                dict(
                    type="story",
                    title="Fictional model",
                    url="https://lab.example/model",
                    score=score,
                    time=clock.now().timestamp(),
                )
            ).encode()
        )

    http = HTTP(response(b"[7]", ETag='"same"'), story(12), response(status=304), story(31))
    poll = polling.Poller(SimpleNamespace(clock=clock, http=http), {})
    src = dict(
        id="front", type="hackernews", url="https://hacker-news.firebaseio.com/v0/topstories.json"
    )
    assert (await poll.read(src))[0].score == 12
    clock.advance(600)
    assert (await poll.read(src))[0].score == 31
    assert http.calls[1][1].endswith("/v0/item/7.json")


async def test_blocked_public_target_is_isolated_and_failed_parse_retries_validators():
    from unittest.mock import AsyncMock

    clock = MockClock()
    http = SimpleNamespace(
        request=AsyncMock(
            side_effect=[
                ValueError("blocked_address"),
                response(RSS, ETag='"good"'),
                response(b"<bad", ETag='"bad"'),
                response(RSS, ETag='"next"'),
            ]
        )
    )
    cache = {}
    poll = polling.Poller(SimpleNamespace(clock=clock, http=http), cache)
    assert not await poll.read(dict(id="blocked", type="rss", url="http://127.0.0.1/"))
    assert cache["blocked"]["error"] == "blocked_address"
    src = dict(id="allowed", type="rss", url=URL)
    assert await poll.read(src)
    clock.advance(600)
    assert not await poll.read(src)
    assert cache["allowed"]["error"] == "invalid_feed"
    clock.advance(600)
    assert await poll.read(src)
    assert http.request.await_args.kwargs["headers"] == {"If-None-Match": '"good"'}
