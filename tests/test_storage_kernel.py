"""真实 SQLite 所有者上的账本闭环；外部能力及平台必须留在事件循环。"""

import threading
from contextlib import asynccontextmanager

from clytie.backup.coordination import run_sync
from clytie.kernel.ledger import Ledger
from clytie.kernel.service import Kernel
from clytie.memory.store import MemoryStore
from clytie.sdk.testing import MockClock
from clytie.sdk.types import Actor, Conversation, Invocation, ToolCall
from clytie.storage.handle import StoreHandle
from clytie.storage.owner import StorageOwner

from .test_kernel_service import Capability, SendingChannel, new_record, reply


@asynccontextmanager
async def owned_kernel(tmp_path):
    clock = MockClock()
    loop_thread = threading.get_ident()

    class Resources:
        def __init__(self):
            assert threading.get_ident() != loop_thread
            self.ledger = Ledger(tmp_path / "ledger.sqlite3", "companion_a", clock)
            self.memory = MemoryStore(
                tmp_path / "memory.sqlite3", clock=clock, config={}, instance="fictional"
            )

        def close(self):
            self.memory.close()
            self.ledger.close()

    owner = await StorageOwner.start(Resources, companion_id="companion_a", clock=clock)
    ledger = StoreHandle(owner, "ledger", Ledger, {"companion_id": "companion_a", "clock": clock})

    class Channel(SendingChannel):
        def features(self):
            assert threading.get_ident() == loop_thread
            return super().features()

        async def send(self, intent, cancel):
            assert threading.get_ident() == loop_thread
            return await super().send(intent, cancel)

        async def react(self, intent, cancel):
            assert threading.get_ident() == loop_thread
            return await super().react(intent, cancel)

    channel = Channel(clock)
    kernel = Kernel(
        ledger, clock, {"fake": channel}, permissions=lambda *_: True, quota=lambda *_: True
    )
    try:
        yield kernel, channel
    finally:
        await kernel.reactions.drain()
        await kernel.bus.drain()
        await owner.close()


async def test_owned_kernel_executes_and_delivers_without_loop_storage(tmp_path):
    async with owned_kernel(tmp_path) as (kernel, channel):
        actor = Actor(actor_id="owner", role="owner", channel_id="fake")
        conversation = Conversation(
            conversation_id="test_group", kind="group", scene="public_group", channel_id="fake"
        )
        invocation = Invocation(
            companion_id="companion_a", actor=actor, conversation=conversation, trace_id="a" * 32
        )
        record = await kernel.open_record(invocation=invocation)
        trace = record["trace_id"]
        result = await kernel.execute(
            trace,
            "echo-tool",
            ToolCall(call_id="call", tool="echo", arguments={}),
            Capability(kernel.clock),
            invocation,
        )
        assert result.status == "ok"
        record = await run_sync(kernel.ledger, kernel.ledger.get, "record", trace)
        await kernel.seal(trace, "ok", [reply(record)])
        await kernel.delivery.deliver(trace)
        record = await run_sync(kernel.ledger, kernel.ledger.get, "record", trace)
        assert record["delivery_status"] == "sent"
        assert record["notified"]
        assert len(channel.text) == 1


async def test_owned_inbound_reactions_and_duplicate_execution(tmp_path):
    async with owned_kernel(tmp_path) as (kernel, channel):
        record, invocation, _ = await new_record(kernel)
        trace = record["trace_id"]
        capability = Capability(kernel.clock)
        call = ToolCall(call_id="call", tool="echo", arguments={})
        assert (
            await kernel.execute(trace, "echo-tool", call, capability, invocation)
        ).status == "ok"
        duplicate = await kernel.execute(trace, "echo-tool", call, capability, invocation)
        assert duplicate["status"] == "ok"
        assert capability.executed == 1
        await kernel.seal(trace, "ok")
        await kernel.reactions.drain()
        assert channel.calls[-1].emoji == "✅"


async def test_owned_queue_and_cancel_keep_callbacks_on_loop(tmp_path):
    async with owned_kernel(tmp_path) as (kernel, _):
        record, invocation, _ = await new_record(kernel)
        loop_thread = threading.get_ident()

        async def operation():
            assert threading.get_ident() == loop_thread
            return "ran"

        future = await kernel.submit(record["trace_id"], invocation.actor, operation)
        assert await future == "ran"
        assert await kernel.cancel("companion_a", invocation.actor, "test_group") == "not_found"
        other, _, event = await new_record(kernel, name="other")
        assert (
            await kernel.cancel(
                "companion_a", invocation.actor, "test_group", reply_to=event.message.ref
            )
            == "cancelled"
        )
        cancelled = await run_sync(kernel.ledger, kernel.ledger.get, "record", other["trace_id"])
        assert cancelled["exec_status"] == "cancelled"


async def test_owned_unknown_receipt_never_resends(tmp_path):
    async with owned_kernel(tmp_path) as (kernel, channel):
        record, _, _ = await new_record(kernel)
        intent = reply(record)
        channel.receipts = ["unknown"]
        await kernel.seal(record["trace_id"], "ok", [intent])
        await kernel.delivery.deliver(record["trace_id"])
        await kernel.delivery.deliver(record["trace_id"])
        row = await run_sync(kernel.ledger, kernel.ledger.get, "intent", intent.intent_id)
        assert row["status"] == "unknown"
        assert row["attempts"] == len(channel.text) == 1
