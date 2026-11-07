from clytie.backup.status import storage_categories, usage


def test_storage_categories_separate_real_snapshot_paths_and_failures(tmp_path):
    files = {
        "backups/plugin-upgrades/companion_a/plugin_a/old.enc": b"snapshot",
        "backups/plugin-upgrades/companion_a/plugin_a/failed-example/state": b"failed",
        "backups/ledger/companion_a/old/snapshot.enc": b"ledger",
        "backups/ledger/companion_a/failed-example/snapshot.enc": b"evidence",
        "backups/memory/companion_a/pre-migrate-example.enc": b"migration",
        "backups/memory/companion_a/pre-import-example.enc": b"import",
        "backups/memory/companion_a/migration-receipts.json": b"{}",
        "run/backup-work/example/partial": b"temporary",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    result = storage_categories(tmp_path)
    assert result["plugin_upgrade_snapshots"]["bytes"] == 8
    assert result["plugin_upgrade_failed_sites"]["bytes"] == 6
    assert result["ledger_snapshots"]["bytes"] == 6
    assert result["ledger_failed_sites"]["bytes"] == 8
    assert result["memory_migration_snapshots"]["bytes"] == 9
    assert result["memory_import_snapshots"]["bytes"] == 6
    assert result["memory_snapshot_metadata"]["bytes"] == 2
    assert result["temporary_backup_work"]["bytes"] == 9
    assert sum(row["bytes"] for row in result.values()) == sum(map(len, files.values()))


def test_usage_missing_is_empty_and_file_errors_are_unknown(tmp_path, monkeypatch):
    assert usage(tmp_path / "absent") == {"bytes": 0, "files": 0, "unknown": 0}
    path = tmp_path / "fictional"
    path.write_bytes(b"fake")
    original = type(path).lstat

    def denied(self):
        if self == path:
            raise PermissionError("fictional")
        return original(self)

    monkeypatch.setattr(type(path), "lstat", denied)
    assert usage(tmp_path) == {"bytes": 0, "files": 0, "unknown": 1}
