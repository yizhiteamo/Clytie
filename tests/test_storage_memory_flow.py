"""记忆上下文、跨库回想与在场租约的真实存储线程回归。"""

import asyncio
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from clytie.backup.coordination import CaptureCoordinator, run_sync
from clytie.kernel.ledger import Ledger
from clytie.memory.context import MemoryContext
from clytie.memory.presence import bind_presence
from clytie.memory.recollection import RecollectionCommit, SnapshotReader
from clytie.memory.retrieval import CandidateReader
from clytie.memory.scheduler import Recollector
from clytie.memory.store import MemoryStore
from clytie.plugin_host.support import HostLog
from clytie.sdk import CancelToken
from clytie.sdk.testing import MockClock
from clytie.sdk.types import RecollectItem, RecollectMessage, RecollectWindow, ToolCall
from clytie.storage.handle import StoreHandle
from clytie.storage.owner import StorageOwner

from .test_agent import event


@pytest.fixture
async def flow(tmp_path):
    clock = MockClock()
    config = {"memory": {"recollect": {"idle_minutes": 1}}}
    instance = {
        "conversations": {
            cid: {"scene": scene, "kind": kind, "channel": "fake", "platform_id": "fake-" + cid}
            for cid, scene, kind in (
                ("owner_private", "owner_private", "private"),
                ("test_group", "public_group", "group"),
            )
        }
    }
    gate_events = []

    class Coordinator(CaptureCoordinator):
        @contextmanager
        def _lock(self):
            with super()._lock():
                gate_events.append("enter")
                try:
                    yield
                finally:
                    gate_events.append("exit")

    coordinator = Coordinator(tmp_path / "capture.lock", clock)

    class Resources:
        def __init__(self):
            self.memory = MemoryStore(
                tmp_path / "memory.db",
                clock=clock,
                config=config,
                instance=instance,
                coordination=coordinator,
            )
            self.ledger = Ledger(
                tmp_path / "ledger.db", "companion_a", clock, coordination=coordinator
            )

        def close(self):
            self.memory.close()
            self.ledger.close()

    owner = await StorageOwner.start(Resources, companion_id="companion_a", clock=clock)
    metadata = {"clock": clock, "config": config, "instance": instance, "coordination": coordinator}
    store = StoreHandle(owner, "memory", MemoryStore, metadata)
    ledger = StoreHandle(owner, "ledger", Ledger, {**metadata, "companion_id": "companion_a"})
    changed = []
    context = MemoryContext(
        store, log=HostLog(), ledger=ledger, changed=lambda: changed.append(threading.get_ident())
    )
    agent = SimpleNamespace(
        cid="companion_a",
        clock=clock,
        config=config,
        instance=instance,
        memory=context,
        ledger=ledger,
        log=HostLog(),
    )
    registration = SimpleNamespace(
        plugin_id="default-consolidator", point="memory.consolidator", key="default"
    )
    main_thread = threading.get_ident()

    class Host:
        before_result = None
        items = [RecollectItem(kind="episode", content="虚构的纸船经历", evidence=[1])]
        windows = []

        def registrations(self, *args):
            return [registration]

        async def invoke(self, *args, **kwargs):
            assert threading.get_ident() == main_thread
            self.windows.append(args[4])
            if self.before_result:
                await self.before_result(args[6])
            return self.items

    agent.host = Host()
    try:
        yield SimpleNamespace(
            agent=agent, owner=owner, store=store, ledger=ledger, changed=changed, gates=gate_events
        )
    finally:
        await owner.close()


async def test_core_write_recall_guard_and_audit_share_owned_boundary(flow):
    agent = flow.agent
    incoming = event(agent, "虚构私密的长句纸船故事")
    call = ToolCall(
        call_id="remember",
        tool="core_remember",
        arguments={"kind": "preference", "content": "虚构私密的长句纸船故事"},
    )
    before = len(flow.gates)
    result = await agent.memory.execute(
        "trace", incoming.conversation, incoming.message.sender, call, CancelToken()
    )
    assert result.status == "ok"
    assert flow.gates[before:] == ["enter", "exit"]
    assert flow.changed == [threading.get_ident()]
    records = await run_sync(flow.store, flow.store.all_records)
    assert len(records) == 1
    audits = await run_sync(flow.ledger, flow.ledger.rows, "audit")
    assert [row["tool"] for row in audits if row["kind"] == "core.tool"] == ["core_remember"]
    assert await agent.memory.guard(records[0].content, "public_group")
    assert not await agent.memory.guard(records[0].content, "owner_private")
    assert "虚构" in str(await agent.memory.build("trace", incoming.conversation, "纸船"))
    recall = ToolCall(call_id="recall", tool="core_recall", arguments={"query": "纸船"})
    assert (
        await agent.memory.execute(
            "trace", incoming.conversation, incoming.message.sender, recall, CancelToken()
        )
    ).status == "ok"


async def test_recollector_owned_snapshot_and_atomic_progress(flow):
    agent = flow.agent
    collector = Recollector(agent)
    await collector.initialize()
    incoming = event(agent, "一起折虚构纸船")
    await run_sync(flow.ledger, flow.ledger.claim_inbound, incoming)
    agent.clock.advance(60)
    result = await collector.process(incoming.conversation)
    assert result["written"] == 1
    progress, records = await run_sync(
        flow.store, lambda: (collector.progress("owner_private"), flow.store.all_records())
    )
    assert progress["inbound"] > 0
    assert [r.content for r in records] == ["虚构的纸船经历"]
    assert agent.host.windows[0].messages[0].text == "一起折虚构纸船"
    assert await collector.process(incoming.conversation) is None


async def test_recollector_late_cancel_does_not_commit_records_or_advance_window(flow):
    agent = flow.agent
    collector = Recollector(agent)
    await collector.initialize()
    incoming = event(agent, "虚构的待取消窗口")
    await run_sync(flow.ledger, flow.ledger.claim_inbound, incoming)
    agent.clock.advance(60)

    async def cancel_late(token):
        token.cancel("fictional_cancel")

    agent.host.before_result = cancel_late
    assert await collector.process(incoming.conversation) is None
    progress, records = await run_sync(
        flow.store, lambda: (collector.progress("owner_private"), flow.store.all_records())
    )
    assert not records
    assert progress["inbound"] == 0
    assert progress["count"] == 1


async def test_queued_profile_read_does_not_extend_expired_presence_lease(flow):
    window = RecollectWindow(
        scene="public_group",
        conversation_kind="group",
        messages=[
            RecollectMessage(number=1, speaker="speaker_1", role="member", text="喜欢虚构纸船")
        ],
    )
    item = RecollectItem(kind="profile", content="喜欢虚构纸船", evidence=[1], about="speaker_1")
    await run_sync(
        flow.store,
        lambda: RecollectionCommit(flow.store).commit(
            [item],
            window,
            SnapshotReader([]),
            cid="test_group",
            actors={"speaker_1": "member_a"},
            trace="profile",
            progress={},
            cancel=CancelToken(),
        ),
    )
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def block(*_):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)

    blocking = flow.owner.submit(block)
    await entered.wait()
    try:
        with bind_presence("test_group", {"member_a"}):
            task = asyncio.create_task(
                CandidateReader(flow.store, "public_group", "test_group").count()
            )
            await flow.agent.clock.drain()
        release.set()
        await blocking.result()
        assert await task == 0
    finally:
        release.set()


@pytest.mark.parametrize("kind", ["profile", "jargon"])
async def test_owned_core_replace_preserves_special_kind_and_checks_revision(flow, kind):
    actor_id = "member_a" if kind == "profile" else "owner"
    window = RecollectWindow(
        scene="public_group",
        conversation_kind="group",
        messages=[
            RecollectMessage(
                number=1,
                speaker="speaker_1",
                role="member" if kind == "profile" else "owner",
                text="虚构纸船",
            )
        ],
    )
    item = RecollectItem(
        kind=kind,
        content="虚构纸船",
        evidence=[1],
        about="speaker_1" if kind == "profile" else None,
    )
    await run_sync(
        flow.store,
        lambda: RecollectionCommit(flow.store).commit(
            [item],
            window,
            SnapshotReader([]),
            cid="test_group",
            actors={"speaker_1": actor_id},
            trace="initial",
            progress={},
            cancel=CancelToken(),
        ),
    )
    original = (await run_sync(flow.store, flow.store.all_records))[0]
    agent = flow.agent
    incoming = event(agent)
    await agent.memory.build("replace", incoming.conversation, "纸船")
    call = ToolCall(
        call_id="replace",
        tool="core_remember",
        arguments={"kind": "fact", "content": "修订的虚构纸船", "replaces": "[记忆1]"},
    )
    result = await agent.memory.execute(
        "replace", incoming.conversation, incoming.message.sender, call, CancelToken()
    )
    assert result.status == "ok"
    revised = await run_sync(flow.store, flow.store.get_raw, original.record_id)
    assert (revised.kind, revised.visibility, revised.revision, revised.basis) == (
        kind,
        original.visibility,
        2,
        "owner_stated",
    )
    conflict = await agent.memory.execute(
        "replace", incoming.conversation, incoming.message.sender, call, CancelToken()
    )
    assert conflict.error.code == "revision_conflict"
    await agent.memory.build("forget", incoming.conversation, "纸船")
    forget = ToolCall(
        call_id="forget", tool="core_forget", arguments={"target": "[记忆1]", "reason": "虚构撤销"}
    )
    assert (
        await agent.memory.execute(
            "forget", incoming.conversation, incoming.message.sender, forget, CancelToken()
        )
    ).status == "ok"
    assert (await run_sync(flow.store, flow.store.get_raw, original.record_id)).retracted


async def test_window_snapshot_stays_fixed_while_external_model_allows_new_writes(flow):
    agent = flow.agent
    collector = Recollector(agent)
    await collector.initialize()
    first = event(agent, "第一段虚构窗口")
    await run_sync(flow.ledger, flow.ledger.claim_inbound, first)
    agent.clock.advance(60)

    async def later_message(token):
        await run_sync(flow.ledger, flow.ledger.claim_inbound, event(agent, "第二段虚构窗口"))

    agent.host.before_result = later_message
    result = await collector.process(first.conversation)
    assert result["inbound"] == 1
    assert [m.text for m in agent.host.windows[0].messages] == ["第一段虚构窗口"]
    assert len(await run_sync(flow.ledger, flow.ledger.rows, "inbound")) == 2


async def test_window_reply_evidence_survives_owned_snapshot_transport(flow):
    agent = flow.agent
    previous = event(agent, "基线之前的虚构主人消息", scene="public_group")
    await run_sync(flow.ledger, flow.ledger.claim_inbound, previous)
    collector = Recollector(agent)
    await collector.initialize()
    reply = event(
        agent,
        "基线之后的虚构回复",
        role="member",
        scene="public_group",
        reply_to=previous.message.ref,
    )
    await run_sync(flow.ledger, flow.ledger.claim_inbound, reply)
    agent.clock.advance(60)
    result = await collector.process(reply.conversation)
    assert result["written"] == 1
    record = (await run_sync(flow.store, flow.store.all_records))[0]
    assert "core:with_owner" in record.tags
    assert len(agent.host.windows) == 1


async def test_recollection_save_failure_rolls_back_memory_and_window_progress(flow):
    agent = flow.agent
    collector = Recollector(agent)
    await collector.initialize()
    incoming = event(agent, "虚构事务失败窗口")
    await run_sync(flow.ledger, flow.ledger.claim_inbound, incoming)
    agent.clock.advance(60)

    def install_failure(resources, command):
        original = resources.memory._save

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("fictional_failure_before_progress")

        resources.memory._save = fail

    await flow.owner.execute(install_failure)
    with pytest.raises(RuntimeError, match="fictional_failure_before_progress"):
        await collector.process(incoming.conversation)
    records, progress = await run_sync(
        flow.store, lambda: (flow.store.all_records(), collector.progress("owner_private"))
    )
    assert not records
    assert progress["inbound"] == 0 and progress["count"] == 1


async def test_retracted_comparison_reads_owned_cache_without_model_for_original(flow):
    from clytie.memory.retracted import RetractedGuard
    from clytie.memory.retrieval import Retrieval

    store = flow.store
    original = await run_sync(
        store,
        store.write,
        {"kind": "fact", "content": "虚构原内容", "visibility": "private"},
        scene="owner_private",
        conversation_id="owner_private",
        operator="owner",
        owner="owner",
    )
    calls = []
    loop_thread = threading.get_ident()

    class Embedder:
        async def embed(self, texts, cancel):
            assert threading.get_ident() == loop_thread
            calls.extend(texts)
            return [[1.0, 0.0] for _ in texts]

    retrieval = Retrieval(store, embedder=Embedder(), embedder_id="fake")
    await retrieval.rebuild()
    retracted = await run_sync(
        store,
        store.revise,
        original.record_id,
        1,
        {"retracted": True},
        scene="owner_private",
        conversation_id="owner_private",
        operator="owner",
    )
    calls.clear()
    candidate = RecollectItem(kind="episode", content="虚构的近义表达", evidence=[1])
    assert await RetractedGuard(store, retrieval, HostLog()).rejected(
        [candidate], [retracted], CancelToken()
    ) == {0}
    assert calls == [candidate.content]
    assert await run_sync(store, store.meta, "recollect_semantic_failures", 0) == 0


async def test_queued_core_replace_keeps_original_expected_revision(flow):
    agent = flow.agent
    incoming = event(agent)
    store = flow.store
    record = await run_sync(
        store,
        store.write,
        {"kind": "preference", "content": "虚构初始纸船", "visibility": "private"},
        scene="owner_private",
        conversation_id="owner_private",
        owner="owner",
        operator="owner",
    )
    await agent.memory.build("queued", incoming.conversation, "纸船")
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def earlier_write(resources, command):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        resources.memory.revise(
            record.record_id,
            1,
            {"content": "虚构先提交修订"},
            scene="owner_private",
            conversation_id="owner_private",
            operator="owner",
        )

    before = flow.owner.submit(earlier_write)
    await entered.wait()
    call = ToolCall(
        call_id="queued",
        tool="core_remember",
        arguments={"kind": "preference", "content": "不应提交的虚构修订", "replaces": "[记忆1]"},
    )
    task = asyncio.create_task(
        agent.memory.execute(
            "queued", incoming.conversation, incoming.message.sender, call, CancelToken()
        )
    )
    try:
        await agent.clock.drain()
        agent.memory.session("queued").references["[记忆1]"] = (record.record_id, 2)
        release.set()
        await before.result()
        assert (await task).error.code == "revision_conflict"
        assert (await run_sync(store, store.get_raw, record.record_id)).revision == 2
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
