"""在线管理命令使用真实所有者与虚构临时实例，保留提交证据。"""

from types import SimpleNamespace

import pytest

from clytie.api.management_journal import Journal, current
from clytie.api.operations import companion_operation, memory_operation
from clytie.backup.coordination import run_sync
from clytie.kernel.ledger import Ledger
from clytie.memory.store import MemoryStore
from clytie.sdk.testing import MockClock
from clytie.storage.handle import StoreHandle
from clytie.storage.owner import StorageOwner

from .test_memory_transfer import remember


@pytest.fixture
async def managed(tmp_path, request):
    clock = MockClock()
    config = {"names": {"internal_scenes": ["owner_private"]}}
    instance = {
        "conversations": {
            "owner_chat": {
                "scene": "owner_private",
                "kind": "private",
                "channel": "fake-channel",
                "platform_id": "fictional-private",
            }
        }
    }
    path = tmp_path / "companions/companion_a"

    class Resources:
        def __init__(self):
            self.memory = MemoryStore(
                path / "memory/memory.sqlite3", clock=clock, config=config, instance=instance
            )
            self.ledger = Ledger(path / "ledger/ledger.sqlite3", "companion_a", clock)

        def close(self):
            self.memory.close()
            self.ledger.close()

    owner = await StorageOwner.start(Resources, companion_id="companion_a", clock=clock)
    memory = StoreHandle(
        owner,
        "memory",
        MemoryStore,
        {
            "clock": clock,
            "config": config,
            "instance": instance,
            "path": path / "memory/memory.sqlite3",
        },
    )
    ledger = StoreHandle(
        owner,
        "ledger",
        Ledger,
        {"clock": clock, "companion_id": "companion_a", "path": path / "ledger/ledger.sqlite3"},
    )
    hosted = SimpleNamespace(
        cid="companion_a",
        home=tmp_path,
        clock=clock,
        host=SimpleNamespace(memory_store=lambda cid: memory),
        agent=SimpleNamespace(ledger=ledger),
    )
    record = (
        await run_sync(memory, lambda: remember(memory))
        if getattr(request, "param", True)
        else None
    )
    try:
        yield hosted, memory, ledger, record
    finally:
        await owner.close()


async def test_online_thaw_preserves_management_marker_on_owner(managed):
    runtime, memory, ledger, record = managed
    journal = Journal.create(runtime.home, runtime.cid, "thaw", runtime.clock)
    journal.runtime_owned = True
    binding = current.set(journal)
    try:
        result = await memory_operation(
            runtime.home,
            runtime.clock,
            runtime.cid,
            "thaw",
            {"record_id": record.record_id},
            runtime,
        )
    finally:
        current.reset(binding)
    assert result == {"record_id": record.record_id, "thawed": True}
    audits = await run_sync(
        memory, lambda: [dict(row) for row in memory.db.execute("SELECT * FROM audit")]
    )
    assert journal.value["committed"]
    assert any(journal.value["id"] in row["data"] for row in audits)


async def test_online_task_query_uses_existing_owner_without_new_connection(managed, monkeypatch):
    runtime, memory, ledger, record = managed

    def forbidden(*args, **kwargs):
        raise AssertionError("online query must reuse the owner's connection")

    monkeypatch.setattr(Ledger, "__init__", forbidden)
    result = await companion_operation(runtime, "tasks", {"task_id": "deadbeef"})
    assert result["error"]["code"] == "not_found"


async def test_online_export_does_not_block_owner_during_encryption(managed, monkeypatch):
    import asyncio
    import threading
    from contextlib import contextmanager

    from clytie.memory import package

    runtime, memory, ledger, record = managed
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = package.encrypt
    original_lock = package.Exports.recovery_lock
    lock_threads = []

    @contextmanager
    def recovery_lock(exports):
        with original_lock(exports):
            lock_threads.append(threading.get_ident())
            try:
                yield
            finally:
                lock_threads.append(threading.get_ident())

    monkeypatch.setattr(package.Exports, "recovery_lock", recovery_lock)

    def slow_encrypt(*args):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(package, "encrypt", slow_encrypt)
    job = asyncio.create_task(
        memory_operation(
            runtime.home,
            runtime.clock,
            runtime.cid,
            "export",
            {"out": str(runtime.home / "fictional.enc"), "passphrase": "fictional-long-passphrase"},
            runtime,
        )
    )
    await entered.wait()
    try:
        # 活动所有者可继续处理读取，加密线程没有持捕获门闩或活动连接。
        assert (await run_sync(memory, memory.get_raw, record.record_id)).content == record.content
    finally:
        release.set()
    result = await job
    assert result["recovery_code"]
    assert len(lock_threads) == 2 and lock_threads[0] == lock_threads[1]
    assert lock_threads[0] != threading.get_ident()


async def test_management_request_cancellation_keeps_real_operation_and_marker(
    managed, monkeypatch
):
    import asyncio
    import threading

    from clytie.api.management_worker import execute

    runtime, memory, ledger, record = managed
    runtime.management_journals = {}
    journal = Journal.create(runtime.home, runtime.cid, "thaw", runtime.clock)
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = MemoryStore.audit

    def audit(self, kind, **fields):
        if kind == "memory.thawed":
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10)
        return original(self, kind, **fields)

    monkeypatch.setattr(MemoryStore, "audit", audit)
    request = asyncio.create_task(
        execute(
            runtime.home,
            runtime.cid,
            journal.value["id"],
            {"record_id": record.record_id},
            clock=runtime.clock,
            hosted=runtime,
        )
    )
    await entered.wait()
    request.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await request
        assert runtime.management_tasks
        assert memory._owner.status()["finalizers"] == 1
    finally:
        release.set()
        await asyncio.gather(*tuple(runtime.management_tasks))
    result = Journal.load(journal.path, runtime.clock)
    assert result.value["committed"]
    assert memory._owner.status()["finalizers"] == 0
    assert not runtime.management_journals


async def test_online_export_audit_failure_recovers_using_same_owned_connection(
    managed, monkeypatch
):
    import sqlite3

    from clytie.api.management_worker import execute

    runtime, memory, ledger, record = managed
    runtime.management_journals = {}
    journal = Journal.create(runtime.home, runtime.cid, "export", runtime.clock)
    original_audit = MemoryStore.audit
    original_connect = sqlite3.connect
    failed = []

    def audit(self, kind, **fields):
        if kind == "memory_export" and not failed:
            failed.append(True)
            raise OSError("fictional audit failure")
        return original_audit(self, kind, **fields)

    def connect(database, *args, **kwargs):
        if "memory/memory.sqlite3" in str(database).replace("\\", "/"):
            assert "mode=ro" in str(database), "online recovery cannot open another writer"
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(MemoryStore, "audit", audit)
    monkeypatch.setattr(sqlite3, "connect", connect)
    result = await execute(
        runtime.home,
        runtime.cid,
        journal.value["id"],
        {"out": str(runtime.home / "fictional.enc"), "passphrase": "fictional-long-passphrase"},
        clock=runtime.clock,
        hosted=runtime,
    )
    assert failed and result["ok"] and result["code"] == "committed"
    records = await run_sync(
        memory, lambda: [row[0] for row in memory.db.execute("SELECT data FROM audit")]
    )
    assert (
        sum('"kind":"memory_export"' in row or '"kind": "memory_export"' in row for row in records)
        == 1
    )
    assert memory._owner.status()["finalizers"] == 0


async def test_online_merge_preview_and_apply_keep_import_id_and_snapshot(managed):

    from clytie.api.management_worker import execute
    from clytie.api.memory import MemoryAPI

    runtime, memory, ledger, record = managed
    runtime.management_journals = {}
    # Existing store's own stable package provides a repeated, no-rewrite merge.
    exported = await memory_operation(
        runtime.home,
        runtime.clock,
        runtime.cid,
        "export",
        {"out": str(runtime.home / "source.enc"), "passphrase": "fictional-long-passphrase"},
        runtime,
    )
    api = MemoryAPI(runtime.home, clock=runtime.clock, hosted=runtime)
    plan = await api.plan(
        runtime.cid, exported["path"], mode="merge", passphrase="fictional-long-passphrase"
    )
    journal = Journal.create(runtime.home, runtime.cid, "import/apply", runtime.clock)
    result = await execute(
        runtime.home,
        runtime.cid,
        journal.value["id"],
        {"plan_id": plan["id"], "passphrase": "fictional-long-passphrase"},
        clock=runtime.clock,
        hosted=runtime,
    )
    assert result["ok"] and result["result"]["import_id"]
    saved = Journal.load(journal.path, runtime.clock)
    assert saved.value["committed"] and saved.snapshot_verified()
    assert (await run_sync(memory, memory.get_raw, record.record_id)).content == record.content


@pytest.mark.parametrize("managed", [False], indirect=True)
async def test_online_importer_stays_on_loop_and_commits_through_owner(managed):
    import threading

    from clytie.api.memory import MemoryAPI
    from clytie.sdk.types import ImportCandidate

    runtime, memory, ledger, _ = managed
    loop_thread = threading.get_ident()
    source = runtime.home / "fictional-source.txt"
    source.write_text("fictional importer source", encoding="utf-8")
    calls = []

    class Importer:
        async def detect(self, source):
            assert threading.get_ident() == loop_thread
            calls.append("detect")
            return True

        async def read(self, source, cancel):
            assert threading.get_ident() == loop_thread
            calls.append("read")
            yield ImportCandidate(
                record_id="e" * 32,
                kind="preference",
                content="fictional import",
                visibility="public",
                created_at=runtime.clock.now(),
                updated_at=runtime.clock.now(),
                proposed_basis="observed",
            )

    api = MemoryAPI(
        runtime.home,
        clock=runtime.clock,
        hosted=runtime,
        importers={"fictional-importer": Importer()},
    )
    # The direct supplied importer exercises the same loop boundary as hosted invocation.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def scope(*_):
        yield api.importers

    api.importer_scope = scope
    plan = await api.plan(
        runtime.cid, source, mode="new", importer="fictional-importer", confirm_identity=True
    )
    journal = Journal.create(runtime.home, runtime.cid, "import/apply", runtime.clock)
    journal.runtime_owned = True
    binding = current.set(journal)
    try:
        imported = await api.apply(plan["id"])
    finally:
        current.reset(binding)
    assert imported and calls == ["detect", "read", "detect", "read"]
    assert journal.value["committed"] and journal.snapshot_verified()
    assert (await run_sync(memory, memory.get_raw, "e" * 32)).content == "fictional import"
