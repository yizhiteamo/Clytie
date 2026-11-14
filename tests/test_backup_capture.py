import asyncio
import sqlite3
import threading
from contextlib import contextmanager

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
        await view.close()
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
    await view.close()
    db.close()


async def test_snapshot_connection_and_lock_have_one_dedicated_owner(tmp_path, monkeypatch):
    clock = MockClock()
    coordinator = CaptureCoordinator(tmp_path / "capture.lock", clock)
    path = tmp_path / "memory.db"
    writer = database(path)
    main_thread = threading.get_ident()
    calls = []
    connect = sqlite3.connect
    lock = coordinator._lock

    class ObservedConnection(sqlite3.Connection):
        def execute(self, *args, **kwargs):
            calls.append(("execute", threading.get_ident()))
            return super().execute(*args, **kwargs)

        def backup(self, *args, **kwargs):
            calls.append(("backup", threading.get_ident()))
            return super().backup(*args, **kwargs)

        def close(self):
            calls.append(("close", threading.get_ident()))
            return super().close()

    def observed_connect(*args, **kwargs):
        assert kwargs.get("check_same_thread", True)
        calls.append(("connect", threading.get_ident()))
        return connect(*args, factory=ObservedConnection, **kwargs)

    @contextmanager
    def observed_lock():
        calls.append(("lock", threading.get_ident()))
        with lock():
            try:
                yield
            finally:
                calls.append(("unlock", threading.get_ident()))

    async def provider(checkpoint):
        assert threading.get_ident() == main_thread
        checkpoint()

    monkeypatch.setattr(sqlite3, "connect", observed_connect)
    monkeypatch.setattr(coordinator, "_lock", observed_lock)
    try:
        view = await capture_group({"memory": path}, coordinator, epoch="owned", capture=provider)
        await view.materialize(tmp_path / "copy", pages=1)
        assert view.closed
        assert not view.resource_pending
        assert {name for name, _ in calls} >= {"connect", "backup", "close", "lock", "unlock"}
        assert len({thread for _, thread in calls}) == 1
        assert calls[0][1] != main_thread
    finally:
        writer.close()


async def test_cancel_waits_for_actual_snapshot_worker_and_releases_wal(tmp_path):
    clock = MockClock()
    coordinator = CaptureCoordinator(tmp_path / "capture.lock", clock)
    path = tmp_path / "memory.db"
    writer = database(path)
    view = await capture_group({"memory": path}, coordinator, epoch="cancel")
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def blocked_check():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10), "test driver did not release snapshot worker"

    task = asyncio.create_task(view.materialize(tmp_path / "copy", pages=1, check=blocked_check))
    try:
        await entered.wait()
        task.cancel()
        await clock.drain()
        assert not task.done()
        assert not view.closed
        assert view.resource_pending
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert view.closed
        assert not view.resource_pending
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    finally:
        release.set()
        await view.close()
        writer.close()


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


@pytest.mark.parametrize("cancel", [False, True])
async def test_slow_capture_lock_keeps_loop_responsive_and_rejects_late_view(
    tmp_path, monkeypatch, cancel
):
    clock = MockClock()
    coordinator = CaptureCoordinator(tmp_path / "capture.lock", clock)
    path = tmp_path / "memory.db"
    writer = database(path)
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    lock = coordinator._lock
    threads, providers = [], []

    @contextmanager
    def slow_lock():
        threads.append(threading.get_ident())
        with lock():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10), "test driver did not release capture lock"
            yield

    async def provider(checkpoint):
        providers.append(True)

    monkeypatch.setattr(coordinator, "_lock", slow_lock)
    task = asyncio.create_task(
        capture_group({"memory": path}, coordinator, epoch="late", capture=provider)
    )
    try:
        await entered.wait()
        if cancel:
            task.cancel()
        else:
            clock.advance(coordinator.hold_seconds)
        await clock.drain()
        assert not task.done()
        assert threads[0] != threading.get_ident()
        release.set()
        with pytest.raises(asyncio.CancelledError if cancel else ValueError):
            await task
        assert not providers
        assert not any(t.ident in threads for t in threading.enumerate())
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        with lock():
            pass
    finally:
        release.set()
        writer.close()


async def test_provider_time_counts_against_shared_hold_deadline(tmp_path):
    clock = MockClock()
    coordinator = CaptureCoordinator(tmp_path / "capture.lock", clock)
    path = tmp_path / "memory.db"
    writer = database(path)

    async def provider(checkpoint):
        clock.advance(0.2)
        checkpoint()
        await clock.drain()
        clock.advance(0.05)

    try:
        with pytest.raises(ValueError, match="capture_busy"):
            await capture_group({"memory": path}, coordinator, epoch="late", capture=provider)
        with coordinator.commit():
            pass
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    finally:
        writer.close()


async def test_release_failure_never_reports_complete_capture(tmp_path, monkeypatch):
    import clytie.backup.capture as implementation

    clock = MockClock()
    coordinator = CaptureCoordinator(tmp_path / "capture.lock", clock)
    path = tmp_path / "memory.db"
    writer = database(path)
    lock = coordinator._lock
    groups = []
    original = implementation.CapturedGroup

    class ObservedGroup(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            groups.append(self)

    @contextmanager
    def failing_release():
        with lock():
            yield
        raise OSError("fictional_release_failure")

    monkeypatch.setattr(implementation, "CapturedGroup", ObservedGroup)
    monkeypatch.setattr(coordinator, "_lock", failing_release)
    try:
        with pytest.raises(OSError, match="fictional_release_failure"):
            await capture_group({"memory": path}, coordinator, epoch="release_failure")
        assert not groups[0].closed
        assert groups[0].resource_pending
        assert not groups[0]._worker.thread.is_alive()
    finally:
        writer.close()
