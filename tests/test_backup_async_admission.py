import asyncio
from datetime import timedelta

import pytest

from clytie.backup.coordination import CaptureCoordinator, CaptureError, run_sync, transaction
from clytie.backup.providers import NamespaceSink, ProviderCapture
from clytie.clock import SystemClock
from clytie.kernel.ledger import Ledger
from clytie.memory.store import MemoryStore
from clytie.sdk import CancelToken
from clytie.sdk.backup import BackupRequest, BackupTicket, BackupView
from clytie.sdk.testing import MockClock


async def test_async_provider_capture_does_not_block_real_sqlite_writes_or_response(tmp_path):
    clock = SystemClock()
    gate = CaptureCoordinator(tmp_path / "gate.lock", clock)
    store = MemoryStore(
        tmp_path / "memory.db", clock=clock, config={}, instance={}, coordination=gate
    )
    ledger = Ledger(tmp_path / "ledger.db", "companion_a", clock, coordination=gate)
    entered = asyncio.Event()
    order = []

    class Provider:
        async def prepare(self, request, cancel):
            return BackupTicket("fictional")

        async def capture(self, ticket, epoch, cancel):
            entered.set()
            await clock.sleep(0.05)
            order.append("captured")
            return BackupView(epoch, 1, "revision_a", ticket.handle)

        async def release(self, ticket):
            pass

    request = BackupRequest(
        operation_id="test",
        companion_id="companion_a",
        plugin_id="fake",
        data_version=1,
        deadline=clock.now() + timedelta(seconds=10),
    )
    provider = ProviderCapture(
        Provider(), request, clock, NamespaceSink(tmp_path / "sink"), cancel=CancelToken()
    )
    assert await provider.prepare()

    async def capture():
        async with gate.capture():
            assert await provider.capture("epoch_a")

    async def write():
        await entered.wait()
        async with transaction(store):
            store.set_meta("fictional", 1)
            with ledger.transaction():
                ledger.audit("fictional_commit")
        order.append("written")

    async def response():
        await entered.wait()
        await clock.sleep(0.01)
        order.append("response")

    try:
        await asyncio.gather(capture(), write(), response())
        assert order == ["response", "captured", "written"]
        assert store.meta("fictional") == 1
        assert len(ledger.rows("audit")) == 1
        assert gate.max_write_wait < 0.25
    finally:
        await provider.release()
        store.close()
        ledger.close()


async def test_admission_timeout_cancel_and_child_task_cannot_reenter(tmp_path):
    clock = MockClock()
    gate = CaptureCoordinator(tmp_path / "gate.lock", clock, wait_seconds=0.05)
    store = MemoryStore(
        tmp_path / "memory.db", clock=clock, config={}, instance={}, coordination=gate
    )
    try:
        async with gate.admit():
            # Same-task nested operations retain the actual lock.
            await run_sync(store, lambda: None)
            child = asyncio.create_task(run_sync(store, lambda: pytest.fail("child bypassed lock")))
            await clock.drain()
            assert not child.done()
            await clock.advance_and_drain(0.06)
            with pytest.raises(CaptureError, match="capture_busy"):
                await child
            child = asyncio.create_task(run_sync(store, lambda: None))
            await clock.drain()
            child.cancel()
            with pytest.raises(asyncio.CancelledError):
                await child
        async with transaction(store):
            store.set_meta("released", True)
        assert store.meta("released")
    finally:
        store.close()


async def test_async_admission_obeys_another_process_lock(tmp_path):
    import subprocess
    import sys

    clock = MockClock()
    gate = CaptureCoordinator(tmp_path / "process.lock", clock, wait_seconds=0.05)
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
        [sys.executable, "-c", script, str(gate.path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert await asyncio.to_thread(process.stdout.readline) == "held\n"

        async def attempt():
            async with gate.admit():
                pytest.fail("cross-process lock bypassed")

        task = asyncio.create_task(attempt())
        await clock.drain()
        assert not task.done()
        await clock.advance_and_drain(0.06)
        with pytest.raises(CaptureError, match="capture_busy"):
            await task
        process.stdin.write("\n")
        process.stdin.flush()
        assert await asyncio.to_thread(process.wait, 10) == 0
        async with gate.admit():
            pass
    finally:
        if process.poll() is None:
            process.kill()
            await asyncio.to_thread(process.wait, 10)
        process.stdin.close()
        process.stdout.close()


async def test_received_receipt_survives_cancel_while_waiting_for_capture(tmp_path):
    from clytie.sdk.types import Receipt

    from .test_kernel_delivery import task_with_intents
    from .test_kernel_service import setup

    kernel, channel, clock = setup(tmp_path)
    kernel.reactions.enabled = False
    record, _, intents = await task_with_intents(kernel)
    await kernel.bus.drain()
    intent = intents[0]
    attempt = kernel.ledger.begin_attempt(intent.intent_id)
    gate = CaptureCoordinator(tmp_path / "gate.lock", clock)
    kernel.ledger.coordination = gate
    receipt = Receipt(intent_id=intent.intent_id, status="sent", at=clock.now())
    async with gate.capture():
        task = asyncio.create_task(
            kernel.delivery.persist(
                kernel.ledger.record_receipt, intent.intent_id, attempt, receipt
            )
        )
        await clock.drain()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert kernel.delivery.background
        assert kernel.ledger.get("intent", intent.intent_id)["status"] == "sending"
    await clock.advance_and_drain(0.01)
    await asyncio.gather(*tuple(kernel.delivery.background))
    assert kernel.ledger.get("intent", intent.intent_id)["status"] == "sent"
    assert not channel.text
    kernel.ledger.close()
