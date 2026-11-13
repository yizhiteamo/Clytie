"""Handwritten fake metadata for offline mechanism predicates only."""

import copy
import json
import sqlite3

from tests.measurement_tools import load

draft = load("measurement_mechanism_validation")
analysis = load("measurement_analysis_index")
evidence = load("measurement_evidence")
FIELDS = load("measurement_control_observation").FIELDS
B = 1 << 40
H = "a" * 64
OTHER = "b" * 64


def control(event, at, **extra):
    row = dict.fromkeys(FIELDS)
    row.update(
        id=0,
        process_sequence=0,
        phase="io_control_binding",
        at_s=at,
        event=event,
        source="runtime_object",
        budget_id=B + 100,
        instance_hash=H,
        controller_hash=H,
        session_hash=H,
        operation_hash=H,
        generation_hash=H,
        execution_hash=H,
        responsibility_controller_hash=H,
        action="unknown",
        role="capture",
        responsibility_state="active",
        storage_owner=True,
        outcome="ok",
    )
    row.update(extra)
    return row


def events():
    rows = [
        control("observer_started", 0),
        control("control_created", 0),
        control("ledger_loaded", 0, source="validated_ledger"),
        control("session_registered", 0),
        control("responsibility_observed", 0, source="durable_save"),
        control("endpoint_bound", 0),
        control("request_started", 1, stage_id=H, action="acquire", call_hash=H, outcome="unknown"),
        dict(
            id=0,
            process_sequence=0,
            phase="io_slot",
            at_s=2,
            budget_id=B + 100,
            stage_id=H,
            event="granted",
        ),
        control("request_finished", 3, stage_id=H, action="acquire", call_hash=H),
    ]
    for event, at, connection in (
        ("started", 4, 0),
        ("connection_attached", 5, B + 200),
        ("driver_finished", 6, 0),
        ("connection_closed", 7, B + 200),
        ("finished", 10, 0),
    ):
        rows.append(
            dict(
                id=0,
                request_id=1,
                phase="native_validation",
                validation_id=B + 300,
                connection_id=connection,
                event=event,
                at_s=at,
                outcome="ok",
                internal_read_bytes=None,
                boundary="native_checks",
                stage_id=H,
                io_scope_state="active",
                stage_thread_matches=True,
                cleanup_source="unknown",
            )
        )
    rows.append(
        dict(
            id=0,
            process_sequence=0,
            phase="io_slot",
            at_s=8,
            budget_id=B + 100,
            stage_id=H,
            event="released",
        )
    )
    for n, row in enumerate(rows, 1):
        row["id"] = B + n
    return rows


def small_index(tmp_path, rows):
    # Reuse only Index's internal analytical storage; no product imports/threads.
    obj = draft.MechanismValidation.__new__(draft.MechanismValidation)
    obj.db = sqlite3.connect(tmp_path / "test.sqlite")
    obj.db.executescript("""
        CREATE TABLE rows(file,n,id,phase,event,t,stage,budget,transfer,validation,connection,j);
        CREATE TABLE slots(stage TEXT PRIMARY KEY,budget,seq,start,end,grant_id,release_id);
        CREATE TABLE findings(check_name,status,code,UNIQUE(check_name,status,code));
    """)
    for n, row in enumerate(rows, 1):
        evidence.validate_record("storage-phases.jsonl", row, format_version=6)
        obj.db.execute(
            "INSERT INTO rows VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "storage-phases.jsonl",
                n,
                row["id"],
                row["phase"],
                row.get("event"),
                row.get("at_s"),
                row.get("stage_id"),
                row.get("budget_id"),
                row.get("transfer_id"),
                row.get("validation_id"),
                row.get("connection_id"),
                json.dumps(row),
            ),
        )
    return obj


def result(obj, name, method):
    obj.checked(name, method)
    return tuple(obj.db.execute("SELECT status,code FROM findings WHERE check_name=?", (name,)))


def manifest():
    counts = dict.fromkeys(evidence.LEDGER_TABLES, 0)
    logical = dict(
        records=1,
        revisions=1,
        content_bytes=16,
        content_bytes_per_record=16,
        content_length_distribution=dict(min_bytes=16, max_bytes=16, total_bytes=16, records=1),
        file_count=1,
        file_bytes=16,
        provider_files=0,
        provider_bytes=0,
        asset_registrations=1,
        asset_bytes=16,
        ledger_rows=counts,
    )
    return dict(
        format=6,
        source="generated_fictional_only",
        method="legal_fixture_comparison_v1",
        candidate="ab" * 20,
        reference=evidence.ORIGINAL_REFERENCE,
        baseline_reference=evidence.BASELINE_REFERENCE,
        source_commit="ab" * 20,
        run_id=1,
        job_id="offline_predicate_test",
        attempt=1,
        platform="Windows",
        python="3.10.6",
        sqlite="3.37.2",
        seed=73,
        scale="large_asset",
        group=1,
        arm="candidate",
        phase="fixture",
        plan_sha256=H,
        fixture_version="legal_memory_v2",
        fixture_sha256=H,
        tool_sha256=H,
        parameters=dict(evidence.PARAMETERS),
        load=dict(
            window_seconds=20,
            interval_seconds=10,
            request_count=2,
            maximum_seconds=30,
            backup_start_seconds=0,
        ),
        result="cancelled",
        scenario="cancel",
        execution_id=H,
        observer_sequences=[0],
        environment=dict(cpu_count=1, os="Windows", release_sha256=H, machine_sha256=H),
        execution=dict(
            order=["baseline", "none", "original", "candidate"], position=3, warmup_requests=0
        ),
        metrics=dict(
            logical=logical,
            ledger_rows=counts,
            memory_database_bytes=4096,
            ledger_database_bytes=4096,
            asset_file_bytes=16,
            plugin_file_bytes=0,
            backup_bytes=0,
        ),
    )


def full_rows():
    rows = events()
    rows.append(
        dict(
            id=B + 60,
            process_sequence=0,
            phase="io_capability",
            at_s=0,
            host_file_batches=True,
            budget_slots=True,
            sqlite_backup=True,
            plugin_internal_io="unobserved",
        )
    )
    for event, at, actual, outcome in (
        ("started", 3.2, None, "unknown"),
        ("finished", 3.5, 16, "ok"),
    ):
        rows.append(
            dict(
                id=B + len(rows) + 70,
                phase="file_batch",
                process_sequence=0,
                transfer_id=B + 400,
                stage_id=H,
                event=event,
                at_s=at,
                direction="write",
                requested_bytes=16,
                actual_bytes=actual,
                outcome=outcome,
            )
        )
    source = rows[-1]
    for event, at in (
        ("triggered", 3.6),
        ("requested", 3.7),
        ("accepted", 3.8),
        ("terminal", 10.5),
    ):
        rows.append(
            dict(
                id=B + len(rows) + 80,
                phase="planned_cancel",
                process_sequence=0,
                event=event,
                at_s=at,
                trigger="first_asset_copy_chunk",
                source_row_id=source["id"],
                source_at_s=source["at_s"],
                stage_id=H,
                view_id=None,
                cancel_requested=True if event in {"accepted", "terminal"} else None,
                terminal_phase="cancelled" if event == "terminal" else None,
            )
        )
    return {
        "storage-phases.jsonl": rows,
        "requests.jsonl": [
            dict(id=1, kind="business", scheduled_s=0, started_s=0, finished_s=0.1, outcome="ok"),
            dict(
                id=2, kind="business", scheduled_s=10, started_s=10, finished_s=10.1, outcome="ok"
            ),
            dict(
                id=1, kind="backup", scheduled_s=0, started_s=0, finished_s=11, outcome="cancelled"
            ),
        ],
        "release-events.jsonl": [
            dict(id=1, resource="thread", event="acquired", at_s=0),
            dict(id=1, resource="thread", event="exit_verified", at_s=12),
        ],
        "resources.jsonl": [],
    }


def write_source(path, run, rows):
    path.mkdir()
    (path / "run-manifest.json").write_text(json.dumps(run), encoding="utf-8")
    for name, values in rows.items():
        with (path / name).open("w", encoding="utf-8") as stream:
            for value in values:
                stream.write(json.dumps(value) + "\n")


def evaluate(tmp_path, rows=None, run=None):
    run = manifest() if run is None else run
    rows = full_rows() if rows is None else rows
    source = tmp_path / "source"
    write_source(source, run, rows)
    return draft.aggregate(source, tmp_path / "analysis", copy.deepcopy(run))
