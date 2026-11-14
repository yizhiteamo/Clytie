"""实际记忆/账本资源经内部句柄使用，事件循环不拿到连接。"""

import asyncio
import threading
from pathlib import Path

import pytest

from clytie.backup.coordination import run_sync
from clytie.kernel.ledger import Ledger
from clytie.memory.store import MemoryStore
from clytie.sdk.testing import MockClock
from clytie.storage.handle import StoreHandle
from clytie.storage.owner import StorageCancelled, StorageOwner


class Pair:
    def __init__(self, root, clock):
        self.memory = MemoryStore(root / "memory.db", clock=clock, config={}, instance={})
        self.ledger = Ledger(root / "ledger.db", "companion_a", clock)

    def close(self):
        self.memory.close()
        self.ledger.close()


async def test_real_store_handle_runs_complete_commands_and_never_exposes_connection(tmp_path):
    clock = MockClock()
    owner = await StorageOwner.start(
        lambda: Pair(tmp_path, clock), companion_id="companion_a", clock=clock
    )
    memory = StoreHandle(
        owner, "memory", MemoryStore, {"clock": clock, "path": tmp_path / "memory.db"}
    )
    ledger = StoreHandle(owner, "ledger", Ledger, {"clock": clock, "companion_id": "companion_a"})

    def write():
        with memory.transaction():
            memory.set_meta("fictional", {"revision": 1})
            memory.audit("fictional_written")
        with ledger.transaction():
            ledger.audit("fictional_tool")
        return threading.get_ident()

    try:
        with pytest.raises(RuntimeError, match="storage_requires_async_command"):
            memory.meta("fictional")
        with pytest.raises(RuntimeError, match="storage_requires_async_command"):
            _ = memory.db
        assert memory.path == tmp_path / "memory.db"
        thread = await run_sync(memory, write)
        assert thread != threading.get_ident()
        assert await run_sync(memory, memory.meta, "fictional") == {"revision": 1}
        assert (await run_sync(ledger, ledger.rows, "audit"))[0]["kind"] == "fictional_tool"
        with pytest.raises(TypeError, match="storage_result_not_snapshot"):
            await run_sync(memory, lambda: memory.db)
        assert isinstance(memory.path, Path)
    finally:
        await owner.close()


async def test_store_handle_does_not_inherit_another_command_private_scope(tmp_path):
    from clytie.plugin_host.memory import bind_scene, current_conversation, current_scene

    clock = MockClock()
    owner = await StorageOwner.start(
        lambda: Pair(tmp_path, clock), companion_id="companion_a", clock=clock
    )
    memory = StoreHandle(owner, "memory", MemoryStore, {"clock": clock})
    try:
        with bind_scene("owner_private", "owner_chat"):
            assert await run_sync(memory, lambda: (current_scene(), current_conversation())) == (
                "owner_private",
                "owner_chat",
            )
        assert await run_sync(memory, lambda: (current_scene(), current_conversation())) == (
            "public_group",
            None,
        )
    finally:
        await owner.close()


@pytest.mark.parametrize("name", ["memory", "ledger"])
async def test_production_transaction_cancel_rolls_back_before_commit(tmp_path, name):
    clock = MockClock()
    owner = await StorageOwner.start(
        lambda: Pair(tmp_path, clock), companion_id="companion_a", clock=clock
    )
    loop = asyncio.get_running_loop()
    entered, resume = asyncio.Event(), threading.Event()

    def write(resources, command):
        store = getattr(resources, name)
        with store.transaction():
            store.audit("fictional_cancelled")
            loop.call_soon_threadsafe(entered.set)
            assert resume.wait(10)

    receipt = owner.submit(write)
    try:
        await entered.wait()
        assert receipt.cancel()
        resume.set()
        with pytest.raises(StorageCancelled):
            await receipt.result()
        assert (
            await owner.execute(
                lambda r, _: getattr(r, name).db.execute("SELECT count(*) FROM audit").fetchone()[0]
            )
            == 0
        )
    finally:
        resume.set()
        await owner.close()


async def test_plugin_memory_uses_owner_projection_and_notifies_after_commit(tmp_path):
    from clytie.plugin_host.memory import bind_scene, memory_view
    from clytie.sdk.types import MemoryDraft

    clock = MockClock()
    owner = await StorageOwner.start(
        lambda: Pair(tmp_path, clock), companion_id="companion_a", clock=clock
    )
    handle = StoreHandle(owner, "memory", MemoryStore, {"clock": clock})
    notified = asyncio.Event()
    notify_threads = []

    def changed():
        notify_threads.append(threading.get_ident())
        notified.set()

    handle.changed = changed
    view = memory_view(handle, ["memory.read.private", "memory.write"])
    try:
        with bind_scene("owner_private", "owner_chat"):
            record = await view.write(
                MemoryDraft(
                    kind="fact", content="虚构观察", visibility="private", source_type="private"
                )
            )
            await notified.wait()
            assert (await view.get(record.record_id)).content == "虚构观察"
            assert len(await view.search("观察")) == 1
        with bind_scene("public_group", "test_group"):
            assert await view.get(record.record_id) is None
            assert await view.search("观察") == []
        assert notify_threads == [threading.get_ident()]
    finally:
        await owner.close()


async def test_queued_command_does_not_extend_scene_or_presence_lease(tmp_path):
    from clytie.memory.presence import bind_presence, present_actors
    from clytie.plugin_host.memory import bind_scene, current_conversation, current_scene

    clock = MockClock()
    owner = await StorageOwner.start(
        lambda: Pair(tmp_path, clock), companion_id="companion_a", clock=clock
    )
    handle = StoreHandle(owner, "memory", MemoryStore, {"clock": clock})
    release = threading.Event()
    blocked = owner.submit(lambda *_: release.wait(10))
    pending = None
    try:
        with bind_scene("owner_private", "owner_chat"), bind_presence("owner_chat", ["actor_a"]):
            pending = asyncio.create_task(
                run_sync(
                    handle,
                    lambda: (current_scene(), current_conversation(), present_actors("owner_chat")),
                )
            )
            await clock.drain()
            assert owner.status()["ordinary"] == 2
        release.set()
        await blocked.result()
        assert await pending == ("public_group", None, frozenset())
    finally:
        release.set()
        await blocked.result()
        if pending:
            await pending
        await owner.close()


@pytest.mark.parametrize("commit_started", [False, True])
async def test_business_cancel_token_at_real_memory_commit_boundary(
    tmp_path, monkeypatch, commit_started
):
    import clytie.memory.store as memory_module
    from clytie.sdk import CancelToken
    from clytie.storage.owner import storage_cancel_scope

    clock = MockClock()
    owner = await StorageOwner.start(
        lambda: Pair(tmp_path, clock), companion_id="companion_a", clock=clock
    )
    handle = StoreHandle(owner, "memory", MemoryStore, {"clock": clock})
    entered, resume = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = memory_module.before_storage_commit

    def barrier():
        if commit_started:
            original()
        loop.call_soon_threadsafe(entered.set)
        assert resume.wait(10)
        if not commit_started:
            original()

    monkeypatch.setattr(memory_module, "before_storage_commit", barrier)

    def write():
        with handle.transaction():
            handle.set_meta("fictional", "committed")
        return "committed"

    token = CancelToken()
    with storage_cancel_scope(token):
        pending = asyncio.create_task(run_sync(handle, write))
    try:
        await entered.wait()
        token.cancel("cancelled")
        resume.set()
        if commit_started:
            assert await pending == "committed"
        else:
            with pytest.raises(asyncio.CancelledError):
                await pending
        assert await run_sync(handle, handle.meta, "fictional") == (
            "committed" if commit_started else None
        )
        assert not owner.status()["failed"]
    finally:
        resume.set()
        await owner.close()
