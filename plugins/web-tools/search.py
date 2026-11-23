"""Conversion of Responses web search citations into provider-independent results."""

import asyncio
import json
from dataclasses import dataclass
from datetime import date, timedelta

from clytie.sdk import CredentialUnavailable
from clytie.sdk.types import Material, MaterialSource

from .pages import WebError, fetch, publication, valid_url


@dataclass
class Hit:
    title: str
    url: str
    summary: str
    published_at: str | None


def citations(output):
    hits = {}
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue
            previous = 0
            for citation in part.get("annotations") or []:
                if not isinstance(citation, dict) or citation.get("type") != "url_citation":
                    continue
                url, title = citation.get("url"), citation.get("title")
                if not isinstance(url, str) or not valid_url(url) or url in hits:
                    continue
                start, end = citation.get("start_index"), citation.get("end_index")
                if (
                    type(start) is not int
                    or type(end) is not int
                    or not 0 <= start <= end <= len(text)
                ):
                    continue
                summary = text[previous:start].strip(" \n\t.,;。；") or text[start:end].strip()
                previous = end
                hits[url] = Hit(
                    title.strip() if isinstance(title, str) and title.strip() else url,
                    url,
                    summary,
                    publication(citation.get("published_at")),
                )
                if len(hits) == 10:
                    return list(hits.values())
    return list(hits.values())


class OpenAISearch:
    def __init__(self, ctx):
        self.ctx = ctx

    async def search(self, query, recent_days, max_results):
        today = self.ctx.clock.now().date()
        since = today - timedelta(days=recent_days - 1) if recent_days else None
        prompt = (
            f"Search for {query!r}. Return a short summary with an inline citation for each source."
        )
        if since:
            prompt += f" Only sources published from {since} through {today} inclusive."
        try:
            key = await self.ctx.credentials.read(self.ctx.config["credential"])
            response = await self.ctx.http.request(
                "POST",
                "https://api.openai.com/v1/responses",
                headers=dict(
                    [("Authorization", "Bearer " + key), ("Content-Type", "application/json")]
                ),
                body=json.dumps(
                    {
                        "model": self.ctx.config["model"],
                        "tools": [{"type": "web_search"}],
                        "tool_choice": "required",
                        "input": prompt,
                        "store": False,
                    }
                ).encode(),
                follow_redirects=False,
            )
        except TimeoutError as exc:
            raise WebError("timeout") from exc
        except (AttributeError, CredentialUnavailable) as exc:
            raise WebError("credential_unavailable") from exc
        except (ValueError, OSError) as exc:
            raise WebError("search_failed") from exc
        if response.status != 200:
            raise WebError(f"http_{response.status}")
        try:
            payload = json.loads(response.body)
            if not isinstance(payload, dict) or not isinstance(payload.get("output"), list):
                raise ValueError
            if payload.get("status", "completed") != "completed":
                raise ValueError
            hits = citations(payload["output"])
        except (ValueError, TypeError) as exc:
            raise WebError("search_failed") from exc
        if not since:
            hits = hits[:max_results]

        async def verify(hit):
            if not hit.published_at:
                try:
                    hit.published_at = await fetch(self.ctx, hit.url, metadata=True)
                except (WebError, OSError, ValueError):
                    pass
            if since and (
                not hit.published_at
                or not since <= date.fromisoformat(hit.published_at[:10]) <= today
            ):
                return None
            return hit

        checked = [h for h in await asyncio.gather(*(verify(h) for h in hits)) if h][:max_results]
        if not checked:
            raise WebError(
                "no_results",
                "没有找到能确认在这段时间内发布的结果" if since else "没有找到搜索结果",
            )
        sources = [
            MaterialSource(title=h.title, url=h.url, published_at=h.published_at) for h in checked
        ]
        text = "\n\n".join(
            "\n".join(v for v in (h.title, h.url, h.published_at, h.summary) if v) for h in checked
        )
        return Material(title="搜索：" + query, sources=sources, text=text)
