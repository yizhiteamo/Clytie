"""真实 Agent/宿主/交付路径的静默、失效与关闭回归。"""

import pytest

from clytie.plugin_host.instance import InstanceNotifiers
from clytie.plugin_host.support import HostLog
from clytie.sdk.testing import MockClock
from clytie.sdk.types import ProcessResult, Receipt
from clytie.supervisor.common import Audit
from clytie.supervisor.notifications import Notifications

from .host_helpers import SOURCE, enable, host, plugin
from .test_agent import event, finish, make_agent
from .test_kernel_delivery import settle, task_with_intents
from .test_kernel_service import setup


async def test_model_auth_invalid_ignores_following_messages_without_fallback(tmp_path):
    a, channel, model = await make_agent(
        tmp_path,
        {
            "finish": "error",
            "error": {"code": "auth_invalid", "message": "fictional private credential"},
        },
    )
    await a.receive(event(a))
    await a.drain()
    assert a.model_health == "auth_invalid"
    assert channel.sent == []
    next_event = event(a)
    assert await a.receive(next_event) is None
    assert a.ledger.get("inbound", next_event.event_id)["reason"] == "model_auth_invalid"
    assert len(model.requests) == 1
    await finish(a)


async def test_silence_owner_inbound_reactions_and_proactive_no_replay(tmp_path):
    a, channel, model = await make_agent(tmp_path)
    a.kernel.delivery.silenced = True
    message = event(a)
    assert await a.receive(message) is None
    assert a.ledger.get("inbound", message.event_id)["reason"] == "silenced"
    submission = await a.submit("owner_private", topic="fictional topic", reason="fictional")
    assert submission.status == "paused" and submission.trace_id is None
    assert not a.ledger.rows("record")
    assert model.requests == [] and channel.sent == [] and channel.reacts == []
    a.kernel.delivery.silenced = False
    assert await a.receive(message) is None
    assert model.requests == []
    a.shutting_down = True
    message = event(a)
    await a.receive(message)
    assert a.ledger.get("inbound", message.event_id)["reason"] == "shutting_down"
    await finish(a)


async def test_silence_finishes_only_current_split_item(tmp_path):
    kernel, channel, clock = setup(tmp_path)
    record, _, intents = await task_with_intents(kernel, split=True)
    original = channel.send

    async def send(intent, cancel):
        result = await original(intent, cancel)
        kernel.delivery.silenced = True
        return result

    channel.send = send
    await kernel.delivery.deliver(record["trace_id"])
    assert len(channel.text) == 1
    assert all(
        kernel.ledger.get("intent", i.intent_id)["error"]["code"] == "silenced" for i in intents[1:]
    )
    kernel.delivery.silenced = False
    await kernel.delivery.deliver(record["trace_id"])
    assert len(channel.text) == 1
    await settle(kernel)
    kernel.ledger.close()


async def test_close_plugin_error_continues_dependencies_and_companions(tmp_path):
    plugin(tmp_path, "base")
    source = SOURCE.replace(
        "await self.ctx.clock.sleep(0)\n    async def on_event",
        'raise ValueError("fictional")\n    async def on_event',
    )
    plugin(tmp_path, "dependent", source, dependencies={"base": {"version": ">=1"}})
    plugin(tmp_path, "other")
    h = host(tmp_path)
    h.configure("companion_b")
    await enable(h, "dependent")
    await enable(h, "other", "companion_b")
    result = await h.close()
    assert result["companion_a"]["stopped"] == ["base"]
    assert "dependent" in result["companion_a"]["not_stopped"]
    assert result["companion_b"]["stopped"] == ["other"]


@pytest.mark.parametrize("point", ["start", "factory"])
async def test_plugin_unavailable_code_propagated_without_message(tmp_path, point):
    source = SOURCE
    if point == "start":
        source = "from clytie.sdk import PluginUnavailable\n" + SOURCE.replace(
            "self.ctx = ctx",
            'self.ctx = ctx\n        raise PluginUnavailable("auth_invalid", "fictional private credential")',
        )
        contribution = {}
    else:
        source += '\n    def create_notifier(self):\n        from clytie.sdk import PluginUnavailable\n        raise PluginUnavailable("auth_invalid", "fictional private credential")\n'
        contribution = {"notifier": {"factory": "create_notifier"}}
    plugin(tmp_path, "unavailable", source, contributes=contribution)
    h = host(tmp_path)
    plan = h.plan_enable("companion_a", "unavailable")
    await h.apply(plan.plan_id)
    assert h.runtimes["companion_a", "unavailable"].failure_code == "auth_invalid"
    assert h.states["companion_a"]["unavailable"]["code"] == "auth_invalid"
    assert "fictional private credential" not in str(h.states)
    await h.close()


class FakeNotifier:
    def __init__(self, clock, sent=True):
        self.clock, self.sent = clock, sent
        self.calls = []

    async def notify(self, notice, cancel):
        self.calls.append(notice)
        return Receipt(
            intent_id="fictional-notice",
            status="sent" if self.sent else "failed",
            at=self.clock.now(),
        )


async def test_notification_persistence_retry_and_outward_silence(tmp_path):
    clock = MockClock()
    local, outward = FakeNotifier(clock, False), FakeNotifier(clock)
    audit, log = Audit(tmp_path, clock), HostLog()
    n = Notifications(tmp_path, clock, log, audit, [(outward, True), (local, False)])
    n.silenced = True
    problem = {"blocked:companion_a:auth_invalid": "companion_a auth_invalid"}
    await n.update(problem)
    assert n.undelivered == 1 and not outward.calls and len(local.calls) == 1
    clock.advance(599)
    n = Notifications(tmp_path, clock, log, audit, [(local, False)])
    await n.update(problem)
    assert len(local.calls) == 1
    clock.advance(1)
    local.sent = True
    await n.update(problem)
    assert len(local.calls) == 2 and n.undelivered == 0
    n = Notifications(tmp_path, clock, log, audit, [(local, False)])
    clock.advance(6 * 3600 - 1)
    await n.update(problem)
    assert len(local.calls) == 2
    clock.advance(1)
    await n.update(problem)
    assert len(local.calls) == 3


async def test_notification_restart_initialization_keeps_retry_deadline(tmp_path):
    clock, log = MockClock(), HostLog()
    audit = Audit(tmp_path, clock)
    notifier = FakeNotifier(clock, False)
    problem = {"auth_invalid:companion_a:fake-model": "companion_a fake-model"}
    first = Notifications(tmp_path, clock, log, audit, [(notifier, False)])
    await first.update(problem)
    clock.advance(100)
    second = Notifications(tmp_path, clock, log, audit, [(notifier, False)])
    await second.update({}, pending={"companion_a"})
    assert second.undelivered == 1
    await second.update(problem)
    assert len(notifier.calls) == 1
    clock.advance(499)
    await second.update(problem)
    assert len(notifier.calls) == 1
    clock.advance(1)
    await second.update(problem)
    assert len(notifier.calls) == 2
    await second.update({})
    assert second.undelivered == 0


async def test_notifiers_prepared_before_any_child_or_notification_tick(tmp_path):
    from clytie.supervisor.service import Supervisor

    from .test_supervisor import FakeLauncher, spin

    clock, launcher = MockClock(), FakeLauncher()
    svc = Supervisor(tmp_path, ["companion_a"], launcher, clock)
    notifier = FakeNotifier(clock)

    async def prepare():
        assert not launcher.children and svc.ticker is None
        assert svc.lock is not None
        svc.notifications.plugins = [(notifier, False)]
        svc.problems["blocked:companion_a:config_invalid"] = "companion_a config_invalid"

    await svc.start(prepare)
    await spin()
    assert len(notifier.calls) == 1
    assert svc.notifications.undelivered == 0
    await svc.stop()


async def test_instance_context_and_desktop_fake_backend(tmp_path):
    h = host(tmp_path)

    class Backend:
        async def run(self, args, **kwargs):
            assert kwargs["env"]["CLYTIE_NOTICE_BODY"] == "companion_a auth_invalid"
            return ProcessResult(returncode=0, stdout=b"", stderr=b"", truncated=False)

    assembly = InstanceNotifiers(h, subprocess_backend=Backend())
    await assembly.start(["desktop-notifier"])
    assert len(assembly.notifiers) == 1
    assert assembly.by_id["desktop-notifier"] == assembly.notifiers[0]
    ctx = assembly.runtimes[0].ctx
    assert set(dir(ctx)) == {"config", "clock", "log", "subprocess"}
    for forbidden in ("companion_id", "memory", "ledger", "storage", "random"):
        assert not hasattr(ctx, forbidden)
    n = Notifications(tmp_path, h.clock, h.log, Audit(tmp_path, h.clock), assembly.notifiers)
    await n.update({"auth_invalid:companion_a:fake-model": "companion_a auth_invalid"})
    assert n.undelivered == 0
    await assembly.close()
    await h.close()


async def test_channel_auth_invalid_only_blocks_affected_channel(tmp_path):
    kernel, channel, clock = setup(tmp_path)
    record, _, intents = await task_with_intents(kernel, split=True)
    calls = []

    async def invalid(intent, cancel):
        calls.append(intent.intent_id)
        return Receipt(
            intent_id=intent.intent_id,
            status="failed",
            at=clock.now(),
            error={"code": "auth_invalid", "message": "fictional credential"},
        )

    channel.send = invalid
    await kernel.delivery.deliver(record["trace_id"])
    assert len(calls) == 1
    assert kernel.delivery.channel_errors[intents[0].channel_id] == "auth_invalid"
    assert "fictional credential" not in str(kernel.delivery.channel_errors)
    from .test_kernel_service import new_record, reply

    other, _, _ = await new_record(kernel, name="other-request")
    other_intent = reply(other).model_copy(update={"channel_id": "another-fictional-account"})
    kernel.delivery.channels["another-fictional-account"] = channel
    await kernel.seal(other["trace_id"], "ok", [other_intent])

    async def valid(intent, cancel):
        return Receipt(intent_id=intent.intent_id, status="sent", at=clock.now())

    channel.send = valid
    await kernel.delivery.deliver(other["trace_id"])
    assert kernel.ledger.get("intent", other_intent.intent_id)["status"] == "sent"
    await settle(kernel)
    kernel.ledger.close()


async def test_companion_stop_shared_cleanup_and_model_never_called(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from clytie.supervisor.companion import Companion

    a, channel, model = await make_agent(tmp_path)
    runtime = Companion(tmp_path, a.cid, a.clock, host=a.host)
    runtime.agent, runtime.kernel = a, a.kernel
    runtime.runtime = SimpleNamespace(close=a.ledger.close)
    calls = []
    original = runtime.host.close
    entered = asyncio.Event()

    async def subscriber(event):
        entered.set()
        await a.clock.sleep(100)

    a.kernel.bus.subscribers = lambda cid, event: (subscriber,)
    a.kernel.bus.publish(a.cid, "fictional_shutdown_event")
    await entered.wait()
    worker = a.kernel.bus.workers[a.cid]
    entered.clear()
    a.host.event_bus.subscribers = lambda cid, event: (subscriber,)
    a.host.event_bus.publish(a.cid, "fictional_host_event")
    await entered.wait()
    host_worker = a.host.event_bus.workers[a.cid]

    async def close():
        calls.append("close")
        return await original()

    runtime.host.close = close
    first, second = await asyncio.gather(runtime.stop(), runtime.stop())
    assert first == second and first["state"] == "stopped"
    assert calls == ["close"] and model.requests == []
    assert worker.cancelled() and a.kernel.bus.closed
    assert host_worker.cancelled() and a.host.event_bus.closed
    assert not a.kernel.bus.publish(a.cid, "after_shutdown")


async def test_companion_stop_flushes_pending_terminal_reaction(tmp_path):
    from types import SimpleNamespace

    from clytie.supervisor.companion import Companion

    from .test_agent_acceptance import ECHO

    agent, channel, _ = await make_agent(
        tmp_path,
        ECHO,
        {"finish": "stop", "text": "fictional final reply"},
        changes={"task_reactions": {"min_interval_seconds": 2}},
    )
    runtime = Companion(tmp_path, agent.cid, agent.clock, host=agent.host)
    runtime.agent, runtime.kernel = agent, agent.kernel
    runtime.runtime = SimpleNamespace(close=agent.ledger.close)
    trace = await agent.receive(event(agent))
    await agent.clock.drain(500)
    try:
        assert channel.sent and channel.sent[-1].parts[0].text == "fictional final reply"
        assert [intent.emoji for intent in channel.reacts] == ["👌"]
        assert agent.kernel.reactions.pending[trace] == "done"
    finally:
        result = await runtime.stop()
    assert result["state"] == "stopped"
    assert agent.clock.monotonic() == 0
    assert [intent.emoji for intent in channel.reacts] == ["👌", "✅"]
    assert not agent.kernel.reactions.workers
    assert not agent.kernel.reactions.last_sent


async def test_health_counts_without_loading_ledger_bodies(tmp_path, monkeypatch):
    from clytie.supervisor.companion import Companion

    a, _, _ = await make_agent(tmp_path)
    runtime = Companion(tmp_path, a.cid, a.clock, host=a.host)
    runtime.agent, runtime.kernel = a, a.kernel
    with a.ledger.transaction():
        for n, state in enumerate(("pending", "running", "ok", "cancelled", "interrupted")):
            a.ledger.put("record", f"fake_{n}", {"kind": "task", "exec_status": state})
        a.ledger.put("record", "fake_chat", {"kind": "chat", "exec_status": "running"})
        for n, state in enumerate(("sending", "pending", "unknown")):
            a.ledger.put("intent", f"fake_{n}", {"status": state})
    with monkeypatch.context() as patch:
        patch.setattr(a.ledger, "rows", lambda _: pytest.fail("health loaded record bodies"))
        await runtime.refresh_health()
        health = runtime.health()
    assert health["inflight_tasks"] == 2 and health["sending"] == 1
    # Both predicates have reusable indexes; routine heartbeats do no JSON decoding.
    indexes = {r[1] for r in a.ledger.db.execute("PRAGMA index_list(record)")}
    assert "health_tasks" in indexes
    await finish(a)


async def test_session_ending_blocks_all_split_remainders(tmp_path):
    kernel, channel, clock = setup(tmp_path)
    record, _, intents = await task_with_intents(kernel, split=True)
    original = channel.send

    async def send(intent, cancel):
        result = await original(intent, cancel)
        kernel.delivery.session_ending = True
        return result

    channel.send = send
    await kernel.delivery.deliver(record["trace_id"])
    assert len(channel.text) == 1
    assert all(
        kernel.ledger.get("intent", i.intent_id)["error"]["code"] == "shutdown_before_send"
        for i in intents[1:]
    )
    await settle(kernel)
    kernel.ledger.close()


@pytest.mark.parametrize("mode", ["cancel", "wait", "escalate"])
async def test_companion_running_model_shutdown_modes(tmp_path, mode):
    import asyncio
    from types import SimpleNamespace

    from clytie.sdk.types import ModelTurn
    from clytie.supervisor.companion import Companion

    started, release = asyncio.Event(), asyncio.Event()

    async def generate(request, cancel):
        started.set()
        if mode == "wait":
            await release.wait()
            return ModelTurn(finish="stop", text="fictional finished", usage={})
        await cancel.wait()
        cancel.raise_if_cancelled()

    a, channel, model = await make_agent(tmp_path, generate)
    await a.receive(event(a))
    await started.wait()
    runtime = Companion(tmp_path, a.cid, a.clock, host=a.host)
    runtime.agent, runtime.kernel = a, a.kernel
    runtime.runtime = SimpleNamespace(close=lambda: None)
    closing = asyncio.create_task(runtime.stop("wait" if mode != "cancel" else "cancel"))
    await a.clock.drain()
    ignored = event(a)
    await a.receive(ignored)
    assert a.ledger.get("inbound", ignored.event_id)["reason"] == "shutting_down"
    if mode == "wait":
        assert not closing.done()
        release.set()
    elif mode == "escalate":
        assert not closing.done()
        runtime.escalated.set()
    for _ in range(30):
        await a.clock.advance_and_drain(1)
        if closing.done():
            break
    result = await closing
    assert result["state"] == "stopped"
    records = a.ledger.rows("record")
    assert all(r["exec_status"] == ("ok" if mode == "wait" else "cancelled") for r in records)
    assert len(model.requests) == 1
    if mode != "wait":
        assert not channel.sent
    a.ledger.close()


async def test_shutdown_finishes_started_split_and_fails_unstarted(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from clytie.supervisor.companion import Companion

    from .test_kernel_service import new_record, reply

    a, channel, model = await make_agent(tmp_path)
    record, _, intents = await task_with_intents(a.kernel, split=True, spaced=True)
    delivery = asyncio.create_task(a.kernel.delivery.deliver(record["trace_id"]))
    await a.clock.drain()
    assert len(channel.sent) == 1
    other, _, _ = await new_record(a.kernel, name="unstarted")
    extra = reply(other)
    await a.kernel.seal(other["trace_id"], "ok", [extra])
    runtime = Companion(tmp_path, a.cid, a.clock, host=a.host)
    runtime.agent, runtime.kernel = a, a.kernel
    runtime.runtime = SimpleNamespace(close=lambda: None)
    stopping = asyncio.create_task(runtime.stop())
    await a.clock.drain()
    assert not stopping.done()
    assert a.ledger.get("intent", extra.intent_id)["error"]["code"] == "shutdown_before_send"
    for _ in range(10):
        await a.clock.advance_and_drain(1)
    assert (await stopping)["state"] == "stopped"
    await delivery
    assert len(channel.sent) == 3
    assert all(a.ledger.get("intent", i.intent_id)["status"] == "sent" for i in intents)
    a.ledger.close()


async def test_silence_during_task_keeps_model_loop_and_internal_recollection(tmp_path):
    import asyncio

    from clytie.memory.scheduler import RecollectionModel
    from clytie.sdk import CancelToken
    from clytie.sdk.testing.samples import sample_request
    from clytie.sdk.types import ModelTurn

    entered, release = asyncio.Event(), asyncio.Event()

    async def first(request, cancel):
        entered.set()
        await release.wait()
        return ModelTurn(
            finish="tool_calls",
            usage={},
            tool_calls=[{"call_id": "call_a", "tool": "echo", "arguments": {"text": "fictional"}}],
        )

    a, channel, model = await make_agent(
        tmp_path, first, {"finish": "stop", "text": "fictional done"}
    )
    try:
        trace = await a.receive(event(a))
        await entered.wait()
        a.kernel.delivery.silenced = True
        release.set()
        await a.drain()
        assert len(model.requests) == 2
        assert a.ledger.get("record", trace)["exec_status"] == "ok"
        intents = a.ledger.rows("intent")
        assert intents and all(i["error"]["code"] == "silenced" for i in intents)
        assert not channel.sent
        result = await RecollectionModel(a).generate(
            sample_request(a.clock), CancelToken(), purpose="test"
        )
        assert result.finish == "stop" and len(model.requests) == 3
        a.model_health = "auth_invalid"
        await RecollectionModel(a).generate(sample_request(a.clock), CancelToken(), purpose="test")
        assert len(model.requests) == 3
    finally:
        await finish(a)
