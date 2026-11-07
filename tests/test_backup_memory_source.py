import hashlib
import json
import secrets

import pytest

from clytie.backup.container import pack
from clytie.backup.memory_source import BackupMemorySource
from clytie.memory.transfer import Transfers

from .test_memory_transfer import homes as homes
from .test_memory_transfer import remember, revise


def backup(store, home, tmp_path, *, scope="memory", complete=True, assets=None, extra_file=False):
    stage = tmp_path / "stage"
    stage.mkdir()
    with store.transaction():
        store.db.execute("INSERT OR REPLACE INTO recollect_progress VALUES('*', '{}')")
    import sqlite3

    db = sqlite3.connect(stage / "memory.sqlite3")
    store.db.backup(db)
    db.close()
    key = secrets.token_bytes(32)
    key_path = home / "secrets/backup.key"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key)
    source = tmp_path / "memory.enc"
    manifest = {
        "schema_version": 1,
        "scope": scope,
        "completeness": "complete" if complete else "partial",
        "code_version": "0.9.0",
        "companions": [{"id": "companion_a", "uid": store.uid, "memory": "memory.sqlite3"}],
    }
    if assets is not None:
        manifest["assets"] = assets
    if extra_file:
        (stage / "fictional-asset.bin").write_bytes(b"fictional")
    pack(stage, source, key, manifest)
    return source, {"backup_id": "a" * 32, "key_fingerprint": hashlib.sha256(key).hexdigest()}


async def test_backup_adapter_reuses_native_new_merge_and_undo_without_progress(homes, tmp_path):
    original, _, _ = homes("original")
    record = revise(original, remember(original, explicit=True), "虚构的修订经历")
    target, target_home, _ = homes("target")
    source, binding = backup(original, target_home, tmp_path)
    adapter = BackupMemorySource(target_home, target.clock, "companion_a", binding)
    transfers = Transfers(target, target_home, "companion_a", source_adapter=adapter)
    plan = await transfers.plan(source, mode="new", confirm_identity=True)
    saved = json.loads(transfers.plan_path(plan["id"]).read_bytes())
    assert saved["source_type"] == "backup"
    assert record.content not in json.dumps(saved, ensure_ascii=False)
    imported = await transfers.apply(plan["id"])
    assert target.get_raw(record.record_id) == original.get_raw(record.record_id)
    assert not target.db.execute("SELECT 1 FROM recollect_progress").fetchone()
    merged = await transfers.plan(source, mode="merge")
    assert all(row["action"] == "skip" for row in merged["records"])
    # The original undo engine, without the backup adapter or a new lifetime rule.
    ordinary = Transfers(target, target_home, "companion_a")
    undo = ordinary.undo(imported)
    await ordinary.apply(undo["id"])
    assert target.get_raw(record.record_id).retracted


async def test_external_memory_source_uses_explicit_key_and_source_protection(homes, tmp_path):
    from clytie.backup.previews import PreviewCatalog
    from clytie.backup.repository import SourceRepository

    original, source_home, _ = homes("external_original")
    remember(original, explicit=True)
    target, target_home, _ = homes("external_target")
    package, binding = backup(original, source_home, tmp_path)
    root = source_home / "backups/regular"
    root.mkdir(parents=True)
    source = root / "fictional.clytie-backup"
    package.replace(source)
    repository = SourceRepository.initialize(
        root, target.clock, owner_instance_id="source_instance"
    )
    repository.register(
        {
            "backup_id": binding["backup_id"],
            "path": str(source),
            "identity": repository.identity(source),
        }
    )
    # An explicitly supplied external key, never a key inferred from a source home.
    key_path = tmp_path / "explicit-key"
    key_path.write_bytes((source_home / "secrets/backup.key").read_bytes())
    binding.update(
        key_path=str(key_path),
        source_repository={
            "root": str(root),
            "repository_id": repository.repository_id,
            "backup_id": binding["backup_id"],
        },
    )
    transfers = Transfers(
        target,
        target_home,
        "companion_a",
        source_adapter=BackupMemorySource(
            target_home,
            target.clock,
            "companion_a",
            binding,
        ),
    )
    plan = await transfers.plan(source, mode="new", confirm_identity=True)
    assert not (target_home / "secrets/backup.key").exists()
    with repository.deletion(source) as permitted:
        assert not permitted
    PreviewCatalog(target_home, target.clock).cancel("memory", plan["id"])
    assert source.is_file()
    with repository.deletion(source) as permitted:
        assert permitted


@pytest.mark.parametrize("scope,complete", [("instance", True), ("memory", False)])
async def test_adapter_rejects_non_memory_or_partial_packages(homes, tmp_path, scope, complete):
    original, _, _ = homes("original")
    remember(original)
    target, home, _ = homes("target")
    source, binding = backup(original, home, tmp_path, scope=scope, complete=complete)
    transfers = Transfers(
        target,
        home,
        "companion_a",
        source_adapter=BackupMemorySource(home, target.clock, "companion_a", binding),
    )
    with pytest.raises(ValueError, match="backup_memory_source_invalid"):
        await transfers.plan(source, mode="new")


@pytest.mark.parametrize("extra_file", [False, True])
async def test_memory_backup_rejects_reserved_asset_payload(homes, tmp_path, extra_file):
    original, _, _ = homes("original")
    remember(original)
    target, home, _ = homes("target")
    source, binding = backup(
        original,
        home,
        tmp_path,
        assets=[] if extra_file else [{"asset_id": "a" * 64}],
        extra_file=extra_file,
    )
    adapter = BackupMemorySource(home, target.clock, "companion_a", binding)
    with pytest.raises(ValueError, match="backup_memory_assets_unsupported"):
        with adapter.open(source):
            pytest.fail("unsupported payload must not reach import")
    assert target.all_records() == []
