"""用假收据、假模型和临时账本验证群聊安全带。"""

import json
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from jsonschema import Draft202012Validator

from clytie.agent.policy import text_parts
from clytie.sdk.behavior import Defer, Respond
from clytie.sdk.types import Receipt, TextPart

from .test_agent import event, finish, make_agent


def settings(replies=50):
    return {
        "boundary": {"safety_belt": {"replies": replies, "window_minutes": 10, "pause_minutes": 5}}
    }


class Notifier:
    def __init__(self):
        self.notices = []

    async def notify(self, notice, token):
        self.notices.append(notice)
        return Receipt(intent_id=notice.dedupe_key, status="sent", at=self.clock.now())


def seed_reply(agent, *, parent=None, proactive=False, parts=1, status="sent"):
    """通过账本的已有入口登记历史收据，模拟接入安全带前的记录。"""
    ledger = agent.ledger
    source = event(agent, scene="public_group", role="member")
    if parent or proactive:
        record = ledger.open_record(conversation=source.conversation, parent_trace_id=parent)
    else:
        key, _ = ledger.claim_inbound(source)
        record = ledger.open_record(event_id=key)
    intents = [
        agent.intent(
            record, [TextPart(text="fictional split")], kind="proactive" if proactive else "reply"
        )
        for _ in range(parts)
    ]
    ledger.seal(record["trace_id"], "ok", intents)
    for intent in intents:
        n = ledger.begin_attempt(intent.intent_id)
        receipt = Receipt(intent_id=intent.intent_id, status=status, at=agent.clock.now())
        ledger.record_receipt(intent.intent_id, n, receipt)
    return record


async def group_reply(agent, *, role="member", cid="test_group"):
    await agent.receive(event(agent, scene="public_group", role=role, cid=cid, mention=True))
    await agent.drain()


async def test_belt_counts_first_confirmation_per_inbound_across_restart(tmp_path):
    agent, _, _ = await make_agent(tmp_path, changes=settings())
    root = seed_reply(agent, parts=2)
    seed_reply(agent, parent=root["trace_id"])
    seed_reply(agent, proactive=True)
    seed_reply(agent, status="unknown")
    seed_reply(agent, status="failed")
    for _ in range(47):
        seed_reply(agent)
    await finish(agent)
    agent, channel, model = await make_agent(
        tmp_path,
        changes=settings(),
        conversations={
            "other_group": {"kind": "group", "scene": "public_group", "channel": "fake-channel"}
        },
    )
    notices = Notifier()
    notices.clock = agent.clock
    agent.safety_belt.notifier = notices
    await group_reply(agent)
    assert agent.safety_belt.read_status() == {}
    await group_reply(agent)
    assert len(notices.notices) == 1
    assert channel.sent[-1].conversation_id == "owner_private"
    assert agent.safety_belt.read_status()["test_group"]["count"] == 1
    trip = next(row for row in agent.ledger.rows("audit") if row["kind"] == "safety_belt.tripped")
    assert set(trip) == {
        "kind",
        "trace_id",
        "at",
        "conversation_id",
        "count",
        "window_minutes",
        "until",
    }
    assert "test_group" in notices.notices[0].body and "00:05" in notices.notices[0].body
    observed = []
    original = agent.host.behavior

    async def observe(cid, point, *args, **kwargs):
        observed.append(point)
        return await original(cid, point, *args, **kwargs)

    agent.host.behavior = observe
    inbound = event(
        agent, "fictional paused utterance", scene="public_group", role="member", mention=True
    )
    calls = len(model.requests)
    assert await agent.receive(inbound) is None
    assert await agent.receive(inbound) is None
    assert observed == ["message_received"] and len(model.requests) == calls
    ignored = agent.ledger.get("inbound", inbound.event_id)
    assert (ignored["state"], ignored["reason"]) == ("ignored", "safety_paused")
    await group_reply(agent, role="owner")
    assert any(
        "fictional paused utterance" in text_parts(msg.parts) for msg in model.requests[-1].messages
    )
    await group_reply(agent, cid="other_group")
    assert channel.sent[-1].conversation_id == "other_group"
    await agent.receive(event(agent))
    await agent.drain()
    assert channel.sent[-1].conversation_id == "owner_private"
    await finish(agent)
    agent, _, _ = await make_agent(tmp_path, changes=settings())
    assert agent.safety_belt.paused("test_group")
    agent.clock.advance(300)
    assert not agent.safety_belt.paused("test_group")
    await group_reply(agent)
    assert agent.safety_belt.read_status()["test_group"]["count"] == 1
    for _ in range(48):
        seed_reply(agent)
    await group_reply(agent)
    assert agent.safety_belt.read_status()["test_group"]["count"] == 2
    agent.clock.advance(24 * 3600)
    assert agent.safety_belt.read_status() == {}
    await finish(agent)


@pytest.mark.parametrize(
    "response",
    [
        {"finish": "stop", "text": ""},
        {"finish": "error", "error": {"code": "auth_invalid", "message": ""}},
        {"finish": "stop", "text": "fictional blocked topic"},
    ],
)
async def test_belt_notifies_owner_with_system_fallback(tmp_path, response):
    agent, channel, _ = await make_agent(
        tmp_path,
        {"finish": "stop", "text": "fictional reply"},
        response,
        response,
        changes=settings(1),
    )
    check = agent.outbound.check

    async def outbound(text, scene, **kwargs):
        if text == "fictional blocked topic":
            return {"privacy": ["fictional restriction"]}
        return await check(text, scene, **kwargs)

    agent.outbound.check = outbound
    await group_reply(agent)
    sent = [i for i in channel.sent if i.conversation_id == "owner_private"]
    assert len(sent) == 1 and sent[0].kind == "proactive"
    assert agent.safety_belt.read_status()["test_group"]["until"].endswith("00:05:00+00:00")
    text = text_parts(sent[0].parts)
    assert text.startswith("Clytie 系统通知") and "test_group" in text and "00:05" in text
    assert any(msg == "safety_belt.notifier_unavailable" for _, msg, _ in agent.log.entries)
    await finish(agent)


async def test_belt_missing_owner_and_window_uses_first_confirmed_receipt(tmp_path):
    agent, channel, model = await make_agent(
        tmp_path,
        changes=settings(2),
        people={
            "owner": {"role": "owner", "accounts": []},
            "member": {"role": "member", "accounts": []},
        },
    )
    old = seed_reply(agent)
    agent.clock.advance(600)
    seed_reply(agent, parent=old["trace_id"])
    await group_reply(agent)
    assert agent.safety_belt.read_status() == {}
    await group_reply(agent)
    assert agent.safety_belt.read_status()["test_group"]["owner_private_missing"]
    assert len(model.requests) == 2 and all(i.conversation_id == "test_group" for i in channel.sent)
    await finish(agent)


async def test_belt_late_sent_correction_trips_once_without_replay(tmp_path):
    agent, _, _ = await make_agent(tmp_path, changes=settings(1))
    row = seed_reply(agent, status="unknown")
    intent = agent.ledger.intents(row["trace_id"])[0]
    receipt = Receipt(
        intent_id=intent["intent_id"], status="sent", at=agent.clock.now() - timedelta(days=1)
    )
    fresh, trip = agent.safety_belt.record_receipt(intent["intent_id"], 1, receipt, retry=False)
    assert not fresh and trip["count"] == 1
    assert agent.safety_belt.record_receipt(intent["intent_id"], 1, receipt, retry=False) == (
        False,
        None,
    )
    assert agent.safety_belt.paused("test_group")
    await finish(agent)


def test_safety_belt_schema_defaults_bounds_and_no_disable():
    path = Path(__file__).resolve().parents[1] / "schemas/companion.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))["properties"]["boundary"]["properties"][
        "safety_belt"
    ]
    validate = Draft202012Validator(schema).is_valid
    assert {k: v["default"] for k, v in schema["properties"].items()} == {
        "replies": 200,
        "window_minutes": 30,
        "pause_minutes": 30,
    }
    for key, minimum, maximum in [
        ("replies", 50, 1000),
        ("window_minutes", 10, 120),
        ("pause_minutes", 5, 240),
    ]:
        assert validate({key: minimum}) and validate({key: maximum})
        assert not validate({key: minimum - 1}) and not validate({key: maximum + 1})
    assert not validate(False) and not validate({"enabled": False})


def test_status_shows_effective_belt_config_before_any_trip(tmp_path, capsys, monkeypatch):
    from clytie import cli
    from clytie.api import local_status

    from .test_config import make_home, write_yaml

    home, repository, _, companion = make_home(tmp_path)
    for expected in (
        {"replies": 200, "window_minutes": 30, "pause_minutes": 30},
        {"replies": 67, "window_minutes": 20, "pause_minutes": 10},
    ):
        if expected["replies"] != 200:
            companion.setdefault("boundary", {})["safety_belt"] = expected
            write_yaml(home / "companions/companion_a/companion.yaml", companion)
        report = local_status(home, repo_root=repository)
        assert report.to_dict()["companions"][0]["safety_belt_config"] == expected
        monkeypatch.setattr(cli, "local_status", lambda _: report)
        assert cli.main(["status", "--home", str(home)]) == 0
        assert f"回复 {expected['replies']} 次" in capsys.readouterr().out


def test_status_renders_unregistered_group_in_instance_timezone(tmp_path, monkeypatch, capsys):
    from clytie import cli
    from clytie.api import local_status
    from clytie.sdk.testing import MockClock
    from clytie.supervisor.common import write

    from .test_config import make_home, write_yaml

    home, repo, instance, _ = make_home(tmp_path)
    instance["timezone"] = "Asia/Shanghai"
    write_yaml(home / "clytie.local.yaml", instance)
    clock = MockClock()
    monkeypatch.setattr("clytie.clock.SystemClock.now", lambda self: clock.now())
    until = (clock.now() + timedelta(minutes=5)).isoformat()
    row = dict(count=1, until=until, owner_private_missing=False)
    write(
        home / "run/health.json",
        {"companions": {"companion_a": {"safety_belt": {"一个未登记的群": [row]}}}},
    )
    report = local_status(home, repo_root=repo)
    monkeypatch.setattr(cli, "local_status", lambda _: report)
    assert cli.main(["status", "--home", str(home)]) == 0
    out = capsys.readouterr().out
    assert "一个未登记的群 当天触发 1 次，恢复时间 08:05" in out and until not in out


async def test_safety_notifier_ipc_each_trip_and_health_projection(tmp_path):
    from .test_supervisor import make, spin

    svc, launcher, clock = await make(tmp_path)
    svc.conversations = {"test_group": "group"}
    notifier = Notifier()
    notifier.clock = clock
    svc.notifications.plugins = [(notifier, True)]
    _, child = launcher.children[0]
    until = (clock.now() + timedelta(minutes=30)).isoformat()
    config = {"replies": 200, "window_minutes": 30, "pause_minutes": 30}
    await child.heartbeat(safety_belt_config=config)
    await spin()
    assert svc.rows["companion_0"]["safety_belt_config"] == config
    for i in (1, 2):
        await child.queue.put(
            dict(type="safety_belt", seq=i, count=i, until=until, conversation_id="test_group")
        )
    await child.heartbeat(
        safety_belt={"test_group": {"count": 2, "until": until, "owner_private_missing": True}},
        attention={"test_group": {"count": 3, "tokens": 12, "text": "fixture"}},
    )
    await spin()
    unknown = "一个未登记的群"
    row = {"count": 1, "until": until, "owner_private_missing": False}
    await child.queue.put(
        dict(type="safety_belt", seq=3, count=1, until=until, conversation_id=unknown)
    )
    await child.heartbeat(
        safety_belt={
            "test_group": {"count": 2, "until": until, "owner_private_missing": True},
            unknown: [row, row],
            "anon_fixture_raw": row,
        },
        attention={
            "test_group": {"count": 3, "tokens": 12},
            "未登记的群": {"count": 4, "tokens": 18},
            "anon_fixture_raw": {"count": 1, "tokens": 0},
        },
    )
    await spin()
    clock.advance(5)
    await svc.flush_health()
    health = (tmp_path / "run/health.json").read_text(encoding="utf8")
    assert "anon_fixture_raw" not in health
    assert json.loads(health)["companions"]["companion_0"]["safety_belt"][unknown] == [row, row]
    assert svc.rows["companion_0"]["safety_belt"][unknown] == [row, row]
    assert len(notifier.notices) == 3
    assert unknown in notifier.notices[-1].body and "00:30" in notifier.notices[-1].body
    assert svc.rows["companion_0"]["safety_belt"]["test_group"]["owner_private_missing"]
    assert svc.rows["companion_0"]["attention"] == {
        "test_group": {"count": 3, "tokens": 12},
        "未登记的群": {"count": 4, "tokens": 18},
    }
    await svc.stop()


async def test_unregistered_groups_trip_independently_and_hide_identity(tmp_path):
    from clytie.sdk.testing import FakeChannel, FakeContext

    cfg = settings(1)
    cfg["boundary"]["admission"] = "owner_present"
    agent, channel, model = await make_agent(tmp_path, changes=cfg)
    groups = ("anon_fixture_alpha", "anon_fixture_beta")
    membership = FakeChannel(
        FakeContext(clock=agent.clock), membership={(group, "owner"): True for group in groups}
    )
    channel.features = membership.features
    channel.is_member = membership.is_member
    notifier = Notifier()
    notifier.clock = agent.clock
    agent.safety_belt.notifier = notifier
    agent.safety_belt.zone = ZoneInfo("Asia/Shanghai")
    agent.clock.advance(16 * 3600 - 60)
    try:
        for group in groups:
            assert not agent.safety_belt.paused(group)
            await group_reply(agent, cid=group)
            assert agent.safety_belt.paused(group)
            before = len(model.requests)
            member = event(agent, scene="public_group", role="member", mention=True, cid=group)
            await agent.receive(member)
            assert agent.ledger.get("inbound", member.event_id)["reason"] == "safety_paused"
            assert len(model.requests) == before
            await group_reply(agent, cid=group, role="owner")
            assert channel.sent[-1].conversation_id == group

        rows = agent.safety_belt.read_status()
        assert [row["count"] for row in rows["一个未登记的群"]] == [1, 1]
        assert all(
            "一个未登记的群" in n.body and "2000-01-02 00:04" in n.body for n in notifier.notices
        )
        audits = [r for r in agent.ledger.rows("audit") if r["kind"] == "safety_belt.tripped"]
        warnings = [
            fields for _, name, fields in agent.log.entries if name == "safety_belt.tripped"
        ]
        assert len(audits) == len(warnings) == 2
        private_prompts = [
            text_parts(msg.parts)
            for req in model.requests
            for msg in req.messages
            if "停不下来了" in text_parts(msg.parts)
        ]
        assert private_prompts and all(
            "一个未登记的群" in m and "2000-01-02 00:04" in m for m in private_prompts
        )
        outward = json.dumps(
            [rows, warnings, audits, private_prompts, [n.body for n in notifier.notices]],
            ensure_ascii=False,
        )
        assert all(group not in outward for group in groups)
    finally:
        await finish(agent)


async def test_pending_member_batch_ignored_if_owner_reply_pauses_group(tmp_path):
    agent, channel, model = await make_agent(tmp_path, changes=settings(1))
    original = agent.host.behavior

    async def behavior(cid, phase, supplied, **kwargs):
        if phase == "attention" and supplied[0].message.sender.role == "member":
            return Defer(seconds=1) if agent.clock.monotonic() < 1 else Respond()
        return await original(cid, phase, supplied, **kwargs)

    agent.host.behavior = behavior
    members = [event(agent, role="member", scene="public_group", mention=True) for _ in range(2)]
    try:
        for item in members:
            await agent.receive(item)
        await agent.clock.drain()
        await agent.receive(event(agent, role="owner", scene="public_group", mention=True))
        await agent.kernel.queue.drain()
        assert agent.safety_belt.paused("test_group")
        await agent.clock.advance_and_drain(1)
        await agent.drain()
        assert all(
            agent.ledger.get("inbound", m.event_id)["reason"] == "safety_paused" for m in members
        )
        assert not {r["source_event_id"] for r in agent.ledger.rows("record")} & {
            m.event_id for m in members
        }
        assert len([i for i in channel.sent if i.conversation_id == "test_group"]) == 1
    finally:
        await finish(agent)
