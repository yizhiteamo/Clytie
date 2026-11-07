from uuid import uuid4

import pytest


def test_old_instance_cleanup_service_uses_registered_plan(tmp_path):
    from tests.test_backup_old_instances import fixture

    from clytie.backup.cleanup import CleanupService

    manager, record, old, saved = fixture(tmp_path)
    service = CleanupService(manager.home, manager.clock)
    plan = service.plan(
        "old_instance", old_id=record["id"], preserved={"state": str(saved / "state")}
    )
    assert plan["allowed"] and plan["category"] == "old_instance"
    assert service.apply(plan["id"])["state"] == "completed"
    assert service.operation(plan["id"])["state"] == "completed"
    assert not old.exists()


def test_regular_selection_must_be_explicit(tmp_path):
    from clytie.backup.cleanup import CleanupService
    from clytie.sdk.testing import MockClock

    with pytest.raises(ValueError, match="cleanup_selection_invalid"):
        CleanupService(tmp_path, MockClock()).plan("regular")


def test_worker_cleanup_and_recovery_never_replays_deletion(tmp_path, monkeypatch):
    from tests.test_backup_old_instances import fixture

    from clytie.backup.cleanup import CleanupService
    from clytie.backup.journal import BackupJournal
    from clytie.backup.worker import dispatch, verify_previous

    class Controls:
        def check(self):
            pass

    manager, record, old, saved = fixture(tmp_path)
    journal, _ = BackupJournal.accept(
        manager.home,
        manager.clock,
        uuid4().hex,
        "cleanup_plan",
        {
            "category": "old_instance",
            "old_id": record["id"],
            "preserved": {"state": str(saved / "state")},
        },
    )
    planned = dispatch(manager.home, manager.clock, journal, Controls())
    verify_previous(manager.home, manager.clock, journal, Controls())
    assert journal.value["result"]["plan_id"] == planned["plan_id"]
    applying, _ = BackupJournal.accept(
        manager.home, manager.clock, uuid4().hex, "cleanup_apply", {"plan_id": planned["plan_id"]}
    )
    assert dispatch(manager.home, manager.clock, applying, Controls())["verification"] == "passed"
    monkeypatch.setattr(CleanupService, "apply", lambda *a, **kw: pytest.fail("cleanup replay"))
    verify_previous(manager.home, manager.clock, applying, Controls())
    assert applying.value["result"]["verification"] == "passed"
    assert not old.exists()


async def test_real_cleanup_workers_plan_apply_and_duplicate(tmp_path):
    from tests.test_backup_old_instances import fixture
    from tests.test_backup_workers import completed

    from clytie.backup.cleanup import CleanupService
    from clytie.backup.workers import BackupWorkers

    manager, record, old, saved = fixture(tmp_path)
    workers = BackupWorkers(manager.home, manager.clock)
    plan_id, apply_id = uuid4().hex, uuid4().hex
    await workers.execute(
        "cleanup_plan",
        {
            "category": "old_instance",
            "old_id": record["id"],
            "preserved": {"state": str(saved / "state")},
        },
        plan_id,
    )
    assert (await completed(workers, plan_id))["phase"] == "committed"
    assert CleanupService(manager.home, manager.clock).operation(plan_id)["allowed"]
    workers = BackupWorkers(manager.home, manager.clock)
    await workers.execute("cleanup_apply", {"plan_id": plan_id}, apply_id)
    result = await completed(workers, apply_id)
    assert result["result"]["verification"] == "passed"
    assert not old.exists()
    duplicate = await BackupWorkers(manager.home, manager.clock).execute(
        "cleanup_apply", {"plan_id": plan_id}, apply_id
    )
    assert duplicate == result


def test_cleanup_cancel_records_stopped_and_preserves_deleted_facts(tmp_path):
    from tests.test_backup_old_instances import fixture

    from clytie.backup.cleanup import CleanupService
    from clytie.backup.journal import BackupJournal
    from clytie.backup.worker import Cancelled, dispatch

    manager, record, old, saved = fixture(tmp_path)
    (old / "z").write_bytes(b"fictional remaining")
    (saved / "z").write_bytes(b"fictional remaining")
    service = CleanupService(manager.home, manager.clock)
    plan = service.plan(
        "old_instance",
        old_id=record["id"],
        preserved={"state": str(saved / "state"), "z": str(saved / "z")},
    )
    journal, _ = BackupJournal.accept(
        manager.home, manager.clock, uuid4().hex, "cleanup_apply", {"plan_id": plan["id"]}
    )

    class Controls:
        def check(self):
            pass

        def check_optional(self):
            if not (old / "state").exists():
                raise Cancelled()

    with pytest.raises(Cancelled):
        dispatch(manager.home, manager.clock, journal, Controls())
    result = service.operation(plan["id"])
    assert result["state"] == "stopped"
    assert result["reason"] == "cancelled"
    assert result["deleted"] == ["state"]
    assert (old / "z").exists()


@pytest.mark.parametrize("replace_retirement", [False, True])
def test_cancel_releases_only_its_repository_retirement(tmp_path, replace_retirement):
    import shutil

    from tests.test_backup_old_instances import fixture

    from clytie.backup.cleanup import CleanupService
    from clytie.backup.journal import BackupJournal
    from clytie.backup.repository import SourceRepository
    from clytie.backup.restore import identity
    from clytie.backup.worker import Cancelled, dispatch

    manager, record, old, saved = fixture(tmp_path)
    repo = SourceRepository.initialize(
        old / "backups/regular", manager.clock, owner_instance_id="source_a"
    )
    package = repo.root / "package.enc"
    package.write_bytes(b"fictional encrypted package")
    repo.register({"backup_id": "package", "path": str(package), "identity": identity(package)})
    copies = tmp_path / "copies"
    shutil.copytree(old, copies)
    service = CleanupService(manager.home, manager.clock)
    plan = service.plan(
        "old_instance",
        old_id=record["id"],
        preserved={
            p.relative_to(old).as_posix(): str(copies / p.relative_to(old))
            for p in old.rglob("*")
            if p.is_file()
        },
    )
    journal, _ = BackupJournal.accept(
        manager.home, manager.clock, uuid4().hex, "cleanup_apply", {"plan_id": plan["id"]}
    )
    catalog = repo.catalog("foreign_holder")

    class Controls:
        def check(self):
            pass

        def check_optional(self):
            with pytest.raises(ValueError, match="source_repository_retiring"):
                catalog.reference(package, "reader")
            if replace_retirement:
                with repo.catalog(None)._transaction() as data:
                    data["repository_retirement"] = "other_cleanup"
            raise Cancelled()

    with pytest.raises(Cancelled):
        dispatch(manager.home, manager.clock, journal, Controls())
    result = service.operation(plan["id"])
    assert result["state"] == "stopped" and result["reason"] == "cancelled"
    assert result["deleted"] == [] and package.exists()
    if replace_retirement:
        with pytest.raises(ValueError, match="source_repository_retiring"):
            catalog.reference(package, "reader_after_cancel")
    else:
        catalog.reference(package, "reader_after_cancel")
        assert catalog.holders(package)
        catalog.release("reader_after_cancel")


def test_real_worker_cancel_after_unlink_preserves_progress(tmp_path):
    import subprocess
    import sys
    import threading

    from tests.test_backup_old_instances import fixture

    from clytie.backup.cleanup import CleanupService
    from clytie.backup.journal import BackupJournal
    from clytie.clock import SystemClock

    manager, record, old, saved = fixture(tmp_path)
    (old / "z").write_bytes(b"fictional remaining")
    (saved / "z").write_bytes(b"fictional remaining")
    service = CleanupService(manager.home, manager.clock)
    plan = service.plan(
        "old_instance",
        old_id=record["id"],
        preserved={"state": str(saved / "state"), "z": str(saved / "z")},
    )
    operation_id = uuid4().hex
    journal, _ = BackupJournal.accept(
        manager.home, manager.clock, operation_id, "cleanup_apply", {"plan_id": plan["id"]}
    )
    marker, proceed = tmp_path / "deleted-marker", tmp_path / "continue-cleanup"
    script = r"""
import sys, threading
from pathlib import Path
from clytie.backup.worker import execute
home, operation, first, marker, proceed = sys.argv[1:]
original = Path.unlink
def paused(path, *args, **kwargs):
    result = original(path, *args, **kwargs)
    if path == Path(first):
        Path(marker).write_text('deleted')
        for _ in range(3000):
            if Path(proceed).exists(): break
            threading.Event().wait(0.01)
        else: raise TimeoutError()
    return result
Path.unlink = paused
execute(home, operation, stream=sys.stdin.buffer.raw, emit=lambda event: None)
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(manager.home),
            operation_id,
            str(old / "state"),
            str(marker),
            str(proceed),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        process.stdin.write(b"start\n")
        process.stdin.flush()
        deadline = SystemClock().monotonic() + 20
        while (
            not marker.exists() and process.poll() is None and SystemClock().monotonic() < deadline
        ):
            threading.Event().wait(0.01)
        assert marker.exists(), process.communicate(timeout=2)
        BackupJournal.load(journal.path, manager.clock).request_cancel()
        process.stdin.write(b"cancel\n")
        process.stdin.flush()
        proceed.write_text("continue")
        output, errors = process.communicate(timeout=10)
        assert process.returncode == 0, (output, errors)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    assert BackupJournal.load(journal.path, manager.clock).value["phase"] == "cancelled"
    result = service.operation(plan["id"])
    assert result["state"] == "stopped" and result["reason"] == "cancelled"
    assert result["deleted"] == ["state"]
    assert not (old / "state").exists() and (old / "z").exists()
