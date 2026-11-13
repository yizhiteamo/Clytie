"""Full tiny wiring; invented inventory hashes are not historical evidence."""

import copy
import hashlib
import json
import os
import platform
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from tests.measurement_tools import load

import clytie

evidence = load("measurement_evidence")
trial = load("measurement_trial")
cancel_validation = load("measurement_cancel_validation")


class BoundedLines(bytes):
    def splitlines(self, *args, **kwargs):
        raise AssertionError("the complete raw file must not be split into an allocated list")


def manifest(scale):
    asset = scale == "large_asset"
    files = 1 if asset else 2
    ledger = dict.fromkeys(evidence.LEDGER_TABLES, 0)
    logical = dict(
        records=8,
        revisions=8,
        content_bytes=8 * 4096,
        content_bytes_per_record=4096,
        content_length_distribution=dict(
            min_bytes=4096, max_bytes=4096, total_bytes=8 * 4096, records=8
        ),
        file_count=files,
        file_bytes=files * 32,
        provider_files=0 if asset else files,
        provider_bytes=0 if asset else files * 32,
        asset_registrations=files if asset else 0,
        asset_bytes=files * 32 if asset else 0,
        ledger_rows=ledger,
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
        job_id="planned_cancel_tool_test",
        attempt=1,
        platform=platform.system(),
        python=platform.python_version(),
        sqlite=sqlite3.sqlite_version,
        seed=73,
        scale=scale,
        group=1,
        arm="candidate",
        phase="fixture",
        plan_sha256="ac" * 32,
        fixture_version="legal_memory_v2",
        fixture_sha256="bd" * 32,
        tool_sha256="ce" * 32,
        parameters=evidence.PARAMETERS.copy(),
        load=dict(
            window_seconds=6,
            interval_seconds=0.025,
            request_count=240,
            maximum_seconds=30,
            backup_start_seconds=0.025,
        ),
        result="incomplete",
        scenario="cancel",
        execution_id="ed" * 32,
        observer_sequences=[0, 1],
        environment=dict(
            cpu_count=os.cpu_count(),
            os=platform.system(),
            release_sha256=hashlib.sha256(platform.release().encode()).hexdigest(),
            machine_sha256=hashlib.sha256(platform.machine().encode()).hexdigest(),
        ),
        execution=dict(
            order=["baseline", "none", "original", "candidate"], position=3, warmup_requests=2
        ),
        metrics=dict(
            logical=logical,
            ledger_rows=ledger,
            memory_database_bytes=64 * 1024,
            ledger_database_bytes=16 * 1024,
            asset_file_bytes=files * 32 if asset else 0,
            plugin_file_bytes=0 if asset else files * 32,
            backup_bytes=0,
        ),
    )


@pytest.mark.parametrize("scale", ["large_database", "large_asset"])
async def test_full_tiny_trial_native_planned_cancel_and_complete_business(scale, monkeypatch):
    tools = Path(__file__).resolve().parents[1] / "scripts"
    assert Path(trial.__file__).resolve() == tools / "measurement_trial.py"
    assert Path(evidence.__file__).resolve() == tools / "measurement_evidence.py"
    run = manifest(scale)
    evidence.validate_run(run)
    product = Path(clytie.__file__).resolve().parents[2]
    # Only source inventory attestation is stubbed. Actual production cancellation,
    # snapshots, workers, I/O, requests, raw validation and resource teardown execute.
    monkeypatch.setattr(
        trial,
        "verify",
        lambda _: dict(
            candidate=run["candidate"],
            source_commit=run["source_commit"],
            mode="candidate",
        ),
    )
    root = product / ".cache/measurement-cancel-draft/r" / uuid4().hex[:8]
    actual = await trial.run(root, run, product, tiny=True)
    status = json.loads((root / "trial-status.json").read_bytes())
    assert status["codes"] == [], (root, status)
    assert actual["result"] == "cancelled", (root, actual["result"])
    contents = {name: (root / "raw" / name).read_bytes() for name in evidence.FILES}
    evidence.validate_files(contents, run)
    checks = cancel_validation.verify_cancel_evidence(contents, run)
    assert checks["planned_cancel_verified"]
    assert checks["mechanism_acceptance"] == "not_evaluated"
    streamed = {
        name: BoundedLines(raw) if name.endswith(".jsonl") else raw
        for name, raw in contents.items()
    }
    assert cancel_validation.verify_cancel_evidence(streamed, run) == checks
    requests = [json.loads(row) for row in contents["requests.jsonl"].splitlines()]
    business = [row for row in requests if row["kind"] == "business"]
    assert len(business) == run["load"]["request_count"]
    assert all(row["outcome"] == "ok" for row in business)
    assert next(row for row in requests if row["kind"] == "backup")["outcome"] == "cancelled"
    phases = [json.loads(row) for row in contents["storage-phases.jsonl"].splitlines()]
    cancel = [row for row in phases if row["phase"] == "planned_cancel"]
    assert [row["event"] for row in cancel] == ["triggered", "requested", "accepted", "terminal"]
    assert cancel[-1]["terminal_phase"] == "cancelled"
    assert cancel[-1]["cancel_requested"] is True
    source = next(row for row in phases if row["id"] == cancel[0]["source_row_id"])
    assert source["phase"] == ("sqlite_batch" if scale == "large_database" else "file_batch")
    assert cancel[0]["source_at_s"] <= cancel[0]["at_s"] <= cancel[1]["at_s"] <= cancel[2]["at_s"]
    releases = [json.loads(row) for row in contents["release-events.jsonl"].splitlines()]
    for resource, event in (
        ("process", "exit_verified"),
        ("thread", "exit_verified"),
        ("view", "released"),
        ("reference", "released"),
    ):
        assert any(row["resource"] == resource and row["event"] == event for row in releases)
    assert_rejects_incomplete_cancel_evidence(contents, run)


def assert_rejects_incomplete_cancel_evidence(contents, run):
    original = {
        name: [json.loads(line) for line in contents[name].splitlines()]
        for name in ("storage-phases.jsonl", "release-events.jsonl", "requests.jsonl")
    }
    for defect in (
        "missing_source",
        "missing_acceptance",
        "different_source",
        "committed",
        "cancel_not_requested",
        "acceptance_after_terminal",
        "view_missing",
        "missing_business",
        "failed_business",
        "changed_arrival",
        "missing_process_exit",
        "missing_thread_exit",
        "missing_reference_release",
        "uncovered_window",
    ):
        rows = copy.deepcopy(original)
        phases, resources, requests = (rows[name] for name in original)
        cancel = [row for row in phases if row["phase"] == "planned_cancel"]
        if defect == "missing_source":
            phases[:] = [row for row in phases if row["id"] != cancel[0]["source_row_id"]]
        elif defect == "missing_acceptance":
            phases.remove(cancel[2])
        elif defect == "different_source":
            cancel[1]["stage_id"] = "f" * 64
        elif defect == "committed":
            cancel[-1]["terminal_phase"] = "committed"
        elif defect == "cancel_not_requested":
            cancel[-1]["cancel_requested"] = False
        elif defect == "acceptance_after_terminal":
            cancel[2]["at_s"] = cancel[-1]["at_s"] + 1
        elif defect == "view_missing":
            resources[:] = [row for row in resources if row["resource"] != "view"]
            if run["scale"] != "large_database":
                continue
        elif defect == "missing_business":
            requests.remove(next(row for row in requests if row["kind"] == "business"))
        elif defect == "failed_business":
            next(row for row in requests if row["kind"] == "business")["outcome"] = "failed"
        elif defect == "changed_arrival":
            next(row for row in requests if row["kind"] == "business")["scheduled_s"] += 0.01
        elif defect in {"missing_process_exit", "missing_thread_exit", "missing_reference_release"}:
            resource = defect.split("_")[1]
            resources[:] = [
                row
                for row in resources
                if not (
                    row["resource"] == resource and row["event"] in {"exit_verified", "released"}
                )
            ]
        elif defect == "uncovered_window":
            first = next(row for row in requests if row["kind"] == "business")
            next(row for row in requests if row["kind"] == "backup")["finished_s"] = (
                first["scheduled_s"] + run["load"]["window_seconds"] + 1
            )
        damaged = dict(contents)
        for name, values in rows.items():
            damaged[name] = b"".join(json.dumps(row).encode() + b"\n" for row in values)
        with pytest.raises(ValueError):
            cancel_validation.verify_cancel_evidence(damaged, run)
