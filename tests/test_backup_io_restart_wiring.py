"""重启准入接线：拒绝在持久操作与实际请求交付之前发生。"""

import asyncio
import os
import threading

import pytest
from tests.test_supervisor import FakeChild, make

from clytie.api.auth import APIError
from clytie.api.management import Management
from clytie.api.management_workers import Workers
from clytie.backup.io_budget import IoBudgetError
from clytie.backup.io_control import IoControl
from clytie.backup.workers import BackupWorkers
from clytie.clock import SystemClock
from clytie.sdk.testing import MockClock


@pytest.mark.parametrize("kind", ["memory", "backup"])
async def test_unverified_responsibility_rejects_before_new_journal(tmp_path, monkeypatch, kind):
    workers = (
        Workers(tmp_path, MockClock()) if kind == "memory" else BackupWorkers(tmp_path, MockClock())
    )

    async def blocked():
        raise IoBudgetError("backup_io_responsibility_unknown")

    def forbidden(*args, **kwargs):
        pytest.fail("an unverified controller must not create a journal")

    workers.io_control.ensure_ready = blocked
    if kind == "memory":
        monkeypatch.setattr("clytie.api.management_workers.Journal.create", forbidden)
    else:
        monkeypatch.setattr("clytie.backup.workers.BackupJournal.accept", forbidden)
    with pytest.raises((IoBudgetError, APIError), match="backup_io_responsibility_unknown"):
        if kind == "memory":
            await workers.execute("companion_a", "export", {})
        else:
            await workers.execute("verify", {"backup_id": "a" * 32})
    assert workers.status() == []
    assert not workers.tasks


async def test_async_pre_send_failure_never_delivers_request(tmp_path):
    svc, launcher, _ = await make(tmp_path)
    child = launcher.children[0][1]
    dispatched = False

    async def bind(receiver):
        nonlocal dispatched
        assert receiver is child
        dispatched = True
        raise IoBudgetError("backup_io_responsibility_unknown")

    try:
        with pytest.raises(IoBudgetError, match="backup_io_responsibility_unknown"):
            await svc.request("companion_0", "test", {}, before_send=bind)
        assert dispatched
        assert not any(row["type"] == "request" for row in child.commands)
    finally:
        await svc.stop()


@pytest.mark.parametrize("kind", ["memory", "backup"])
async def test_bind_failure_drains_and_waits_real_undelivered_child(tmp_path, monkeypatch, kind):
    clock = SystemClock()
    workers = Workers(tmp_path, clock) if kind == "memory" else BackupWorkers(tmp_path, clock)
    spawned = []
    original_spawn = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        child = await original_spawn(*args, **kwargs)
        spawned.append(child)
        return child

    async def reject(*args):
        raise IoBudgetError("backup_io_recheck_required")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    workers.io_control.bind_execution = reject
    try:
        with pytest.raises((APIError, IoBudgetError), match="backup_io_recheck_required"):
            if kind == "memory":
                await workers.execute("companion_a", "export", {})
            else:
                await workers.execute("verify", {"backup_id": "b" * 32})
    finally:
        await workers.stop("wait")
    assert len(spawned) == 1
    assert spawned[0].returncode is not None
    assert not workers.io_control.sessions
    assert (await workers.io_control.recheck())["state"] == "ready"
    row = workers.status()[0]
    assert row.get("state", row.get("phase")) in {"failed", "cancelled"}


async def test_trusted_failed_online_reply_retires_live_companion_responsibility(tmp_path):
    svc, launcher, clock = await make(tmp_path)
    api = Management(svc, None)
    child = launcher.children[0][1]
    original_send = child.send

    async def respond(message):
        if message["type"] != "request":
            return await original_send(message)
        from clytie.api.management_journal import Journal

        key = message["args"]["operation_id"]
        path = tmp_path / "companions/companion_0/management/operations" / (key + ".json")
        Journal.load(path, clock).phase("failed")
        await child.queue.put(
            {
                "type": "response",
                "id": message["id"],
                "ok": False,
                "error": {"code": "fictional_operation_failure", "status": 409},
            }
        )

    child.send = respond
    try:
        with pytest.raises(APIError, match="fictional_operation_failure"):
            await api.call(
                "/companions/<cid>/memory/thaw",
                {"cid": "companion_0"},
                {"record_id": "fictional"},
                {},
                "POST",
            )
        assert not child.exited.is_set()
        restarted_control = IoControl(clock, home=tmp_path)
        assert (await restarted_control.recheck())["state"] == "ready"
    finally:
        await svc.stop()


@pytest.mark.parametrize("kind", ["memory", "backup"])
async def test_cancel_during_binding_waits_undelivered_process_exit(tmp_path, monkeypatch, kind):
    clock = SystemClock()
    workers = Workers(tmp_path, clock) if kind == "memory" else BackupWorkers(tmp_path, clock)
    binding = asyncio.Event()
    spawned = []
    original_spawn = asyncio.create_subprocess_exec
    operation_id = "c" * 32

    async def spawn(*args, **kwargs):
        child = await original_spawn(*args, **kwargs)
        spawned.append(child)
        return child

    async def paused(*args):
        binding.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    workers.io_control.bind_execution = paused
    call = asyncio.create_task(
        workers.execute("companion_a", "export", {}, operation_id)
        if kind == "memory"
        else workers.execute("verify", {"backup_id": "b" * 32}, operation_id)
    )
    try:
        await binding.wait()
        workers.tasks[operation_id].cancel()
        await asyncio.gather(call, return_exceptions=True)
    finally:
        await workers.stop("wait")
    assert len(spawned) == 1 and spawned[0].returncode is not None
    assert not workers.io_control.sessions
    assert (await workers.io_control.recheck())["state"] == "ready"


@pytest.mark.parametrize("failure", ["write_error", "cancel"])
async def test_online_pre_send_metadata_failure_never_delivers_and_releases(
    tmp_path, monkeypatch, failure
):
    from clytie.api.management_journal import Journal

    svc, launcher, clock = await make(tmp_path)
    api = Management(svc, None)
    child = launcher.children[0][1]
    original_save = Journal.save
    saving, proceed = threading.Event(), threading.Event()
    operation_id = "d" * 32

    def paused_save(journal):
        if hasattr(journal, "io_session") and journal.value["state"] == "accepted":
            saving.set()
            if failure == "write_error":
                raise OSError("fictional metadata write failure")
            assert proceed.wait(10)
        return original_save(journal)

    monkeypatch.setattr(Journal, "save", paused_save)
    call = asyncio.create_task(
        api.call(
            "/companions/<cid>/memory/thaw",
            {"cid": "companion_0"},
            {"record_id": "fictional", "operation_id": operation_id},
            {},
            "POST",
        )
    )
    try:
        assert await asyncio.to_thread(saving.wait, 10)
        if failure == "cancel":
            api.memory_workers.tasks[operation_id].cancel()
            proceed.set()
            with pytest.raises(asyncio.CancelledError):
                await call
        else:
            with pytest.raises(APIError, match="management_worker_failed"):
                await call
        assert not any(row["type"] == "request" for row in child.commands)
        assert api.memory_workers.status()[0]["state"] == "failed"
        assert (await IoControl(clock, home=tmp_path).recheck())["state"] == "ready"
    finally:
        proceed.set()
        await asyncio.gather(call, return_exceptions=True)
        await svc.stop()


async def test_cancel_waits_for_executor_metadata_save_before_releasing(tmp_path, monkeypatch):
    from clytie.api.management_journal import Journal

    workers = Workers(tmp_path, SystemClock())
    original_save, original_thread = Journal.save, asyncio.to_thread
    saving, proceed, saved = threading.Event(), threading.Event(), threading.Event()
    interrupted = asyncio.Event()
    key = "e" * 32

    def paused_save(journal):
        if journal.value["state"] == "accepted" and journal.value["executor"]["pid"] != os.getpid():
            saving.set()
            assert proceed.wait(10)
            original_save(journal)
            saved.set()
        else:
            original_save(journal)

    async def observed_thread(function, *args, **kwargs):
        try:
            return await original_thread(function, *args, **kwargs)
        except asyncio.CancelledError:
            if getattr(function, "__name__", "") == "save_executor":
                interrupted.set()
            raise

    monkeypatch.setattr(Journal, "save", paused_save)
    monkeypatch.setattr(asyncio, "to_thread", observed_thread)
    call = asyncio.create_task(workers.execute("companion_a", "export", {}, key))
    try:
        assert await original_thread(saving.wait, 10)
        workers.tasks[key].cancel()
        await workers.clock.sleep(0)
        assert not interrupted.is_set()
    finally:
        proceed.set()
        await asyncio.gather(call, return_exceptions=True)
        await workers.stop("wait")
        assert await original_thread(saved.wait, 10)
    assert workers.status()[0]["state"] == "failed"
    assert (await workers.io_control.recheck())["state"] == "ready"


async def test_stop_drains_reserved_admission_without_accepting_new_work(tmp_path):
    reserved, proceed = asyncio.Event(), asyncio.Event()
    ran = False

    async def runner(journal, args, control):
        nonlocal ran
        ran = True
        journal.committed()
        return {"ok": True, "result": {}}

    workers = Workers(tmp_path, MockClock(), runner=runner)
    original_reserve = workers.io_control.reserve

    async def reserve(*args, **kwargs):
        await original_reserve(*args, **kwargs)
        reserved.set()
        await proceed.wait()

    workers.io_control.reserve = reserve
    call = asyncio.create_task(workers.execute("companion_a", "export", {}))
    stopping = None
    try:
        await reserved.wait()
        stopping = asyncio.create_task(workers.stop("wait"))
        await workers.clock.drain(10)
        assert not stopping.done()
        assert workers.status() == []
        proceed.set()
        with pytest.raises(APIError, match="supervisor_stopping"):
            await call
        await stopping
        assert not ran
        assert workers.status() == []
        assert not workers.io_control.sessions
        assert (await workers.io_control.recheck())["state"] == "ready"
    finally:
        proceed.set()
        await asyncio.gather(call, *([stopping] if stopping else []), return_exceptions=True)


async def test_capture_identity_cannot_authorize_a_replacement_session(tmp_path):
    svc, launcher, _ = await make(tmp_path)
    old_child = launcher.children[0][1]
    cid, operation = "companion_0", "a" * 32
    old_session = svc.backup_io_sessions[cid]
    entered, proceed = threading.Event(), threading.Event()
    original_identity = old_child.identity

    def paused_identity():
        entered.set()
        assert proceed.wait(10)
        return original_identity()

    old_child.identity = paused_identity
    call = asyncio.create_task(svc.authorize_backup_capture(cid, operation, child=old_child))
    new_session = svc.backup_io.register(storage_owner=True)
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        svc.children[cid] = FakeChild()
        svc.backup_io_sessions[cid] = new_session
        proceed.set()
        with pytest.raises(IoBudgetError, match="backup_io_executor_lost"):
            await call
        assert not svc.backup_io.sessions[new_session].endpoints
        assert not svc.backup_io.sessions[new_session].persisted
    finally:
        proceed.set()
        await asyncio.gather(call, return_exceptions=True)
        svc.children[cid] = old_child
        svc.backup_io_sessions[cid] = old_session
        await svc.backup_io.ended(new_session, confirmed=True)
        await svc.stop()


async def test_release_failure_keeps_evidence_but_settles_worker_resources(tmp_path, monkeypatch):
    workers = BackupWorkers(tmp_path, SystemClock())
    entered, proceed = asyncio.Event(), asyncio.Event()
    original_ended, original_spawn = workers.io_control.ended, asyncio.create_subprocess_exec
    spawned = []

    async def spawn(*args, **kwargs):
        child = await original_spawn(*args, **kwargs)
        spawned.append(child)
        return child

    async def fail(*args, **kwargs):
        entered.set()
        await proceed.wait()
        raise IoBudgetError("backup_io_coordination_busy")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    workers.io_control.ended = fail
    try:
        await workers.execute("verify", {"backup_id": "b" * 32}, "a" * 32)
        await entered.wait()
        task = workers.tasks["a" * 32]
        proceed.set()
        with pytest.raises(IoBudgetError, match="backup_io_coordination_busy"):
            await task
        await workers.stop("wait")
        assert not workers.tasks and not workers.controls and not workers.processes
        assert spawned[0].returncode is not None and spawned[0].stdin.is_closing()
        assert workers.io_control.sessions
        assert all(state.disconnected for state in workers.io_control.sessions.values())
    finally:
        proceed.set()
        await workers.stop("wait")
        for session in tuple(workers.io_control.sessions):
            await original_ended(session, confirmed=True)


@pytest.mark.parametrize("kind", ["memory", "backup"])
async def test_finishing_reminder_includes_only_unsettled_admission(tmp_path, kind):
    clock = MockClock()
    workers = Workers(tmp_path, clock) if kind == "memory" else BackupWorkers(tmp_path, clock)
    entered, proceed = asyncio.Event(), asyncio.Event()
    original_reserve = workers.io_control.reserve

    async def reserve(*args, **kwargs):
        await original_reserve(*args, **kwargs)
        entered.set()
        await proceed.wait()
        raise IoBudgetError("backup_io_recheck_required")

    workers.io_control.reserve = reserve
    call = asyncio.create_task(
        workers.execute("companion_a", "export", {})
        if kind == "memory"
        else workers.execute("verify", {"backup_id": "b" * 32})
    )
    stopping = None
    try:
        await entered.wait()
        stopping = asyncio.create_task(workers.stop("cancel"))
        await clock.drain(10)
        since = workers.cancel_since
        clock.advance(119)
        assert not workers.still_finishing
        clock.advance(2)
        workers.escalate()
        assert workers.cancel_since == since
        assert workers.still_finishing and not stopping.done()
        assert workers.status() == []
        proceed.set()
        with pytest.raises((APIError, IoBudgetError), match="backup_io_recheck_required"):
            await call
        await stopping
        assert not workers.still_finishing
        assert workers.status() == []
    finally:
        proceed.set()
        await asyncio.gather(call, *([stopping] if stopping else []), return_exceptions=True)
