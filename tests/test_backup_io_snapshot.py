import sqlite3
import threading

import pytest

from clytie.backup.capture import capture_group
from clytie.backup.coordination import CaptureCoordinator, CaptureError
from clytie.backup.io_budget import CHUNK
from clytie.backup.io_client import IoClient, io_scope
from clytie.sdk.testing import MockClock


def source_database(path):
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE facts(version INTEGER, payload BLOB)")
    db.execute("INSERT INTO facts VALUES(1, zeroblob(?))", (CHUNK * 3,))
    db.commit()
    return db


async def test_pinned_sqlite_copy_charges_real_batches_on_snapshot_thread(tmp_path):
    clock = MockClock()
    coordinator = CaptureCoordinator(tmp_path / "capture.lock", clock)
    path = tmp_path / "memory.db"
    source = source_database(path)
    size = (
        source.execute("PRAGMA page_count").fetchone()[0]
        * source.execute("PRAGMA page_size").fetchone()[0]
    )
    calls = []
    thread = threading.get_ident()

    def request(arguments, cleanup=False):
        assert threading.get_ident() != thread
        # Every budget wait happens outside the capture/commit latch.
        with coordinator.commit():
            with sqlite3.connect(path) as writer:
                writer.execute("UPDATE facts SET version=version+1")
        calls.append(arguments)
        return {}

    try:
        with io_scope(IoClient(request)):
            view = await capture_group({"memory": path}, coordinator, epoch="test_epoch")
            outputs = await view.materialize(tmp_path / "copy", pages=128)
        assert view.closed and not view.resource_pending
        assert calls and calls[0]["action"] == "acquire"
        charged = [call for call in calls if call["action"] == "charge"]
        assert all(0 < call["size"] <= CHUNK for call in charged)
        for direction in ("read", "write"):
            assert sum(call["size"] for call in charged if call["direction"] == direction) == size
        assert calls[-1]["action"] == "release"
        with sqlite3.connect(outputs["memory"]) as copied:
            assert copied.execute("SELECT version FROM facts").fetchone()[0] == 1
        assert source.execute("SELECT version FROM facts").fetchone()[0] > 1
    finally:
        source.close()


async def test_snapshot_budget_wait_checks_view_limit_and_closes_real_reader(tmp_path):
    clock = MockClock()
    coordinator = CaptureCoordinator(tmp_path / "capture.lock", clock)
    path = tmp_path / "memory.db"
    source = source_database(path)
    calls = []

    def request(arguments, cleanup=False):
        calls.append(arguments)
        if arguments["action"] == "charge":
            clock.advance(300)
        return {}

    try:
        with io_scope(IoClient(request)):
            view = await capture_group({"memory": path}, coordinator, epoch="test_epoch")
            with pytest.raises(CaptureError, match="snapshot_view_expired"):
                await view.materialize(tmp_path / "copy")
        assert calls[-1]["action"] == "release"
        assert view.closed and not view.resource_pending
        assert source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    finally:
        source.close()
