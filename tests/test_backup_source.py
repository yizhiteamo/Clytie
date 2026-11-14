import hashlib
import json
import shutil
from pathlib import Path

import pytest

from clytie.backup.restore import validate_contents
from clytie.backup.source import capture_instance
from clytie.kernel.ledger import Ledger
from clytie.memory.store import MemoryStore
from clytie.sdk.testing import MockClock

ROOT = Path(__file__).resolve().parents[1]


def test_capture_rejects_undeclared_shared_file(tmp_path):
    from clytie.backup.source import SourceError, _accept, _entry

    work, stage = tmp_path / "work", tmp_path / "stage"
    path = work / "shared/fictional.txt"
    path.parent.mkdir(parents=True)
    path.write_text("fictional", encoding="utf-8")
    captured = {"files": [_entry(work, "shared/fictional.txt")], "assets": []}
    with pytest.raises(SourceError, match="snapshot_inconsistent"):
        _accept(captured, work, stage, {}, {}, "instance", "companion_a")
    assert not stage.exists()


def fictional(tmp_path):
    home = tmp_path / "home"
    shutil.copytree(ROOT / "examples/home", home)
    clock = MockClock()
    memory = MemoryStore(
        home / "companions/companion_a/memory/memory.sqlite3", clock=clock, config={}, instance={}
    )
    with memory.transaction():
        memory.set_meta("companion_uid", "a" * 32)
        memory.db.execute(
            "INSERT INTO recollect_progress VALUES('*', ?)",
            (json.dumps({"inbound": 0, "intent": 0}),),
        )
    memory.close()
    ledger = Ledger(
        (home / "companions/companion_a/ledger/ledger.sqlite3").resolve(), "companion_a", clock
    )
    ledger.close()
    versions = {}
    for pid in ("fake-model", "fake-channel"):
        manifest = json.loads((ROOT / "plugins" / pid / "plugin.json").read_bytes())
        versions[pid] = {
            "plugin_version": manifest["version"],
            "data_version": manifest.get("data", {}).get("version", 1),
        }
    (home / "companions/companion_a/plugin-versions.json").write_text(json.dumps(versions))
    return home, clock


async def test_offline_complete_source_passes_restore_validation(tmp_path):
    home, clock = fictional(tmp_path)
    result = await capture_instance(
        home,
        clock,
        tmp_path / "stage",
        scope="instance",
        epoch="test",
        source_instance_id="fictional-instance",
    )
    assert result["manifest"]["completeness"] == "complete", result["missing"]
    validate_contents(tmp_path / "stage", result, repo_root=ROOT)
    assert all(not f["path"].endswith(("-wal", "-shm", ".lock")) for f in result["files"])


async def test_online_timeout_never_falls_back_to_offline(tmp_path):
    home, clock = fictional(tmp_path)

    async def unavailable(cid, destination, epoch):
        raise TimeoutError()

    result = await capture_instance(
        home,
        clock,
        tmp_path / "stage",
        scope="instance",
        epoch="test",
        source_instance_id="fictional-instance",
        online=unavailable,
        online_ids={"companion_a"},
    )
    assert result["manifest"]["completeness"] == "partial"
    assert not (tmp_path / "stage/companions/companion_a/memory/memory.sqlite3").exists()


async def test_memory_source_excludes_progress_and_unrelated_files(tmp_path):
    import sqlite3

    home, clock = fictional(tmp_path)
    (home / "secrets").mkdir(exist_ok=True)
    (home / "secrets/fictional.key").write_bytes(b"fictional key")
    result = await capture_instance(
        home,
        clock,
        tmp_path / "stage",
        scope="memory",
        cid="companion_a",
        epoch="test",
        source_instance_id="fictional-instance",
    )
    assert result["manifest"]["completeness"] == "complete"
    assert len(result["files"]) == 1
    path = tmp_path / "stage" / result["files"][0]["path"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM recollect_progress").fetchone()[0] == 0
    assert hashlib.sha256(path.read_bytes()).hexdigest() == result["files"][0]["sha256"]


async def test_source_real_encrypted_package_restores_to_new_target(tmp_path):
    from clytie.backup.container import pack
    from clytie.backup.repository import SourceRepository
    from clytie.backup.restore import RestoreManager

    home, clock = fictional(tmp_path)
    from clytie.backup.old_instances import OldInstances
    from clytie.supervisor.common import Audit

    audit = Audit(home, clock, rotate_bytes=30)
    audit.append("fictional_before_backup")
    OldInstances(home, clock).register(
        dict(machine_id="foreign-fictional-machine", home="unavailable", identity={}),
        source_instance_id="prior_source",
        target_instance_id="fictional-instance",
        operation_id="prior_switch",
    )
    stage = tmp_path / "stage"
    captured = await capture_instance(
        home,
        clock,
        stage,
        scope="instance",
        epoch="test",
        source_instance_id="fictional-instance",
        audit_writers=(audit.writer,),
    )
    (tmp_path / "packages").mkdir()
    package = tmp_path / "packages/source.clytie-backup"
    key = tmp_path / "fictional.key"
    key.write_bytes(b"k" * 32)
    pack(stage, package, key.read_bytes(), captured["manifest"])
    manager = RestoreManager(tmp_path / "management", clock, reserve_bytes=0)
    repository = SourceRepository.initialize(
        package.parent, clock, owner_instance_id="fictional_source"
    )
    repository.register(
        {
            "backup_id": "fictional_backup",
            "path": str(package),
            "identity": repository.identity(package),
        }
    )
    manager.previews.bind_source(
        package,
        {
            "root": str(package.parent),
            "repository_id": repository.repository_id,
            "backup_id": "fictional_backup",
        },
        confirmed=True,
    )
    workspace = tmp_path / "restore-work"
    workspace.mkdir()
    target = tmp_path / "restored"
    result = manager.apply(manager.plan(package, key, target, workspace)["id"])
    assert result["data_state"] == "ready_for_switch"
    assert (target / "companions/companion_a/memory/memory.sqlite3").is_file()
    assert not (target / "secrets/backup.key").exists()
    restored = Audit(target, clock)
    restored.append("fictional_after_restore")
    rows = restored.writer.seal()
    events = [
        json.loads(line)["operation"]
        for row in rows
        for line in (target / row["path"]).read_bytes().splitlines()
    ]
    assert events == ["fictional_before_backup", "fictional_after_restore"]
    assert OldInstances(target, clock).status()["count"] == 1
    assert OldInstances(target, clock).status()["unknown"] == 1


async def test_config_changes_during_capture_are_not_complete(tmp_path):
    home, clock = fictional(tmp_path)
    changed = []

    def change_config():
        if not changed:
            changed.append(True)
            path = home / "clytie.local.yaml"
            path.write_bytes(path.read_bytes() + b"\n# fictional concurrent edit\n")

    captured = await capture_instance(
        home,
        clock,
        tmp_path / "stage",
        scope="instance",
        epoch="test",
        source_instance_id="fictional-instance",
        check=change_config,
    )
    assert captured["manifest"]["completeness"] == "partial"
    assert {"component": "configuration", "reason": "snapshot_inconsistent"} in captured["missing"]


async def test_failed_component_material_is_kept_outside_package_root(tmp_path):
    home, clock = fictional(tmp_path)
    counter = []

    def fail():
        counter.append(1)
        if len(counter) == 3:
            raise OSError("fictional copy failure")

    captured = await capture_instance(
        home,
        clock,
        tmp_path / "stage",
        scope="instance",
        epoch="test",
        source_instance_id="fictional-instance",
        check=fail,
    )
    assert captured["manifest"]["completeness"] == "partial"
    registered = {entry["path"] for entry in captured["files"]}
    actual = {
        p.relative_to(tmp_path / "stage").as_posix()
        for p in (tmp_path / "stage").rglob("*")
        if p.is_file()
    }
    assert actual == registered
    assert captured["temporary_directories"]
