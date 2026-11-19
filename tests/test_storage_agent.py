import asyncio
import logging
import threading
from types import SimpleNamespace

import pytest

from clytie.agent.context import Context
from clytie.agent.policy import Boundary
from clytie.backup.coordination import run_sync
from clytie.kernel.ledger import Ledger
from clytie.memory.store import MemoryStore
from clytie.sdk import CancelToken
from clytie.sdk.testing import MockClock
from clytie.sdk.types import Actor, Conversation
from clytie.storage.handle import StoreHandle
from clytie.storage.owner import StorageOwner


class Resources:
    def __init__(self, path, clock):
        self.ledger = Ledger(path / "ledger.sqlite3", "companion_a", clock)
        self.memory = MemoryStore(path / "memory.sqlite3", clock=clock, config={}, instance={})

    def close(self):
        self.memory.close()
        self.ledger.close()


@pytest.fixture
async def storage(tmp_path):
    clock = MockClock()
    owner = await StorageOwner.start(
        lambda: Resources(tmp_path, clock), companion_id="companion_a", clock=clock
    )
    ledger = StoreHandle(owner, "ledger", Ledger, {"clock": clock, "companion_id": "companion_a"})
    try:
        yield owner, ledger, clock
    finally:
        await owner.close()


async def test_boundary_notification_stays_on_loop_and_state_is_snapshot(storage, tmp_path):
    owner, ledger, clock = storage
    loop_thread = threading.get_ident()
    notified = []

    async def notify(notice, token):
        assert threading.get_ident() == loop_thread
        notified.append(notice)

    boundary = Boundary(
        tmp_path / "boundary.json",
        {"refusal_threshold": 1},
        clock,
        ledger,
        logging.getLogger(__name__),
        SimpleNamespace(notify=notify),
    )
    actor = Actor(actor_id="member_a", role="member", channel_id="fake")
    await boundary.report("trace_a", actor, "romance", CancelToken())
    await boundary.drain_notifications()
    assert len(notified) == 1
    assert boundary.paused(actor)
    state = boundary.state
    state["actors"].clear()
    assert boundary.paused(actor)
    assert len(await run_sync(ledger, ledger.rows, "audit")) == 2


async def test_context_build_uses_owner_snapshot_and_calls_host_on_loop(storage):
    owner, ledger, clock = storage
    loop_thread = threading.get_ident()

    async def sections(*args, **kwargs):
        assert threading.get_ident() == loop_thread
        return []

    async def memory(*args):
        assert threading.get_ident() == loop_thread
        return []

    agent = SimpleNamespace(
        features=SimpleNamespace(supports_images=False),
        ledger=ledger,
        clock=clock,
        config={},
        cid="companion_a",
        persona=SimpleNamespace(render=lambda _: "fictional persona"),
        host=SimpleNamespace(context_sections=sections),
        memory_context=memory,
        boundary=SimpleNamespace(reminder=lambda _: None),
    )
    context = Context(agent)
    conversation = Conversation(
        conversation_id="test_group", channel_id="fake", kind="group", scene="public_group"
    )
    actor = Actor(actor_id="member_a", role="member", channel_id="fake")
    messages = await context.build(conversation, actor, [], "")
    assert any("最近任务：[]" in part.text for message in messages for part in message.parts)


async def model_agent(storage):
    from clytie.agent.service import Agent

    owner, ledger, clock = storage
    conversation = Conversation(
        conversation_id="test_group", channel_id="fake", kind="group", scene="public_group"
    )
    record = await run_sync(ledger, ledger.open_record, conversation=conversation)
    agent = Agent.__new__(Agent)
    agent.ledger, agent.clock, agent.config = ledger, clock, {"model": {"plugin": "fake-model"}}
    agent.protection = SimpleNamespace(update=lambda _: None)
    agent.background = set()
    agent.kernel = SimpleNamespace(
        grace=1,
        finalizations={},
        delivery=SimpleNamespace(ledger=ledger, background=agent.background),
    )
    return agent, conversation, record["trace_id"]


async def test_model_does_not_start_without_finalizer_capacity(storage):
    from clytie.sdk.types import ModelTurn
    from clytie.storage.owner import StorageBusy

    owner, ledger, clock = storage
    agent, conversation, trace = await model_agent(storage)
    started = []

    async def model(*args, **kwargs):
        started.append(True)
        return ModelTurn(finish="stop", text="fictional reply", usage={})

    agent.model_call = model
    reserved = [owner.reserve_finalizer() for _ in range(16)]
    try:
        with pytest.raises(StorageBusy):
            await agent.generate(trace, [], [], conversation, CancelToken())
        assert not started
    finally:
        for token in reserved:
            owner.release_finalizer(token, executor_finished=True, obligations_complete=True)


async def test_late_model_keeps_finalizer_until_audit_under_full_queue(storage):
    from clytie.sdk.types import ModelTurn

    owner, ledger, clock = storage
    agent, conversation, trace = await model_agent(storage)
    started, release_model = asyncio.Event(), asyncio.Event()

    async def model(*args, **kwargs):
        started.set()
        while not release_model.is_set():
            try:
                await release_model.wait()
            except asyncio.CancelledError:
                pass
        return ModelTurn(finish="stop", text="fictional late reply", usage={})

    agent.model_call = model
    running = asyncio.create_task(agent.generate(trace, [], [], conversation, CancelToken()))
    await started.wait()
    assert owner.status()["finalizers"] == 1
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert owner.status()["finalizers"] == 1
    release_storage = threading.Event()
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()

    def block(*_):
        loop.call_soon_threadsafe(entered.set)
        assert release_storage.wait(10)

    queued = [owner.submit(block)]
    await entered.wait()
    queued.extend(owner.submit(lambda *_: None) for _ in range(63))
    try:
        release_model.set()
        await clock.drain()
        assert owner.status()["finalizers"] == 1
    finally:
        release_storage.set()
        await asyncio.gather(*(command.result() for command in queued))
        await asyncio.gather(*tuple(agent.background))
    assert owner.status()["finalizers"] == 0
    audits = await run_sync(ledger, ledger.rows, "audit")
    usage = [row for row in audits if row["kind"] == "model.usage"]
    assert len(usage) == 1
    assert usage[0]["plugin"] == "fake-model" and usage[0]["timing"] == "late"
    assert usage[0]["input_tokens"] is None and usage[0]["output_tokens"] is None
    assert [row["content"]["text"] for row in audits if row["kind"] == "model.late_result"] == [
        "fictional late reply"
    ]


async def test_cancel_during_model_registration_waits_for_storage_before_release(
    storage, monkeypatch
):
    owner, ledger, clock = storage
    agent, conversation, trace = await model_agent(storage)
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    started = []
    original_put = Ledger.put

    def put(self, table, key, value):
        if table == "record" and value.get("model_calls"):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10)
        return original_put(self, table, key, value)

    async def model(*args, **kwargs):
        started.append(True)
        raise AssertionError("cancelled registration cannot start model")

    monkeypatch.setattr(Ledger, "put", put)
    agent.model_call = model
    running = asyncio.create_task(agent.generate(trace, [], [], conversation, CancelToken()))
    await entered.wait()
    try:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert owner.status()["finalizers"] == 1
        assert not started
    finally:
        release.set()
        await asyncio.gather(*tuple(agent.background))
    assert owner.status()["finalizers"] == 0
    assert not started
    assert (await run_sync(ledger, ledger.get, "record", trace))["model_calls"] == 1
