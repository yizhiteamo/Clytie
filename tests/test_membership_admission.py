import asyncio
from types import SimpleNamespace

import pytest

from clytie.admission import Admission
from clytie.sdk.testing import FakeChannel, FakeContext, MockClock


def setup(value=True, supported=True):
    clock = MockClock()
    channel = FakeChannel(
        FakeContext(clock=clock), membership={("other", "owner"): value} if supported else None
    )
    admission = Admission(
        {"boundary": {"admission": "owner_present"}},
        {
            "people": {"owner": {"role": "owner", "accounts": []}},
            "conversations": {"registered": {"kind": "group"}},
        },
        channels={"fake": channel},
        clock=clock,
    )
    conversation = SimpleNamespace(kind="group", channel_id="fake", conversation_id="other")
    actor = SimpleNamespace(role="stranger", actor_id="stranger")
    return admission, channel, clock, conversation, actor


@pytest.mark.parametrize(
    "value,expected", [(True, None), (False, "owner_absent"), (None, "membership_unknown")]
)
async def test_membership_result_and_expiring_cache(value, expected):
    admission, channel, clock, conversation, actor = setup(value)
    assert await admission.checked_reason(conversation, actor) == expected
    assert await admission.checked_reason(conversation, actor) == expected
    assert len(channel.membership_queries) == 1
    clock.advance(600)
    assert await admission.checked_reason(conversation, actor) == expected
    assert len(channel.membership_queries) == 2
    admission.invalidate("fake", "other")
    assert await admission.checked_reason(conversation, actor) == expected
    assert len(channel.membership_queries) == 3


async def test_unsupported_channel_only_admits_registered_groups():
    admission, channel, _, conversation, actor = setup(supported=False)
    assert await admission.checked_reason(conversation, actor) == "membership_unknown"
    conversation.conversation_id = "registered"
    assert await admission.checked_reason(conversation, actor) is None
    assert not channel.membership_queries


async def test_query_timeout_and_exception_fail_closed():
    admission, channel, clock, conversation, actor = setup()

    async def hang(*args):
        await clock.sleep(60)

    channel.is_member = hang
    task = asyncio.create_task(admission.checked_reason(conversation, actor))
    await clock.advance_and_drain(0)
    await clock.advance_and_drain(30)
    assert await task == "membership_unknown"
    admission.invalidate("fake", "other")

    async def fail(*args):
        raise RuntimeError("fictional")

    channel.is_member = fail
    assert await admission.checked_reason(conversation, actor) == "membership_unknown"


async def test_notification_during_query_prevents_stale_admission():
    admission, channel, _, conversation, actor = setup()
    entered, release = asyncio.Event(), asyncio.Event()

    async def query(*args):
        entered.set()
        await release.wait()
        return True

    channel.is_member = query
    task = asyncio.create_task(admission.checked_reason(conversation, actor))
    await entered.wait()
    admission.invalidate("fake", "other")
    release.set()
    assert await task == "membership_unknown"
    assert not admission.membership


@pytest.mark.parametrize("kind", ["member_join", "member_leave"])
async def test_membership_notice_invalidates_before_admission_without_model_or_ledger(
    tmp_path, kind
):
    from clytie.sdk.types import EventEnvelope

    from .test_agent import finish, make_agent

    agent, _, model = await make_agent(tmp_path)
    admission, channel, clock, conversation, actor = setup()
    agent.admission = admission
    try:
        assert await admission.checked_reason(conversation, actor) is None
        before = agent.ledger.rows("inbound")
        notice = EventEnvelope(
            event_id="fake:notice",
            type="conversation.notice",
            dedupable=False,
            companion_id=agent.cid,
            channel_id="fake",
            conversation={
                "kind": "group",
                "channel_id": "fake",
                "conversation_id": "other",
                "scene": "public_group",
            },
            notice={"kind": kind},
            received_at=clock.now(),
        )
        assert await agent.receive(notice) is None
        assert not admission.membership
        assert agent.ledger.rows("inbound") == before and not model.requests
    finally:
        await finish(agent)


async def test_outbound_rechecks_owner_after_invalidation():
    admission, channel, _, conversation, actor = setup()
    intent = SimpleNamespace(channel_id="fake", conversation_id="other")
    assert not await admission.allows_delivery_checked(intent, None)
    assert await admission.checked_reason(conversation, actor) is None
    assert await admission.allows_delivery_checked(intent, None)
    admission.invalidate("fake", "other")
    channel.membership[("other", "owner")] = False
    assert not await admission.allows_delivery_checked(intent, None)
    assert not await admission.allows_delivery_checked(
        intent,
        {
            "conversation": {"conversation_id": "other", "kind": "group"},
            "message": {"sender": {"role": "member", "actor_id": "member"}},
        },
    )


async def test_delivery_deadline_before_membership_result_never_calls_platform(tmp_path):
    from uuid import uuid4

    from clytie.sdk.types import DeliveryIntent

    from .test_agent import event, finish, make_agent

    agent, channel, _ = await make_agent(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()

    class PendingAdmission:
        async def allows_delivery_checked(self, intent, inbound):
            started.set()
            await release.wait()
            return True

    agent.kernel.delivery.admission = PendingAdmission()
    agent.kernel.delivery.window = 1
    try:
        incoming = event(agent)
        key, _ = agent.ledger.claim_inbound(incoming)
        record = agent.ledger.open_record(event_id=key)
        intent = DeliveryIntent(
            intent_id=uuid4().hex,
            trace_id=record["trace_id"],
            companion_id=agent.cid,
            conversation_id=incoming.conversation.conversation_id,
            channel_id="fake",
            kind="reply",
            parts=[{"type": "text", "text": "fictional"}],
        )
        agent.ledger.register_intents(record["trace_id"], [intent])
        task = asyncio.create_task(agent.kernel.delivery.send_one(intent.intent_id))
        await started.wait()
        await agent.clock.advance_and_drain(1)
        await agent.clock.advance_and_drain(agent.kernel.delivery.grace)
        # The deadline is already accepted; a late membership success cannot call send.
        release.set()
        await agent.clock.advance_and_drain(0)
        assert await task == "failed"
        assert not channel.sent
        assert agent.ledger.get("intent", intent.intent_id)["error"]["code"] == "not_admitted"
    finally:
        release.set()
        await finish(agent)
