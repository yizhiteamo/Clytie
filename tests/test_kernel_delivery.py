"""有限重发、拆分取消、超时 unknown 和迟到收据。"""

import asyncio
from datetime import timedelta

from clytie.sdk.types import DeliveryIntent, Receipt

from .test_kernel_service import Capability, ToolCall, new_record, reply, setup


async def task_with_intents(kernel, *, split=False, spaced=False):
    r, inv, event = await new_record(kernel)
    await kernel.execute(
        r["trace_id"],
        "echo-tool",
        ToolCall(call_id="call", tool="echo", arguments={}),
        Capability(kernel.clock),
        inv,
    )
    r = kernel.ledger.get("record", r["trace_id"])
    intents = [reply(r) for _ in range(3 if split else 1)]
    if split:
        intents = [
            i.model_copy(
                update={
                    "split": dict(group_id="example", index=n, total=3),
                    "not_before": kernel.clock.now() + timedelta(seconds=n * 3) if spaced else None,
                }
            )
            for n, i in enumerate(intents)
        ]
    await kernel.seal(r["trace_id"], "ok", intents)
    return r, inv, intents


async def settle(kernel):
    await kernel.clock.advance_and_drain(kernel.reactions.interval)
    await kernel.reactions.drain()
    await kernel.bus.drain()


async def test_transient_failures_retry_using_clock_and_stop_after_limit(tmp_path):
    kernel, channel, clock = setup(tmp_path)
    channel.receipts = ["failed", "failed", "sent"]
    r, _, intents = await task_with_intents(kernel)
    task = asyncio.create_task(kernel.delivery.deliver(r["trace_id"]))
    await clock.drain()
    assert len(channel.text) == 1
    await clock.advance_and_drain(2)
    assert len(channel.text) == 2
    await clock.advance_and_drain(5)
    await task
    assert len(channel.text) == 3
    assert kernel.ledger.get("record", r["trace_id"])["delivery_status"] == "sent"
    await settle(kernel)
    kernel.ledger.close()


async def test_unknown_stops_split_without_resend(tmp_path):
    kernel, channel, clock = setup(tmp_path)
    channel.receipts = ["sent", "unknown"]
    r, _, intents = await task_with_intents(kernel, split=True)
    await kernel.delivery.deliver(r["trace_id"])
    assert len(channel.text) == 2
    assert (
        kernel.ledger.get("intent", intents[2].intent_id)["error"]["code"]
        == "skipped_after_unknown"
    )
    await kernel.delivery.deliver(r["trace_id"])
    assert len(channel.text) == 2
    await settle(kernel)
    kernel.ledger.close()


async def test_started_group_finishes_after_cancel_and_deadline(tmp_path):
    kernel, channel, clock = setup(tmp_path, max_task_seconds=1)
    channel.receipts = ["sent"] * 3
    r, inv, intents = await task_with_intents(kernel, split=True, spaced=True)
    task = asyncio.create_task(kernel.delivery.deliver(r["trace_id"]))
    await clock.drain()
    assert len(channel.text) == 1
    assert (
        await kernel.cancel("companion_a", inv.actor, r["conversation_id"], task_id=r["task_id"])
        == "cancelled"
    )
    await clock.advance_and_drain(6)
    await task
    assert len(channel.text) == 3
    assert all(kernel.ledger.get("intent", i.intent_id)["status"] == "sent" for i in intents)
    await settle(kernel)
    assert channel.calls[-1].emoji == "✅"
    kernel.ledger.close()


async def test_hanging_send_becomes_unknown_then_late_receipt_only_updates_audit(tmp_path):
    kernel, channel, clock = setup(tmp_path)
    r, _, intents = await task_with_intents(kernel)
    release = asyncio.Event()
    tokens = []

    async def send(intent, cancel):
        tokens.append(cancel)
        await release.wait()
        return Receipt(intent_id=intent.intent_id, status="sent", at=clock.now())

    channel.send = send
    task = asyncio.create_task(kernel.delivery.deliver(r["trace_id"]))
    await clock.advance_and_drain(60)
    assert tokens[0].cancelled
    await clock.advance_and_drain(5)
    await task
    await settle(kernel)
    assert kernel.ledger.get("intent", intents[0].intent_id)["status"] == "unknown"
    event_count = len(kernel.events)
    release.set()
    await clock.drain()
    assert kernel.ledger.get("intent", intents[0].intent_id)["status"] == "sent"
    assert len(kernel.events) == event_count and len(tokens) == 1
    kernel.ledger.close()


async def test_jump_beyond_retry_window_does_not_send_again(tmp_path):
    kernel, channel, clock = setup(tmp_path)
    channel.receipts = ["failed"]
    r, _, intents = await task_with_intents(kernel)
    task = asyncio.create_task(kernel.delivery.deliver(r["trace_id"]))
    await clock.drain()
    clock.advance(61)
    await clock.drain()
    await task
    assert len(channel.text) == 1
    assert kernel.ledger.get("intent", intents[0].intent_id)["error"]["code"] == "transient"
    await settle(kernel)
    kernel.ledger.close()


async def test_asset_rejected_before_attempt_and_mismatched_receipt_warns(tmp_path):
    kernel, channel, clock = setup(tmp_path)
    r, _, _ = await new_record(kernel)
    intent = DeliveryIntent.model_validate(
        {
            **reply(r).model_dump(),
            "parts": [
                {
                    "type": "image",
                    "asset": {
                        "asset_id": "a" * 64,
                        "media_type": "image/png",
                        "size": 10,
                        "origin": "test",
                    },
                }
            ],
        }
    )
    await kernel.seal(r["trace_id"], "ok", [intent])
    await kernel.delivery.deliver(r["trace_id"])
    assert kernel.ledger.get("intent", intent.intent_id)["error"]["code"] == "asset_rejected"
    assert not channel.text and not kernel.ledger.rows("attempt")
    other, _, _ = await new_record(kernel, name="other")
    outgoing = reply(other)

    async def mismatch(intent, cancel):
        return Receipt(intent_id="wrong-intent", status="sent", at=clock.now())

    channel.send = mismatch
    await kernel.seal(other["trace_id"], "ok", [outgoing])
    await kernel.delivery.deliver(other["trace_id"])
    assert kernel.ledger.get("intent", outgoing.intent_id)["status"] == "unknown"
    assert any(e[1] == "delivery.receipt_mismatch" for e in kernel.log.entries)
    await settle(kernel)
    kernel.ledger.close()


async def test_receipt_message_retention_clears_summary_copy_too(tmp_path):
    kernel, channel, clock = setup(tmp_path)
    r, _, intents = await task_with_intents(kernel)

    async def fail(intent, cancel):
        return Receipt(
            intent_id=intent.intent_id,
            status="failed",
            at=clock.now(),
            error={"code": "permanent", "message": "fictional detail"},
        )

    channel.send = fail
    await kernel.delivery.deliver(r["trace_id"])
    await settle(kernel)
    clock.advance(24 * 60 * 60)
    kernel.ledger.expire_content(1)
    row = kernel.ledger.get("intent", intents[0].intent_id)
    assert row["error"] == {"code": "permanent", "message": None}
    attempt = next(
        a for a in kernel.ledger.rows("attempt") if a["intent_id"] == intents[0].intent_id
    )
    assert attempt["receipt"]["error"]["message"] is None
    kernel.ledger.close()


async def test_cancel_during_retry_wait_preserves_last_error(tmp_path):
    kernel, channel, clock = setup(tmp_path)
    channel.receipts = ["failed"]
    r, inv, intents = await task_with_intents(kernel)
    work = asyncio.create_task(kernel.delivery.deliver(r["trace_id"]))
    await clock.drain()
    assert kernel.ledger.get("intent", intents[0].intent_id)["status"] == "retry_wait"
    assert (
        await kernel.cancel("companion_a", inv.actor, r["conversation_id"], task_id=r["task_id"])
        == "cancelled"
    )
    await clock.advance_and_drain(2)
    await work
    row = kernel.ledger.get("intent", intents[0].intent_id)
    assert row["status"] == "failed" and row["error"]["code"] == "transient"
    assert len(channel.text) == 1
    await settle(kernel)
    kernel.ledger.close()
