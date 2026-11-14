from clytie.api.management_journal import Journal
from clytie.backup.previews import PreviewCatalog
from clytie.sdk.testing import MockClock


def test_admission_journal_gap_releases_only_verified_operation_reference(tmp_path):
    clock = MockClock()
    source = tmp_path / "fictional.enc"
    source.write_bytes(b"fictional")
    catalog = PreviewCatalog(tmp_path, clock)
    plan_id, operation_id = "a" * 32, "b" * 32
    catalog.reference(source, "generation", kind="generation")
    catalog.publish("memory", plan_id, source, "generation", target="companion_a", fingerprint={})
    catalog.accept("memory", plan_id, operation_id)
    catalog.reference(source, "another_reader", kind="reader")
    journal = Journal.create(tmp_path, "companion_a", "import/apply", clock, operation_id)
    # The acceptance is durable, but execution has not yet touched the database.
    assert "backup_execution" not in journal.value
    assert journal.recover()["state"] == "interrupted"
    remaining = catalog.holders(source)
    assert len(remaining) == 1 and remaining[0]["kind"] == "reader"
    assert journal.recover()["state"] == "interrupted"
    assert catalog.holders(source) == remaining


def test_blocked_recovery_keeps_source_execution_protection(tmp_path):
    clock = MockClock()
    source = tmp_path / "fictional.enc"
    source.write_bytes(b"fictional")
    catalog = PreviewCatalog(tmp_path, clock)
    plan_id, operation_id = "c" * 32, "d" * 32
    catalog.reference(source, "generation", kind="generation")
    catalog.publish("memory", plan_id, source, "generation", target="companion_a", fingerprint={})
    catalog.accept("memory", plan_id, operation_id)
    journal = Journal.create(tmp_path, "companion_a", "import/apply", clock, operation_id)
    journal.value["database"] = str(tmp_path / "missing.sqlite3")
    journal.save()
    assert journal.recover()["state"] == "recovery_blocked"
    assert catalog.holders(source)[0]["kind"] == "execution"
