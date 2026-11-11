"""虚构伴侣停止期间的固定容量、回调租约与真实线程退出。"""

import asyncio
import threading

import pytest

from clytie.backup.coordination import run_sync
from clytie.sdk.testing import MockClock
from clytie.sdk.types import MemoryDraft
from clytie.storage.finalization import StopFinalization
from clytie.storage.owner import StorageBusy
from clytie.supervisor.companion import Companion

from .test_storage_kernel import owned_kernel
from .test_storage_kernel_finalizers import spin
from .test_supervisor_startup import home_copy


async def fill_ordinary(owner):
    entered, release = threading.Event(), threading.Event()

    def hold(*_):
        entered.set()
        assert release.wait(5)

    commands = [owner.submit(hold)]
    await spin(entered.is_set)
    commands.extend(owner.submit(lambda *_: None) for _ in range(63))
    return release, commands


async def test_stop_uses_startup_reservation_when_both_capacities_are_full(tmp_path):
    runtime = Companion(home_copy(tmp_path), "companion_a", MockClock())
    await runtime.start()
    owner = runtime.storage_owner
    try:
        await runtime.agent.drain()
        await spin(lambda: owner.status()["finalizers"] == 1)
        assert owner.status()["finalizers"] == 1
        held = [owner.reserve_finalizer() for _ in range(15)]
        release, commands = await fill_ordinary(owner)
        stopping = asyncio.create_task(runtime.stop())
        try:
            await spin(lambda: stopping.done() or owner.status()["queued"] == 64)
            assert not stopping.done()
        finally:
            release.set()
            await asyncio.gather(*(command.result() for command in commands))
            for token in held:
                owner.release_finalizer(token, executor_finished=True, obligations_complete=True)
        assert (await stopping)["state"] == "stopped"
        assert not owner._thread.is_alive()
    finally:
        await runtime.stop()


async def test_stop_callback_memory_write_survives_full_ordinary_capacity(tmp_path):
    runtime = Companion(home_copy(tmp_path), "companion_a", MockClock())
    await runtime.start()
    store = runtime.host.memory_store("companion_a")
    plugin = runtime.host.runtimes["companion_a", "fake-model"]
    unit = (plugin.units or [plugin])[0]
    original = unit.plugin.stop
    ready = asyncio.Event()
    state = {}
    written = []

    async def stop():
        state["release"], state["commands"] = await fill_ordinary(runtime.storage_owner)
        ready.set()
        written.append(
            await store.for_permissions(["memory.write"]).write(
                MemoryDraft(
                    kind="fact",
                    content="Fictional shutdown observation",
                    visibility="public",
                    source_type="group",
                )
            )
        )
        await original()

    unit.plugin.stop = stop
    stopping = asyncio.create_task(runtime.stop())
    await ready.wait()
    try:
        await spin(lambda: stopping.done() or runtime.storage_owner.status()["queued"] == 64)
    finally:
        state["release"].set()
        await asyncio.gather(*(command.result() for command in state["commands"]))
    result = await stopping
    assert result["state"] == "stopped"
    assert len(written) == 1


async def test_timed_out_callback_keeps_owner_until_real_late_write_finishes(tmp_path):
    clock = MockClock()
    runtime = Companion(home_copy(tmp_path), "companion_a", clock)
    await runtime.start()
    store = runtime.host.memory_store("companion_a")
    plugin = runtime.host.runtimes["companion_a", "fake-model"]
    unit = (plugin.units or [plugin])[0]
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    written = []

    async def stop():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        written.append(
            await store.for_permissions(["memory.write"]).write(
                MemoryDraft(
                    kind="fact",
                    content="Fictional late shutdown observation",
                    visibility="public",
                    source_type="group",
                )
            )
        )

    unit.plugin.stop = stop
    stopping = asyncio.create_task(runtime.stop())
    await entered.wait()
    await clock.advance_and_drain(10)
    await cancelled.wait()
    assert runtime.storage_owner._thread.is_alive()
    assert not runtime.stop_finalization.released
    assert not stopping.done()
    release.set()
    result = await stopping
    assert result["state"] == "not_stopped"
    assert result["not_stopped"]["fake-model"] == "TimeoutError"
    assert len(written) == 1
    assert not runtime.storage_owner._thread.is_alive()


async def test_stop_scope_rejects_extra_ready_commands_without_waiter_queue(tmp_path):
    async with owned_kernel(tmp_path) as (kernel, _):
        lifecycle = StopFinalization(kernel.ledger)
        release, commands = await fill_ordinary(kernel.ledger._owner)

        async def stop():
            first = asyncio.create_task(run_sync(kernel.ledger, kernel.ledger.rows, "record"))
            await spin(lambda: lifecycle.busy)
            with pytest.raises(StorageBusy):
                await run_sync(kernel.ledger, kernel.ledger.rows, "intent")
            assert kernel.ledger._owner.status()["queued"] == 64
            release.set()
            await first

        try:
            await asyncio.create_task(lifecycle.callback(stop()))
        finally:
            release.set()
            await asyncio.gather(*(command.result() for command in commands))
            await lifecycle.finish()


async def test_expired_callback_child_cannot_borrow_stop_reservation(tmp_path):
    async with owned_kernel(tmp_path) as (kernel, _):
        lifecycle = StopFinalization(kernel.ledger)
        release, commands = await fill_ordinary(kernel.ledger._owner)
        proceed = asyncio.Event()
        children = []

        async def child():
            await proceed.wait()
            with pytest.raises(StorageBusy):
                await run_sync(kernel.ledger, kernel.ledger.rows, "record")

        async def stop():
            children.append(asyncio.create_task(child()))

        try:
            callback = asyncio.create_task(lifecycle.callback(stop()))
            await callback
            await lifecycle.finish()
            proceed.set()
            await children[0]
            assert kernel.ledger._owner.status()["queued"] == 63
        finally:
            release.set()
            await asyncio.gather(*(command.result() for command in commands))


async def test_callback_exit_does_not_release_still_running_storage_command(tmp_path):
    async with owned_kernel(tmp_path) as (kernel, _):
        lifecycle = StopFinalization(kernel.ledger)
        entered, release = threading.Event(), threading.Event()

        def command():
            entered.set()
            assert release.wait(5)
            kernel.ledger.audit("fictional_stop_write")

        async def stop():
            await run_sync(kernel.ledger, command)

        callback = asyncio.create_task(lifecycle.callback(stop()))
        await spin(entered.is_set)
        callback.cancel()
        with pytest.raises(asyncio.CancelledError):
            await callback
        finishing = asyncio.create_task(lifecycle.finish())
        try:
            assert lifecycle.busy
            assert not lifecycle.released
            assert not finishing.done()
        finally:
            release.set()
            await finishing
        rows = await run_sync(kernel.ledger, kernel.ledger.rows, "audit")
        assert any(row["kind"] == "fictional_stop_write" for row in rows)


async def test_stop_scope_does_not_lend_capacity_to_another_companion(tmp_path):
    first_path, second_path = tmp_path / "first", tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()
    async with owned_kernel(first_path) as (first, _), owned_kernel(second_path) as (second, _):
        lifecycle = StopFinalization(first.ledger)
        release, commands = await fill_ordinary(second.ledger._owner)
        try:
            with lifecycle.scope(), pytest.raises(StorageBusy):
                await run_sync(second.ledger, second.ledger.rows, "record")
            assert second.ledger._owner.status()["finalizers"] == 0
            assert not lifecycle.busy
        finally:
            release.set()
            await asyncio.gather(*(command.result() for command in commands))
            await lifecycle.finish()
