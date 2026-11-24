"""Conditional polling. Validators advance only after the response parses successfully."""

import asyncio
import json
from datetime import timedelta
from urllib.parse import urlencode, urlsplit
from xml.etree.ElementTree import ParseError

from clytie.sdk import CredentialUnavailable

from .sources import feed_entries, hn_entry, json_entries, timestamp


class SourceFailure(Exception):
    def __init__(self, code, retry_at=0):
        super().__init__(code)
        self.code = code
        self.retry_at = retry_at


def retry_at(headers, now):
    value = headers.get("retry-after", "")
    date = timestamp(value)
    relative = now + int(value) if value.isascii() and value.isdigit() and len(value) <= 12 else 0
    reset = headers.get("x-ratelimit-reset", "")
    absolute = int(reset) if reset.isascii() and reset.isdigit() and len(reset) <= 12 else 0
    return max(relative, date.timestamp() if date else 0, absolute)


def validators(headers):
    values = {}
    for key, field in (("etag", "If-None-Match"), ("last-modified", "If-Modified-Since")):
        if key in headers:
            values[field] = headers[key]
    return values


class Poller:
    def __init__(self, ctx, cache):
        self.ctx = ctx
        self.cache = cache

    async def get(self, url, previous, credential=None):
        headers = dict(previous)
        if credential:
            target = urlsplit(url)
            if (target.scheme, target.netloc, target.path) != (
                "https",
                "api.github.com",
                "/search/repositories",
            ):
                raise SourceFailure("invalid_credential_target")
            # One origin receives this header. Never persist request headers.
            credentials = getattr(self.ctx, "credentials", None)
            if credentials is None:
                raise SourceFailure("credential_unavailable")
            headers["Authorization"] = "Bearer " + await credentials.read(credential)
        response = await self.ctx.http.request(
            "GET",
            url,
            headers=headers,
            timeout_s=15,
            max_bytes=16 * 1024 * 1024,
            follow_redirects=not bool(credential),
        )
        fields = {k.lower(): v for k, v in response.headers.items()}
        if response.status == 429 or (
            response.status == 403
            and ("retry-after" in fields or fields.get("x-ratelimit-remaining") == "0")
        ):
            raise SourceFailure("rate_limited", retry_at(fields, self.ctx.clock.now().timestamp()))
        if response.status != 304 and not 200 <= response.status < 300:
            raise SourceFailure("http_" + str(response.status))
        return response, fields

    async def read(self, source):
        now = self.ctx.clock.now()
        kind = source["type"]
        state = self.cache.setdefault(source["id"], {})
        if now.timestamp() < state.get("next_poll", 0):
            return []
        state["next_poll"] = now.timestamp() + (600 if kind in {"rss", "hackernews"} else 3600)
        try:
            rows = await self.fetch(source, state, now)
        except SourceFailure as exc:
            state.update(error=exc.code, next_poll=max(state["next_poll"], exc.retry_at))
        except CredentialUnavailable:
            state["error"] = "credential_unavailable"
        except TimeoutError:
            state["error"] = "timeout"
        except OSError:
            state["error"] = "fetch_failed"
        except (ValueError, ParseError, UnicodeError, RecursionError) as exc:
            state["error"] = (
                "blocked_address" if exc.args == ("blocked_address",) else "invalid_feed"
            )
        else:
            state.update(last_success=now.timestamp(), error=None)
            return rows
        return []

    async def fetch(self, source, state, now):
        kind, url = source["type"], source["url"]
        if kind == "github_rising":
            query = (
                "topic:artificial-intelligence created:>="
                + (now.date() - timedelta(days=7)).isoformat()
            )
            url += "?" + urlencode(dict(q=query, sort="stars", order="desc", per_page=100))
        previous = state.get("validators", {}) if url == state.get("url") else {}
        response, headers = await self.get(
            url, previous, source.get("credential") if kind == "github_rising" else None
        )
        if kind == "hackernews":
            if response.status == 304:
                ids = state.get("front_ids", [])
            else:
                data = json.loads(response.body)
                if not isinstance(data, list):
                    raise ValueError("invalid_feed")
                ids = [i for i in data[:30] if type(i) is int and i > 0]
            rows = await self.stories(source, ids, state)
            state["front_ids"] = ids
        elif response.status == 304:
            return []
        elif kind in {"rss", "arxiv"}:
            rows = await asyncio.to_thread(feed_entries, response.body, kind)
        else:
            rows = await asyncio.to_thread(json_entries, response.body, kind)
        if response.status != 304:
            state.update(url=url, validators=validators(headers))
        return rows

    async def stories(self, source, ids, state):
        items = dict(state.get("items", {}))
        live = {str(i) for i in ids}
        for key in list(items):
            if key not in live:
                del items[key]
        rows = []
        for rank, num in enumerate(ids, 1):
            key = str(num)
            url = source["url"].rsplit("/", 1)[0] + f"/item/{num}.json"
            saved = items.get(key, {})
            previous = saved.get("validators", {}) if rank == saved.get("rank") else {}
            try:
                response, headers = await self.get(url, previous)
            except SourceFailure as exc:
                if exc.code in {"http_404", "http_410"}:
                    items.pop(key, None)
                    continue
                raise
            if response.status == 304:
                continue
            try:
                record = json.loads(response.body)
            except (ValueError, UnicodeError):
                continue
            row = hn_entry(record, rank)
            if row:
                rows.append(row)
            items[key] = {
                "rank": rank,
                "validators": validators(headers),
                "url": row.url if row else None,
            }
        state["items"] = items
        return rows
