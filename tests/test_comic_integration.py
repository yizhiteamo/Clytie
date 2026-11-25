"""脚本模型通过漫画工具与绘图服务逐页绘制；所有传输使用虚构图片。"""

import base64
import hashlib
import json
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
    story = {"title": "Paper garden", "pages": [{"panels": [panel]}, {"panels": [panel]}]}
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
    pages = [encoded_image("PNG", color=c) for c in ("green", "yellow")]
    calls = []

    async def request(method, url, **options):
        assert method == "POST" and url.endswith("/images/edits")
        body = options["body"]
        calls.append(body.count(b'name="image[]"'))
        if len(calls) == 2:
            assert pages[0] in body
            if failure == "render":
                return HttpResponse(status=429, headers={}, body=b"{}")
        payload = {
            "data": [{"b64_json": base64.b64encode(pages[len(calls) - 1]).decode()}],
            "usage": {"input_tokens": len(calls)},
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
        await agent.drain()
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
        assert calls == [1, 2]
        service = [row for row in agent.ledger.rows("audit") if row["kind"] == "service.call"]
        assert [row["trace_id"] for row in service] == [trace, trace]
        assert service[0]["usage"] == {"input_tokens": 1}
        assert service[1]["status"] == ("error" if failure else "ok")
        delivered = [
            p.asset.asset_id for msg in channel.sent for p in msg.parts if p.type == "image"
        ]
        assert delivered == ([] if failure else [hashlib.sha256(p).hexdigest() for p in pages])
    finally:
        await finish(agent)
