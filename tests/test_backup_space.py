"""空间估算、真实 SQLite 固定视图及暂停 provider 的离线验收。"""

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from clytie.backup.space import SpaceError, SpaceGuard, estimate, space_status
from clytie.sdk.testing import MockClock


def setup(tmp_path, *, clock=None):
    clock = clock or MockClock()
    home = tmp_path / "home"
    home.mkdir()
    core = home / "companions/companion_a/memory/memory.sqlite3"
    core.parent.mkdir(parents=True)
    core.write_bytes(b"fictional metadata")
    op = uuid4().hex
    plan = estimate(
        home,
        op,
        companions=["companion_a"],
        scope="memory",
        reserve_bytes=1024,
        destination=home / "backups/regular",
        clock=clock,
    )
    guard = SpaceGuard(home, op, clock=clock)
    guard.start(plan)
    return home, core, guard, plan, clock


def test_estimate_uncompressed_peak_excludes_credentials_and_existing_backups(tmp_path):
    home, core, guard, plan, clock = setup(tmp_path)
    for relative in ("secrets/private", "run/private", "backups/regular/existing", "logs/private"):
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 4096)
    other = estimate(
        home,
        uuid4().hex,
        companions=["companion_a"],
        scope="memory",
        reserve_bytes=1024,
        destination=home / "backups/regular",
        clock=clock,
    )
    assert other["source_bytes"] == plan["source_bytes"] == core.stat().st_size
    assert other["package_bytes"] > other["source_bytes"]
    assert other["temporary_budget_bytes"] >= 3 * other["source_bytes"]
    assert other["occupied"]["regular_repository"]["bytes"] == 4096
    status = space_status(home)[0]
    assert status["estimated"] and not status["guaranteed"]
    assert str(home) not in str(status)


def test_preflight_disk_shortage_and_midwrite_shortage_preserve_existing_backup(
    tmp_path, monkeypatch
):
    from clytie.backup import space

    home, core, guard, plan, clock = setup(tmp_path)
    package = home / "backups/regular/known"
    package.parent.mkdir(parents=True)
    package.write_bytes(b"existing verified package")
    monkeypatch.setattr(space.shutil, "disk_usage", lambda path: SimpleNamespace(free=1023))
    with pytest.raises(SpaceError) as error:
        guard.check(force=True)
    assert error.value.dimension == "reserve_free"
    assert package.read_bytes() == b"existing verified package"
    second_plan = {**plan, "operation_id": uuid4().hex}
    second = SpaceGuard(home, second_plan["operation_id"], clock=clock)
    with pytest.raises(SpaceError) as error:
        second.start(second_plan)
    assert error.value.dimension == "preflight_free"


def test_temporary_growth_is_bounded_and_failure_is_shared(tmp_path):
    home, core, guard, plan, clock = setup(tmp_path)
    work = Path(plan["work"])
    work.mkdir(parents=True)
    with (work / "fictional-partial").open("wb") as stream:
        stream.truncate(plan["temporary_budget_bytes"] + 1)
    with pytest.raises(SpaceError) as error:
        guard.check(force=True)
    assert error.value.dimension == "temporary_budget"
    other = SpaceGuard(home, plan["operation_id"], clock=clock).load()
    with pytest.raises(SpaceError):
        other.check(force=True)


def test_creation_preflight_refuses_before_capture_without_deleting_existing_data(
    tmp_path, monkeypatch
):
    from tests.test_backup_creation import Controls
    from tests.test_backup_source import fictional

    from clytie.backup import creation, space
    from clytie.backup.journal import BackupJournal
    from clytie.backup.keys import BackupKeys

    home, clock = fictional(tmp_path)
    journal, _ = BackupJournal.accept(home, clock, uuid4().hex, "create", {"scope": "instance"})
    BackupKeys(home, clock, repository=Path(__file__).resolve().parents[1]).ensure()
    old = home / "backups/known-fictional-package"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"preserve existing")
    monkeypatch.setattr(space.shutil, "disk_usage", lambda path: SimpleNamespace(free=1024**3))
    monkeypatch.setattr(
        creation, "capture_instance", lambda *a, **k: pytest.fail("capture started")
    )
    with pytest.raises(SpaceError) as error:
        creation.dispatch(home, clock, journal, Controls())
    assert error.value.dimension == "preflight_free"
    assert old.read_bytes() == b"preserve existing"
    assert space_status(home)[0]["state"] == "blocked"


def test_real_package_write_space_loss_prevents_publication(tmp_path, monkeypatch):
    from tests.test_backup_creation import Controls
    from tests.test_backup_source import fictional

    from clytie.backup import creation, space
    from clytie.backup.journal import BackupJournal

    home, clock = fictional(tmp_path)
    journal, _ = BackupJournal.accept(home, clock, uuid4().hex, "create", {"scope": "instance"})
    work = home / "run/backup-work" / journal.value["id"]
    original = space.shutil.disk_usage

    def capacity(path):
        pending = work / "package.pending"
        if pending.exists() and pending.stat().st_size:
            return SimpleNamespace(free=0)
        return original(path)

    monkeypatch.setattr(space.shutil, "disk_usage", capacity)
    with pytest.raises(SpaceError):
        creation.dispatch(home, clock, journal, Controls())
    assert (work / "package.pending").stat().st_size > 0
    assert not (home / "backups/regular" / (journal.value["id"] + ".clytie-backup")).exists()


def test_two_real_processes_merge_observations_without_overwriting_blocked_state(tmp_path):
    import subprocess
    import sys

    home, core, guard, plan, clock = setup(tmp_path)
    script = """
import sys
from clytie.backup.space import SpaceGuard
guard = SpaceGuard(sys.argv[1], sys.argv[2]).load()
for _ in range(5):
    guard.check(force=True)
"""
    processes = [
        subprocess.Popen([sys.executable, "-c", script, str(home), plan["operation_id"]])
        for _ in range(2)
    ]
    try:
        assert [child.wait(timeout=20) for child in processes] == [0, 0]
    finally:
        for child in processes:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
    assert space_status(home)[0]["state"] == "monitoring"


async def test_real_sqlite_wal_growth_aborts_snapshot_and_releases_read_view(tmp_path):
    from clytie.backup.capture import capture_group
    from clytie.backup.coordination import CaptureCoordinator

    home, core, _, _, clock = setup(tmp_path)
    core.unlink()
    db = sqlite3.connect(core)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA wal_autocheckpoint=0")
    db.execute("CREATE TABLE facts(value BLOB)")
    db.execute("INSERT INTO facts VALUES(zeroblob(4096))")
    db.commit()
    op = uuid4().hex
    plan = estimate(
        home,
        op,
        companions=["companion_a"],
        scope="memory",
        reserve_bytes=1024,
        destination=home / "backups/regular",
        clock=clock,
    )
    guard = SpaceGuard(home, op, clock=clock)
    guard.start(plan)
    coordinator = CaptureCoordinator(home / "capture.lock", clock)
    view = await capture_group({"memory": core}, coordinator, epoch=op)
    wrote, responsive = [], []
    loop = asyncio.get_running_loop()

    def concurrent_write():
        if not wrote:
            wrote.append(True)
            with coordinator.commit(), sqlite3.connect(core) as writer:
                writer.executemany("INSERT INTO facts VALUES(zeroblob(4096))", [()] * 512)
            loop.call_soon_threadsafe(responsive.append, True)
        guard.check(force=True)

    try:
        with pytest.raises(SpaceError) as error:
            await view.materialize(Path(plan["work"]) / "copy", pages=1, check=concurrent_write)
        assert error.value.dimension == "wal_growth"
        assert view.closed
        await clock.drain()
        assert responsive
        assert db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        db.execute("INSERT INTO facts VALUES('still writing')")
        db.commit()
    finally:
        view.close()
        db.close()


async def test_paused_provider_is_cancelled_without_waiting_for_sink_write(tmp_path, monkeypatch):
    from tests.test_backup_providers import Provider, session

    from clytie.backup import space

    home, core, guard, plan, clock = setup(tmp_path)
    started = asyncio.Event()

    class Paused(Provider):
        async def materialize(self, view, sink, cancel):
            started.set()
            await asyncio.Event().wait()

    provider = Paused()
    run = session(tmp_path, provider, clock)
    assert await run.prepare()
    assert await run.capture("epoch")
    task = asyncio.create_task(guard.watch(run.materialize(), clock=clock))
    await started.wait()
    monkeypatch.setattr(space.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    # Advance injected time until the observer's thread has completed its sample.
    for _ in range(100):
        if task.done():
            break
        await clock.advance_and_drain(0.25)
        await asyncio.to_thread(lambda: None)
    with pytest.raises(SpaceError):
        await task
    await run.release()
    assert "release" in provider.calls
    assert not run.result()["resource_pending"]
