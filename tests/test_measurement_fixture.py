import random
import threading

import pytest
from tests.measurement_tools import load

from clytie.backup.coordination import run_sync
from clytie.backup.restore import MEMORY_TABLES

fixture = load("measurement_fixture")


@pytest.mark.parametrize("mode", ["legacy", "original", "candidate"])
async def test_legal_fixture_schema_business_and_thread_ownership(tmp_path, monkeypatch, mode):
    from clytie.memory.store import MemoryStore

    threads = []
    lifecycle = []
    original = MemoryStore._save
    original_init, original_close = MemoryStore.__init__, MemoryStore.close

    def created(store, *args, **kwargs):
        lifecycle.append(("created", threading.get_ident()))
        original_init(store, *args, **kwargs)

    def closed(store):
        lifecycle.append(("closed", threading.get_ident()))
        original_close(store)

    def save(*args, **kwargs):
        threads.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(MemoryStore, "_save", save)
    monkeypatch.setattr(MemoryStore, "__init__", created)
    monkeypatch.setattr(MemoryStore, "close", closed)
    owner, metrics = await fixture.setup(tmp_path / mode, "small_text", mode, 7, tiny=True)
    try:
        assert metrics["logical"]["records"] == 8
        assert metrics["logical"]["revisions"] == 8
        assert metrics["logical"]["content_bytes"] == 8 * 4096
        assert metrics["logical"]["content_length_distribution"] == {
            "min_bytes": 4096,
            "max_bytes": 4096,
            "total_bytes": 8 * 4096,
            "records": 8,
        }
        assert set(metrics["ledger_rows"]) == {
            "inbound",
            "record",
            "invocation",
            "intent",
            "attempt",
            "dedupe",
            "audit",
        }

        def inspect():
            tables = {
                row[0]
                for row in owner.store.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            return tables, owner.store.get_raw(f"{1:032x}").content

        tables, content = await run_sync(owner.store, inspect)
        assert tables == set(MEMORY_TABLES)
        assert content == random.Random(7).randbytes(2048).hex()
        expected = threading.get_ident() if mode == "legacy" else owner.storage_owner._thread.ident
        assert set(threads) == {expected}
        if mode != "legacy":
            with pytest.raises(RuntimeError, match="storage_requires_async_command"):
                _ = owner.store.db
        turn = await fixture.business(owner, 9)
        assert turn.finish == "stop" and turn.text == "Fictional offline response."
        assert await run_sync(owner.store, owner.store.meta, "benchmark_counter") == 9
        assert len(owner.model.requests) == 1
    finally:
        await fixture.close(owner)
    if mode != "legacy":
        assert not owner.storage_owner._thread.is_alive()
    assert lifecycle == [("created", expected), ("closed", expected)]


async def test_same_seed_has_same_logical_data_and_registered_assets(tmp_path):
    from clytie.backup.container import pack, unpack
    from clytie.backup.restore import validate_contents
    from clytie.backup.runtime_capture import capture_online
    from clytie.backup.source import capture_instance

    logical, records, assets = [], [], []
    for mode in ("legacy", "original", "candidate"):
        owner, metrics = await fixture.setup(tmp_path / mode, "large_asset", mode, 11, tiny=True)
        try:
            logical.append(metrics["logical"])
            records.append(
                await run_sync(
                    owner.store,
                    lambda: [
                        record.model_dump(mode="json") for record in owner.store.all_records()
                    ],
                )
            )
            snapshot = owner.host.asset_store.snapshot()
            assert len(snapshot.registrations) == 1
            ref = snapshot.registrations[0]
            assets.append((ref.asset_id, ref.size))
            assert ref.companion_id == "companion_a"
            owner.host.asset_store.verify_resolved(
                owner.host.asset_store.resolve(snapshot, "companion_a", ref.asset_id)
            )
            assert metrics["logical"]["asset_registrations"] == 1
            if mode != "candidate":
                continue

            async def online(cid, target, epoch):
                assert cid == owner.cid
                return await capture_online(owner, target, epoch)

            stage = tmp_path / "stage"
            captured = await capture_instance(
                owner.home,
                owner.clock,
                stage,
                source_instance_id="b" * 32,
                epoch="c" * 32,
                online=online,
                online_ids={owner.cid},
            )
            assert captured["manifest"]["completeness"] == "complete"
            package = tmp_path / "package"
            key = b"k" * 32
            pack(stage, package, key, captured["manifest"])
            restored = tmp_path / "restored"
            restored.mkdir()
            envelope = unpack(package, restored, key)
            validate_contents(restored, envelope)
        finally:
            await fixture.close(owner)
    assert logical[0] == logical[1] == logical[2]
    assert records[0] == records[1] == records[2]
    assert assets[0] == assets[1] == assets[2]

    def numeric_leaves(value):
        if isinstance(value, dict):
            return all(numeric_leaves(item) for item in value.values())
        return type(value) is int

    assert numeric_leaves(logical[0])


@pytest.mark.parametrize("mode", ["", "auto", "unknown"])
async def test_invalid_mode_never_creates_fixture(tmp_path, mode):
    with pytest.raises(ValueError, match="version_mode"):
        await fixture.setup(tmp_path / "unused", "small_text", mode, 1, tiny=True)
    assert not (tmp_path / "unused").exists()


async def test_seed_failure_releases_storage_owner(tmp_path, monkeypatch):
    from clytie.memory.store import MemoryStore
    from clytie.storage.owner import StorageOwner

    owners = []
    original = StorageOwner.start

    async def start(*args, **kwargs):
        owner = await original(*args, **kwargs)
        owners.append(owner)
        return owner

    def fail(*args, **kwargs):
        raise ValueError("fixture_seed_failure")

    monkeypatch.setattr(StorageOwner, "start", start)
    monkeypatch.setattr(MemoryStore, "_save", fail)
    with pytest.raises(ValueError, match="fixture_seed_failure"):
        await fixture.setup(tmp_path / "failed", "small_text", "candidate", 1, tiny=True)
    assert owners and not owners[0]._thread.is_alive()


async def test_explicit_product_root_cannot_mix_other_tree(tmp_path):
    with pytest.raises(ValueError, match="product_root_mismatch"):
        await fixture.setup(
            tmp_path / "unused",
            "small_text",
            "candidate",
            1,
            tiny=True,
            product_root=tmp_path,
        )
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize("mode", ["legacy", "candidate"])
async def test_failed_commit_does_not_call_model_and_rolls_back(tmp_path, monkeypatch, mode):
    from clytie.memory.store import MemoryStore

    owner, _ = await fixture.setup(tmp_path / mode, "small_text", mode, 1, tiny=True)
    original = MemoryStore.set_meta

    def fail(store, key, value):
        original(store, key, value)
        if key == "benchmark_counter":
            raise ValueError("fictional_write_failure")

    try:
        monkeypatch.setattr(MemoryStore, "set_meta", fail)
        with pytest.raises(ValueError, match="fictional_write_failure"):
            await fixture.business(owner, 0)
        assert not owner.model.requests
        assert await run_sync(owner.store, owner.store.meta, "benchmark_counter") is None
    finally:
        await fixture.close(owner)
