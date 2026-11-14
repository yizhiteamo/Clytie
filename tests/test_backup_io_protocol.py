import asyncio

import pytest

from clytie.backup.io_budget import CHUNK, IoBudget, IoBudgetError
from clytie.backup.io_protocol import BudgetEndpoint
from clytie.sdk.testing import MockClock


async def test_endpoint_binds_executor_and_operation_and_deduplicates_charge():
    budget = IoBudget(MockClock())
    endpoint = BudgetEndpoint(budget, executor="executor_a", operation="backup_a")
    await endpoint.request({"action": "acquire", "stage": "phase_a"})
    charge = {
        "action": "charge",
        "stage": "phase_a",
        "sequence": 1,
        "direction": "read",
        "size": CHUNK,
    }
    await endpoint.request(charge)
    await endpoint.request(charge)
    assert budget.status()["charged"]["read"] == CHUNK
    with pytest.raises(IoBudgetError, match="backup_io_request_invalid"):
        await endpoint.request({"action": "release", "stage": "phase_a", "executor": "other"})
    await endpoint.request({"action": "release", "stage": "phase_a"})
    await endpoint.request({"action": "release", "stage": "phase_a"})
    assert budget.status()["active"] is None
    with pytest.raises(IoBudgetError, match="backup_io_stage_finished"):
        await endpoint.request({"action": "acquire", "stage": "phase_a"})


async def test_completed_phase_cancels_queued_acquisition_without_waiting_for_holder():
    clock = MockClock()
    budget = IoBudget(clock)
    first = BudgetEndpoint(budget, executor="executor_a", operation="backup_a")
    second = BudgetEndpoint(budget, executor="executor_b", operation="backup_a")
    await first.request({"action": "acquire", "stage": "phase_a"})
    pending = asyncio.create_task(second.request({"action": "acquire", "stage": "phase_b"}))
    await clock.drain()
    assert budget.status()["waiting"] == 1
    await second.request({"action": "release", "stage": "phase_b"})
    assert budget.status()["waiting"] == 0
    with pytest.raises(IoBudgetError, match="backup_io_stage_finished"):
        await pending
    assert budget.status()["active"] is not None
    await first.request({"action": "release", "stage": "phase_a"})


async def test_release_before_delayed_acquire_is_tombstoned_not_replayed():
    budget = IoBudget(MockClock())
    endpoint = BudgetEndpoint(budget, executor="executor_a", operation="backup_a")
    await endpoint.request({"action": "release", "stage": "phase_a"})
    with pytest.raises(IoBudgetError, match="backup_io_stage_finished"):
        await endpoint.request({"action": "acquire", "stage": "phase_a"})
    assert budget.status()["active"] is None


async def test_release_during_token_wait_settles_tracking_and_does_not_charge_again():
    clock = MockClock()
    budget = IoBudget(clock)
    endpoint = BudgetEndpoint(budget, executor="executor_a", operation="backup_a")
    await endpoint.request({"action": "acquire", "stage": "phase_a"})
    for sequence in (1, 2):
        await endpoint.request(
            {
                "action": "charge",
                "stage": "phase_a",
                "sequence": sequence,
                "direction": "read",
                "size": CHUNK,
            }
        )
    pending = asyncio.create_task(
        endpoint.request(
            {
                "action": "charge",
                "stage": "phase_a",
                "sequence": 3,
                "direction": "write",
                "size": CHUNK,
            }
        )
    )
    await clock.drain()
    await endpoint.request({"action": "release", "stage": "phase_a"})
    with pytest.raises(IoBudgetError, match="backup_io_stage_finished"):
        await pending
    assert clock.pending_sleep_count == 0
    assert budget.status()["charged"]["write"] == 0
    assert budget.status()["active"] is None
