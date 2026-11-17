import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from clytie.sdk import CancelToken, ChannelUnavailable, CredentialUnavailable
from clytie.sdk.testing import (
    FakeContext,
    FakeIdentity,
    FakeWebSocket,
    FakeWebSocketConnection,
    assert_channel_contract,
)
from clytie.sdk.testing.samples import sample_intent
from clytie.sdk.types import Conversation


def load_channel():
    path = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location(
        "qq_channel_test", path / "channel.py", submodule_search_locations=[str(path)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.OneBotChannel


class Credentials:
    def __init__(self, error=None):
        self.error = error

    async def account_secret(self):
        if self.error:
            raise CredentialUnavailable(self.error)
        return "fictional-marker"


class Protocol(FakeWebSocketConnection):
    def __init__(self, *, account="1000000001", reply=None):
        super().__init__()
        self.account, self.reply = account, reply
        self.actions = []

    async def send_text(self, text):
        await super().send_text(text)
        payload = json.loads(text)
        self.actions.append(payload)
        if payload["action"] == "get_login_info":
            result = {"status": "ok", "data": {"user_id": self.account}}
        else:
            result = self.reply
            if result is None:
                result = {"status": "ok", "retcode": 0, "data": {"message_id": 7001}}
        if result != "silence":
            self.messages.put_nowait(json.dumps({"echo": payload["echo"], **result}))


def make(tmp_path, protocol=None, **config):
    identity = FakeIdentity(
        "qq-onebot",
        self_account="1000000001",
        conversations={
            ("group", "1000000003"): Conversation(
                conversation_id="test_group",
                kind="group",
                scene="public_group",
                channel_id="qq-onebot",
            )
        },
    )
    ctx = FakeContext(
        plugin_id="qq-onebot",
        root=tmp_path,
        channel=True,
        identity=identity,
        permissions=["network"],
        config={
            "url": "ws://127.0.0.1:1",
            "reconnect_delays": [1],
            "emoji_ids": {"👌": "1", "⏳": "2", "✅": "3", "❌": "4"},
            **config,
        },
    )
    ctx.credentials = Credentials()
    protocol = protocol or Protocol()
    ctx.websocket = FakeWebSocket([protocol])
    return ctx, load_channel()(ctx), protocol


def intent(*, react=False):
    sample = sample_intent(react=react)
    return sample.model_copy(
        update={
            "channel_id": "qq-onebot",
            "emoji": "👌" if react else None,
            "target": sample.target.model_copy(update={"channel_id": "qq-onebot"}),
        }
    )


async def test_channel_contract(tmp_path):
    ctx, channel, _ = make(tmp_path)
    await assert_channel_contract(channel, intent(), intent(react=True))
    assert ctx.clock.pending_sleep_count == 0


def test_features_preserve_configured_media_limits(tmp_path):
    _, channel, _ = make(
        tmp_path, media_limits={"image": {"max_bytes": 1024, "formats": ["image/png"]}}
    )
    assert channel.features().media_limits["image"]["max_bytes"] == 1024


async def test_bound_channel_reference_differs_from_plugin_id(tmp_path):
    ctx, _, protocol = make(tmp_path)
    ctx.identity.channel_id = "example_bot"
    ctx.identity.conversations[("group", "1000000003")] = ctx.identity.conversations[
        ("group", "1000000003")
    ].model_copy(update={"channel_id": "example_bot"})
    channel = load_channel()(ctx)
    sample = intent().model_copy(update={"channel_id": "example_bot"})
    reaction = intent(react=True).model_copy(
        update={
            "channel_id": "example_bot",
            "target": intent().target.model_copy(update={"channel_id": "example_bot"}),
        }
    )
    await channel.connect()
    try:
        assert (await channel.send(sample, CancelToken())).status == "sent"
        assert (await channel.react(reaction, CancelToken())).status == "sent"
        wrong = reaction.model_copy(
            update={"target": reaction.target.model_copy(update={"conversation_id": "other_group"})}
        )
        assert (await channel.react(wrong, CancelToken())).status == "failed"
        assert len(protocol.actions) == 3
    finally:
        await channel.close()


@pytest.mark.parametrize(
    "reply,status,error",
    [
        ({"status": "ok", "data": {"message_id": 7}}, "sent", None),
        ({"status": "ok", "data": {}}, "unknown", None),
        ({"status": "async", "data": {"message_id": 7}}, "unknown", None),
        ({"status": "failed", "retcode": 1400}, "failed", "permanent"),
        ({"status": "failed", "retcode": 1404}, "failed", "permanent"),
        ({"status": "failed", "retcode": 5001}, "failed", "rate_limited"),
        ({"status": "failed", "retcode": 5002}, "failed", "transient"),
        ({"status": "other", "data": []}, "unknown", None),
        ({"status": "failed"}, "unknown", None),
        ({"status": "failed", "retcode": True}, "unknown", None),
        ({"status": "failed", "retcode": "fictional-secret"}, "unknown", None),
        ({"status": "ok", "data": {"message_id": True}}, "unknown", None),
    ],
)
async def test_receipt_classification(tmp_path, reply, status, error):
    ctx, channel, protocol = make(
        tmp_path,
        Protocol(reply=reply),
        retcode_classes={"5001": "rate_limited", "5002": "transient"},
    )
    await channel.connect()
    try:
        result = await channel.send(intent(), CancelToken())
        assert result.status == status
        assert (result.error.code if result.error else None) == error
        assert protocol.actions[-1]["action"] == "send_group_msg"
        assert protocol.actions[-1]["params"]["group_id"] == "1000000003"
    finally:
        await channel.close()


@pytest.mark.parametrize("react", [False, True])
@pytest.mark.parametrize(
    "mode", ["timeout", "late", "mismatch", "disconnect", "send_error", "cancel"]
)
async def test_pending_unknown_no_resend_or_late_receipt(tmp_path, mode, react):
    ctx, channel, protocol = make(tmp_path, Protocol(reply="silence"), request_timeout_s=2)
    await channel.connect()
    token = CancelToken()
    if mode == "send_error":

        async def fail_after_call(text):
            protocol.actions.append(json.loads(text))
            raise ConnectionError("fictional failure")

        protocol.send_text = fail_after_call
    invoke = channel.react if react else channel.send
    task = asyncio.create_task(invoke(intent(react=react), token))
    await ctx.clock.drain()
    request = protocol.actions[-1]
    try:
        if mode == "disconnect":
            await protocol.close()
        elif mode == "cancel":
            token.cancel()
        else:
            if mode == "mismatch":
                protocol.messages.put_nowait(json.dumps({"echo": "unmatched", "status": "ok"}))
            ctx.clock.advance(2)
        if mode == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task).status == "unknown"
        if not protocol.closed:
            protocol.messages.put_nowait(json.dumps({"echo": request["echo"], "status": "ok"}))
            await ctx.clock.drain()
        assert len(protocol.actions) == 2
        assert not channel._pending
    finally:
        await channel.close()


@pytest.mark.parametrize("cause", ["missing", "mismatch"])
async def test_auth_invalid_never_reconnects(tmp_path, cause):
    ctx, channel, protocol = make(
        tmp_path,
        Protocol(account="1000000002" if cause == "mismatch" else "1000000001"),
        reconnect_delays=[1, 2],
    )
    if cause == "missing":
        ctx.credentials = Credentials("missing")
    with pytest.raises(ChannelUnavailable) as caught:
        await channel.connect()
    assert caught.value.code == "auth_invalid"
    assert channel.state().status == "auth_invalid"
    await ctx.clock.advance_and_drain(100)
    assert len(ctx.websocket.opened) == (1 if cause == "mismatch" else 0)
    await channel.close()
    assert "fictional-marker" not in repr(ctx.log.entries)


@pytest.mark.parametrize(
    "reply,disabled",
    [
        ({"status": "ok"}, False),
        ({"status": "failed", "retcode": 1404}, True),
        ({"status": "failed", "retcode": 5001}, False),
    ],
)
async def test_reaction_compatibility(tmp_path, reply, disabled):
    ctx, channel, protocol = make(tmp_path, Protocol(reply=reply))
    await channel.connect()
    try:
        await channel.react(intent(react=True), CancelToken())
        assert channel.features().supports_react is not disabled
        await channel.react(intent(react=True), CancelToken())
        assert len(protocol.actions) == (2 if disabled else 3)
        assert protocol.actions[1]["params"] == {"message_id": "fake-message", "emoji_id": "1"}
        assert channel.state().status == "online"
    finally:
        await channel.close()


async def test_missing_reaction_config_skips_action(tmp_path):
    _, channel, protocol = make(tmp_path, emoji_ids={"👌": "1"})
    await channel.connect()
    try:
        await channel.react(intent(react=True), CancelToken())
        assert not channel.features().supports_react
        assert len(protocol.actions) == 1
    finally:
        await channel.close()


async def test_heartbeat_reconnect_repeats_last_delay_and_recovers(tmp_path):
    ctx, channel, protocol = make(tmp_path, heartbeat_timeout_s=5, reconnect_delays=[1, 2])

    restored = Protocol()

    class FailingSocket:
        def __init__(self):
            self.calls = 0

        async def connect(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return protocol
            if self.calls >= 4:
                return restored
            raise ConnectionError("fictional-secret")

    ctx.websocket = FailingSocket()
    await channel.connect()
    await ctx.clock.advance_and_drain(5)
    assert channel.state().status == "unknown"
    await ctx.clock.advance_and_drain(3)
    assert ctx.websocket.calls == 3
    assert not channel._manager.done()
    await ctx.clock.advance_and_drain(1)
    assert ctx.websocket.calls == 3
    await ctx.clock.advance_and_drain(1)
    assert ctx.websocket.calls == 4
    assert channel.state().status == "online"
    await channel.close()
    await ctx.clock.advance_and_drain(100)
    assert ctx.websocket.calls == 4
    assert ctx.clock.pending_sleep_count == 0
    assert "fictional-secret" not in repr(ctx.log.entries)


async def test_no_outbound_before_login_verification(tmp_path):
    ctx, channel, protocol = make(tmp_path)

    async def hold_login(text):
        await FakeWebSocketConnection.send_text(protocol, text)
        protocol.actions.append(json.loads(text))

    protocol.send_text = hold_login
    opening = asyncio.create_task(channel.connect())
    await ctx.clock.drain()
    try:
        assert channel.state().status == "connecting"
        for receipt in (
            await channel.send(intent(), CancelToken()),
            await channel.react(intent(react=True), CancelToken()),
        ):
            assert receipt.status == "failed" and receipt.error.code == "transient"
        assert len(protocol.actions) == 1
    finally:
        opening.cancel()
        await asyncio.gather(opening, return_exceptions=True)
        await channel.close()


def message(message_id=1, **changes):
    return {
        "post_type": "message",
        "message_type": "group",
        "message_id": message_id,
        "self_id": 1000000001,
        "user_id": 1000000004,
        "group_id": 1000000003,
        "time": 946_684_800,
        "message": [{"type": "text", "data": {"text": "fictional-message"}}],
        **changes,
    }


async def test_event_identity_queue_limits_and_status_counters(tmp_path):
    ctx, channel, protocol = make(tmp_path, queue_limit=1)
    await channel.connect()
    try:
        for item in [
            message(self_id=1000000002),
            message(user_id=1000000001),
            message(2),
            message(3),
        ]:
            protocol.messages.put_nowait(json.dumps(item))
        await ctx.clock.drain(128)
        snapshot = ctx.state.snapshot.states[ctx.plugin_id]
        assert snapshot["self_id_mismatch"] == 1
        assert snapshot["queue_overflow"] == 1
        stream = channel.events()
        assert (await anext(stream)).event_id == "qq-onebot:2"
        await stream.aclose()
        assert any(entry[1] == "qq.queue_overflow" for entry in ctx.log.entries)
    finally:
        await channel.close()


async def test_reconnect_restores_reactions_and_reads_new_secret(tmp_path):
    first = Protocol(reply={"status": "failed", "retcode": 1404})
    ctx, channel, _ = make(tmp_path, first, reconnect_delays=[1])
    second = Protocol()
    ctx.websocket.connections.append(second)
    await channel.connect()
    try:
        await channel.react(intent(react=True), CancelToken())
        assert not channel.features().supports_react
        await first.close()
        await ctx.clock.advance_and_drain(1)
        assert channel.state().status == "online"
        assert channel.features().supports_react
        assert len(ctx.websocket.opened) == 2
    finally:
        await channel.close()


async def test_close_cancels_pending_connect(tmp_path):
    ctx, channel, _ = make(tmp_path)
    entered, released = asyncio.Event(), asyncio.Event()

    class BlockedSocket:
        async def connect(self, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                released.set()

    ctx.websocket = BlockedSocket()
    opening = asyncio.create_task(channel.connect())
    await entered.wait()
    try:
        await channel.close()
        assert released.is_set()
        assert opening.done()
    finally:
        opening.cancel()
        await asyncio.gather(opening, return_exceptions=True)


async def test_close_unblocks_events_without_pending_clock(tmp_path):
    ctx, channel, _ = make(tmp_path)
    await channel.connect()
    stream = channel.events()
    pending = asyncio.create_task(anext(stream))
    await ctx.clock.drain()
    await channel.close()
    with pytest.raises(StopAsyncIteration):
        await pending
    await stream.aclose()
    assert ctx.clock.pending_sleep_count == 0


async def test_only_matching_heartbeat_renews_liveness(tmp_path):
    ctx, channel, protocol = make(tmp_path, heartbeat_timeout_s=5)
    await channel.connect()
    try:
        await ctx.clock.advance_and_drain(4)
        protocol.messages.put_nowait(
            json.dumps(
                {
                    "post_type": "meta_event",
                    "meta_event_type": "heartbeat",
                    "self_id": 1000000001,
                }
            )
        )
        await ctx.clock.drain()
        await ctx.clock.advance_and_drain(4)
        assert channel.state().status == "online"
        protocol.messages.put_nowait(
            json.dumps(
                {
                    "post_type": "meta_event",
                    "meta_event_type": "heartbeat",
                    "self_id": 1000000002,
                }
            )
        )
        await ctx.clock.drain()
        await ctx.clock.advance_and_drain(1)
        assert protocol.closed
        assert channel.state().status == "unknown"
    finally:
        await channel.close()


@pytest.mark.parametrize("react", [False, True])
@pytest.mark.parametrize("state", ["offline", "connecting", "unknown", "auth_invalid"])
async def test_not_online_is_definitely_unsent(tmp_path, react, state):
    _, channel, protocol = make(tmp_path)
    await channel._publish(state)
    invoke = channel.react if react else channel.send
    result = await invoke(intent(react=react), CancelToken())
    assert result.status == "failed" and result.error.code == "transient"
    assert not protocol.sent and not protocol.actions
    await channel.close()


@pytest.mark.parametrize("react", [False, True])
@pytest.mark.parametrize("connection", ["missing", "closed"])
async def test_connection_unavailable_before_send_is_transient(tmp_path, react, connection):
    _, channel, protocol = make(tmp_path)
    await channel._publish("online")
    if connection == "closed":
        await protocol.close()
        channel._ws = protocol
    invoke = channel.react if react else channel.send
    result = await invoke(intent(react=react), CancelToken())
    assert result.status == "failed" and result.error.code == "transient"
    assert not protocol.sent and not protocol.actions
    assert not channel._pending
    await channel.close()


async def test_request_default_timeout_is_thirty_seconds(tmp_path):
    ctx, channel, protocol = make(tmp_path, Protocol(reply="silence"))
    await channel.connect()
    task = asyncio.create_task(channel.send(intent(), CancelToken()))
    try:
        await ctx.clock.drain()
        await ctx.clock.advance_and_drain(29)
        assert not task.done()
        await ctx.clock.advance_and_drain(1)
        assert (await task).status == "unknown"
        assert len(protocol.actions) == 2
    finally:
        await channel.close()


def test_reconnect_and_request_defaults():
    schema = json.loads((Path(__file__).parents[1] / "plugin.json").read_text(encoding="utf-8"))[
        "config"
    ]
    assert schema["properties"]["request_timeout_s"]["default"] == 30
    delays = schema["properties"]["reconnect_delays"]
    assert delays["default"] == [1, 2, 4, 8, 16, 30]
    assert delays["minItems"] == 1


async def test_close_stops_repeated_reconnect_wait(tmp_path):
    ctx, channel, protocol = make(tmp_path, reconnect_delays=[1])

    class FailingSocket:
        calls = 0

        async def connect(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return protocol
            raise ConnectionError("fictional failure")

    ctx.websocket = FailingSocket()
    await channel.connect()
    await protocol.close()
    await ctx.clock.advance_and_drain(3)
    assert ctx.websocket.calls == 4
    assert not channel._manager.done()
    await channel.close()
    await ctx.clock.advance_and_drain(100)
    assert ctx.websocket.calls == 4
    assert ctx.clock.pending_sleep_count == 0


async def test_owner_notice_capture_is_not_an_inbound_event(tmp_path):
    from clytie.sdk.types import Actor

    ctx, channel, protocol = make(tmp_path)
    ctx.identity.actors["1000000002"] = Actor(
        actor_id="owner", role="owner", channel_id="qq-onebot"
    )
    await channel.connect()
    try:
        channel.capture.start()
        protocol.messages.put_nowait(
            json.dumps(
                {
                    "self_id": "1000000001",
                    "post_type": "notice",
                    "notice_type": "group_msg_emoji_like",
                    "user_id": "1000000002",
                    "is_add": True,
                    "likes": [{"emoji_id": "7"}],
                }
            )
        )
        for _ in range(20):
            await ctx.clock.sleep(0)
        assert channel.capture.status()["ids"] == ["7"]
        assert channel._queue.empty()
        assert "1000000002" not in str(ctx.log.entries)
    finally:
        await channel.close()


@pytest.mark.parametrize("remove", [False, True])
async def test_reaction_remove_precedes_add(tmp_path, remove):
    _, channel, protocol = make(tmp_path, react_remove=remove)
    await channel.connect()
    try:
        assert channel.features().supports_react_remove is remove
        receipt = await channel.react(
            intent(react=True).model_copy(update={"replaces_emoji": "⏳"}), CancelToken()
        )
        assert receipt.status == "sent"
        calls = [p for p in protocol.actions if p["action"] == "set_msg_emoji_like"]
        assert len(calls) == (2 if remove else 1)
        if remove:
            assert calls[0]["params"]["set"] is False
            assert calls[0]["params"]["emoji_id"] == "2"
        assert "set" not in calls[-1]["params"]
    finally:
        await channel.close()


@pytest.mark.parametrize("code,remove_enabled", [(1404, False), (5001, True)])
async def test_remove_failure_still_adds_and_only_remove_degrades(tmp_path, code, remove_enabled):
    class RemovalProtocol(Protocol):
        async def send_text(self, text):
            request = json.loads(text)
            self.reply = (
                {"status": "failed", "retcode": code}
                if request["params"].get("set") is False
                else {"status": "ok", "retcode": 0}
            )
            await super().send_text(text)

    ctx, channel, protocol = make(tmp_path, RemovalProtocol(), react_remove=True)
    await channel.connect()
    try:
        target = intent(react=True).model_copy(update={"replaces_emoji": "⏳"})
        assert (await channel.react(target, CancelToken())).status == "sent"
        assert channel.features().supports_react
        assert channel.features().supports_react_remove is remove_enabled
        await channel.react(target, CancelToken())
        removals = [x for x in protocol.actions if x["params"].get("set") is False]
        assert len(removals) == (2 if remove_enabled else 1)
        await channel._disconnect()
        ctx.websocket.connections.append(Protocol())
        await channel._open()
        assert channel.features().supports_react_remove
    finally:
        await channel.close()


async def test_remove_timeout_still_adds_new_marker(tmp_path):
    class SilentRemoval(Protocol):
        async def send_text(self, text):
            data = json.loads(text)
            self.reply = (
                "silence" if data["params"].get("set") is False else {"status": "ok", "retcode": 0}
            )
            await super().send_text(text)

    ctx, channel, protocol = make(tmp_path, SilentRemoval(), react_remove=True)
    await channel.connect()
    try:
        task = asyncio.create_task(
            channel.react(
                intent(react=True).model_copy(update={"replaces_emoji": "⏳"}), CancelToken()
            )
        )
        for _ in range(20):
            await ctx.clock.sleep(0)
        ctx.clock.advance(30)
        for _ in range(30):
            await ctx.clock.sleep(0)
        assert task.done()
        assert (await task).status == "sent"
        assert len([x for x in protocol.actions if x["action"] == "set_msg_emoji_like"]) == 2
    finally:
        await channel.close()


@pytest.mark.parametrize("route", ["capture_start", "capture_status", "capture_cancel"])
async def test_capture_route_without_channel_returns_conflict(route):
    load_channel()
    from qq_channel_test.plugin import Plugin

    assert await getattr(Plugin(), route)({}) == {
        "status": 409,
        "json": {"code": "channel_unavailable"},
    }
