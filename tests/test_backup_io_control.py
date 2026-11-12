import asyncio

import pytest

from clytie.backup.io_budget import RATE_BUSY, RATE_NORMAL, IoBudgetError
from clytie.backup.io_control import IoControl
from clytie.sdk.testing import MockClock


def message(op="backup_a", stage="stage_a", action="acquire"):
    return {
        "type": "backup_io",
        "id": stage + "_" + action,
        "operation_id": op,
        "arguments": {"action": action, "stage": stage},
    }


async def test_control_shared_worker_and_companion_slot_and_verified_exit():
    clock = MockClock()
    control = IoControl(clock)
    worker = control.register()
    companion = control.register(storage_owner=True)
    control.authorize(worker, "backup_a")
    control.authorize(companion, "backup_a")
    replies = []

    async def send(reply):
        replies.append(reply)

    await control.dispatch(worker, message(), send)
    pending = control.dispatch(companion, message(stage="stage_b"), send)
    await clock.drain()
    assert control.budget.status()["waiting"] == 1
    control.disconnect(worker)
    await clock.drain()
    assert not pending.done()
    with pytest.raises(IoBudgetError, match="unconfirmed"):
        await control.ended(worker, confirmed=False)
    await control.ended(worker, confirmed=True)
    await pending
    assert replies[-1]["result"] == {}
    await control.dispatch(companion, message(stage="stage_b", action="release"), send)
    await control.ended(companion, confirmed=True)
    assert control.budget.status()["active"] is None


async def test_unauthorized_operation_cannot_allocate_or_send_arbitrary_error():
    control = IoControl(MockClock())
    session = control.register()
    replies = []

    async def send(reply):
        replies.append(reply)

    await control.dispatch(session, message(op="other_operation"), send)
    assert replies[-1]["error"] == "backup_io_operation_not_authorized"
    assert control.budget.status()["active"] is None
    await control.ended(session, confirmed=True)


async def test_pressure_bound_to_live_owner_and_stale_signal_busy():
    clock = MockClock()
    control = IoControl(clock)
    session = control.register(storage_owner=True)
    sample = {
        "source": "owner_control_cache",
        "ready_wait_seconds": 0,
        "execution_seconds": 0,
        "finalizer_wait_seconds": 0,
        "finalizer_capacity_blocked": False,
    }
    control.pressure(session, sample)
    for _ in range(11):
        await clock.advance_and_drain(0.1)
        control.pressure(session, sample)
    assert control.budget.status()["rate"] == RATE_NORMAL
    await clock.advance_and_drain(1.01)
    assert control.budget.status()["rate"] == RATE_BUSY
    with pytest.raises(IoBudgetError):
        control.pressure(control.register(), sample)
    await control.ended(session, confirmed=True)


async def test_transport_failure_does_not_free_executor_occupancy():
    control = IoControl(MockClock())
    session = control.register()
    control.authorize(session, "backup_a")

    async def failed_send(reply):
        raise BrokenPipeError()

    await control.dispatch(session, message(), failed_send)
    assert control.budget.status()["active"]["disconnected"]
    await control.ended(session, confirmed=True)
    assert control.budget.status()["active"] is None


async def test_finished_operation_retires_history_only_after_real_release():
    control = IoControl(MockClock())
    companion = control.register(storage_owner=True)
    control.authorize(companion, "backup_a")
    replies = []

    async def send(reply):
        replies.append(reply)

    await control.dispatch(companion, message(), send)
    assert not control.finish_operation("backup_a")
    assert control.budget.status()["active"] is not None
    await control.dispatch(companion, message(stage="late"), send)
    assert replies[-1]["error"] == "backup_io_operation_not_authorized"
    await control.dispatch(companion, message(action="release"), send)
    assert control.finish_operation("backup_a")
    assert not control.sessions[companion].endpoints
    assert not control.budget._stages
    await control.dispatch(companion, message(), send)
    assert replies[-1]["error"] == "backup_io_operation_not_authorized"
    await control.ended(companion, confirmed=True)


async def test_monitor_and_stop_can_confirm_same_exit_concurrently():
    control = IoControl(MockClock())
    session = control.register()
    pending = asyncio.create_task(control.budget.clock.sleep(10))
    control.sessions[session].tasks.add(pending)
    await asyncio.gather(
        control.ended(session, confirmed=True), control.ended(session, confirmed=True)
    )
    assert not control.sessions
