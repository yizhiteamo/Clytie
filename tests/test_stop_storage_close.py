"""Exercise live storage ownership with a fictional instance."""

import asyncio
import sqlite3

import pytest

from clytie.backup.capture import capture_group
from clytie.backup.coordination import run_sync
from clytie.kernel.delivery import OwnedFinalization

from .test_storage_agent_lifecycle import inbound, ready_runtime
from .test_storage_kernel_finalizers import spin


async def test_failed_quote_lookup_after_durable_ignore_does_not_hold_close(tmp_path):
    async with ready_runtime(tmp_path) as runtime:
        owner, ledger = runtime.storage_owner, runtime.kernel.ledger
        message = inbound(runtime, reply_to=inbound(runtime).message.ref)
        reservations = set(owner._finalizers)

        def deny_lookup(action, table, *unused):
            if action == sqlite3.SQLITE_READ and table == "attempt":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        await owner.execute(lambda stores, _: stores.ledger.db.set_authorizer(deny_lookup))
        try:
            with pytest.raises(sqlite3.DatabaseError):
                await runtime.agent.receive(message)
        finally:
            await owner.execute(
                lambda stores, _: stores.ledger.db.set_authorizer(lambda *args: sqlite3.SQLITE_OK)
            )
        row = await run_sync(ledger, ledger.get, "inbound", message.event_id)
        assert row["state"] == "ignored"
        for table in ("record", "invocation", "intent"):
            assert not await run_sync(ledger, ledger.rows, table)
        stopping = asyncio.create_task(runtime.stop("wait"))
        try:
            await spin(lambda: stopping.done() or not owner.status()["accepting"])
            assert not (owner._finalizers - reservations), "completed inbound still holds a slot"
        finally:
            # Only the injected read failed. The terminal inbound above is durable.
            for token in owner._finalizers - reservations:
                owner.release_finalizer(token, executor_finished=True, obligations_complete=True)
            await stopping
        assert not owner._thread.is_alive()


@pytest.mark.parametrize("hold", ["capture", "stop_callback", "unconfirmed_write"])
async def test_storage_close_waits_for_actual_responsibility(tmp_path, hold, monkeypatch):
    async with ready_runtime(tmp_path) as runtime:
        owner = runtime.storage_owner
        ledger = runtime.kernel.ledger
        gate = asyncio.Event()
        if hold == "capture":
            view = await capture_group(
                {"ledger": runtime.runtime.path},
                ledger.coordination,
                epoch="fictional",
                owner=owner,
            )
        elif hold == "stop_callback":
            lifecycle = runtime.stop_finalization
            finishing = asyncio.Event()
            finish = lifecycle.finish

            async def observe_finish():
                finishing.set()
                await finish()

            monkeypatch.setattr(lifecycle, "finish", observe_finish)
            waiter = asyncio.create_task(lifecycle.callback(gate.wait()))
            await spin(lambda: bool(lifecycle.tasks))
        else:
            operation = OwnedFinalization(runtime.kernel.delivery)

            def write_then_fail():
                with ledger.transaction():
                    ledger.audit("fictional_committed")
                raise RuntimeError("fictional_result_lost")

            with pytest.raises(RuntimeError, match="fictional_result_lost"):
                await operation.persist(write_then_fail)
            operation.release()
            assert operation.failed
        stopping = asyncio.create_task(runtime.stop("wait"))
        try:
            if hold == "stop_callback":
                await spin(finishing.is_set)
                assert owner.status()["accepting"]
            else:
                await spin(lambda: not owner.status()["accepting"])
            assert owner._thread.is_alive() and not stopping.done()
            expected = {
                "capture": "capture",
                "stop_callback": "stop_finalizer",
                "unconfirmed_write": "finalizer",
            }[hold]
            assert f"storage_close.{expected}=1" in runtime.health()["warnings"]
            runtime.health()
            assert runtime.log.entries.count(("warning", "storage.close_wait", {expected: 1})) == 1
        finally:
            if hold == "capture":
                await view.close()
            elif hold == "stop_callback":
                gate.set()
                await waiter
            else:
                # Verify the test's post-commit outcome before returning its slot.
                rows = await operation.read(ledger.rows, "audit")
                assert operation.failed and not operation.released
                assert sum(r["kind"] == "fictional_committed" for r in rows) == 1
                owner.release_finalizer(
                    operation.token, executor_finished=True, obligations_complete=True
                )
            await stopping
        assert not owner._thread.is_alive()
