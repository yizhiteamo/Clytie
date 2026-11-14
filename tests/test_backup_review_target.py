"""真实虚构源捕获与加密恢复的源/目标边界回归。"""

from pathlib import Path

import pytest

from clytie.backup.container import pack
from clytie.backup.repository import SourceRepository
from clytie.backup.restore import RestoreError, RestoreManager, machine_id
from clytie.backup.source import capture_instance

from .test_backup_source import ROOT, fictional


async def captured_package(tmp_path):
    home, clock = fictional(tmp_path)
    stage = tmp_path / "stage"
    result = await capture_instance(
        home, clock, stage, scope="instance", epoch="fictional", source_instance_id="fictional"
    )
    assert result["manifest"]["completeness"] == "complete", result["missing"]
    directory = home / "backups/regular"
    directory.mkdir(parents=True)
    package = directory / "fictional.clytie-backup"
    key = tmp_path / "fictional-key"
    key.write_bytes(b"k" * 32)
    pack(stage, package, key.read_bytes(), result["manifest"])
    repository = SourceRepository.initialize(directory, clock, owner_instance_id="fictional")
    repository.register(
        dict(backup_id="fictional", path=str(package), identity=repository.identity(package))
    )
    manager = RestoreManager(tmp_path / "maintenance", clock, repo_root=ROOT, reserve_bytes=0)
    manager.previews.bind_source(
        package,
        dict(
            root=str(repository.root), repository_id=repository.repository_id, backup_id="fictional"
        ),
        confirmed=True,
    )
    return home, manager, package, key


@pytest.mark.parametrize(
    "relative", ["restored-child", "companions/restored-child", "backups/other"]
)
async def test_restore_preview_rejects_target_inside_authenticated_source(tmp_path, relative):
    home, manager, package, key = await captured_package(tmp_path)
    target = home / relative
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    before = {p.relative_to(home) for p in home.rglob("*") if p.is_file()}
    with pytest.raises(RestoreError, match="restore_target_invalid"):
        manager.plan(package, key, target, workspace)
    assert not target.exists()
    assert not list(manager.directory.glob("*.json"))
    # Protection receipts may change; the source's business configuration stays intact.
    assert (home / "companions/companion_a/companion.yaml").is_file()
    assert Path("clytie.local.yaml") in before


async def test_restore_separate_target_remains_allowed(tmp_path):
    home, manager, package, key = await captured_package(tmp_path)
    target, workspace = tmp_path / "restored", tmp_path / "workspace"
    workspace.mkdir()
    plan = manager.plan(package, key, target, workspace)
    state = manager.apply(plan["id"])
    assert state["data_state"] == "ready_for_switch"
    assert (home / "companions/companion_a/companion.yaml").is_file()
    assert (target / "companions/companion_a/companion.yaml").read_bytes() == (
        workspace / "companions/companion_a/companion.yaml"
    ).read_bytes()


@pytest.mark.parametrize("entry", ["admit", "apply", "recover"])
async def test_legacy_preview_cannot_bypass_source_boundary(tmp_path, monkeypatch, entry):
    home, manager, package, key = await captured_package(tmp_path)
    target, workspace = home / "nested-restore", tmp_path / "workspace"
    workspace.mkdir()
    original = manager._target_relation
    # Simulate a preview issued before the source boundary fix. Its package,
    # fingerprint and protection remain real; no execution has been accepted.
    with monkeypatch.context() as old:
        old.setattr(
            manager,
            "_target_relation",
            lambda target, source, envelope=None: original(target, source),
        )
        plan = manager.plan(package, key, target, workspace)
    with pytest.raises(RestoreError, match="restore_target_invalid"):
        if entry == "admit":
            manager.admit(plan["id"], "fictional-operation")
        else:
            getattr(manager, entry)(plan["id"])
    assert not target.exists()
    assert not list(home.glob(".clytie-restore-*.lock"))


def test_historical_source_path_is_deny_only_without_filesystem_access(tmp_path, monkeypatch):
    from clytie.sdk.testing import MockClock

    manager = RestoreManager(tmp_path / "maintenance", MockClock())
    recorded = tmp_path / "unavailable-source"
    assert not recorded.exists()
    target = tmp_path / "allowed"
    source = tmp_path / "packages/fictional.clytie-backup"
    original_stat, original_resolve = Path.stat, Path.resolve

    def stat(path, *args, **kwargs):
        assert path != recorded, "historical path must not grant discovery access"
        return original_stat(path, *args, **kwargs)

    def resolve(path, *args, **kwargs):
        assert path != recorded, "historical path must not be resolved"
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "resolve", resolve)
    envelope = dict(
        manifest=dict(source_registration=dict(machine_id=machine_id(), home=str(recorded)))
    )
    assert manager._target_relation(target, source, envelope) == target
