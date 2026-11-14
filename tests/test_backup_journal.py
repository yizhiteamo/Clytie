import pytest

from clytie.backup.journal import BackupJournal, JournalError
from clytie.sdk.testing import MockClock


def test_journal_idempotency_does_not_reexecute_or_expose_arguments(tmp_path):
    clock = MockClock()
    identifier = "a" * 32
    item, created = BackupJournal.accept(
        tmp_path, clock, identifier, "restore_apply", {"key_path": "fictional"}
    )
    assert created
    again, created = BackupJournal.accept(
        tmp_path, clock, identifier, "restore_apply", {"key_path": "changed"}
    )
    assert not created
    assert again.value["arguments"] == {"key_path": "fictional"}
    assert "arguments" not in item.public()
    assert "fictional" not in str(item.public())
    with pytest.raises(JournalError):
        BackupJournal.accept(tmp_path, clock, identifier, "create", {})


def test_committed_result_survives_late_cancel(tmp_path):
    item, _ = BackupJournal.accept(tmp_path, MockClock(), "b" * 32, "create", {})
    item.phase("publishing")
    item.commit({"creation": "complete", "verification": "passed", "mirror": "failed"})
    item.request_cancel()
    result = BackupJournal.load(item.path, item.clock).public()
    assert result["phase"] == "committed"
    assert result["result"]["creation"] == "complete"
    assert result["result"]["mirror"] == "failed"
    with pytest.raises(JournalError):
        item.phase("cancelled")


def test_cancel_request_is_idempotent(tmp_path):
    clock = MockClock()
    item, _ = BackupJournal.accept(tmp_path, clock, "c" * 32, "create", {})
    item.request_cancel()
    first = item.value["cancel_at"]
    clock.advance(60)
    item.request_cancel()
    assert item.value["cancel_at"] == first


@pytest.mark.parametrize("value", ["../outside", "", "a" * 31, "z" * 32])
def test_operation_identifier_is_confined(tmp_path, value):
    with pytest.raises(JournalError):
        BackupJournal.accept(tmp_path, MockClock(), value, "create", {})
