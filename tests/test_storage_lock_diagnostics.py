"""独立诊断只观察虚构锁文件；不修改产品互斥或持久行为。"""

import asyncio
import importlib.util
import json
import os
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

from clytie.backup.coordination import CaptureCoordinator
from clytie.plugin_host import locking
from clytie.sdk.testing import MockClock
from clytie.storage.owner import StorageOwner


def diagnostic():
    path = Path(__file__).resolve().parents[1] / "scripts/diagnose_storage_lock.py"
    spec = importlib.util.spec_from_file_location("lock_diagnostic", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Resources:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE fixture(value INTEGER)")

    def close(self):
        self.db.close()


def test_descriptor_reuse_cannot_erase_new_open_ownership(tmp_path):
    trace = diagnostic().LockTrace(tmp_path)
    trace._descriptors[7] = 1

    def close_and_concurrently_reopen():
        # The OS can reuse the integer after close returns, before the tracing
        # wrapper reacquires its guard. The new open has a different trace id.
        with trace._guard:
            trace._descriptors[7] = 2

    trace._fd_call(7, "close", close_and_concurrently_reopen, closing=True)
    assert trace.active_descriptors == 1
    assert trace._descriptors[7] == 2


async def test_real_owner_lock_trace_separates_io_and_preserves_patches(tmp_path, monkeypatch):
    module = diagnostic()
    root = tmp_path / "diagnostic"
    root.mkdir()
    loop = asyncio.get_running_loop()
    entered = loop.create_future()
    release = threading.Event()
    real_fsync, real_lock = os.fsync, locking._os_lock
    seen = []

    def delayed_fsync(fd):
        hidden_value = "fictional-sensitive-content"
        if not seen:
            seen.append(hidden_value)
            loop.call_soon_threadsafe(entered.set_result, None)
            release.wait(5)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", delayed_fsync)
    trace = module.LockTrace(root)
    owner = await StorageOwner.start(
        lambda: Resources(root / "fixture.db"), companion_id="fixture", clock=MockClock()
    )
    coordinator = CaptureCoordinator(root / "backup-capture.lock", owner.clock)

    def write(resources, command):
        with coordinator.commit():
            resources.db.execute("INSERT INTO fixture VALUES(1)")
            command.begin_commit()
            resources.db.commit()

    try:
        async with trace:
            command = owner.submit(write)
            await entered
            trace.sample_owner_positions()
            assert owner.status()["ordinary"] == 1
            release.set()
            await command.result()
            with locking.file_lock(tmp_path / "backup-capture.lock", {"operation": "outside"}):
                pass
        assert os.fsync is delayed_fsync
        assert locking._os_lock is real_lock
        assert not trace.sampler.is_alive()
        assert trace.active_descriptors == 0
        events = trace.events
        assert len([row for row in events if row["stage"] == "open"]) == 1
        assert {row["stage"] for row in events} >= {
            "open",
            "os_lock_acquire",
            "metadata_write",
            "metadata_truncate",
            "metadata_fsync",
            "os_lock_release",
            "close",
            "coordinator_enter",
            "coordinator_exit",
        }
        assert all(row["thread"] == "clytie-storage-owner" for row in events)
        assert trace.stacks
        encoded = json.dumps(trace.report())
        assert "fictional-sensitive-content" not in encoded
        assert str(tmp_path) not in encoded
        assert all(
            set(frame) == {"file", "line", "function"}
            for row in trace.stacks
            for frame in row["stack"]
        )
    finally:
        release.set()
        await owner.close()


async def test_nonblocking_lock_failure_is_counted_without_changing_retry(tmp_path, monkeypatch):
    module = diagnostic()
    real_lock = locking._os_lock
    failed = []

    def busy_once(fd, acquire):
        if acquire and not failed:
            failed.append(True)
            raise BlockingIOError("fictional busy")
        return real_lock(fd, acquire)

    monkeypatch.setattr(locking, "_os_lock", busy_once)
    trace = module.LockTrace(tmp_path)
    owner = await StorageOwner.start(
        lambda: Resources(tmp_path / "fixture.db"), companion_id="fixture", clock=MockClock()
    )
    coordinator = CaptureCoordinator(tmp_path / "backup-capture.lock", owner.clock)

    def enter(resources, command):
        with coordinator.commit():
            return 1

    try:
        async with trace:
            assert await owner.execute(enter) == 1
        report = trace.report()
        assert report["lock_attempts"] == 2
        assert report["lock_failures"] == 1
        assert report["in_process_or_os_busy"] == 1
        assert trace.active_descriptors == 0
        assert locking._os_lock is busy_once
        summary = module.render_summary(report, raw_hash="a" * 64)
        assert "not a performance verdict" in summary
        assert "metadata_fsync" in summary
    finally:
        await owner.close()


async def test_short_diagnostic_run_keeps_raw_evidence_and_scannable_summary(
    tmp_path, monkeypatch, capsys
):
    import shlex
    import subprocess
    import sys

    import yaml

    module = diagnostic()
    monkeypatch.setitem(module.benchmark.SCALES, "small_text", (2, 1, 16))
    output = tmp_path / "new_diagnostic"
    await module.main(
        SimpleNamespace(
            output=output, scale="small_text", requests=3, interval=0.01, no_backup=True
        )
    )
    report = json.loads((output / "trace.json").read_text())
    result = json.loads((output / "case/result.json").read_text())
    assert report["scope"] == "diagnostic_only"
    assert report["active_descriptors"] == 0
    assert report["sampler_alive"] is False
    assert report["benchmark_origin_s"] > 0
    assert {row["window"] for row in report["groups"]} >= {"setup", "workload"}
    assert result["persisted_counter"] == 2
    assert result["committed_request_indices"] == [0, 1, 2]
    assert not result["sampler_thread_alive_after_close"]
    assert "not a performance verdict" in capsys.readouterr().out
    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    program = shlex.split(config["repos"][0]["hooks"][0]["entry"])[2]
    checked = subprocess.run(
        [sys.executable, "-c", program, "--message", str(output / "summary.md")],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


async def test_patch_cleanup_also_runs_after_original_os_failure(tmp_path, monkeypatch):
    module = diagnostic()
    real_fsync = os.fsync

    def failing_fsync(fd):
        raise OSError("fictional fsync failure")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    trace = module.LockTrace(tmp_path)
    coordinator = CaptureCoordinator(tmp_path / "backup-capture.lock", MockClock())
    try:
        async with trace:
            with coordinator.commit():
                raise AssertionError("must not acquire after fsync failure")
    except OSError:
        pass
    else:
        raise AssertionError("original failure was hidden")
    assert trace.active_descriptors == 0
    assert not trace.sampler.is_alive()
    assert os.fsync is failing_fsync
    assert any(
        row["stage"] == "metadata_fsync" and row["error"] == "OSError" for row in trace.events
    )
    monkeypatch.setattr(os, "fsync", real_fsync)
