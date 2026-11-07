import hashlib
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from clytie.backup.mirror import MirrorError, MirrorManager, StorageIdentity, latest_per_group
from clytie.backup.retention import BackupRecord
from clytie.sdk.testing import MockClock


def setup(tmp_path):
    primary, target = tmp_path / "primary", tmp_path / "mirror"
    primary.mkdir()
    target.mkdir()
    identities = {}

    def probe(path):
        path = path.resolve()
        disk = identities.get(str(path), "primary" if path == primary else "secondary")
        return StorageIdentity(str(path), str(path.stat().st_ino), "volume-" + disk, (disk,))

    manager = MirrorManager(
        tmp_path / "state", primary, MockClock(), owner_id="fictional_owner", probe=probe
    )
    return manager, primary, target, identities


def source(primary, name="backup_a", group="companion_a", scope="memory"):
    path = primary / (name + ".enc")
    path.write_bytes(b"fictional authenticated package")
    record = BackupRecord(
        name,
        str(path),
        scope,
        group,
        datetime(2000, 1, 1, tzinfo=timezone.utc),
        path.stat().st_size,
    )
    return record, hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def reference(record):
    yield


def test_confirm_copy_receipt_and_activate_new_repository(tmp_path):
    manager, primary, target, _ = setup(tmp_path)
    record, digest = source(primary)
    plan = manager.plan(target, [record])
    assert not (target / ".clytie-mirror.json").exists()
    manager.confirm(plan["plan_id"])
    result = manager.copy_verified(record, digest, reference=reference, verify=lambda path: True)
    assert result["status"] == "complete"
    assert manager.activate()["status"] == "ready"
    assert (target / (record.backup_id + ".enc")).read_bytes() == (
        primary / "backup_a.enc"
    ).read_bytes()
    assert manager.state()["active"]["repo_id"] == plan["repo_id"]


def test_same_disk_changed_identity_and_unknown_probe_refuse(tmp_path):
    manager, primary, target, identities = setup(tmp_path)
    identities[str(target.resolve())] = "primary"
    with pytest.raises(MirrorError, match="mirror_same_device"):
        manager.plan(target, [])
    identities.clear()
    plan = manager.plan(target, [])
    identities[str(target.resolve())] = "replaced"
    with pytest.raises(MirrorError, match="mirror_identity_mismatch"):
        manager.confirm(plan["plan_id"])


def test_interrupted_copy_keeps_primary_and_does_not_replay(tmp_path):
    manager, primary, target, _ = setup(tmp_path)
    record, digest = source(primary)
    manager.confirm(manager.plan(target, [record])["plan_id"])

    def stop():
        raise ValueError("fictional cancellation")

    result = manager.copy_verified(
        record, digest, reference=reference, verify=lambda path: True, check=stop
    )
    assert result["status"] == "failed"
    assert (primary / "backup_a.enc").is_file()
    assert manager.recover_pending()[0]["status"] == "interrupted"
    assert not (target / "backup_a.enc").exists()


def test_catchup_selects_latest_per_group_only(tmp_path):
    _, primary, _, _ = setup(tmp_path)
    a, _ = source(primary, "a")
    b, _ = source(primary, "b")
    c, _ = source(primary, "c", group="instance", scope="instance")
    assert [r.backup_id for r in latest_per_group([a, b, c])] == ["c", "b"]


def test_published_before_receipt_recovers_without_recopy(tmp_path, monkeypatch):
    manager, primary, target, _ = setup(tmp_path)
    record, digest = source(primary)
    manager.confirm(manager.plan(target, [record])["plan_id"])
    receipt = manager._receipt

    def fail_receipt(*args):
        raise OSError("fictional crash point")

    monkeypatch.setattr(manager, "_receipt", fail_receipt)
    assert (
        manager.copy_verified(record, digest, reference=reference, verify=lambda path: True)[
            "status"
        ]
        == "failed"
    )
    monkeypatch.setattr(manager, "_receipt", receipt)
    (primary / "backup_a.enc").unlink()
    assert manager.recover_pending()[0]["status"] == "committed"
    assert (target / "backup_a.enc").is_file()


def test_existing_unowned_equal_file_is_not_adopted_on_recovery(tmp_path):
    manager, primary, target, _ = setup(tmp_path)
    record, digest = source(primary)
    manager.confirm(manager.plan(target, [record])["plan_id"])
    (target / "backup_a.enc").write_bytes((primary / "backup_a.enc").read_bytes())
    assert (
        manager.copy_verified(record, digest, reference=reference, verify=lambda path: True)[
            "status"
        ]
        == "failed"
    )
    assert manager.recover_pending()[0]["status"] == "interrupted"
    assert manager.state()["receipts"] == {}


def test_real_same_volume_cannot_claim_independent_storage(tmp_path):
    from clytie.backup.mirror import probe_storage

    manager, _, target, _ = setup(tmp_path)
    manager.probe = probe_storage
    with pytest.raises(MirrorError) as error:
        manager.plan(target, [])
    assert error.value.code in {"mirror_same_device", "mirror_device_unverified"}


def test_switch_failure_keeps_previous_active_and_its_files(tmp_path):
    manager, primary, target, identities = setup(tmp_path)
    record, digest = source(primary)
    first = manager.plan(target, [record])
    manager.confirm(first["plan_id"])
    manager.copy_verified(record, digest, reference=reference, verify=lambda path: True)
    manager.activate()
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    next_plan = manager.plan(replacement, [record])
    manager.confirm(next_plan["plan_id"])
    identities[str(replacement.resolve())] = "disconnected"
    assert (
        manager.copy_verified(record, digest, reference=reference, verify=lambda path: True)[
            "status"
        ]
        == "failed"
    )
    assert manager.state()["active"]["repo_id"] == first["repo_id"]
    assert (target / "backup_a.enc").exists()


def test_atomic_publish_does_not_require_hardlinks_or_overwrite(tmp_path, monkeypatch):
    from clytie.backup.mirror import publish_no_replace

    source = tmp_path / "temporary"
    target = tmp_path / "published"
    source.write_bytes(b"fictional new")
    target.write_bytes(b"fictional existing")
    with pytest.raises(FileExistsError):
        publish_no_replace(source, target)
    assert target.read_bytes() == b"fictional existing"
    next_target = tmp_path / "next"
    publish_no_replace(source, next_target)
    assert next_target.read_bytes() == b"fictional new"


def test_repeated_verified_copy_reuses_owned_receipt(tmp_path):
    manager, primary, target, _ = setup(tmp_path)
    record, digest = source(primary)
    manager.confirm(manager.plan(target, [record])["plan_id"])
    first = manager.copy_verified(record, digest, reference=reference, verify=lambda path: True)
    assert first["status"] == "complete"
    repeat = manager.copy_verified(
        record,
        digest,
        reference=reference,
        verify=lambda path: True,
        operation_id=first["operation_id"],
    )
    assert repeat["status"] == "complete"
    again = manager.copy_verified(record, digest, reference=reference, verify=lambda path: True)
    assert again["status"] == "complete"


def test_different_volumes_on_same_physical_device_are_rejected(tmp_path):
    manager, primary, target, _ = setup(tmp_path)

    def partitions(path):
        return StorageIdentity(
            str(path.resolve()),
            str(path.stat().st_ino),
            "volume_primary" if path == primary else "volume_other",
            ("same_disk",),
        )

    manager.probe = partitions
    with pytest.raises(MirrorError, match="mirror_same_device"):
        manager.plan(target, [])
    manager.probe = lambda path: StorageIdentity(str(path), "directory", "volume", ())
    with pytest.raises(MirrorError, match="mirror_device_unverified"):
        manager.plan(target, [])


def test_retention_only_returns_owned_mirror_paths(tmp_path):
    manager, primary, target, _ = setup(tmp_path)
    records = []
    for index in range(9):
        record, digest = source(primary, "backup_" + str(index))
        records.append(record)
        if index == 0:
            manager.confirm(manager.plan(target, [record])["plan_id"])
        manager.copy_verified(record, digest, reference=reference, verify=lambda path: True)
    manager.activate()
    unknown = replace(records[0], backup_id="unknown", path=str(primary / "unowned.enc"))
    plan = manager.retention([*records, unknown], recent=3, monthly_months=0)
    assert len(plan.delete) == 6
    assert all(str(target) in path for path in plan.delete)
    assert all(str(primary) not in path for path in plan.delete)


def test_disconnect_during_copy_preserves_primary_and_no_receipt(tmp_path):
    manager, primary, target, identities = setup(tmp_path)
    record, _ = source(primary)
    path = primary / "backup_a.enc"
    path.write_bytes(b"fictional block" * 200_000)
    record = replace(record, size=path.stat().st_size)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manager.confirm(manager.plan(target, [record])["plan_id"])
    checks = []

    def unplug():
        checks.append(1)
        if len(checks) == 4:
            identities[str(target.resolve())] = "replacement_device"

    result = manager.copy_verified(
        record, digest, reference=reference, verify=lambda path: True, check=unplug
    )
    assert result["status"] == "failed"
    assert result["reason"] == "mirror_identity_mismatch"
    assert path.exists() and not (target / "backup_a.enc").exists()
    assert manager.state()["receipts"] == {}


def test_retention_refuses_modified_receipted_file(tmp_path):
    manager, primary, target, _ = setup(tmp_path)
    record, digest = source(primary)
    manager.confirm(manager.plan(target, [record])["plan_id"])
    manager.copy_verified(record, digest, reference=reference, verify=lambda path: True)
    manager.activate()
    (target / "backup_a.enc").write_bytes(b"changed".ljust(record.size, b" "))
    with pytest.raises(MirrorError, match="mirror_verification_failed"):
        manager.retention([record])


def test_reconnect_copies_latest_groups_before_activation(tmp_path):
    manager, primary, target, _ = setup(tmp_path)
    old, old_hash = source(primary, "a")
    newest, newest_hash = source(primary, "b")
    full, full_hash = source(primary, "c", group="instance", scope="instance")
    manager.confirm(manager.plan(target, [old])["plan_id"])
    hashes = {old.backup_id: old_hash, newest.backup_id: newest_hash, full.backup_id: full_hash}
    result = manager.synchronize(
        [old, newest, full],
        sha256=lambda r: hashes[r.backup_id],
        reference=reference,
        verify=lambda p: True,
    )
    assert result["status"] == "ready"
    assert not (target / "a.enc").exists()
    assert (target / "b.enc").exists() and (target / "c.enc").exists()
    before = len(manager.state()["operations"])
    assert (
        manager.synchronize(
            [old, newest, full],
            sha256=lambda r: hashes[r.backup_id],
            reference=reference,
            verify=lambda p: True,
        )["status"]
        == "ready"
    )
    assert len(manager.state()["operations"]) == before


def test_mirror_rotation_uses_own_receipts_and_repository_references(tmp_path):
    from clytie.backup.repository import SourceRepository

    manager, primary, target, _ = setup(tmp_path)
    for index in range(5):
        record, hashed = source(primary, "backup_" + str(index))
        if index == 0:
            manager.confirm(manager.plan(target, [record])["plan_id"])
        assert (
            manager.copy_verified(record, hashed, reference=reference, verify=lambda p: True)[
                "status"
            ]
            == "complete"
        )
    manager.activate()
    repo = SourceRepository(target, manager.clock)
    catalog = repo.catalog("fictional_holder")
    protected = target / "backup_0.enc"
    catalog.reference(protected, "reader")
    assert manager.rotate(recent=3, monthly_months=0) == "retention_protected"
    assert protected.exists()
    assert not (target / "backup_1.enc").exists()
    assert all((target / ("backup_" + str(i) + ".enc")).exists() for i in range(2, 5))


@pytest.mark.parametrize("after", [False, True])
def test_real_process_mirror_publication_interruption(tmp_path, after):
    import subprocess
    import sys
    import threading

    from clytie.clock import SystemClock

    manager, primary, target, _ = setup(tmp_path)
    record, hashed = source(primary)
    manager.confirm(manager.plan(target, [record])["plan_id"])
    marker = tmp_path / "publication-marker"
    script = r"""
import sys, threading, hashlib
from pathlib import Path
from datetime import datetime, timezone
from contextlib import nullcontext
from clytie.backup import mirror
from clytie.backup.retention import BackupRecord
from clytie.sdk.testing import MockClock
state, primary, marker, after = map(str, sys.argv[1:])
primary = Path(primary)
def probe(path):
    path = path.resolve()
    disk = 'primary' if path == primary else 'secondary'
    return mirror.StorageIdentity(str(path), str(path.stat().st_ino), 'volume-' + disk, (disk,))
manager = mirror.MirrorManager(state, primary, MockClock(), owner_id='fictional_owner', probe=probe)
path = primary / 'backup_a.enc'
record = BackupRecord('backup_a',str(path),'memory','companion_a',datetime(2000,1,1,tzinfo=timezone.utc),path.stat().st_size)
original = mirror.publish_no_replace
def pause(source,target):
    if after == 'True': original(source,target)
    Path(marker).write_text('ready')
    threading.Event().wait(90)
mirror.publish_no_replace = pause
manager.copy_verified(record,hashlib.sha256(path.read_bytes()).hexdigest(),reference=lambda r:nullcontext(),verify=lambda p:True)
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(manager.directory),
            str(primary),
            str(marker),
            str(after),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = SystemClock().monotonic() + 20
        while (
            not marker.exists() and process.poll() is None and SystemClock().monotonic() < deadline
        ):
            threading.Event().wait(0.01)
        assert marker.exists(), process.communicate(timeout=2)
        process.kill()
        process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    results = manager.recover_pending()
    assert results[0]["status"] == ("committed" if after else "interrupted")
    assert (primary / "backup_a.enc").exists()
    assert (target / "backup_a.enc").exists() is after


def test_mirror_delete_receipt_recovers_without_repeating_unlink(tmp_path, monkeypatch):
    from pathlib import Path

    manager, primary, target, _ = setup(tmp_path)
    for index in range(4):
        record, hashed = source(primary, "backup_" + str(index))
        if index == 0:
            manager.confirm(manager.plan(target, [record])["plan_id"])
        manager.copy_verified(record, hashed, reference=reference, verify=lambda p: True)
    manager.activate()
    original = Path.unlink

    def stop(path, *a, **kw):
        original(path, *a, **kw)
        if path == target / "backup_0.enc":
            raise SystemExit()

    monkeypatch.setattr(Path, "unlink", stop)
    with pytest.raises(SystemExit):
        manager.rotate(recent=3, monthly_months=0)
    monkeypatch.setattr(Path, "unlink", lambda *a, **kw: pytest.fail("repeated deletion"))
    manager.recover_pending()
    assert manager.state()["deletions"][0]["state"] == "deleted"
    assert len(manager.state()["receipts"]) == 3


def test_legacy_mirror_receipt_without_manual_proof_is_protected(tmp_path):
    manager, primary, target, _ = setup(tmp_path)
    for index in range(4):
        record, hashed = source(primary, "backup_" + str(index))
        if index == 0:
            manager.confirm(manager.plan(target, [record])["plan_id"])
        manager.copy_verified(record, hashed, reference=reference, verify=lambda p: True)
    manager.activate()
    state = manager.state()
    for row in state["receipts"].values():
        row.pop("manual")
    manager._save(state)
    manager.rotate(recent=3, monthly_months=0)
    assert (target / "backup_0.enc").exists()
