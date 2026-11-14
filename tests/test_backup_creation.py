from uuid import uuid4

import pytest
from tests.test_backup_source import fictional

from clytie.backup.journal import BackupJournal


class Controls:
    capture_context = {}

    def check(self):
        pass


def test_create_real_package_and_unknown_publication_recovery(tmp_path, monkeypatch):
    from clytie.backup import creation
    from clytie.backup.storage import BackupStorage

    home, clock = fictional(tmp_path)
    journal, _ = BackupJournal.accept(home, clock, uuid4().hex, "create", {"scope": "instance"})
    original = creation.publish_no_replace

    def publish_then_die(source, target):
        original(source, target)
        raise SystemExit()

    monkeypatch.setattr(creation, "publish_no_replace", publish_then_die)
    with pytest.raises(SystemExit):
        creation.dispatch(home, clock, journal, Controls())
    assert journal.value["phase"] == "publishing"
    monkeypatch.setattr(creation, "capture_instance", lambda *a, **k: pytest.fail("recaptured"))
    creation.verify_previous(home, clock, journal, Controls())
    assert journal.value["result"]["creation"] == "complete"
    assert len(BackupStorage(home, clock).list()) == 1


def test_manual_package_and_verify_separate_results(tmp_path):
    from clytie.backup.creation import dispatch
    from clytie.backup.storage import BackupStorage

    home, clock = fictional(tmp_path)
    journal, _ = BackupJournal.accept(
        home, clock, uuid4().hex, "create", {"scope": "memory", "companion_id": "companion_a"}
    )
    result = dispatch(home, clock, journal, Controls())
    assert result["creation"] == "complete"
    assert result["retention"] == "skipped"
    record = BackupStorage(home, clock).get(result["backup_id"])
    assert record["manual"] is True
    verify, _ = BackupJournal.accept(
        home, clock, uuid4().hex, "verify", {"backup_id": result["backup_id"]}
    )
    assert dispatch(home, clock, verify, Controls())["verification"] == "passed"


def test_unpublished_package_is_never_published_by_recovery(tmp_path, monkeypatch):
    from clytie.backup import creation
    from clytie.backup.storage import BackupStorage

    home, clock = fictional(tmp_path)
    journal, _ = BackupJournal.accept(home, clock, uuid4().hex, "create", {"scope": "instance"})

    def die(*args):
        raise SystemExit()

    monkeypatch.setattr(creation, "publish_no_replace", die)
    with pytest.raises(SystemExit):
        creation.dispatch(home, clock, journal, Controls())
    creation.verify_previous(home, clock, journal, Controls())
    assert journal.value["phase"] == "failed"
    assert BackupStorage(home, clock).list() == []


def test_postcommit_mirror_failure_keeps_primary(tmp_path):
    import yaml

    from clytie.backup.creation import dispatch
    from clytie.backup.storage import BackupStorage

    home, clock = fictional(tmp_path)
    config = home / "clytie.local.yaml"
    document = yaml.safe_load(config.read_bytes())
    document["backup"] = {"mirror": {"enabled": True, "path": str(tmp_path / "missing")}}
    config.write_text(yaml.safe_dump(document))
    journal, _ = BackupJournal.accept(home, clock, uuid4().hex, "create", {"scope": "instance"})
    result = dispatch(home, clock, journal, Controls())
    assert result["creation"] == "complete"
    assert result["mirror"] == "failed"
    assert result["mirror_reason"] == "mirror_switch_pending"
    assert BackupStorage(home, clock).valid(BackupStorage(home, clock).get(result["backup_id"]))


def test_partial_package_lists_missing_components_without_exception_text(tmp_path):
    from clytie.backup.creation import dispatch
    from clytie.backup.storage import BackupStorage

    home, clock = fictional(tmp_path)

    async def unavailable(*args):
        raise TimeoutError("fictional sensitive detail")

    controls = Controls()
    controls.capture_context = {"online": unavailable, "online_ids": {"companion_a"}}
    journal, _ = BackupJournal.accept(home, clock, uuid4().hex, "create", {"scope": "instance"})
    result = dispatch(home, clock, journal, controls)
    row = BackupStorage(home, clock).get(result["backup_id"])
    assert result["creation"] == "partial"
    assert len(row["missing"]) == result["missing_count"] > 0
    assert all(set(item) == {"component", "reason"} for item in row["missing"])
    assert "fictional sensitive detail" not in str(row)


def test_retention_protects_live_reference_and_three_complete(tmp_path):
    from datetime import timedelta

    from clytie.backup.previews import PreviewCatalog
    from clytie.backup.restore import digest, identity
    from clytie.backup.retention import RetentionConfig
    from clytie.backup.storage import BackupStorage
    from clytie.sdk.testing import MockClock

    clock = MockClock()
    home = tmp_path / "home"
    store = BackupStorage(home, clock)
    store.root.mkdir(parents=True, exist_ok=True)
    rows = []
    for number in range(5):
        path = store.root / (str(number) + ".clytie-backup")
        path.write_bytes(b"fictional")
        row = dict(
            backup_id=str(number),
            path=str(path),
            scope="instance",
            group="fictional",
            completed_at=(clock.now() - timedelta(days=5 - number)).isoformat(),
            size=9,
            sha256=digest(path),
            identity=identity(path),
            manual=False,
            completeness="complete",
            missing_count=0,
        )
        store.register(row)
        rows.append(row)
    refs = PreviewCatalog(home, clock)
    refs.reference(rows[0]["path"], "reader-one")
    config = RetentionConfig(daily_days=0, weekly_weeks=0, monthly_months=0, target_bytes=1)
    assert store.rotate(config, started_bytes=36, new_backup_id="4") == "retention_protected"
    assert {r["backup_id"] for r in store.list()} == {"0", "2", "3", "4"}


@pytest.mark.parametrize("after_publish", [False, True])
def test_real_process_kill_at_publication_does_not_recapture(tmp_path, after_publish):
    import subprocess
    import sys
    import threading

    from clytie.backup.creation import verify_previous
    from clytie.backup.storage import BackupStorage
    from clytie.clock import SystemClock

    home, clock = fictional(tmp_path)
    journal, _ = BackupJournal.accept(home, clock, uuid4().hex, "create", {"scope": "instance"})
    marker = tmp_path / "at-publication"
    script = r"""
import sys, threading
from pathlib import Path
from clytie.backup import creation
from clytie.backup.journal import BackupJournal
from clytie.sdk.testing import MockClock
home, op, marker, after = sys.argv[1:]
clock = MockClock()
journal = BackupJournal.load(BackupJournal.path_for(home, op), clock)
original = creation.publish_no_replace
def pause(source, target):
    if after == 'True':
        original(source, target)
    Path(marker).write_text('ready')
    threading.Event().wait(120)
creation.publish_no_replace = pause
class Controls:
    capture_context = {}
    def check(self): pass
creation.dispatch(Path(home), clock, journal, Controls())
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(home),
            journal.value["id"],
            str(marker),
            str(after_publish),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = SystemClock().monotonic() + 30
        while (
            not marker.exists() and process.poll() is None and SystemClock().monotonic() < deadline
        ):
            threading.Event().wait(0.01)
        assert marker.exists(), process.communicate(timeout=2)
        process.kill()
        process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    journal = BackupJournal.load(journal.path, clock)
    verify_previous(home, clock, journal, Controls())
    assert journal.value["phase"] == ("committed" if after_publish else "failed")
    assert len(BackupStorage(home, clock).list()) == int(after_publish)
    assert (home / "run/backup-work" / journal.value["id"]).exists() is not after_publish


def test_success_removes_verified_plaintext_workspaces(tmp_path):
    from clytie.backup.creation import dispatch
    from clytie.backup.temporary import TemporaryArtifacts

    home, clock = fictional(tmp_path)
    journal, _ = BackupJournal.accept(home, clock, uuid4().hex, "create", {"scope": "instance"})
    result = dispatch(home, clock, journal, Controls())
    assert result["creation"] == "complete"
    temporary = TemporaryArtifacts(home, journal.value["id"]).state()
    assert temporary["state"] == "cleaned", temporary.get("failures")
    assert not (home / "run/backup-work" / journal.value["id"]).exists()


def test_temporary_cleanup_refuses_changed_file_and_unknown_new_file(tmp_path):
    from clytie.backup.temporary import TemporaryArtifacts

    artifacts = TemporaryArtifacts(tmp_path, uuid4().hex)
    root = artifacts.root / "verified"
    root.mkdir(parents=True)
    known = root / "memory.sqlite3"
    known.write_bytes(b"fictional")
    artifacts.begin()
    artifacts.register([root])
    known.write_bytes(b"modified!")
    assert artifacts.cleanup() == "cleanup_blocked"
    assert known.exists()
    known.write_bytes(b"fictional")
    (root / "unknown").write_bytes(b"keep")
    assert artifacts.cleanup() == "cleanup_blocked"
    assert not known.exists()
    assert (root / "unknown").read_bytes() == b"keep"
