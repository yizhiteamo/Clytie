import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "fictional_feed_events", root / "events.py", submodule_search_locations=[str(root)]
)
events = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = events
spec.loader.exec_module(events)
Entry = importlib.import_module(events.__name__ + ".sources").Entry

NOW = datetime(2026, 3, 3, 9, tzinfo=timezone.utc)


def add(index, url, title="Fictional bulletin", *, at=NOW, source="lab", **kwargs):
    row = Entry(title, url, "", at, kwargs.get("score", 0), kwargs.get("rank", 0))
    return index.add(
        {
            "id": source,
            "url": f"https://{source}.example/rss",
            "type": kwargs.get("kind", "rss"),
            "major": kwargs.get("major", False),
        },
        row,
        at,
    )


@pytest.mark.parametrize(
    ("one", "two"),
    [
        ("https://LAB.example/a?utm_source=feed&edition=2#top", "https://lab.example/a?edition=2"),
        ("https://arxiv.org/pdf/9912.0123v2.pdf", "https://huggingface.co/papers/9912.0123"),
        (
            "https://github.com/example/fictional/releases/tag/v2",
            "https://github.com/EXAMPLE/fictional",
        ),
        (
            "https://huggingface.co/example/fictional-model",
            "https://huggingface.co/example/fictional-model/blob/main/README.md",
        ),
    ],
)
def test_identity_merges_distinct_publications(one, two):
    book = events.EventBook({})
    first = add(book, one)
    second = add(book, two, "A completely different headline", source="other")
    assert first["key"] == second["key"]
    assert len(book.events) == 1


def test_title_similarity_has_forty_eight_hour_window():
    book = events.EventBook({})
    title = "Fictional researchers unveil efficient model for small robots"
    first = add(book, "https://one.example/a", title)
    second = add(book, "https://two.example/b", title + "!", at=NOW + timedelta(hours=47))
    third = add(book, "https://three.example/c", title, at=NOW + timedelta(hours=97))
    assert first["key"] == second["key"] != third["key"]


def test_prune_removes_only_old_entries_and_excluded_entries_never_enter_book():
    book = events.EventBook({}, exclude=["spoiler"])
    assert add(book, "https://lab.example/s", "Latest SPOILER") is None
    old = add(book, "https://lab.example/a")
    add(
        book, "https://lab.example/a", "Latest bulletin", source="other", at=NOW + timedelta(days=6)
    )
    book.prune(NOW + timedelta(days=8))
    assert [r["source"] for r in book.events[old["key"]]["entries"]] == ["other"]
    book.prune(NOW + timedelta(days=14))
    assert not book.events


def test_same_feed_aliases_are_one_site_and_three_independent_publishers_can_link_one_url():
    from .test_ranking import ranking

    book = events.EventBook({})
    for num, publisher in enumerate(["one", "one", "two", "three"]):
        event = book.add(
            dict(id=f"feed{num}", url=f"https://{publisher}.example/rss", type="rss"),
            Entry("Fictional release", "https://origin.example/story", "", NOW, 0, 0),
            NOW,
        )
        assert ranking.urgent(event["entries"], NOW.timestamp()) == (num == 3)


def test_updated_headline_connects_known_link_to_previously_separate_event():
    book = events.EventBook({})
    first = add(book, "https://one.example/article", "Unconfirmed report from the Arctic lab")
    second = add(
        book, "https://two.example/news", "Fictional astronomy team releases a new catalogue"
    )
    assert len(book.events) == 2
    second["offered"] = "brief"
    merged = add(book, "https://one.example/article", second["entries"][0]["title"])
    assert len(book.events) == 1 and merged["key"] == second["key"]
    assert len(merged["entries"]) == 2 and first["key"] in book.dirty
