from datetime import timedelta
from pathlib import Path

import pytest

from clytie.backup.cleanup import CleanupService
from clytie.backup.restore import digest, identity
from clytie.backup.storage import BackupStorage
from clytie.sdk.testing import MockClock


def fixture(tmp_path, count=5):
    clock = MockClock()
    store = BackupStorage(tmp_path / "home", clock)
    for number in range(count):
        path = store.root / f"package_{number}.enc"
        path.write_bytes(b"fictional")
        store.register(
            dict(
                backup_id=f"package_{number}",
                path=str(path),
                scope="instance",
                group="group_a",
                completed_at=(clock.now() - timedelta(days=count - number)).isoformat(),
                size=9,
                sha256=digest(path),
                identity=identity(path),
                manual=number == 0,
                completeness="complete",
                missing_count=0,
            )
        )
    return store, CleanupService(store.home, clock)


def test_explicit_manual_and_recent_protection(tmp_path):
    store, service = fixture(tmp_path)
    plan = service.plan("regular", backup_ids=["package_0", "package_4"])
    assert plan["eligible_ids"] == ["package_0"]
    assert plan["estimated_freed_bytes"] == 9
    result = service.apply(plan["id"])
    assert result["state"] == "partial"
    assert result["items"][0]["state"] == "deleted"
    assert result["items"][1]["state"] == "blocked"
    assert not Path(store.root / "package_0.enc").exists()
    assert service.apply(plan["id"]) == result


def test_reference_added_after_plan_blocks_deletion(tmp_path):
    store, service = fixture(tmp_path)
    plan = service.plan("regular", backup_ids=["package_0"])
    catalog = store.repository.catalog("other_home")
    catalog.reference(store.get("package_0")["path"], "foreign_execution", kind="execution")
    result = service.apply(plan["id"])
    assert result["state"] == "blocked"
    assert result["items"][0]["reason"] == "backup_referenced"
    assert Path(store.get("package_0")["path"]).exists()


@pytest.mark.parametrize("ids", [None, [], ["*"], ["../other"], ["a", "a"], "a"])
def test_no_implicit_or_path_selection(tmp_path, ids):
    with pytest.raises(ValueError, match="cleanup_selection_invalid"):
        CleanupService(tmp_path, MockClock()).plan("regular", backup_ids=ids)


def test_partial_unverified_and_other_group_never_replace_minimum(tmp_path):
    store, service = fixture(tmp_path, 6)
    from clytie.supervisor.common import write

    state = store.state()
    state["records"]["package_5"]["group"] = "group_b"
    state["records"]["package_4"]["completeness"] = "partial"
    state["records"]["package_3"]["verified"] = False
    write(store.path, state)
    plan = service.plan("regular", backup_ids=list(state["records"]))
    assert plan["eligible_ids"] == ["package_4"]
    assert {i["reason"] for i in plan["items"] if i["backup_id"] in {"package_0", "package_5"}} == {
        "minimum_restore_points"
    }


def test_identity_change_and_current_minimum_rechecked(tmp_path):
    store, service = fixture(tmp_path)
    plan = service.plan("regular", backup_ids=["package_0", "package_1"])
    Path(store.get("package_4")["path"]).write_bytes(b"damaged")
    result = service.apply(plan["id"])
    assert [item["state"] for item in result["items"]] == ["deleted", "blocked"]
    assert result["items"][1]["reason"] == "minimum_restore_points"
    assert result["state"] == "partial"


def test_replaced_selected_file_is_never_deleted(tmp_path):
    store, service = fixture(tmp_path)
    plan = service.plan("regular", backup_ids=["package_0"])
    path = Path(store.get("package_0")["path"])
    path.rename(path.with_suffix(".saved"))
    path.write_bytes(b"replacement")
    result = service.apply(plan["id"])
    assert result["state"] == "blocked"
    assert path.read_bytes() == b"replacement"


def test_rotation_does_not_count_invalid_file_as_minimum(tmp_path):
    from clytie.backup.retention import RetentionConfig

    store, _ = fixture(tmp_path)
    Path(store.get("package_4")["path"]).write_bytes(b"damaged")
    store.rotate(
        RetentionConfig(daily_days=0, weekly_weeks=0, monthly_months=0, target_bytes=1),
        started_bytes=45,
        new_backup_id="package_3",
    )
    assert Path(store.root / "package_1.enc").exists()


def test_rotation_and_explicit_display_share_instance_calendar_days(tmp_path):
    from clytie.backup.retention import RetentionConfig
    from clytie.supervisor.common import write

    store, service = fixture(tmp_path)
    (store.home / "clytie.local.yaml").write_text("timezone: Asia/Shanghai\n")
    state = store.state()
    for number, hour in [(1, 15), (2, 17), (3, 19)]:
        state["records"][f"package_{number}"]["completed_at"] = f"1999-12-30T{hour}:00:00+00:00"
    write(store.path, state)
    plan = service.plan("regular", backup_ids=["package_1"])
    assert "daily" in plan["items"][0]["retention_layers"]
    store.rotate(RetentionConfig(target_bytes=None), started_bytes=45, new_backup_id="package_4")
    assert (store.root / "package_1.enc").exists()


def test_failure_keeps_per_item_results_and_never_retries(tmp_path, monkeypatch):
    store, service = fixture(tmp_path, 7)
    plan = service.plan("regular", backup_ids=["package_0", "package_1", "package_2"])
    original = Path.unlink

    def fail(path, *args, **kwargs):
        if path.name == "package_1.enc":
            raise OSError("fictional failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail)
    result = service.apply(plan["id"])
    assert result["state"] == "partial"
    assert [item["state"] for item in result["items"]] == ["deleted", "failed", "not_executed"]
    monkeypatch.setattr(Path, "unlink", lambda *a, **kw: pytest.fail("must not replay"))
    assert service.apply(plan["id"]) == result
    assert service.recover(plan["id"]) == result


def test_post_delete_reference_receipt_failure_preserves_deleted_fact(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from clytie.backup.repository import SourceRepository

    store, service = fixture(tmp_path)
    plan = service.plan("regular", backup_ids=["package_0", "package_1"])
    original = SourceRepository.deletion

    @contextmanager
    def failure(repository, source):
        with original(repository, source) as allowed:
            yield allowed
        raise OSError("fictional reference receipt failure")

    monkeypatch.setattr(SourceRepository, "deletion", failure)
    result = service.apply(plan["id"])
    assert result["state"] == "partial"
    assert result["items"][0]["state"] == "deleted"
    assert result["items"][1]["state"] == "not_executed"
    assert not (store.root / "package_0.enc").exists()


def test_crash_after_unlink_does_not_infer_success_or_retry(tmp_path, monkeypatch):
    store, service = fixture(tmp_path)
    plan = service.plan("regular", backup_ids=["package_0", "package_1"])
    original = Path.unlink

    def crash(path, *args, **kwargs):
        original(path, *args, **kwargs)
        if path.name == "package_0.enc":
            raise SystemExit()

    monkeypatch.setattr(Path, "unlink", crash)
    with pytest.raises(SystemExit):
        service.apply(plan["id"])
    monkeypatch.setattr(Path, "unlink", lambda *a, **kw: pytest.fail("must not replay"))
    result = service.recover(plan["id"])
    assert result["state"] == "recovery_blocked"
    assert result["items"][0]["reason"] == "cleanup_result_unknown"
    assert result["items"][1]["state"] == "not_executed"
    assert service.apply(plan["id"]) == result


def test_regular_worker_recovery_verifies_immutable_selection(tmp_path, monkeypatch):
    from uuid import uuid4

    from clytie.backup.journal import BackupJournal
    from clytie.backup.worker import dispatch, verify_previous

    store, service = fixture(tmp_path)
    plan = service.plan("regular", backup_ids=["package_0"])
    journal, _ = BackupJournal.accept(
        store.home, store.clock, uuid4().hex, "cleanup_apply", {"plan_id": plan["id"]}
    )

    class Controls:
        def check(self):
            pass

    assert dispatch(store.home, store.clock, journal, Controls())["verification"] == "passed"
    monkeypatch.setattr(CleanupService, "apply", lambda *a, **kw: pytest.fail("must not replay"))
    verify_previous(store.home, store.clock, journal, Controls())
    assert journal.value["result"]["verification"] == "passed"


def test_same_plan_id_is_immutable_and_cancel_keeps_progress(tmp_path):
    from uuid import uuid4

    from clytie.backup.old_instances import OldInstanceError

    store, service = fixture(tmp_path)
    plan_id = uuid4().hex
    plan = service.plan("regular", backup_ids=["package_0", "package_1"], plan_id=plan_id)
    assert service.plan("regular", backup_ids=plan["backup_ids"], plan_id=plan_id) == plan
    with pytest.raises(ValueError, match="cleanup_plan_conflict"):
        service.plan("regular", backup_ids=["package_1"], plan_id=plan_id)

    def check():
        if not (store.root / "package_0.enc").exists():
            raise OldInstanceError("cancelled")

    result = service.apply(plan_id, check=check)
    assert result["state"] == "stopped" and result["reason"] == "cancelled"
    assert [item["state"] for item in result["items"]] == ["deleted", "not_executed"]
    assert service.plan("regular", backup_ids=plan["backup_ids"], plan_id=plan_id) == result
    assert service.apply(plan_id) == result


def test_foreign_restore_binding_grants_no_cleanup_right(tmp_path):
    from clytie.backup.previews import PreviewCatalog

    store, _ = fixture(tmp_path)
    management = tmp_path / "management"
    source = Path(store.get("package_0")["path"])
    catalog = PreviewCatalog(management, store.clock)
    catalog.bind_source(source, store.repository.resolve("package_0"), confirmed=True)
    service = CleanupService(management, store.clock)
    plan = service.plan("regular", backup_ids=["package_0"])
    assert plan["eligible_ids"] == []
    assert plan["items"][0]["reason"] == "backup_not_owned"
    assert service.apply(plan["id"])["state"] == "blocked"
    assert source.exists()


def test_longterm_layer_selection_and_group_summary(tmp_path):
    store, service = fixture(tmp_path)
    (store.home / "clytie.local.yaml").write_text(
        "timezone: Asia/Shanghai\nbackup:\n  retention:\n    daily_days: 0\n    weekly_weeks: 52\n"
    )
    # A unique older week is expected to remain automatically, but may be selected explicitly.
    from clytie.supervisor.common import write

    state = store.state()
    state["records"]["package_1"]["completed_at"] = (
        store.clock.now() - timedelta(days=90)
    ).isoformat()
    write(store.path, state)
    plan = service.plan("regular", backup_ids=["package_1"])
    assert "weekly" in plan["items"][0]["retention_layers"]
    assert plan["eligible_ids"] == ["package_1"]
    assert plan["groups_after"] == [
        dict(scope="instance", group="group_a", valid_before=5, valid_after=4)
    ]


RACE_CHILD = r"""
import json, sys, threading
from pathlib import Path
from clytie.backup.cleanup import CleanupService
from clytie.backup.repository import SourceRepository
from clytie.backup.retention import RetentionConfig
from clytie.backup.storage import BackupStorage
from clytie.sdk.testing import MockClock
home, root, gate, action, plan_id = sys.argv[1:]
clock = MockClock()
for _ in range(2000):
    if Path(gate).exists(): break
    threading.Event().wait(0.01)
else: raise TimeoutError('gate')
try:
    if action == 'reference':
        repo = SourceRepository(root, clock)
        source = repo.resolve('package_0')['source']
        repo.catalog('foreign_management').reference(source, 'execution_a', kind='execution')
        result = 'referenced'
    elif action == 'rotate':
        store = BackupStorage(home, clock)
        result = store.rotate(RetentionConfig(daily_days=0, weekly_weeks=0,
            monthly_months=0, target_bytes=1), started_bytes=45, new_backup_id='package_3')
    else:
        result = CleanupService(home, clock).apply(plan_id)['state']
except Exception as exc:
    result = type(exc).__name__ + ':' + str(exc)
print(json.dumps(result), flush=True)
"""


@pytest.mark.parametrize("competitor", ["reference", "rotate", "cleanup"])
@pytest.mark.parametrize("damaged", [False, True])
def test_real_process_explicit_cleanup_competes_with_source_locks(tmp_path, competitor, damaged):
    import json
    import subprocess
    import sys

    store, service = fixture(tmp_path)
    first = service.plan("regular", backup_ids=["package_0", "package_1"])
    second = service.plan("regular", backup_ids=["package_0", "package_1"])
    if damaged:
        Path(store.get("package_4")["path"]).write_bytes(b"damaged after plan")
    gate = tmp_path / "gate"
    children = []
    try:
        for action, plan in [("cleanup", first), (competitor, second)]:
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        RACE_CHILD,
                        str(store.home),
                        str(store.root),
                        str(gate),
                        action,
                        plan["id"],
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        gate.write_text("go")
        results = []
        for child in children:
            output, errors = child.communicate(timeout=30)
            assert child.returncode == 0, errors
            results.append(json.loads(output))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=10)
    assert all(Path(store.get(f"package_{number}")["path"]).exists() for number in [2, 3, 4])
    if damaged:
        assert Path(store.get("package_1")["path"]).exists()
    if competitor == "reference" and results[1] == "referenced":
        assert Path(store.get("package_0")["path"]).exists()
        assert service.operation(first["id"])["items"][0]["state"] != "deleted"
    if competitor == "cleanup":
        receipts = store.state()["deletions"]
        assert len({r["backup_id"] for r in receipts if r["state"] == "deleted"}) == len(receipts)
