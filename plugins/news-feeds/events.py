"""Group public feed entries and retain their individual publication times."""

import hashlib
import re
from collections import Counter
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DAY = 24 * 3600
HF_RESERVED = {
    "blog",
    "papers",
    "spaces",
    "datasets",
    "docs",
    "api",
    "collections",
    "posts",
    "learn",
    "organizations",
}


def identity(url):
    parts = urlsplit(url)
    host = parts.hostname.lower()
    path = parts.path.rstrip("/") or "/"
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}
    )
    authority = host if ":" not in host else f"[{host}]"
    if parts.port and (parts.scheme, parts.port) not in {("http", 80), ("https", 443)}:
        authority += f":{parts.port}"
    canonical = urlunsplit((parts.scheme, authority, path, urlencode(query), ""))
    segments = path.strip("/").split("/")
    resource = None
    if (
        host in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}
        and segments[0] in {"pdf", "abs", "html"}
    ) or (host == "huggingface.co" and segments[0] == "papers"):
        match = re.fullmatch(
            r"(\d{4}\.\d{4,5}|[a-z.-]+/\d{7})(?:v\d+)?(?:\.pdf)?", "/".join(segments[1:])
        )
        if match:
            resource = "paper:" + match[1]
    elif host == "github.com" and len(segments) >= 2:
        resource = "repo:" + "/".join(segments[:2]).removesuffix(".git").lower()
    elif host == "huggingface.co" and len(segments) >= 2 and segments[0] not in HF_RESERVED:
        resource = "model:" + "/".join(segments[:2])
    return canonical, resource


TECH = (
    "AI",
    "artificial intelligence",
    "model",
    "machine learning",
    "software",
    "robot",
    "chip",
    "GPU",
    "CPU",
    "hardware",
    "research",
    "technology",
    "computer",
    "programming",
    "language model",
    "neural",
    "LLM",
    "open source",
    "linux",
    "python",
    "javascript",
    "compiler",
    "database",
    "security",
    "quantum",
    "inference",
    "training",
    "processor",
    "科技",
    "人工智能",
    "模型",
)


def terms(words):
    # Short Latin abbreviations must stand alone (e.g. 'AI' inside 'said').
    return [
        re.compile(
            r"\b" + re.escape(w) + r"\b" if w.isascii() and len(w) <= 3 else re.escape(w),
            flags=re.IGNORECASE,
        )
        for w in words
        if w
    ]


def normalized(title):
    return re.sub(r"[\W_]+", " ", title.casefold()).strip()


def grams(title):
    return {title[i : i + 4] for i in range(len(title) - 3)}


def similar(left, right):
    if left == right:
        return bool(left)
    if min(len(left), len(right)) < 14:
        return False
    matcher = SequenceMatcher(None, left, right, autojunk=False)
    return matcher.real_quick_ratio() >= 0.90 and matcher.ratio() >= 0.90


class EventBook:
    def __init__(self, events, exclude=(), interests=None):
        self.events = events
        self.exclude = terms(exclude)
        self.interests = terms(interests) if interests is not None else None
        self.default_topics = terms(TECH)
        self.dirty = set()
        self.reindex()

    def permits(self, title, summary, kind):
        text = title + " " + summary
        if any(term.search(text) for term in self.exclude):
            return False
        wanted = self.interests
        if wanted is None and kind == "hackernews":
            wanted = self.default_topics
        return not wanted or any(term.search(text) for term in wanted)

    def reindex(self):
        self.identities = {}
        self.substrings = {}
        self.titles = {}
        for key, event in self.events.items():
            for row in event["entries"]:
                self.index(key, row)

    def index(self, key, row):
        self.identities[row["url_key"]] = key
        if row["resource"]:
            self.identities[row["resource"]] = key
        title = normalized(row["title"])
        first = title not in self.titles
        self.titles.setdefault(title, {}).setdefault(key, set()).add(row["at"])
        if first:
            for part in grams(title):
                self.substrings.setdefault(part, set()).add(title)

    def match(self, url, resource, title, at):
        direct = self.identities.get(url) or self.identities.get(resource)
        title = normalized(title)
        if direct and direct in self.titles.get(title, {}):
            return self.events[direct]
        candidates = {title} if title in self.titles else set()
        # Index overlapping four-character sequences before applying the similarity cutoff.
        parts = grams(title)
        overlaps = Counter(
            candidate for part in parts for candidate in self.substrings.get(part, ())
        )
        candidates.update(
            candidate for candidate, count in overlaps.items() if count >= len(parts) * 0.30
        )
        matches = {direct} if direct else set()
        for candidate in sorted(candidates):
            within = [
                key
                for key, stamps in self.titles[candidate].items()
                if any(abs(stamp - at) <= 2 * DAY for stamp in stamps)
            ]
            if within and similar(title, candidate):
                matches.update(within)
        if not matches:
            return None
        if len(matches) == 1:
            return self.events[matches.pop()]

        # New reporting can connect two previously distinct events. Keep the key already
        # known to the delivery service, preferring an existing breaking offer.
        def precedence(key):
            event = self.events[key]
            return (
                event.get("offered") == "breaking",
                bool(event.get("offered")),
                -event["first_seen"],
                key,
            )

        kept_key = max(matches, key=precedence)
        kept = self.events[kept_key]
        for key in matches - {kept_key}:
            other = self.events.pop(key)
            kept["entries"].extend(other["entries"])
            kept["first_seen"] = min(kept["first_seen"], other["first_seen"])
            if other.get("judgment"):
                kept["judgment"] = True
            self.dirty.add(key)
        self.dirty.add(kept_key)
        self.reindex()
        return kept

    def prune(self, now, sources=None):
        cutoff = now.timestamp() - 7 * DAY
        changed = False
        for key, event in list(self.events.items()):
            rows = event["entries"]
            kept = [
                r
                for r in rows
                if r["at"] >= cutoff
                and (
                    sources is None
                    or (
                        r["source"] in sources and self.permits(r["title"], r["summary"], r["kind"])
                    )
                )
            ]
            if len(kept) == len(rows):
                continue
            changed = True
            self.dirty.add(key)
            if kept:
                event["entries"] = kept
            else:
                del self.events[key]
        if changed:
            self.reindex()

    def refresh_front(self, source_id, items, now):
        current = {item["url"]: item["rank"] for item in items.values() if item.get("url")}
        for key, event in self.events.items():
            for row in event["entries"]:
                if row["source"] != source_id or row["observed"] == now.timestamp():
                    continue
                rank = current.get(row["url"], 0)
                if rank or row["rank"]:
                    row.update(rank=rank, rate=0.0, observed=now.timestamp())
                    self.dirty.add(key)

    def add(self, source, row, now):
        stamp = now.timestamp()
        at = row.published.timestamp() if row.published else stamp
        if (
            at < stamp - 7 * DAY
            or at > stamp + DAY
            or not self.permits(row.title, row.summary, source["type"])
        ):
            return None
        url, resource = identity(row.url)
        event = self.match(url, resource, row.title, at)
        if event is None:
            key = hashlib.sha256((resource or url).encode()).hexdigest()
            event = self.events[key] = {
                "key": key,
                "entries": [],
                "tier": "passive",
                "first_seen": stamp,
            }
        entries = event["entries"]
        old = next(
            (r for r in entries if r["source"] == source["id"] and r["url_key"] == url), None
        )
        if old and row.published is None:
            at = old["at"]
        rate = 0.0
        if old and stamp > old["observed"]:
            rate = (row.score - old["score"]) * 3600 / (stamp - old["observed"])
        elif source["type"] == "github_rising" and stamp > at:
            rate = row.score * 3600 / max(3600, stamp - at)
        record = dict(
            title=row.title,
            url=row.url,
            url_key=url,
            resource=resource,
            summary=row.summary,
            at=at,
            published=row.published.isoformat() if row.published else None,
            score=row.score,
            rank=row.rank,
            observed=stamp,
            rate=rate,
            source=source["id"],
            publisher=urlsplit(source.get("url", row.url)).hostname,
            kind=source["type"],
            major=source.get("major", False),
        )
        replaced = old and (old["title"], old["at"]) != (row.title, at)
        if old:
            old.update(record)
        else:
            entries.append(record)
        if replaced:
            self.reindex()
        else:
            self.index(event["key"], record)
        self.dirty.add(event["key"])
        return event
