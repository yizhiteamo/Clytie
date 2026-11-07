import os
from pathlib import Path

import pytest

from clytie.backup.old_instances import OldInstanceError, OldInstances, startup_gate
from clytie.backup.restore import source_registration
from clytie.plugin_host.locking import OperationBusy, file_lock, process_identity
from clytie.sdk.testing import MockClock


def fixture(tmp_path):
    current, old, saved = (tmp_path / name for name in ("current", "old", "saved"))
    for directory in (current, old, saved):
        directory.mkdir()
    (old / "state").write_bytes(b"fictional state")
    (saved / "state").write_bytes(b"fictional state")
    manager = OldInstances(current, MockClock())
    record = manager.register(
        source_registration(old),
        source_instance_id="source_a",
        target_instance_id="target_a",
        operation_id="operation_a",
    )
    return manager, record, old, saved


async def test_switch_registration_and_summary_notice_are_idempotent_and_path_free(tmp_path):
    from types import SimpleNamespace

    from clytie.supervisor.service import Supervisor

    manager, record, old, saved = fixture(tmp_path)
    state = dict(
        switched=True,
        source_stopped=True,
        source_registration=source_registration(old),
        source_instance_id="source_a",
        target_instance_id="target_b",
        operation_id="switch_b",
        companions=[],
    )
    manager.register_switch(state)
    manager.register_switch(state)
    assert manager.status()["count"] == 1
    calls = []

    class FakeNotifier:
        async def notify(self, notice, cancel):
            calls.append(notice)
            return SimpleNamespace(status="sent")

    svc = Supervisor(manager.home, [], None, MockClock())
    svc.notifications.plugins = [(FakeNotifier(), False)]
    summary = await svc.refresh_old_instances()
    await svc.notifications.update(svc.problems)
    await svc.notifications.update(svc.problems)
    assert summary["count"] == 1
    assert summary["scanned_at"]
    assert len(calls) == 1
    assert str(old) not in calls[0].body
    assert calls[0].dedupe_key == "old_instances:instance"


def test_multiple_registrations_persist_and_missing_is_unknown(tmp_path):
    manager, record, old, saved = fixture(tmp_path)
    second = tmp_path / "old_b"
    second.mkdir()
    manager.register(
        source_registration(second),
        source_instance_id="source_b",
        target_instance_id="target_a",
        operation_id="operation_b",
    )
    second.rmdir()
    status = OldInstances(manager.home, MockClock()).status()
    assert status["count"] == 2
    assert status["known_bytes"] == len(b"fictional state")
    assert status["unknown"] == 1
    assert (
        manager.register(
            source_registration(old),
            source_instance_id="source_a",
            target_instance_id="target_a",
            operation_id="operation_a",
        )["id"]
        == record["id"]
    )


def test_plan_readonly_and_explicit_file_cleanup(tmp_path):
    manager, record, old, saved = fixture(tmp_path)
    before = (old / "state").read_bytes()
    plan = manager.plan_cleanup(record["id"], preserved={"state": saved / "state"})
    assert plan["allowed"]
    assert (old / "state").read_bytes() == before
    result = manager.apply_cleanup(plan["id"])
    assert result["state"] == "completed"
    assert result["deleted"] == ["state"]
    assert not old.exists()
    assert (saved / "state").read_bytes() == before
    assert manager.status()["count"] == 0


def test_unique_key_requires_real_independent_copy(tmp_path):
    manager, record, old, saved = fixture(tmp_path)
    (old / "secrets").mkdir()
    key = old / "secrets/backup.key"
    key.write_bytes(b"k" * 32)
    plan = manager.plan_cleanup(record["id"], preserved={"state": saved / "state"})
    assert not plan["allowed"]
    assert any(r["code"] == "unique_key_unpreserved" for r in plan["blocked"])
    (saved / "key").write_bytes(b"k" * 32)
    plan = manager.plan_cleanup(
        record["id"], preserved={"state": saved / "state", "secrets/backup.key": saved / "key"}
    )
    assert plan["allowed"]


def test_hardlinked_preservation_is_not_independent(tmp_path):
    manager, record, old, saved = fixture(tmp_path)
    linked = saved / "linked"
    try:
        os.link(old / "state", linked)
    except OSError:
        pytest.skip("platform sandbox does not permit creating hard links")
    assert not manager.plan_cleanup(record["id"], preserved={"state": linked})["allowed"]


@pytest.mark.parametrize("change", ["new_file", "changed_source", "changed_copy"])
def test_apply_revalidates_complete_plan_before_deleting(tmp_path, change):
    manager, record, old, saved = fixture(tmp_path)
    plan = manager.plan_cleanup(record["id"], preserved={"state": saved / "state"})
    if change == "new_file":
        (old / "new").write_text("new")
    elif change == "changed_source":
        (old / "state").write_text("changed")
    else:
        (saved / "state").write_text("changed")
    result = manager.apply_cleanup(plan["id"])
    assert result["state"] == "stopped"
    assert result["deleted"] == []
    assert (old / "state").exists()
    with pytest.raises(OldInstanceError):
        manager.apply_cleanup(plan["id"])


def test_partial_delete_is_audited_and_cannot_replay(tmp_path, monkeypatch):
    manager, record, old, saved = fixture(tmp_path)
    (old / "z").write_text("second")
    (saved / "z").write_text("second")
    plan = manager.plan_cleanup(
        record["id"], preserved={"state": saved / "state", "z": saved / "z"}
    )
    unlink = Path.unlink

    def fail(path, *args, **kwargs):
        if path == old / "z":
            raise PermissionError("fictional")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail)
    result = manager.apply_cleanup(plan["id"])
    assert result["state"] == "stopped"
    assert result["deleted"] == ["state"]
    assert (old / "z").exists()
    assert any(event["kind"] == "deleted" for event in result["audit"])


def test_live_source_and_restore_reference_are_protected(tmp_path):
    manager, record, old, saved = fixture(tmp_path)
    with file_lock(old / "run/supervisor.lock", {**process_identity(), "operation": "supervisor"}):
        assert "source_active" in {r["code"] for r in manager.plan_cleanup(record["id"])["blocked"]}
    protected = OldInstances(manager.home, MockClock(), references=lambda: [old / "state"])
    assert "restore_reference" in {
        r["code"] for r in protected.plan_cleanup(record["id"])["blocked"]
    }


def test_imported_registry_has_no_automatic_deletion_authority(tmp_path):
    manager, record, old, saved = fixture(tmp_path)
    other = tmp_path / "other"
    (other / "supervisor").mkdir(parents=True)
    (other / "supervisor/old-instances.json").write_bytes(manager.registry_path.read_bytes())
    imported = OldInstances(other, MockClock())
    assert not imported.plan_cleanup(record["id"], preserved={"state": saved / "state"})["allowed"]


def test_startup_gate_excludes_cleanup_and_other_startup(tmp_path):
    with startup_gate(tmp_path / "old"):
        with pytest.raises(OperationBusy):
            with startup_gate(tmp_path / "old"):
                pytest.fail("must be exclusive")


def test_register_retries_after_source_was_cleaned(tmp_path):
    manager, record, old, saved = fixture(tmp_path)
    plan = manager.plan_cleanup(record["id"], preserved={"state": saved / "state"})
    manager.apply_cleanup(plan["id"])
    again = manager.register(
        record["registration"],
        source_instance_id="source_a",
        target_instance_id="target_a",
        operation_id="operation_a",
    )
    assert again["id"] == record["id"]
    assert again["state"] == "cleaned"


def test_same_file_identity_cannot_count_as_preservation(tmp_path, monkeypatch):
    from clytie.backup import old_instances

    manager, record, old, saved = fixture(tmp_path)
    original = old_instances._file
    same = original(old / "state")["identity"]

    def duplicate_identity(path):
        value = original(path)
        if path == saved / "state":
            value["identity"] = same
        return value

    monkeypatch.setattr(old_instances, "_file", duplicate_identity)
    assert not manager.plan_cleanup(record["id"], preserved={"state": saved / "state"})["allowed"]


def test_copy_changed_after_first_delete_stops_remaining_files(tmp_path, monkeypatch):
    manager, record, old, saved = fixture(tmp_path)
    (old / "z").write_text("second")
    (saved / "z").write_text("second")
    plan = manager.plan_cleanup(
        record["id"], preserved={"state": saved / "state", "z": saved / "z"}
    )
    unlink = Path.unlink

    def change_copy(path, *args, **kwargs):
        result = unlink(path, *args, **kwargs)
        if path == old / "state":
            (saved / "state").write_text("changed")
        return result

    monkeypatch.setattr(Path, "unlink", change_copy)
    result = manager.apply_cleanup(plan["id"])
    assert result["state"] == "stopped"
    assert result["deleted"] == ["state"]
    assert (old / "z").exists()


def test_cleanup_real_kill_records_unknown_without_replaying(tmp_path):
    import subprocess
    import sys
    import threading

    manager, record, old, saved = fixture(tmp_path)
    plan = manager.plan_cleanup(record["id"], preserved={"state": saved / "state"})
    code = """
import sys, threading
from pathlib import Path
from clytie.backup.old_instances import OldInstances
from clytie.sdk.testing import MockClock
original = Path.unlink
def pause(path, *args, **kwargs):
    result = original(path, *args, **kwargs)
    print("deleted_checkpoint", flush=True)
    threading.Event().wait()
    return result
Path.unlink = pause
OldInstances(Path(sys.argv[1]), MockClock()).apply_cleanup(sys.argv[2])
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(manager.home), plan["id"]],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "deleted_checkpoint"
        process.kill()
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.stdout.close()
        process.stderr.close()
    assert not (old / "state").exists()
    with pytest.raises(OldInstanceError):
        manager.apply_cleanup(plan["id"])
    for _ in range(500):
        try:
            result = manager.recover_cleanup(plan["id"])
            break
        except OperationBusy:
            threading.Event().wait(0.005)
    else:
        pytest.fail("OS did not release the killed cleanup lock")
    assert result["state"] == "stopped"
    assert result["deleted"] == []
    assert result["unknown_paths"] == ["state"]
    assert old.exists()
    assert (saved / "state").read_bytes() == b"fictional state"


def test_repeated_source_keeps_transition_history_without_double_count(tmp_path):
    manager, record, old, saved = fixture(tmp_path)
    second = manager.register(
        record["registration"],
        source_instance_id="source_a",
        target_instance_id="target_b",
        operation_id="operation_b",
    )
    assert second["id"] == record["id"]
    assert manager.status()["count"] == 1
    assert [item["operation_id"] for item in second["transitions"]] == [
        "operation_a",
        "operation_b",
    ]


def test_current_home_is_never_cleanup_candidate(tmp_path):
    manager, record, old, saved = fixture(tmp_path)
    current = manager.register(
        source_registration(manager.home),
        source_instance_id="current",
        target_instance_id="next",
        operation_id="operation_current",
    )
    assert "current_instance" in {
        item["code"] for item in manager.plan_cleanup(current["id"])["blocked"]
    }


def test_imported_local_record_requires_explicit_acceptance(tmp_path):
    manager, record, old, saved = fixture(tmp_path)
    other = tmp_path / "other"
    (other / "supervisor").mkdir(parents=True)
    (other / "supervisor/old-instances.json").write_bytes(manager.registry_path.read_bytes())
    imported = OldInstances(other, MockClock())
    imported.accept_local(record["id"], registration=record["registration"])
    assert imported.plan_cleanup(record["id"], preserved={"state": saved / "state"})["allowed"]


def test_startup_gate_blocks_separate_process(tmp_path):
    import subprocess
    import sys

    home = tmp_path / "old"
    code = """
import sys
from pathlib import Path
from clytie.backup.old_instances import startup_gate
from clytie.plugin_host.locking import OperationBusy
try:
    with startup_gate(Path(sys.argv[1])):
        raise SystemExit(2)
except OperationBusy:
    raise SystemExit(0)
"""
    with startup_gate(home):
        result = subprocess.run(
            [sys.executable, "-c", code, str(home)], capture_output=True, timeout=15
        )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_old_instance_cleanup_blocks_foreign_repository_references(tmp_path):
    from clytie.backup.repository import SourceRepository
    from clytie.backup.restore import identity

    manager, record, old, saved = fixture(tmp_path)
    repo = SourceRepository.initialize(
        old / "backups/regular", manager.clock, owner_instance_id="source_a"
    )
    package = repo.root / "package.enc"
    package.write_bytes(b"fictional encrypted package")
    repo.register({"backup_id": "package", "path": str(package), "identity": identity(package)})
    repo.catalog("foreign_home").reference(package, "foreign_reader")
    plan = manager.plan_cleanup(record["id"], preserved={"state": saved / "state"})
    assert any(item["code"] == "restore_reference" for item in plan["blocked"])


def test_old_repository_cleanup_retires_and_removes_explicit_files(tmp_path):
    import shutil

    from clytie.backup.repository import SourceRepository
    from clytie.backup.restore import identity

    manager, record, old, saved = fixture(tmp_path)
    repo = SourceRepository.initialize(
        old / "backups/regular", manager.clock, owner_instance_id="source_a"
    )
    package = repo.root / "package.enc"
    package.write_bytes(b"fictional encrypted package")
    repo.register({"backup_id": "package", "path": str(package), "identity": identity(package)})
    copies = tmp_path / "copies"
    shutil.copytree(old, copies)
    preserved = {
        p.relative_to(old).as_posix(): copies / p.relative_to(old)
        for p in old.rglob("*")
        if p.is_file()
    }
    plan = manager.plan_cleanup(record["id"], preserved=preserved)
    assert plan["allowed"], plan["blocked"]
    result = manager.apply_cleanup(plan["id"])
    assert result["state"] == "completed", result
    assert not old.exists()
    assert (copies / "backups/regular/package.enc").exists()


def test_real_process_retirement_crash_blocks_reader_until_recovered(tmp_path):
    import shutil
    import subprocess
    import sys

    from clytie.backup.repository import SourceRepository
    from clytie.backup.restore import identity

    manager, record, old, saved = fixture(tmp_path)
    repo = SourceRepository.initialize(
        old / "backups/regular", manager.clock, owner_instance_id="source_a"
    )
    package = repo.root / "package.enc"
    package.write_bytes(b"fictional encrypted package")
    repo.register({"backup_id": "package", "path": str(package), "identity": identity(package)})
    copies = tmp_path / "copies"
    shutil.copytree(old, copies)
    preserved = {
        p.relative_to(old).as_posix(): copies / p.relative_to(old)
        for p in old.rglob("*")
        if p.is_file()
    }
    plan = manager.plan_cleanup(record["id"], preserved=preserved)
    assert plan["allowed"]
    script = r"""
import sys,threading
from clytie.backup.old_instances import OldInstances
from clytie.backup.repository import SourceRepository
from clytie.sdk.testing import MockClock
original = SourceRepository.retire
def pause(self, op):
    original(self,op)
    print('retired',flush=True)
    threading.Event().wait(60)
SourceRepository.retire = pause
OldInstances(sys.argv[1],MockClock()).apply_cleanup(sys.argv[2])
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(manager.home), plan["id"]],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "retired"
        with pytest.raises(ValueError, match="source_repository_retiring"):
            repo.catalog("foreign_home").reference(package, "reader")
        process.kill()
        process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    assert manager.recover_cleanup(plan["id"])["state"] == "stopped"
    assert package.exists()
    repo.catalog("foreign_home").reference(package, "reader_after_recovery")
    repo.catalog("foreign_home").release("reader_after_recovery")
