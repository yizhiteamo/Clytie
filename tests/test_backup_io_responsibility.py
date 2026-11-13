import importlib
import importlib.util
import json
import multiprocessing
from pathlib import Path

import pytest

from clytie.api.management_journal import Journal
from clytie.backup.io_budget import IoBudgetError
from clytie.backup.journal import BackupJournal
from clytie.plugin_host.locking import process_identity
from clytie.sdk.testing import MockClock


@pytest.fixture
def responsibility():
    name = "clytie.backup.io_responsibility"
    assert importlib.util.find_spec(name) is not None, "persistent I/O responsibility is missing"
    return importlib.import_module(name)


def test_live_external_blocks_but_same_controller_can_queue(tmp_path, responsibility):
    ledger = responsibility.Responsibilities(tmp_path, MockClock())
    assert ledger.check("controller_a")["state"] == "ready"
    generation = ledger.register(
        "controller_a", "session_a", "operation_a", process_identity(), "operation"
    )
    assert isinstance(generation, str) and generation
    assert ledger.check("controller_a")["state"] == "ready"
    assert (
        ledger.register("controller_a", "session_b", "operation_b", process_identity(), "capture")
        != generation
    )
    blocked = ledger.check("controller_b")
    assert blocked["state"] == "blocked"
    assert {item["operation"] for item in blocked["blockers"]} == {"operation_a", "operation_b"}
    assert "started_at" not in json.dumps(blocked) and "pid" not in json.dumps(blocked)
    with pytest.raises(IoBudgetError):
        ledger.register("controller_b", "session_c", "operation_c", process_identity(), "operation")


def test_release_requires_confirmation_and_tombstone_survives_reload(tmp_path, responsibility):
    ledger = responsibility.Responsibilities(tmp_path, MockClock())
    arguments = ("controller_a", "session_a", "operation_a", process_identity(), "operation")
    ledger.register(*arguments)
    with pytest.raises(IoBudgetError):
        ledger.release(*arguments[:3])
    assert ledger.check("controller_b")["state"] == "blocked"
    ledger.release(*arguments[:3], confirmed=True)
    replacement = responsibility.Responsibilities(tmp_path, MockClock())
    assert replacement.check("controller_b")["state"] == "ready"
    with pytest.raises(IoBudgetError):
        replacement.register(*arguments)


@pytest.mark.parametrize("alive", [True, None, False])
def test_recheck_uses_three_states_without_replaying(tmp_path, responsibility, monkeypatch, alive):
    ledger = responsibility.Responsibilities(tmp_path, MockClock())
    ledger.register("controller_a", "session_a", "operation_a", process_identity(), "operation")
    monkeypatch.setattr(responsibility, "executor_alive", lambda identity: alive)
    state = ledger.check("controller_b")
    assert state["state"] == ("ready" if alive is False else "blocked")
    if alive is None:
        assert state["blockers"][0]["reason"] == "executor_unverified"
    if alive is False:
        assert state["executions"][0]["state"] == "ended"


def test_ended_settles_only_selected_session(tmp_path, responsibility):
    ledger = responsibility.Responsibilities(tmp_path, MockClock())
    for session in ("session_a", "session_b"):
        ledger.register("controller_a", session, session, process_identity(), "operation")
    ledger.ended("controller_a", "session_a")
    assert {row["operation"] for row in ledger.check("controller_b")["blockers"]} == {"session_b"}


def test_reserve_precedes_journal_and_bind_requires_same_execution(tmp_path, responsibility):
    clock = MockClock()
    ledger = responsibility.Responsibilities(tmp_path, clock)
    operation = "a" * 32
    generation = ledger.reserve("controller_a", "session_a", operation, "operation")
    Journal.create(tmp_path, "companion_a", "export", clock, operation)
    assert ledger.check("controller_a")["state"] == "ready"
    assert ledger.check("controller_b")["state"] == "blocked"
    with pytest.raises(IoBudgetError):
        ledger.bind("controller_b", "session_a", operation, process_identity())
    assert ledger.bind("controller_a", "session_a", operation, process_identity()) == generation
    assert ledger.check("controller_b")["blockers"][0]["reason"] == "executor_active"


def test_reserved_missing_identity_is_not_released_by_process_probe(
    tmp_path, responsibility, monkeypatch
):
    ledger = responsibility.Responsibilities(tmp_path, MockClock())
    ledger.reserve("controller_a", "session_a", "operation_a", "operation")
    monkeypatch.setattr(responsibility, "executor_alive", lambda identity: False)
    assert ledger.check("controller_b")["state"] == "blocked"
    ledger.release("controller_a", "session_a", "operation_a", confirmed=True)
    assert ledger.check("controller_b")["state"] == "ready"


@pytest.mark.parametrize("kind", ["management", "backup"])
def test_terminal_legacy_live_executor_still_blocks(tmp_path, responsibility, kind):
    clock = MockClock()
    if kind == "management":
        journal = Journal.create(tmp_path, "companion_a", "export", clock)
        journal.phase("cancelled")
    else:
        journal, _ = BackupJournal.accept(tmp_path, clock, "a" * 32, "verify", {})
        journal.phase("cancelled", executor=process_identity())
    original = journal.path.read_bytes()
    ledger = responsibility.Responsibilities(tmp_path, clock)
    state = ledger.check("controller_b")
    assert state["state"] == "blocked"
    assert state["blockers"][0]["operation"] == journal.value["id"]
    assert journal.path.read_bytes() == original


def test_dead_original_and_recovery_have_separate_persistent_records(
    tmp_path, responsibility, monkeypatch
):
    clock = MockClock()
    journal = Journal.create(tmp_path, "companion_a", "export", clock)
    original = journal.path.read_bytes()
    ledger = responsibility.Responsibilities(tmp_path, clock)
    monkeypatch.setattr(responsibility, "executor_alive", lambda identity: False)
    assert ledger.check("controller_b")["state"] == "ready"
    monkeypatch.setattr(responsibility, "executor_alive", lambda identity: True)
    ledger.register(
        "controller_b", "session_b", journal.value["id"], process_identity(), "recovery"
    )
    state = ledger.check("controller_c")
    assert {(row["role"], row["state"]) for row in state["executions"]} == {
        ("operation", "ended"),
        ("recovery", "active"),
    }
    assert journal.path.read_bytes() == original


@pytest.mark.parametrize("damage", ["missing_index", "broken_index", "missing_anchor"])
def test_damaged_evidence_never_initializes_empty(tmp_path, responsibility, damage):
    ledger = responsibility.Responsibilities(tmp_path, MockClock())
    ledger.register("controller_a", "session_a", "operation_a", process_identity(), "operation")
    if damage == "missing_index":
        ledger.index_path.unlink()
    elif damage == "broken_index":
        ledger.index_path.write_text("{", encoding="utf-8")
    else:
        ledger.anchor_path.unlink()
    state = responsibility.Responsibilities(tmp_path, MockClock()).check("controller_b")
    assert state["state"] == "blocked"
    assert state["blockers"][0]["reason"] == "responsibility_evidence_unverified"
    with pytest.raises(IoBudgetError):
        ledger.register("controller_b", "session_b", "operation_b", process_identity(), "operation")


def test_missing_record_is_detected_by_persistent_index(tmp_path, responsibility):
    ledger = responsibility.Responsibilities(tmp_path, MockClock())
    generation = ledger.register(
        "controller_a", "session_a", "operation_a", process_identity(), "operation"
    )
    document = json.loads(ledger.index_path.read_bytes())
    del document["records"][generation]
    ledger.index_path.write_text(json.dumps(document), encoding="utf-8")
    assert ledger.check("controller_b")["state"] == "blocked"


def test_initialization_failure_is_sticky(tmp_path, responsibility, monkeypatch):
    ledger = responsibility.Responsibilities(tmp_path, MockClock())
    original = responsibility.atomic_write

    def fail_index(path, *args, **kwargs):
        if Path(path) == ledger.index_path:
            raise OSError("synthetic interrupted initialization")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(responsibility, "atomic_write", fail_index)
    assert ledger.check("controller_a")["state"] == "blocked"
    monkeypatch.setattr(responsibility, "atomic_write", original)
    assert (
        responsibility.Responsibilities(tmp_path, MockClock()).check("controller_b")["state"]
        == "blocked"
    )


def test_own_active_unknown_identity_blocks_new_work(tmp_path, responsibility, monkeypatch):
    ledger = responsibility.Responsibilities(tmp_path, MockClock())
    ledger.register("controller_a", "session_a", "operation_a", process_identity(), "operation")
    monkeypatch.setattr(responsibility, "executor_alive", lambda identity: None)
    assert ledger.check("controller_a")["state"] == "blocked"
    with pytest.raises(IoBudgetError):
        ledger.reserve("controller_a", "session_b", "operation_b", "operation")


def test_legacy_recovery_executor_is_not_hidden_by_original(tmp_path, responsibility, monkeypatch):
    clock = MockClock()
    journal = Journal.create(tmp_path, "companion_a", "export", clock)
    original_identity = {**process_identity(), "started_at": "2000-01-01T00:00:00+00:00"}
    journal.value["executor"] = original_identity
    journal.value["recovery_executor"] = process_identity()
    journal.save()
    monkeypatch.setattr(
        responsibility, "executor_alive", lambda identity: identity != original_identity
    )
    ledger = responsibility.Responsibilities(tmp_path, clock)
    state = ledger.check("controller_a")
    assert state["state"] == "blocked"
    assert {(row["role"], row["state"]) for row in state["executions"]} == {
        ("operation", "ended"),
        ("recovery", "active"),
    }


def test_missing_observed_legacy_journal_remains_unknown(tmp_path, responsibility):
    clock = MockClock()
    journal = Journal.create(tmp_path, "companion_a", "export", clock)
    ledger = responsibility.Responsibilities(tmp_path, clock)
    assert ledger.check("controller_a")["state"] == "blocked"
    journal.path.unlink()
    assert "journal_unverified" in {
        row["reason"] for row in ledger.check("controller_a")["blockers"]
    }


def test_pid_reuse_is_confirmed_exit_without_replaying(tmp_path, responsibility, monkeypatch):
    import clytie.api.management_journal as journals

    ledger = responsibility.Responsibilities(tmp_path, MockClock())
    identity = process_identity()
    ledger.register("controller_a", "session_a", "operation_a", identity, "operation")
    monkeypatch.setattr(
        journals,
        "process_identity",
        lambda pid: {"pid": pid, "started_at": "2000-01-01T00:00:00+00:00"},
    )
    assert ledger.check("controller_b")["state"] == "ready"


def test_valid_json_identity_corruption_does_not_prove_exit(tmp_path, responsibility):
    ledger = responsibility.Responsibilities(tmp_path, MockClock())
    generation = ledger.register(
        "controller_a", "session_a", "operation_a", process_identity(), "operation"
    )
    document = json.loads(ledger.index_path.read_bytes())
    document["records"][generation]["identity"]["started_at"] = "2000-01-01T00:00:00+00:00"
    ledger.index_path.write_text(json.dumps(document), encoding="utf-8")
    assert ledger.check("controller_b")["state"] == "blocked"


def test_lost_stable_lock_and_interrupted_first_lock_are_not_fresh(tmp_path, responsibility):
    ledger = responsibility.Responsibilities(tmp_path, MockClock())
    ledger.lock_path.parent.mkdir(parents=True)
    ledger.lock_path.write_bytes(b"\0")
    assert ledger.check("controller_a")["state"] == "blocked"


def test_released_session_cannot_be_reused_by_new_controller(tmp_path, responsibility):
    ledger = responsibility.Responsibilities(tmp_path, MockClock())
    ledger.register("controller_a", "session_a", "operation_a", process_identity(), "operation")
    ledger.release("controller_a", "session_a", "operation_a", confirmed=True)
    with pytest.raises(IoBudgetError, match="execution_reused"):
        ledger.register("controller_b", "session_a", "operation_a", process_identity(), "operation")


def test_missing_stable_lock_does_not_create_replacement(tmp_path, responsibility):
    ledger = responsibility.Responsibilities(tmp_path, MockClock())
    assert ledger.check("controller_a")["state"] == "ready"
    ledger.lock_path.unlink()
    assert ledger.check("controller_b")["state"] == "blocked"
    assert not ledger.lock_path.exists()


def test_legacy_invalid_birth_time_is_unknown_not_pid_reuse(tmp_path, responsibility):
    clock = MockClock()
    journal = Journal.create(tmp_path, "companion_a", "export", clock)
    journal.value["executor"]["started_at"] = "invalid"
    journal.save()
    state = responsibility.Responsibilities(tmp_path, clock).check("controller_a")
    assert state["state"] == "blocked"
    assert state["blockers"][0]["reason"] == "executor_unverified"


def test_changed_legacy_executor_does_not_hide_new_untracked_process(
    tmp_path, responsibility, monkeypatch
):
    clock = MockClock()
    journal, _ = BackupJournal.accept(tmp_path, clock, "a" * 32, "verify", {})
    original = {**process_identity(), "started_at": "2000-01-01T00:00:00+00:00"}
    journal.phase("verifying", executor=original)
    monkeypatch.setattr(responsibility, "executor_alive", lambda identity: identity != original)
    ledger = responsibility.Responsibilities(tmp_path, clock)
    assert ledger.check("controller_a")["state"] == "ready"
    journal.phase("verifying", executor=process_identity())
    state = ledger.check("controller_b")
    assert state["state"] == "blocked"
    assert state["blockers"][0]["reason"] == "journal_executor_changed"


def _contend(home, name, ready, go, result, finish):
    from clytie.backup.io_responsibility import Responsibilities

    ledger = Responsibilities(Path(home), MockClock())
    ready.put(name)
    go.wait(10)
    try:
        ledger.register(name, name, name, process_identity(), "operation")
    except IoBudgetError:
        result.put("blocked")
    else:
        result.put("registered")
    finish.wait(10)


def test_real_processes_cannot_both_admit(tmp_path, responsibility, monkeypatch):
    # pytest.exe does not add cwd like python -m pytest. Spawn must import the
    # same test module by its qualified name when unpickling the target.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    context = multiprocessing.get_context("spawn")
    ready, result = context.Queue(), context.Queue()
    go, finish = context.Event(), context.Event()
    processes = [
        context.Process(target=_contend, args=(tmp_path, name, ready, go, result, finish))
        for name in ("controller_a", "controller_b")
    ]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            ready.get(timeout=10)
        go.set()
        outcomes = [result.get(timeout=10) for _ in processes]
        assert outcomes.count("registered") == 1
        assert outcomes.count("blocked") == 1
    finally:
        finish.set()
        for process in processes:
            process.join(10)
            if process.is_alive():
                process.kill()
                process.join(10)
        ready.close()
        result.close()
