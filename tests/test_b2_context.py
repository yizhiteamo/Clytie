"""B2 宿主绑定与 SDK 假连接，只使用虚构账号和临时文件。"""

import asyncio

import pytest

from clytie.plugin_host.identity import IdentityResolver
from clytie.sdk import Cancelled, CancelToken, CredentialUnavailable, WebSocketClosed
from clytie.sdk.testing import FakeIdentity, FakeWebSocket, FakeWebSocketConnection
from clytie.sdk.testing.context_contracts import (
    assert_channel_credentials_contract,
    assert_websocket_contract,
)

from .host_helpers import SOURCE, enable, host, plugin

CREDENTIAL_FILE = "secret"


CHANNEL_SOURCE = (
    SOURCE
    + """
    def channel(self):
        from clytie.sdk.testing import FakeChannel
        return FakeChannel(self.ctx)
"""
)


async def test_two_companions_channel_accounts_and_secrets_are_isolated(tmp_path):
    plugin(
        tmp_path,
        "bound-channel",
        source=CHANNEL_SOURCE,
        contributes={"channel": {"factory": "channel", "platform": "fictional"}},
    )
    instance = {
        "accounts": {
            "first": {"account": "fictional_first", CREDENTIAL_FILE: "first.txt"},
            "second": {"account": "fictional_second", CREDENTIAL_FILE: "second.txt"},
        }
    }
    (tmp_path / "secrets").mkdir()
    for ref in ("first", "second"):
        (tmp_path / "secrets" / (ref + ".txt")).write_text("fictional_token_" + ref)
    h = host(tmp_path)
    h.accounts = instance["accounts"]
    h.configure("companion_b")
    try:
        for cid, ref in (("companion_a", "first"), ("companion_b", "second")):
            h.unit_configs[cid, "bound-channel"] = [
                {"role": "channel", "account": ref, "config": {}}
            ]
            h.identities[cid, "bound-channel", ref] = IdentityResolver(
                ref, "bound-channel", instance
            )
            runtime = await enable(h, "bound-channel", cid)
            assert runtime.ctx.identity.self_account() == "fictional_" + ref
            await assert_channel_credentials_contract(
                runtime.ctx.credentials, expected="fictional_token_" + ref
            )
            assert not hasattr(runtime.ctx.credentials, "home")
        assert "fictional_token" not in repr(h.log.entries)
        assert "fictional_token" not in repr(h.failures)
    finally:
        await h.close()


async def test_unbound_channel_and_nonchannel_surfaces(tmp_path):
    plugin(
        tmp_path,
        "bound-channel",
        source=CHANNEL_SOURCE,
        contributes={"channel": {"factory": "channel", "platform": "fictional"}},
    )
    plugin(tmp_path, "other")
    identity = IdentityResolver("unbound", "bound-channel", {})
    h = host(tmp_path, backends=lambda cid, pid: {"identity": identity})
    try:
        channel = await enable(h, "bound-channel")
        assert channel.ctx.identity.self_account() is None
        await assert_channel_credentials_contract(channel.ctx.credentials, error="unknown_ref")
        with pytest.raises(CredentialUnavailable):
            await channel.ctx.credentials.account_secret()
        other = await enable(h, "other")
        assert not hasattr(other.ctx, "identity")
        assert not hasattr(other.ctx, "credentials")
    finally:
        await h.close()


async def test_fake_websocket_contract_and_identity():
    assert FakeIdentity("fake-channel").self_account() is None
    assert (
        FakeIdentity("fake-channel", self_account="fictional_self").self_account()
        == "fictional_self"
    )
    connection = FakeWebSocketConnection(["fictional incoming"])
    await assert_websocket_contract(
        FakeWebSocket([connection]), CancelToken(), "fictional incoming"
    )


async def test_fake_websocket_receive_cancellation_closes():
    connection = FakeWebSocketConnection()
    cancel = CancelToken()
    task = asyncio.create_task(connection.receive(cancel))
    from clytie.sdk.testing import checkpoint

    await checkpoint()
    cancel.cancel("test")
    with pytest.raises((Cancelled, asyncio.CancelledError)):
        await task
    assert connection.closed
    with pytest.raises(WebSocketClosed):
        await connection.send_text("cannot send")


@pytest.mark.parametrize("stop_fails", [False, True])
async def test_default_websocket_owner_isolation_and_stop_cleanup(tmp_path, stop_fails):
    source = SOURCE
    if stop_fails:
        source = source.replace(
            "await self.ctx.clock.sleep(0)", "raise ValueError('fictional stop failure')", 1
        )
    plugin(tmp_path, "network-one", source=source, permissions=["network"])
    plugin(tmp_path, "network-two", permissions=["network"])
    plugin(tmp_path, "local-only")
    h = host(tmp_path)
    try:
        one = await enable(h, "network-one")
        two = await enable(h, "network-two")
        local = await enable(h, "local-only")
        assert one.owned_websocket is not two.owned_websocket
        assert one.owned_websocket._session is None
        assert two.owned_websocket._session is None
        assert not one.owned_websocket._closed
        assert not two.owned_websocket._closed
        assert hasattr(one.ctx, "websocket")
        assert not hasattr(local.ctx, "websocket")
        assert local.owned_websocket is None
        await h.apply(h.plan_disable("companion_a", "network-one").plan_id)
        assert one.owned_websocket._closed
        assert not two.owned_websocket._closed
    finally:
        await h.close()
    assert two.owned_websocket._closed


async def test_default_websocket_closed_after_failed_plugin_start(tmp_path):
    plugin(
        tmp_path,
        "bad-network",
        source=SOURCE.replace("self.seen = []", "raise ValueError('fictional start failure')"),
        permissions=["network"],
    )
    h = host(tmp_path)
    try:
        result = await h.apply(h.plan_enable("companion_a", "bad-network").plan_id)
        assert result["failed"]
        runtime = h.runtimes["companion_a", "bad-network"]
        assert runtime.owned_websocket._closed
        assert runtime.owned_websocket._session is None
    finally:
        await h.close()


async def test_websocket_facade_properties_stay_live_and_hide_backend(tmp_path):
    connection = FakeWebSocketConnection(["fictional incoming"])
    backend = FakeWebSocket([connection])
    plugin(tmp_path, "network-one", permissions=["network"])
    plugin(tmp_path, "local-only")
    h = host(tmp_path, backends=lambda cid, pid: {"websocket": backend})
    try:
        runtime = await enable(h, "network-one")
        local = await enable(h, "local-only")
        assert not hasattr(local.ctx, "websocket")
        assert not hasattr(runtime.ctx.websocket, "_backend")
        assert not hasattr(runtime.ctx.websocket, "connections")
        facade = await runtime.ctx.websocket.connect("ws://fictional.invalid")
        assert not facade.closed
        assert facade.close_code is None
        for hidden in ("_backend", "_ws", "messages", "sent", "__dict__"):
            assert not hasattr(facade, hidden)
        assert await facade.receive(CancelToken()) == "fictional incoming"
        await facade.send_text("fictional outgoing")
        assert connection.sent == ["fictional outgoing"]
        await connection.close()
        assert facade.closed
        assert facade.close_code == 1000
        with pytest.raises(WebSocketClosed):
            await facade.send_text("already closed")
        await facade.close()
    finally:
        await h.close()
