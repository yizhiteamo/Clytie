"""通过假的 Invocation/DeliveryIntent 跑执行、取消与交付闭环。"""

import asyncio
from uuid import uuid4

import pytest

from clytie.kernel.ledger import Ledger
from clytie.kernel.service import Kernel, record_scope
from clytie.sdk.testing import MockClock
from clytie.sdk.types import (
    Actor,
    Conversation,
    DeliveryIntent,
    EventEnvelope,
    Invocation,
    Plan,
    Receipt,
    ToolCall,
    ToolResult,
)

from .test_kernel_reactions import Channel


class Capability:
    def __init__(self, clock, *, block=False):
        self.clock, self.block = clock, block
        self.prepared = self.executed = 0

    def idempotency_key(self, call, inv):
        return "same"

    async def prepare(self, call, inv):
        self.prepared += 1
        return Plan(summary="Fictional echo", heavy=False, data={"call_id": call.call_id})

    async def execute(self, plan, inv, cancel):
        self.executed += 1
        if self.block:
            await cancel.wait()
            cancel.raise_if_cancelled()
        return ToolResult(call_id=plan.data["call_id"], status="ok", content="Fictional", data={})


class SendingChannel(Channel):
    def __init__(self, clock, receipts=("sent",)):
        super().__init__(clock)
        self.receipts = list(receipts)

    async def send(self, intent, cancel):
        self.text.append(intent)
        status = self.receipts.pop(0)
        return Receipt(
            intent_id=intent.intent_id,
            status=status,
            at=self.clock.now(),
            error={"code": "transient", "message": ""} if status == "failed" else None,
        )


def setup(tmp_path, *, clock=None, cid="companion_a", **kwargs):
    clock = clock or MockClock()
    ledger = Ledger(tmp_path / "ledger.sqlite3", cid, clock)
    channel = SendingChannel(clock)
    kernel = Kernel(
        ledger,
        clock,
        {"fake": channel},
        permissions=lambda a, t: True,
        quota=lambda a, t: True,
        **kwargs,
    )
    return kernel, channel, clock


async def new_record(kernel, *, role="owner", name="request", conversation="test_group"):
    actor = Actor(actor_id=role, role=role, channel_id="fake")
    conv = Conversation(
        conversation_id=conversation, kind="group", scene="public_group", channel_id="fake"
    )
    event = EventEnvelope(
        event_id="fake:" + name,
        type="conversation.message",
        companion_id=kernel.ledger.companion_id,
        channel_id="fake",
        conversation=conv,
        received_at=kernel.clock.now(),
        message=dict(
            sender=actor,
            sent_at=kernel.clock.now(),
            ref=dict(channel_id="fake", conversation_id=conversation, platform_message_id=name),
            parts=[dict(type="text", text="Fictional request")],
        ),
    )

    async def filtered(value):
        return value

    assert await kernel.receive(event, filter_event=filtered) == event.event_id
    inv = Invocation(
        companion_id=kernel.ledger.companion_id,
        actor=actor,
        conversation=conv,
        trace_id=uuid4().hex,
    )
    r = kernel.open_record(event_id=event.event_id, invocation=inv)
    return r, inv, event


def reply(r):
    return DeliveryIntent(
        intent_id=uuid4().hex,
        companion_id=r["companion_id"],
        conversation_id=r["conversation_id"],
        channel_id="fake",
        kind="reply",
        trace_id=r["trace_id"],
        task_id=r["task_id"],
        parts=[dict(type="text", text="Fictional result")],
    )


async def test_offline_execution_delivery_summary_and_idempotency(tmp_path):
    kernel, channel, clock = setup(tmp_path)
    r, inv, event = await new_record(kernel)
    capability = Capability(clock)
    call = ToolCall(call_id="call", tool="echo", arguments={"value": "fictional"})
    result = await kernel.execute(r["trace_id"], "echo-tool", call, capability, inv)
    assert result.status == "ok"
    duplicate = await kernel.execute(r["trace_id"], "echo-tool", call, capability, inv)
    assert duplicate["status"] == "ok" and capability.executed == capability.prepared == 1
    r = kernel.ledger.get("record", r["trace_id"])
    await kernel.seal(r["trace_id"], "ok", [reply(r)])
    assert "task.completed" not in [e.name for e in kernel.events]
    await kernel.delivery.deliver(r["trace_id"])
    await kernel.reactions.drain()
    await kernel.bus.drain()
    assert kernel.ledger.get("record", r["trace_id"])["delivery_status"] == "sent"
    assert [e.name for e in kernel.events].count("task.completed") == 1
    assert [i.emoji for i in channel.calls] == ["✅"]
    assert len(channel.text) == 1
    fields = next(e[2] for e in kernel.log.entries if e[1] == "record.completed")
    assert {
        "trace_id",
        "task_id",
        "queue_seconds",
        "execution_seconds",
        "delivery_seconds",
    } <= fields.keys()
    kernel.ledger.close()


async def test_ordinary_chat_reaction_requires_declared_exception(tmp_path):
    kernel, channel, clock = setup(tmp_path)
    r, _, event = await new_record(kernel)
    intent = reply(r).model_copy(
        update={"kind": "react", "parts": [], "emoji": "👌", "target": event.message.ref}
    )
    with pytest.raises(ValueError, match="ordinary chat"):
        await kernel.seal(r["trace_id"], "ok", [intent])
    assert not kernel.allow_reaction_exception(r["trace_id"], intent)
    assert kernel.allow_reaction_exception(r["trace_id"], intent, permissions=["message.react"])
    await kernel.seal(r["trace_id"], "ok", [intent])
    await kernel.delivery.deliver(r["trace_id"])
    await kernel.bus.drain()
    assert len(channel.calls) == 1
    assert not any(e.name.startswith("task.") for e in kernel.events)
    kernel.ledger.close()


@pytest.mark.parametrize("mode", ["reply_to", "task_id", "latest"])
async def test_three_cancel_target_modes_and_failed_reaction(tmp_path, mode):
    kernel, channel, clock = setup(tmp_path)
    r, inv, event = await new_record(kernel, role="member")
    work = asyncio.create_task(
        kernel.execute(
            r["trace_id"],
            "echo-tool",
            ToolCall(call_id="call", tool="echo", arguments={}),
            Capability(clock, block=True),
            inv,
        )
    )
    await clock.drain()
    task_id = kernel.ledger.get("record", r["trace_id"])["task_id"]
    kwargs = (
        {"reply_to": event.message.ref}
        if mode == "reply_to"
        else {"task_id": task_id}
        if mode == "task_id"
        else {}
    )
    outcome = await kernel.cancel("companion_a", inv.actor, "test_group", **kwargs)
    assert outcome == "cancelled"
    assert (await work).status == "cancelled"
    await kernel.reactions.drain()
    await kernel.bus.drain()
    assert channel.calls[-1].emoji == "❌" and channel.text == []
    kernel.ledger.close()


async def test_owner_latest_does_not_cancel_other_queued_chat_and_excludes_self(tmp_path):
    kernel, _, clock = setup(tmp_path)
    member, inv, event = await new_record(kernel, role="member")
    owner = Actor(actor_id="owner", role="owner", channel_id="fake")
    assert await kernel.cancel("companion_a", owner, "test_group") == "not_found"
    assert (
        await kernel.cancel("companion_a", owner, "test_group", reply_to=event.message.ref)
        == "cancelled"
    )
    own, own_inv, _ = await new_record(kernel, name="owner-request")
    with record_scope(own["trace_id"]):
        assert await kernel.cancel("companion_a", own_inv.actor, "test_group") == "not_found"
    assert await kernel.cancel("companion_a", own_inv.actor, "test_group") == "cancelled"
    await kernel.bus.drain()
    kernel.ledger.close()


async def test_deadline_uses_first_tool_and_produces_cancelled_deadline(tmp_path):
    kernel, channel, clock = setup(tmp_path, max_task_seconds=10)
    r, inv, _ = await new_record(kernel)
    work = asyncio.create_task(
        kernel.execute(
            r["trace_id"],
            "echo-tool",
            ToolCall(call_id="call", tool="echo", arguments={}),
            Capability(clock, block=True),
            inv,
            timeout_s=100,
        )
    )
    await clock.advance_and_drain(10)
    result = await work
    assert result.status == "cancelled" and result.error.code == "deadline"
    await kernel.seal(r["trace_id"], "cancelled", error=dict(code="deadline", message=""))
    await kernel.reactions.drain()
    await kernel.bus.drain()
    assert any(entry[1] == "task.timeout_capped" for entry in kernel.log.entries)
    kernel.ledger.close()


async def test_execute_success_delivery_pending_can_be_cancelled(tmp_path):
    kernel, channel, clock = setup(tmp_path)
    r, inv, _ = await new_record(kernel)
    await kernel.execute(
        r["trace_id"],
        "echo-tool",
        ToolCall(call_id="call", tool="echo", arguments={}),
        Capability(clock),
        inv,
    )
    r = kernel.ledger.get("record", r["trace_id"])
    await kernel.seal(r["trace_id"], "ok", [reply(r)])
    assert (
        await kernel.cancel("companion_a", inv.actor, "test_group", task_id=r["task_id"])
        == "cancelled"
    )
    assert kernel.ledger.get("record", r["trace_id"])["exec_status"] == "ok"
    assert kernel.ledger.intents(r["trace_id"])[0]["error"]["code"] == "cancelled_before_send"
    await kernel.reactions.drain()
    await kernel.bus.drain()
    assert channel.text == []
    kernel.ledger.close()


async def test_cancel_denied_finished_and_invalid_requests(tmp_path):
    kernel, _, clock = setup(tmp_path)
    r, inv, event = await new_record(kernel)
    member = Actor(actor_id="member", role="member", channel_id="fake")
    before = kernel.ledger.get("record", r["trace_id"])
    assert (
        await kernel.cancel("companion_a", member, "test_group", reply_to=event.message.ref)
        == "denied"
    )
    assert kernel.ledger.get("record", r["trace_id"]) == before
    assert kernel.ledger.rows("audit")[-1]["allowed"] is False
    assert await kernel.cancel("companion_b", inv.actor, "test_group") == "invalid_request"
    assert await kernel.cancel("companion_a", None, "test_group") == "invalid_request"
    assert await kernel.cancel("companion_a", inv.actor, "") == "invalid_request"
    await kernel.seal(r["trace_id"], "ok")
    assert (
        await kernel.cancel("companion_a", inv.actor, "test_group", reply_to=event.message.ref)
        == "already_finished"
    )
    await kernel.bus.drain()
    kernel.ledger.close()
