"""Tiny current-product integration; inventory stubs are not historical evidence."""

import asyncio
import hashlib
import json
import os
import platform
import sqlite3
import tempfile
from pathlib import Path

import pytest
from tests.measurement_tools import load
from tests.test_measurement_manifest import manifest_v3

import clytie

trial = load("measurement_trial")


def test_sampler_cannot_observe_child_before_its_identity_is_available():
    from types import SimpleNamespace

    children, identities = [], {}
    child = SimpleNamespace(pid=123)

    def identify(pid):
        # The real sampler is an independent thread and may run while process
        # identity is being resolved. Anything published must already be usable.
        assert all(process.pid in identities for process in children)
        return dict(pid=pid, started_at="fictional")

    trial.register_child(children, identities, child, identify)
    assert children == [child] and identities[123]["pid"] == 123


@pytest.fixture
def tmp_path():
    # Real backup nesting must fit Windows path limits; these are new fictional
    # roots, retained for diagnostics rather than bulk-deleted by this test.
    return Path(tempfile.mkdtemp(prefix="mt-"))


def prepared(tmp_path, monkeypatch, arm="candidate", *, window=3):
    run = manifest_v3(tmp_path / "expected")
    run.update(
        arm=arm,
        scale="small_text",
        platform=platform.system(),
        python=platform.python_version(),
        sqlite=sqlite3.sqlite_version,
    )
    run["execution"]["position"] = run["execution"]["order"].index(arm)
    run["metrics"]["logical"].update(
        provider_files=2,
        provider_bytes=64,
        asset_registrations=0,
        asset_bytes=0,
    )
    run["source_commit"] = {
        "baseline": run["baseline_reference"],
        "original": run["reference"],
        "none": run["candidate"],
        "candidate": run["candidate"],
    }[arm]
    run["environment"] = dict(
        cpu_count=os.cpu_count(),
        os=platform.system(),
        release_sha256=hashlib.sha256(platform.release().encode()).hexdigest(),
        machine_sha256=hashlib.sha256(platform.machine().encode()).hexdigest(),
    )
    run["load"] = dict(
        window_seconds=window,
        interval_seconds=0.025,
        request_count=round(window / 0.025),
        maximum_seconds=30,
        backup_start_seconds=0.025,
    )
    monkeypatch.setattr(
        trial,
        "verify",
        lambda directory: dict(
            mode=trial.MODES[arm],
            candidate=run["candidate"],
            source_commit=run["source_commit"],
        ),
    )
    return run, Path(clytie.__file__).resolve().parents[2]


@pytest.mark.parametrize("arm", ["baseline", "none", "candidate"])
async def test_tiny_trial_retains_full_plan_commit_and_real_releases(tmp_path, monkeypatch, arm):
    run, product = prepared(tmp_path, monkeypatch, arm, window=6 if arm == "candidate" else 0.2)
    capture_errors = []
    original_capture = trial.capture_request

    async def capture(*args, **kwargs):
        try:
            return await original_capture(*args, **kwargs)
        except Exception as exc:
            capture_errors.append(exc)
            raise

    monkeypatch.setattr(trial, "capture_request", capture)
    root = tmp_path / "trial"
    actual = await trial.run(root, run, product, tiny=True)
    if capture_errors:
        raise capture_errors[0]
    if arm != "candidate":
        assert actual["result"] == "pass"
    contents = {name: (root / "raw" / name).read_bytes() for name in trial.evidence.FILES}
    trial.evidence.validate_files(contents, run)
    rows = [json.loads(row) for row in contents["requests.jsonl"].splitlines()]
    business = [row for row in rows if row["kind"] == "business"]
    assert len(business) == run["load"]["request_count"]
    assert all(row["outcome"] == "ok" for row in business)
    phases = [json.loads(row) for row in contents["storage-phases.jsonl"].splitlines()]
    assert any(row["phase"] == "commit" and row["request_id"] == 1 for row in phases)
    releases = [json.loads(row) for row in contents["release-events.jsonl"].splitlines()]
    assert any(row["resource"] == "reference" and row["event"] == "released" for row in releases)
    if arm == "baseline":
        assert not any(row["phase"] in {"queue", "execute", "finalizer"} for row in phases)
        assert not any(row["resource"] == "thread" for row in releases)
    if arm == "candidate":
        backup = next(row for row in rows if row["kind"] == "backup")
        assert backup["outcome"] == "ok"
        covered = (
            backup["finished_s"] <= business[0]["scheduled_s"] + run["load"]["window_seconds"]
            and backup["finished_s"] <= business[-1]["finished_s"]
        )
        assert actual["result"] == ("pass" if covered else "fail")
        assert actual["metrics"]["backup_bytes"] > 0
        assert (root / "child-storage-phases.jsonl").read_bytes() in contents[
            "storage-phases.jsonl"
        ]
        assert any(
            row["resource"] == "process" and row["event"] == "exit_verified" for row in releases
        )
    else:
        assert not (root / "child-storage-phases.jsonl").exists()


async def test_formal_method_rejected_before_directory_creation(tmp_path, monkeypatch):
    run, product = prepared(tmp_path, monkeypatch)
    run.update(method="legal_storage_v1", phase="matrix")
    with pytest.raises(ValueError, match="not_frozen"):
        await trial.run(tmp_path / "unused", run, product)
    assert not (tmp_path / "unused").exists()


async def test_short_frozen_window_is_failed_without_extending_requests(tmp_path, monkeypatch):
    run, product = prepared(tmp_path, monkeypatch, window=0.1)
    root = tmp_path / "short"
    result = await trial.run(root, run, product, tiny=True)
    assert result["result"] == "fail"
    rows = [json.loads(row) for row in (root / "raw/requests.jsonl").read_bytes().splitlines()]
    assert len([row for row in rows if row["kind"] == "business"]) == 4
    backup = next(row for row in rows if row["kind"] == "backup")
    first = next(row for row in rows if row["kind"] == "business")
    assert backup["finished_s"] > first["scheduled_s"] + 0.1


async def test_cancel_keeps_cancelled_manifest_and_waits_for_worker_exit(tmp_path, monkeypatch):
    run, product = prepared(tmp_path, monkeypatch)
    started = asyncio.Event()
    original = trial.capture_request

    async def capture(*args, **kwargs):
        started.set()
        return await original(*args, **kwargs)

    monkeypatch.setattr(trial, "capture_request", capture)
    root = tmp_path / "cancel"
    task = asyncio.create_task(trial.run(root, run, product, tiny=True))
    waiting = asyncio.create_task(started.wait())
    done, _ = await asyncio.wait((task, waiting), return_when=asyncio.FIRST_COMPLETED)
    if task in done:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        pytest.fail("trial_ended_before_capture")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = json.loads((root / "raw/run-manifest.json").read_bytes())
    assert result["result"] == "cancelled"
    rows = [
        json.loads(row) for row in (root / "raw/release-events.jsonl").read_bytes().splitlines()
    ]
    assert any(row["resource"] == "process" and row["event"] == "exit_verified" for row in rows)


async def test_sampler_failure_keeps_failed_result_and_actual_owner_exit(tmp_path, monkeypatch):
    measurement_resources = load("measurement_resources")

    run, product = prepared(tmp_path, monkeypatch, "none", window=0.3)
    original = measurement_resources.rss_bytes
    samples = 0

    def fail(identity):
        nonlocal samples
        samples += 1
        if samples > 1:
            raise ValueError("fictional_sampler_failure")
        return original(identity)

    monkeypatch.setattr(measurement_resources, "rss_bytes", fail)
    root = tmp_path / "failed"
    result = await trial.run(root, run, product, tiny=True)
    assert result["result"] == "fail"
    status = json.loads((root / "trial-status.json").read_bytes())
    assert "measurement_sampler_failed" in status["codes"]
    events = [
        json.loads(row) for row in (root / "raw/release-events.jsonl").read_bytes().splitlines()
    ]
    assert any(row["resource"] == "thread" and row["event"] == "exit_verified" for row in events)


async def test_recording_capacity_preserves_invalid_bytes_and_closes_owner(tmp_path, monkeypatch):
    measurement_fixture = load("measurement_fixture")

    run, product = prepared(tmp_path, monkeypatch, "none", window=0.2)
    original_setup, original_recorder = measurement_fixture.setup, trial.Recorder
    owners = []

    async def setup(*args, **kwargs):
        owner, metrics = await original_setup(*args, **kwargs)
        owners.append(owner)
        return owner, metrics

    monkeypatch.setattr(measurement_fixture, "setup", setup)
    monkeypatch.setattr(trial, "Recorder", lambda: original_recorder(max_file_bytes=16 * 1024))
    # Exhaust bounded observation storage without substituting success records.
    run["load"].update(window_seconds=1, request_count=40)
    root = tmp_path / "capacity"
    with pytest.raises(trial.TrialError, match="evidence_invalid"):
        await trial.run(root, run, product, tiny=True)
    assert not (root / "raw").exists()
    manifest = json.loads((root / "invalid-raw/run-manifest.json").read_bytes())
    assert manifest["result"] == "incomplete"
    assert (root / "invalid-raw/storage-phases.jsonl").stat().st_size > 0
    assert owners and not owners[0].storage_owner._thread.is_alive()


async def test_original_source_mismatch_does_not_fallback(tmp_path, monkeypatch):
    run, product = prepared(tmp_path, monkeypatch, "original")
    monkeypatch.setattr(
        trial,
        "verify",
        lambda directory: dict(
            mode="candidate",
            candidate=run["candidate"],
            source_commit=run["candidate"],
        ),
    )
    with pytest.raises(ValueError, match="source_identity_mismatch"):
        await trial.run(tmp_path / "unused", run, product, tiny=True)
    assert not (tmp_path / "unused").exists()


async def test_maximum_budget_cancels_waiters_then_drains_actual_resources(tmp_path, monkeypatch):
    measurement_fixture = load("measurement_fixture")

    run, product = prepared(tmp_path, monkeypatch, "none", window=0.1)
    run["load"]["maximum_seconds"] = 0.15
    run["execution"]["warmup_requests"] = 0
    original = measurement_fixture.business

    async def slow(owner, index):
        await trial.wall_until(trial.perf_counter() + 0.1)
        return await original(owner, index)

    monkeypatch.setattr(measurement_fixture, "business", slow)
    root = tmp_path / "deadline"
    result = await trial.run(root, run, product, tiny=True)
    assert result["result"] == "fail"
    status = json.loads((root / "trial-status.json").read_bytes())
    assert "measurement_maximum_exceeded" in status["codes"]
    events = [
        json.loads(row) for row in (root / "raw/release-events.jsonl").read_bytes().splitlines()
    ]
    assert any(row["resource"] == "thread" and row["event"] == "exit_verified" for row in events)
