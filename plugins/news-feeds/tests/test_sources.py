import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


def load_module(name):
    path = Path(__file__).resolve().parents[1] / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"fixture_news_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sources = load_module("sources")
NOW = datetime(2026, 2, 4, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("kind", ["rss", "arxiv"])
def test_rss_skips_bad_item_and_reads_public_summary(kind):
    body = b"""<rss><channel><item><title>Missing link</title></item>
    <item><title>Fictional optics paper</title><link>https://paper.example/demo</link>
    <description>&lt;p&gt;A public summary.&lt;/p&gt;</description>
    <pubDate>Tue, 03 Feb 2026 14:00:00 GMT</pubDate></item></channel></rss>"""
    rows = sources.feed_entries(body, kind)
    assert len(rows) == 1
    assert rows[0].title == "Fictional optics paper"
    assert rows[0].summary == "A public summary."
    assert rows[0].published.hour == 14


def test_atom_uses_alternate_link_and_publication_date():
    body = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Release</title>
    <link rel="self" href="https://feed.example/entry/one"/>
    <link rel="alternate" href="https://project.example/release"/>
    <updated>2026-02-04T11:00:00Z</updated><summary>Notes</summary></entry></feed>"""
    assert sources.feed_entries(body, "rss")[0].url == "https://project.example/release"


def test_hn_story_keeps_rank_score_and_skips_deleted_items():
    story = dict(
        type="story",
        title="Synthetic research",
        url="https://lab.example/news",
        score=31,
        time=NOW.timestamp(),
    )
    assert sources.hn_entry(dict(story, deleted=True), 1) is None
    row = sources.hn_entry(story, 2)
    assert (row.title, row.score, row.rank) == ("Synthetic research", 31, 2)


def test_papers_handles_bad_rows_and_keeps_votes():
    record = {
        "paper": {
            "id": "9911.0001",
            "title": "Fictional paper",
            "summary": "Summary",
            "publishedAt": NOW.isoformat(),
            "upvotes": 19,
        }
    }
    rows = sources.json_entries(json.dumps([None, {}, record]).encode(), "hf_papers")
    assert len(rows) == 1
    assert rows[0].url.endswith("/papers/9911.0001") and rows[0].score == 19


def test_github_keeps_repo_creation_and_star_sample():
    record = dict(
        full_name="fictional/project",
        html_url="https://github.com/fictional/project",
        description="Synthetic",
        created_at=NOW.isoformat(),
        stargazers_count=42,
    )
    rows = sources.json_entries(json.dumps({"items": [{}, record]}).encode(), "github_rising")
    assert [(row.title, row.score, row.published) for row in rows] == [
        ("fictional/project", 42, NOW)
    ]


def test_bad_rows_do_not_hide_a_following_paper():
    def paper(identifier):
        return {"paper": {"id": identifier, "title": "Fictional task"}}

    body = json.dumps([paper(9912.1234), paper("9912.0123")]).encode()
    assert len(sources.json_entries(body, "hf_papers")) == 1
    assert sources.entry("Fictional item", "https://lab.example/x", "", "invalid") is None
    assert sources.entry("Fictional item", "https://lab.example/x", "", None, 10**400) is None
