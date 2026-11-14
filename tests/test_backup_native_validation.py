"""Native SQLite reads use a stage, not invented byte accounting."""

import asyncio
import sqlite3
import threading
from contextlib import contextmanager

import pytest

from clytie.backup import io_client, restore
from clytie.backup.io_client import IoClient, io_checks, io_scope
from clytie.memory.store import MemoryStore
from clytie.sdk.testing import MockClock


@contextmanager
def opened(path):
    with restore._database(path, restore.MEMORY_TABLES) as db:
        yield db


def memory_file(tmp_path):
    path = tmp_path / "memory.sqlite3"
    store = MemoryStore(path, clock=MockClock(), config={}, instance={})
    store.close()
    return path


def test_native_database_holds_stage_until_connection_closed_without_byte_charge(tmp_path):
    path = memory_file(tmp_path)
    calls, connections = [], []

    def request(arguments, cleanup=False):
        calls.append(arguments)
        if arguments["action"] == "release":
            for db in connections:
                with pytest.raises(sqlite3.ProgrammingError):
                    db.execute("SELECT 1")
        return {}

    with io_scope(IoClient(request)), opened(path) as db:
        connections.append(db)
        assert io_client._stage.get() is not None
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert db.execute("PRAGMA query_only").fetchone() == (1,)
    assert [row["action"] for row in calls] == ["acquire", "release"]


def test_native_database_nested_reads_reuse_one_stage(tmp_path):
    path = memory_file(tmp_path)
    calls = []

    def request(arguments, cleanup=False):
        calls.append(arguments)
        return {}

    with io_scope(IoClient(request)), opened(path) as first, opened(path) as second:
        assert first.execute("SELECT count(*) FROM records").fetchone() == (0,)
        assert second.execute("SELECT count(*) FROM records").fetchone() == (0,)
    assert [row["action"] for row in calls] == ["acquire", "release"]


def test_native_progress_restores_original_cancellation_and_closes(tmp_path):
    path = memory_file(tmp_path)
    calls, executing, checkpoints = [], False, 0
    connection = None

    def request(arguments, cleanup=False):
        calls.append(arguments)
        return {}

    def check():
        nonlocal checkpoints
        if executing:
            checkpoints += 1
            if checkpoints == 3:
                raise InterruptedError("fictional_cancel")

    with (
        io_scope(IoClient(request)),
        io_checks(check),
        pytest.raises(InterruptedError, match="fictional_cancel"),
        opened(path) as db,
    ):
        connection = db
        executing = True
        db.execute(
            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<512) "
            "SELECT sum(a.x*b.x) FROM n a,n b"
        ).fetchone()
    assert checkpoints == 3
    assert calls[-1]["action"] == "release"
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_native_validation_error_releases_stage(tmp_path):
    path = memory_file(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=3")
    db.close()
    calls = []

    def request(arguments, cleanup=False):
        calls.append(arguments)
        return {}

    with io_scope(IoClient(request)), pytest.raises(restore.RestoreError), opened(path):
        pytest.fail("invalid database accepted")
    assert [row["action"] for row in calls] == ["acquire", "release"]


@pytest.mark.parametrize("scope", ["instance", "memory"])
def test_real_create_holds_shared_stage_for_every_native_check(tmp_path, monkeypatch, scope):
    from uuid import uuid4

    from tests.test_backup_creation import Controls
    from tests.test_backup_source import fictional

    from clytie.backup import creation
    from clytie.backup.journal import BackupJournal

    home, clock = fictional(tmp_path)
    observed, calls = [], []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        db = connect(*args, **kwargs)

        def trace(statement):
            if statement.casefold() in {"pragma quick_check", "pragma integrity_check"}:
                observed.append(io_client._stage.get() is not None)

        db.set_trace_callback(trace)
        return db

    def request(arguments, cleanup=False):
        calls.append(arguments)
        return {}

    arguments = {"scope": scope}
    if scope == "memory":
        arguments["companion_id"] = "companion_a"
    journal, _ = BackupJournal.accept(home, clock, uuid4().hex, "create", arguments)
    monkeypatch.setattr(sqlite3, "connect", traced)
    with io_scope(IoClient(request)):
        result = creation.dispatch(home, clock, journal, Controls())
    assert result["creation"] == "complete"
    assert len(observed) >= 2
    assert all(observed), observed
    assert sum(row["action"] == "acquire" for row in calls) == sum(
        row["action"] == "release" for row in calls
    )


async def test_native_query_and_other_heavy_io_share_real_instance_slot(tmp_path):
    from clytie.backup.io_control import IoControl

    path = memory_file(tmp_path)
    clock, loop = MockClock(), asyncio.get_running_loop()
    control = IoControl(clock)
    inside, queued = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    order = []
    grant_waiter = control.budget._grant

    def observe_grant():
        grant_waiter()
        if control.budget.status()["waiting"] == 1:
            queued.set()

    control.budget._grant = observe_grant

    def client(number):
        session = control.register()
        operation = str(number) * 32
        control.authorize(session, operation)
        endpoint = control.sessions[session].endpoints[operation]

        async def grant(arguments):
            return await endpoint.request(arguments)

        def request(arguments, cleanup=False):
            return asyncio.run_coroutine_threadsafe(grant(arguments), loop).result(timeout=10)

        return IoClient(request), session

    first_client, first_session = client(1)
    other_client, other_session = client(2)

    def blocked():
        loop.call_soon_threadsafe(inside.set)
        assert release.wait(10)
        return 1

    def native():
        with io_scope(first_client), opened(path) as db:
            db.create_function("fictional_block", 0, blocked)
            assert db.execute("SELECT fictional_block()").fetchone() == (1,)
        order.append("native_closed")

    def other():
        with io_scope(other_client), other_client.stage():
            order.append("other_entered")

    first = asyncio.create_task(asyncio.to_thread(native))
    second = None
    try:
        await inside.wait()
        second = asyncio.create_task(asyncio.to_thread(other))
        await queued.wait()
        assert control.budget.status()["waiting"] == 1
        assert not first.done() and not second.done()
        assert order == []
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second is not None else []))
        await control.ended(first_session, confirmed=True)
        await control.ended(other_session, confirmed=True)
    assert set(order) == {"native_closed", "other_entered"}
    assert control.budget.status()["active"] is None


def test_cancel_during_real_package_native_validation_never_publishes(tmp_path, monkeypatch):
    from uuid import uuid4

    from tests.test_backup_creation import Controls
    from tests.test_backup_source import fictional

    from clytie.backup import creation
    from clytie.backup.journal import BackupJournal
    from clytie.backup.storage import BackupStorage
    from clytie.backup.worker import Cancelled

    home, clock = fictional(tmp_path)
    journal, _ = BackupJournal.accept(home, clock, uuid4().hex, "create", {"scope": "instance"})
    cancelled, calls = False, []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        db = connect(*args, **kwargs)

        def trace(statement):
            nonlocal cancelled
            if journal.value["phase"] == "verifying" and statement == "PRAGMA integrity_check":
                cancelled = True

        db.set_trace_callback(trace)
        return db

    class CancellingControls(Controls):
        def check(self):
            if cancelled:
                raise Cancelled()

    def request(arguments, cleanup=False):
        calls.append(arguments)
        return {}

    monkeypatch.setattr(sqlite3, "connect", traced)
    with io_scope(IoClient(request)), pytest.raises(Cancelled):
        creation.dispatch(home, clock, journal, CancellingControls())
    assert cancelled
    assert BackupStorage(home, clock).list() == []
    assert journal.value["phase"] == "verifying"
    assert sum(row["action"] == "acquire" for row in calls) == sum(
        row["action"] == "release" for row in calls
    )


async def test_queued_native_cancel_never_opens_database_or_releases_other_slot(
    tmp_path, monkeypatch
):
    from clytie.backup.io_budget import IoBudgetError
    from clytie.backup.io_channel import IoChannel
    from clytie.backup.io_control import IoControl
    from clytie.clock import SystemClock
    from clytie.plugin_host.support import timed

    path = memory_file(tmp_path)
    control, loop = IoControl(MockClock()), asyncio.get_running_loop()
    holder, waiter = control.register(), control.register()
    control.authorize(holder, "holder")
    control.authorize(waiter, "waiter")
    holding = control.sessions[holder].endpoints["holder"]
    waiting = control.sessions[waiter].endpoints["waiter"]
    await holding.request({"action": "acquire", "stage": "occupied"})
    queued, cancel = asyncio.Event(), threading.Event()
    tasks, opened_connections = [], []
    grant = control.budget._grant
    connect = sqlite3.connect

    def observe_grant():
        grant()
        if control.budget.status()["waiting"] == 1:
            queued.set()

    def traced(*args, **kwargs):
        opened_connections.append(args)
        return connect(*args, **kwargs)

    async def handle(message):
        try:
            reply = {"result": await waiting.request(message["arguments"])}
        except IoBudgetError:
            reply = {"error": "backup_io_stage_finished"}
        channel.receive({"type": "backup_io_reply", "id": message["id"], **reply})

    channel = IoChannel(
        lambda msg: loop.call_soon_threadsafe(
            lambda: tasks.append(asyncio.create_task(handle(msg)))
        )
    )

    def check():
        if cancel.is_set():
            raise InterruptedError("fictional_cancel")

    def work():
        with io_scope(channel.client("waiter", check=check)), io_checks(check), opened(path):
            pytest.fail("cancelled queued validation entered")

    monkeypatch.setattr(control.budget, "_grant", observe_grant)
    monkeypatch.setattr(sqlite3, "connect", traced)
    task = asyncio.create_task(asyncio.to_thread(work))
    try:
        await timed(queued.wait(), SystemClock(), 10)
        cancel.set()
        with pytest.raises(InterruptedError, match="fictional_cancel"):
            await timed(task, SystemClock(), 10)
        await asyncio.gather(*tasks)
        assert opened_connections == []
        assert channel.pending_count == 0
        assert control.budget.status()["waiting"] == 0
        assert control.budget.status()["active"] is not None
    finally:
        cancel.set()
        await holding.request({"action": "release", "stage": "occupied"})
        await control.ended(holder, confirmed=True)
        await control.ended(waiter, confirmed=True)
    assert control.budget.status()["active"] is None
