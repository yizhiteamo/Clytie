"""PR 第 1 轮：正则语义、通知隔离、真实入站暂停与模型取消。"""

import asyncio
from uuid import uuid4

import pytest

from clytie.agent.persona import Persona
from clytie.config import compile_input_pattern
from clytie.sdk import CancelToken
from clytie.sdk.types import Receipt, ToolCall

from .test_agent import event, finish, make_agent

BOUNDARY = {
    "finish": "tool_calls",
    "tool_calls": [
        {"call_id": "report", "tool": "core_report_boundary", "arguments": {"category": "romance"}}
    ],
}
REFUSAL = {"finish": "stop", "text": "fictional refusal"}


@pytest.mark.parametrize(
    "pattern,text,matched",
    [
        (r"^\S+$", "spam", True),
        (r"^\S+$", "two words", False),
        (r"spam\Z", "spam", True),
        (r"spam\Z", "spam later", False),
        ("SPAM", "ＳＰＡＭ", True),
    ],
)
async def test_filter_preserves_regex_escapes(tmp_path, pattern, text, matched):
    assert compile_input_pattern(pattern).pattern == pattern
    agent, _, model = await make_agent(
        tmp_path, changes={"boundary": {"input_filter": {"patterns": [pattern]}}}
    )
    incoming = event(agent, text, role="member", mention=True)
    trace = await agent.receive(incoming)
    await agent.drain()
    assert (trace is None) == matched
    assert bool(model.requests) != matched
    await finish(agent)


@pytest.mark.parametrize("stuck", [False, True])
async def test_notification_failure_does_not_block_reply_or_queue(tmp_path, stuck):
    agent, channel, model = await make_agent(
        tmp_path, BOUNDARY, REFUSAL, changes={"boundary": {"refusal_threshold": 1}}
    )
    started = asyncio.Event()
    tokens = []

    class Notifier:
        async def notify(self, notice, cancel):
            tokens.append(cancel)
            started.set()
            if stuck:
                await asyncio.Event().wait()
            raise RuntimeError("fictional notification failure")

    agent.boundary.notifier = Notifier()
    trace = await agent.receive(event(agent, role="member", scene="public_group", mention=True))
    await started.wait()
    await agent.drain()
    assert agent.ledger.get("record", trace)["exec_status"] == "ok"
    assert channel.sent[0].parts[0].text == "fictional refusal"
    second = await agent.receive(event(agent, role="owner", scene="public_group", mention=True))
    await agent.drain()
    assert agent.ledger.get("record", second)["exec_status"] == "ok"
    if stuck:
        agent.clock.advance(agent.boundary.notification_timeout)
    await agent.boundary.drain_notifications()
    assert "boundary_notification_failed" in agent.boundary.warnings
    assert tokens[0].cancelled
    assert any(m.role == "tool" and m.parts[0].text == "已记录" for m in model.requests[1].messages)
    await finish(agent)


async def test_cross_group_boundary_pause_skips_all_behavior_and_model(tmp_path):
    agent, _, model = await make_agent(
        tmp_path,
        BOUNDARY,
        REFUSAL,
        BOUNDARY,
        REFUSAL,
        changes={"boundary": {"refusal_threshold": 2}},
    )
    notices = []

    class Notifier:
        async def notify(self, notice, cancel):
            notices.append(notice)
            return Receipt(intent_id=uuid4().hex, status="sent", at=agent.clock.now())

    agent.boundary.notifier = Notifier()
    for cid in ["test_group", "another_group"]:
        await agent.receive(
            event(agent, role="member", scene="public_group", cid=cid, mention=True)
        )
        await agent.drain()
    await agent.boundary.drain_notifications()
    assert len(notices) == 1
    count = len(model.requests)
    phases = []
    original = agent.host.behavior

    async def behavior(cid, phase, value, **kwargs):
        phases.append(phase)
        return await original(cid, phase, value, **kwargs)

    agent.host.behavior = behavior
    paused = event(agent, role="member", scene="public_group", mention=True)
    await agent.receive(paused)
    row = agent.ledger.get("inbound", paused.event_id)
    assert row["state"] == "ignored" and row["reason"] == "boundary_paused"
    await agent.receive(
        event(agent, role="member", scene="public_group").model_copy(update={"dedupable": False})
    )
    assert not phases and len(model.requests) == count
    # 解除只清零暂停和类别计数，通知窗口保留。
    await agent.boundary.release(agent.cid, paused.message.sender.actor_id)
    assert not agent.boundary.paused(paused.message.sender)
    for _ in range(2):
        await agent.boundary.report(uuid4().hex, paused.message.sender, "romance", CancelToken())
    await agent.boundary.drain_notifications()
    assert len(notices) == 1
    assert agent.boundary.paused(paused.message.sender)
    await finish(agent)


@pytest.mark.parametrize("same_actor,expected", [(True, "cancelled"), (False, "denied")])
async def test_natural_language_core_cancel_checks_requester(tmp_path, same_actor, expected):
    agent, _, model = await make_agent(tmp_path)
    source = event(agent, role="member", actor="member_a", scene="public_group")
    agent.ledger.claim_inbound(source)
    target = await agent.kernel.open_record(event_id=source.event_id)
    agent.ledger.claim_invocation(
        target["trace_id"],
        "echo-tool",
        ToolCall(call_id="work", tool="echo", arguments={"text": "fictional"}),
        "fictional-key",
        timeout=60,
        schema={},
    )
    task_id = agent.ledger.get("record", target["trace_id"])["task_id"]
    model.turns.extend(
        [
            {
                "finish": "tool_calls",
                "tool_calls": [
                    {
                        "call_id": "cancel",
                        "tool": "core_cancel_task",
                        "arguments": {"task_id": task_id.upper()},
                    }
                ],
            },
            {"finish": "stop", "text": "fictional result"},
        ]
    )
    incoming = event(
        agent,
        "请取消那件事",
        role="member",
        actor="member_a" if same_actor else "member_b",
        scene="public_group",
        mention=True,
    )
    await agent.receive(incoming)
    await agent.drain()
    assert "core_cancel_task" in [tool.name for tool in model.requests[0].tools]
    assert [m.parts[0].text for m in model.requests[1].messages if m.role == "tool"] == [expected]
    assert (
        agent.ledger.get("record", target["trace_id"])["exec_status"] == "cancelled"
    ) == same_actor
    if same_actor:
        specs, _ = await agent.tools(incoming.conversation, incoming.message.sender)
        assert "core_cancel_task" not in [tool.name for tool in specs]
    await finish(agent)


async def test_persona_layers_and_unpaired_marker_line(tmp_path):
    agent, _, _ = await make_agent(tmp_path)
    root = tmp_path / "companions/companion_a/persona"
    (root / "public.md").write_text("fictional public layer", encoding="utf-8")
    agent.config["persona"]["files"]["public"] = "public.md"
    agent.config["persona"]["scenes"]["public_group"] = ["base", "public"]
    persona = Persona(root, agent.config)
    assert "fictional public layer" not in persona.render("owner_private")
    rendered = persona.render("public_group")
    assert rendered.index("Fictional persona") < rendered.index("fictional public layer")
    assert rendered.endswith("有人认真需要确认时如实说明。")
    (root / "public.md").write_text("first line\n<!-- private -->oops", encoding="utf-8")
    with pytest.raises(ValueError, match=r"public.md:line 2: unpaired"):
        Persona(root, agent.config)
    await finish(agent)


async def test_agent_model_type_rejected(tmp_path):
    agent, _, _ = await make_agent(tmp_path)
    agent.features = agent.features.model_copy(update={"agent_type": True})
    with pytest.raises(ValueError, match="unsupported_model_type"):
        await agent.start()
    await finish(agent)


async def test_private_proactive_model_failure_never_falls_back(tmp_path):
    agent, channel, _ = await make_agent(tmp_path, {"finish": "error"})
    trace = await agent.submit("owner_private", topic="fictional topic", reason="fictional timer")
    await agent.drain()
    assert agent.ledger.get("record", trace)["exec_status"] == "error"
    assert not channel.sent and not channel.reacts
    await finish(agent)


async def test_tool_not_offered_stays_denied_after_quota_recovers(tmp_path):
    from clytie.sdk.types import QuotaSnapshot

    from .test_agent_acceptance import ECHO

    agent, channel, model = await make_agent(tmp_path)
    original = agent.declarations

    def declarations():
        values = original()
        pid, declaration = values["echo"]
        values["echo"] = (pid, {**declaration, "heavy": True})
        return values

    agent.declarations = declarations
    agent.protection.update(
        QuotaSnapshot(
            windows=[{"name": "window", "remaining_ratio": 0.1}],
            observed_at=agent.clock.now(),
            source="fake",
        )
    )
    model.turns.extend(
        [
            {
                **ECHO,
                "quota": {
                    "windows": [{"name": "window", "remaining_ratio": 0.8}],
                    "observed_at": agent.clock.now(),
                    "source": "fake",
                },
            },
            REFUSAL,
        ]
    )
    trace = await agent.receive(event(agent, role="member", scene="public_group", mention=True))
    await agent.drain()
    assert "echo" not in [tool.name for tool in model.requests[0].tools]
    assert not agent.protection.active
    assert not agent.ledger.rows("invocation")
    assert [m.parts[0].text for m in model.requests[1].messages if m.role == "tool"] == [
        "unknown_tool"
    ]
    assert agent.ledger.get("record", trace)["exec_status"] == "ok"
    assert channel.sent[0].parts[0].text == "fictional refusal"
    await finish(agent)


async def test_unavailable_notifier_warning_is_deduplicated(tmp_path):
    agent, _, _ = await make_agent(tmp_path, changes={"boundary": {"refusal_threshold": 1}})
    for actor in ["member_a", "member_b"]:
        member = event(agent, role="member", actor=actor).message.sender
        await agent.boundary.report(uuid4().hex, member, "romance", CancelToken())
    assert agent.boundary.warnings == ["boundary_notifier_unavailable"]
    await finish(agent)
