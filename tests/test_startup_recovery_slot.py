"""启动前的真实日志/SQLite 恢复；调度等待在 Owner 工厂之外。"""

import threading
from types import SimpleNamespace

import pytest

from clytie.api.management_journal import Journal
from clytie.backup.io_client import IoClient, _stage
from clytie.memory.store import MemoryStore
from clytie.sdk.testing import MockClock

from .test_memory_transfer import remember


async def test_startup_recovery_uses_new_slot_and_file_owned_connection(tmp_path, monkeypatch):
    from clytie.api import management_journal
    from clytie.storage import startup_recovery

    clock = MockClock()
    path = tmp_path / "companions/companion_a/memory/memory.sqlite3"
    store = MemoryStore(
        path, clock=clock, config={"names": {"internal_scenes": ["owner_private"]}}, instance={}
    )
    remember(store)
    store.close()
    journal = Journal.create(tmp_path, "companion_a", "import/apply", clock)
    journal.database(path)
    journal.phase("transaction")
    monkeypatch.setattr(management_journal, "executor_alive", lambda identity: False)
    events = []
    main_thread = threading.get_ident()

    def factory(operation, *, check):
        assert operation == journal.value["id"]

        def request(arguments, *, cleanup=False):
            assert threading.get_ident() != main_thread
            events.append(arguments["action"])
            return {}

        return IoClient(request)

    original = Journal._database
    threads = []

    def database(self, *args):
        assert _stage.get() is not None
        threads.append(threading.get_ident())
        return original(self, *args)

    monkeypatch.setattr(Journal, "_database", database)
    runtime = SimpleNamespace(
        home=tmp_path,
        cid="companion_a",
        clock=clock,
        backup_io_client=factory,
        stop_requested=lambda: False,
    )
    await startup_recovery.recover_before_owner(runtime)
    assert events == ["acquire", "release"]
    assert threads and all(thread != main_thread for thread in threads)
    assert Journal.load(journal.path, clock).value["state"] == "interrupted"
    # A repeated startup does not replay recovery or resurrect its old credential.
    await startup_recovery.recover_before_owner(runtime)
    assert events == ["acquire", "release"]


async def test_startup_recovery_cancel_before_admission_leaves_journal_untouched(
    tmp_path, monkeypatch
):
    from clytie.api import management_journal
    from clytie.api.management_journal import ManagementCancelled
    from clytie.storage import startup_recovery

    clock = MockClock()
    journal = Journal.create(tmp_path, "companion_a", "import/apply", clock)
    journal.phase("preparing")
    original = journal.path.read_bytes()
    monkeypatch.setattr(management_journal, "executor_alive", lambda identity: False)
    runtime = SimpleNamespace(
        home=tmp_path,
        cid="companion_a",
        clock=clock,
        backup_io_client=lambda *args, **kwargs: pytest.fail("cancelled before acquire"),
        stop_requested=lambda: True,
    )
    with pytest.raises(ManagementCancelled):
        await startup_recovery.recover_before_owner(runtime)
    assert journal.path.read_bytes() == original


@pytest.mark.parametrize("phase", ["before_commit", "after_commit", "damaged_database"])
async def test_real_kill_then_companion_start_recovers_with_fresh_authorized_slot(tmp_path, phase):
    import asyncio
    import shutil

    from clytie.plugin_host.support import timed
    from clytie.supervisor.processes import ProcessLauncher, python_process
    from clytie.supervisor.service import Supervisor

    from .management_integration_helpers import until
    from .test_supervisor_smoke import ROOT

    home = tmp_path / "fictional-home"
    shutil.copytree(ROOT / "examples/home", home)
    clock = MockClock()
    journal = Journal.create(
        home, "companion_a", "before_commit" if phase == "damaged_database" else phase, clock
    )
    executable, env = python_process()
    child = await asyncio.create_subprocess_exec(
        executable,
        "-m",
        "tests.management_crash_fixture",
        str(home),
        journal.value["id"],
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    launcher = ProcessLauncher(home)
    svc = Supervisor(home, ["companion_a"], launcher, clock)
    events = []
    dispatch = svc.backup_io.dispatch

    def observe(session, message, send):
        if message.get("operation_id") == journal.value["id"]:
            events.append((session, message["arguments"]["action"]))
        return dispatch(session, message, send)

    svc.backup_io.dispatch = observe
    try:
        from clytie.clock import SystemClock

        # Read exactly the fixture's commit barrier before killing its real writer.
        line = await timed(child.stdout.readline(), SystemClock(), 15)
        assert line.strip() == b"ready"
        child.kill()
        await child.wait()
        if phase == "damaged_database":
            import sqlite3

            damaged = home / "companions/companion_a/memory/memory.sqlite3"
            # Settle the killed transaction before fault injection. Otherwise a
            # valid retained WAL can legitimately reconstruct the overwritten page.
            db = sqlite3.connect(damaged)
            try:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                db.close()
            damaged.write_bytes(b"fictional damaged database")
        await svc.start()
        await until(lambda: svc.rows["companion_a"]["state"] in {"running", "blocked"})
        if phase == "damaged_database":
            assert svc.rows["companion_a"]["state"] == "blocked"
            assert svc.rows["companion_a"]["restart_reason"] == "management_recovery_blocked"
            assert Journal.load(journal.path, clock).value["state"] == "recovery_blocked"
            await until(lambda: svc.backup_io.budget.status()["active"] is None)
            assert [action for _, action in events] == ["acquire"]
            assert launcher.children[0].process.poll() is not None
            return
        assert svc.rows["companion_a"]["state"] == "running", svc.rows
        recovered = Journal.load(journal.path, clock).value
        assert recovered["committed"] is (phase == "after_commit")
        assert recovered["state"] == ("committed" if phase == "after_commit" else "interrupted")
        assert [action for _, action in events] == ["acquire", "release"]
        assert len({session for session, _ in events}) == 1
        assert svc.backup_io.budget.status()["active"] is None
        # Query through online management is unnecessary here: a temporary
        # read-only connection verifies the durable marker, never a second writer.
        import json
        import sqlite3

        db = sqlite3.connect(
            (home / "companions/companion_a/memory/memory.sqlite3").resolve().as_uri() + "?mode=ro",
            uri=True,
        )
        try:
            entries = [json.loads(row[0]) for row in db.execute("SELECT data FROM audit")]
        finally:
            db.close()
        assert sum(row["kind"] == "fictional_write" for row in entries) == (
            1 if phase == "after_commit" else 0
        )
        # No original management worker was launched by recovery.
        assert len(launcher.children) == 1
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
        await child.communicate()
        await svc.stop("cancel")
        launcher.close()
