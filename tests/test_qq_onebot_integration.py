"""真实宿主、生产 WebSocket 与本机假协议端；没有真实平台。"""

import base64
import json
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestServer

from clytie.clock import SystemClock
from clytie.plugin_host import PluginHost
from clytie.plugin_host.identity import IdentityResolver
from clytie.plugin_host.support import timed
from clytie.sdk import CancelToken
from clytie.sdk.testing.samples import sample_intent
from clytie.sdk.types import ImagePart, TextPart

ROOT = Path(__file__).resolve().parents[1]
CREDENTIAL_FILE = "secret"


async def test_real_host_unbound_channel_fails_before_network(tmp_path):
    h = PluginHost(ROOT, tmp_path, clock=SystemClock())
    h.configure("companion_a")
    h.unit_configs["companion_a", "qq-onebot"] = [
        {"role": "channel", "account": "unbound", "config": {"url": "ws://example.invalid/"}}
    ]
    h.identities["companion_a", "qq-onebot", "unbound"] = IdentityResolver(
        "unbound", "qq-onebot", {}
    )
    try:
        result = await h.apply(h.plan_enable("companion_a", "qq-onebot").plan_id)
        assert result["failed"] == ["qq-onebot"]
        runtime = h.runtimes["companion_a", "qq-onebot"]
        assert runtime.failure_code == "auth_invalid"
        assert runtime.owned_websocket._session is None
        assert runtime.owned_websocket._closed
    finally:
        await h.close()


def test_onebot_manifest_has_no_builtin_retcode_or_emoji_ids():
    from jsonschema import Draft202012Validator

    manifest = json.loads((ROOT / "plugins/qq-onebot/plugin.json").read_text(encoding="utf-8"))
    schema = manifest["config"]
    validator = Draft202012Validator(schema)
    assert schema["properties"]["retcode_classes"]["default"] == {}
    assert schema["properties"]["emoji_ids"]["default"] == {}
    assert validator.is_valid({"url": "ws://example.invalid/"})
    assert not validator.is_valid({"url": "https://example.invalid/"})
    assert not validator.is_valid(
        {"url": "ws://example.invalid/", "retcode_classes": {"fictional": "transient"}}
    )
    assert not validator.is_valid(
        {"url": "ws://example.invalid/", "retcode_classes": {"2": "permanent"}}
    )


async def test_real_host_onebot_credentials_event_and_asset_send(tmp_path):
    requests = []
    header_values = []

    async def handler(request):
        header_values.append(request.headers.get("Authorization"))
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            call = json.loads(message.data)
            requests.append(call)
            if call["action"] == "get_login_info":
                await ws.send_json(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"user_id": "1000000001"},
                        "echo": call["echo"],
                    }
                )
                await ws.send_json(
                    {
                        "post_type": "message",
                        "message_type": "group",
                        "self_id": "1000000001",
                        "message_id": 1,
                        "group_id": "1000000003",
                        "user_id": "1000000002",
                        "time": 0,
                        "message": [{"type": "text", "data": {"text": "fictional hello"}}],
                    }
                )
            else:
                await ws.send_json(
                    {"status": "ok", "retcode": 0, "data": {"message_id": 2}, "echo": call["echo"]}
                )
        return ws

    app = web.Application()
    app.router.add_get("/", handler)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    clock = SystemClock()
    h = PluginHost(ROOT, tmp_path, clock=clock)
    instance = {
        "accounts": {
            "example_bot": {
                "channel": "qq-onebot",
                "account": "1000000001",
                CREDENTIAL_FILE: "token.txt",
            }
        },
        "people": {},
        "conversations": {
            "test_group": {
                "channel": "qq-onebot",
                "kind": "group",
                "platform_id": "1000000003",
                "scene": "public_group",
            }
        },
    }
    (tmp_path / "secrets").mkdir()
    marker = "fictional_onebot_marker"
    (tmp_path / "secrets/token.txt").write_text(marker, encoding="utf-8")
    h.accounts = instance["accounts"]
    h.configure("companion_a")
    h.unit_configs["companion_a", "qq-onebot"] = [
        {
            "role": "channel",
            "account": "example_bot",
            "config": {
                "url": str(server.make_url("/")).replace("http://", "ws://"),
                "reconnect_delays": [],
                "media_limits": {"image": {"max_bytes": 1024, "formats": ["image/png"]}},
            },
        }
    ]
    h.identities["companion_a", "qq-onebot", "example_bot"] = IdentityResolver(
        "example_bot", "qq-onebot", instance
    )
    try:
        result = await h.apply(h.plan_enable("companion_a", "qq-onebot").plan_id)
        assert not result["failed"], h.failures
        runtime = h.runtimes["companion_a", "qq-onebot"]
        channel = runtime.plugin.create_channel()
        await timed(channel.connect(), clock, 5)
        stream = channel.events()
        event = await timed(anext(stream), clock, 5)
        assert event.event_id == "example_bot:1"
        assert event.message.parts[0].text == "fictional hello"
        data = b"fictional image bytes"
        asset = await h.asset_store.bind("companion_a").put(data, "image/png", "fictional-tool")
        intent = sample_intent().model_copy(
            update={
                "channel_id": "example_bot",
                "parts": [TextPart(text="fictional reply"), ImagePart(asset=asset)],
            }
        )
        receipt = await timed(channel.send(intent, CancelToken()), clock, 5)
        assert receipt.status == "sent" and receipt.platform_message_id == "2"
        sent = requests[-1]
        assert sent["action"] == "send_group_msg"
        assert sent["params"]["message"][1] == {
            "type": "image",
            "data": {"file": "base64://" + base64.b64encode(data).decode()},
        }
        assert header_values == ["Bearer " + marker]
        assert (
            marker
            not in repr(h.log.entries)
            + repr(h.states)
            + repr(h.failures)
            + receipt.model_dump_json()
        )
        await stream.aclose()
    finally:
        await h.close()
        await server.close()
    assert runtime.owned_websocket._closed
