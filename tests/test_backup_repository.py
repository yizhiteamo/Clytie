import pytest

from clytie.backup.previews import PreviewCatalog
from clytie.sdk.testing.clock import MockClock


def test_external_source_requires_confirmation_and_protects_rotation(tmp_path):
    from clytie.backup.repository import RepositoryError, SourceRepository

    root = tmp_path / "source" / "backups" / "regular"
    root.mkdir(parents=True)
    source = root / "fictional.backup"
    source.write_bytes(b"fictional")
    clock = MockClock()
    repo = SourceRepository.initialize(root, clock, owner_instance_id="source_instance")
    repo.register({"backup_id": "backup_a", "path": str(source), "identity": repo.identity(source)})
    binding = {"root": str(root), "repository_id": repo.repository_id, "backup_id": "backup_a"}
    catalog = PreviewCatalog(tmp_path / "maintenance", clock)
    with pytest.raises(RepositoryError, match="source_repository_confirmation_required"):
        catalog.bind_source(source, binding, confirmed=False)
    catalog.bind_source(source, binding, confirmed=True)
    catalog.reference(source, "generation_a", kind="generation")
    with repo.deletion(source) as allowed:
        assert not allowed
    plan = catalog.publish(
        "instance", "a" * 32, source, "generation_a", target="target", fingerprint="hash"
    )
    assert plan["source_repository"]["repository_id"] == repo.repository_id
    catalog = PreviewCatalog(tmp_path / "maintenance", clock)
    assert catalog.get("instance", "a" * 32)["state"] == "ready"
    catalog.cancel("instance", "a" * 32)
    with repo.deletion(source) as allowed:
        assert allowed


def test_storage_migrates_legacy_reference_before_rotation(tmp_path):
    from clytie.backup.previews import _LocalCatalog
    from clytie.backup.repository import SourceRepository
    from clytie.backup.restore import identity
    from clytie.backup.storage import BackupStorage
    from clytie.plugin_host.snapshots import atomic_write, canonical

    clock = MockClock()
    source = tmp_path / "backups/regular/source.backup"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"fictional")
    legacy = _LocalCatalog(tmp_path, clock)
    legacy.reference(source, "generation", kind="generation")
    legacy.publish("instance", "b" * 32, source, "generation", target="target", fingerprint="hash")
    atomic_write(
        tmp_path / "supervisor/backup-index.json",
        canonical(
            {
                "schema_version": 1,
                "records": {
                    "backup_a": {
                        "backup_id": "backup_a",
                        "path": str(source),
                        "identity": identity(source),
                    }
                },
                "deletions": [],
            }
        ),
    )
    storage = BackupStorage(tmp_path, clock)
    assert storage.path.parent == source.parent
    repo = SourceRepository(source.parent, clock)
    with repo.deletion(source) as allowed:
        assert not allowed
    PreviewCatalog(tmp_path, clock).cancel("instance", "b" * 32)
    with repo.deletion(source) as allowed:
        assert allowed


def _repository(tmp_path):
    from clytie.backup.repository import SourceRepository

    root = tmp_path / "repository"
    root.mkdir()
    source = root / "fictional.backup"
    source.write_bytes(b"fictional")
    clock = MockClock()
    repo = SourceRepository.initialize(root, clock, owner_instance_id="source_instance")
    repo.register({"backup_id": "backup_a", "path": str(source), "identity": repo.identity(source)})
    binding = {"root": str(root), "repository_id": repo.repository_id, "backup_id": "backup_a"}
    return repo, source, clock, binding


def test_two_holders_same_operation_ids_cannot_release_each_other(tmp_path):
    repo, source, clock, binding = _repository(tmp_path)
    first, second = [PreviewCatalog(tmp_path / name, clock) for name in ("first", "second")]
    for catalog in (first, second):
        catalog.bind_source(source, binding, confirmed=True)
        catalog.reference(source, "shared_operation", kind="execution")
    first.release("shared_operation")
    assert len(repo.catalog(None).holders(source)) == 1
    with repo.deletion(source) as allowed:
        assert not allowed
    second.release("shared_operation")
    with repo.deletion(source) as allowed:
        assert allowed


def test_missing_authority_and_wrong_repository_never_read_content(tmp_path, monkeypatch):
    from clytie.backup.repository import REFERENCES, RepositoryError

    repo, source, clock, binding = _repository(tmp_path)
    catalog = PreviewCatalog(tmp_path / "maintenance", clock)
    with pytest.raises(RepositoryError, match="identity_mismatch"):
        catalog.bind_source(source, {**binding, "repository_id": "wrong"}, confirmed=True)
    catalog.bind_source(source, binding, confirmed=True)
    (repo.root / REFERENCES).unlink()
    from clytie.backup.previews import PreviewError

    with pytest.raises(PreviewError, match="recovery_blocked"):
        catalog.reference(source, "generation", kind="generation")
    with pytest.raises(PreviewError, match="recovery_blocked"):
        with repo.deletion(source):
            pytest.fail("missing source authority is not empty")


def test_source_expiry_preserves_accepted_execution_after_holder_restart(tmp_path):
    repo, source, clock, binding = _repository(tmp_path)
    catalog = PreviewCatalog(tmp_path / "maintenance", clock)
    catalog.bind_source(source, binding, confirmed=True)
    catalog.reference(source, "generation", kind="generation")
    catalog.publish("instance", "c" * 32, source, "generation", target="target", fingerprint="hash")
    catalog.accept("instance", "c" * 32, "operation")
    clock.advance(48 * 3600)
    with repo.deletion(source) as allowed:
        assert not allowed
    restarted = PreviewCatalog(tmp_path / "maintenance", clock)
    assert restarted.for_operation("operation")[0]["state"] == "consumed"
    restarted.release("operation")
    with repo.deletion(source) as allowed:
        assert allowed


EXTERNAL_CHILD = """
import json,sys
from pathlib import Path
from clytie.backup.previews import PreviewCatalog, PreviewError
from clytie.backup.repository import SourceRepository
from clytie.sdk.testing.clock import MockClock
home,root,action = Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
clock = MockClock()
catalog = PreviewCatalog(home,clock)
print('ready',flush=True)
sys.stdin.readline()
if action == 'apply':
    try:
        result = catalog.accept('instance','d'*32,'accepted_operation')
        print(json.dumps({'accepted':result[0]}),flush=True)
    except (PreviewError,ValueError) as exc:
        print(json.dumps({'error':str(exc)}),flush=True)
elif action == 'generation':
    catalog.reference(root/'fictional.backup','generation',kind='generation')
    print('protected',flush=True)
    sys.stdin.readline()
else:
    clock.advance(24*3600)
    repo = SourceRepository(root,clock)
    with repo.deletion(root/'fictional.backup') as allowed:
        if allowed:
            (root/'fictional.backup').unlink()
        print(json.dumps({'deleted':allowed}),flush=True)
"""


def _external_child(home, root, action):
    import subprocess
    import sys

    child = subprocess.Popen(
        [sys.executable, "-c", EXTERNAL_CHILD, str(home), str(root), action],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout.readline().strip() == "ready"
    return child


def test_real_cross_home_process_apply_and_source_expiry_delete_race(tmp_path):
    import json

    repo, source, clock, binding = _repository(tmp_path)
    catalog = PreviewCatalog(tmp_path / "maintenance", clock)
    catalog.bind_source(source, binding, confirmed=True)
    catalog.reference(source, "generation", kind="generation")
    catalog.publish("instance", "d" * 32, source, "generation", target="target", fingerprint="hash")
    children = [_external_child(catalog.home, repo.root, action) for action in ("apply", "delete")]
    try:
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
        results = []
        for child in children:
            output, error = child.communicate(timeout=20)
            assert child.returncode == 0, error
            results.append(json.loads(output))
        accepted, deleted = results
        assert ("accepted" in accepted) != deleted["deleted"]
        if "accepted" in accepted:
            assert source.exists()
            assert repo.catalog(None).holders(source)[0]["kind"] == "execution"
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)


def test_killed_generation_remains_protected_after_source_restart(tmp_path):
    from clytie.backup.repository import SourceRepository

    repo, source, clock, binding = _repository(tmp_path)
    catalog = PreviewCatalog(tmp_path / "maintenance", clock)
    catalog.bind_source(source, binding, confirmed=True)
    child = _external_child(catalog.home, repo.root, "generation")
    try:
        child.stdin.write("go\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "protected"
        child.kill()
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
    clock.advance(48 * 3600)
    with SourceRepository(repo.root, clock).deletion(source) as allowed:
        assert not allowed
    refs = repo.catalog(None).holders(source)
    assert refs[0]["reason"] == "recovery_unverified"


@pytest.mark.parametrize("stage", ["generation", "publication", "acceptance"])
def test_lost_response_and_cross_file_commit_recovery(tmp_path, monkeypatch, stage):
    import clytie.backup.previews as module

    repo, source, clock, binding = _repository(tmp_path)
    catalog = PreviewCatalog(tmp_path / "maintenance", clock)
    catalog.bind_source(source, binding, confirmed=True)
    original_write = module.atomic_write
    fired = False

    def lost(path, content, **kwargs):
        nonlocal fired
        original_write(path, content, **kwargs)
        if not fired and Path(path).name == ".clytie-references.json":
            import json

            data = json.loads(content)
            match = (
                (stage == "generation" and bool(data["references"]))
                or (stage == "publication" and bool(data["previews"]))
                or (
                    stage == "acceptance"
                    and any(p["state"] == "consumed" for p in data["previews"].values())
                )
            )
            if match:
                fired = True
                raise OSError("fictional lost response")

    from pathlib import Path

    if stage != "generation":
        catalog.reference(source, "generation", kind="generation")
    if stage == "acceptance":
        catalog.publish(
            "instance", "e" * 32, source, "generation", target="target", fingerprint="hash"
        )
    monkeypatch.setattr(module, "atomic_write", lost)
    with pytest.raises(OSError, match="lost response"):
        if stage == "generation":
            catalog.reference(source, "generation", kind="generation")
        elif stage == "publication":
            catalog.publish(
                "instance", "e" * 32, source, "generation", target="target", fingerprint="hash"
            )
        else:
            catalog.accept("instance", "e" * 32, "accepted_operation")
    monkeypatch.setattr(module, "atomic_write", original_write)
    restarted = PreviewCatalog(catalog.home, clock)
    with repo.deletion(source) as allowed:
        assert not allowed
    if stage == "generation":
        restarted.reference(source, "generation", kind="generation")
        assert len(repo.catalog(None).holders(source)) == 1
    elif stage == "publication":
        assert (
            restarted.publish(
                "instance", "e" * 32, source, "generation", target="target", fingerprint="hash"
            )["state"]
            == "ready"
        )
    else:
        assert restarted.for_operation("accepted_operation")[0]["state"] == "consumed"
        assert restarted.accept("instance", "e" * 32, "new_operation") == (
            "accepted_operation",
            False,
        )


def test_retirement_blocks_new_readers_and_never_overrides_active_reference(tmp_path):
    from clytie.backup.previews import PreviewError
    from clytie.backup.repository import RepositoryError

    repo, source, clock, binding = _repository(tmp_path)
    catalog = PreviewCatalog(tmp_path / "maintenance", clock)
    catalog.bind_source(source, binding, confirmed=True)
    catalog.reference(source, "reader")
    with pytest.raises(RepositoryError, match="recovery_blocked"):
        repo.retire("cleanup")
    catalog.release("reader")
    repo.retire("cleanup")
    with pytest.raises(PreviewError, match="retiring"):
        catalog.reference(source, "new_reader")
    repo.unretire("cleanup")
    catalog.reference(source, "new_reader")


def test_hardlinked_file_is_not_an_independent_physical_source(tmp_path):
    import os

    from clytie.backup.repository import RepositoryError

    repo, source, clock, binding = _repository(tmp_path)
    os.link(source, tmp_path / "alias")
    with pytest.raises(RepositoryError, match="identity_mismatch"):
        PreviewCatalog(tmp_path / "maintenance", clock).bind_source(source, binding, confirmed=True)


def test_release_outage_is_persisted_and_status_never_reports_unknown_as_empty(
    tmp_path, monkeypatch
):
    from clytie.backup.repository import MARKER

    repo, source, clock, binding = _repository(tmp_path)
    catalog = PreviewCatalog(tmp_path / "maintenance", clock)
    catalog.bind_source(source, binding, confirmed=True)
    catalog.reference(source, "execution", kind="execution")
    marker = (repo.root / MARKER).read_bytes()
    (repo.root / MARKER).unlink()
    assert catalog.release("execution") is False
    status = catalog.status()
    assert status["pending_release"][0]["operation_id"] == "execution"
    assert status["repositories"][0]["state"] == "unavailable"
    assert status["repositories"][0]["references"] is None
    (repo.root / MARKER).write_bytes(marker)
    assert catalog.release("execution") is True
    assert catalog.status()["pending_release"] == []
    with repo.deletion(source) as allowed:
        assert allowed


def test_external_unbound_restore_refuses_before_key_or_content_read(tmp_path):
    from clytie.backup.restore import RestoreError, RestoreManager

    package = tmp_path / "fictional.backup"
    package.write_bytes(b"not a readable encrypted package")
    manager = RestoreManager(tmp_path / "maintenance", MockClock())
    with pytest.raises(RestoreError, match="source_repository_binding_required"):
        manager.plan(package, tmp_path / "missing_key", tmp_path / "target", tmp_path / "workspace")
    assert not (tmp_path / "workspace").exists()


def test_consumed_retry_returns_original_operation_after_source_rotation(tmp_path):
    repo, source, clock, binding = _repository(tmp_path)
    catalog = PreviewCatalog(tmp_path / "maintenance", clock)
    catalog.bind_source(source, binding, confirmed=True)
    catalog.reference(source, "generation", kind="generation")
    catalog.publish("instance", "f" * 32, source, "generation", target="target", fingerprint="hash")
    assert catalog.accept("instance", "f" * 32, "execution") == ("execution", True)
    catalog.release("execution")
    with repo.deletion(source) as allowed:
        assert allowed
        source.unlink()
    assert catalog.accept("instance", "f" * 32, "new_execution") == ("execution", False)


def test_migration_interrupted_after_source_copy_remains_protected_and_restarts(
    tmp_path, monkeypatch
):
    from contextlib import contextmanager

    from clytie.backup.previews import _LocalCatalog
    from clytie.backup.repository import SourceRepository
    from clytie.backup.storage import BackupStorage
    from clytie.plugin_host.snapshots import atomic_write, canonical

    clock = MockClock()
    source = tmp_path / "backups/regular/source.backup"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"fictional")
    legacy = _LocalCatalog(tmp_path, clock)
    legacy.reference(source, "reader", kind="reader")
    atomic_write(
        tmp_path / "supervisor/backup-index.json",
        canonical(
            {
                "schema_version": 1,
                "records": {
                    "backup_a": {
                        "backup_id": "backup_a",
                        "path": str(source),
                        "identity": SourceRepository.identity(source),
                    }
                },
                "deletions": [],
            }
        ),
    )
    original = PreviewCatalog._routes

    @contextmanager
    def interrupted(self):
        raise OSError("fictional publication interruption")
        yield

    monkeypatch.setattr(PreviewCatalog, "_routes", interrupted)
    with pytest.raises(OSError, match="publication interruption"):
        BackupStorage(tmp_path, clock)
    repository = SourceRepository(source.parent, clock)
    assert repository.marker["migration_complete"] is False
    with repository.deletion(source) as allowed:
        assert not allowed
    monkeypatch.setattr(PreviewCatalog, "_routes", original)
    storage = BackupStorage(tmp_path, clock)
    assert storage.repository.marker["migration_complete"] is True
    assert len(storage.repository.catalog(None).holders(source)) == 1
    PreviewCatalog(tmp_path, clock).release("reader")
    with storage.repository.deletion(source) as allowed:
        assert allowed


CRASH_CHILD = """
import json,sys
from pathlib import Path
import clytie.backup.previews as module
from clytie.sdk.testing.clock import MockClock
catalog=module.PreviewCatalog(Path(sys.argv[1]),MockClock())
source,stage=Path(sys.argv[2]),sys.argv[3]
original=module.atomic_write
def checkpoint(path,content,**kwargs):
    original(path,content,**kwargs)
    if Path(path).name != '.clytie-references.json': return
    data=json.loads(content)
    states={p['state'] for p in data['previews'].values()}
    if (stage=='publish' and 'ready' in states) or (stage=='accept' and 'consumed' in states):
        print('source_committed',flush=True)
        sys.stdin.readline()
module.atomic_write=checkpoint
print('ready',flush=True)
sys.stdin.readline()
if stage=='publish':
    catalog.publish('instance','a1'*16,source,'generation',target='target',fingerprint='hash')
else:
    catalog.accept('instance','a1'*16,'execution')
"""


@pytest.mark.parametrize("stage", ["publish", "accept"])
def test_real_kill_between_source_commit_and_local_receipt(tmp_path, stage):
    import subprocess
    import sys

    repo, source, clock, binding = _repository(tmp_path)
    catalog = PreviewCatalog(tmp_path / "maintenance", clock)
    catalog.bind_source(source, binding, confirmed=True)
    catalog.reference(source, "generation", kind="generation")
    if stage == "accept":
        catalog.publish(
            "instance", "a1" * 16, source, "generation", target="target", fingerprint="hash"
        )
    child = subprocess.Popen(
        [sys.executable, "-c", CRASH_CHILD, str(catalog.home), str(source), stage],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        child.stdin.write("go\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "source_committed"
        child.kill()
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
    restarted = PreviewCatalog(catalog.home, clock)
    with repo.deletion(source) as allowed:
        assert not allowed
    if stage == "publish":
        assert restarted.get("instance", "a1" * 16)["state"] == "ready"
        assert (
            restarted.publish(
                "instance", "a1" * 16, source, "generation", target="target", fingerprint="hash"
            )["state"]
            == "ready"
        )
    else:
        assert restarted.for_operation("execution")[0]["state"] == "consumed"
        assert restarted.accept("instance", "a1" * 16, "another") == ("execution", False)


def test_pending_deletion_receipt_refuses_new_source_reference(tmp_path):
    from clytie.backup.repository import INDEX, RepositoryError
    from clytie.plugin_host.snapshots import atomic_write, canonical

    repo, source, clock, binding = _repository(tmp_path)
    data = repo.index()
    data["deletions"].append({"backup_id": "backup_a", "state": "deleting"})
    atomic_write(repo.root / INDEX, canonical(data))
    with pytest.raises(RepositoryError, match="source_repository_retiring"):
        PreviewCatalog(tmp_path / "maintenance", clock).bind_source(source, binding, confirmed=True)
