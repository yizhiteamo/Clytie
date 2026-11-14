import asyncio
import sqlite3
import subprocess
import sys

import pytest

from clytie.backup.coordination import CaptureCoordinator, CaptureError, companion_coordinator
from clytie.kernel.ledger import Ledger
from clytie.memory.store import MemoryStore
from clytie.sdk.testing import MockClock


async def test_hold_timeout_releases_gate(tmp_path):
    clock = MockClock()
    gate = CaptureCoordinator(tmp_path / "gate.lock", clock)
    with pytest.raises(CaptureError, match="capture_busy"):
        async with gate.capture():
            clock.advance(0.251)
    with gate.commit():
        pass


def test_transactions_enter_same_gate_and_rollback(tmp_path):
    clock = MockClock()
    gate = CaptureCoordinator(tmp_path / "gate.lock", clock)
    ledger = Ledger(tmp_path / "ledger.db", "companion_a", clock, coordination=gate)
    memory = MemoryStore(
        tmp_path / "memory.db", clock=clock, config={}, instance={}, coordination=gate
    )
    try:
        with gate.commit():
            with ledger.transaction():
                ledger.audit("test")
            with memory.transaction():
                memory.set_meta("test", 1)
        with pytest.raises(ValueError):
            with memory.transaction():
                memory.set_meta("test", 2)
                raise ValueError("rollback")
        assert memory.meta("test") == 1
    finally:
        memory.close()
        ledger.close()


def test_existing_rollback_ledger_switches_wal_only_for_writer(tmp_path):
    path = tmp_path / "ledger.db"
    clock = MockClock()
    ledger = Ledger(path, "companion_a", clock)
    ledger.close()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    ledger = Ledger(path, "companion_a", clock, readonly=True)
    assert ledger.db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    ledger.close()
    ledger = Ledger(path, "companion_a", clock)
    assert ledger.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    ledger.close()


async def test_cancel_wait_leaves_no_held_gate(tmp_path):
    clock = MockClock()
    gate = CaptureCoordinator(tmp_path / "gate.lock", clock)

    async def acquire():
        async with gate.capture():
            pass

    with gate.commit():
        task = asyncio.create_task(acquire())
        await clock.drain()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with gate.capture():
        pass


def test_killed_process_releases_os_gate(tmp_path):
    path = tmp_path / "gate.lock"
    script = """
import sys
from pathlib import Path
from clytie.backup.coordination import CaptureCoordinator
from clytie.sdk.testing import MockClock
with CaptureCoordinator(Path(sys.argv[1]), MockClock()).commit():
    print('held', flush=True)
    sys.stdin.readline()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "held"
        gate = CaptureCoordinator(path, MockClock(), wait_seconds=0.005)
        with pytest.raises(CaptureError, match="capture_busy"):
            with gate.commit():
                pass
        process.kill()
        process.wait(timeout=10)
        with gate.commit():
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()


async def test_production_runtime_and_host_share_companion_gate(tmp_path):
    from pathlib import Path

    from clytie.kernel.migration import LedgerRuntime
    from clytie.plugin_host import PluginHost

    clock = MockClock()
    runtime = LedgerRuntime(tmp_path, "companion_a", clock)
    host = PluginHost(Path(__file__).resolve().parents[1], tmp_path, clock=clock)
    ledger = await runtime.open()
    store = host.memory_store("companion_a", {}, {})
    gate = companion_coordinator(tmp_path, "companion_a", clock)
    try:
        assert ledger.coordination.path == store.coordination.path == gate.path
        with gate.commit():
            with store.transaction():
                store.audit("test")
            with ledger.transaction():
                ledger.audit("test")
    finally:
        await host.close()
        runtime.close()
