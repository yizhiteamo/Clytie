import asyncio

import pytest

from clytie.backup.coordination import run_sync
from clytie.kernel.ledger import LedgerError
from clytie.sdk.testing import FakeChannel, MockClock
from clytie.sdk.types import ChannelState
from clytie.storage.owner import StorageBusy
from clytie.supervisor.companion import Companion

from .test_storage_agent_lifecycle import full_queue, inbound, private_home
from .test_storage_kernel_finalizers import spin


@pytest.fixture
async def feed(tmp_path, monkeypatch):
    queue = asyncio.Queue()
    fetched = []

    async def events(channel):
        while True:
            item = await queue.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            fetched.append(item)
            yield item

    monkeypatch.setattr(FakeChannel, "events", events)
    monkeypatch.setattr(
        FakeChannel, "state", lambda self: ChannelState(status="online"), raising=False
    )
    runtime = Companion(private_home(tmp_path), "companion_a", MockClock())
    await runtime.start()
    try:
        yield runtime, queue, fetched
    finally:
        await runtime.stop()


async def test_receive_failure_ignores_only_failed_message_and_continues(feed, monkeypatch):
    runtime, queue, fetched = feed
    first, second, third = (inbound(runtime) for _ in range(3))
    mark = runtime.agent._mark_addressed

    async def mark_addressed(event, finalization):
        if event.event_id == first.event_id:
            raise LedgerError("lookup_failed", "fictional sensitive detail")
        return await mark(event, finalization)

    monkeypatch.setattr(runtime.agent, "_mark_addressed", mark_addressed)
    for item in (first, second, third, None):
        queue.put_nowait(item)
    await asyncio.gather(*runtime.feeds, return_exceptions=True)
    assert fetched == [first, second, third]
    ledger = runtime.kernel.ledger
    rows = [await run_sync(ledger, ledger.get, "inbound", e.event_id) for e in fetched]
    assert rows[0]["state"] == "ignored"
    assert all(row and row["state"] != "ignored" for row in rows[1:])
    assert "inbound.failed=1" in runtime.health()["warnings"]
    warnings = [entry for entry in runtime.log.entries if entry[1] == "inbound.receive_failed"]
    assert warnings == [
        (
            "warning",
            "inbound.receive_failed",
            {"error_type": "LedgerError", "code": "lookup_failed"},
        )
    ]


async def test_storage_busy_retries_in_order_with_capped_backoff_and_dedup(feed, monkeypatch):
    runtime, queue, fetched = feed
    first, second = inbound(runtime), inbound(runtime)
    clock = runtime.clock
    receive = runtime.agent.receive
    attempts = []
    delays = [1, 2, 4, 8, 16, 30, 30]

    async def sometimes_busy(event):
        attempts.append(event)
        if len(attempts) <= len(delays):
            raise StorageBusy()
        await receive(event)

    monkeypatch.setattr(runtime.agent, "receive", sometimes_busy)
    for item in (first, first, second, None):
        queue.put_nowait(item)
    for i, delay in enumerate(delays):
        await clock.drain()
        assert fetched == [first]
        assert len(attempts) == i + 1
        assert "inbound.storage_busy=1" in runtime.health()["warnings"]
        await clock.advance_and_drain(delay - 0.25)
        assert len(attempts) == i + 1
        await clock.advance_and_drain(0.25)
    await asyncio.gather(*runtime.feeds)
    assert attempts == [first] * (len(delays) + 2) + [second]
    assert runtime.kernel.duplicates == 1
    assert not any(
        w.startswith(("inbound.failed", "inbound.storage_busy"))
        for w in runtime.health()["warnings"]
    )


async def test_full_storage_queue_delays_claim_then_accepts_once(feed):
    runtime, queue, fetched = feed
    first = inbound(runtime)
    ledger = runtime.kernel.ledger
    async with full_queue(runtime.storage_owner):
        for item in (first, first, None):
            queue.put_nowait(item)
        await spin(lambda: "inbound.storage_busy=1" in runtime.health()["warnings"])
        assert fetched == [first]
    assert await run_sync(ledger, ledger.get, "inbound", first.event_id) is None
    await runtime.clock.advance_and_drain(1)
    await asyncio.gather(*runtime.feeds)
    assert await run_sync(ledger, ledger.get, "inbound", first.event_id)
    assert runtime.kernel.duplicates == 1


async def test_stopping_cancels_busy_feed_without_consuming_next(feed, monkeypatch):
    runtime, queue, fetched = feed
    first, second = inbound(runtime), inbound(runtime)

    async def busy(event):
        raise StorageBusy()

    monkeypatch.setattr(runtime.agent, "receive", busy)
    queue.put_nowait(first)
    queue.put_nowait(second)
    await runtime.clock.drain()
    assert "inbound.storage_busy=1" in runtime.health()["warnings"]
    await runtime.stop()
    assert runtime.feeds[0].cancelled()
    assert fetched == [first]
    assert not any(w.startswith("inbound.") for w in runtime.health()["warnings"])


async def test_consume_during_stop_records_ignored_inbound(feed, monkeypatch):
    runtime, queue, _ = feed
    retrying, busy, incoming = (inbound(runtime) for _ in range(3))
    attempts = []
    received = asyncio.Event()
    receive = runtime.agent.receive

    async def receive_event(event):
        attempts.append(event)
        if event in (retrying, busy):
            raise StorageBusy()
        await receive(event)
        received.set()

    monkeypatch.setattr(runtime.agent, "receive", receive_event)
    queue.put_nowait(retrying)
    await spin(lambda: "inbound.storage_busy=1" in runtime.health()["warnings"])

    # Hold wait-mode shutdown at its drain step, before it cancels the channel feed.
    release = asyncio.Event()
    monkeypatch.setattr(runtime.agent, "drain", release.wait)
    stopping = asyncio.create_task(runtime.stop("wait"))
    try:
        await spin(lambda: runtime.agent.shutting_down)
        queue.put_nowait(busy)
        queue.put_nowait(incoming)
        await runtime.clock.advance_and_drain(1)
        feed_task = runtime.feeds[0]
        await spin(lambda: received.is_set() or feed_task.done())
        ledger = runtime.kernel.ledger
        row = await run_sync(ledger, ledger.get, "inbound", incoming.event_id)
        assert row and (row["state"], row["reason"]) == ("ignored", "shutting_down")
        assert [r["event_id"] for r in await run_sync(ledger, ledger.rows, "dedupe")] == [
            incoming.event_id
        ]
        assert attempts == [retrying, busy, incoming]
        assert not feed_task.done()
    finally:
        release.set()
        await stopping
    assert feed_task.cancelled()
    assert not any(w.startswith("inbound.") for w in runtime.health()["warnings"])


@pytest.mark.parametrize("end", [None, RuntimeError("fictional iterator failure")])
async def test_unexpected_feed_end_reported_even_when_channel_online(feed, end):
    runtime, queue, _ = feed
    queue.put_nowait(end)
    await asyncio.gather(*runtime.feeds, return_exceptions=True)
    health = runtime.health()
    assert "online" in health["channels"].values()
    assert "inbound.feed_ended=1" in health["warnings"]
    assert "inbound.failed=1" not in health["warnings"]
