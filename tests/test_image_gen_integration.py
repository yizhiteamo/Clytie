"""假模型 → 生产 image-gen/资产后端 → fake-channel → 真实账本。"""

import base64
import json

import pytest

from clytie.plugin_host.identity import IdentityResolver
from clytie.sdk.types import HttpResponse

from .host_helpers import enable
from .test_agent import event, finish, make_agent

CREDENTIAL_FILE = "secret"


@pytest.mark.parametrize("failed", [False, True])
async def test_image_gen_model_channel_ledger_and_secret_exclusion(tmp_path, failed):
    agent, _, model = await make_agent(
        tmp_path,
        {
            "finish": "tool_calls",
            "tool_calls": [
                {
                    "call_id": "draw_a",
                    "tool": "draw_image",
                    "arguments": {"prompt": "fictional blue bird"},
                }
            ],
        },
        {"finish": "stop", "text": ""},
    )
    h = agent.host
    marker = "fictional-image-api-marker"
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets/image.txt").write_text(marker, encoding="utf-8")
    h.credentials = {"image_test": {CREDENTIAL_FILE: "image.txt", "plugins": ["image-gen"]}}

    class FakeHTTP:
        async def request(self, method, url, **kwargs):
            assert marker in kwargs["headers"]["Authorization"]
            # 让回应发送任务获得执行机会，不进行真实网络请求。
            for _ in range(20):
                await agent.clock.sleep(0)
            payload = (
                {"error": {"code": "insufficient_quota", "message": marker}}
                if failed
                else {
                    "data": [
                        {
                            "b64_json": base64.b64encode(
                                b"\x89PNG\r\n\x1a\nfictional-image-bytes"
                            ).decode()
                        }
                    ]
                }
            )
            return HttpResponse(
                status=429 if failed else 200, headers={}, body=json.dumps(payload).encode()
            )

    prior = h.backends
    h.backends = lambda cid, pid: {
        **prior(cid, pid),
        **({"http": FakeHTTP()} if pid == "image-gen" else {}),
        **(
            {"identity": IdentityResolver("fake", "fake-channel", {})}
            if pid == "fake-channel"
            else {}
        ),
    }
    h.configure(
        agent.cid,
        plugins={
            "image-gen": {
                "config": {"backends": [{"model": "fictional-image", "api_key_ref": "image_test"}]}
            }
        },
    )
    await enable(h, "image-gen")
    agent.config["plugins"]["image-gen"] = {"enabled": True}
    await enable(h, "fake-channel")
    channel = next(r.implementation for r in h.registrations(agent.cid, "channel"))
    features = channel.features()
    channel.features = lambda: features.model_copy(
        update={"media_limits": {"image": {"max_bytes": 1024, "formats": ["image/png"]}}}
    )
    agent.kernel.delivery.channels["fake"] = channel
    try:
        trace = await agent.receive(event(agent))
        await agent.drain()
        row = agent.ledger.get("record", trace)
        intents = agent.ledger.intents(trace)
        assert any(i["reaction_state"] == "running" for i in intents)
        # 本体回应对应整个任务；工具错误交给模型如实回复后，任务仍可正常完成。
        assert any(i["reaction_state"] == "done" for i in intents)
        if not failed:
            assert row["exec_status"] == "ok" and row["delivery_status"] == "sent"
            images = [
                part for intent in channel.sent for part in intent.parts if part.type == "image"
            ]
            assert len(images) == 1
            bound = h.asset_store.bind(agent.cid)
            assert (
                await bound.read(images[0].asset.asset_id)
                == b"\x89PNG\r\n\x1a\nfictional-image-bytes"
            )
        else:
            assert "quota_exceeded" in repr([request.model_dump() for request in model.requests])
        assert marker not in repr(h.log.entries)
        assert marker not in repr(agent.log.entries)
        assert marker not in repr([request.model_dump() for request in model.requests])
        assert marker not in "\n".join(agent.ledger.db.iterdump())
    finally:
        await finish(agent)
