"""补收的离线时序，通过生产入站、账本、上下文与虚构模型核对。"""

import json
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from clytie.agent.policy import text_parts
from clytie.sdk.types import ChannelState, ImagePart
from clytie.supervisor.catch_up import PlatformCatchUp
from clytie.supervisor.companion import Companion

from .test_agent import event, finish, make_agent


class Harness:
    def __init__(self, companion, channel, model):
        self.companion, self.agent = companion, companion.agent
        self.channel, self.model = channel, model
        self.clock = self.agent.clock
        self.status = "offline"
        self.supported = True
        self.calls, self.history = [], {}
        self.failure = None
        self.block = False
        self.tokens = []
        features = channel.features()
        channel.features = lambda: features.model_copy(update={"supports_history": self.supported})
        channel.state = lambda: ChannelState(status=self.status)
        channel.history = self.read
        self.reset()

    def reset(self):
        self.recovery = PlatformCatchUp(self.companion, "fake", "fake-channel", self.channel)
        self.companion.catch_up = {"fake": self.recovery}

    def switch(self, status):
        self.status = status
        self.recovery.observe()

    async def read(self, cid, *, after, limit, cancel):
        self.calls.append((cid, after, limit, self.clock.monotonic()))
        self.tokens.append(cancel)
        if self.block:
            await cancel.wait()
            cancel.raise_if_cancelled()
        if self.failure:
            raise self.failure
        return [
            e.model_copy(update={"received_at": self.clock.now()})
            for e in self.history.get(cid, [])
            if e.message.sent_at > after
        ][-limit:]

    def seed(self, **options):
        seen = event(self.agent, **options)
        self.agent.ledger.claim_inbound(seen)
        self.agent.ledger.ignore(seen.event_id, "shutting_down")
        return seen

    def audit(self, kind):
        return [
            json.loads(row[0])
            for row in self.agent.ledger.db.execute(
                "SELECT data FROM audit WHERE json_extract(data,'$.kind')=?", (kind,)
            )
        ]

    async def tick(self, seconds):
        await self.clock.advance_and_drain(seconds)

    async def stop(self):
        await self.recovery.stop()
        await finish(self.agent)


@pytest.fixture
async def h(tmp_path):
    agent, channel, model = await make_agent(
        tmp_path, changes={"memory": {"recollect": {"enabled": False}}}
    )
    companion = Companion(tmp_path, agent.cid, agent.clock)
    companion.agent = agent
    fixture = Harness(companion, channel, model)
    try:
        yield fixture
    finally:
        await fixture.stop()


async def test_reconnect_anchor_live_dedupe_one_owner_reply_and_restart(h):
    seen = h.seed()
    before = h.clock.now()
    h.clock.local_tz = ZoneInfo("Asia/Shanghai")
    await h.tick(3600)
    old = event(h.agent, "fictional missed")
    await h.tick(3600)
    h.switch("online")
    await h.tick(1)
    live = event(h.agent, "fictional live")
    await h.companion._receive_event(live)
    assert h.agent.ledger.get("inbound", live.event_id)["state"] == "received"
    assert not h.model.requests
    h.history["owner_private"] = [seen, old, live]
    await h.tick(10)
    await h.agent.drain()
    assert h.calls[0][1] == before - timedelta(minutes=5)
    assert len(h.model.requests) == len(h.channel.sent) == 1
    context = "\n".join(text_parts(m.parts) for m in h.model.requests[0].messages)
    assert all(
        phrase in context
        for phrase in (
            "不要用固定的说法",
            "不要编造你当时在做什么",
            "不要逐条回复",
            "09:00:00",
            "fictional missed",
            "fictional live",
        )
    )
    assert h.audit("catch_up.done")[0]["unseen"] == 1
    assert h.agent.ledger.get("inbound", seen.event_id)["reason"] == "shutting_down"
    await h.tick(61)
    for restart in (False, True):
        h.switch("offline")
        await h.tick(2)
        if restart:
            await h.recovery.stop()
            h.reset()
        h.switch("online")
        await h.tick(11)
        await h.agent.drain()
        assert len(h.channel.sent) == 1


async def test_first_conversation_and_flicker_release_live_without_reading(h):
    h.switch("online")
    await h.tick(1)
    await h.companion._receive_event(event(h.agent))
    assert not h.model.requests
    h.switch("offline")
    await h.tick(0)
    await h.agent.drain()
    assert len(h.channel.sent) == 1
    assert not h.calls
    await h.tick(1)
    h.switch("online")
    await h.tick(11)
    assert [cid for cid, *_ in h.calls] == ["owner_private"]
    h.switch("offline")
    h.switch("online")
    await h.tick(11)
    assert len(h.calls) == 1


async def test_unsupported_never_waits_or_reads(h):
    h.supported = False
    h.switch("online")
    await h.companion._receive_event(event(h.agent))
    await h.agent.drain()
    assert len(h.channel.sent) == 1
    assert not h.calls


async def test_private_history_containing_only_live_duplicates_replies_once(h):
    h.seed()
    h.clock.advance(1)
    h.switch("online")
    first, second = event(h.agent), event(h.agent)
    for msg in [first, second]:
        await h.companion._receive_event(msg)
    h.history["owner_private"] = [first, second]
    await h.tick(12)
    await h.recovery.cycle
    await h.agent.drain()
    assert h.audit("catch_up.done")[0]["unseen"] == 0
    assert len(h.channel.sent) == 1


async def test_no_anchor_and_clamped_lookback(h):
    h.seed()
    h.clock.advance(48 * 3600)
    h.switch("online")
    await h.tick(11)
    assert len(h.calls) == 1
    cid, after, limit, _ = h.calls[0]
    assert (cid, limit) == ("owner_private", 50)
    assert after == h.clock.now() - timedelta(hours=24, seconds=1)


async def test_owner_limit_batch_and_historical_cancel(h, monkeypatch):
    h.seed()
    await h.tick(1)
    history = [event(h.agent, f"fictional {i}") for i in range(54)]
    history.append(event(h.agent, "停"))
    h.history["owner_private"] = history

    async def unexpected_cancel(*args):
        pytest.fail("historical cancellation")

    monkeypatch.setattr(h.agent, "cancel", unexpected_cancel)
    h.switch("online")
    await h.tick(11)
    await h.clock.drain(6000)
    await h.agent.drain()
    assert len(h.model.requests) == len(h.channel.sent) == 1
    assert "更早还有消息没看到" in str(h.model.requests[0].messages)
    assert len(h.agent.ledger.rows("inbound")) == 51
    assert h.audit("catch_up.done")[0]["responding"] == 50


async def test_group_history_only_no_image_fetch_or_model(h):
    h.seed(scene="public_group")
    await h.tick(1)
    group = [event(h.agent, scene="public_group", role="member") for _ in range(205)]
    group[-1] = group[-1].model_copy(
        update={
            "message": group[-1].message.model_copy(
                update={"parts": [ImagePart(raw_ref="fictional")]}
            )
        }
    )
    h.history["test_group"] = group
    h.switch("online")
    await h.tick(12)
    await h.recovery.cycle
    rows = h.agent.ledger.rows("inbound")
    assert len(rows) == 201
    assert all(row["reason"] == "catch_up" for row in rows[1:])
    assert (
        not h.model.requests and not h.agent.media_jobs and not h.agent.ledger.rows("inbound_media")
    )
    audit = h.audit("catch_up.done")[0]
    assert audit["recorded"] == 200 and audit["limit_reached"]
    assert "fictional" not in str(audit)


async def test_group_selects_three_speakers_ten_messages_and_orders_live(h):
    h.seed(scene="public_group")
    await h.tick(1)
    group = []
    for i in range(16):
        group.append(
            event(
                h.agent,
                f"fictional {i}",
                scene="public_group",
                role="member",
                actor=f"member_{i % 4}",
                mention=True,
            )
        )
    h.history["test_group"] = group
    h.switch("online")
    newcomer = event(h.agent, "fictional later", scene="public_group", mention=True)
    await h.companion._receive_event(newcomer)
    await h.tick(12)
    await h.recovery.cycle
    await h.agent.drain()
    assert h.audit("catch_up.done")[0]["responding"] == 10
    records = h.agent.ledger.rows("record")
    assert len(records) == 4
    assert records[-1]["source_event_id"] == newcomer.event_id


async def test_timeout_releases_live_and_stop_cancels_read(h):
    h.seed()
    await h.tick(1)
    h.block = True
    h.switch("online")
    await h.tick(9)
    await h.companion._receive_event(event(h.agent))
    await h.tick(30)
    assert len(h.model.requests) == 0
    await h.tick(1)
    await h.agent.drain()
    assert len(h.channel.sent) == 1
    assert h.tokens[0].cancelled
    assert h.audit("catch_up.failed")[0]["code"] == "timeout"
    h.switch("offline")
    await h.tick(61)
    h.switch("online")
    await h.tick(10)
    await h.recovery.stop()
    assert h.tokens[-1].cancelled


@pytest.mark.parametrize("failure", [False, True])
async def test_notice_requires_observed_outage_failure_and_cooldown(h, failure):
    h.supported = failure
    h.failure = RuntimeError("fictional sensitive failure") if failure else None
    h.seed()
    h.clock.advance(1)
    h.switch("online")
    await h.tick(11)
    assert not h.audit("catch_up.notice")
    for gap, expected in [(899, 0), (900, 1), (901, 1), (3 * 3600, 2)]:
        h.switch("offline")
        await h.tick(0)
        h.clock.advance(gap)
        h.switch("online")
        await h.tick(11)
        await h.agent.drain()
        assert len(h.audit("catch_up.notice")) == expected
    assert len(h.model.requests) == 2
    assert all(not req.tools for req in h.model.requests) or all(
        not any(t.name == "echo" for t in req.tools) for req in h.model.requests
    )
    assert "sensitive" not in str(h.audit("catch_up.failed"))


@pytest.mark.parametrize("guard", ["silenced", "paused", "busy", "successful_empty"])
async def test_notice_suppressed_by_runtime_guard_or_success(h, guard):
    h.seed()
    h.clock.advance(1)
    h.supported = guard == "successful_empty"
    h.switch("online")
    await h.tick(12)
    h.switch("offline")
    await h.tick(0)
    h.clock.advance(900)
    if guard == "silenced":
        h.agent.kernel.delivery.silenced = True
    elif guard == "paused":
        await h.agent.proactive_pause.set(True)
    elif guard == "busy":
        h.agent.batches[("owner_private", "owner")] = {}
    h.switch("online")
    try:
        await h.tick(12)
        assert not h.audit("catch_up.notice") and not h.channel.sent
    finally:
        h.agent.batches.clear()


async def test_group_old_addressed_message_uses_earlier_five_and_chat_window(h):
    h.agent.config["context"] = {"max_messages": 2}
    h.recovery.settings["group_scan_max"] = 10
    h.seed(scene="public_group")
    group = []
    for index in range(10):
        h.clock.advance(1)
        group.append(
            event(
                h.agent,
                f"fictional-topic-{index}",
                scene="public_group",
                role="member",
                actor="member",
                mention=index == 5,
            )
        )
    h.history["test_group"] = group
    h.switch("online")
    await h.tick(12)
    await h.recovery.cycle
    await h.agent.drain()
    assert len(h.model.requests) == 1
    messages = h.model.requests[0].messages
    earlier = [text_parts(m.parts) for m in messages if "当时在聊的" in text_parts(m.parts)]
    assert len(earlier) == 5 and all(
        f"fictional-topic-{i}" in line for i, line in enumerate(earlier)
    )
    assert "这之前还有一段没看到" in str(messages)
    assert "fictional-topic-9" in str(messages)


async def test_history_other_conversation_is_live_while_group_read_is_pending(h):
    h.seed(scene="public_group")
    h.clock.advance(1)
    h.block = True
    h.switch("online")
    await h.tick(12)
    assert h.calls[-1][0] == "test_group"
    before = len(h.model.requests)
    await h.companion._receive_event(event(h.agent, "fictional private"))
    await h.agent.drain()
    assert len(h.model.requests) == before + 1


async def test_quiet_history_and_queued_live_contribute_in_sent_order(h):
    h.seed(scene="public_group")
    h.clock.advance(1)
    old = event(h.agent, "fictional older", scene="public_group", role="member")
    h.clock.advance(20)
    h.history["test_group"] = [old]
    h.switch("online")
    await h.tick(1)
    live = event(h.agent, "fictional newer", scene="public_group", mention=True)
    live = live.model_copy(
        update={
            "message": live.message.model_copy(
                update={"sent_at": old.message.sent_at + timedelta(milliseconds=250)}
            )
        }
    )
    await h.companion._receive_event(live)
    await h.tick(12)
    await h.recovery.cycle
    await h.agent.drain()
    followup = event(h.agent, "fictional question", scene="public_group", mention=True)
    await h.agent.receive(followup)
    await h.agent.drain()
    text = "\n".join(text_parts(msg.parts) for msg in h.model.requests[-1].messages)
    assert text.index("fictional older") < text.index("fictional newer")


async def test_name_judgment_and_safety_pause_cover_historical_batches(h, monkeypatch):
    from clytie.sdk.behavior import Judge, Skip

    from .test_agent_judgment import FACTS

    h.seed(scene="public_group")
    h.clock.advance(1)
    item = event(h.agent, "fictional_internal", scene="public_group", role="member")
    h.history["test_group"] = [item]
    original = h.agent.host.behavior

    async def opinions(cid, point, arg, **kwargs):
        if point == "attention":
            assert arg[0].addressed.name == "internal"
            if len(arg) == 2:
                kwargs["judge_origin"].append(None)
                return Judge(facts=FACTS.model_copy(update={"name_hit": True}))
            return Skip()
        return await original(cid, point, arg, **kwargs)

    monkeypatch.setattr(h.agent.host, "behavior", opinions)
    h.switch("online")
    await h.tick(12)
    await h.recovery.cycle
    await h.clock.drain(512)
    assert len(h.model.requests) == 1 and not h.channel.sent
    h.switch("offline")
    await h.tick(61)
    paused = event(h.agent, scene="public_group", role="member", mention=True)
    h.history["test_group"].append(paused)
    monkeypatch.setattr(h.agent.safety_belt, "paused", lambda cid: True)
    h.switch("online")
    await h.tick(12)
    await h.recovery.cycle
    assert h.agent.ledger.get("inbound", paused.event_id)["reason"] == "safety_paused"
    assert h.audit("catch_up.done")[-1]["responding"] == 0
    assert len(h.model.requests) == 1


async def test_production_feed_storage_owner_health_and_no_private_payload(tmp_path, monkeypatch):
    from clytie.backup.coordination import run_sync
    from clytie.sdk.testing import FakeChannel, MockClock

    from .test_storage_agent_lifecycle import inbound, private_home

    state = "offline"
    monkeypatch.setattr(FakeChannel, "state", lambda _: ChannelState(status=state), raising=False)
    with_history = FakeChannel.features
    monkeypatch.setattr(
        FakeChannel,
        "features",
        lambda channel: with_history(channel).model_copy(update={"supports_history": True}),
    )
    runtime = Companion(private_home(tmp_path), "companion_a", MockClock())

    async def history(channel, cid, **kwargs):
        if cid == "test_group":
            raise RuntimeError("fictional detail")
        return rows

    monkeypatch.setattr(FakeChannel, "history", history, raising=False)
    await runtime.start()
    try:
        ledger = runtime.agent.ledger
        for seed in [inbound(runtime), inbound(runtime, scene="public_group")]:
            await run_sync(ledger, ledger.claim_inbound, seed)
        runtime.clock.advance(1)
        state = "online"
        rows = [inbound(runtime, text="fictional private material") for _ in range(50)]
        recovery = next(iter(runtime.catch_up.values()))
        recovery.settings["request_interval_seconds"] = 0
        recovery.observe()
        await runtime.clock.advance_and_drain(10)
        await recovery.cycle
        await runtime.agent.drain()
        audit = [
            r
            for r in await run_sync(ledger, ledger.rows, "audit")
            if r["kind"].startswith("catch_up.")
        ]
        assert [(r["kind"], r.get("read")) for r in audit] == [
            ("catch_up.done", 50),
            ("catch_up.failed", None),
        ]
        assert "catch_up.failed=1" in runtime.health()["warnings"]
        assert "fictional" not in str(audit) + str(runtime.health()["warnings"])
        assert runtime.storage_owner.status()["finalizers"] <= 1
    finally:
        await runtime.stop()
