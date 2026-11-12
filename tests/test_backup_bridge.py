import io
from types import SimpleNamespace

import pytest
from tests.test_backup_online_source import online  # noqa: F401

from clytie.backup.io_channel import IoChannel
from clytie.backup.io_control import IoControl
from clytie.backup.worker import Controls


def attach_test_capture_channel(supervisor, runtime=None):
    import asyncio

    sessions = {}
    control = supervisor.backup_io
    loop = asyncio.get_running_loop()

    def authorize(cid, operation):
        if cid not in sessions:
            sessions[cid] = control.register(storage_owner=True)
        control.authorize(sessions[cid], operation)

    supervisor.authorize_backup_capture = authorize
    if runtime is not None:

        async def reply(value):
            channel.receive(value)

        def emit(value):
            loop.call_soon_threadsafe(lambda: control.dispatch(sessions[runtime.cid], value, reply))

        channel = IoChannel(emit)
        runtime.backup_io_client = channel.client
    return sessions


async def test_generated_key_notice_persists_until_acknowledged_not_copy_verified(tmp_path):
    from clytie.backup.controller import BackupController
    from clytie.backup.keys import BackupKeys
    from clytie.config import repository_root
    from clytie.sdk.testing import MockClock

    clock = MockClock()
    svc = SimpleNamespace(home=tmp_path, clock=clock, problems={})
    controller = BackupController(svc, None, {})
    keys = BackupKeys(tmp_path, clock, repository=repository_root())
    await controller._key_notice()
    assert svc.problems == {}
    keys.ensure()
    for _ in range(2):
        await controller._key_notice()
        assert svc.problems == {"backup_key:instance": "backup_key_needs_safekeeping"}
    keys.acknowledge()
    await controller._key_notice()
    assert svc.problems == {}
    assert keys.status()["copy_verified"] is False


def test_controls_accept_bounded_bridge_reply():
    controls = Controls(SimpleNamespace(value={"phase": "capturing"}), None)
    controls._read(
        io.BytesIO(b'{"type":"bridge_reply","id":"one","result":{"online_ids":["companion_a"]}}\n')
    )
    assert controls.responses["one"]["result"]["online_ids"] == ["companion_a"]


def test_optional_finalization_cancel_does_not_interrupt_publication(tmp_path):
    from clytie.backup.journal import BackupJournal
    from clytie.backup.worker import Cancelled
    from clytie.sdk.testing import MockClock

    journal, _ = BackupJournal.accept(tmp_path, MockClock(), "c" * 32, "create", {})
    journal.phase("publishing")
    controls = Controls(journal, None)
    controls.cancelled.set()
    controls.check()
    with pytest.raises(Cancelled):
        controls.check_optional()
    assert BackupJournal.load(journal.path, journal.clock).value["phase"] == "publishing"


@pytest.mark.asyncio
async def test_management_workers_stop_concurrently():
    import asyncio

    from clytie.backup.controller import ManagementWorkers

    entered = []
    both = asyncio.Event()

    class Worker:
        stopping = False
        still_finishing = False

        async def stop(self, mode):
            entered.append(mode)
            if len(entered) == 2:
                both.set()
            await both.wait()

        def status(self):
            return []

    workers = ManagementWorkers(Worker(), Worker())
    await workers.stop("cancel")
    assert entered == ["cancel", "cancel"]


async def test_real_worker_bridge_captures_and_seals_live_audit(tmp_path):
    from tests.test_backup_source import fictional
    from tests.test_backup_workers import completed

    from clytie.backup.controller import CaptureBridge
    from clytie.backup.workers import BackupWorkers
    from clytie.clock import SystemClock
    from clytie.supervisor.common import Audit

    home, _ = fictional(tmp_path)
    clock = SystemClock()
    audit = Audit(home, clock)
    audit.append("test_event")
    supervisor = SimpleNamespace(children={}, rows={}, audit=audit, backup_io=IoControl(clock))
    workers = BackupWorkers(home, clock, capture_handler=CaptureBridge(supervisor))
    await workers.execute("create", {"scope": "instance"}, "d" * 32)
    result = await completed(workers, "d" * 32)
    assert result["phase"] == "committed"
    assert result["result"]["creation"] == "complete"
    assert audit.writer.status()["archive_bytes"] > 0


async def test_bridge_online_failure_cannot_select_offline(tmp_path):
    from tests.test_backup_source import fictional
    from tests.test_backup_workers import completed

    from clytie.backup.controller import CaptureBridge
    from clytie.backup.workers import BackupWorkers
    from clytie.clock import SystemClock
    from clytie.supervisor.common import Audit

    home, _ = fictional(tmp_path)
    clock = SystemClock()
    calls = []

    async def request(cid, op, args, **kwargs):
        calls.append((cid, op))
        raise ValueError("unavailable")

    supervisor = SimpleNamespace(
        children={"companion_a": object()},
        rows={"companion_a": {"state": "running"}},
        audit=Audit(home, clock),
        request=request,
        backup_io=IoControl(clock),
    )
    attach_test_capture_channel(supervisor)
    workers = BackupWorkers(home, clock, capture_handler=CaptureBridge(supervisor))
    await workers.execute("create", {"scope": "instance"}, "e" * 32)
    result = await completed(workers, "e" * 32)
    assert calls == [("companion_a", "backup_capture")]
    assert result["result"]["creation"] == "partial"


async def test_controller_reconciles_dirty_and_verified_result(tmp_path):
    from tests.test_backup_source import fictional

    from clytie.backup.controller import BackupController
    from clytie.backup.journal import BackupJournal
    from clytie.backup.workers import BackupWorkers
    from clytie.sdk.testing import MockClock

    home, _ = fictional(tmp_path)
    clock = MockClock()
    supervisor = SimpleNamespace(home=home, clock=clock, companions=["companion_a"])
    workers = BackupWorkers(home, clock)
    controller = BackupController(
        supervisor, workers, {"backup": {"schedule": {"catch_up": False}}}
    )
    controller.scheduler.mark_dirty("companion_a", 4, "fact")
    clock.advance(300)
    request = controller.scheduler.poll()
    journal, _ = BackupJournal.accept(home, clock, request.operation_id, "create", {})
    journal.commit({"creation": "complete", "verification": "passed"})
    await controller._finish_active()
    assert controller.scheduler.status()["dirty"] == {}


async def test_shutdown_poll_does_not_claim_an_older_memory_job(tmp_path):
    from clytie.backup.scheduling import BackupScheduler
    from clytie.sdk.testing import MockClock

    clock = MockClock()
    scheduler = BackupScheduler(tmp_path, clock=clock, config={"schedule": {"catch_up": False}})
    scheduler.manual("manual_memory", "memory", "companion_a")
    scheduler.mark_dirty("companion_a", 1, "fact")
    assert scheduler.shutdown("stop_one")
    request = scheduler.poll(trigger="shutdown")
    assert request.scope == "instance"


async def test_controller_shutdown_creates_once_after_worker_cancel_drain(tmp_path):
    import asyncio
    import sqlite3

    from tests.test_backup_source import fictional

    from clytie.backup.controller import BackupController
    from clytie.backup.storage import BackupStorage
    from clytie.backup.workers import BackupWorkers
    from clytie.clock import SystemClock
    from clytie.sdk.testing import MockClock

    home, _ = fictional(tmp_path)
    with sqlite3.connect(home / "companions/companion_a/memory/memory.sqlite3") as db:
        db.execute("INSERT OR REPLACE INTO metadata VALUES('backup_revision', '1')")
    clock = MockClock()
    supervisor = SimpleNamespace(
        home=home,
        clock=clock,
        companions=["companion_a"],
        escalation=asyncio.Event(),
    )
    workers = BackupWorkers(home, clock)
    controller = BackupController(
        supervisor, workers, {"backup": {"schedule": {"catch_up": False}}}
    )
    await workers.stop("cancel")
    for _ in range(2):
        closing = asyncio.create_task(controller.shutdown())
        try:
            for _ in range(500):
                if closing.done():
                    break
                await SystemClock().sleep(0.02)
                clock.advance(0.02)
            assert closing.done(), workers.io_control.budget.status()
            await closing
        finally:
            if not closing.done():
                workers.escalate()
                closing.cancel()
                await asyncio.gather(closing, return_exceptions=True)
    rows = BackupStorage(home, clock).list()
    assert len(rows) == 1
    assert rows[0]["completeness"] == "complete"
    assert not rows[0]["manual"]
    state = controller.scheduler.status()
    assert not state["dirty"]
    assert len(state["shutdowns"]) == 1


async def test_worker_bridge_uses_live_provider_and_keeps_writes_responsive(online):  # noqa: F811
    import asyncio

    from tests.test_backup_workers import completed

    from clytie.api.operations import companion_operation
    from clytie.backup.controller import CaptureBridge
    from clytie.backup.keys import BackupKeys
    from clytie.backup.workers import BackupWorkers
    from clytie.config import repository_root
    from clytie.supervisor.common import Audit

    owner, holder = online
    BackupKeys(owner.home, owner.clock, repository=repository_root()).ensure()
    calls = []

    async def request(cid, op, args, **kwargs):
        calls.append(op)
        return await companion_operation(owner, op, args)

    supervisor = SimpleNamespace(
        children={owner.cid: object()},
        rows={owner.cid: {"state": "running"}},
        audit=Audit(owner.home, owner.clock),
        request=request,
        backup_io=IoControl(owner.clock),
    )
    sessions = attach_test_capture_channel(supervisor, owner)
    workers = BackupWorkers(owner.home, owner.clock, capture_handler=CaptureBridge(supervisor))
    store = owner.host.memory_resources[owner.cid][0]
    finished = asyncio.Event()
    writes = []

    async def write():
        from clytie.backup.coordination import run_sync

        def commit():
            with store.transaction():
                store.set_meta("parallel_counter", len(writes))
            writes.append(1)

        while not finished.is_set():
            await run_sync(store, commit)
            await owner.clock.sleep(0.01)

    writer = asyncio.create_task(write())
    try:
        await workers.execute("create", {"scope": "instance"}, "f" * 32)
        result = await completed(workers, "f" * 32)
    finally:
        if workers.tasks:
            await workers.stop("cancel")
        finished.set()
        await writer
    assert result["result"]["creation"] == "complete"
    assert calls == ["backup_capture"]
    assert holder.plugin.phases == ["prepare", "capture", "materialize", "release"]
    assert len(writes) > 2
    assert sessions
    assert not supervisor.backup_io.sessions[sessions[owner.cid]].endpoints
    assert supervisor.backup_io.budget.status()["charged"]["read"] > 0
