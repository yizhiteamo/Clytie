"""脚本模型通过漫画工具与绘图服务逐页绘制；所有传输使用虚构图片。"""

import asyncio
import base64
import hashlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from clytie.sdk.types import HttpResponse

from .host_helpers import enable
from .test_agent import event, finish, make_agent
from .test_appearance_requests import single_reference
from .test_image_gen_integration import CREDENTIAL_FILE
from .test_vision_image import encoded_image


@pytest.mark.parametrize("failure", [None, "render", "storyboard"])
async def test_comic_pages_share_record_and_deliver_in_order_or_none(tmp_path, failure):
    single_reference(tmp_path)
    panel = {"scene": "fictional garden", "characters": ["self"], "dialogue": []}
    story = {
        "title": "Paper garden",
        "pages": [{"panels": [{**panel, "sfx": f"rustle-{n}"}]} for n in range(1, 5)],
    }
    story["pages"][3]["continues"] = True
    if failure == "storyboard":
        story["pages"][1]["panels"] = []
    agent, channel, model = await make_agent(
        tmp_path,
        {
            "finish": "tool_calls",
            "tool_calls": [
                {"tool": "draw_comic", "call_id": "comic", "arguments": story},
            ],
        },
        {"finish": "stop", "text": ""},
        changes={"appearance": {"self_check": False}},
    )
    pages = [encoded_image("PNG", color=c) for c in ("green", "yellow", "purple", "blue")]
    calls = []
    responded = []
    second_started = asyncio.Event()
    release_second = asyncio.Event()

    async def request(method, url, **options):
        assert method == "POST" and url.endswith("/images/edits")
        body = options["body"]
        (number,) = (n for n in range(1, 5) if f"rustle-{n}".encode() in body)
        calls.append(number)
        assert body.count(b'name="image[]"') == (1 if number == 1 else 2)
        if number > 1:
            assert pages[2 if number == 4 else 0] in body
        if number == 2:
            second_started.set()
            await release_second.wait()
        if number == 3:
            await second_started.wait()
        responded.append(number)
        if number == 4:
            if failure == "render":
                return HttpResponse(status=429, headers={}, body=b"{}")
            release_second.set()
        payload = {
            "data": [{"b64_json": base64.b64encode(pages[number - 1]).decode()}],
            "usage": {"input_tokens": number},
        }
        return HttpResponse(status=200, headers={}, body=json.dumps(payload).encode())

    h = agent.host
    try:
        (tmp_path / "secrets").mkdir(exist_ok=True)
        (tmp_path / "secrets/fictional.txt").write_text("fictional-marker", encoding="utf-8")
        h.credentials = {"test_key": {CREDENTIAL_FILE: "fictional.txt", "plugins": ["image-gen"]}}
        previous = h.backends
        h.backends = lambda cid, pid: {
            **previous(cid, pid),
            **({"http": SimpleNamespace(request=request)} if pid == "image-gen" else {}),
        }
        settings = {
            "image-gen": {
                "config": {
                    "backends": [
                        {"model": "fictional-image", "api_key_ref": "test_key"},
                    ]
                }
            },
            "comic-maker": {},
        }
        h.configure(agent.cid, plugins=settings)
        await enable(h, "comic-maker")
        agent.config["plugins"].update(dict.fromkeys(settings, {"enabled": True}))
        trace = await agent.receive(event(agent))
        # Bound a deadlocked test independently of the simulated business deadline.
        await asyncio.wait_for(asyncio.shield(agent.drain()), 10)  # noqa: TID251
        if failure == "storyboard":
            assert not calls and not agent.ledger.rows("invocation")
            errors = [
                p.text
                for msg in model.requests[-1].messages
                if msg.role == "tool"
                for p in msg.parts
                if p.type == "text"
            ]
            assert any("pages[2].panels" in error and "minItems" in error for error in errors)
            return
        assert sorted(calls) == [1, 2, 3, 4]
        assert responded == ([1, 3, 4] if failure else [1, 3, 4, 2])
        record = agent.ledger.get("record", trace)
        claimed = datetime.fromisoformat(record["claimed_at"])
        assert datetime.fromisoformat(record["deadline"]) == claimed + timedelta(hours=1)
        service = [row for row in agent.ledger.rows("audit") if row["kind"] == "service.call"]
        assert len(service) == 4 and all(row["trace_id"] == trace for row in service)
        succeeded = sorted(row["usage"]["input_tokens"] for row in service if row["status"] == "ok")
        assert succeeded == ([1, 3] if failure else [1, 2, 3, 4])
        if failure:
            assert any(row["status"] == "error" for row in service)
        delivered = [
            p.asset.asset_id for msg in channel.sent for p in msg.parts if p.type == "image"
        ]
        assert delivered == ([] if failure else [hashlib.sha256(p).hexdigest() for p in pages])
        (invocation,) = agent.ledger.rows("invocation")
        assert invocation["asset_ids"] == delivered
    finally:
        second_started.set()
        release_second.set()
        await finish(agent)
