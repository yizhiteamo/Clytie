import asyncio
import sqlite3

import pytest

from clytie.backup.capture import capture_group
from clytie.backup.coordination import CaptureCoordinator
from clytie.sdk.testing import MockClock


def database(path):
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE facts(value INTEGER, payload BLOB)")
    db.execute("INSERT INTO facts VALUES(0, zeroblob(?))", (100_000,))
    db.commit()
    return db


async def test_group_keeps_read_views_while_concurrent_writes_continue(tmp_path):
    clock = MockClock()
    coordinator = CaptureCoordinator(tmp_path / "capture.lock", clock)
    paths = {name: tmp_path / f"{name}.db" for name in ("memory", "ledger")}
    writers = {name: database(path) for name, path in paths.items()}
    view = await capture_group(paths, coordinator, epoch="test_epoch")
    batches = []
    copying = asyncio.Event()
    pulse = []
    loop = asyncio.get_running_loop()

    def write_during_copy():
        loop.call_soon_threadsafe(copying.set)
        with coordinator.commit():
            for path in paths.values():
                with sqlite3.connect(path) as db:
                    db.execute("UPDATE facts SET value=value+1")
        batches.append(1)

    async def chat_request():
        await copying.wait()
        pulse.append("responded")

    try:
        chatting = asyncio.create_task(chat_request())
        results = await view.materialize(tmp_path / "output", pages=1, check=write_during_copy)
        await chatting
        assert pulse == ["responded"]
        assert len(batches) > 2
        for path in results.values():
            with sqlite3.connect(path) as db:
                assert db.execute("SELECT value FROM facts").fetchone()[0] == 0
        assert writers["memory"].execute("SELECT value FROM facts").fetchone()[0] > 0
    finally:
        view.close()
        for db in writers.values():
            db.close()


async def test_capture_wait_bound_and_failure_release(tmp_path):
    clock = MockClock()
    coordinator = CaptureCoordinator(tmp_path / "capture.lock", clock)
    path = tmp_path / "memory.db"
    db = database(path)
    with coordinator.commit():
        task = asyncio.create_task(capture_group({"memory": path}, coordinator, epoch="test"))
        await clock.drain()
        clock.advance(2)
        await clock.drain()
        with pytest.raises(ValueError, match="capture_busy"):
            await task
    view = await capture_group({"memory": path}, coordinator, epoch="next")
    view.close()
    db.close()


async def test_materialize_cancel_closes_view(tmp_path):
    clock = MockClock()
    coordinator = CaptureCoordinator(tmp_path / "capture.lock", clock)
    path = tmp_path / "memory.db"
    db = database(path)
    view = await capture_group({"memory": path}, coordinator, epoch="test")

    def stop():
        raise ValueError("cancelled")

    with pytest.raises(ValueError, match="cancelled"):
        await view.materialize(tmp_path / "output", check=stop)
    assert view.closed
    db.close()
