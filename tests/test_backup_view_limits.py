import asyncio
import sqlite3
import struct
import threading

import pytest

from clytie.backup.capture import capture_group
from clytie.backup.coordination import CaptureCoordinator, CaptureError
from clytie.backup.view_limits import ViewLimits, WalGrowth
from clytie.sdk.testing import MockClock


def wal(path, size, salt=1):
    with path.open("wb") as stream:
        stream.write(struct.pack(">8I", 0x377F0682, 3_007_000, 4096, 0, salt, salt, 0, 0))
        stream.truncate(size)


def test_wal_growth_counts_generations_and_truncation_without_negative_delta(tmp_path):
    path = tmp_path / "memory.db-wal"
    wal(path, 128)
    growth = WalGrowth(path)
    with path.open("ab") as stream:
        stream.write(b"x" * 64)
    assert growth.sample() == 64
    wal(path, 96, salt=2)
    assert growth.sample() == 160  # generation replacement doesn't erase previous growth
    wal(path, 64, salt=2)
    assert growth.sample() == 224  # truncation cannot subtract earlier high water
    with path.open("ab") as stream:
        stream.write(b"x" * 16)
    assert growth.sample() == 240
    assert growth.sample() == 240


def test_unknown_or_disappearing_wal_is_not_assumed_empty(tmp_path):
    path = tmp_path / "memory.db-wal"
    missing = WalGrowth(path)
    assert missing.sample() == 0
    wal(path, 64)
    assert missing.sample() == 64
    path.rename(tmp_path / "retained-wal")
    with pytest.raises(CaptureError, match="snapshot_wal_unconfirmed"):
        missing.sample()
    path.write_bytes(b"broken")
    with pytest.raises(CaptureError, match="snapshot_wal_unconfirmed"):
        WalGrowth(path)


def test_view_and_wal_limits_are_fixed_and_boundary_inclusive(tmp_path, monkeypatch):
    import clytie.backup.view_limits as module

    clock = MockClock()
    path = tmp_path / "memory.db-wal"
    wal(path, 64)
    limits = ViewLimits({"memory": tmp_path / "memory.db"}, clock, pinned_at=0)
    clock.advance(300)
    with pytest.raises(CaptureError, match="snapshot_view_expired"):
        limits.check()
    assert module.MAX_WAL_GROWTH == 256 * 1024 * 1024
    # This unit's tiny file exercises exact comparison; production default is unchanged.
    monkeypatch.setattr(module, "MAX_WAL_GROWTH", 64)
    limits = ViewLimits({"memory": tmp_path / "memory.db"}, clock, pinned_at=300)
    with path.open("ab") as stream:
        stream.write(b"x" * 64)
    with pytest.raises(CaptureError, match="snapshot_wal_limit"):
        limits.check()


async def test_real_pinned_view_expiry_closes_reader_and_releases_wal(tmp_path):
    clock = MockClock()
    path = tmp_path / "memory.db"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE facts(value TEXT)")
    writer.execute("INSERT INTO facts VALUES('fictional')")
    writer.commit()
    view = await capture_group(
        {"memory": path}, CaptureCoordinator(tmp_path / "capture.lock", clock), epoch="test"
    )
    try:
        writer.execute("UPDATE facts SET value='new fictional value'")
        writer.commit()
        clock.advance(300)
        with pytest.raises(CaptureError, match="snapshot_view_expired"):
            await view.materialize(tmp_path / "copy")
        assert view.closed and not view.resource_pending
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        assert not (tmp_path / "copy").exists()
    finally:
        await view.close()
        writer.close()


@pytest.mark.parametrize("blocked_io", [False, True])
async def test_view_watchdog_expires_idle_view_but_waits_for_uninterruptible_io(
    tmp_path, blocked_io
):
    clock = MockClock()
    path = tmp_path / "memory.db"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE facts(value TEXT)")
    writer.commit()
    view = await capture_group(
        {"memory": path}, CaptureCoordinator(tmp_path / "capture.lock", clock), epoch="test"
    )
    entered, release = threading.Event(), threading.Event()
    task = None

    def block():
        entered.set()
        assert release.wait(10)

    try:
        await clock.drain()
        if blocked_io:
            task = asyncio.create_task(view.materialize(tmp_path / "copy", check=block))
            for _ in range(10_000):
                if entered.is_set():
                    break
                await clock.sleep(0)
            assert entered.is_set()
        clock.advance(300)
        await clock.drain()
        assert view._worker.limit_error.code == "snapshot_view_expired"
        if blocked_io:
            assert view.resource_pending and not task.done()
            release.set()
            with pytest.raises(CaptureError, match="snapshot_view_expired"):
                await task
        else:
            # Explicitly await the already-started cleanup, not a new materialization.
            assert view._closing is not None
            await view._closing
        assert view.closed and not view.resource_pending
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    finally:
        release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await view.close()
        writer.close()
