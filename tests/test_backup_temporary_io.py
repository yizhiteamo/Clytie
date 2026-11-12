"""提交前临时登记摘要计费；恢复和已提交清理不新增等待。"""

import hashlib
from uuid import uuid4

import pytest
from tests.test_backup_creation import Controls
from tests.test_backup_source import fictional

from clytie.backup.io_budget import CHUNK
from clytie.backup.io_client import IoClient, io_scope
from clytie.backup.journal import BackupJournal
from clytie.backup.temporary import TemporaryArtifacts


def material(tmp_path):
    manager = TemporaryArtifacts(tmp_path / "home", uuid4().hex)
    manager.begin()
    root = manager.root / "capture"
    root.mkdir(parents=True)
    path = root / "fictional.bin"
    data = b"a" * (CHUNK * 2 + 17)
    path.write_bytes(data)
    return manager, root, path, data


def test_budgeted_registration_reads_real_file_in_bounded_client_batches(tmp_path):
    manager, root, path, data = material(tmp_path)
    calls, checks = [], []

    def request(arguments, cleanup=False):
        calls.append(arguments)
        return {}

    with io_scope(IoClient(request)):
        manager.register([root], budgeted=True, check=lambda: checks.append(True))
    charges = [row for row in calls if row["action"] == "charge"]
    assert [row["size"] for row in charges] == [CHUNK, CHUNK, 17]
    assert {row["direction"] for row in charges} == {"read"}
    assert [row["action"] for row in calls].count("acquire") == 1
    assert calls[-1]["action"] == "release"
    assert checks
    assert manager.state()["files"][str(path)]["sha256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("cancel_at", ["charge", "release"])
def test_cancelled_digest_never_publishes_successful_registration(tmp_path, cancel_at):
    manager, root, path, _ = material(tmp_path)
    original = manager.path.read_bytes()
    cancelled, calls = False, []

    def check():
        if cancelled:
            raise InterruptedError("fictional_cancel")

    def request(arguments, cleanup=False):
        nonlocal cancelled
        calls.append(arguments)
        if arguments["action"] == cancel_at:
            cancelled = True
        return {}

    with io_scope(IoClient(request)), pytest.raises(InterruptedError, match="fictional_cancel"):
        manager.register([root], budgeted=True, check=check)
    assert manager.path.read_bytes() == original
    assert not manager.state()["files"]
    assert path.exists()
    assert calls[-1]["action"] == "release"


def test_default_registration_and_committed_cleanup_never_request_budget(tmp_path):
    manager, root, path, _ = material(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("necessary_completion_requested_budget")

    with io_scope(IoClient(forbidden)):
        manager.register([root])
        assert manager.cleanup() == "cleaned"
    assert not path.exists()


def test_create_and_ordinary_verify_explicitly_budget_precommit_registrations(
    tmp_path, monkeypatch
):
    from clytie.backup import creation

    home, clock = fictional(tmp_path)
    journal, _ = BackupJournal.accept(home, clock, uuid4().hex, "create", {"scope": "instance"})
    registrations = []
    original = TemporaryArtifacts.register

    def register(manager, roots, **kwargs):
        registrations.append((journal.value["phase"], kwargs))
        return original(manager, roots, **kwargs)

    monkeypatch.setattr(TemporaryArtifacts, "register", register)
    result = creation.dispatch(home, clock, journal, Controls())
    assert result["creation"] == "complete"
    assert len(registrations) == 3
    assert all(phase in {"capturing", "verifying"} for phase, _ in registrations)
    assert all(
        options.get("budgeted") is True and callable(options.get("check"))
        for _, options in registrations
    )
    registrations.clear()
    journal, _ = BackupJournal.accept(
        home, clock, uuid4().hex, "verify", {"backup_id": result["backup_id"]}
    )
    assert creation.dispatch(home, clock, journal, Controls())["verification"] == "passed"
    assert len(registrations) == 1
    assert registrations[0][0] == "verifying"
    assert registrations[0][1].get("budgeted") is True
    assert callable(registrations[0][1].get("check"))


def test_publication_and_recovery_registration_do_not_add_budget_wait(tmp_path, monkeypatch):
    from clytie.backup import creation

    home, clock = fictional(tmp_path)
    journal, _ = BackupJournal.accept(home, clock, uuid4().hex, "create", {"scope": "instance"})
    published = False
    original_publish = creation.publish_no_replace

    def publish(source, target):
        nonlocal published
        original_publish(source, target)
        published = True

    def request(arguments, cleanup=False):
        assert not published, "postpublication_budget_wait"
        return {}

    monkeypatch.setattr(creation, "publish_no_replace", publish)
    with io_scope(IoClient(request)):
        assert creation.dispatch(home, clock, journal, Controls())["creation"] == "complete"
    assert published
    original_register = TemporaryArtifacts.register
    restored = []

    def forbidden(*args, **kwargs):
        pytest.fail("recovery_registration_requested_budget")

    def recovering(manager, roots, **kwargs):
        assert not kwargs.get("budgeted", False)
        with io_scope(IoClient(forbidden)):
            result = original_register(manager, roots, **kwargs)
        restored.append(True)
        return result

    monkeypatch.setattr(TemporaryArtifacts, "register", recovering)
    creation.verify_previous(home, clock, journal, Controls())
    assert restored == [True]
    assert journal.value["result"]["creation"] == "complete"
