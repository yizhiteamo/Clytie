"""Real shutdown backup: a committed journal is not executor release evidence."""

import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.test_backup_source import fictional

from clytie.backup.controller import BackupController
from clytie.backup.io_budget import IoBudgetError
from clytie.backup.workers import BackupWorkers, executor_alive, worker_locked
from clytie.clock import SystemClock
from clytie.sdk.testing import MockClock

HOLD_COMMIT = """
import sys, threading
from pathlib import Path
from clytie.backup import worker
entered, release = Path(sys.argv[3]), Path(sys.argv[4])
sys.argv = sys.argv[:3]
original = worker.WorkerJournal.commit
def commit(self, result):
    original(self, result)
    entered.write_text('committed', encoding='utf-8')
    while not release.exists():
        threading.Event().wait(.01)
worker.WorkerJournal.commit = commit
worker.main()
"""


@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_shutdown_retains_executor_until_real_exit(tmp_path, monkeypatch, cancel_waiter):
    # Keep SQLite auxiliary paths below legacy Windows path limits. The fixture
    # still lives under pytest's temporary root, never in a real instance.
    work = Path(tempfile.mkdtemp(prefix="sr-", dir=tmp_path.parent))
    home, _ = fictional(work)
    with sqlite3.connect(home / "companions/companion_a/memory/memory.sqlite3") as db:
        db.execute("INSERT OR REPLACE INTO metadata VALUES('backup_revision', '1')")
    entered, release = work / "entered", work / "release"
    original = asyncio.create_subprocess_exec

    async def launch(*args, **kwargs):
        if args[1:3] == ("-m", "clytie.backup.worker"):
            args = (args[0], "-c", HOLD_COMMIT, *args[3:], str(entered), str(release))
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    clock = MockClock()
    supervisor = SimpleNamespace(
        home=home, clock=clock, companions=["companion_a"], escalation=asyncio.Event()
    )
    workers = BackupWorkers(home, clock)
    controller = BackupController(
        supervisor, workers, {"backup": {"schedule": {"catch_up": False}}}
    )
    await workers.stop("cancel")
    closing = asyncio.create_task(controller.shutdown())

    async def until(predicate):
        for _ in range(500):
            if predicate():
                return
            await SystemClock().sleep(0.02)
            clock.advance(0.02)
        pytest.fail("shutdown release checkpoint not reached")

    try:
        await until(entered.exists)
        assert not closing.done()
        operation, process = next(iter(workers.processes.items()))
        value = workers.query(operation)
        assert value["phase"] == "committed"
        assert value["result"]["creation"] == "complete"
        assert process.returncode is None
        assert worker_locked(home)
        replacement = BackupWorkers(home, clock)
        with pytest.raises(IoBudgetError, match="backup_io_recheck_required"):
            await replacement.io_control.ensure_ready()
        if cancel_waiter:
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
            assert not workers.tasks[operation].done()
            assert process.returncode is None
            assert worker_locked(home)
        release.write_text("release", encoding="utf-8")
        await until(lambda: not workers.tasks)
        if not cancel_waiter:
            await closing
        assert process.returncode == 0
        assert process.stdout.at_eof()
        assert process.stdin.is_closing()
        assert not worker_locked(home)
        assert not workers.io_control.sessions
        assert workers.io_control.budget.status()["active"] is None
        assert workers.io_control.budget.status()["waiting"] == 0
        assert not workers.io_control._metadata_lock.locked()
        journal = json.loads(workers.journals[operation].path.read_bytes())
        assert executor_alive(journal["executor"]) is False
        index = json.loads((home / "supervisor/io-responsibility/index.json").read_bytes())
        assert all(row["state"] == "ended" for row in index["records"].values())
        assert (await replacement.io_control.recheck())["state"] == "ready"
        await controller.shutdown()
        assert len(workers.status()) == 1
        assert workers.query(operation)["phase"] == "committed"
        assert not workers.processes
        assert not workers.tasks
    finally:
        release.write_text("release", encoding="utf-8")
        if not closing.done():
            closing.cancel()
        await asyncio.gather(closing, return_exceptions=True)
        await workers._drain()
