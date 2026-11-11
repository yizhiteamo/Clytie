import asyncio
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from clytie.backup.coordination import CaptureCoordinator, run_sync
from clytie.sdk.testing import MockClock
from clytie.storage.capture_scope import capture_scope
from clytie.storage.owner import (
    StorageBusy,
    StorageCancelled,
    StorageContext,
    StorageFailed,
    StorageOwner,
    current_storage_context,
)


class Resources:
    def __init__(self, path, lifecycle):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE facts(value TEXT)")
        self.values = []
        self.lifecycle = lifecycle
        lifecycle.append(("open", threading.get_ident()))

    def close(self):
        self.db.close()
        self.lifecycle.append(("close", threading.get_ident()))


async def create(tmp_path, *, ordinary=64, finalizers=16):
    lifecycle = []
    owner = await StorageOwner.start(
        lambda: Resources(tmp_path / "memory.db", lifecycle),
        companion_id="companion_a",
        clock=MockClock(),
        ordinary_capacity=ordinary,
        finalizer_capacity=finalizers,
        coordination_path=tmp_path / "capture.lock",
    )
    return owner, lifecycle


async def test_connections_context_and_results_are_owned(tmp_path):
    owner, lifecycle = await create(tmp_path)
    context = StorageContext("companion_a", "owner_private", "owner_chat", ("memory.read.private",))

    def inspect(resources, command):
        assert current_storage_context() == command.context
        resources.values.append(command.context.scene)
        return {"values": resources.values, "thread": threading.get_ident()}

    try:
        result = await owner.execute(inspect, context=context)
        result["values"].append("external_mutation")
        other = await owner.execute(inspect)
        assert other["values"] == ["owner_private", "public_group"]
        assert result["thread"] == other["thread"] != threading.get_ident()
        assert current_storage_context() is None
        with pytest.raises(ValueError, match="storage_context_mismatch"):
            await owner.execute(inspect, context=StorageContext("companion_b"))
        with pytest.raises(TypeError, match="storage_result_not_snapshot"):
            await owner.execute(lambda resources, _: resources.db)
    finally:
        await owner.close()
    assert lifecycle == [("open", result["thread"]), ("close", result["thread"])]


async def test_ready_fifo_capacity_and_multistage_finalization(tmp_path):
    owner, _ = await create(tmp_path, ordinary=2, finalizers=1)
    entered, release = threading.Event(), threading.Event()
    order = []

    def block(_, command):
        entered.set()
        assert release.wait(10)
        order.append("first")

    token = owner.reserve_finalizer()
    with pytest.raises(StorageBusy):
        owner.reserve_finalizer()
    first = owner.submit(block)
    second = owner.submit(lambda *_: order.append("second"))
    try:
        # running 项也占普通容量；未就绪的 token 不占 FIFO 队头。
        with pytest.raises(StorageBusy):
            owner.submit(lambda *_: None)
        final = owner.submit(lambda *_: order.append("receipt"), finalizer=token)
        with pytest.raises(StorageBusy):
            owner.submit(lambda *_: None, finalizer=token)
        release.set()
        await first.result()
        await second.result()
        await final.result()
        assert order == ["first", "second", "receipt"]
        assert owner.status()["finalizers"] == 1
        await owner.execute(lambda *_: order.append("late_audit"), finalizer=token)
        owner.release_finalizer(token, executor_finished=True, obligations_complete=True)
        owner.release_finalizer(token, executor_finished=True, obligations_complete=True)
        assert owner.status()["finalizers"] == 0
    finally:
        release.set()
        await owner.close()


async def test_cancel_queued_and_running_transaction_before_commit(tmp_path):
    owner, _ = await create(tmp_path)
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    def transaction(resources, command):
        with resources.db:
            resources.db.execute("INSERT INTO facts VALUES('fictional')")
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10)
            command.begin_commit()

    active = owner.submit(transaction)
    queued = owner.submit(lambda *_: pytest.fail("cancelled queued command executed"))
    try:
        await entered.wait()
        assert active.cancel()
        assert queued.cancel()
        assert owner.status()["ordinary"] == 2
        release.set()
        for receipt in (active, queued):
            with pytest.raises(StorageCancelled):
                await receipt.result()
        assert (
            await owner.execute(lambda r, _: r.db.execute("SELECT * FROM facts").fetchall()) == []
        )
    finally:
        release.set()
        await owner.close()


async def test_caller_cancel_does_not_erase_committed_fact(tmp_path):
    owner, _ = await create(tmp_path)
    loop = asyncio.get_running_loop()
    committing = asyncio.Event()
    release = threading.Event()

    def transaction(resources, command):
        with resources.db:
            resources.db.execute("INSERT INTO facts VALUES('fictional')")
            command.begin_commit()
            loop.call_soon_threadsafe(committing.set)
            assert release.wait(10)
        return "committed"

    receipt = owner.submit(transaction)
    waiter = asyncio.create_task(receipt.result())
    try:
        await committing.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not receipt.cancel()
        assert receipt.phase == "committing"
        release.set()
        assert await receipt.result() == "committed"
        assert (
            await owner.execute(
                lambda r, _: r.db.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            )
            == 1
        )
    finally:
        release.set()
        await owner.close()


async def test_stop_allows_reserved_plugin_finalization(tmp_path):
    owner, lifecycle = await create(tmp_path)
    token = owner.reserve_finalizer()
    owner.begin_stop()
    try:
        with pytest.raises(StorageBusy):
            owner.submit(lambda *_: None)
        await owner.execute(lambda r, _: r.values.append("plugin_stopped"), finalizer=token)
        with pytest.raises(StorageBusy):
            owner.release_finalizer(token, executor_finished=False, obligations_complete=True)
        closing = asyncio.create_task(owner.close())
        await owner.clock.drain()
        assert not closing.done()
        owner.release_finalizer(token, executor_finished=True, obligations_complete=True)
        await closing
    finally:
        await owner.close()
    assert [event for event, _ in lifecycle] == ["open", "close"]


async def test_busy_companion_does_not_block_another_owner(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    a, _ = await create(left, ordinary=1)
    b, _ = await create(right)
    release = threading.Event()
    blocked = a.submit(lambda *_: release.wait(10))
    try:
        assert await b.execute(lambda *_: "available") == "available"
        assert a.status()["ordinary"] == 1
        assert a.status()["observed_at"]
    finally:
        release.set()
        await blocked.result()
        await a.close()
        await b.close()


async def test_default_limits_include_running_and_reservations_are_not_commands(tmp_path):
    owner, _ = await create(tmp_path)
    release = threading.Event()
    first = owner.submit(lambda *_: release.wait(10))
    receipts = [first]
    tokens = []
    try:
        tokens = [owner.reserve_finalizer() for _ in range(16)]
        with pytest.raises(StorageBusy):
            owner.reserve_finalizer()
        receipts.extend(owner.submit(lambda *_: None) for _ in range(63))
        with pytest.raises(StorageBusy):
            owner.submit(lambda *_: None)
        assert owner.status()["ordinary"] == 64
        for token in tokens:
            receipts.append(owner.submit(lambda *_: "necessary", finalizer=token))
        release.set()
        for receipt in receipts:
            await receipt.result()
        assert owner.status()["ordinary"] == 0
        assert owner.status()["finalizers"] == 16
    finally:
        release.set()
        for receipt in receipts:
            await receipt.result()
        for token in tokens:
            owner.release_finalizer(token, executor_finished=True, obligations_complete=True)
        await owner.close()


async def test_finalizer_waiter_cancel_and_stop_do_not_drop_late_responsibility(tmp_path):
    owner, _ = await create(tmp_path)
    token = owner.reserve_finalizer()
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    def necessary(resources, _):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        with resources.db:
            resources.db.execute("INSERT INTO facts VALUES('late_result')")
        return "recorded"

    receipt = owner.submit(necessary, finalizer=token)
    waiter = asyncio.create_task(receipt.result())
    try:
        await entered.wait()
        owner.begin_stop()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not receipt.cancel()
        with pytest.raises(StorageBusy):
            owner.release_finalizer(token, executor_finished=True, obligations_complete=True)
        release.set()
        assert await receipt.result() == "recorded"
        # 额外审计是同一责任的第二次提交，停止不移除它的容量。
        await owner.execute(lambda r, _: r.values.append("late_audit"), finalizer=token)
        owner.release_finalizer(token, executor_finished=True, obligations_complete=True)
    finally:
        release.set()
        await owner.close()


async def test_owner_fatal_failure_closes_original_resources_without_replacement(tmp_path):
    owner, lifecycle = await create(tmp_path)
    release = threading.Event()

    def fail(*_):
        assert release.wait(10)
        raise SystemExit("fictional owner failure")

    active = owner.submit(fail)
    queued = owner.submit(lambda *_: pytest.fail("executed after fatal failure"))
    release.set()
    for receipt in (active, queued):
        with pytest.raises(StorageFailed):
            await receipt.result()
    with pytest.raises(StorageFailed):
        await owner.close()
    with pytest.raises(StorageFailed):
        owner.submit(lambda *_: None)
    assert owner.status()["failed"]
    assert owner.status()["ordinary"] == 0
    assert len(lifecycle) == 2
    assert lifecycle[0][1] == lifecycle[1][1]


async def test_context_is_reset_after_command_error(tmp_path):
    owner, _ = await create(tmp_path)

    def fail(*_):
        assert current_storage_context().scene == "owner_private"
        raise ValueError("fictional failure")

    try:
        with pytest.raises(ValueError, match="fictional failure"):
            await owner.execute(fail, context=StorageContext("companion_a", "owner_private"))
        assert await owner.execute(lambda *_: current_storage_context().scene) == "public_group"
    finally:
        await owner.close()


async def test_capture_reentry_follows_child_tasks_but_not_unrelated_requests(tmp_path):
    owner, _ = await create(tmp_path)
    start = asyncio.Event()

    async def unrelated():
        await start.wait()
        return await owner.execute(lambda *_: "unrelated")

    task = asyncio.create_task(unrelated())
    try:
        with capture_scope(tmp_path / "capture.lock"):
            with pytest.raises(RuntimeError, match="storage_capture_reentry"):
                await asyncio.create_task(owner.execute(lambda *_: None))
            # 旧接口尚未迁移完成时也不能让 capture 调用排队等自己。
            store = SimpleNamespace(
                coordination=CaptureCoordinator(tmp_path / "capture.lock", owner.clock)
            )
            with pytest.raises(RuntimeError, match="storage_capture_reentry"):
                await asyncio.create_task(run_sync(store, lambda: None))
            start.set()
            assert await task == "unrelated"
        assert await owner.execute(lambda *_: "outside_capture") == "outside_capture"
        with capture_scope(tmp_path / "other-companion.lock"):
            assert await owner.execute(lambda *_: "other_capture") == "other_capture"
    finally:
        start.set()
        await task
        await owner.close()


async def test_worker_cannot_enqueue_behind_itself(tmp_path):
    owner, _ = await create(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="storage_thread_reentry"):
            await owner.execute(lambda *_: owner.submit(lambda *_: None))
        assert await owner.execute(lambda *_: "still_available") == "still_available"
    finally:
        await owner.close()
