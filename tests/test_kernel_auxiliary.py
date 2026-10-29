"""权限、观察、防重、队列公平性、内容保留和任务单。"""

import asyncio

from clytie.kernel.queue import ConversationQueue
from clytie.kernel.service import Kernel
from clytie.kernel.task_card import render_task_card
from clytie.sdk.types import Actor, ToolCall

from .test_kernel_reactions import fixture
from .test_kernel_service import Capability, new_record, setup


async def test_queue_serializes_conversation_and_promotes_waiting_member(tmp_path):
    kernel, _, clock = setup(tmp_path)
    queue = ConversationQueue(clock, maximum=1)
    owner = Actor(actor_id="owner", role="owner", channel_id="fake")
    member = Actor(actor_id="member", role="member", channel_id="fake")
    release, calls = asyncio.Event(), []

    async def busy():
        await release.wait()

    def action(name):
        async def run():
            calls.append(name)

        return run

    queue.submit("busy", owner, busy)
    queue.submit("same", member, action("member"))
    clock.advance(61)
    queue.submit("same", owner, action("owner"))
    release.set()
    await queue.drain()
    assert calls == ["member", "owner"]
    kernel.ledger.close()


async def test_queue_maximum_and_same_conversation_never_overlap(tmp_path):
    kernel, _, clock = setup(tmp_path)
    queue = ConversationQueue(clock, maximum=2)
    actor = Actor(actor_id="owner", role="owner", channel_id="fake")
    active, observed = set(), []
    release = asyncio.Event()

    def action(cid):
        async def run():
            assert cid not in active
            active.add(cid)
            observed.append(len(active))
            await release.wait()
            active.remove(cid)

        return run

    for cid in ("one", "one", "two", "three"):
        queue.submit(cid, actor, action(cid))
    await clock.drain()
    assert active == {"one", "two"}
    release.set()
    await queue.drain()
    assert max(observed) == 2
    kernel.ledger.close()


async def test_default_permission_denies_before_prepare_and_quota_denial(tmp_path):
    kernel, channel, clock = setup(tmp_path)
    r, inv, _ = await new_record(kernel)
    denied = Kernel(kernel.ledger, clock, {"fake": channel})
    tool = Capability(clock)
    call = ToolCall(call_id="call", tool="echo", arguments={})
    assert (
        await denied.execute(r["trace_id"], "echo-tool", call, tool, inv)
    ).reason_code == "permission"
    denied.permissions = lambda actor, name: True
    assert (
        await denied.execute(r["trace_id"], "echo-tool", call, tool, inv)
    ).reason_code == "quota"
    assert tool.prepared == 0 and kernel.ledger.rows("invocation") == []
    await kernel.bus.drain()
    kernel.ledger.close()


async def test_nondedupable_only_observed_after_filtering(tmp_path):
    kernel, channel, clock = setup(tmp_path)
    _, _, event = await new_record(kernel)
    count, before = [], len(kernel.ledger.rows("inbound"))
    event = event.model_copy(update={"dedupable": False})

    async def observe(value):
        count.append(value)

    async def filtered(value):
        return value

    kernel.observe = observe
    events = len(kernel.events)
    assert await kernel.receive(event, filter_event=filtered) is None
    assert await kernel.receive(event, filter_event=filtered) is None
    assert count == [event, event]
    assert len(kernel.ledger.rows("inbound")) == before and len(kernel.events) == events
    assert channel.calls == channel.text == []
    await kernel.bus.drain()
    kernel.ledger.close()


def test_content_retention_preserves_dedupe_and_newer_late_content(tmp_path):
    ledger, _, _, clock, trace = fixture(tmp_path)
    with ledger.transaction():
        ledger.audit("late_result", trace_id=trace, content={"result": "old fictional result"})
    clock.advance(24 * 60 * 60)
    with ledger.transaction():
        ledger.audit("late_result", trace_id=trace, content={"result": "new fictional result"})
    ledger.expire_content(1)
    assert ledger.get("inbound", "fake:example")["message"]["parts"] is None
    assert ledger.rows("invocation")[0]["args"] is None
    audits = [a for a in ledger.rows("audit") if a["kind"] == "late_result"]
    assert audits[0]["content"] is None and audits[1]["content"]["result"] == "new fictional result"
    assert len(ledger.rows("dedupe")) == 1
    assert ledger.get("record", trace)["task_id"]
    ledger.close()


def test_task_card_names_helpers_defaults_and_rounding():
    config = {"names": {"internal": "companion_a", "external": {"public_group": "example"}}}
    assert "example" in render_task_card(config, "abcdef12", 1.5, scene="public_group")
    assert "2 秒" in render_task_card(config, "abcdef12", 1.5, scene="public_group")
    assert "协助" not in render_task_card(config, "abcdef12", 1, scene="public_group")
    helper = {"names": {"internal": "companion_b"}}
    assert "companion_b" in render_task_card(
        config, "abcdef12", 65, scene="owner_private", helper=helper
    )
    assert "分钟" in render_task_card(config, "abcdef12", 65, scene="owner_private")


async def test_cancelled_queued_record_is_never_executed(tmp_path):
    kernel, channel, clock = setup(tmp_path, config={"tasks": {"max_concurrent": 1}})
    r, inv, event = await new_record(kernel)
    other, _, _ = await new_record(kernel, name="other")
    gate, ran = asyncio.Event(), []
    first = kernel.submit(r["trace_id"], inv.actor, gate.wait)

    async def later():
        ran.append(True)

    second = kernel.submit(other["trace_id"], inv.actor, later)
    await clock.drain()
    assert await kernel.cancel("companion_a", inv.actor, "test_group") == "cancelled"
    gate.set()
    await first
    await kernel.queue.drain()
    assert second.cancelled() and ran == []
    await kernel.bus.drain()
    kernel.ledger.close()
