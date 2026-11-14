"""回想外部执行前预留容量，真实 SQLite 上保留审计和进度责任。"""

import asyncio
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from clytie.backup.coordination import run_sync
from clytie.memory.scheduler import Recollector
from clytie.sdk import CancelToken
from clytie.sdk.types import ModelRequest, ModelTurn, ToolCall
from clytie.storage.finalization import current_finalizer
from clytie.storage.owner import StorageBusy

from .test_agent import event
from .test_storage_kernel_finalizers import spin
from .test_storage_memory_flow import flow as flow
from .test_storage_stop import fill_ordinary


async def prepare(flow):
    agent = flow.agent
    collector = Recollector(agent)
    await collector.initialize()
    incoming = event(agent, "Fictional recollection window")
    await run_sync(flow.ledger, flow.ledger.claim_inbound, incoming)
    agent.clock.advance(60)
    agent.persona = SimpleNamespace(render=lambda _: "Fictional persona")
    agent.protection = SimpleNamespace(update=lambda _: None)
    calls = []
    original = agent.host.invoke

    async def invoke(*args, **kwargs):
        assert current_finalizer(flow.owner) is None
        calls.append(True)
        await collector.model.generate(
            ModelRequest(budget={"deadline": agent.clock.now()}, trace_id="a" * 32),
            args[6],
            purpose="recollect",
        )
        return await original(*args, **kwargs)

    agent.host.invoke = invoke
    return collector, incoming.conversation, calls


async def test_model_audit_and_memory_commit_survive_ordinary_capacity_full(flow):
    collector, conversation, calls = await prepare(flow)
    started, returned = asyncio.Event(), asyncio.Event()

    async def model_call(*args):
        assert current_finalizer(flow.owner) is None
        started.set()
        await returned.wait()
        return ModelTurn(finish="stop", text="[]", usage={"input_tokens": 3, "output_tokens": 2})

    flow.agent.model_call = model_call
    processing = asyncio.create_task(collector.process(conversation))
    await started.wait()
    release, commands = await fill_ordinary(flow.owner)
    try:
        returned.set()
        await spin(lambda: processing.done() or flow.owner.status()["queued"] == 64)
    finally:
        release.set()
        await asyncio.gather(*(command.result() for command in commands))
    assert (await processing)["written"] == 1
    audits = await run_sync(flow.ledger, flow.ledger.rows, "audit")
    assert [row["usage"] for row in audits if row["kind"] == "memory.recollect_model"] == [
        {"input_tokens": 3, "output_tokens": 2}
    ]
    assert len(calls) == 1
    assert flow.owner.status()["finalizers"] == 0


async def test_full_finalizer_capacity_rejects_before_consolidator_or_model(flow):
    collector, conversation, calls = await prepare(flow)

    async def model_call(*args):
        return ModelTurn(finish="stop", text="[]", usage={})

    flow.agent.model_call = model_call
    held = [flow.owner.reserve_finalizer() for _ in range(16)]
    try:
        with pytest.raises(StorageBusy):
            await collector.process(conversation)
        assert not calls
    finally:
        for token in held:
            flow.owner.release_finalizer(token, executor_finished=True, obligations_complete=True)


async def test_cancelled_waiter_keeps_late_model_audit_but_writes_no_memory(flow):
    collector, conversation, _ = await prepare(flow)
    entered, returned = asyncio.Event(), asyncio.Event()

    async def model_call(*args):
        entered.set()
        await returned.wait()
        return ModelTurn(finish="stop", text="[]", usage={"input_tokens": 7})

    flow.agent.model_call = model_call
    processing = asyncio.create_task(collector.process(conversation))
    await entered.wait()
    release, commands = await fill_ordinary(flow.owner)
    try:
        processing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await processing
        assert flow.owner.status()["finalizers"] == 1
        assert conversation.conversation_id in collector.current
        returned.set()
        await spin(lambda: flow.owner.status()["queued"] == 64)
    finally:
        returned.set()
        release.set()
        await asyncio.gather(*(command.result() for command in commands))
    await spin(lambda: flow.owner.status()["finalizers"] == 0)
    assert not await run_sync(flow.store, flow.store.all_records)
    audits = await run_sync(flow.ledger, flow.ledger.rows, "audit")
    assert len([row for row in audits if row["kind"] == "memory.recollect_model"]) == 1
    progress = await run_sync(flow.store, collector.progress, conversation.conversation_id)
    assert progress["inbound"] == 0 and progress["count"] == 1


@pytest.mark.parametrize("saturated", ["ordinary", "finalizers"])
async def test_busy_tick_warns_and_next_existing_tick_can_process(flow, saturated):
    collector, conversation, calls = await prepare(flow)
    flow.agent.host.manifests = {}
    flow.agent.host.recollection_models = {}
    collector.conversations[conversation.conversation_id] = conversation

    async def model_call(*args):
        return ModelTurn(finish="stop", text="[]", usage={})

    flow.agent.model_call = model_call
    release, commands, held = None, [], []
    if saturated == "ordinary":
        release, commands = await fill_ordinary(flow.owner)
    else:
        held = [flow.owner.reserve_finalizer() for _ in range(16)]
    running = asyncio.create_task(collector.run())
    try:
        await flow.agent.clock.advance_and_drain(60)
        await spin(
            lambda: any(row[1] == "memory.recollect_failed" for row in flow.agent.log.entries)
        )
        assert not running.done()
        assert not calls
        if release:
            release.set()
            await asyncio.gather(*(command.result() for command in commands))
        for token in held:
            flow.owner.release_finalizer(token, executor_finished=True, obligations_complete=True)
        held.clear()
        await flow.agent.clock.drain()
        assert not calls
        await flow.agent.clock.advance_and_drain(60)
        await spin(lambda: len(calls) == 1 and flow.owner.status()["finalizers"] == 0)
        assert not running.done()
        assert len(await run_sync(flow.store, flow.store.all_records)) == 1
    finally:
        if release:
            release.set()
        for token in held:
            flow.owner.release_finalizer(token, executor_finished=True, obligations_complete=True)
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_recollection_cancel_at_commit_rolls_back_and_releases_reservation(flow):
    collector, conversation, _ = await prepare(flow)

    async def model_call(*args):
        return ModelTurn(finish="stop", text="[]", usage={})

    flow.agent.model_call = model_call

    def install(resources, _):
        original = resources.memory.audit

        def audit(kind, **fields):
            original(kind, **fields)
            if kind == "memory.recollect":
                handled = threading.Event()

                def cancel():
                    collector.current[conversation.conversation_id].cancel("fictional_commit")
                    handled.set()

                flow.owner.loop.call_soon_threadsafe(cancel)
                assert handled.wait(5)

        resources.memory.audit = audit

    await flow.owner.execute(install)
    assert await collector.process(conversation) is None
    assert flow.owner.status()["finalizers"] == 0
    assert not await run_sync(flow.store, flow.store.all_records)
    progress = await run_sync(flow.store, collector.progress, conversation.conversation_id)
    assert progress["inbound"] == 0


async def test_consolidator_failure_progress_uses_same_reserved_capacity(flow):
    collector, conversation, _ = await prepare(flow)
    entered, returned = asyncio.Event(), asyncio.Event()

    async def model_call(*args):
        entered.set()
        await returned.wait()
        return ModelTurn(finish="stop", text="[]", usage={"input_tokens": 4})

    flow.agent.model_call = model_call

    async def fail_after_model(_):
        raise ValueError("fictional_consolidator_failure")

    flow.agent.host.before_result = fail_after_model
    processing = asyncio.create_task(collector.process(conversation))
    await entered.wait()
    release, commands = await fill_ordinary(flow.owner)
    try:
        returned.set()
        await spin(lambda: flow.owner.status()["queued"] == 64)
    finally:
        release.set()
        await asyncio.gather(*(command.result() for command in commands))
    with pytest.raises(ValueError, match="fictional_consolidator_failure"):
        await processing
    assert flow.owner.status()["finalizers"] == 0
    progress = await run_sync(flow.store, collector.progress, conversation.conversation_id)
    assert progress["failures"] == 1 and progress["inbound"] == 0


@pytest.mark.parametrize("cancel_after_memory_commit", [False, True])
async def test_core_reserved_commit_cancels_before_but_finishes_audit_after_commit(
    flow,
    cancel_after_memory_commit,
):
    from clytie.kernel.delivery import OwnedFinalization

    token = CancelToken()
    incoming = event(flow.agent, "Fictional core memory")
    operation = OwnedFinalization(SimpleNamespace(ledger=flow.store, background=set()))

    def install(resources, _):
        def cancel():
            handled = threading.Event()

            def on_loop():
                token.cancel("fictional_commit")
                handled.set()

            flow.owner.loop.call_soon_threadsafe(on_loop)
            assert handled.wait(5)

        if cancel_after_memory_commit:
            original = resources.memory.write

            def write(*args, **kwargs):
                result = original(*args, **kwargs)
                cancel()
                return result

            resources.memory.write = write
        else:
            original = resources.memory.set_meta

            def set_meta(key, value):
                original(key, value)
                if key == "generation":
                    cancel()

            resources.memory.set_meta = set_meta

    await flow.owner.execute(install)
    try:
        work = flow.agent.memory.execute(
            "fictional_trace",
            incoming.conversation,
            incoming.message.sender,
            ToolCall(
                call_id="write",
                tool="core_remember",
                arguments={"kind": "fact", "content": "Fictional core memory"},
            ),
            token,
            finalization=operation,
        )
        if cancel_after_memory_commit:
            assert (await work).status == "ok"
        else:
            with pytest.raises(asyncio.CancelledError):
                await work
    finally:
        operation.release()
    assert flow.owner.status()["finalizers"] == 0
    records = await run_sync(flow.store, flow.store.all_records)
    audits = await run_sync(flow.ledger, flow.ledger.rows, "audit")
    assert len(records) == int(cancel_after_memory_commit)
    assert len([row for row in audits if row["kind"] == "core.tool"]) == int(
        cancel_after_memory_commit
    )


async def test_confirmed_commit_rollback_preserves_error_count_and_releases_slot(flow):
    collector, conversation, _ = await prepare(flow)
    failure = RuntimeError("fictional_save_failure")

    async def model_call(*args):
        return ModelTurn(finish="stop", text="[]", usage={})

    flow.agent.model_call = model_call

    def install(resources, _):
        original = resources.memory._save

        def save(*args, **kwargs):
            original(*args, **kwargs)
            raise failure

        resources.memory._save = save

    await flow.owner.execute(install)
    with pytest.raises(RuntimeError) as caught:
        await collector.process(conversation)
    assert caught.value is failure
    assert flow.owner.status()["finalizers"] == 0
    progress = await run_sync(flow.store, collector.progress, conversation.conversation_id)
    assert progress["inbound"] == 0 and progress["count"] == 1
    assert progress.get("failures", 0) == 0
    assert not await run_sync(flow.store, flow.store.all_records)


async def test_post_commit_error_without_rollback_evidence_retains_reservation(flow):
    collector, conversation, _ = await prepare(flow)
    originals = []

    async def model_call(*args):
        def install(resources, _):
            original = resources.memory.transaction
            originals.append(original)

            @contextmanager
            def transaction():
                with original():
                    yield
                raise RuntimeError("fictional_post_commit_failure")

            resources.memory.transaction = transaction

        await flow.owner.execute(install)
        return ModelTurn(finish="stop", text="[]", usage={})

    flow.agent.model_call = model_call
    with pytest.raises(RuntimeError, match="fictional_post_commit_failure"):
        await collector.process(conversation)
    assert flow.owner.status()["finalizers"] == 1

    # 测试核实注入故障的实际提交结果后才清理；产品没有此自动恢复路径。
    def verify(resources, _):
        resources.memory.transaction = originals[0]
        return resources.memory.all_records(), collector.progress(conversation.conversation_id)

    records, progress = await flow.owner.execute(verify)
    assert len(records) == 1 and progress["inbound"] > 0
    token = next(iter(flow.owner._finalizers))
    flow.owner.release_finalizer(token, executor_finished=True, obligations_complete=True)
