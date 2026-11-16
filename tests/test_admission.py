"""B4a admission uses fictional logical identities only."""

from types import SimpleNamespace

import pytest

from clytie.admission import Admission


def policy(mode="registered", test="test_group"):
    return Admission(
        {"boundary": {"admission": mode}},
        {
            "test_conversation": test,
            "conversations": {
                "test_group": {"kind": "group", "members_expected": ["owner", "member"]},
                "test_private": {
                    "kind": "private",
                    "members_expected": ["owner", "member", "stranger"],
                },
            },
        },
    )


@pytest.mark.parametrize(
    "mode,kind,cid,role,actor,expected",
    [
        ("registered", "group", "test_group", "stranger", "stranger", None),
        ("registered", "group", "other", "owner", "owner", "not_registered"),
        ("registered", "private", "other", "owner", "owner", None),
        ("registered", "private", "other", "member", "member", None),
        ("registered", "private", "test_private", "stranger", "stranger", "stranger_private"),
        ("test_only", "group", "test_group", "owner", "owner", None),
        ("test_only", "group", "test_group", "member", "unexpected", "sender_not_expected"),
        ("test_only", "group", "other", "owner", "owner", "not_test_conversation"),
        ("test_only", "private", "test_private", "owner", "owner", None),
        ("test_only", "private", "other", "owner", "owner", "not_test_conversation"),
        ("test_only", "private", "test_private", "stranger", "stranger", "stranger_private"),
        ("test_only", "private", "test_private", "member", "unexpected", "sender_not_expected"),
        ("registered", "future", "test_group", "owner", "owner", "unknown_kind"),
        ("test_only", "future", "test_group", "owner", "owner", "unknown_kind"),
    ],
)
def test_admission_table(mode, kind, cid, role, actor, expected):
    p = policy(mode, "test_private" if kind == "private" else "test_group")
    conversation = SimpleNamespace(
        kind=kind, conversation_id=cid, scene="irrelevant", channel_id="irrelevant"
    )
    sender = SimpleNamespace(role=role, actor_id=actor)
    assert p.reason(conversation, sender) == expected


def test_admission_configuration_is_startup_snapshot():
    config = {"boundary": {"admission": "test_only"}}
    instance = {
        "test_conversation": "test_group",
        "conversations": {"test_group": {"kind": "group", "members_expected": ["owner"]}},
    }
    p = Admission(config, instance)
    config["boundary"]["admission"] = "registered"
    instance["conversations"]["other"] = {"kind": "group"}
    assert (
        p.reason(
            SimpleNamespace(kind="group", conversation_id="other"),
            SimpleNamespace(role="owner", actor_id="owner"),
        )
        == "not_test_conversation"
    )


@pytest.mark.parametrize(
    "mode,test", [("test_only", None), ("owner_present", "test_group"), ("bad", "test_group")]
)
def test_admission_invalid_configuration(tmp_path, mode, test):
    from clytie.config import load_home

    from .test_config import make_home, write_yaml

    home, root, instance, companion = make_home(tmp_path)
    companion.setdefault("boundary", {})["admission"] = mode
    if test is None:
        instance.pop("test_conversation", None)
    write_yaml(home / "clytie.local.yaml", instance)
    write_yaml(home / "companions/companion_a/companion.yaml", companion)
    report = load_home(home, repo_root=root)
    assert not next(row for row in report.companions if row["id"] == "companion_a")["valid"]
    assert any("admission" in error.pointer for error in report.errors)
    if mode == "owner_present":
        assert any("尚未支持" in error.expected for error in report.errors)


async def test_denied_inbound_has_only_safe_counters(tmp_path):
    from clytie.admission import Admission

    from .test_agent import event, finish, make_agent

    agent, channel, model = await make_agent(tmp_path)
    agent.instance["test_conversation"] = "test_group"
    agent.instance["conversations"]["test_group"]["members_expected"] = ["owner"]
    agent.admission = Admission({"boundary": {"admission": "test_only"}}, agent.instance)
    agent.kernel.delivery.admission = agent.admission
    before = {
        table: agent.ledger.rows(table)
        for table in ("inbound", "record", "audit", "intent", "dedupe")
    }
    memory_before = agent.memory.store.db.total_changes
    marker = "fictional-admission-content-must-not-persist"
    try:
        assert await agent.receive(event(agent, marker, cid="other", scene="public_group")) is None
        assert (
            await agent.receive(event(agent, marker, role="stranger", scene="stranger_private"))
            is None
        )
        assert (
            await agent.receive(event(agent, marker, role="member", scene="public_group")) is None
        )
        await agent.drain()
        assert not model.requests and not channel.sent and not channel.reacts
        assert before == {table: agent.ledger.rows(table) for table in before}
        assert agent.memory.store.db.total_changes == memory_before
        assert agent.admission.counts["not_test_conversation"] == 1
        assert agent.admission.counts["stranger_private"] == 1
        assert agent.admission.counts["sender_not_expected"] == 1
        assert marker not in repr(agent.admission.status()) + repr(agent.log.entries)
        assert "other" not in repr(agent.admission.status())
    finally:
        await finish(agent)


@pytest.mark.parametrize("kind", ["reply", "proactive", "react"])
async def test_delivery_denied_without_platform_call_or_retry(tmp_path, kind):
    from uuid import uuid4

    from clytie.sdk.types import DeliveryIntent

    from .test_agent import event, finish, make_agent

    agent, channel, _ = await make_agent(tmp_path)
    try:
        incoming = event(agent, scene="public_group")
        key, _ = agent.ledger.claim_inbound(incoming)
        record = (
            agent.ledger.open_record(event_id=key)
            if kind != "proactive"
            else agent.ledger.open_record(conversation=incoming.conversation)
        )
        intent = DeliveryIntent(
            intent_id=uuid4().hex,
            trace_id=record["trace_id"],
            companion_id=agent.cid,
            conversation_id="test_group",
            channel_id="fake",
            kind=kind,
            parts=[] if kind == "react" else [{"type": "text", "text": "fictional outgoing"}],
            emoji="ok" if kind == "react" else None,
        )
        agent.ledger.register_intents(record["trace_id"], [intent])
        agent.kernel.delivery.admission = policy("test_only", "another_test")
        assert await agent.kernel.delivery.send_one(intent.intent_id) == "failed"
        assert await agent.kernel.delivery.send_one(intent.intent_id) == "failed"
        assert not channel.sent and not channel.reacts
        row = agent.ledger.get("intent", intent.intent_id)
        assert row["error"]["code"] == "not_admitted"
        attempts = agent.ledger.rows("attempt")
        assert len(attempts) == 1
        assert attempts[0]["receipt"]["status"] == "failed"
        assert attempts[0]["receipt"]["error"]["code"] == "not_admitted"
    finally:
        await finish(agent)


async def test_counters_reach_health_and_local_http_without_identifiers(tmp_path):
    from aiohttp import ClientSession

    from clytie.api.management import Management
    from clytie.api.server import Server
    from clytie.supervisor.common import Audit, read

    from .test_supervisor import make, spin

    svc, launcher, clock = await make(tmp_path)
    api = Management(svc, None)
    api.configuration_status = lambda: {"valid": True}
    server = Server(tmp_path, clock, api, Audit(tmp_path, clock), {"port": 0})
    await server.start()
    try:
        await launcher.children[0][1].heartbeat(
            admission={
                "mode": "test_only",
                "rejected": {
                    "not_registered": 3,
                    "fictional-private-marker": 4,
                    "unknown_kind": -1,
                },
                "content": "fictional-private-marker",
            }
        )
        await spin()
        clock.advance(5)
        await svc.flush_health()
        expected = {"mode": "test_only", "rejected": {"not_registered": 3}}
        assert (
            read(tmp_path / "run/health.json")["companions"]["companion_0"]["admission"] == expected
        )
        headers = {}
        headers["Authorization"] = "Bearer " + server.auth.token
        async with ClientSession() as client:
            response = await client.get(
                f"http://127.0.0.1:{server.port}/api/v1/status",
                headers=headers,
            )
            assert response.status == 200
            data = await response.json()
        assert data["result"]["companions"]["companion_0"]["admission"] == expected
        assert "fictional-private-marker" not in repr(data) + repr(svc.log.entries)
    finally:
        await server.close()
        await svc.stop()


@pytest.mark.parametrize("mode", [None, "registered", "test_only"])
def test_valid_admission_configuration(tmp_path, mode):
    from clytie.config import load_home

    from .test_config import make_home, write_yaml

    home, root, instance, companion = make_home(tmp_path)
    if mode is not None:
        companion.setdefault("boundary", {})["admission"] = mode
    write_yaml(home / "companions/companion_a/companion.yaml", companion)
    report = load_home(home, repo_root=root)
    assert report.valid, report.to_dict()
    assert report.plugin_configs["companion_a"]["config"]["boundary"]["admission"] == (
        mode or "registered"
    )


async def test_companion_health_reports_actual_agent_denials(tmp_path):
    from clytie.supervisor.companion import Companion

    from .test_agent import event, finish, make_agent

    agent, _, _ = await make_agent(tmp_path)
    runtime = Companion(tmp_path, agent.cid, agent.clock, host=agent.host)
    runtime.agent, runtime.kernel = agent, agent.kernel
    try:
        incoming = event(agent, role="stranger", scene="stranger_private")
        assert await agent.receive(incoming) is None
        # A future/unrecognized event shape must fail closed even before SDK validation.
        unknown = SimpleNamespace(
            companion_id=agent.cid,
            message=incoming.message,
            conversation=SimpleNamespace(kind="future", conversation_id="unknown"),
        )
        assert await agent.receive(unknown) is None
        status = runtime.health()["admission"]
        assert status["mode"] == "registered"
        assert status["rejected"]["stranger_private"] == 1
        assert status["rejected"]["unknown_kind"] == 1
        assert agent.ledger.rows("inbound") == []
    finally:
        await finish(agent)


def test_admission_ignores_configuration_extension_entries():
    instance = {
        "conversations": {
            "x-metadata": False,
            "x-not-a-conversation": {"kind": "group"},
            "owner_private": {
                "kind": "private",
                "channel": "fake",
                "platform_id": "fictional-owner",
            },
        },
        "people": {
            "x-metadata": False,
            "owner": {
                "role": "owner",
                "accounts": [{"channel": "fake", "account": "fictional-owner"}],
            },
        },
    }
    p = Admission({}, instance)
    assert set(p.conversations) == {"owner_private"}
    assert p.private_actors["owner_private"].actor_id == "owner"
    assert (
        p.reason(SimpleNamespace(kind="group", conversation_id="x-not-a-conversation"), None)
        == "not_registered"
    )
