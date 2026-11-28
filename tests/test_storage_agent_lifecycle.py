"""已接受入站及排队记录持有真实所有者收尾容量。"""

import asyncio
import threading
from contextlib import asynccontextmanager

import pytest
import yaml

from clytie.backup.coordination import run_sync
from clytie.kernel.ledger import Ledger
from clytie.sdk.behavior import Defer
from clytie.sdk.testing import MockClock
from clytie.sdk.types import ModelTurn
from clytie.storage.finalization import current_finalizer
from clytie.storage.owner import StorageBusy
from clytie.supervisor.companion import Companion

from .test_agent import event
from .test_config import write_yaml
from .test_supervisor_startup import home_copy


def private_home(tmp_path):
    home = home_copy(tmp_path)
    path = home / "clytie.local.yaml"
    instance = yaml.safe_load(path.read_text(encoding="utf-8"))
    account = instance["people"]["owner"]["accounts"][0]
    instance["conversations"]["owner_private"] = {
        "channel": account["channel"],
        "platform_id": account["account"],
        "kind": "private",
        "scene": "owner_private",
        "members_expected": ["owner", "companion_a"],
    }
    write_yaml(path, instance)
    return home


def inbound(runtime, **kwargs):
    value = event(runtime.agent, **kwargs)
    channel = next(iter(runtime.channels))
    data = value.model_dump(mode="json")
    data["channel_id"] = channel
    data["event_id"] = channel + ":" + data["message"]["ref"]["platform_message_id"]
    data["conversation"]["channel_id"] = channel
    data["message"]["sender"]["channel_id"] = channel
    data["message"]["ref"]["channel_id"] = channel
    return type(value).model_validate(data)


@asynccontextmanager
async def ready_runtime(tmp_path):
    runtime = Companion(private_home(tmp_path), "companion_a", MockClock())
    await runtime.start()
    for task in runtime.feeds:
        task.cancel()
    await asyncio.gather(*runtime.feeds, return_exceptions=True)
    await runtime.agent.drain()
    await runtime.clock.drain()
    try:
        yield runtime
    finally:
        await runtime.stop()


@asynccontextmanager
async def full_queue(owner):
    entered, release = asyncio.Event(), threading.Event()

    def hold(*_):
        owner.loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)

    commands = [owner.submit(hold)]
    try:
        await entered.wait()
        while owner.status()["ordinary"] < owner.ordinary_capacity:
            commands.append(owner.submit(lambda *_: None))
        yield
    finally:
        release.set()
        await asyncio.gather(*(command.result() for command in commands))


async def test_inbound_rejects_before_claim_when_finalizers_exhausted(tmp_path):
    runtime = Companion(private_home(tmp_path), "companion_a", MockClock())
    await runtime.start()
    owner, ledger = runtime.storage_owner, runtime.kernel.ledger
    reservations = []
    incoming = inbound(runtime)
    try:
        while owner.status()["finalizers"] < owner.finalizer_capacity:
            reservations.append(owner.reserve_finalizer())
        with pytest.raises(StorageBusy):
            await runtime.agent.receive(incoming)
        assert await run_sync(ledger, ledger.get, "inbound", incoming.event_id) is None
        assert not any(
            row["source_event_id"] == incoming.event_id
            for row in await run_sync(ledger, ledger.rows, "record")
        )
    finally:
        for token in reservations:
            owner.release_finalizer(token, executor_finished=True, obligations_complete=True)
        await runtime.stop()


async def test_full_ordinary_queue_rejects_inbound_without_claim(tmp_path):
    async with ready_runtime(tmp_path) as runtime:
        owner, ledger = runtime.storage_owner, runtime.kernel.ledger
        incoming = inbound(runtime)
        async with full_queue(owner):
            with pytest.raises(StorageBusy):
                await runtime.agent.receive(incoming)
        assert await run_sync(ledger, ledger.get, "inbound", incoming.event_id) is None


async def test_cancelled_receive_waiter_does_not_abandon_accepted_input(tmp_path, monkeypatch):
    async with ready_runtime(tmp_path) as runtime:
        owner, ledger = runtime.storage_owner, runtime.kernel.ledger
        incoming = inbound(runtime)
        entered, release = asyncio.Event(), threading.Event()
        original = Ledger.claim_inbound

        def claim(store, value, **kwargs):
            if value.event_id == incoming.event_id:
                owner.loop.call_soon_threadsafe(entered.set)
                assert release.wait(10)
            return original(store, value, **kwargs)

        monkeypatch.setattr(Ledger, "claim_inbound", claim)
        caller = asyncio.create_task(runtime.agent.receive(incoming))
        try:
            await entered.wait()
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            assert runtime.agent.intake_tasks
        finally:
            release.set()
        await runtime.agent.drain()
        rows = await run_sync(ledger, ledger.rows, "record")
        record = next(row for row in rows if row["source_event_id"] == incoming.event_id)
        assert record["exec_status"] == "ok" and record["notified"]


async def test_failure_after_acceptance_is_persisted_before_reporting_busy(tmp_path):
    async with ready_runtime(tmp_path) as runtime:
        owner, ledger = runtime.storage_owner, runtime.kernel.ledger
        incoming = inbound(runtime, text="取消", scene="public_group")
        entered, release = asyncio.Event(), asyncio.Event()
        original = runtime.agent.cancellable

        async def cancellable(*args):
            entered.set()
            await release.wait()
            return await original(*args)

        runtime.agent.cancellable = cancellable
        caller = asyncio.create_task(runtime.agent.receive(incoming))
        await entered.wait()
        async with full_queue(owner):
            release.set()
            await runtime.clock.drain()
            assert not caller.done()
        with pytest.raises(StorageBusy):
            await caller
        row = await run_sync(ledger, ledger.get, "inbound", incoming.event_id)
        assert row["state"] == "ignored" and row["reason"] == "storage_busy"


async def test_record_reuses_last_slot_for_model_and_delivery(tmp_path):
    async with ready_runtime(tmp_path) as runtime:
        owner, ledger = runtime.storage_owner, runtime.kernel.ledger
        reservations = []
        original = runtime.agent.model_call

        async def model(*args, **kwargs):
            assert current_finalizer(owner) is None
            return await original(*args, **kwargs)

        runtime.agent.model_call = model
        try:
            while owner.status()["finalizers"] < owner.finalizer_capacity - 1:
                reservations.append(owner.reserve_finalizer())
            trace = await runtime.agent.receive(inbound(runtime))
            await runtime.agent.drain()
            await runtime.clock.drain()
            record = await run_sync(ledger, ledger.get, "record", trace)
            assert record["exec_status"] == "ok"
            assert record["delivery_status"] == "sent"
            assert trace not in runtime.kernel.finalizations
        finally:
            for token in reservations:
                owner.release_finalizer(token, executor_finished=True, obligations_complete=True)


async def test_accepted_model_result_seals_under_full_ordinary_queue(tmp_path):
    async with ready_runtime(tmp_path) as runtime:
        owner, ledger = runtime.storage_owner, runtime.kernel.ledger
        entered, release_model = asyncio.Event(), asyncio.Event()
        original = runtime.agent.model_call

        async def model(method, *args, **kwargs):
            assert current_finalizer(owner) is None
            if method != "generate":
                return await original(method, *args, **kwargs)
            entered.set()
            await release_model.wait()
            return ModelTurn(finish="stop", text="Fictional output", usage={})

        runtime.agent.model_call = model
        trace = await runtime.agent.receive(inbound(runtime))
        await entered.wait()
        async with full_queue(owner):
            release_model.set()
            await runtime.clock.drain()
            assert trace in runtime.kernel.finalizations
        await runtime.agent.drain()
        await runtime.clock.drain()
        record = await run_sync(ledger, ledger.get, "record", trace)
        assert record["exec_status"] == "ok"
        assert record["delivery_status"] == "sent"
        assert record["notified"]


async def test_accepted_busy_context_records_error_without_fallback(tmp_path):
    async with ready_runtime(tmp_path) as runtime:
        owner, ledger = runtime.storage_owner, runtime.kernel.ledger
        entered, release, rejected = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def build(*args, **kwargs):
            entered.set()
            await release.wait()
            try:
                await run_sync(ledger, ledger.rows, "record")
            except StorageBusy:
                rejected.set()
                raise

        runtime.agent.context.build = build
        trace = await runtime.agent.receive(inbound(runtime))
        await entered.wait()
        async with full_queue(owner):
            release.set()
            await rejected.wait()
        await runtime.agent.drain()
        record = await run_sync(ledger, ledger.get, "record", trace)
        assert record["exec_status"] == "error"
        assert record["error"]["code"] == "storage_busy"
        assert record.get("model_calls", 0) == 0
        assert await run_sync(ledger, ledger.intents, trace) == []


async def test_queued_cancel_seals_with_reserved_capacity(tmp_path):
    async with ready_runtime(tmp_path) as runtime:
        owner, ledger = runtime.storage_owner, runtime.kernel.ledger
        runtime.kernel.queue.maximum = 0
        trace = await runtime.agent.receive(inbound(runtime))
        finalization = runtime.kernel.finalizations[trace]
        settled = asyncio.Event()
        finalization.on_complete(settled.set)
        async with full_queue(owner):
            runtime.kernel.queued[trace].cancel()
            await runtime.clock.drain()
            assert not finalization.released
        await settled.wait()
        record = await run_sync(ledger, ledger.get, "record", trace)
        assert record["exec_status"] == "cancelled"
        assert finalization.released
        assert trace not in runtime.kernel.finalizations


async def test_record_cancel_keeps_capacity_and_registry_for_late_model(tmp_path):
    async with ready_runtime(tmp_path) as runtime:
        owner, ledger = runtime.storage_owner, runtime.kernel.ledger
        entered, release = asyncio.Event(), asyncio.Event()
        original = runtime.agent.model_call

        async def model(method, *args, **kwargs):
            if method != "generate":
                return await original(method, *args, **kwargs)
            assert current_finalizer(owner) is None
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
            return ModelTurn(finish="stop", text="Fictional late model result", usage={})

        runtime.agent.model_call = model
        trace = await runtime.agent.receive(inbound(runtime))
        await entered.wait()
        finalization = runtime.kernel.finalizations[trace]
        settled = asyncio.Event()
        finalization.on_complete(settled.set)
        try:
            runtime.kernel.tokens[trace].cancel("cancelled")
            runtime.kernel.record_tasks[trace].cancel()
            await runtime.agent.drain()
            record = await run_sync(ledger, ledger.get, "record", trace)
            assert record["exec_status"] == "cancelled"
            assert trace in runtime.kernel.finalizations and not finalization.released
            async with full_queue(owner):
                release.set()
                await runtime.clock.drain()
                assert not finalization.released
            await settled.wait()
            audits = await run_sync(ledger, ledger.rows, "audit")
            assert any(
                row["kind"] == "model.late_result"
                and row["trace_id"] == trace
                and row["content"]["text"] == "Fictional late model result"
                for row in audits
            )
            assert trace not in runtime.kernel.finalizations
        finally:
            release.set()


async def test_deferred_batch_cancel_accounts_for_every_accepted_event(tmp_path):
    async with ready_runtime(tmp_path) as runtime:
        owner, ledger = runtime.storage_owner, runtime.kernel.ledger
        original = runtime.host.behavior

        async def behavior(cid, hook, *args, **kwargs):
            if hook == "attention":
                return Defer(seconds=5)
            return await original(cid, hook, *args, **kwargs)

        runtime.host.behavior = behavior
        before = set(runtime.agent.background)
        first, second = inbound(runtime), inbound(runtime)
        assert await runtime.agent.receive(first) is None
        assert await runtime.agent.receive(second) is None
        responsibilities = next(iter(runtime.agent.batches.values()))["finalizations"]
        tasks = set(runtime.agent.background) - before
        async with full_queue(owner):
            for task in tasks:
                task.cancel()
            await runtime.clock.drain()
            assert not responsibilities[0].released
        await asyncio.gather(*tasks, return_exceptions=True)
        assert not runtime.agent.batches
        for incoming in (first, second):
            row = await run_sync(ledger, ledger.get, "inbound", incoming.event_id)
            assert row["state"] == "ignored"
        assert all(responsibility.released for responsibility in responsibilities)


async def test_failed_inbound_transaction_rolls_back_before_model(tmp_path, monkeypatch):
    async with ready_runtime(tmp_path) as runtime:
        incoming = inbound(runtime)
        ledger = runtime.kernel.ledger
        original = Ledger.put
        model_calls = []

        def fail_write(store, table, key, value):
            original(store, table, key, value)
            if table == "inbound" and key == incoming.event_id:
                raise ValueError("fictional_write_failure")

        async def model(*args, **kwargs):
            model_calls.append(args)
            pytest.fail("model called after failed transaction")

        monkeypatch.setattr(Ledger, "put", fail_write)
        monkeypatch.setattr(runtime.agent, "model_call", model)
        with pytest.raises(ValueError, match="fictional_write_failure"):
            await runtime.agent.receive(incoming)
        await runtime.agent.drain()
        assert not model_calls
        assert await run_sync(ledger, ledger.get, "inbound", incoming.event_id) is None
        assert not any(
            row["event_id"] == incoming.event_id
            for row in await run_sync(ledger, ledger.rows, "dedupe")
        )
