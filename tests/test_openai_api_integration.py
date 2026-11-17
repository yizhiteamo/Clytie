"""B3a: fake protocol → real model plugin → image-gen → fake protocol."""

import base64
import json

import pytest

from clytie.admission import Admission
from clytie.plugin_host.identity import IdentityResolver
from clytie.sdk.testing import FakeWebSocket, FakeWebSocketConnection
from clytie.sdk.types import HttpResponse

from .host_helpers import enable
from .test_agent import finish, make_agent


class Protocol(FakeWebSocketConnection):
    membership_available = True

    async def send_text(self, text):
        await super().send_text(text)
        call = json.loads(text)
        data = (
            {"user_id": "1000000001"} if call["action"] == "get_login_info" else {"message_id": 2}
        )
        status = "ok"
        if call["action"] == "get_group_member_info":
            data = {"group_id": call["params"]["group_id"], "user_id": call["params"]["user_id"]}
            status = "ok" if self.membership_available else "failed"
        self.messages.put_nowait(
            json.dumps(
                {
                    "echo": call["echo"],
                    "status": status,
                    "retcode": 0 if status == "ok" else 1,
                    "data": data,
                }
            )
        )


@pytest.mark.parametrize(
    "test_only,scope_case,owner_present",
    [
        (False, None, False),
        (True, None, False),
        (True, "member", False),
        (True, "excluded", False),
        (False, "member", False),
        (True, None, True),
    ],
)
async def test_onebot_model_image_tool_usage_and_credentials(
    tmp_path, test_only, scope_case, owner_present
):
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
    if test_only:
        instance["test_conversation"] = "test_group"
        for cid, platform_id in (("test_group", "1000000003"), ("other_group", "1000000004")):
            instance["conversations"][cid] = {
                "channel": "qq-onebot",
                "kind": "group",
                "platform_id": platform_id,
                "scene": "public_group",
                "members_expected": ["owner"],
            }
        agent.config["boundary"] = {"admission": "test_only"}
    blocked = scope_case == "excluded" or (scope_case == "member" and not test_only)
    if scope_case:
        instance["people"][scope_case] = {
            "role": "member",
            "accounts": [{"channel": "qq-onebot", "account": "1000000005"}],
        }
        agent.config["permissions"]["member"] = ["draw_image", "edit_image"]
        agent.config["tool_scopes"] = {
            "draw_image": {
                "private": ["owner"],
                "group": ["owner", "peer", "member", "stranger"],
                "exclude": ["excluded"],
            }
        }
        if test_only:
            instance["conversations"]["test_group"]["members_expected"].append(scope_case)
        else:
            instance["conversations"]["owner_private"]["platform_id"] = "1000000005"
            instance["conversations"]["owner_private"]["scene"] = "stranger_private"
    if owner_present:
        agent.config["boundary"] = {"admission": "owner_present"}
        del instance["conversations"]["test_group"]
    agent.instance = instance
    agent.admission = Admission(
        agent.config, instance, channels=agent.kernel.delivery.channels, clock=agent.clock
    )
    agent.kernel.delivery.admission = agent.admission
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
                if blocked:
                    assert "draw_image" not in [t["function"]["name"] for t in body["tools"]]
                    message, reason = {"content": "fictional unavailable"}, "stop"
                elif len(model_requests) == 1:
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
        stream = channel.events()
        if test_only and not owner_present:
            protocol.messages.put_nowait(
                json.dumps(
                    {
                        "post_type": "message",
                        "message_type": "group",
                        "self_id": "1000000001",
                        "user_id": "1000000002",
                        "group_id": "1000000004",
                        "message_id": 3,
                        "time": 0,
                        "message": [{"type": "text", "data": {"text": "draw a fictional bird"}}],
                    }
                )
            )
            assert await agent.receive(await anext(stream)) is None
            assert not model_requests and not agent.ledger.rows("inbound")
            assert agent.admission.counts["not_test_conversation"] == 1
        protocol.messages.put_nowait(
            json.dumps(
                {
                    "post_type": "message",
                    "message_type": "group" if test_only else "private",
                    **({"group_id": "1000000003"} if test_only else {}),
                    "self_id": "1000000001",
                    "user_id": "1000000005" if scope_case else "1000000002",
                    "message_id": 1,
                    "time": 0,
                    "message": [
                        *([{"type": "at", "data": {"qq": "1000000001"}}] if test_only else []),
                        {"type": "text", "data": {"text": "draw a fictional bird"}},
                    ],
                }
            )
        )
        incoming = await anext(stream)
        trace = await agent.receive(incoming)
        await agent.drain()
        assert agent.ledger.get("record", trace)["exec_status"] == "ok", agent.log.entries
        assert len(model_requests) == (1 if blocked else 2)
        sent = [json.loads(s) for s in protocol.sent]
        images = [
            p
            for s in sent
            if s["action"] == ("send_group_msg" if test_only else "send_private_msg")
            for p in s["params"]["message"]
            if p["type"] == "image"
        ]
        assert images == (
            []
            if blocked
            else [{"type": "image", "data": {"file": "base64://" + base64.b64encode(png).decode()}}]
        )
        if test_only:
            assert all(
                s["params"]["group_id"] == "1000000003"
                for s in sent
                if s["action"] == "send_group_msg"
            )
        usage = [r for r in agent.ledger.rows("audit") if r["kind"] == "model.usage"]
        assert len(usage) == (1 if blocked else 2) and all(
            r["plugin"] == "openai-api" and r["input_tokens"] == 17 and r["output_tokens"] == 4
            for r in usage
        )
        assert marker not in "\n".join(agent.ledger.db.iterdump()) + repr(h.log.entries) + repr(
            agent.log.entries
        )
        if owner_present:
            queries = [call for call in sent if call["action"] == "get_group_member_info"]
            assert len(queries) == 1
            protocol.membership_available = False
            protocol.messages.put_nowait(
                json.dumps(
                    {
                        "post_type": "notice",
                        "notice_type": "group_decrease",
                        "self_id": "1000000001",
                        "group_id": "1000000003",
                        "user_id": "1000000002",
                    }
                )
            )
            assert await agent.receive(await anext(stream)) is None
            assert not agent.admission.membership
            before = len(agent.ledger.rows("inbound"))
            assert (
                await agent.receive(
                    incoming.model_copy(
                        update={
                            "event_id": "fake:another",
                            "message": incoming.message.model_copy(
                                update={
                                    "ref": incoming.message.ref.model_copy(
                                        update={"platform_message_id": "another"}
                                    )
                                }
                            ),
                        }
                    )
                )
                is None
            )
            assert len(agent.ledger.rows("inbound")) == before
            assert agent.admission.counts["membership_unknown"] == 1
        await stream.aclose()
    finally:
        await finish(agent)
