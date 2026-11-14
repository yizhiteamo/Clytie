import asyncio
import sqlite3
import threading
from contextlib import closing

import pytest

from clytie.backup.capture import capture_group
from clytie.backup.coordination import CaptureCoordinator, CaptureError
from clytie.sdk.testing import MockClock
from clytie.storage.owner import StorageOwner


class Pair:
    def __init__(self, root):
        self.paths = {name: root / f"{name}.db" for name in ("memory", "ledger")}
        self.connections = {name: sqlite3.connect(path) for name, path in self.paths.items()}
        self.closed = threading.Event()
        for db in self.connections.values():
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE generation(value INTEGER)")
            db.execute("INSERT INTO generation VALUES(0)")
            db.commit()

    def write(self, value):
        for db in self.connections.values():
            with db:
                db.execute("UPDATE generation SET value=?", (value,))

    def close(self):
        for db in self.connections.values():
            db.close()
        self.closed.set()


async def test_capture_barrier_keeps_fifo_and_slot_until_real_view_close(tmp_path):
    clock = MockClock()
    coordinator = CaptureCoordinator(tmp_path / "capture.lock", clock)
    owner = await StorageOwner.start(
        lambda: Pair(tmp_path),
        companion_id="companion_a",
        clock=clock,
        coordination_path=coordinator.path,
    )
    paths = await owner.execute(lambda r, _: r.paths)
    loop = asyncio.get_running_loop()
    writing, capturing = asyncio.Event(), asyncio.Event()
    finish_write, finish_capture = threading.Event(), asyncio.Event()

    def prior(resources, command):
        loop.call_soon_threadsafe(writing.set)
        assert finish_write.wait(10)
        resources.write(2)

    async def provider(checkpoint):
        capturing.set()
        await finish_capture.wait()
        checkpoint()

    first = owner.submit(prior)
    await writing.wait()
    task = asyncio.create_task(
        capture_group(paths, coordinator, epoch="test", capture=provider, owner=owner)
    )
    view = None
    try:
        await clock.drain()
        assert owner.status()["capture_pending"]
        finish_write.set()
        await capturing.wait()
        after = owner.submit(lambda r, _: r.write(3))
        assert after.phase == "queued"
        finish_capture.set()
        view = await task
        await first.result()
        await after.result()
        with pytest.raises(CaptureError, match="capture_busy"):
            await capture_group(paths, coordinator, epoch="duplicate", owner=owner)
        result = await view.materialize(tmp_path / "copy", pages=1)
        for path in result.values():
            with closing(sqlite3.connect(path)) as db:
                assert db.execute("SELECT value FROM generation").fetchone()[0] == 2
        assert not owner.status()["capture_pending"]
    finally:
        finish_write.set()
        finish_capture.set()
        if view is None:
            view = await task
        await view.close()
        await owner.close()


async def test_closing_owner_waits_for_snapshot_and_wakes_after_release(tmp_path):
    clock = MockClock()
    coordinator = CaptureCoordinator(tmp_path / "capture.lock", clock)
    resources = []

    def factory():
        pair = Pair(tmp_path)
        resources.append(pair)
        return pair

    owner = await StorageOwner.start(factory, companion_id="companion_a", clock=clock)
    paths = await owner.execute(lambda r, _: r.paths)
    view = await capture_group(paths, coordinator, epoch="test", owner=owner)
    closing_owner = asyncio.create_task(owner.close())
    try:
        await clock.drain()
        assert not closing_owner.done()
        await view.close()
        # 物理线程退出信号；测试驱动等待不属于产品时钟或性能测量。
        assert resources[0].closed.wait(2)
    finally:
        await view.close()
        # 即使断言发现唤醒缺陷，也必须收回本测试明确创建的线程。
        with owner._condition:
            owner._condition.notify_all()
        await closing_owner


@pytest.mark.parametrize("cancel", [False, True])
async def test_capture_wait_deadline_or_cancel_releases_views_but_tracks_queued_barrier(
    tmp_path, cancel
):
    clock = MockClock()
    coordinator = CaptureCoordinator(tmp_path / "capture.lock", clock)
    owner = await StorageOwner.start(
        lambda: Pair(tmp_path), companion_id="companion_a", clock=clock
    )
    paths = await owner.execute(lambda r, _: r.paths)
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def blocked(*_):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)

    active = owner.submit(blocked)
    await entered.wait()
    pending = asyncio.create_task(capture_group(paths, coordinator, epoch="test", owner=owner))
    try:
        while owner._capture_ticket is None or owner._capture_ticket.command is None:
            await clock.sleep(0)
        await clock.drain()
        if cancel:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            clock.advance(2)
            with pytest.raises(CaptureError, match="capture_busy"):
                await pending
        # 只读线程已关闭，但尚未执行的屏障仍占自己的名额，不能谎称全部释放。
        assert owner.status()["capture_pending"]
        assert owner._capture_ticket.command.phase == "queued"
        release.set()
        await active.result()
        while owner.status()["capture_pending"]:
            await clock.sleep(0)
        assert clock.pending_sleep_count == 0
        view = await capture_group(paths, coordinator, epoch="next", owner=owner)
        await view.close()
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await owner.close()


async def test_capture_full_ordinary_queue_rejects_without_abandoned_resources(tmp_path):
    clock = MockClock()
    coordinator = CaptureCoordinator(tmp_path / "capture.lock", clock)
    owner = await StorageOwner.start(
        lambda: Pair(tmp_path), companion_id="companion_a", clock=clock, ordinary_capacity=1
    )
    paths = await owner.execute(lambda r, _: r.paths)
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def blocked(*_):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)

    active = owner.submit(blocked)
    await entered.wait()
    try:
        with pytest.raises(CaptureError, match="capture_busy"):
            await capture_group(paths, coordinator, epoch="test", owner=owner)
        assert not owner.status()["capture_pending"]
        assert owner.status()["ordinary"] == 1
    finally:
        release.set()
        await active.result()
        await owner.close()


async def test_real_owner_capture_reentry_rejected_across_child_task(tmp_path):
    clock = MockClock()
    coordinator = CaptureCoordinator(tmp_path / "capture.lock", clock)
    owner = await StorageOwner.start(
        lambda: Pair(tmp_path),
        companion_id="companion_a",
        clock=clock,
        coordination_path=coordinator.path,
    )
    paths = await owner.execute(lambda r, _: r.paths)

    async def provider(checkpoint):
        with pytest.raises(RuntimeError, match="storage_capture_reentry"):
            await asyncio.create_task(owner.execute(lambda *_: "must not execute"))
        checkpoint()

    try:
        view = await capture_group(paths, coordinator, epoch="test", capture=provider, owner=owner)
        await view.close()
        assert not owner.status()["capture_pending"]
        assert owner.status()["ordinary"] == 0
    finally:
        await owner.close()
