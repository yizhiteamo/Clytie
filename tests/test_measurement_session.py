import asyncio
import json
import random
import sqlite3
import tempfile
from pathlib import Path

import pytest
from tests.test_measurement_evidence import evidence, fixture_files, load_tool

from clytie.clock import SystemClock
from clytie.plugin_host.support import timed

for name in (
    "benchmark_backup",
    "measurement_recording",
    "measurement_load",
    "measurement_observation",
    "measurement_resources",
    "measurement_views",
):
    load_tool(name)
session = load_tool("measurement_session")


def local_fixture(path):
    run = fixture_files(path)
    run.update(
        platform=session.platform.system(),
        python=session.platform.python_version(),
        sqlite=session.sqlite3.sqlite_version,
    )
    return run


async def test_tiny_recording_fixture_combines_live_owner_real_child_and_encryption_schema(
    tmp_path,
    monkeypatch,
):
    errors = []
    original = session.capture_request

    async def tracked(*args, **kwargs):
        try:
            return await original(*args, **kwargs)
        except Exception as exc:
            errors.append(exc)
            raise

    monkeypatch.setattr(session, "capture_request", tracked)
    run = local_fixture(tmp_path / "expected")
    run["load"] = dict(
        window_seconds=0.25, interval_seconds=0.025, request_count=10, maximum_seconds=30
    )
    root = Path(tempfile.mkdtemp(prefix="cm-")) / "r"
    result = await timed(session.run_fixture(root, run, scale=(8, 1, 32)), SystemClock(), 30)
    if errors:
        error = errors[0]
        while error.__context__ is not None:
            error = error.__context__
        raise error
    assert result["result"] == "pass"
    database = root / "home/companions/companion_a/memory/memory.sqlite3"
    db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        record = json.loads(
            db.execute("SELECT data FROM records WHERE id=?", (f"{1:032x}",)).fetchone()[0]
        )
        assert record["content"] == random.Random(run["seed"]).randbytes(2048).hex()
    finally:
        db.close()
    contents = {name: (root / "raw" / name).read_bytes() for name in evidence.FILES}
    evidence.validate_files(contents, result)
    stages = [json.loads(row) for row in contents["storage-phases.jsonl"].splitlines()]
    assert any(row["id"] >= 1 << 40 for row in stages)
    assert any(row["phase"] == "commit" and row["request_id"] for row in stages)
    assert (root / "child-storage-phases.jsonl").read_bytes() in contents["storage-phases.jsonl"]
    resources = [json.loads(row) for row in contents["resources.jsonl"].splitlines()]
    assert resources and max(row["backup_bytes"] for row in resources) > 0
    assert any(row["rss_bytes"] > 0 for row in resources)


async def test_unfrozen_mechanism_cannot_run_through_fixture_entry(tmp_path):
    run = local_fixture(tmp_path / "expected")
    run.update(phase="mechanism", method="a43_bounded_io_v1")
    with pytest.raises(ValueError, match="launcher_not_frozen"):
        await session.run_fixture(tmp_path / "must_not_exist", run, scale=(8, 1, 32))
    assert not (tmp_path / "must_not_exist").exists()


async def test_sampler_failure_preserves_failed_raw_and_closes_owner(tmp_path, monkeypatch):
    owners = []
    original = session.benchmark.setup

    async def tracked(*args, **kwargs):
        owner, store = await original(*args, **kwargs)
        owners.append(owner)
        return owner, store

    monkeypatch.setattr(session.benchmark, "setup", tracked)
    original_rss = session.rss_bytes
    samples = 0

    def failing_sample(identity):
        nonlocal samples
        samples += 1
        if samples > 2:
            raise ValueError("fictional_sample_failure")
        return original_rss(identity)

    monkeypatch.setattr(session, "rss_bytes", failing_sample)
    run = local_fixture(tmp_path / "expected")
    run["load"] = dict(
        window_seconds=0.05, interval_seconds=0.025, request_count=2, maximum_seconds=30
    )
    root = Path(tempfile.mkdtemp(prefix="cm-")) / "r"
    try:
        with pytest.raises(ValueError, match="fictional_sample_failure"):
            await session.run_fixture(root, run, scale=(2, 1, 16))
        assert not owners[0].storage_owner._thread.is_alive()
        manifest = json.loads((root / "raw/run-manifest.json").read_bytes())
        assert manifest["result"] == "fail"
    finally:
        # A red test must not leave its own test process hanging on a live owner.
        for owner in owners:
            await owner.host.close()
            await owner.storage_owner.close()
            owner.runtime.close()


async def test_fixture_cancel_waits_for_actual_child_and_view_exit(tmp_path, monkeypatch):
    captured = asyncio.Event()
    owners = []
    original = session.capture_request

    async def track(owner, *args, **kwargs):
        owners.append(owner)
        captured.set()
        return await original(owner, *args, **kwargs)

    monkeypatch.setattr(session, "capture_request", track)
    run = local_fixture(tmp_path / "expected")
    run["load"] = dict(
        window_seconds=2, interval_seconds=0.025, request_count=80, maximum_seconds=30
    )
    root = Path(tempfile.mkdtemp(prefix="cm-")) / "r"
    failures = []

    async def preserve():
        try:
            return await session.run_fixture(root, run, scale=(8, 1, 32))
        except BaseException as exc:
            failures.append(exc)
            raise

    task = asyncio.create_task(preserve())
    await timed(captured.wait(), SystemClock(), 30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await timed(task, SystemClock(), 30)
    if failures and failures[0].__cause__ is not None:
        raise failures[0].__cause__
    assert not owners[0].storage_owner._thread.is_alive()
    manifest = json.loads((root / "raw/run-manifest.json").read_bytes())
    assert manifest["result"] == "cancelled"
    events = [
        json.loads(line) for line in (root / "raw/release-events.jsonl").read_bytes().splitlines()
    ]
    assert any(row["resource"] == "process" and row["event"] == "exit_verified" for row in events)


async def test_setup_seed_failure_closes_actual_owner(tmp_path, monkeypatch):
    from clytie.memory.store import MemoryStore

    started = []
    original = session.benchmark.StorageOwner.start

    async def start(*args, **kwargs):
        owner = await original(*args, **kwargs)
        started.append(owner)
        return owner

    def fail(*args, **kwargs):
        raise ValueError("fictional_seed_failure")

    monkeypatch.setattr(session.benchmark.StorageOwner, "start", start)
    monkeypatch.setattr(MemoryStore, "_save", fail)
    with pytest.raises(ValueError, match="fictional_seed_failure"):
        await session.benchmark.setup(tmp_path, (2, 1, 16), record_payload=True)
    assert started and not started[0]._thread.is_alive()


@pytest.mark.parametrize(
    "field,value",
    [
        ("arm", "original"),
        ("platform", "Linux" if session.platform.system() == "Windows" else "Windows"),
        ("python", "0.0.0"),
        ("sqlite", "0.0.0"),
    ],
)
async def test_fixture_rejects_inaccurate_runtime_identity(tmp_path, field, value):
    run = local_fixture(tmp_path / "expected")
    run[field] = value
    root = tmp_path / "must_not_exist"
    with pytest.raises(ValueError, match="identity_mismatch"):
        await session.run_fixture(root, run, scale=(2, 1, 16))
    assert not root.exists()
