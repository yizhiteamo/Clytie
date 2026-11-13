"""Round-two admission regression: fictional data, real scheduler and worker gate."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from clytie.backup.controller import BackupController
from clytie.backup.io_budget import IoBudgetError
from clytie.backup.workers import BackupWorkers
from clytie.clock import SystemClock
from clytie.sdk.testing import MockClock


@pytest.mark.parametrize("reason", ["backup_io_recheck_required", "backup_io_coordination_busy"])
async def test_automatic_claim_admission_failure_keeps_retry_budget_and_loop(
    tmp_path, monkeypatch, reason
):
    clock = MockClock()
    supervisor = SimpleNamespace(home=tmp_path, clock=clock, companions=[], problems={})
    workers = BackupWorkers(tmp_path, clock)
    controller = BackupController(
        supervisor, workers, {"backup": {"schedule": {"catch_up": False}}}
    )
    controller.scheduler.mark_dirty("companion_a", 1, "fact")
    clock.advance(300)
    rejected, checks, accepted = True, [], []

    async def readiness():
        checks.append(clock.now())
        if rejected:
            raise IoBudgetError(reason)

    def complete_fake_backend(journal, **kwargs):
        # Only the backend completion is fake; claim, gate, journal and retry are production.
        accepted.append(journal.value["id"])
        journal.commit({"creation": "complete", "verification": "passed"})
        started = asyncio.get_running_loop().create_future()
        started.set_result(None)
        return started

    monkeypatch.setattr(workers.io_control, "ensure_ready", readiness)
    monkeypatch.setattr(workers, "_track", complete_fake_backend)
    await controller.tick()
    state = controller.scheduler.status()
    assert state["active"] is None
    job = next(j for j in state["jobs"].values() if j["trigger"] == "memory")
    first = job["operation_id"]
    assert job["attempt"] == 1 and job["status"] == "pending"
    assert job["due"] == (clock.now() + timedelta(minutes=10)).isoformat()
    assert job["attempts"][0]["result"]["creation"] == "failed"
    assert not accepted and not workers.tasks
    rejected = False
    clock.advance(599)
    await controller.tick()
    assert len(checks) == 1  # No immediate retry after the old responsibility clears.
    clock.advance(1)
    await controller.tick()
    assert len(accepted) == 1 and accepted[0] != first
    await controller._finish_active()
    state = controller.scheduler.status()
    job = next(j for j in state["jobs"].values() if j["trigger"] == "memory")
    assert job["attempt"] == 2 and job["status"] == "committed"
    assert not state["dirty"] and state["active"] is None
    task = asyncio.create_task(controller._loop())
    try:
        await SystemClock().sleep(0.05)
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # The fake backend has no process whose exit normally settles this session.
        await workers.io_control.retire_operation(accepted[0])
        for session in tuple(workers.io_control.sessions):
            await workers.io_control.ended(session, confirmed=True)


async def test_unknown_automatic_result_is_not_new_admission_or_failed_retry(tmp_path):
    clock = MockClock()
    workers = BackupWorkers(tmp_path, clock)
    controller = BackupController(
        SimpleNamespace(home=tmp_path, clock=clock, companions=[], problems={}),
        workers,
        {"backup": {"schedule": {"catch_up": False}}},
    )
    controller.scheduler.mark_dirty("companion_a", 1, "fact")
    clock.advance(300)
    request = controller.scheduler.poll()
    controller.scheduler.finish(request.operation_id, creation="unknown")
    clock.advance(7200)
    assert controller.scheduler.poll() is None
    state = controller.scheduler.status()
    assert state["active"] == request.trigger_id
    assert state["jobs"][state["active"]]["status"] == "unknown"
    assert state["jobs"][state["active"]]["attempt"] == 1
