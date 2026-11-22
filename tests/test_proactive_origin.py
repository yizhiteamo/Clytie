"""主动任务在能力插件、事件和账本中均没有请求人。"""

import json

import pytest

from clytie.kernel.query import task_view
from clytie.sdk.behavior import valid_after_generation
from clytie.sdk.types import Actor, Conversation, Invocation, ReplyDraft, ToolCall

from .test_kernel_service import Capability, new_record, reply, setup


@pytest.mark.parametrize("status,event_name", [("ok", "task.completed"), ("error", "task.failed")])
async def test_proactive_origin_across_capability_events_ledger_and_query(
    tmp_path, status, event_name
):
    kernel, channel, _ = setup(tmp_path)
    permission_actor = Actor(actor_id="fictional_permission_only", role="owner", channel_id="fake")
    permission_checks, quota_checks, seen, summaries = [], [], [], []

    def permissions(actor, tool):
        permission_checks.append(actor)
        return actor == permission_actor and tool == "echo"

    def quota(actor, tool):
        quota_checks.append(actor)
        return True

    async def completed(summary):
        summaries.append(summary)

    class Observed(Capability):
        def idempotency_key(self, call, inv):
            seen.append(inv)
            return super().idempotency_key(call, inv)

        async def prepare(self, call, inv):
            seen.append(inv)
            return await super().prepare(call, inv)

        async def execute(self, plan, inv, cancel):
            seen.append(inv)
            return await super().execute(plan, inv, cancel)

    kernel.permissions, kernel.quota = permissions, quota
    kernel.task_completed = completed
    conversation = Conversation(
        conversation_id="test_private", kind="private", scene="owner_private", channel_id="fake"
    )
    try:
        record = await kernel.open_record(conversation=conversation, proactive=True)
        trace = record["trace_id"]
        invocation = Invocation(
            companion_id=kernel.ledger.companion_id,
            actor=None,
            origin="proactive",
            conversation=conversation,
            trace_id=trace,
        )
        call = ToolCall(call_id="fictional", tool="echo", arguments={})
        result = await kernel.execute(
            trace,
            "echo-tool",
            call,
            Observed(kernel.clock),
            invocation,
            permission_actor=permission_actor,
        )
        assert result.status == "ok"
        assert permission_checks == [permission_actor] and quota_checks == [None]
        assert len(seen) == 3 and all(i.actor is None and i.origin == "proactive" for i in seen)
        await kernel.seal(trace, status)
        events = [e for e in kernel.events if e.name.startswith("task.")]
        assert [e.name for e in events] == ["task.started", event_name]
        assert all(
            e.payload["actor_id"] is None and e.payload["origin"] == "proactive" for e in events
        )
        assert (
            len(summaries) == 1
            and summaries[0].actor_id is None
            and summaries[0].origin == "proactive"
        )
        record = kernel.ledger.get("record", trace)
        assert record["requester"] is None and record["task_id"] == seen[-1].task_id
        view = task_view(kernel.ledger, record, full=True)
        assert view["requester"] is None and view["origin"] == "proactive"
        assert [row["origin"] for row in kernel.ledger.rows("invocation")] == ["proactive"]
        audits = kernel.ledger.rows("audit")
        assert any(a.get("origin") == "proactive" and a["trace_id"] == trace for a in audits)
        assert permission_actor.actor_id not in json.dumps(
            [
                record,
                view,
                audits,
                kernel.ledger.rows("invocation"),
                [e.model_dump(mode="json") for e in events],
            ]
        )
        assert not channel.calls
    finally:
        await kernel.bus.drain()
        kernel.ledger.close()


async def test_message_task_uses_mapped_requester_even_with_permission_actor(tmp_path):
    kernel, _, _ = setup(tmp_path)
    kernel.reactions.enabled = False
    try:
        record, inv, _ = await new_record(kernel, role="member")
        trace = record["trace_id"]
        call = ToolCall(call_id="fictional", tool="echo", arguments={})
        owner = Actor(actor_id="owner", role="owner", channel_id="fake")
        kernel.permissions = lambda actor, _: actor == owner
        capability = Capability(kernel.clock)
        denied = await kernel.execute(
            trace, "echo-tool", call, capability, inv, permission_actor=owner
        )
        assert denied.reason_code == "permission" and not capability.prepared
        kernel.permissions = lambda actor, _: actor == inv.actor
        assert (await kernel.execute(trace, "echo-tool", call, capability, inv)).status == "ok"
        await kernel.seal(trace, "ok")
        events = [e for e in kernel.events if e.name.startswith("task.")]
        assert [e.name for e in events] == ["task.started", "task.completed"]
        assert all(
            e.payload["actor_id"] == "member" and e.payload["origin"] == "message" for e in events
        )
        assert task_view(kernel.ledger, kernel.ledger.get("record", trace))["origin"] == "message"
    finally:
        await kernel.reactions.drain()
        await kernel.bus.drain()
        kernel.ledger.close()


def test_reply_draft_kind_defaults_to_reply_and_is_protected_for_both_kinds():
    conversation = Conversation(
        conversation_id="test_private", kind="private", scene="owner_private", channel_id="fake"
    )
    draft = ReplyDraft(conversation=conversation, trace_id="a" * 32, form="plain")
    assert draft.kind == "reply"
    proactive = draft.model_copy(update={"kind": "proactive"})
    assert valid_after_generation(proactive, proactive)
    assert not valid_after_generation(draft, proactive)
    assert not valid_after_generation(proactive, draft)


@pytest.mark.parametrize(
    "role,kind", [(None, "private"), ("member", "private"), ("owner", "group")]
)
async def test_proactive_tool_requires_owner_permission_in_private_chat(tmp_path, role, kind):
    kernel, _, _ = setup(tmp_path)
    conversation = Conversation(
        conversation_id="test_conversation",
        kind=kind,
        scene="owner_private" if kind == "private" else "public_group",
        channel_id="fake",
    )
    try:
        record = await kernel.open_record(conversation=conversation, proactive=True)
        invocation = Invocation(
            companion_id=kernel.ledger.companion_id,
            actor=None,
            origin="proactive",
            conversation=conversation,
            trace_id=record["trace_id"],
        )
        principal = Actor(actor_id=role, role=role, channel_id="fake") if role else None
        capability = Capability(kernel.clock)
        call = ToolCall(call_id="fictional", tool="echo", arguments={})
        denied = await kernel.execute(
            record["trace_id"],
            "echo-tool",
            call,
            capability,
            invocation,
            permission_actor=principal,
        )
        assert denied.reason_code == "permission" and not capability.prepared
        assert not kernel.ledger.rows("invocation")
        reaction = reply(record).model_copy(update={"kind": "react", "parts": [], "emoji": "👌"})
        assert not await kernel.allow_reaction_exception(
            record["trace_id"], reaction, owner_ignore=True
        )
    finally:
        await kernel.bus.drain()
        kernel.ledger.close()
