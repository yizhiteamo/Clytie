"""Parse the public fields used for subscriptions. A malformed entry is local to that entry."""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit
from xml.etree import ElementTree


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def clean(value, limit):
    if not isinstance(value, str):
        return ""
    parser = _Text()
    try:
        parser.feed(value)
        parser.close()
    except (ValueError, AssertionError):
        return ""
    return " ".join(" ".join(parser.parts).split())[:limit]


def timestamp(value):
    try:
        if type(value) in (int, float):
            return datetime.fromtimestamp(value, timezone.utc)
        if isinstance(value, str):
            try:
                result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                result = parsedate_to_datetime(value)
            if result and result.tzinfo is not None:
                return result.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError, TypeError):
        pass
    return None


@dataclass(frozen=True)
class Entry:
    title: str
    url: str
    summary: str
    published: datetime | None
    score: int
    rank: int


def entry(title, url, summary, date, score=0, rank=0):
    title = clean(title, 300)
    if not title or not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 80, 443)
        ):
            return None
    except ValueError:
        return None
    if type(score) is not int or not 0 <= score <= 2**53:
        return None
    published = timestamp(date)
    if date is not None and published is None:
        return None
    return Entry(title, url, clean(summary, 1200), published, score, rank)


def feed_entries(body, kind):
    root = ElementTree.fromstring(body)
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    if root.tag not in {"rss", "RDF", "feed"}:
        raise ValueError("invalid_feed")
    rows = []
    for node in root.iter():
        if node.tag not in {"item", "entry"}:
            continue
        link = node.findtext("link")
        if node.tag == "entry":
            link = next(
                (
                    el.get("href")
                    for el in node.findall("link")
                    if el.get("rel", "alternate") == "alternate" and el.get("href")
                ),
                None,
            )
        row = entry(
            node.findtext("title"),
            link,
            node.findtext("summary") or node.findtext("description"),
            node.findtext("published")
            or node.findtext("pubDate")
            or node.findtext("date")
            or node.findtext("updated"),
        )
        if row:
            rows.append(row)
    return rows


def hn_entry(story, rank):
    if (
        not isinstance(story, dict)
        or story.get("type") != "story"
        or story.get("deleted")
        or story.get("dead")
    ):
        return None
    url = story.get("url")
    if not url and type(story.get("id")) is int:
        url = f"https://news.ycombinator.com/item?id={story['id']}"
    return entry(
        story.get("title"), url, story.get("text"), story.get("time"), story.get("score", 0), rank
    )


def json_entries(body, kind):
    data = json.loads(body)
    if kind == "github_rising":
        data = data.get("items") if isinstance(data, dict) else None
    if not isinstance(data, list):
        raise ValueError("invalid_feed")
    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if kind == "hf_papers":
            paper = item.get("paper")
            if (
                not isinstance(paper, dict)
                or not isinstance(paper.get("id"), str)
                or not re.fullmatch(r"\d{4}\.\d{4,5}(?:v\d+)?", paper["id"])
            ):
                continue
            row = entry(
                paper.get("title"),
                "https://huggingface.co/papers/" + paper["id"],
                paper.get("summary"),
                paper.get("publishedAt") or item.get("publishedAt"),
                paper.get("upvotes", 0),
            )
        else:
            row = entry(
                item.get("full_name"),
                item.get("html_url"),
                item.get("description"),
                item.get("created_at"),
                item.get("stargazers_count", 0),
            )
        if row:
            rows.append(row)
    return rows
