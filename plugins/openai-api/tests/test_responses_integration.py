"""Real host/agent/tool/ledger path; protocol and HTTP are entirely fictional."""

import base64
import json

from tests.host_helpers import enable
from tests.test_agent import finish, make_agent
from tests.test_openai_api_integration import IdentityResolver, Protocol

from clytie.sdk.testing import FakeWebSocket
from clytie.sdk.types import HttpResponse


async def test_responses_draw_then_edit_through_fake_protocol(tmp_path):
    agent, _, _ = await make_agent(tmp_path)
    host = agent.host
    marker, opaque = "fictional-b3c-credential", "fictional-opaque-reasoning"
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets/test.txt").write_text(marker, encoding="utf-8")
    secret_field = "secret"
    host.credentials = {
        "model_test": {secret_field: "test.txt", "plugins": ["openai-api", "image-gen"]}
    }
    host.accounts = {
        "fake": {"channel": "qq-onebot", "account": "1000000001", secret_field: "test.txt"}
    }
    instance = {
        "accounts": host.accounts,
        "people": {
            "owner": {
                "role": "owner",
                "accounts": [{"channel": "qq-onebot", "account": "1000000002"}],
            }
        },
        "conversations": {
            "owner_private": {
                "channel": "qq-onebot",
                "kind": "private",
                "platform_id": "1000000002",
                "scene": "owner_private",
            }
        },
    }
    host.identities[agent.cid, "qq-onebot", "fake"] = IdentityResolver(
        "fake", "qq-onebot", instance
    )
    protocol = Protocol()
    model_requests, image_requests = [], []
    images = [b"\x89PNG\r\n\x1a\nfictional-drawing", b"\x89PNG\r\n\x1a\nfictional-edit"]

    class Http:
        async def request(self, method, url, **kwargs):
            assert method == "POST" and kwargs["headers"]["Authorization"] == "Bearer " + marker
            if "/images/" in url:
                image_requests.append(url)
                if len(image_requests) == 1:
                    assert url.endswith("/images/generations")
                else:
                    assert url.endswith("/images/edits")
                    assert images[0] in kwargs["body"]
                data = {
                    "data": [
                        {"b64_json": base64.b64encode(images[len(image_requests) - 1]).decode()}
                    ]
                }
            else:
                assert url == "https://example.invalid/v1/responses"
                body = json.loads(kwargs["body"])
                model_requests.append(body)
                number = len(model_requests)
                assert body["store"] is False and body["include"] == ["reasoning.encrypted_content"]
                assert body["reasoning"] == {"effort": "medium"}
                assert "previous_response_id" not in body
                tool = "draw_image" if number <= 2 else "edit_image"
                call_id = "draw_a" if number <= 2 else "edit_a"
                if number % 2:
                    definition = next(t for t in body["tools"] if t["name"] == tool)
                    assert (
                        definition["parameters"]["type"] == "object"
                        and definition["strict"] is False
                    )
                    items = [
                        {
                            "type": "reasoning",
                            "id": "rs_" + call_id,
                            "encrypted_content": opaque,
                            "summary": [],
                        },
                        {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": tool,
                            "arguments": '{"prompt":"fictional blue bird"}',
                        },
                    ]
                else:
                    items = []
                    index = next(
                        i
                        for i, x in enumerate(body["input"])
                        if x.get("type") == "function_call" and x["call_id"] == call_id
                    )
                    assert body["input"][index - 1]["encrypted_content"] == opaque
                    assert any(
                        x.get("type") == "function_call_output" and x["call_id"] == call_id
                        for x in body["input"]
                    )
                data = {
                    "status": "completed",
                    "output": items,
                    "usage": {"input_tokens": 17, "output_tokens": 4},
                }
            return HttpResponse(status=200, headers={}, body=json.dumps(data).encode())

    previous = host.backends
    host.backends = lambda cid, pid: {
        **previous(cid, pid),
        **({"http": Http()} if pid in {"openai-api", "image-gen"} else {}),
        **({"websocket": FakeWebSocket([protocol])} if pid == "qq-onebot" else {}),
    }
    host.configure(
        agent.cid,
        plugins={
            "openai-api": {
                "config": {
                    "api": "responses",
                    "reasoning_effort": "medium",
                    "model": "fictional-model",
                    "base_url": "https://example.invalid/v1",
                    "api_key_ref": "model_test",
                    "context_chars": 20_000,
                }
            },
            "image-gen": {
                "config": {
                    "backends": [
                        {
                            "model": "fictional-image",
                            "api_key_ref": "model_test",
                            "base_url": "https://example.invalid/v1",
                        }
                    ]
                }
            },
        },
    )
    host.unit_configs[agent.cid, "qq-onebot"] = [
        {
            "role": "channel",
            "account": "fake",
            "config": {
                "url": "ws://example.invalid/",
                "media_limits": {"image": {"max_bytes": 1024, "formats": ["image/png"]}},
            },
        }
    ]
    try:
        for pid in ("openai-api", "image-gen", "qq-onebot"):
            await enable(host, pid)
        reg = next(r for r in host.registrations(agent.cid, "model") if r.plugin_id == "openai-api")
        agent.model_registration, agent.model = reg, reg.implementation
        agent.features = agent.model.features()
        agent.config["model"] = {"plugin": "openai-api"}
        agent.config["plugins"]["image-gen"] = {"enabled": True}
        agent.config["channels"] = [{"plugin": "qq-onebot", "account": "fake"}]
        channel = next(
            r.implementation
            for r in host.registrations(agent.cid, "channel")
            if r.plugin_id == "qq-onebot"
        )
        agent.kernel.delivery.channels["fake"] = channel
        await channel.connect()
        stream = channel.events()
        for number, text in enumerate(
            ("draw a fictional bird", "change the last image to blue"), 1
        ):
            protocol.messages.put_nowait(
                json.dumps(
                    {
                        "post_type": "message",
                        "message_type": "private",
                        "self_id": "1000000001",
                        "user_id": "1000000002",
                        "message_id": number,
                        "time": 0,
                        "message": [{"type": "text", "data": {"text": text}}],
                    }
                )
            )
            trace = await agent.receive(await anext(stream))
            await agent.drain()
            assert agent.ledger.get("record", trace)["exec_status"] == "ok", agent.log.entries
        assert len(model_requests) == 4 and len(image_requests) == 2
        sent = [json.loads(s) for s in protocol.sent]
        delivered = [
            p
            for s in sent
            if s["action"] == "send_private_msg"
            for p in s["params"]["message"]
            if p["type"] == "image"
        ]
        assert delivered == [
            {"type": "image", "data": {"file": "base64://" + base64.b64encode(png).decode()}}
            for png in images
        ]
        usage = [r for r in agent.ledger.rows("audit") if r["kind"] == "model.usage"]
        assert len(usage) == 4 and all(
            r["input_tokens"] == 17 and r["output_tokens"] == 4 for r in usage
        )
        stored = (
            "\n".join(agent.ledger.db.iterdump()) + repr(host.log.entries) + repr(agent.log.entries)
        )
        assert marker not in stored and opaque not in stored
        assert not agent.model._reasoning
        await stream.aclose()
    finally:
        await finish(agent)
