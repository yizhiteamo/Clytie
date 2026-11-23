import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from clytie.sdk import CancelToken
from clytie.sdk.testing.samples import sample_invocation
from clytie.sdk.types import HttpResponse, ToolCall


def load_plugin():
    path = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "fixture_web_plugin", path / "plugin.py", submodule_search_locations=[str(path)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.Plugin()


def response(body=b"", content_type="text/html", status=200):
    return HttpResponse(status=status, headers={"Content-Type": content_type}, body=body)


class HTTP:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    async def request(self, method, url, **kw):
        self.requests.append((method, url, kw))
        item = next(self.replies)
        if isinstance(item, Exception):
            raise item
        return item


async def run(name, args, *replies):
    plugin = load_plugin()
    http = HTTP(replies)

    async def credential(ref):
        assert ref == "search_key"
        return "fictional-marker"

    ctx = SimpleNamespace(
        config={"backend": "openai", "model": "fictional", "credential": "search_key"},
        http=http,
        credentials=SimpleNamespace(read=credential),
        clock=SimpleNamespace(now=lambda: datetime(2026, 1, 4, 15, tzinfo=timezone.utc)),
    )
    await plugin.start(ctx)
    tool = plugin.create_read() if name == "read_url" else plugin.create_search()
    inv = sample_invocation()
    plan = await tool.prepare(ToolCall(tool=name, call_id="call", arguments=args), inv)
    result = await tool.execute(plan, inv, CancelToken())
    return result, http.requests


@pytest.mark.parametrize(
    "reply,expected",
    [
        (ValueError("blocked_address"), "blocked_address"),
        (ValueError("HTTP redirect limit exceeded"), "blocked_address"),
        (ValueError("HTTP redirect scheme downgrade forbidden"), "blocked_address"),
        (TimeoutError(), "timeout"),
        (ValueError("HTTP response exceeds max_bytes"), "too_large"),
        (response(content_type="image/png"), "not_text"),
        (response(status=404), "http_404"),
        (response(status=401), "needs_login"),
        (response(status=403), "needs_login"),
        (response(b'<form><input type="password"></form>'), "needs_login"),
        (response(), "empty"),
    ],
)
async def test_read_failure_reasons(reply, expected):
    result, _ = await run("read_url", {"url": "https://fictional.example/article"}, reply)
    assert result.status == "error" and result.error.code == expected
    assert result.material is None


@pytest.mark.parametrize("mime", ["text/html", "application/xhtml+xml"])
async def test_read_decodes_and_extracts_article_metadata(mime):
    html = (
        '<meta charset="gbk"><title>虚构文章</title><nav>omit</nav>'
        '<meta property="article:published_time" content="2026-01-03">'
        "<article><p>示例正文</p><script>omit</script><style>omit</style></article>"
    )
    result, calls = await run(
        "read_url",
        {"url": "https://fictional.example/article"},
        response(html.encode("gbk"), mime),
    )
    assert result.material.title == "虚构文章" and result.material.text == "示例正文"
    assert result.material.published_at == "2026-01-03" and result.material.sources is None
    assert len(calls) == 1 and calls[0][2]["timeout_s"] == 20
    assert calls[0][2]["max_bytes"] == 3 * 1024 * 1024


def citations():
    text = "A fictional result [1]. Another report from 2026-01-04 [2]."
    annotations = [
        {
            "type": "url_citation",
            "title": title,
            "url": url,
            "start_index": text.index(mark),
            "end_index": text.index(mark) + 3,
        }
        for title, url, mark in [
            ("First", "https://one.example/a", "[1]"),
            ("Second", "https://two.example/b", "[2]"),
        ]
    ]
    annotations.append({"type": "url_citation", "title": "Missing URL"})
    return response(
        json.dumps(
            {
                "output": [
                    {"type": "web_search_call", "status": "completed"},
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": text, "annotations": annotations}
                        ],
                    },
                ]
            }
        ).encode(),
        "application/json",
    )


@pytest.mark.parametrize(
    "recent,failure",
    [
        (None, TimeoutError()),
        (2, TimeoutError()),
        (2, ValueError("HTTP response exceeds max_bytes")),
    ],
)
async def test_search_citations_verify_dates_and_ignore_dates_in_model_text(recent, failure):
    result, calls = await run(
        "search_web",
        {"query": "fictional", **({"recent_days": recent} if recent else {})},
        citations(),
        response(b'<time datetime="2026-01-03"></time>'),
        failure,
    )
    assert result.status == "ok" and result.material.url is None
    sources = result.material.sources
    assert sources[0].published_at == "2026-01-03" and "A fictional result" in result.material.text
    assert len(sources) == (1 if recent else 2)
    if not recent:
        assert sources[1].published_at is None
    method, endpoint, opts = calls[0]
    assert method == "POST" and endpoint == "https://api.openai.com/v1/responses"
    args = json.loads(opts["body"])
    assert args["tools"] == [{"type": "web_search"}] and args["model"] == "fictional"
    if recent:
        assert "2026-01-03" in args["input"] and "2026-01-04" in args["input"]
    assert all(o["timeout_s"] == 5 and o["max_bytes"] == 512 * 1024 for _, _, o in calls[1:])
    assert all("Authorization" not in o["headers"] for _, _, o in calls[1:])
    assert "fictional-marker" not in result.model_dump_json()


async def test_search_all_dates_excluded():
    result, _ = await run(
        "search_web",
        {"query": "fictional", "recent_days": 2},
        citations(),
        response(b'<time datetime="2025-12-01"></time>'),
        response(),
    )
    assert result.material is None
    assert result.content == "没有找到能确认在这段时间内发布的结果"


@pytest.mark.parametrize(
    "reply,code",
    [
        (TimeoutError(), "timeout"),
        (response(status=503), "http_503"),
        (response(b"{}"), "search_failed"),
        (response(b'{"output":[]}'), "no_results"),
    ],
)
async def test_search_reports_provider_failures_and_empty_results(reply, code):
    result, _ = await run("search_web", {"query": "fictional"}, reply)
    assert result.status == "error" and result.error.code == code and result.material is None


async def test_structured_publication_and_result_limit():
    payload = json.loads(citations().body)
    payload["output"][1]["content"][0]["annotations"][0]["published_at"] = "2026-01-03"
    result, calls = await run(
        "search_web",
        {"query": "fictional", "max_results": 1},
        response(json.dumps(payload).encode()),
    )
    assert len(calls) == 1
    (source,) = result.material.sources
    assert source.published_at == "2026-01-03" and source.url == "https://one.example/a"


async def test_read_text_header_charset_and_replacement():
    result, _ = await run(
        "read_url",
        {"url": "https://fictional.example/"},
        response("虚构".encode("gbk"), "text/plain; charset=gbk"),
    )
    assert result.material.text == "虚构"
    result, _ = await run(
        "read_url", {"url": "https://fictional.example/"}, response(b"sample\xff", "text/plain")
    )
    assert result.material.text == "sample\ufffd"


async def test_json_ld_publication_ignores_script_body():
    html = b'<script type="application/ld+json">{"@graph":[{"datePublished":"2026-01-03"}]}</script><p>article</p>'
    result, _ = await run("read_url", {"url": "https://fictional.example/"}, response(html))
    assert result.material.published_at == "2026-01-03" and result.material.text == "article"
