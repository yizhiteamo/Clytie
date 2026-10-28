import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from clytie.sdk.testing import MockClock, assert_clock_contract, checkpoint


async def test_mock_clock_protocol_and_no_real_time():
    local = timezone(timedelta(hours=3))
    clock = MockClock(datetime(2000, 1, 1, 3, tzinfo=local), local_tz=local)
    await assert_clock_contract(clock)
    assert clock.now() == datetime(2000, 1, 1, tzinfo=timezone.utc)
    assert clock.monotonic() == 0
    assert clock.local_tz is local
    assert clock.pending_sleep_count == 0


async def test_deadline_order_then_registration_order():
    clock = MockClock()
    order = []

    async def sleeper(label, delay):
        await clock.sleep(delay)
        order.append(label)

    tasks = [
        asyncio.create_task(sleeper("later", 3)),
        asyncio.create_task(sleeper("first-at-one", 1)),
        asyncio.create_task(sleeper("second-at-one", 1)),
        asyncio.create_task(sleeper("middle", 2)),
    ]
    await checkpoint()
    assert clock.pending_sleep_count == 4
    clock.advance(3)
    await asyncio.gather(*tasks)
    assert order == ["first-at-one", "second-at-one", "middle", "later"]
    assert clock.pending_sleep_count == 0
    assert clock.monotonic() == 3


async def test_advance_only_wakes_due_sleepers_and_set_uses_utc():
    clock = MockClock()
    initial = clock.now()
    task = asyncio.create_task(clock.sleep_until(initial + timedelta(seconds=2)))
    await checkpoint()
    clock.advance(1)
    await checkpoint()
    assert not task.done()
    clock.set((initial + timedelta(seconds=2)).astimezone(timezone(timedelta(hours=5))))
    await task
    assert clock.now() == initial + timedelta(seconds=2)
    assert clock.monotonic() == 2


async def test_cancelled_sleep_is_removed_immediately():
    clock = MockClock()
    task = asyncio.create_task(clock.sleep(100))
    await checkpoint()
    assert clock.pending_sleep_count == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert clock.pending_sleep_count == 0
    clock.advance(100)


async def test_sleep_zero_and_past_deadline_yield_without_advancing():
    clock = MockClock()
    await clock.sleep(0)
    await clock.sleep_until(clock.now() - timedelta(days=1))
    assert clock.monotonic() == 0
    assert clock.pending_sleep_count == 0


async def test_same_operation_sequence_is_reproducible():
    async def replay():
        clock = MockClock()
        seen = []

        async def actor(label):
            await clock.sleep(1)
            seen.append((label, clock.monotonic()))
            await clock.sleep(2)
            seen.append((label, clock.monotonic()))

        tasks = [asyncio.create_task(actor(label)) for label in ("a", "b")]
        await checkpoint()
        clock.advance(1)
        await checkpoint()
        clock.advance(2)
        await asyncio.gather(*tasks)
        return seen

    assert await replay() == await replay() == [("a", 1), ("b", 1), ("a", 3), ("b", 3)]


@pytest.mark.parametrize("seconds", [-1, float("inf"), float("nan")])
async def test_invalid_durations_are_rejected(seconds):
    clock = MockClock()
    with pytest.raises(ValueError):
        clock.advance(seconds)
    with pytest.raises(ValueError):
        await clock.sleep(seconds)


def test_naive_or_backwards_set_is_rejected():
    clock = MockClock()
    with pytest.raises(ValueError):
        MockClock(datetime(2000, 1, 1))
    with pytest.raises(ValueError):
        clock.set(datetime(2000, 1, 1))
    with pytest.raises(ValueError):
        clock.set(clock.now() - timedelta(seconds=1))
