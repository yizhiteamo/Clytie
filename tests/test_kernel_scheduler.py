"""模拟时钟量化调度行为；没有真实时间等待或平台连接。"""

import asyncio
from datetime import datetime, timezone

import pytest

from clytie.kernel.events import EventBus
from clytie.kernel.scheduler import Scheduler
from clytie.plugin_host.support import HostLog
from clytie.sdk.testing import MockClock


async def test_jump_latest_tick_sorted_and_no_restart_catchup():
    clock, log, calls = MockClock(), HostLog(), []
    scheduler = Scheduler(clock, log)

    def handler(pid):
        async def run(tick, cancel):
            calls.append((pid, tick))

        return run

    for pid in ("second", "first"):
        scheduler.add(pid, dict(id="tick", every="PT10S", handler="on_tick"), handler(pid))
    await clock.drain()
    clock.advance(35)
    await clock.drain()
    assert [p for p, _ in calls] == ["first", "second"]
    assert all(t.missed == 2 and t.scheduled_at.second == 30 for _, t in calls)
    await scheduler.close()
    clock.advance(100)
    fresh = Scheduler(clock, log)
    fresh.add("first", dict(id="tick", every="PT10S", handler="on_tick"), handler("first"))
    await clock.advance_and_drain(9)
    assert len(calls) == 2
    await clock.advance_and_drain(1)
    assert calls[-1][1].missed == 0
    await fresh.close()


async def test_nonoverlap_accumulates_missed_and_disable_cancels():
    clock, log, calls = MockClock(), HostLog(), []
    release = asyncio.Event()
    scheduler = Scheduler(clock, log)

    async def run(tick, cancel):
        calls.append((tick, cancel))
        if len(calls) == 1:
            await release.wait()
        else:
            await cancel.wait()

    scheduler.add("example", dict(id="tick", every="PT10S", handler="on_tick"), run)
    await clock.advance_and_drain(30)
    assert len(calls) == 1
    release.set()
    await clock.drain()
    await clock.advance_and_drain(10)
    assert len(calls) == 2 and calls[1][0].missed == 2
    await scheduler.remove("example")
    assert calls[1][1].cancelled
    await clock.advance_and_drain(100)
    assert len(calls) == 2
    await scheduler.close()


@pytest.mark.parametrize(
    "start,at,expected",
    [
        (
            datetime(2025, 3, 9, 6, 59, tzinfo=timezone.utc),
            "02:30",
            datetime(2025, 3, 9, 7, 0, tzinfo=timezone.utc),
        ),
        (
            datetime(2025, 11, 2, 5, 29, tzinfo=timezone.utc),
            "01:30",
            datetime(2025, 11, 2, 5, 30, tzinfo=timezone.utc),
        ),
    ],
)
async def test_dst_nonexistent_moves_forward_and_repeated_only_once(start, at, expected):
    clock, calls = MockClock(start), []
    scheduler = Scheduler(clock, HostLog(), "America/New_York")

    async def run(tick, cancel):
        calls.append(tick)

    scheduler.add("daily", dict(id="tick", at=at, days=[], handler="on_tick"), run)
    await clock.advance_and_drain(60)
    assert len(calls) == 1 and calls[0].scheduled_at == expected
    await clock.advance_and_drain(3600)
    assert len(calls) == 1
    await scheduler.close()


async def test_event_order_recursion_limit_and_broken_subscriber_isolation():
    clock, log, received = MockClock(), HostLog(), []

    async def broken(event):
        raise ValueError("fictional subscriber failure")

    async def recursive(event):
        received.append(event)
        bus.publish("companion_a", event + 1)

    bus = EventBus(clock, log, lambda cid, e: (broken, recursive))
    bus.publish("companion_a", 0)
    await bus.drain()
    assert received == list(range(8))
    assert bus.dropped == 1
    assert len([x for x in log.entries if x[1] == "event.subscriber_failed"]) == 8


async def test_bus_timeout_does_not_block_other_companion_or_publisher():
    clock, log, received = MockClock(), HostLog(), []

    async def slow(event):
        await clock.sleep(100)

    async def fast(event):
        received.append(event)

    bus = EventBus(
        clock, log, lambda cid, e: (slow, fast) if cid == "companion_a" else (fast,), capacity=1
    )
    assert bus.publish("companion_a", "a")
    assert not bus.publish("companion_a", "overflow")
    bus.publish("companion_b", "b")
    await clock.drain()
    assert received == ["b"]
    await clock.advance_and_drain(2)
    await bus.drain()
    assert received == ["b", "a"]


async def test_bus_subscriber_cancel_isolated_but_dispatch_cancel_propagates():
    import asyncio

    clock, log, received = MockClock(), HostLog(), []
    entered = asyncio.Event()

    async def cancelled(event):
        raise asyncio.CancelledError

    async def slow(event):
        received.append(event)
        entered.set()
        await clock.sleep(100)

    bus = EventBus(clock, log, lambda cid, e: (cancelled, slow))
    bus.publish("companion_a", "first")
    bus.publish("companion_a", "queued")
    await entered.wait()
    worker = bus.workers["companion_a"]
    await bus.close()
    assert worker.cancelled()
    assert received == ["first"]
    assert not bus.publish("companion_a", "after_close")
    assert not bus.workers and not bus.queues
    assert len([x for x in log.entries if x[1] == "event.subscriber_failed"]) == 1
