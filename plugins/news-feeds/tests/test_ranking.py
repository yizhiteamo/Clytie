from datetime import timedelta
from importlib import import_module
from unittest.mock import AsyncMock

import pytest

from .test_events import NOW, add, events

ranking = import_module(events.__name__ + ".ranking")


@pytest.mark.parametrize(
    ("result", "tier"), [(True, "breaking"), (False, "brief"), (TimeoutError(), "brief")]
)
async def test_major_public_judgment_runs_once(result, tier):
    book = events.EventBook({})
    event = add(book, "https://lab.example/new", major=True)
    decide = AsyncMock(
        return_value=result, side_effect=result if isinstance(result, Exception) else None
    )
    for _ in range(2):
        await ranking.assign(book.events, NOW, decide)
    assert event["tier"] == tier
    decide.assert_awaited_once()
    assert all(set(item) == {"title", "source", "summary"} for item in decide.await_args.args[0])


async def test_three_sites_and_fast_hn_top_three_are_candidates():
    book = events.EventBook({})
    title = "Fictional optical circuits break the previous speed record"
    for site in ("one", "two", "three"):
        event = add(book, f"https://{site}.example/post", title, source=site)
    later = NOW + timedelta(minutes=10)
    add(
        book,
        "https://paper.example/new",
        "A fast rising AI story",
        kind="hackernews",
        rank=2,
        score=10,
    )
    hn = add(
        book,
        "https://paper.example/new",
        "A fast rising AI story",
        kind="hackernews",
        rank=2,
        score=40,
        at=later,
    )
    judge = AsyncMock(return_value=True)
    await ranking.assign(book.events, later, judge)
    assert event["tier"] == hn["tier"] == "breaking"
    assert judge.await_count == 2


@pytest.mark.parametrize("signal", ["major", "sites", "hackernews"])
def test_urgent_uses_two_hour_window_for_each_signal(signal):
    stamp = NOW.timestamp()
    row = dict(
        at=stamp - 7200, publisher="first", major=signal == "major", kind=signal, rank=2, rate=70
    )
    entries = [row]
    if signal == "sites":
        entries += [dict(row, publisher=site, at=stamp) for site in ("second", "third")]
    assert ranking.urgent(entries, stamp)
    row["at"] -= 1
    assert not ranking.urgent(entries, stamp)


async def test_daily_candidates_and_passive_entries():
    book = events.EventBook({})
    hf = add(
        book,
        "https://huggingface.co/papers/9912.0123",
        "Fictional optics preprint",
        kind="hf_papers",
        score=8,
    )
    gh = add(
        book,
        "https://github.com/example/wheels",
        "Fictional robot components",
        kind="github_rising",
        score=8,
    )
    later = NOW + timedelta(hours=1)
    add(
        book,
        "https://github.com/example/wheels",
        "Fictional robot components",
        kind="github_rising",
        score=40,
        at=later,
    )
    repo = add(
        book,
        "https://github.com/example/fictitious",
        "Fictional library released",
        source="publisher",
    )
    add(book, "https://github.com/example/fictitious/releases", "Another bulletin", source="feed")
    hot = add(
        book, "https://front.example/n", "Fictional processors", kind="hackernews", rank=5, score=20
    )
    other = add(book, "https://boring.example/p", "Routine update")
    judge = AsyncMock()
    await ranking.assign(book.events, later, judge)
    assert all(e["tier"] == "brief" for e in (hf, gh, repo, hot))
    assert other["tier"] == "passive"
    judge.assert_not_awaited()


def test_recent_news_material_includes_distinct_sources_and_no_single_url():
    book = events.EventBook({})
    title = "Fictional groups announce a small language model"
    for i in range(2):
        add(book, f"https://source{i}.example/story", title, source=str(i))
    result = ranking.recent_material(book.events)
    assert result.url is None
    assert len(result.sources) == 2
    for row in result.sources:
        assert row.url in result.text
    assert ranking.recent_material({}) is None


async def test_new_public_evidence_upgrades_a_previous_negative_once():
    book = events.EventBook({})
    story = add(book, "https://one.example/article", "Fictional robotics announcement", major=True)
    judge = AsyncMock(side_effect=[False, True])
    await ranking.assign(book.events, NOW, judge)
    assert story["tier"] == "brief"
    await ranking.assign(book.events, NOW, judge)
    add(book, "https://one.example/article", "Fictional independent confirmation", source="second")
    await ranking.assign(book.events, NOW, judge)
    await ranking.assign(book.events, NOW, judge)
    assert story["tier"] == "breaking" and judge.await_count == 2
