"""真实临时伴侣的存储所有权与停止顺序。"""

import asyncio
import sqlite3
import threading
from contextlib import contextmanager

import pytest

from clytie.backup.coordination import run_sync
from clytie.kernel.ledger import Ledger
from clytie.memory.store import MemoryStore
from clytie.plugin_host.memory import bind_scene
from clytie.sdk.testing import MockClock
from clytie.sdk.types import MemoryDraft
from clytie.storage.handle import StoreHandle
from clytie.supervisor.companion import Companion, StartupBlocked

from .test_supervisor_startup import home_copy


async def test_companion_owns_real_connections_through_shutdown(tmp_path, monkeypatch):
    loop_thread = threading.get_ident()
    observed = []
    for kind in (Ledger, MemoryStore):
        for name in ("__init__", "close"):
            original = getattr(kind, name)

            def observed_call(self, *args, _original=original, _kind=kind, _name=name, **kwargs):
                observed.append((_kind.__name__, _name, threading.get_ident()))
                return _original(self, *args, **kwargs)

            monkeypatch.setattr(kind, name, observed_call)
        original_transaction = kind.transaction

        @contextmanager
        def transaction(self, _original=original_transaction, _kind=kind):
            observed.append((_kind.__name__, "transaction", threading.get_ident()))
            with _original(self):
                yield

        monkeypatch.setattr(kind, "transaction", transaction)
    runtime = Companion(home_copy(tmp_path), "companion_a", MockClock())
    try:
        assert await runtime.start()
        assert isinstance(runtime.kernel.ledger, StoreHandle)
        assert isinstance(runtime.host.memory_store("companion_a"), StoreHandle)
        await run_sync(runtime.kernel.ledger, runtime.kernel.ledger.health_counts)
    finally:
        await runtime.stop()
    assert observed
    assert all(thread != loop_thread for _, _, thread in observed)
    assert len({thread for _, _, thread in observed}) == 1


async def test_companions_use_distinct_owners(tmp_path):
    home = home_copy(tmp_path, two=True)
    first = Companion(home, "companion_a", MockClock())
    second = Companion(home, "companion_b", MockClock())
    try:
        assert await first.start()
        assert await second.start()
        assert first.storage_owner is not second.storage_owner
        assert first.storage_owner._thread.ident != second.storage_owner._thread.ident
        with pytest.raises(RuntimeError, match="storage_requires_async_command"):
            first.kernel.ledger.health_counts()
    finally:
        await first.stop()
        await second.stop()


async def test_plugin_stop_can_write_before_owner_closes(tmp_path):
    runtime = Companion(home_copy(tmp_path), "companion_a", MockClock())
    await runtime.start()
    store = runtime.host.memory_store("companion_a")
    plugin = runtime.host.runtimes["companion_a", "fake-model"]
    unit = (plugin.units or [plugin])[0]
    original = unit.plugin.stop
    written = []

    async def stop():
        with bind_scene("public_group", "test_group"):
            written.append(
                await store.for_permissions(["memory.write"]).write(
                    MemoryDraft(
                        kind="fact",
                        content="Fictional shutdown observation",
                        visibility="public",
                        source_type="group",
                    )
                )
            )
        await original()

    unit.plugin.stop = stop
    try:
        assert (await runtime.stop())["state"] == "stopped"
        assert len(written) == 1
        assert not runtime.storage_owner._thread.is_alive()
        db = sqlite3.connect(store.path)
        try:
            assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1
        finally:
            db.close()
    finally:
        await runtime.stop()


async def test_busy_health_keeps_last_observation_without_loop_reads(tmp_path, monkeypatch):
    runtime = Companion(home_copy(tmp_path), "companion_a", MockClock())
    await runtime.start()
    owner = runtime.storage_owner
    entered, release = asyncio.Event(), threading.Event()

    def hold(resources, command):
        owner.loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)

    command = owner.submit(hold)
    try:
        await entered.wait()
        owner.ordinary_capacity = 1
        before = runtime.health()["storage_observed_at"]
        runtime.clock.advance(1)
        with monkeypatch.context() as patch:
            patch.setattr(Ledger, "health_counts", lambda *_: pytest.fail("unexpected query"))
            await runtime.refresh_health()
            assert runtime.health()["storage_observed_at"] == before
        release.set()
        await command.result()
        owner.ordinary_capacity = 64
        await runtime.refresh_health()
        assert runtime.health()["storage_observed_at"] != before
    finally:
        owner.ordinary_capacity = 64
        release.set()
        await command.result()
        await runtime.stop()


async def test_owner_factory_failure_releases_memory_and_ledger_locks(tmp_path):
    from clytie.memory.transfer import runtime_lock
    from clytie.plugin_host.locking import file_lock

    home = home_copy(tmp_path)
    directory = home / "companions/companion_a/ledger"
    directory.mkdir()
    db = sqlite3.connect(directory / "ledger.sqlite3")
    db.execute("PRAGMA user_version=999")
    db.close()
    runtime = Companion(home, "companion_a", MockClock())
    try:
        with pytest.raises(StartupBlocked) as exc:
            await runtime.start()
        assert exc.value.reason == "migration_blocked"
        assert not runtime.host.memory_resources
        with runtime_lock(home, "companion_a"), file_lock(directory / ".lock", {}):
            pass
    finally:
        await runtime.stop()
