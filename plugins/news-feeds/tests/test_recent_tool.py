from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace

import pytest

from clytie.sdk import CancelToken

from .test_events import NOW, add, events

recent = import_module(events.__name__ + ".recent")


@pytest.mark.parametrize("expired", [False, True])
async def test_recent_news_light_plan_and_result(expired):
    book = events.EventBook({})
    add(book, "https://example.org/demo", "Fictional release")
    when = NOW + timedelta(days=8 if expired else 0)

    async def material():
        return import_module(events.__name__ + ".ranking").recent_material(book.events, when)

    plugin = SimpleNamespace(material=material)
    tool = recent.RecentNews(plugin)
    call = SimpleNamespace(call_id="fixture-call", arguments={})
    plan = await tool.prepare(call, None)
    assert not plan.heavy
    result = await tool.execute(plan, None, CancelToken())
    assert result.status == "ok"
    if expired:
        assert result.material is None
    else:
        assert result.material.url is None
        assert [s.url for s in result.material.sources] == ["https://example.org/demo"]
