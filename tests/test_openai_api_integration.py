"""B3a: fake protocol → real model plugin → image-gen → fake protocol."""

import base64
import json

from clytie.plugin_host.identity import IdentityResolver
from clytie.sdk.testing import FakeWebSocket, FakeWebSocketConnection
from clytie.sdk.types import HttpResponse

from .host_helpers import enable
from .test_agent import finish, make_agent


class Protocol(FakeWebSocketConnection):
    async def send_text(self, text):
        await super().send_text(text)
        call = json.loads(text)
        data = (
            {"user_id": "1000000001"} if call["action"] == "get_login_info" else {"message_id": 2}
        )
        self.messages.put_nowait(
            json.dumps({"echo": call["echo"], "status": "ok", "retcode": 0, "data": data})
        )


async def test_onebot_model_image_tool_usage_and_credentials(tmp_path):
    agent, _, _ = await make_agent(tmp_path)
    h = agent.host
    marker = "fictional-b3a-credential"
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets/test.txt").write_text(marker, encoding="utf-8")
    secret_field = "secret"
    h.credentials = {
        "model_test": {secret_field: "test.txt", "plugins": ["openai-api", "image-gen"]}
    }
    h.accounts = {
        "fake": {"channel": "qq-onebot", "account": "1000000001", secret_field: "test.txt"}
    }
    instance = {
        "accounts": h.accounts,
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
    h.identities[agent.cid, "qq-onebot", "fake"] = IdentityResolver("fake", "qq-onebot", instance)
    protocol = Protocol()
    model_requests = []
    png = b"\x89PNG\r\n\x1a\nfictional-image"

    class Http:
        async def request(self, method, url, **kwargs):
            assert kwargs["headers"]["Authorization"] == "Bearer " + marker
            if url.endswith("/images/generations"):
                data = {"data": [{"b64_json": base64.b64encode(png).decode()}]}
            else:
                assert url == "https://example.invalid/v1/chat/completions"
                body = json.loads(kwargs["body"])
                model_requests.append(body)
                if len(model_requests) == 1:
                    tool = next(
                        t["function"]
                        for t in body["tools"]
                        if t["function"]["name"] == "draw_image"
                    )
                    assert tool["parameters"]["type"] == "object"
                    message = {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "draw_a",
                                "type": "function",
                                "function": {
                                    "name": "draw_image",
                                    "arguments": '{"prompt":"fictional blue bird"}',
                                },
                            }
                        ],
                    }
                    reason = "tool_calls"
                else:
                    assert any(
                        m["role"] == "tool" and m["tool_call_id"] == "draw_a"
                        for m in body["messages"]
                    )
                    message, reason = {"content": ""}, "stop"
                data = {
                    "choices": [{"message": message, "finish_reason": reason}],
                    "usage": {"prompt_tokens": 17, "completion_tokens": 4},
                }
            return HttpResponse(status=200, headers={}, body=json.dumps(data).encode())

    previous = h.backends
    h.backends = lambda cid, pid: {
        **previous(cid, pid),
        **({"http": Http()} if pid in {"openai-api", "image-gen"} else {}),
        **({"websocket": FakeWebSocket([protocol])} if pid == "qq-onebot" else {}),
    }
    h.configure(
        agent.cid,
        plugins={
            "openai-api": {
                "config": {
                    "model": "fictional-model",
                    "base_url": "https://example.invalid/v1",
                    "api_key_ref": "model_test",
                    "context_chars": 20_000,
                }
            },
            "image-gen": {
                "config": {"backends": [{"model": "fictional-image", "api_key_ref": "model_test"}]}
            },
        },
    )
    h.unit_configs[agent.cid, "qq-onebot"] = [
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
            await enable(h, pid)
        reg = next(r for r in h.registrations(agent.cid, "model") if r.plugin_id == "openai-api")
        agent.model_registration, agent.model = reg, reg.implementation
        agent.features = agent.model.features()
        agent.config["model"] = {"plugin": "openai-api"}
        agent.config["plugins"]["image-gen"] = {"enabled": True}
        agent.config["channels"] = [{"plugin": "qq-onebot", "account": "fake"}]
        channel = next(
            r.implementation
            for r in h.registrations(agent.cid, "channel")
            if r.plugin_id == "qq-onebot"
        )
        agent.kernel.delivery.channels["fake"] = channel
        await channel.connect()
        protocol.messages.put_nowait(
            json.dumps(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "self_id": "1000000001",
                    "user_id": "1000000002",
                    "message_id": 1,
                    "time": 0,
                    "message": [{"type": "text", "data": {"text": "draw a fictional bird"}}],
                }
            )
        )
        stream = channel.events()
        incoming = await anext(stream)
        trace = await agent.receive(incoming)
        await agent.drain()
        assert agent.ledger.get("record", trace)["exec_status"] == "ok", agent.log.entries
        assert len(model_requests) == 2
        sent = [json.loads(s) for s in protocol.sent]
        images = [
            p
            for s in sent
            if s["action"] == "send_private_msg"
            for p in s["params"]["message"]
            if p["type"] == "image"
        ]
        assert images == [
            {"type": "image", "data": {"file": "base64://" + base64.b64encode(png).decode()}}
        ]
        usage = [r for r in agent.ledger.rows("audit") if r["kind"] == "model.usage"]
        assert len(usage) == 2 and all(
            r["plugin"] == "openai-api" and r["input_tokens"] == 17 and r["output_tokens"] == 4
            for r in usage
        )
        assert marker not in "\n".join(agent.ledger.db.iterdump()) + repr(h.log.entries) + repr(
            agent.log.entries
        )
        await stream.aclose()
    finally:
        await finish(agent)
