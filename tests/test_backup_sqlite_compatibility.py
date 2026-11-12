"""Older SQLite JSON flags must not disable safe ledger restore validation."""

import hashlib
import json
import sqlite3

import pytest

from clytie.backup import restore
from clytie.kernel.ledger import Ledger
from clytie.memory.store import MemoryStore
from clytie.sdk.testing import MockClock


def legacy_json(monkeypatch, target):
    connect = sqlite3.connect
    connections, calls = [], []

    def opened(database, *args, **kwargs):
        db = connect(database, *args, **kwargs)
        if str(database).startswith(target.resolve().as_uri()):
            # Deterministic but not INNOCUOUS, matching the affected SQLite flag.
            def extract(raw, path):
                calls.append(path)
                return json.loads(raw).get(path.removeprefix("$."))

            db.create_function("json_extract", 2, extract, deterministic=True)
            db.create_function(
                "dangerous", 1, lambda value: calls.append("dangerous"), deterministic=True
            )
            connections.append(db)
        return db

    monkeypatch.setattr(sqlite3, "connect", opened)
    return connections, calls


def ledger_file(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    book = Ledger(path, "companion_a", MockClock())
    book.db.execute("INSERT INTO record VALUES(?,?)", ("fictional", '{"task_id":"task_a"}'))
    book.db.commit()
    book.close()
    return path


def test_legacy_json_flags_validate_real_ledger_without_changing_file(tmp_path, monkeypatch):
    path = ledger_file(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    _, calls = legacy_json(monkeypatch, path)
    with restore._database(path, restore.LEDGER_TABLES) as db:
        assert db.execute("SELECT id FROM record").fetchall() == [("fictional",)]
        assert db.execute("PRAGMA query_only").fetchone() == (1,)
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("DELETE FROM record")
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("SELECT dangerous(data) FROM record").fetchall()
        assert "dangerous" not in calls
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_legacy_json_flags_accept_ledger_before_optional_health_indexes(tmp_path, monkeypatch):
    path = ledger_file(tmp_path)
    db = sqlite3.connect(path)
    db.execute("DROP INDEX health_tasks")
    db.execute("DROP INDEX health_sending")
    db.commit()
    db.close()
    legacy_json(monkeypatch, path)
    with restore._database(path, restore.LEDGER_TABLES):
        pass


def test_legacy_json_flags_do_not_change_memory_schema_policy(tmp_path, monkeypatch):
    path = tmp_path / "memory.sqlite3"
    store = MemoryStore(path, clock=MockClock(), config={}, instance={})
    store.close()
    legacy_json(monkeypatch, path)
    with restore._database(path, restore.MEMORY_TABLES) as restored:
        assert restored.execute("PRAGMA trusted_schema").fetchone() == (0,)


def test_legacy_json_flags_full_restore_checks_progress_and_apply(tmp_path, monkeypatch):
    from tests.test_backup_restore import fixture_package

    manager, package, key, target, work = fixture_package(tmp_path, monkeypatch)
    _, calls = legacy_json(monkeypatch, tmp_path)
    plan = manager.plan(package, key, target, work)
    state = manager.apply(plan["id"])
    assert state["data_state"] == "ready_for_switch"
    assert manager.apply(plan["id"]) == state
    assert "dangerous" not in calls


@pytest.mark.parametrize("mutation", ["index", "view", "trigger", "check", "extra_index"])
def test_legacy_json_flags_do_not_trust_modified_schema(tmp_path, monkeypatch, mutation):
    path = ledger_file(tmp_path)
    with sqlite3.connect(path) as db:
        db.create_function("dangerous", 1, lambda value: value, deterministic=True)
        if mutation == "index":
            db.execute("DROP INDEX task_id")
            db.execute("CREATE UNIQUE INDEX task_id ON record(dangerous(data))")
        elif mutation == "view":
            db.execute("CREATE VIEW injected AS SELECT dangerous(data) FROM record")
        elif mutation == "trigger":
            db.execute(
                "CREATE TRIGGER injected AFTER INSERT ON record BEGIN SELECT dangerous(new.data); END"
            )
        elif mutation == "check":
            db.execute("PRAGMA writable_schema=ON")
            db.execute(
                "UPDATE sqlite_master SET sql=replace(sql,'data TEXT NOT NULL','data TEXT NOT NULL CHECK(dangerous(data))') WHERE name='record'"
            )
        else:
            db.execute("CREATE INDEX injected ON record(data)")
    db.close()  # Checkpoint the mutation before opening the immutable snapshot.
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    _, calls = legacy_json(monkeypatch, path)
    with pytest.raises((restore.RestoreError, sqlite3.DatabaseError)):
        with restore._database(path, restore.LEDGER_TABLES):
            pytest.fail("modified schema accepted")
    assert not calls
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
