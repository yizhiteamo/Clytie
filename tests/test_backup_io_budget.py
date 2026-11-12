import asyncio

import pytest

from clytie.backup.io_budget import (
    BURST,
    CHUNK,
    RATE_BUSY,
    RATE_NORMAL,
    IoBudget,
    IoBudgetError,
    StageKey,
)
from clytie.sdk.testing import MockClock


def key(number, *, executor=None):
    return StageKey(executor or f"executor_{number}", "backup_a", f"stage_{number}")


async def test_single_stage_eight_waiters_fifo_and_replay():
    clock = MockClock()
    budget = IoBudget(clock)
    first = await budget.acquire(key(0))
    assert await budget.acquire(key(0)) is first
    waiting = [asyncio.create_task(budget.acquire(key(i))) for i in range(1, 9)]
    await clock.drain()
    assert budget.status()["waiting"] == 8
    with pytest.raises(IoBudgetError, match="backup_io_busy"):
        await budget.acquire(key(9))
    budget.release(first)
    for index, task in enumerate(waiting):
        await clock.drain()
        assert task.done()
        assert all(not value.done() for value in waiting[index + 1 :])
        budget.release(await task)
    budget.release(first)  # transport replay cannot release another stage
    with pytest.raises(IoBudgetError, match="backup_io_stage_finished"):
        await budget.acquire(key(0))
    assert budget.status()["active"] is None


async def test_independent_ready_stages_from_same_executor_can_queue():
    clock = MockClock()
    budget = IoBudget(clock)
    first = await budget.acquire(key(0))
    second = asyncio.create_task(budget.acquire(key(1, executor="executor_0")))
    await clock.drain()
    assert not second.done()
    assert budget.status()["waiting"] == 1
    budget.release(first)
    budget.release(await second)


async def test_queue_cancellation_is_not_executor_exit_after_grant():
    clock = MockClock()
    budget = IoBudget(clock)
    first = await budget.acquire(key(0))
    cancelled = asyncio.create_task(budget.acquire(key(1)))
    second = asyncio.create_task(budget.acquire(key(2)))
    await clock.drain()
    cancelled.cancel()
    await asyncio.gather(cancelled, return_exceptions=True)
    assert budget.status()["waiting"] == 1
    budget.release(first)
    lease = await second
    budget.disconnected("executor_2")
    blocked = asyncio.create_task(budget.acquire(key(3)))
    await clock.drain()
    assert not blocked.done()
    with pytest.raises(IoBudgetError, match="backup_io_exit_unconfirmed"):
        budget.executor_ended("executor_2", confirmed=False)
    assert budget.status()["active"]["disconnected"]
    budget.executor_ended("executor_2", confirmed=True)
    budget.release(await blocked)
    with pytest.raises(IoBudgetError, match="backup_io_executor_lost"):
        await budget.charge(lease, 1, "read", 1)


async def test_read_write_share_bucket_and_stage_switch_does_not_refill_burst():
    clock = MockClock()
    budget = IoBudget(clock)
    lease = await budget.acquire(key(0))
    await budget.charge(lease, 1, "read", CHUNK)
    await budget.charge(lease, 2, "write", CHUNK)
    assert budget.status()["charged"] == {"read": CHUNK, "write": CHUNK}
    await budget.charge(lease, 2, "write", CHUNK)  # same result, not another charge
    with pytest.raises(IoBudgetError, match="backup_io_sequence_mismatch"):
        await budget.charge(lease, 2, "read", CHUNK)
    budget.release(lease)
    next_lease = await budget.acquire(key(1))
    task = asyncio.create_task(budget.charge(next_lease, 1, "read", CHUNK))
    await clock.drain()
    assert not task.done()
    await clock.advance_and_drain(CHUNK / RATE_BUSY)
    assert task.done()
    await task
    assert budget.status()["charged"] == {"read": 2 * CHUNK, "write": CHUNK}
    budget.release(next_lease)
    assert clock.pending_sleep_count == 0


async def test_pressure_hysteresis_stale_and_explicit_empty_instance():
    clock = MockClock()
    budget = IoBudget(clock)
    assert budget.status()["rate"] == RATE_BUSY
    budget.set_owners({"companion_a", "companion_b"})
    for _ in range(12):
        for cid in ("companion_a", "companion_b"):
            budget.pressure(cid, waiting=0.001, executing=0.001, finalizer_wait=False)
        await clock.advance_and_drain(0.1)
    assert budget.status()["rate"] == RATE_NORMAL
    budget.pressure("companion_b", waiting=0.020, executing=0, finalizer_wait=False)
    assert budget.status()["rate"] == RATE_BUSY
    for _ in range(12):
        for cid in ("companion_a", "companion_b"):
            budget.pressure(cid, waiting=0.004, executing=0.004, finalizer_wait=False)
        await clock.advance_and_drain(0.1)
    assert budget.status()["rate"] == RATE_NORMAL
    budget.pressure("companion_a", waiting=0, executing=0, finalizer_wait=True)
    assert budget.status()["rate"] == RATE_BUSY
    budget.set_owners(set())  # trusted control-plane acknowledgement
    await clock.advance_and_drain(1)
    assert budget.status()["rate"] == RATE_NORMAL
    budget.set_owners({"companion_a"})
    assert budget.status()["rate"] == RATE_BUSY
    for _ in range(12):
        budget.pressure("companion_a", waiting=0, executing=0, finalizer_wait=False)
        await clock.advance_and_drain(0.1)
    assert budget.status()["rate"] == RATE_NORMAL
    await clock.advance_and_drain(1.01)
    assert budget.status()["rate"] == RATE_BUSY


async def test_reconnect_and_single_inflight_charge_do_not_reset_budget():
    clock = MockClock()
    budget = IoBudget(clock)
    lease = await budget.acquire(key(0))
    await budget.charge(lease, 1, "read", CHUNK)
    await budget.charge(lease, 2, "write", CHUNK)
    pending = asyncio.create_task(budget.charge(lease, 3, "read", CHUNK))
    duplicate = asyncio.create_task(budget.charge(lease, 3, "read", CHUNK))
    await clock.drain()
    with pytest.raises(IoBudgetError, match="backup_io_sequence_mismatch"):
        await budget.charge(lease, 4, "read", 1)
    pending.cancel()  # waiter lost, budget request remains tracked
    await asyncio.gather(pending, return_exceptions=True)
    await clock.advance_and_drain(CHUNK / RATE_BUSY)
    await duplicate
    assert budget.status()["charged"]["read"] == CHUNK * 2
    budget.release(lease)


@pytest.mark.parametrize("size", [True, -1, CHUNK + 1, 1.5])
async def test_invalid_chunk_never_changes_bucket(size):
    budget = IoBudget(MockClock())
    lease = await budget.acquire(key(0))
    with pytest.raises(IoBudgetError, match="backup_io_request_invalid"):
        await budget.charge(lease, 1, "read", size)
    assert budget.status()["available"] == BURST
    budget.release(lease)


async def test_disconnect_during_budget_wait_retains_stage_until_exit():
    clock = MockClock()
    budget = IoBudget(clock)
    lease = await budget.acquire(key(0))
    for seq, direction in enumerate(("read", "write"), 1):
        await budget.charge(lease, seq, direction, CHUNK)
    task = asyncio.create_task(budget.charge(lease, 3, "read", CHUNK))
    await clock.drain()
    budget.disconnected("executor_0")
    await clock.drain()
    with pytest.raises(IoBudgetError, match="backup_io_executor_lost"):
        await task
    assert budget.status()["active"] is not None
    budget.executor_ended("executor_0", confirmed=True)
    assert budget.status()["active"] is None
    assert clock.pending_sleep_count == 0


async def test_cancel_waiter_after_grant_cannot_silently_reuse_stage():
    clock = MockClock()
    budget = IoBudget(clock)
    first = await budget.acquire(key(0))
    waiter = asyncio.create_task(budget.acquire(key(1)))
    await clock.drain()
    budget.release(first)  # grant, before caller receives the reply
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    assert budget.status()["active"] is not None
    budget.executor_ended("executor_1", confirmed=True)
    assert budget.status()["active"] is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, "slow", True])
async def test_invalid_pressure_is_not_zero_or_no_business(bad):
    clock = MockClock()
    budget = IoBudget(clock)
    budget.set_owners({"companion_a"})
    with pytest.raises(IoBudgetError, match="backup_io_pressure_invalid"):
        budget.pressure("companion_a", waiting=bad, executing=0, finalizer_wait=False)
    assert budget.status()["rate"] == RATE_BUSY
    with pytest.raises(IoBudgetError, match="backup_io_pressure_owner_unknown"):
        budget.pressure("companion_b", waiting=0, executing=0, finalizer_wait=False)
