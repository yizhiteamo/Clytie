"""真实所有者上的必要收尾容量；仅虚构能力、平台与 SQLite。"""

import asyncio
import threading

import pytest

from clytie.backup.coordination import run_sync
from clytie.sdk.types import Receipt, ToolCall
from clytie.storage.owner import StorageBusy

from .test_kernel_service import Capability, new_record, reply
from .test_storage_kernel import owned_kernel


async def spin(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)  # noqa: TID251 -- 等待真实所有者线程，不是产品时钟。

    await asyncio.wait_for(wait(), 5)  # noqa: TID251 -- 失败保护，不驱动产品截止时间。


async def test_capacity_rejection_precedes_tool_prepare_and_platform_send(tmp_path):
    async with owned_kernel(tmp_path) as (kernel, channel):
        record, invocation, _ = await new_record(kernel)
        kernel.reactions.enabled = False
        intent = reply(record)
        await run_sync(kernel.ledger, kernel.ledger.register_intents, record["trace_id"], [intent])
        owner = kernel.ledger._owner
        reservations = [owner.reserve_finalizer() for _ in range(owner.finalizer_capacity)]
        capability = Capability(kernel.clock)
        try:
            with pytest.raises(StorageBusy):
                await kernel.execute(
                    record["trace_id"],
                    "echo-tool",
                    ToolCall(call_id="call", tool="echo", arguments={}),
                    capability,
                    invocation,
                )
            assert capability.prepared == capability.executed == 0
            with pytest.raises(StorageBusy):
                await kernel.delivery.send_one(intent.intent_id)
            assert not channel.text
        finally:
            for reservation in reservations:
                owner.release_finalizer(
                    reservation, executor_finished=True, obligations_complete=True
                )


async def test_receipt_uses_reserved_capacity_with_all_ordinary_slots_full(tmp_path):
    async with owned_kernel(tmp_path) as (kernel, channel):
        record, _, _ = await new_record(kernel)
        intent = reply(record)
        await kernel.seal(record["trace_id"], "ok", [intent])
        entered, returned = asyncio.Event(), asyncio.Event()

        async def send(value, cancel):
            entered.set()
            await returned.wait()
            return Receipt(intent_id=value.intent_id, status="sent", at=kernel.clock.now())

        channel.send = send
        sending = asyncio.create_task(kernel.delivery.send_one(intent.intent_id))
        await entered.wait()
        owner = kernel.ledger._owner
        blocked, release = threading.Event(), threading.Event()

        def hold(*_):
            blocked.set()
            assert release.wait(5)

        commands = [owner.submit(hold)]
        await spin(blocked.is_set)
        commands.extend(owner.submit(lambda *_: None) for _ in range(owner.ordinary_capacity - 1))
        returned.set()
        await spin(lambda: sending.done() or owner.status()["queued"] == owner.ordinary_capacity)
        release.set()
        await asyncio.gather(*(command.result() for command in commands))
        assert await sending == "sent"
        await spin(lambda: owner.status()["finalizers"] == 0)


async def test_caller_cancellation_retains_late_send_obligation(tmp_path):
    async with owned_kernel(tmp_path) as (kernel, channel):
        record, _, _ = await new_record(kernel)
        intent = reply(record)
        await kernel.seal(record["trace_id"], "ok", [intent])
        entered, returned = asyncio.Event(), asyncio.Event()

        async def send(value, cancel):
            entered.set()
            await returned.wait()
            return Receipt(intent_id=value.intent_id, status="sent", at=kernel.clock.now())

        channel.send = send
        sending = asyncio.create_task(kernel.delivery.send_one(intent.intent_id))
        await entered.wait()
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
        assert kernel.ledger._owner.status()["finalizers"] == 1
        returned.set()
        await spin(lambda: kernel.ledger._owner.status()["finalizers"] == 0)
        row = await run_sync(kernel.ledger, kernel.ledger.get, "intent", intent.intent_id)
        assert row["status"] == "sent"


@pytest.mark.parametrize("flag", ["silenced", "session_ending"])
async def test_state_change_while_begin_attempt_is_queued_prevents_send(tmp_path, flag):
    async with owned_kernel(tmp_path) as (kernel, channel):
        record, _, _ = await new_record(kernel)
        intent = reply(record)
        await kernel.seal(record["trace_id"], "ok", [intent])
        reached, release = threading.Event(), threading.Event()
        original = kernel.delivery._begin_attempt

        def waiting(*args):
            reached.set()
            assert release.wait(5)
            return original(*args)

        kernel.delivery._begin_attempt = waiting
        sending = asyncio.create_task(kernel.delivery.send_one(intent.intent_id))
        await spin(reached.is_set)
        setattr(kernel.delivery, flag, True)
        release.set()
        assert await sending == "failed"
        assert not channel.text
        assert not await run_sync(kernel.ledger, kernel.ledger.rows, "attempt")


async def test_cancel_during_invocation_registration_finishes_without_executing(tmp_path):
    async with owned_kernel(tmp_path) as (kernel, _):
        record, invocation, _ = await new_record(kernel)
        kernel.reactions.enabled = False
        reached, release = threading.Event(), threading.Event()

        def intercept(resources, _):
            original = resources.ledger.claim_invocation

            def claim(*args, **kwargs):
                result = original(*args, **kwargs)
                reached.set()
                assert release.wait(5)
                return result

            resources.ledger.claim_invocation = claim

        await kernel.ledger._owner.execute(intercept)
        capability = Capability(kernel.clock)
        execution = asyncio.create_task(
            kernel.execute(
                record["trace_id"],
                "echo-tool",
                ToolCall(call_id="call", tool="echo", arguments={}),
                capability,
                invocation,
            )
        )
        await spin(reached.is_set)
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
        assert kernel.ledger._owner.status()["finalizers"] == 1
        release.set()
        await spin(lambda: kernel.ledger._owner.status()["finalizers"] == 0)
        rows = await run_sync(kernel.ledger, kernel.ledger.rows, "invocation")
        assert rows[0]["status"] == "cancelled"
        assert capability.executed == 0


async def test_tool_ignoring_cancellation_retains_slot_through_late_audit(tmp_path):
    async with owned_kernel(tmp_path) as (kernel, _):
        record, invocation, _ = await new_record(kernel)
        kernel.reactions.enabled = False
        entered, released = asyncio.Event(), asyncio.Event()
        from clytie.storage.finalization import current_finalizer

        class Late(Capability):
            async def execute(self, plan, inv, cancel):
                assert current_finalizer(kernel.ledger._owner) is None
                entered.set()
                try:
                    await released.wait()
                except asyncio.CancelledError:
                    await released.wait()
                return await super().execute(plan, inv, cancel)

        execution = asyncio.create_task(
            kernel.execute(
                record["trace_id"],
                "echo-tool",
                ToolCall(call_id="call", tool="echo", arguments={}),
                Late(kernel.clock),
                invocation,
            )
        )
        await entered.wait()
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
        assert kernel.ledger._owner.status()["finalizers"] == 1
        rows = await run_sync(kernel.ledger, kernel.ledger.rows, "invocation")
        assert rows[0]["status"] == "cancelled"
        released.set()
        await spin(lambda: kernel.ledger._owner.status()["finalizers"] == 0)
        audits = await run_sync(kernel.ledger, kernel.ledger.rows, "audit")
        assert len([item for item in audits if item["kind"] == "late_result"]) == 1


async def test_unknown_retains_slot_until_late_receipt_without_resend(tmp_path):
    async with owned_kernel(tmp_path) as (kernel, channel):
        record, _, _ = await new_record(kernel)
        intent = reply(record)
        await kernel.seal(record["trace_id"], "ok", [intent])
        entered, released = asyncio.Event(), asyncio.Event()
        calls = []

        async def send(value, cancel):
            from clytie.storage.finalization import current_finalizer

            assert current_finalizer(kernel.ledger._owner) is None
            calls.append(value)
            entered.set()
            await released.wait()
            return Receipt(intent_id=value.intent_id, status="sent", at=kernel.clock.now())

        channel.send = send
        sending = asyncio.create_task(kernel.delivery.send_one(intent.intent_id))
        await entered.wait()
        await kernel.clock.advance_and_drain(60)
        await kernel.clock.advance_and_drain(5)
        assert await sending == "unknown"
        assert kernel.ledger._owner.status()["finalizers"] == 1
        assert await kernel.delivery.send_one(intent.intent_id) == "unknown"
        released.set()
        await spin(lambda: kernel.ledger._owner.status()["finalizers"] == 0)
        audits = await run_sync(kernel.ledger, kernel.ledger.rows, "audit")
        assert len([item for item in audits if item["kind"] == "late_receipt"]) == 1
        assert len(calls) == 1


async def test_failed_receipt_commit_retains_capacity_until_obligation_is_repaired(tmp_path):
    from clytie.storage.finalization import finalizer_scope

    async with owned_kernel(tmp_path) as (kernel, _):
        record, _, _ = await new_record(kernel)
        intent = reply(record)
        await kernel.seal(record["trace_id"], "ok", [intent])
        owner = kernel.ledger._owner

        def break_once(resources, _):
            original = resources.ledger.record_receipt

            def fail(*args, **kwargs):
                resources.ledger.record_receipt = original
                raise OSError("fictional disk failure")

            resources.ledger.record_receipt = fail

        await owner.execute(break_once)
        with pytest.raises(OSError, match="fictional disk failure"):
            await kernel.delivery.send_one(intent.intent_id)
        assert owner.status()["finalizers"] == 1
        row = await run_sync(kernel.ledger, kernel.ledger.get, "intent", intent.intent_id)
        assert row["status"] == "sending"
        # 测试显式修复所注入的故障；产品不得把失败自动声称为已收尾。
        token = next(iter(owner._finalizers))
        with finalizer_scope(token):
            await run_sync(
                kernel.ledger,
                kernel.ledger.record_receipt,
                intent.intent_id,
                1,
                Receipt(intent_id=intent.intent_id, status="sent", at=kernel.clock.now()),
            )
        owner.release_finalizer(token, executor_finished=True, obligations_complete=True)


async def test_reaction_registration_and_send_share_one_reservation(tmp_path):
    from clytie.storage.finalization import current_finalizer

    async with owned_kernel(tmp_path) as (kernel, channel):
        record, invocation, _ = await new_record(kernel)
        kernel.reactions.enabled = False
        await kernel.execute(
            record["trace_id"],
            "echo-tool",
            ToolCall(call_id="call", tool="echo", arguments={}),
            Capability(kernel.clock),
            invocation,
        )
        owner = kernel.ledger._owner
        held = [owner.reserve_finalizer() for _ in range(owner.finalizer_capacity - 1)]
        original = channel.react

        async def react(value, cancel):
            assert current_finalizer(owner) is None
            assert owner.status()["finalizers"] == owner.finalizer_capacity
            return await original(value, cancel)

        channel.react = react
        try:
            await kernel.reactions._send(record["trace_id"], "done")
            assert len(channel.calls) == 1
            assert owner.status()["finalizers"] == len(held)
        finally:
            for token in held:
                owner.release_finalizer(token, executor_finished=True, obligations_complete=True)


async def test_record_tool_reactions_and_delivery_reuse_one_of_sixteen_slots(tmp_path):
    from clytie.kernel.delivery import OwnedFinalization

    async with owned_kernel(tmp_path) as (kernel, channel):
        record, invocation, _ = await new_record(kernel)
        trace = record["trace_id"]
        operation = OwnedFinalization(kernel.delivery)
        kernel.finalizations[trace] = operation
        operation.on_complete(lambda: kernel.finalizations.pop(trace))
        owner = kernel.ledger._owner
        held = [owner.reserve_finalizer() for _ in range(owner.finalizer_capacity - 1)]
        kernel.reactions.interval = 0
        try:
            result = await kernel.execute(
                trace,
                "echo-tool",
                ToolCall(call_id="call", tool="echo", arguments={}),
                Capability(kernel.clock),
                invocation,
            )
            assert result.status == "ok"
            await kernel.seal(trace, "ok", [reply(record)])
            await kernel.delivery.deliver(trace, finalization=operation)
            operation.release()
            await kernel.reactions.drain()
            await spin(lambda: operation.released)
            assert trace not in kernel.finalizations
            assert owner.status()["finalizers"] == len(held)
            assert len(channel.text) == 1
            assert channel.calls[-1].emoji == "✅"
        finally:
            operation.release()
            for token in held:
                owner.release_finalizer(token, executor_finished=True, obligations_complete=True)


async def test_cancelled_queue_future_does_not_release_running_worker_responsibility(tmp_path):
    from clytie.kernel.delivery import OwnedFinalization

    async with owned_kernel(tmp_path) as (kernel, _):
        record, invocation, _ = await new_record(kernel)
        trace = record["trace_id"]
        finalization = OwnedFinalization(kernel.delivery)
        kernel.finalizations[trace] = finalization
        finalization.on_complete(lambda: kernel.finalizations.pop(trace))
        entered, returned = asyncio.Event(), asyncio.Event()

        async def work():
            entered.set()
            await returned.wait()
            await kernel.persist(trace, kernel.ledger.audit, "fictional_completed", trace_id=trace)

        future = await kernel.submit(trace, invocation.actor, work)
        await entered.wait()
        future.cancel()
        finalization.release()
        assert not finalization.released
        assert trace in kernel.finalizations
        returned.set()
        await spin(lambda: finalization.released)
        assert trace not in kernel.finalizations
        assert kernel.ledger._owner.status()["finalizers"] == 0


async def test_deadline_and_caller_cancel_do_not_duplicate_late_finalization(tmp_path):
    async with owned_kernel(tmp_path) as (kernel, _):
        record, invocation, _ = await new_record(kernel)
        kernel.reactions.enabled = False
        entered, returned = asyncio.Event(), asyncio.Event()
        writing, release_write = threading.Event(), threading.Event()

        class Late(Capability):
            async def execute(self, plan, inv, cancel):
                entered.set()
                try:
                    await returned.wait()
                except asyncio.CancelledError:
                    await returned.wait()
                return await super().execute(plan, inv, cancel)

        def intercept(resources, _):
            original = resources.ledger.finish_invocation

            def finish(identifier, result):
                if result.status == "cancelled":
                    writing.set()
                    assert release_write.wait(5)
                return original(identifier, result)

            resources.ledger.finish_invocation = finish

        await kernel.ledger._owner.execute(intercept)
        execution = asyncio.create_task(
            kernel.execute(
                record["trace_id"],
                "echo-tool",
                ToolCall(call_id="call", tool="echo", arguments={}),
                Late(kernel.clock),
                invocation,
                timeout_s=1,
            )
        )
        await entered.wait()
        await kernel.clock.advance_and_drain(1)
        await kernel.clock.advance_and_drain(5)
        await spin(writing.is_set)
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
        release_write.set()
        assert kernel.ledger._owner.status()["finalizers"] == 1
        returned.set()
        await spin(lambda: kernel.ledger._owner.status()["finalizers"] == 0)
        rows = await run_sync(kernel.ledger, kernel.ledger.rows, "invocation")
        assert rows[0]["status"] == "cancelled"
        assert rows[0]["error"]["code"] == "deadline"
        audits = await run_sync(kernel.ledger, kernel.ledger.rows, "audit")
        assert len([item for item in audits if item["kind"] == "late_result"]) == 1


async def test_executor_cancelled_error_is_persisted_before_releasing_slot(tmp_path):
    async with owned_kernel(tmp_path) as (kernel, _):
        record, invocation, _ = await new_record(kernel)
        kernel.reactions.enabled = False

        class Cancelled(Capability):
            async def execute(self, plan, inv, cancel):
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await kernel.execute(
                record["trace_id"],
                "echo-tool",
                ToolCall(call_id="call", tool="echo", arguments={}),
                Cancelled(kernel.clock),
                invocation,
            )
        await spin(lambda: kernel.ledger._owner.status()["finalizers"] == 0)
        rows = await run_sync(kernel.ledger, kernel.ledger.rows, "invocation")
        assert rows[0]["status"] == "cancelled"
