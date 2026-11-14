"""限定槽位沿真实管理调用链交接，不改变活动连接所有者。"""

import asyncio
import threading

import pytest

from clytie.api.management_journal import Journal
from clytie.api.management_worker import execute
from clytie.api.memory import MemoryAPI
from clytie.backup.io_client import IoClient, _stage
from clytie.clock import SystemClock
from clytie.memory.transfer import Transfers
from clytie.plugin_host.support import timed

from .test_storage_management import managed as managed


async def test_real_memory_apply_acquires_before_materializing_and_hands_off(managed, monkeypatch):  # noqa: F811
    runtime, memory, _, record = managed
    runtime.management_journals = {}
    api = MemoryAPI(runtime.home, clock=runtime.clock, hosted=runtime)
    exported = await api.export_online(
        runtime.cid, str(runtime.home / "source.enc"), passphrase="fictional-long-passphrase"
    )
    plan = await api.plan(
        runtime.cid, exported["path"], mode="merge", passphrase="fictional-long-passphrase"
    )
    events = []

    def request(arguments, **kwargs):
        events.append((arguments.copy(), threading.get_ident()))
        return {}

    runtime.backup_io_client = lambda *args, **kwargs: IoClient(request)
    original = Transfers._prepare_live
    prepared = []

    def prepare(self):
        held = _stage.get()
        assert held is not None, "materializing must already own the instance slot"
        assert threading.get_ident() == memory._owner._thread.ident
        prepared.append(held["id"])
        return original(self)

    monkeypatch.setattr(Transfers, "_prepare_live", prepare)
    journal = Journal.create(runtime.home, runtime.cid, "import/apply", runtime.clock)
    result = await execute(
        runtime.home,
        runtime.cid,
        journal.value["id"],
        {"plan_id": plan["id"], "passphrase": "fictional-long-passphrase"},
        clock=runtime.clock,
        hosted=runtime,
    )
    assert result["ok"], result
    acquired = [event for event in events if event[0]["action"] == "acquire"]
    released = [event for event in events if event[0]["action"] == "release"]
    assert len(acquired) == len(released) == 1
    assert prepared == [acquired[0][0]["stage"]]
    assert acquired[0][1] != memory._owner._thread.ident
    assert Journal.load(journal.path, runtime.clock).snapshot_verified()


def test_slot_rejects_overlap_wrong_execution_and_released_capability(tmp_path):
    from clytie.backup.io_budget import IoBudgetError
    from clytie.backup.operation_slot import OperationSlot

    calls = []
    client = IoClient(lambda value, **kwargs: calls.append(value) or {})
    slot = OperationSlot(client, tmp_path, "companion_a", "a" * 32)
    slot.acquire()
    with slot.borrow(slot.binding):
        with pytest.raises(IoBudgetError, match="overlap"):
            with slot.borrow(slot.binding):
                pass
        with pytest.raises(IoBudgetError, match="borrowed"):
            slot.release()
    with pytest.raises(IoBudgetError, match="mismatch"):
        with slot.borrow(object()):
            pass
    slot.release()
    with pytest.raises(IoBudgetError, match="released"):
        with slot.borrow(slot.binding):
            pass
    with pytest.raises(IoBudgetError, match="released"):
        slot.release()
    assert [value["action"] for value in calls] == ["acquire", "release"]


def connected(runtime, operation):
    from clytie.backup.io_channel import IoChannel
    from clytie.backup.io_control import IoControl
    from clytie.clock import SystemClock

    loop = asyncio.get_running_loop()
    control = IoControl(SystemClock())
    session = control.register()
    control.authorize(session, operation)
    frames = []

    async def reply(value):
        channel.receive(value)

    def emit(value):
        frames.append(value)
        loop.call_soon_threadsafe(control.dispatch, session, value, reply)

    channel = IoChannel(emit)
    runtime.backup_io_client = channel.client
    return channel, control, session, frames


async def wait_until(predicate):
    for _ in range(500):
        if predicate():
            return
        await SystemClock().sleep(0.01)
    assert predicate()


async def test_queued_management_cancel_precedes_materializing_and_leaves_owner_usable(managed):  # noqa: F811
    from clytie.backup.coordination import run_sync

    runtime, memory, _, record = managed
    runtime.management_journals = {}
    journal = Journal.create(runtime.home, runtime.cid, "thaw", runtime.clock)
    _, control, session, frames = connected(runtime, journal.value["id"])
    endpoint = control.sessions[session].endpoints[journal.value["id"]]
    await endpoint.request({"action": "acquire", "stage": "blocker"})
    job = asyncio.create_task(
        execute(
            runtime.home,
            runtime.cid,
            journal.value["id"],
            {"record_id": record.record_id},
            clock=runtime.clock,
            hosted=runtime,
        )
    )
    try:
        await wait_until(lambda: control.budget.status()["waiting"] == 1)
        live = runtime.management_journals[journal.value["id"]]
        assert live.value["state"] == "preparing" and "database" not in live.value
        assert (await run_sync(memory, memory.get_raw, record.record_id)).content == record.content
        live.cancel()
        result = await timed(job, SystemClock(), 5)
        assert result["code"] == "cancelled"
        assert control.budget.status()["waiting"] == 0
        assert len([f for f in frames if f["arguments"]["action"] == "acquire"]) == 1
    finally:
        await endpoint.request({"action": "release", "stage": "blocker"})
        await control.ended(session, confirmed=True)
        if not job.done():
            await timed(job, SystemClock(), 5)


async def test_eof_after_publication_recovers_on_original_owner_without_new_acquire(
    managed, monkeypatch
):  # noqa: F811
    from clytie.memory.package import Exports

    runtime, memory, _, _ = managed
    runtime.management_journals = {}
    journal = Journal.create(runtime.home, runtime.cid, "export", runtime.clock)
    channel, control, session, frames = connected(runtime, journal.value["id"])
    recovery = []
    original = Journal._recover

    def recover(self, store=None):
        recovery.append((_stage.get()["id"], threading.get_ident()))
        assert store is memory
        return original(self, store)

    def lost_response(*_):
        channel.disconnect()
        control.disconnect(session)
        raise OSError("fictional response loss")

    monkeypatch.setattr(Exports, "_audit_export", lost_response)
    monkeypatch.setattr(Journal, "_recover", recover)
    try:
        result = await execute(
            runtime.home,
            runtime.cid,
            journal.value["id"],
            {"out": str(runtime.home / "export.enc"), "passphrase": "fictional-long-passphrase"},
            clock=runtime.clock,
            hosted=runtime,
        )
        assert result["ok"]
        saved = Journal.load(journal.path, runtime.clock)
        assert saved.value["committed"]
        assert saved.public()["io_release_unconfirmed"]
        assert saved.public()["io_release_error"] == "backup_io_disconnected"
        acquired = [f for f in frames if f["arguments"]["action"] == "acquire"]
        assert len(acquired) == 1
        assert recovery == [(acquired[0]["arguments"]["stage"], memory._owner._thread.ident)]
        assert control.budget.status()["active"] is not None
        assert not any(f["arguments"]["action"] == "release" for f in frames)
    finally:
        await control.ended(session, confirmed=True)


async def test_cancelled_file_waiter_waits_for_actual_borrower_exit(tmp_path):
    from clytie.api.management_io import FileCommands
    from clytie.backup.operation_slot import OperationSlot, operation_scope

    calls = []
    slot = OperationSlot(
        IoClient(lambda v, **kw: calls.append(v) or {}), tmp_path, "companion_a", "b" * 32
    )
    entered, release = threading.Event(), threading.Event()
    slot.acquire()

    def work():
        entered.set()
        assert release.wait(5)

    from clytie.clock import SystemClock

    async with FileCommands(SystemClock()) as files:
        with operation_scope(slot.binding):
            task = asyncio.create_task(files.run(work))
        await wait_until(entered.is_set)
        task.cancel()
        await SystemClock().sleep(0)
        assert not task.done()
        assert len(calls) == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    slot.release()
    assert [value["action"] for value in calls] == ["acquire", "release"]


async def test_wrong_journal_cannot_borrow_file_capability(tmp_path):
    from clytie.api.management_io import FileCommands
    from clytie.api.management_journal import current
    from clytie.backup.io_budget import IoBudgetError
    from clytie.backup.operation_slot import OperationSlot, operation_scope

    slot = OperationSlot(IoClient(lambda *a, **kw: {}), tmp_path, "companion_a", "c" * 32)
    slot.acquire()
    wrong = Journal.create(tmp_path, "companion_b", "export", SystemClock())
    binding = current.set(wrong)
    try:
        async with FileCommands(SystemClock()) as files:
            with operation_scope(slot.binding), pytest.raises(IoBudgetError, match="mismatch"):
                await files.run(lambda: None)
    finally:
        current.reset(binding)
        slot.release()


async def test_new_recovery_process_authorizes_each_old_journal_without_replay(tmp_path):
    from clytie.api.management_workers import Workers
    from clytie.memory.store import MemoryStore

    clock = SystemClock()
    path = tmp_path / "companions/companion_a/memory/memory.sqlite3"
    store = MemoryStore(path, clock=clock, config={}, instance={})
    store.close()
    journals = []
    for _ in range(2):
        journal = Journal.create(tmp_path, "companion_a", "import/apply", clock)
        journal.database(path)
        from clytie.plugin_host.locking import process_identity

        journal.value["executor"] = {
            **process_identity(),
            "started_at": "2000-01-01T00:00:00+00:00",
        }
        journal.phase("transaction")
        journals.append(journal)
    workers = Workers(tmp_path, clock)
    frames = []
    original = workers.io_control.dispatch

    def dispatch(session, frame, send):
        frames.append(frame)
        return original(session, frame, send)

    workers.io_control.dispatch = dispatch
    result = await timed(
        workers._process(journals[0], {}, lambda _: None, recover_only=True), clock, 15
    )
    assert result["ok"]
    acquired = [f for f in frames if f["arguments"]["action"] == "acquire"]
    assert {f["operation_id"] for f in acquired} == {j.value["id"] for j in journals}
    assert len({f["arguments"]["stage"] for f in acquired}) == 2
    assert all(Journal.load(j.path, clock).value["state"] == "interrupted" for j in journals)
    assert workers.io_control.budget.status()["active"] is None
    assert not workers.io_control.budget._stages


async def test_recovery_authorization_precheck_failure_cannot_launch_worker(tmp_path, monkeypatch):
    from clytie.api.management_journal import ManagementBusy
    from clytie.api.management_workers import Workers

    clock = SystemClock()
    journal = Journal.create(tmp_path, "companion_a", "import/apply", clock)
    journal.blocked("fictional_unverified_database")
    workers = Workers(tmp_path, clock)
    launched = []

    async def spawn(*args, **kwargs):
        launched.append(True)
        raise AssertionError("recovery precheck must precede process launch")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(ManagementBusy):
        await workers._process(journal, {}, lambda _: None, recover_only=True)
    assert not launched and not workers.io_control.sessions


@pytest.mark.parametrize("managed", [False], indirect=True)
async def test_actual_backup_apply_keeps_normal_charges_across_snapshot_and_owner(
    managed, tmp_path, monkeypatch
):  # noqa: F811
    from contextlib import contextmanager

    from tests.test_backup_memory_io_channel import external_backup

    from clytie.backup.coordination import run_sync
    from clytie.backup.operation_slot import OperationSlot

    runtime, memory, _, _ = managed
    runtime.management_journals = {}
    _, arguments = external_backup(tmp_path / "external")
    api = MemoryAPI(runtime.home, clock=runtime.clock, hosted=runtime)
    plan = await api.backup_plan(runtime.cid, **arguments)
    journal = Journal.create(runtime.home, runtime.cid, "import/apply", runtime.clock)
    _, control, session, frames = connected(runtime, journal.value["id"])
    borrowers = []
    active = []
    original = OperationSlot.borrow

    @contextmanager
    def borrow(self, binding, **kwargs):
        with original(self, binding, **kwargs):
            assert not active
            active.append(threading.get_ident())
            borrowers.append((threading.current_thread().name, _stage.get()["id"]))
            try:
                yield
            finally:
                active.clear()

    monkeypatch.setattr(OperationSlot, "borrow", borrow)
    try:
        result = await timed(
            execute(
                runtime.home,
                runtime.cid,
                journal.value["id"],
                {"plan_id": plan["id"]},
                clock=runtime.clock,
                hosted=runtime,
            ),
            SystemClock(),
            15,
        )
        assert result["ok"], result
        assert len(await run_sync(memory, memory.all_records)) == 1
        assert len({stage for _, stage in borrowers}) == 1
        names = {name for name, _ in borrowers}
        assert "clytie-management-files" in names and "clytie-snapshot" in names
        assert memory._owner._thread.name in names
        charges = [f["arguments"] for f in frames if f["arguments"]["action"] == "charge"]
        assert charges and {c["direction"] for c in charges} == {"read", "write"}
        assert [c["sequence"] for c in charges] == list(range(1, len(charges) + 1))
        assert control.budget.status()["active"] is None
    finally:
        await control.ended(session, confirmed=True)


@pytest.mark.parametrize("phase", ["acquire", "release"])
async def test_cancel_during_acquire_or_release_delivery_does_not_leak_slot(
    managed, monkeypatch, phase
):
    from clytie.backup.operation_slot import OperationSlot

    runtime, memory, _, record = managed
    runtime.management_journals = {}
    journal = Journal.create(runtime.home, runtime.cid, "thaw", runtime.clock)
    _, control, session, _ = connected(runtime, journal.value["id"])
    entered, resume = threading.Event(), threading.Event()
    original = getattr(OperationSlot, phase)

    def paused(self):
        result = original(self)
        entered.set()
        assert resume.wait(5)
        return result

    monkeypatch.setattr(OperationSlot, phase, paused)
    request = asyncio.create_task(
        execute(
            runtime.home,
            runtime.cid,
            journal.value["id"],
            {"record_id": record.record_id},
            clock=runtime.clock,
            hosted=runtime,
        )
    )
    try:
        await wait_until(entered.is_set)
        task = next(iter(runtime.management_tasks))
        task.cancel()
        await SystemClock().sleep(0)
        assert not task.done()
        assert memory._owner.status()["finalizers"] == 1
        resume.set()
        result = await timed(request, SystemClock(), 5)
        assert result["ok"] is (phase == "release")
        assert control.budget.status()["active"] is None
        assert memory._owner.status()["finalizers"] == 0
    finally:
        resume.set()
        await control.ended(session, confirmed=True)
        # A failed assertion must not hide a retained finalizer by hanging teardown.
        if memory._owner.status()["finalizers"]:
            from clytie.storage.finalization import release_finalizer

            for finalizer in tuple(memory._owner._finalizers):
                release_finalizer(finalizer, executor_finished=True, obligations_complete=True)


async def test_offline_backup_apply_keeps_original_store_and_moves_temporary_validation(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from tests.test_backup_memory_io_channel import external_backup

    from clytie.memory import package
    from clytie.memory.store import MemoryStore

    home, arguments = external_backup(tmp_path)
    clock = SystemClock()
    api = MemoryAPI(home, clock=clock)
    plan = await api.backup_plan("companion_a", **arguments)
    journal = Journal.create(home, "companion_a", "import/apply", clock)
    runtime = SimpleNamespace(home=home, cid="companion_a", clock=clock)
    channel, control, session, _ = connected(runtime, journal.value["id"])
    loop_thread = threading.get_ident()
    validated = []
    original_validate = package._validate_database
    original_transaction = MemoryStore.transaction
    writes = []

    def validate(*args, **kwargs):
        validated.append(threading.get_ident())
        assert threading.get_ident() != loop_thread, (
            "temporary native validation must use file executor"
        )
        assert _stage.get() is not None
        return original_validate(*args, **kwargs)

    def transaction(self, *args, **kwargs):
        if self.path == home / "companions/companion_a/memory/memory.sqlite3":
            writes.append(threading.get_ident())
        return original_transaction(self, *args, **kwargs)

    monkeypatch.setattr(package, "_validate_database", validate)
    monkeypatch.setattr(MemoryStore, "transaction", transaction)
    try:
        result = await timed(
            execute(
                home,
                "companion_a",
                journal.value["id"],
                {"plan_id": plan["id"]},
                clock=clock,
                io_channel=channel,
            ),
            clock,
            15,
        )
        assert result["ok"], result
        assert validated
        assert writes and set(writes) == {loop_thread}
        assert Journal.load(journal.path, clock).snapshot_verified()
    finally:
        await control.ended(session, confirmed=True)


@pytest.mark.parametrize("managed", [False], indirect=True)
@pytest.mark.parametrize("hosted", [False, True])
async def test_backup_apply_eof_after_db_commit_preserves_evidence_and_slot(
    managed, tmp_path, monkeypatch, hosted
):
    from contextlib import contextmanager

    from tests.test_backup_memory_io_channel import external_backup

    from clytie.backup.operation_slot import OperationSlot
    from clytie.memory import transfer

    runtime, memory, _, _ = managed
    runtime.management_journals = {}
    offline_home, arguments = external_backup(tmp_path / "external")
    home = runtime.home if hosted else offline_home
    api = MemoryAPI(home, clock=SystemClock(), hosted=runtime if hosted else None)
    plan = await api.backup_plan("companion_a", **arguments)
    journal = Journal.create(home, "companion_a", "import/apply", api.clock)
    channel, control, session, frames = connected(runtime, journal.value["id"])
    original_borrow = OperationSlot.borrow
    original_recover = Journal._recover
    lost = threading.Event()
    recovered = []
    loop_thread = threading.get_ident()

    def committed_response_lost(*_):
        lost.set()
        channel.disconnect()
        control.disconnect(session)
        raise OSError("fictional response loss after database commit")

    @contextmanager
    def borrow(self, binding, **kwargs):
        if lost.is_set():
            assert kwargs.get("cleanup"), "EOF cannot admit a normal phase"
        with original_borrow(self, binding, **kwargs):
            yield

    def recover(self, store=None):
        recovered.append((_stage.get()["id"], threading.get_ident()))
        assert threading.get_ident() != loop_thread
        if hosted:
            assert store is memory and threading.get_ident() == memory._owner._thread.ident
        else:
            assert store is None
        return original_recover(self, store)

    monkeypatch.setattr(transfer, "finish_commit", committed_response_lost)
    monkeypatch.setattr(OperationSlot, "borrow", borrow)
    monkeypatch.setattr(Journal, "_recover", recover)
    try:
        result = await timed(
            execute(
                home,
                "companion_a",
                journal.value["id"],
                {"plan_id": plan["id"]},
                clock=api.clock,
                hosted=runtime if hosted else None,
                io_channel=None if hosted else channel,
            ),
            SystemClock(),
            15,
        )
        assert result["ok"], result
        saved = Journal.load(journal.path, api.clock)
        assert saved.value["committed"] and saved.snapshot_verified()
        assert saved.public()["io_release_unconfirmed"]
        acquired = [f for f in frames if f["arguments"]["action"] == "acquire"]
        assert len(acquired) == 1 and recovered[0][0] == acquired[0]["arguments"]["stage"]
        assert control.budget.status()["active"] is not None
    finally:
        await control.ended(session, confirmed=True)


async def test_backup_plan_cancellation_still_releases_existing_generation(
    managed, tmp_path, monkeypatch
):
    from tests.test_backup_memory_io_channel import external_backup

    from clytie.api.management_journal import ManagementCancelled, current
    from clytie.backup import memory_source

    runtime, _, _, _ = managed
    runtime.management_journals = {}
    _, arguments = external_backup(tmp_path / "external")
    journal = Journal.create(runtime.home, runtime.cid, "backup/plan", runtime.clock)
    _, control, session, _ = connected(runtime, journal.value["id"])
    cancelled, cleaned = [], []
    original = memory_source.BackupMemorySource.failed_generation

    def cancel(*args, **kwargs):
        cancelled.append(True)
        current.get().cancel()
        raise ManagementCancelled()

    def cleanup(self):
        cleaned.append(self.generation_id)
        return original(self)

    monkeypatch.setattr(memory_source, "_validate_database", cancel)
    monkeypatch.setattr(memory_source.BackupMemorySource, "failed_generation", cleanup)
    try:
        result = await timed(
            execute(
                runtime.home,
                runtime.cid,
                journal.value["id"],
                arguments,
                clock=runtime.clock,
                hosted=runtime,
            ),
            SystemClock(),
            15,
        )
        assert result["code"] == "cancelled"
        assert cancelled and len(cleaned) == 1
    finally:
        await control.ended(session, confirmed=True)
