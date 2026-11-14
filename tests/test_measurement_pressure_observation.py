"""Small fictional SQLite/owner integration; no formal measurements."""

from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from tests.test_measurement_evidence import evidence, load_tool

from clytie.backup.io_budget import IoBudget
from clytie.backup.io_control import IoControl

load_tool("measurement_io_observation")
module = load_tool("measurement_pressure_observation")


class Clock:
    value = 10.0

    def monotonic(self):
        return self.value


class Io:
    budget = SimpleNamespace(IoBudget=IoBudget)

    def __init__(self):
        self.sequence = 1 << 40
        self.rows = []
        self.budgets = {}
        self.observer = SimpleNamespace(recorder=SimpleNamespace(failed=False), now=lambda: 1.0)

    def identifier(self):
        self.sequence += 1
        return self.sequence

    def budget_identifier(self, budget):
        if budget not in self.budgets:
            self.budgets[budget] = self.identifier()
        return self.budgets[budget]

    def stage(self):
        return "a" * 64

    def event(self, phase, **fields):
        row = dict(id=self.identifier(), process_sequence=0, phase=phase, at_s=1.0, **fields)
        module.validate_record(row)
        self.rows.append(row)


def sample():
    return dict(
        source="owner_control_cache",
        observed_at=datetime(2026, 1, 1, microsecond=123_456, tzinfo=timezone.utc).isoformat(),
        ready_wait_seconds=0.003,
        execution_seconds=0.004,
        finalizer_wait_seconds=0.002,
        finalizer_capacity_blocked=False,
        ordinary=1,
        finalizers=2,
    )


def install(monkeypatch, observation, modules):
    monkeypatch.setattr(module, "_optional", modules.get)
    stack = ExitStack()
    observation.install(stack)
    return stack


def test_raw_pressure_fields_receipt_conversion_and_stale(monkeypatch):
    raw = sample()

    class Owner:
        calls = 0

        def sample_io_pressure(self):
            self.calls += 1
            return raw

    clock, io = Clock(), Io()
    control = IoControl(clock)
    session = control.register(storage_owner=True)
    observation = module.PressureObservation(io)
    modules = {
        "clytie.storage.owner": SimpleNamespace(StorageOwner=Owner),
        "clytie.backup.io_control": SimpleNamespace(IoControl=IoControl),
    }
    with install(monkeypatch, observation, modules):
        owner = Owner()
        returned = owner.sample_io_pressure()
        assert returned is raw and owner.calls == 1
        control.pressure(session, returned)
        received = next(row for row in io.rows if row["phase"] == "budget_pressure")
        assert received["waiting_s"] == raw["ready_wait_seconds"]
        assert received["executing_s"] == raw["execution_seconds"]
        assert received["finalizer_wait"] is True
        assert received["received_monotonic_s"] == 10
        assert received["normal"] is False and received["accepted"] is True
        raw["finalizer_wait_seconds"] = 0
        control.pressure(session, raw)
        clock.value += 0.9
        control.pressure(session, raw)
        clock.value += 0.1
        control.budget._refill()
        assert control.budget._normal is True
        clock.value += 1.01
        control.budget._refill()
        assert control.budget._normal is False
    owner_row = next(row for row in io.rows if row["phase"] == "owner_pressure")
    control_row = next(row for row in io.rows if row["phase"] == "control_pressure")
    assert owner_row["sample_hash"] == control_row["sample_hash"]
    assert owner_row["sample"]["observed_at_unix_us"] == 1_767_225_600 * 1_000_000 + 123_456
    assert received["receipt_id"] == control_row["receipt_id"]
    assert not io.observer.recorder.failed


def test_wal_only_existing_state_and_original_result(monkeypatch):
    first = b"\x37\x7f\x06\x82" + bytes(4) + (4096).to_bytes(4, "big") + bytes(12)
    second = first[:-1] + b"\x01"

    class Growth:
        calls = 0

        def __init__(self, path):
            self.last, self.additional = ((11, 22, first), 4096), 0

        def sample(self):
            self.calls += 1
            self.last, self.additional = ((11, 22, second), 8192), 8192
            return self.additional

    io = Io()
    observation = module.PressureObservation(io)
    with install(
        monkeypatch, observation, {"clytie.backup.view_limits": SimpleNamespace(WalGrowth=Growth)}
    ):
        growth = Growth("fictional-unused")
        assert growth.sample() == 8192 and growth.calls == 1
    initialized, sampled = [row for row in io.rows if row["phase"] == "wal_growth"]
    assert initialized["before"] is None
    assert sampled["before"]["generation_hash"] != sampled["after"]["generation_hash"]
    assert sampled["after"]["additional_bytes"] == sampled["returned_additional_bytes"] == 8192
    assert sampled["after"]["page_size"] == 4096
    assert sampled["wal_id"] == initialized["wal_id"]
    assert "fictional-unused" not in str(io.rows)


def test_geometry_does_not_execute_or_fetch_extra_and_unknown_stays_unknown():
    io = Io()
    observation = module.PressureObservation(io)
    source = object()
    state = dict(id=io.identifier(), source=source, stage=None, page_size=None, page_count=None)

    class Cursor:
        calls = 0

        def fetchone(self):
            self.calls += 1
            return (4096,)

    cursor = Cursor()
    token = observation.copy.set(state)
    try:
        wrapped = observation.cursor(source, "PRAGMA page_size", cursor)
        assert cursor.calls == 0
        assert wrapped.fetchone() == (4096,) and cursor.calls == 1
        assert observation.cursor(source, "SELECT fictional", cursor) is cursor
        observation.geometry(state, "finished", "ok")
    finally:
        observation.copy.reset(token)
    assert state["page_size"] == 4096 and state["page_count"] is None
    assert all(row["internal_read_bytes"] is None for row in io.rows)


def test_format6_strict_and_old_formats_reject_new_events():
    io = Io()
    module.PressureObservation(io).geometry(
        dict(id=io.identifier(), stage=None, page_size=None, page_count=None), "finished", "failed"
    )
    row = io.rows[-1]
    evidence.validate_record("storage-phases.jsonl", row, format_version=6)
    for version in range(1, 6):
        with pytest.raises(evidence.EvidenceError, match="unsupported_pressure_event"):
            evidence.validate_record("storage-phases.jsonl", row, format_version=version)
    for mutation in ({"extra": 1}, {"internal_read_bytes": 0}, {"page_size": True}):
        with pytest.raises(evidence.EvidenceError):
            evidence.validate_record("storage-phases.jsonl", row | mutation, format_version=6)


def test_missing_pressure_and_wal_state_are_not_zero():
    assert module.raw_sample(None) == dict.fromkeys(module.RAW) | {"source": "unknown"}
    assert module.sample_hash(module.raw_sample(None)) is None
    assert module.wal_state(object()) == dict.fromkeys(module.WAL)


def test_invalid_signal_preserves_product_exception(monkeypatch):
    from clytie.backup.io_budget import IoBudgetError

    io = Io()
    budget = IoBudget(Clock())
    budget.set_owners({"fictional-owner"})
    observation = module.PressureObservation(io)
    with install(monkeypatch, observation, {}):
        with pytest.raises(IoBudgetError, match="backup_io_pressure_invalid"):
            budget.pressure("fictional-owner", waiting=-1, executing=0, finalizer_wait=False)
    row = next(row for row in io.rows if row["phase"] == "budget_pressure")
    assert row["accepted"] is False and row["received_monotonic_s"] is None
    assert row["waiting_s"] == -1
    assert not io.observer.recorder.failed


def test_geometry_links_real_backup_id_and_legacy_io_schema_unchanged():
    io = Io()
    observation = module.PressureObservation(io)
    state = dict(id=io.identifier(), stage=None, page_size=4096, page_count=5)
    token = observation.copy.set(state)
    try:
        transfer = io.identifier()
        observation.bind_backup(dict(transfer_id=transfer, stage_id="a" * 64))
    finally:
        observation.copy.reset(token)
    row = io.rows[-1]
    assert row["transfer_id"] == transfer
    assert row["event"] == "backup_started"
    assert row["page_size"] == 4096 and row["page_count"] == 5
    assert row["internal_read_bytes"] is None
    old = dict(
        id=io.identifier(),
        process_sequence=0,
        phase="sqlite_batch",
        at_s=1,
        transfer_id=transfer,
        stage_id="a" * 64,
        event="started",
        outcome="unknown",
        pages_per_step=64,
        sleep_seconds=0,
        callback_present=True,
        status=None,
        remaining_pages=None,
        total_pages=None,
        copied_pages=None,
        internal_read_bytes=None,
    )
    for version in (5, 6):
        evidence.validate_record("storage-phases.jsonl", old, format_version=version)
    with pytest.raises(evidence.EvidenceError):
        module.raw_sample(sample() | {"unexpected": 1})


def test_optional_legacy_storage_parent_absent_never_imports_current_owner(monkeypatch):
    inspected = []

    def find_spec(name):
        inspected.append(name)
        return None

    def forbidden_import(name):
        raise AssertionError("must not import or substitute an absent legacy parent")

    monkeypatch.setattr(module.importlib.util, "find_spec", find_spec)
    monkeypatch.setattr(module, "_optional_module", forbidden_import)
    assert module._optional("clytie.storage.owner") is None
    assert inspected == ["clytie.storage"]


@pytest.mark.parametrize("missing", ["fictional_dependency", "clytie.storage.helper"])
def test_optional_existing_storage_internal_dependency_failure_propagates(monkeypatch, missing):
    import measurement_io_observation as io_module

    def failed_import(name):
        assert name == "clytie.storage.owner"
        raise ModuleNotFoundError(name=missing)

    monkeypatch.setattr(module.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(io_module.importlib, "import_module", failed_import)
    with pytest.raises(ModuleNotFoundError) as error:
        module._optional("clytie.storage.owner")
    assert error.value.name == missing


def test_optional_parent_discovery_failure_is_not_absence(monkeypatch):
    def failed_spec(name):
        raise ModuleNotFoundError(name="fictional_parent_dependency")

    monkeypatch.setattr(module.importlib.util, "find_spec", failed_spec)
    with pytest.raises(ModuleNotFoundError) as error:
        module._optional("clytie.storage.owner")
    assert error.value.name == "fictional_parent_dependency"


def test_optional_missing_leaf_keeps_existing_behavior(monkeypatch):
    import measurement_io_observation as io_module

    def missing_leaf(name):
        raise ModuleNotFoundError(name=name)

    monkeypatch.setattr(module.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(io_module.importlib, "import_module", missing_leaf)
    assert module._optional("clytie.storage.owner") is None
    assert module._optional("clytie.backup.io_control") is None


def real_observer(root):
    recording = load_tool("measurement_recording")
    load_tool("measurement_native_observation")
    observation = load_tool("measurement_observation")
    recorder = recording.Recorder(format_version=6)
    observer = observation.Observation(
        root, recorder, origin=0, timer=lambda: 1, process_sequence=0
    )
    return observer, recorder


def recorded(recorder, name="storage-phases.jsonl", phase=None):
    import json

    values = [json.loads(line) for line in recorder.rows[name].splitlines()]
    return [row for row in values if phase is None or row.get("phase") == phase]


@pytest.mark.parametrize("cancel", [False, True])
async def test_real_snapshot_copy_wal_binding_and_actual_thread_release(
    tmp_path, monkeypatch, cancel
):
    import sqlite3

    from clytie.backup import io_client, view_limits
    from clytie.backup.capture import capture_group
    from clytie.backup.coordination import CaptureCoordinator, CaptureError
    from clytie.sdk.testing import MockClock

    observer, recorder = real_observer(tmp_path)
    views_module = load_tool("measurement_views")
    views = views_module.Views(recorder, now=observer.now)
    paths = {name: tmp_path / (name + ".db") for name in ("memory", "ledger")}
    writers = {}
    for name, path in paths.items():
        db = sqlite3.connect(path)
        writers[name] = db
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE fictional(value INTEGER, payload BLOB)")
        db.execute("INSERT INTO fictional VALUES(1, zeroblob(?))", (12_000,))
        db.commit()
    samples = []
    original_sample = view_limits._wal_sample

    def sampled(path):
        result = original_sample(path)
        samples.append(result)
        return result

    monkeypatch.setattr(view_limits, "_wal_sample", sampled)
    statements = []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced)
    client_calls = []
    client = io_client.IoClient(lambda value, **_: client_calls.append(value))
    view = None
    failure = CaptureError("cancelled")
    checks = []

    def checkpoint():
        checks.append(True)
        # Cancel from a genuine SQLite backup progress callback (after one batch).
        if cancel and recorded(recorder, phase="sqlite_batch"):
            raise failure

    try:
        with observer.install(), views.install(), io_client.io_scope(client):
            view = await capture_group(
                paths, CaptureCoordinator(tmp_path / "capture.lock", MockClock()), epoch="fictional"
            )
            for db in writers.values():
                db.execute("UPDATE fictional SET value=2")
                db.commit()
            if cancel:
                with pytest.raises(CaptureError) as caught:
                    await view.materialize(tmp_path / "output", pages=1, check=checkpoint)
                assert caught.value is failure
            else:
                output = await view.materialize(tmp_path / "output", pages=1, check=checkpoint)
                for path in output.values():
                    db = sqlite3.connect(path)
                    try:
                        assert db.execute("SELECT value FROM fictional").fetchone() == (1,)
                    finally:
                        db.close()
            assert view.closed and not view.resource_pending
            assert not view._worker.thread.is_alive()
            assert view._worker._connections == {}
            views.verify_released()
        assert not recorder.failed
        all_rows = recorded(recorder)
        bindings = recorded(recorder, phase="wal_binding")
        growth = recorded(recorder, phase="wal_growth")
        geometry = recorded(recorder, phase="sqlite_geometry")
        batches = recorded(recorder, phase="sqlite_batch")
        assert len(samples) == len(growth)
        assert len(bindings) == 2
        assert {row["database_kind"] for row in bindings} == {"memory", "ledger"}
        assert len({row["view_id"] for row in bindings}) == 1
        assert all(row["view_id"] == views.workers[view._worker] for row in bindings)
        assert len({row["limits_id"] for row in bindings}) == 1
        assert any(row["after"]["additional_bytes"] > 0 for row in growth)
        known = {row["wal_id"] for row in bindings}
        assert geometry and all(row["wal_id"] in known for row in geometry)
        assert all(row["internal_read_bytes"] is None for row in geometry + batches)
        copies = {row["copy_id"] for row in geometry}
        assert sum(sql == "PRAGMA page_size" for sql in statements) == len(copies)
        assert sum(sql == "PRAGMA page_count" for sql in statements) == len(copies)
        transfers = {row["transfer_id"] for row in batches}
        assert {
            row["transfer_id"] for row in geometry if row["event"] == "backup_started"
        } == transfers
        assert all(
            row["page_size"] > 0 and row["page_count"] > 0
            for row in geometry
            if row["event"] == "backup_started"
        )
        assert batches[-1]["outcome"] == ("cancelled" if cancel else "ok")
        checker = module.WalBindings()
        for row in all_rows:
            evidence.validate_record("storage-phases.jsonl", row, format_version=6)
            checker.add("storage-phases.jsonl", row)
        releases = recorded(recorder, "release-events.jsonl")
        for row in releases:
            checker.add("release-events.jsonl", row)
        checker.complete()
        assert any(
            row["event"] == "exit_verified" and row["resource"] == "thread" for row in releases
        )
        for db in writers.values():
            assert db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    finally:
        if view is not None and not view.closed:
            await view.close()
        for db in writers.values():
            db.close()


async def test_real_owner_control_budget_preserves_pressure_and_exits_thread(tmp_path):
    import threading

    from clytie.backup.io_budget import StageKey
    from clytie.sdk.testing import MockClock
    from clytie.storage.owner import StorageOwner

    observer, recorder = real_observer(tmp_path)
    clock = MockClock()
    control = IoControl(clock)
    session = control.register(storage_owner=True)
    entered, release = threading.Event(), threading.Event()
    closed_threads = []

    class Resources:
        def close(self):
            closed_threads.append(threading.get_ident())

    def blocked(*_):
        entered.set()
        assert release.wait(10)

    with observer.install():
        owner = await StorageOwner.start(Resources, companion_id="companion_a", clock=clock)
        finalizer = owner.reserve_finalizer()
        active = owner.submit(blocked)
        try:
            for _ in range(10_000):
                if entered.is_set():
                    break
                await clock.sleep(0)
            assert entered.is_set()
            waiting = owner.submit(lambda *_: None, finalizer=finalizer)
            clock.advance(0.025)
            sample = owner.sample_io_pressure()
            before = dict(sample)
            control.pressure(session, sample)
            assert sample == before
            assert sample["ready_wait_seconds"] == pytest.approx(0.025)
            assert sample["finalizer_wait_seconds"] == pytest.approx(0.025)
            assert control.budget._normal is False
            stage = await control.budget.acquire(StageKey("fictional", "operation", "a" * 32))
            control.budget.release(stage)
            release.set()
            await active.result()
            await waiting.result()
        finally:
            release.set()
            await active.result()
            owner.release_finalizer(finalizer, executor_finished=True, obligations_complete=True)
            await owner.close()
        assert not owner._thread.is_alive()
        assert closed_threads == [owner._thread.ident]
        assert owner._thread.ident != threading.get_ident()
    owner_rows = recorded(recorder, phase="owner_pressure")
    controls = recorded(recorder, phase="control_pressure")
    budgets = recorded(recorder, phase="budget_pressure")
    assert len(owner_rows) == 1 and len(controls) == 2 and len(budgets) == 1
    assert all(row["sample_hash"] == owner_rows[0]["sample_hash"] for row in controls)
    assert budgets[0]["receipt_id"] == controls[0]["receipt_id"]
    assert budgets[0]["waiting_s"] == sample["ready_wait_seconds"]
    assert budgets[0]["finalizer_wait"] is True
    assert budgets[0]["accepted"] is True
    slots = recorded(recorder, phase="io_slot")
    assert [row["event"] for row in slots] == ["granted", "released"]
    assert {row["budget_id"] for row in slots} == {budgets[0]["pressure_budget_id"]}
    assert all(row["pressure_budget_id"] == slots[0]["budget_id"] for row in controls)
    assert not recorder.failed


def test_wal_binding_unknown_view_does_not_verify_as_complete():
    checker = module.WalBindings()
    io = Io()
    observation = module.PressureObservation(io)

    class Growth:
        pass

    observation.bind_wal(Growth(), "fictional-unused", "ok")
    row = io.rows[-1]
    assert row["view_id"] is None and row["database_hash"] is None
    checker.add("storage-phases.jsonl", dict(phase="wal_growth", wal_id=row["wal_id"]))
    checker.add("storage-phases.jsonl", row)
    with pytest.raises(evidence.EvidenceError, match="wal_view_unconfirmed"):
        checker.complete()


def test_mode_cache_freezes_mutable_owner_membership():
    io = Io()
    observation = module.PressureObservation(io)
    budget = IoBudget(Clock())
    budget._owners = {"owner_a"}
    observation.mode(budget)
    budget._owners.add("owner_b")
    observation.mode(budget)
    assert [row["owners_count"] for row in io.rows] == [1, 2]
    observation.mode(budget)
    assert len(io.rows) == 2


def test_legacy_without_view_limits_does_not_require_new_capture_interface(monkeypatch):
    seen = []

    def optional(name):
        seen.append(name)
        if name == "clytie.backup.capture":
            raise AssertionError("no historical capture substitution")
        return None

    monkeypatch.setattr(module, "_optional", optional)
    with ExitStack() as stack:
        module.PressureObservation(Io()).install(stack)
    assert "clytie.backup.capture" not in seen


async def test_partial_capture_failure_keeps_wal_associations_and_real_release(
    tmp_path, monkeypatch
):
    import sqlite3

    from clytie.backup import view_limits
    from clytie.backup.capture import capture_group
    from clytie.backup.coordination import CaptureCoordinator, CaptureError
    from clytie.sdk.testing import MockClock

    observer, recorder = real_observer(tmp_path)
    views = load_tool("measurement_views").Views(recorder, now=observer.now)
    paths = {name: tmp_path / (name + ".db") for name in ("memory", "ledger")}
    writers = []
    for path in paths.values():
        db = sqlite3.connect(path)
        writers.append(db)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE fictional(value INTEGER)")
        db.commit()
    original = view_limits._wal_sample
    calls = []
    failure = CaptureError("snapshot_wal_unconfirmed")

    def sample(path):
        calls.append(path)
        if len(calls) == 2:
            raise failure
        return original(path)

    monkeypatch.setattr(view_limits, "_wal_sample", sample)
    try:
        with observer.install(), views.install():
            with pytest.raises(CaptureError) as caught:
                await capture_group(
                    paths,
                    CaptureCoordinator(tmp_path / "capture.lock", MockClock()),
                    epoch="fictional",
                )
            assert caught.value is failure
            assert len(views.workers) == 1
            worker = next(iter(views.workers))
            assert not worker.thread.is_alive() and not worker._connections
            views.verify_released()
        bindings = recorded(recorder, phase="wal_binding")
        assert len(bindings) == 2
        assert [row["outcome"] for row in bindings] == ["ok", "failed"]
        assert all(row["view_id"] == views.workers[worker] for row in bindings)
        growth = recorded(recorder, phase="wal_growth")
        assert growth[-1]["after"] == dict.fromkeys(module.WAL)
        assert growth[-1]["returned_additional_bytes"] is None
        releases = recorded(recorder, "release-events.jsonl")
        assert [(row["resource"], row["event"]) for row in releases] == [
            ("thread", "acquired"),
            ("view", "unknown"),
            ("view", "released"),
            ("thread", "exit_verified"),
        ]
        checker = module.WalBindings()
        for row in recorded(recorder):
            checker.add("storage-phases.jsonl", row)
        for row in releases:
            checker.add("release-events.jsonl", row)
        checker.complete()
        assert not recorder.failed
        for db in writers:
            assert db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    finally:
        for db in writers:
            db.close()
