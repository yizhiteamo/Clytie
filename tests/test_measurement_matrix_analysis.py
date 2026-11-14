"""Offline numeric adaptation is never formal performance acceptance."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests.measurement_tools import load
from tests.test_measurement_matrix_plan import prepared

analysis = load("measurement_matrix_analysis")


def rows():
    return [
        dict(platform=p, scale=s, group=g, arm=a, business={"p99": value})
        for p in ("Windows", "Linux")
        for s in analysis.plan.SCALES
        for g in (1, 2, 3)
        for a, value in (("baseline", g * 10), ("none", 29), ("original", 200), ("candidate", 100))
    ]


def test_old_minimum_reference_and_original_retained_without_new_gate():
    report = analysis.compare(rows())
    assert report["numeric_status"] == "passed"
    assert report["status"] == "not_evaluated"
    assert len(report["comparisons"]) == 24
    assert report["comparisons"][0]["old_reference_ms"] == 10
    assert report["comparisons"][0]["no_backup_limit_ms"] == 30
    assert report["comparisons"][0]["original_p99_ms"] == 200


def test_slow_none_cannot_take_easier_paired_baseline():
    data = rows()
    next(r for r in data if r["arm"] == "none" and r["group"] == 3)["business"]["p99"] = 31
    assert analysis.compare(data)["numeric_status"] == "failed"


def test_missing_or_duplicate_arm_rejected():
    with pytest.raises(ValueError):
        analysis.compare(rows()[:-1])
    with pytest.raises(ValueError):
        analysis.compare(rows()[:-1] + [rows()[0]])


def test_nearest_rank_and_backlog_end_are_preserved():
    records = [dict(id=1, kind="business", scheduled_s=1, started_s=2, finished_s=4, outcome="ok")]
    metric = analysis.business_metric(records, dict(request_count=1, interval_seconds=0.025))
    assert metric["p99"] == 3000
    assert metric == analysis.legacy.percentiles([3000])
    records[0]["outcome"] = "failed"
    with pytest.raises(ValueError):
        analysis.business_metric(records, dict(request_count=1, interval_seconds=0.025))


def verified_fixture(tmp_path):
    frozen, _, _, runtime, _ = prepared()
    row = frozen["runs"][0]
    run = analysis.mechanism._manifest(
        frozen,
        row,
        runtime=runtime[row["platform"]],
        workflow=dict(run_id=123, attempt=1, job_id="matrix_" + analysis.matrix.group_key(row)),
        phase="baseline",
    )
    run["result"] = "pass"
    raw = tmp_path / "raw"
    raw.mkdir()
    requests = [
        dict(
            id=i + 1,
            kind="business",
            scheduled_s=i * 0.025,
            started_s=i * 0.025,
            finished_s=i * 0.025 + 0.001,
            outcome="ok",
        )
        for i in range(run["load"]["request_count"])
    ]
    resource = dict(
        at_s=0,
        rss_bytes=123,
        wal_bytes=0,
        temp_bytes=0,
        queue=0,
        finalizers=0,
        views=0,
        backup_bytes=0,
        loop_delay_ms=0,
        rate="none",
        logical_io_bytes=None,
        backup_phase="not_applicable",
    )
    records = {
        "requests.jsonl": requests,
        "resources.jsonl": [resource],
        "storage-phases.jsonl": [],
        "release-events.jsonl": [],
    }
    for name, values in records.items():
        (raw / name).write_text("".join(json.dumps(v) + "\n" for v in values))
    (raw / "run-manifest.json").write_text(json.dumps(run))
    state = dict(
        accepted=True,
        private_scan="pass",
        state="verified_private_pass_generic_exception",
        measurement_result="pass",
        ciphertext_sha256="f" * 64,
        files={
            p.name: dict(size=p.stat().st_size, sha256=hashlib.sha256(p.read_bytes()).hexdigest())
            for p in raw.iterdir()
        },
    )
    (tmp_path / "state.json").write_text(json.dumps(state))
    return frozen


def test_local_state_boundary_rechecks_all_actual_hashes(tmp_path):
    frozen = verified_fixture(tmp_path)
    assert analysis.load_verified(tmp_path, frozen, 123)["peaks"]["rss_bytes"] == 123
    (tmp_path / "raw/release-events.jsonl").write_text(
        json.dumps(dict(id=1, at_s=0, resource="thread", event="exit_verified")) + "\n"
    )
    with pytest.raises(ValueError, match="verified_raw_changed"):
        analysis.load_verified(tmp_path, frozen, 123)


def test_actual_workflow_identity_not_taken_from_self_report(tmp_path):
    frozen = verified_fixture(tmp_path)
    with pytest.raises(ValueError, match="expected_identity_mismatch"):
        analysis.load_verified(tmp_path, frozen, 124)


def test_altered_business_arrivals_are_rejected_even_with_same_count():
    rows = [
        dict(
            id=i + 1,
            kind="business",
            scheduled_s=i * 0.1,
            started_s=i * 0.1,
            finished_s=i * 0.1 + 0.01,
            outcome="ok",
        )
        for i in range(2)
    ]
    with pytest.raises(ValueError, match="business_arrivals"):
        analysis.business_metric(rows, dict(request_count=2, interval_seconds=0.025))


def test_fresh_process_module_help_needs_no_pythonpath_or_test_imports():
    env = {key: value for key, value in os.environ.items() if key.upper() != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, "-m", "scripts.measurement_matrix_analysis", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--verified-index" in result.stdout
