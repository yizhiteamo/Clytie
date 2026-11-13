"""Fictional host I/O observations; no arbitrary-plugin coverage claim."""

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from tests.test_measurement_evidence import evidence, load_tool
from tests.test_measurement_manifest import contents, manifest_v4, save_manifest

from clytie.backup import io_client
from clytie.backup.io_budget import CHUNK, IoBudget, StageKey
from clytie.sdk.testing import MockClock

recording = load_tool("measurement_recording")
load_tool("measurement_sample_validation")
io_observation = load_tool("measurement_io_observation")
load_tool("measurement_native_observation")
observation = load_tool("measurement_observation")


def observer_for(root, sequence=0):
    recorder = recording.Recorder(format_version=5)
    observer = observation.Observation(
        root, recorder, origin=0, timer=lambda: 1, process_sequence=sequence
    )
    return observer, recorder


def rows(recorder, phase=None):
    result = [json.loads(line) for line in recorder.rows["storage-phases.jsonl"].splitlines()]
    return [row for row in result if phase is None or row["phase"] == phase]


def run_v5(directory, sequences=(0,)):
    run = manifest_v4(directory)
    run.update(
        format=5, observer_sequences=list(sequences), scenario="normal", execution_id="ac" * 32
    )
    save_manifest(directory, run)
    (directory / "release-events.jsonl").write_text(
        "".join(
            json.dumps(dict(id=1, at_s=at, resource=resource, event=event)) + "\n"
            for resource, event, at in (
                ("thread", "acquired", 0),
                ("reference", "acquired", 0),
                ("reference", "released", 2),
                ("thread", "exit_verified", 2),
            )
        ),
        encoding="utf-8",
    )
    return run


def test_actual_host_file_calls_are_distinct_from_requested_and_charged_sizes(tmp_path):
    observer, recorder = observer_for(tmp_path)
    requests = []
    client = io_client.IoClient(lambda value, **_: requests.append(value))
    source = tmp_path / "fictional.bin"
    source.write_bytes(b"x" * (CHUNK + 7))
    with observer.install(), io_client.io_scope(client), client.stage():
        with source.open("rb") as incoming, (tmp_path / "copy.bin").open("wb") as outgoing:
            data = io_client.io_read(incoming, CHUNK * 2)
            assert io_client.io_write(outgoing, data) == len(data)
    batches = rows(recorder, "file_batch")
    finished = [row for row in batches if row["event"] == "finished"]
    assert [(row["direction"], row["actual_bytes"]) for row in finished] == [
        ("read", CHUNK),
        ("read", 7),
        ("write", CHUNK),
        ("write", 7),
    ]
    stage = next(value["stage"] for value in requests if value["action"] == "acquire")
    assert all(row["stage_id"] == hashlib.sha256(stage.encode()).hexdigest() for row in batches)
    assert all(row["process_sequence"] == 0 for row in batches)
    assert str(tmp_path).encode() not in recorder.rows["storage-phases.jsonl"]
    before = len(batches)
    with observer.install(), source.open("rb") as incoming:
        incoming.read(CHUNK * 2)  # Bypassing the host interface stays unobserved.
    assert len(rows(recorder, "file_batch")) == before


def test_unbudgeted_host_short_read_keeps_actual_count_and_original_argument(tmp_path):
    observer, recorder = observer_for(tmp_path)
    source = tmp_path / "fictional.bin"
    source.write_bytes(b"abc")
    with observer.install(), source.open("rb") as incoming:
        assert io_client.io_read(incoming, CHUNK * 2) == b"abc"
    finished = rows(recorder, "file_batch")[-1]
    assert finished["requested_bytes"] == CHUNK * 2
    assert finished["actual_bytes"] == 3
    assert finished["stage_id"] is None


def test_sqlite_progress_reports_real_batches_without_invented_internal_bytes(tmp_path):
    observer, recorder = observer_for(tmp_path)
    callbacks = []
    with observer.install():
        source = sqlite3.connect(tmp_path / "source.db")
        target = sqlite3.connect(tmp_path / "target.db")
        try:
            source.execute("CREATE TABLE fixture(value)")
            source.executemany("INSERT INTO fixture VALUES (?)", [(b"a" * 4096,)] * 8)
            source.commit()
            source.backup(target, pages=2, progress=lambda *args: callbacks.append(args), sleep=0)
            assert target.execute("SELECT count(*) FROM fixture").fetchone()[0] == 8
        finally:
            source.close()
            target.close()
    events = rows(recorder, "sqlite_batch")
    progress = [row for row in events if row["event"] == "progress"]
    assert [(r["status"], r["remaining_pages"], r["total_pages"]) for r in progress] == callbacks
    assert progress and all(0 < row["copied_pages"] <= 2 for row in progress)
    assert sum(row["copied_pages"] for row in progress) == progress[-1]["total_pages"]
    assert all(row["internal_read_bytes"] is None for row in events)
    assert events[0]["event"] == "started" and events[-1]["event"] == "finished"


def test_sqlite_callback_cancellation_is_not_replaced(tmp_path):
    observer, recorder = observer_for(tmp_path)
    failure = asyncio.CancelledError()

    def stop(*_):
        raise failure

    with observer.install():
        source = sqlite3.connect(tmp_path / "source.db")
        target = sqlite3.connect(tmp_path / "target.db")
        try:
            source.execute("CREATE TABLE fixture(value)")
            source.commit()
            with pytest.raises(asyncio.CancelledError) as caught:
                source.backup(target, pages=1, progress=stop, sleep=0)
            assert caught.value is failure
        finally:
            source.close()
            target.close()
    assert rows(recorder, "sqlite_batch")[-1]["outcome"] == "cancelled"


async def test_real_budget_grants_and_trusted_releases_keep_fifo_and_charge_separate(tmp_path):
    observer, recorder = observer_for(tmp_path)
    budget = IoBudget(MockClock())
    with observer.install():
        first = await budget.acquire(StageKey("worker_a", "operation_a", "a" * 32))
        waiting = asyncio.create_task(budget.acquire(StageKey("worker_b", "operation_b", "b" * 32)))
        await budget.clock.sleep(0)
        await budget.charge(first, 1, "read", 17)
        await budget.charge(first, 1, "read", 17)
        budget.disconnected("worker_a")
        assert not waiting.done()
        budget.executor_ended("worker_a", confirmed=True)
        second = await waiting
        budget.release(second)
    assert [row["event"] for row in rows(recorder, "io_slot")] == [
        "granted",
        "released",
        "granted",
        "released",
    ]
    charges = rows(recorder, "io_charge")
    assert [row["event"] for row in charges] == ["started", "finished"]
    assert charges[-1]["charged_bytes"] == 17
    assert not rows(recorder, "file_batch")


@pytest.mark.parametrize("sequence", [None, True, -1, 256, "1"])
def test_format5_requires_explicit_bounded_process_sequence(tmp_path, sequence):
    recorder = recording.Recorder(format_version=5)
    with pytest.raises(evidence.EvidenceError):
        observation.Observation(tmp_path, recorder, origin=0, process_sequence=sequence)


@pytest.mark.parametrize("sequences", [[], [0, 0], [True], [256], [1], list(range(257))])
def test_format5_manifest_requires_unique_complete_bounded_sequence_plan(tmp_path, sequences):
    run = run_v5(tmp_path / "source", sequences)
    with pytest.raises(evidence.EvidenceError):
        evidence.validate_run(run)


def test_format5_exact_archive_and_unknown_field_rejection(tmp_path):
    source = tmp_path / "source"
    run = run_v5(source, (0, 1))
    observer, recorder = observer_for(tmp_path, 1)
    path = tmp_path / "fictional.bin"
    path.write_bytes(b"data")
    with observer.install(), path.open("rb") as stream:
        io_client.io_read(stream, 5)
    raw = bytes(recorder.rows["storage-phases.jsonl"])
    (source / "storage-phases.jsonl").write_bytes(raw)
    identity = evidence.native_identity().generate()
    evidence.seal(source, tmp_path / "sealed", str(identity.to_public()), run)
    state = evidence.open_bundle(
        tmp_path / "sealed/measurements.age",
        tmp_path / "opened",
        identity,
        run,
        private_scan=lambda _: "pass",
    )
    assert state["measurement_acceptance"] == "ineligible"
    assert (tmp_path / "opened/raw/storage-phases.jsonl").read_bytes() == raw
    broken = rows(recorder)[0]
    broken["path"] = "fictional.bin"
    with pytest.raises(evidence.EvidenceError, match="invalid_fields"):
        evidence.validate_record("storage-phases.jsonl", broken, format_version=5)
    with pytest.raises(evidence.EvidenceError):
        evidence.validate_record("storage-phases.jsonl", rows(recorder)[0], format_version=4)


def test_pass_requires_io_lifecycle_completion_but_failure_preserves_start(tmp_path):
    source = tmp_path / "source"
    run = run_v5(source)
    observer, recorder = observer_for(tmp_path)
    path = tmp_path / "fictional.bin"
    path.write_bytes(b"data")
    with observer.install(), path.open("rb") as stream:
        io_client.io_read(stream, 5)
    first = next(
        line
        for line in bytes(recorder.rows["storage-phases.jsonl"]).splitlines(keepends=True)
        if json.loads(line)["phase"] == "file_batch"
    )
    (source / "storage-phases.jsonl").write_bytes(first)
    evidence.validate_files(contents(source), run)
    run["result"] = "pass"
    save_manifest(source, run)
    with pytest.raises(evidence.EvidenceError, match="io_lifecycle_incomplete"):
        evidence.validate_files(contents(source), run)


def test_frozen_worker_sequences_have_exact_distinct_targets_and_cannot_be_reused(
    tmp_path, monkeypatch
):
    from clytie.backup import worker as product_worker

    worker = load_tool("measurement_worker")
    (tmp_path / "home").mkdir()
    (tmp_path / "measurement-fixture.json").write_text(
        json.dumps({"format": 1, "source": "generated_fictional_only"}), encoding="utf-8"
    )
    execution = "ad" * 32
    (tmp_path / "measurement-observers.json").write_text(
        json.dumps({"format": 1, "execution_id": execution, "observer_sequences": [0, 1, 2]}),
        encoding="utf-8",
    )
    called = []
    monkeypatch.setattr(product_worker, "main", lambda: called.append(True))
    for sequence in (1, 2):
        args = [str(tmp_path), "a" * 32, "0", "5", str(sequence), execution]
        assert worker.main(args) == 0
        target = tmp_path / f"child-storage-phases-{sequence}.jsonl"
        original = target.read_bytes()
        assert original and all(
            json.loads(line)["id"] >> 40 == sequence + 1 for line in original.splitlines()
        )
        assert worker.main(args) == 2
        assert target.read_bytes() == original
    assert len(called) == 2
    assert worker.main([str(tmp_path), "a" * 32, "0", "5", "3", execution]) == 2
    assert worker.main([str(tmp_path), "a" * 32, "0", "5", "0", execution]) == 2
    assert worker.main([str(tmp_path), "a" * 32, "0", "5", "1", "af" * 32]) == 2


def test_format5_mechanism_freezes_single_candidate_and_scenario(tmp_path):
    run = run_v5(tmp_path / "source")
    run.update(phase="mechanism", method="legal_storage_v1", scenario="cancel")
    run["execution"].update(order=["candidate"], position=0)
    evidence.validate_run(run)
    for field, value in (("scenario", "other"), ("execution_id", True), ("arm", "original")):
        with pytest.raises(evidence.EvidenceError):
            evidence.validate_run({**run, field: value})


def test_legacy_capabilities_do_not_import_candidate_helpers(tmp_path, monkeypatch):
    actual = io_observation.importlib.import_module
    requested = []

    def selected_source(name):
        requested.append(name)
        if name in {"clytie.backup.io_client", "clytie.backup.io_budget"}:
            raise ModuleNotFoundError(name=name)
        return actual(name)

    monkeypatch.setattr(io_observation.importlib, "import_module", selected_source)
    observer, recorder = observer_for(tmp_path)
    with observer.install():
        source = sqlite3.connect(tmp_path / "legacy.db")
        target = sqlite3.connect(tmp_path / "copy.db")
        try:
            source.execute("CREATE TABLE fixture(value)")
            source.commit()
            source.backup(target, pages=1, sleep=0)
        finally:
            source.close()
            target.close()
    capability = rows(recorder, "io_capability")[0]
    assert capability["host_file_batches"] is False and capability["budget_slots"] is False
    assert capability["sqlite_backup"] is True
    assert rows(recorder, "sqlite_batch")
    assert all("candidate" not in name for name in requested)


def test_real_product_sqlite_copy_keeps_effective_pages_and_charge_parameters(tmp_path):
    from clytie.backup.sqlite_io import copy_database

    observer, recorder = observer_for(tmp_path)
    requests = []
    client = io_client.IoClient(lambda request, **_: requests.append(request))
    with observer.install():
        source = sqlite3.connect(tmp_path / "source.db")
        target = sqlite3.connect(tmp_path / "copy.db")
        try:
            source.execute("CREATE TABLE fixture(value)")
            source.executemany("INSERT INTO fixture VALUES (?)", [(b"a" * 4096,)] * 80)
            source.commit()
            page_size = source.execute("PRAGMA page_size").fetchone()[0]
            with io_client.io_scope(client):
                copy_database(source, target, pages=128, check=lambda: None)
            assert target.execute("SELECT count(*) FROM fixture").fetchone()[0] == 80
        finally:
            source.close()
            target.close()
    events = rows(recorder, "sqlite_batch")
    assert all(row["pages_per_step"] == CHUNK // page_size for row in events)
    progress = [row for row in events if row["event"] == "progress"]
    assert len(progress) > 1
    assert all(row["copied_pages"] * page_size <= CHUNK for row in progress)
    charges = [row for row in requests if row["action"] == "charge"]
    assert sum(row["size"] for row in charges) == progress[-1]["total_pages"] * page_size * 2


def test_short_write_records_real_result_without_hiding_product_failure(tmp_path):
    class ShortStream:
        def write(self, value):
            return len(value) - 1

    observer, recorder = observer_for(tmp_path)
    client = io_client.IoClient(lambda *_, **__: None)
    with observer.install(), io_client.io_scope(client), client.stage():
        with pytest.raises(Exception, match="backup_io_short_write"):
            io_client.io_write(ShortStream(), b"abc")
    result = rows(recorder, "file_batch")[-1]
    assert result["requested_bytes"] == 3 and result["actual_bytes"] == 2
    assert result["outcome"] == "ok"  # The file call returned; the host rejected the short count.


def test_io_row_shape_rejects_wrong_types_unknown_fields_and_invented_bytes(tmp_path):
    observer, recorder = observer_for(tmp_path)
    with observer.install():
        source = sqlite3.connect(tmp_path / "source.db")
        target = sqlite3.connect(tmp_path / "target.db")
        try:
            source.backup(target, pages=1, sleep=0)
        finally:
            source.close()
            target.close()
    row = rows(recorder, "sqlite_batch")[0]
    for field, value in (
        ("internal_read_bytes", 0),
        ("pages_per_step", True),
        ("sleep_seconds", "0"),
        ("callback_present", 1),
        ("process_sequence", True),
        ("stage_id", "path"),
        ("sql", "SELECT fictional"),
        ("id", 1),
        ("outcome", "ok"),
    ):
        with pytest.raises(evidence.EvidenceError):
            evidence.validate_record(
                "storage-phases.jsonl", {**row, field: value}, format_version=5
            )


def test_namespace_rejects_duplicate_unallocated_and_changed_plan_even_for_failure(tmp_path):
    source = tmp_path / "source"
    run = run_v5(source)
    observer, recorder = observer_for(tmp_path)
    with observer.install():
        pass
    raw = bytes(recorder.rows["storage-phases.jsonl"])
    (source / "storage-phases.jsonl").write_bytes(raw + raw)
    with pytest.raises(evidence.EvidenceError, match="io_namespace_invalid"):
        evidence.validate_files(contents(source), run)
    (source / "storage-phases.jsonl").write_bytes(raw)
    expected = {**run, "observer_sequences": [0, 1]}
    with pytest.raises(evidence.EvidenceError, match="run_identity_mismatch"):
        evidence.validate_files(contents(source), expected)


async def test_late_observer_does_not_fabricate_a_grant_and_never_prevents_release(tmp_path):
    budget = IoBudget(MockClock())
    stage = await budget.acquire(StageKey("worker", "operation", "a" * 32))
    observer, recorder = observer_for(tmp_path)
    with observer.install():
        await budget.charge(stage, 1, "read", 2)
        budget.release(stage)
    assert recorder.failed
    assert stage.finished and budget._active is None
    assert not rows(recorder, "io_slot")


def test_complete_format5_pass_still_has_no_self_reported_acceptance(tmp_path):
    source = tmp_path / "source"
    run = run_v5(source)
    run["result"] = "pass"
    save_manifest(source, run)
    observer, recorder = observer_for(tmp_path)
    with observer.install():
        pass
    (source / "storage-phases.jsonl").write_bytes(bytes(recorder.rows["storage-phases.jsonl"]))
    evidence.validate_files(contents(source), run)


def test_new_native_validation_uses_existing_strict_rules(tmp_path):
    from clytie.backup import native_sqlite

    source = tmp_path / "source"
    run = run_v5(source)
    observer, recorder = observer_for(tmp_path)
    with observer.install():
        with native_sqlite.validation_stage():
            db = sqlite3.connect(tmp_path / "fixture.db")
            try:
                with native_sqlite.native_checks(db):
                    assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            finally:
                db.close()
    events = rows(recorder, "native_validation")
    assert events and all(row["internal_read_bytes"] is None for row in events)
    raw = bytes(recorder.rows["storage-phases.jsonl"])
    (source / "storage-phases.jsonl").write_bytes(raw)
    run["result"] = "pass"
    save_manifest(source, run)
    evidence.validate_files(contents(source), run)
    (source / "storage-phases.jsonl").write_bytes(
        b"".join(
            line
            for line in raw.splitlines(keepends=True)
            if json.loads(line).get("event") != "finished"
        )
    )
    with pytest.raises(evidence.EvidenceError, match="native_release_unconfirmed"):
        evidence.validate_files(contents(source), run)


def test_distinct_threads_can_append_unique_ids_out_of_numeric_order(tmp_path):
    source = tmp_path / "source"
    run = run_v5(source)
    observer, recorder = observer_for(tmp_path)
    with observer.install():
        observer.record("fsync", 1, 2, "ok")
        observer.record("fsync", 1, 2, "ok")
    encoded = bytes(recorder.rows["storage-phases.jsonl"]).splitlines(keepends=True)
    (source / "storage-phases.jsonl").write_bytes(encoded[0] + encoded[2] + encoded[1])
    evidence.validate_files(contents(source), run)


def test_io_reference_ids_must_belong_to_the_declared_process(tmp_path):
    observer, recorder = observer_for(tmp_path)
    path = tmp_path / "fictional.bin"
    path.write_bytes(b"x")
    with observer.install(), path.open("rb") as stream:
        io_client.io_read(stream, 1)
    row = rows(recorder, "file_batch")[0]
    with pytest.raises(evidence.EvidenceError, match="io_namespace_invalid"):
        evidence.validate_record(
            "storage-phases.jsonl", {**row, "transfer_id": (2 << 40) + 1}, format_version=5
        )


def test_real_children_use_disjoint_frozen_namespaces_and_exclusive_targets(tmp_path):
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "home").mkdir()
    (tmp_path / "payload.bin").write_bytes(b"fictional")
    (tmp_path / "measurement-fixture.json").write_text(
        json.dumps({"format": 1, "source": "generated_fictional_only"}), encoding="utf-8"
    )
    execution = "ac" * 32
    (tmp_path / "measurement-observers.json").write_text(
        json.dumps({"format": 1, "execution_id": execution, "observer_sequences": [0, 1, 2, 3]}),
        encoding="utf-8",
    )
    code = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv.pop(1))
import measurement_worker as harness
from clytie.backup import worker, io_client
arguments = sys.argv[1:]
fixture = Path(arguments[0])
def fictional_worker():
    with (fixture / 'payload.bin').open('rb') as stream:
        io_client.io_read(stream, 3)
worker.main = fictional_worker
raise SystemExit(harness.main(arguments))
"""
    env = dict(os.environ)
    env.pop("CLYTIE_HOME", None)

    def start(sequence):
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                code,
                str(root / "scripts"),
                str(tmp_path),
                "a" * 32,
                "0",
                "5",
                str(sequence),
                execution,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    children = [start(1), start(2)]
    for child in children:
        _, error = child.communicate(timeout=10)
        assert child.returncode == 0, error
    raw = [(tmp_path / f"child-storage-phases-{n}.jsonl").read_bytes() for n in (1, 2)]
    identifiers = [{json.loads(line)["id"] for line in data.splitlines()} for data in raw]
    assert not identifiers[0] & identifiers[1]
    racing = [start(3), start(3)]
    for child in racing:
        child.communicate(timeout=10)
    assert sorted(child.returncode for child in racing) == [0, 2]


async def test_observation_preserves_token_wait_and_cancelled_charge_cleanup(tmp_path):
    from clytie.backup.io_budget import RATE_BUSY

    observer, recorder = observer_for(tmp_path)
    clock = MockClock()
    budget = IoBudget(clock)
    key = StageKey("worker", "operation", "a" * 32)
    with observer.install():
        stage = await budget.acquire(key)
        await budget.charge(stage, 1, "read", CHUNK)
        await budget.charge(stage, 2, "write", CHUNK)
        pending = asyncio.create_task(budget.charge(stage, 3, "read", CHUNK))
        await clock.drain()
        assert not pending.done()
        await clock.advance_and_drain(CHUNK / RATE_BUSY)
        await pending
        blocked = asyncio.create_task(budget.charge(stage, 4, "write", CHUNK))
        await clock.drain()
        assert not blocked.done()
        await budget.finish_requested(key)
        with pytest.raises(asyncio.CancelledError):
            await blocked
    assert budget.status()["charged"] == {"read": CHUNK * 2, "write": CHUNK}
    assert clock.pending_sleep_count == 0 and stage.finished
    assert rows(recorder, "io_charge")[-1]["outcome"] == "cancelled"
    assert rows(recorder, "io_slot")[-1]["event"] == "released"


@pytest.mark.parametrize(
    "damage", ["wrong_delta", "missing_progress", "backward_time", "reused_transfer"]
)
def test_pass_requires_consistent_real_sqlite_batch_lifecycle(tmp_path, damage):
    source = tmp_path / "source"
    run = run_v5(source)
    run["result"] = "pass"
    save_manifest(source, run)
    observer, recorder = observer_for(tmp_path)
    with observer.install():
        incoming = sqlite3.connect(tmp_path / "in.db")
        outgoing = sqlite3.connect(tmp_path / "out.db")
        try:
            incoming.execute("CREATE TABLE fixture(value)")
            incoming.commit()
            incoming.backup(outgoing, pages=1, sleep=0)
        finally:
            incoming.close()
            outgoing.close()
    original = rows(recorder)
    changed = [dict(row) for row in original]
    if damage == "wrong_delta":
        next(row for row in changed if row.get("event") == "progress")["copied_pages"] += 1
    elif damage == "missing_progress":
        changed = [row for row in changed if row.get("event") != "progress"]
    elif damage == "backward_time":
        progress = [row for row in changed if row.get("event") == "progress"]
        progress[0]["at_s"] = 2
    else:
        copy = [dict(row, id=row["id"] + 100) for row in original if row["phase"] == "sqlite_batch"]
        changed += copy
    (source / "storage-phases.jsonl").write_bytes(
        b"".join((json.dumps(row) + "\n").encode() for row in changed)
    )
    with pytest.raises(evidence.EvidenceError):
        evidence.validate_files(contents(source), run)


def test_formal_candidate_pass_rejects_missing_loaded_source_capability(tmp_path, monkeypatch):
    source = tmp_path / "source"
    run = run_v5(source)
    run.update(result="pass", phase="mechanism", method="legal_storage_v1")
    run["execution"].update(order=["candidate"], position=0)
    save_manifest(source, run)
    monkeypatch.setattr(io_observation, "_optional", lambda _: None)
    observer, recorder = observer_for(tmp_path)
    with observer.install():
        pass
    (source / "storage-phases.jsonl").write_bytes(bytes(recorder.rows["storage-phases.jsonl"]))
    with pytest.raises(evidence.EvidenceError, match="io_observation_unavailable"):
        evidence.validate_files(contents(source), run)


@pytest.mark.parametrize("version", [4, 5])
def test_other_child_exit_cannot_confirm_format5_legacy_unknown_close(tmp_path, version):
    source = tmp_path / "source"
    run = run_v5(source, (0, 1, 2)) if version == 5 else manifest_v4(source)
    run["result"] = "pass"
    save_manifest(source, run)
    encoded = bytearray()
    if version == 5:
        for sequence in (0, 1, 2):
            observer, recorder = observer_for(tmp_path, sequence)
            with observer.install():
                pass
            encoded.extend(recorder.rows["storage-phases.jsonl"])
    base = 2 << 40
    for offset, (event, connection) in enumerate(
        (
            ("started", 0),
            ("connection_attached", base + 2),
            ("driver_finished", 0),
            ("connection_close_unknown", base + 2),
        ),
        10,
    ):
        row = dict(
            id=base + offset,
            request_id=0,
            phase="native_validation",
            validation_id=base + 1,
            connection_id=connection,
            event=event,
            at_s=1,
            outcome="unknown" if event == "connection_close_unknown" else "ok",
            internal_read_bytes=None,
            boundary="legacy_validation",
        )
        encoded.extend((json.dumps(row) + "\n").encode())
    (source / "storage-phases.jsonl").write_bytes(encoded)
    # Only child two exits; the native connection belongs to child one.
    (source / "release-events.jsonl").write_text(
        "".join(
            json.dumps(dict(id=2, at_s=at, resource="process", event=event)) + "\n"
            for at, event in ((0, "acquired"), (2, "exit_verified"))
        ),
        encoding="utf-8",
    )
    if version == 4:
        evidence.validate_files(contents(source), run)  # Historical semantics unchanged.
    else:
        with pytest.raises(evidence.EvidenceError, match="native_release_unconfirmed"):
            evidence.validate_files(contents(source), run)
        with (source / "release-events.jsonl").open("a", encoding="utf-8") as stream:
            for at, event in ((0, "acquired"), (2, "exit_verified")):
                stream.write(json.dumps(dict(id=1, at_s=at, resource="thread", event=event)) + "\n")
            for at, event in ((0, "acquired"), (2, "exit_verified")):
                stream.write(
                    json.dumps(dict(id=1, at_s=at, resource="process", event=event)) + "\n"
                )
        evidence.validate_files(contents(source), run)
        run["result"] = "incomplete"
        save_manifest(source, run)
        evidence.validate_files(contents(source), run)
