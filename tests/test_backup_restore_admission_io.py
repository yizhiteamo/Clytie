"""Production restore admission must use the instance I/O budget before hashing."""

import asyncio
import threading

import pytest
from tests.test_backup_restore import configured_package
from tests.test_backup_workers import completed

from clytie.backup.io_budget import IoBudgetError
from clytie.backup.io_client import current_client
from clytie.backup.journal import BackupJournal
from clytie.backup.previews import PreviewError
from clytie.backup.restore import RestoreManager
from clytie.backup.workers import BackupWorkers
from clytie.clock import SystemClock
from clytie.plugin_host.locking import process_identity


async def test_real_restore_admission_hash_uses_shared_slot_and_charges(tmp_path, monkeypatch):
    manager, package, key, target, workspace = configured_package(tmp_path, monkeypatch)
    plan = manager.plan(package, key, target, workspace)
    workers = BackupWorkers(manager.home, manager.clock)
    charges, observations = [], []
    original_charge = workers.io_control.budget.charge
    original_admit = RestoreManager.admit

    async def charge(lease, sequence, direction, size):
        charges.append((direction, size))
        return await original_charge(lease, sequence, direction, size)

    def admit(self, plan_id, operation_id):
        client = current_client()
        result = original_admit(self, plan_id, operation_id)
        observations.append((client is not None, list(charges)))
        return result

    monkeypatch.setattr(workers.io_control.budget, "charge", charge)
    monkeypatch.setattr(RestoreManager, "admit", admit)
    accepted = await workers.execute("restore_apply", {"plan_id": plan["id"]}, "a" * 32)
    result = await completed(workers, accepted["id"])
    assert result["phase"] == "committed"
    assert observations and observations[0][0]
    assert sum(size for direction, size in observations[0][1] if direction == "read") >= (
        package.stat().st_size
    )
    assert workers.io_control.budget.status()["active"] is None
    assert not workers._admissions


async def until(predicate):
    for _ in range(500):
        if predicate():
            return
        await SystemClock().sleep(0.01)
    pytest.fail("admission synchronization signal missing")


@pytest.mark.parametrize("cancel", [False, True])
async def test_admission_waits_for_other_stage_or_cancels_without_consuming(
    tmp_path, monkeypatch, cancel
):
    manager, package, key, target, workspace = configured_package(tmp_path, monkeypatch)
    plan = manager.plan(package, key, target, workspace)
    workers = BackupWorkers(manager.home, manager.clock)
    control = workers.io_control
    holder = control.register()
    await control.authorize_execution(holder, "f" * 32, process_identity())
    endpoint = control.sessions[holder].endpoints["f" * 32]
    await endpoint.request({"action": "acquire", "stage": "other_stage"})
    entered = threading.Event()
    original = RestoreManager.admit

    def admit(self, *args):
        entered.set()
        return original(self, *args)

    monkeypatch.setattr(RestoreManager, "admit", admit)
    caller = asyncio.create_task(
        workers.execute("restore_apply", {"plan_id": plan["id"]}, "b" * 32)
    )
    try:
        await until(lambda: control.budget.status()["waiting"] == 1 or entered.is_set())
        assert control.budget.status()["waiting"] == 1 and not entered.is_set()
        if cancel:
            stop = asyncio.create_task(workers.stop("cancel"))
            with pytest.raises(IoBudgetError, match="backup_io_cancelled"):
                await caller
            await stop
            assert manager.previews.get("instance", plan["id"])["state"] == "ready"
            assert not BackupJournal.path_for(manager.home, "b" * 32).exists()
            assert control.budget.status()["active"] is not None  # Other executor still owns it.
    finally:
        await endpoint.request({"action": "release", "stage": "other_stage"})
        await control.retire_operation("f" * 32)
        await control.ended(holder, confirmed=True)
    if not cancel:
        accepted = await caller
        result = await completed(workers, accepted["id"])
        assert result["phase"] == "committed" and entered.is_set()
    assert control.budget.status()["active"] is None


@pytest.mark.parametrize("action", ["cancel", "expire", "corrupt"])
async def test_hash_exit_precedes_release_and_preview_recheck(tmp_path, monkeypatch, action):
    from clytie.backup import restore

    manager, package, key, target, workspace = configured_package(tmp_path, monkeypatch)
    plan = manager.plan(package, key, target, workspace)
    workers = BackupWorkers(manager.home, manager.clock)
    entered, proceed = threading.Event(), threading.Event()
    original = restore.io_read

    def blocked_read(stream, count):
        value = original(stream, count)
        if not entered.is_set():
            entered.set()
            assert proceed.wait(10)
        return value

    monkeypatch.setattr(restore, "io_read", blocked_read)
    caller = asyncio.create_task(
        workers.execute("restore_apply", {"plan_id": plan["id"]}, "c" * 32)
    )
    stop = None
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        assert workers.io_control.budget.status()["active"] is not None
        # Reader protection is durable, but the registry lock is not held by I/O.
        holders = await asyncio.to_thread(manager.previews.holders, package)
        assert any(row["kind"] == "reader" for row in holders)
        if action == "cancel":
            stop = asyncio.create_task(workers.stop("cancel"))
            await until(lambda: workers.cancel_since is not None)
            assert not stop.done() and not caller.done()
        elif action == "expire":
            manager.clock.advance(24 * 60 * 60)
        else:
            with package.open("ab") as output:
                output.write(b"fictional_change")
        assert workers.io_control.budget.status()["active"] is not None
    finally:
        proceed.set()
    error = (
        IoBudgetError
        if action == "cancel"
        else (PreviewError if action == "expire" else ValueError)
    )
    with pytest.raises(error):
        await caller
    if stop:
        await stop
    expected = {"cancel": "ready", "expire": "expired", "corrupt": "stale"}[action]
    assert manager.previews.get("instance", plan["id"])["state"] == expected
    assert not BackupJournal.path_for(manager.home, "c" * 32).exists()
    assert workers.io_control.budget.status()["active"] is None
    holders = manager.previews.holders(package)
    assert all(row["kind"] == "preview" for row in holders)
