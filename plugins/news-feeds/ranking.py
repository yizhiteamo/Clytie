"""Rank public events and build the light tool's multi-source material."""

import hashlib
import json

from clytie.sdk.types import Material, MaterialSource


def urgent(entries, stamp):
    recent = [row for row in entries if stamp - 7200 <= row["at"] <= stamp]
    sites = {row["publisher"] for row in recent}
    return len(sites) >= 3 or any(
        row["major"]
        or (row["kind"] == "hackernews" and 1 <= row["rank"] <= 3 and row["rate"] >= 60)
        for row in recent
    )


def leaders(events):
    """Select the top three papers by votes and repositories by observed stars/hour per source."""
    groups = {}
    latest = {}
    for key, event in events.items():
        for row in event["entries"]:
            if row["kind"] in {"hf_papers", "github_rising"}:
                value = row["score"] if row["kind"] == "hf_papers" else row["rate"]
                group = (row["source"], row["kind"])
                if row["observed"] > latest.get(group, -1):
                    groups[group] = {}
                    latest[group] = row["observed"]
                if value > 0 and row["observed"] == latest[group]:
                    scores = groups[group]
                    scores[key] = max(value, scores.get(key, 0))
    return {
        key
        for scores in groups.values()
        for key in sorted(scores, key=scores.get, reverse=True)[:3]
    }


async def assign(events, now, decide):
    changed = set()
    best = leaders(events)
    for key, event in events.items():
        before = (event["tier"], event.get("judged_evidence"))
        rows = event["entries"]
        eligible = urgent(rows, now.timestamp())
        if eligible and decide is not None and not event.get("judgment"):
            public = [
                dict(title=r["title"], source=r["publisher"], summary=r["summary"])
                for r in sorted(rows, key=lambda row: (row["at"], row["publisher"]), reverse=True)[
                    :10
                ]
            ]
            evidence = hashlib.sha256(json.dumps(public, sort_keys=True).encode()).hexdigest()
            if evidence != event.get("judged_evidence"):
                event["judged_evidence"] = evidence
                try:
                    event["judgment"] = await decide(public) is True
                except Exception:
                    event["judgment"] = False
        if eligible and event.get("judgment"):
            event["tier"] = "breaking"
        elif (
            "judgment" in event
            or key in best
            or (any(r["resource"] for r in rows) and len({r["publisher"] for r in rows}) >= 2)
            or any(r["kind"] == "hackernews" and 1 <= r["rank"] <= 10 for r in rows)
        ):
            event["tier"] = "brief"
        else:
            event["tier"] = "passive"
        if (event["tier"], event.get("judged_evidence")) != before:
            changed.add(key)
    return changed


def recent_material(events, now=None):
    ordered = sorted(
        events.values(), key=lambda e: max(r["at"] for r in e["entries"]), reverse=True
    )
    sources = []
    lines = []
    seen = set()
    for event in ordered:
        for row in event["entries"]:
            if now and row["at"] < now.timestamp() - 7 * 24 * 3600:
                continue
            if row["url_key"] in seen:
                continue
            seen.add(row["url_key"])
            sources.append(
                MaterialSource(title=row["title"], url=row["url"], published_at=row["published"])
            )
            lines.append(
                "\n".join(
                    filter(None, (row["title"], row["url"], row["published"], row["summary"]))
                )
            )
            if len(sources) == 10:
                break
        if len(sources) == 10:
            break
    if sources:
        return Material(title="近期科技新闻", text="\n\n".join(lines), sources=sources)
    return None
