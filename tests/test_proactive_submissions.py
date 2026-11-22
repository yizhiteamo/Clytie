"""主动提交的会话顺序、交付结果和伴侣启动恢复，全部使用虚构输入。"""

import asyncio
import shutil
import sqlite3
from contextlib import closing, contextmanager

import pytest
import yaml

from clytie.backup.coordination import run_sync
from clytie.clock import SystemClock
from clytie.kernel.ledger import Ledger
from clytie.plugin_host.support import timed
from clytie.sdk.testing import FakeChannel, MockClock
from clytie.sdk.types import ModelTurn, Receipt, StickerPart
from clytie.supervisor.companion import Companion

from .host_helpers import plugin
from .test_agent import event, finish, make_agent
from .test_agent_acceptance import ECHO
from .test_agent_judgment import group
from .test_agent_prewrite import draft, timing_agent
from .test_config import write_yaml
from .test_storage_agent_lifecycle import private_home

SUBMITTER = "fictional-sender"
OTHER_PLUGIN = "fictional-observer"
OWNER = "owner_private"


class HeldTurn:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, request, cancel):
        self.entered.set()
        await self.release.wait()
        return ModelTurn(text="fictional answer", finish="stop", usage={})


async def wait_for_gate(gate):
    await timed(gate.wait(), SystemClock(), 5)


async def submit(agent, cid=OWNER, *, tools=False):
    return await agent.submit(
        cid, topic="fictional topic", reason="sample", plugin_id=SUBMITTER, tools=tools
    )


async def assert_busy(agent, model, cid=OWNER):
    records = agent.ledger.rows("record")
    calls = len(model.requests)
    audits = len(agent.ledger.rows("audit"))
    refused = await submit(agent, cid)
    assert refused.model_dump() == {"status": "busy", "trace_id": None}
    assert agent.ledger.rows("record") == records
    assert len(model.requests) == calls
    (audit,) = [
        row for row in agent.ledger.rows("audit")[audits:] if row["kind"] == "proactive.busy"
    ]
    assert (audit["plugin"], audit["reason"], audit["trace_id"]) == (SUBMITTER, "sample", None)


async def assert_outcome(agent, trace, status, *, sent_at=None, plugin_id=SUBMITTER):
    outcome = await agent.proactive_result(plugin_id, trace)
    assert outcome.model_dump() == {"status": status, "sent_at": sent_at}


@pytest.mark.parametrize("prewriting", [False, True], ids=["waiting", "prewriting"])
async def test_busy_during_unopened_batch(tmp_path, prewriting):
    held = HeldTurn()
    agent, _, model = await timing_agent(tmp_path, held)
    item = group(agent, "fictional group message", mention=prewriting)
    try:
        assert await agent.receive(item) is None
        if prewriting:
            await wait_for_gate(held.entered)
            assert not draft(agent, item).task.done()
        assert len(model.requests) == int(prewriting)
        assert not agent.ledger.rows("record")
        await assert_busy(agent, model, "test_group")
    finally:
        held.release.set()
        if prewriting and held.entered.is_set():
            await draft(agent, item).task
        await agent.clock.advance_and_drain(30)
        await finish(agent)


async def test_busy_while_timing_plugin_waits_for_judgment(tmp_path):
    judgment = HeldTurn()
    agent, _, model = await timing_agent(
        tmp_path, {"finish": "stop", "text": "fictional draft"}, judgment
    )
    item = group(agent, "companion hello")
    try:
        await agent.receive(item)
        await draft(agent, item).task
        await agent.clock.advance_and_drain(3)
        await wait_for_gate(judgment.entered)
        assert len(model.requests) == 2 and not agent.ledger.rows("record")
        await assert_busy(agent, model, "test_group")
    finally:
        judgment.release.set()
        await agent.clock.drain()
        await finish(agent)


async def test_busy_between_respond_decision_and_record_creation(tmp_path, monkeypatch):
    agent, channel, model = await make_agent(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    open_record = agent.kernel.open_record

    async def hold_reply(**options):
        if options.get("event_id"):
            entered.set()
            await release.wait()
        return await open_record(**options)

    monkeypatch.setattr(agent.kernel, "open_record", hold_reply)
    inbound = asyncio.create_task(agent.receive(event(agent)))
    try:
        await wait_for_gate(entered)
        assert not agent.ledger.rows("record") and not model.requests
        await assert_busy(agent, model)
        release.set()
        trace = await inbound
        await agent.drain()
        assert [intent.trace_id for intent in channel.sent] == [trace]
    finally:
        release.set()
        await inbound
        await finish(agent)


@pytest.mark.parametrize("queued", [False, True], ids=["running", "queued"])
async def test_busy_for_existing_ordinary_record(tmp_path, queued):
    held = HeldTurn()
    agent, _, model = await make_agent(tmp_path, held, changes={"tasks": {"max_concurrent": 1}})
    try:
        if queued:
            await agent.receive(group(agent, "fictional preceding request", mention=True))
            await wait_for_gate(held.entered)
        trace = await agent.receive(event(agent))
        await wait_for_gate(held.entered)
        record = agent.ledger.get("record", trace)
        assert record["exec_status"] == ("pending" if queued else "running")
        assert not record.get("proactive")
        await assert_busy(agent, model)
    finally:
        held.release.set()
        await finish(agent)


async def test_reservation_precedes_later_owner_reply_even_while_worker_is_full(
    tmp_path, monkeypatch
):
    held = HeldTurn()
    agent, channel, model = await make_agent(
        tmp_path, held, changes={"tasks": {"max_concurrent": 1}}
    )
    checking, claim, decided = asyncio.Event(), asyncio.Event(), asyncio.Event()
    open_record = agent.kernel.open_record
    behavior = agent.host.behavior

    async def hold_proactive(**options):
        if options.get("proactive"):
            checking.set()
            await claim.wait()
        return await open_record(**options)

    async def observe_decision(cid, hook, *args, **kwargs):
        result = await behavior(cid, hook, *args, **kwargs)
        if hook == "attention":
            decided.set()
        return result

    try:
        blocker = await agent.receive(group(agent, "fictional preceding request", mention=True))
        await wait_for_gate(held.entered)
        monkeypatch.setattr(agent.kernel, "open_record", hold_proactive)
        monkeypatch.setattr(agent.host, "behavior", observe_decision)
        proactive = asyncio.create_task(submit(agent))
        await wait_for_gate(checking)
        await assert_busy(agent, model)
        inbound = asyncio.create_task(agent.receive(event(agent)))
        await wait_for_gate(decided)
        assert not inbound.done()
        assert [row["trace_id"] for row in agent.ledger.rows("record")] == [blocker]
        claim.set()
        accepted, following = await asyncio.gather(proactive, inbound)
        assert accepted.status == "accepted"
        order = [blocker, accepted.trace_id, following]
        assert [row["trace_id"] for row in agent.ledger.rows("record")] == order
        assert len(model.requests) == 1
        held.release.set()
        await agent.drain()
        assert [intent.trace_id for intent in channel.sent] == order
    finally:
        claim.set()
        held.release.set()
        await finish(agent)


async def test_record_committed_before_claim_transaction_makes_submission_busy(
    tmp_path, monkeypatch
):
    agent, _, model = await make_agent(tmp_path)
    other_writer = Ledger(agent.ledger.path, agent.cid, agent.clock)
    target = event(agent).conversation
    transaction = agent.ledger.transaction
    winning_records = []

    @contextmanager
    def competing_writer_commits_first():
        if not winning_records:
            winning_records.append(other_writer.open_record(conversation=target))
        with transaction():
            yield

    monkeypatch.setattr(agent.ledger, "transaction", competing_writer_commits_first)
    try:
        refused = await submit(agent)
        assert refused.model_dump() == {"status": "busy", "trace_id": None}
        assert len(winning_records) == 1
        assert agent.ledger.rows("record") == winning_records
        assert not model.requests
        (audit,) = [row for row in agent.ledger.rows("audit") if row["kind"] == "proactive.busy"]
        assert (audit["plugin"], audit["reason"]) == (SUBMITTER, "sample")
    finally:
        other_writer.close()
        await finish(agent)


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["unknown", "sent", "sent"], "sent"),
        (["sent", "unknown"], "sent"),
        (["failed", "unknown"], "unknown"),
        (["failed", "failed"], "failed"),
    ],
)
async def test_outcome_uses_first_confirmed_receipt_without_disclosing_content(
    tmp_path, monkeypatch, statuses, expected
):
    agent, channel, model = await make_agent(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    receipts = []

    async def send(intent, cancel):
        entered.set()
        await release.wait()
        agent.clock.advance(1)
        receipt = Receipt(
            intent_id=intent.intent_id, status=statuses[len(receipts)], at=agent.clock.now()
        )
        receipts.append(receipt)
        return receipt

    monkeypatch.setattr(channel, "send", send)
    try:
        submitted = await agent.submit(
            OWNER,
            parts=[StickerPart(sticker_id=f"fictional-{i}") for i in range(len(statuses))],
            reason="sample",
            plugin_id=SUBMITTER,
        )
        assert submitted.status == "accepted"
        trace = submitted.trace_id
        await wait_for_gate(entered)
        await assert_outcome(agent, trace, "pending")
        await assert_busy(agent, model)
        await assert_outcome(agent, trace, "not_found", plugin_id=OTHER_PLUGIN)
        await assert_outcome(agent, "fictional-missing-trace", "not_found")
        release.set()
        await agent.drain()
        assert [receipt.status for receipt in receipts] == statuses
        first_sent = next((receipt.at for receipt in receipts if receipt.status == "sent"), None)
        await assert_outcome(agent, trace, expected, sent_at=first_sent)
        await assert_outcome(agent, trace, "not_found", plugin_id=OTHER_PLUGIN)
        assert not model.requests
    finally:
        release.set()
        await finish(agent)


@pytest.mark.parametrize(
    ("target", "turns", "status", "code"),
    [
        (OWNER, [{"finish": "stop", "text": ""}], "declined", None),
        (OWNER, [{"finish": "error"}], "failed", "model_error"),
        (OWNER, [{"finish": "deadline"}], "failed", "model_error"),
        (
            "test_group",
            [{"finish": "stop", "text": "fictional_internal"}] * 2,
            "failed",
            "outbound_blocked",
        ),
    ],
)
async def test_topic_outcome_when_nothing_is_delivered(tmp_path, target, turns, status, code):
    agent, channel, model = await make_agent(tmp_path, *turns)
    try:
        submitted = await submit(agent, target)
        await agent.drain()
        await assert_outcome(agent, submitted.trace_id, status)
        error = agent.ledger.get("record", submitted.trace_id)["error"]
        assert (error["code"] if error else None) == code
        assert len(model.requests) == len(turns) and not channel.sent
    finally:
        await finish(agent)


@pytest.mark.parametrize(
    ("target", "incoming_cid", "speaker", "used_tool", "expected"),
    [
        (OWNER, OWNER, "owner", False, "superseded"),
        (OWNER, "other_private", "owner", False, "sent"),
        (OWNER, OWNER, "owner", True, "sent"),
        ("test_group", "test_group", "owner", False, "superseded"),
        ("test_group", "other_group", "owner", False, "sent"),
        ("test_group", "test_group", "member", False, "sent"),
    ],
)
async def test_inbound_during_proactive_writing(
    tmp_path, target, incoming_cid, speaker, used_tool, expected
):
    held = HeldTurn()
    agent, channel, model = await make_agent(
        tmp_path,
        *([ECHO, held] if used_tool else [held]),
        changes={"proactive": {"tools": {"echo": {"daily": 1}}}},
        conversations={
            "other_private": {
                "kind": "private",
                "scene": "owner_private",
                "channel": "fake-channel",
            },
            "other_group": {"kind": "group", "scene": "public_group", "channel": "fake-channel"},
        },
    )
    try:
        trace = (await submit(agent, target, tools=used_tool)).trace_id
        await wait_for_gate(held.entered)
        await assert_outcome(agent, trace, "pending")
        assert [row["status"] for row in agent.ledger.rows("invocation")] == (
            ["ok"] if used_tool else []
        )
        if used_tool:
            await assert_busy(agent, model)
        is_group = target == "test_group"
        following = await agent.receive(
            event(
                agent,
                "fictional incoming message",
                cid=incoming_cid,
                role=speaker,
                scene="public_group" if is_group else "owner_private",
                mention=is_group,
            )
        )
        await assert_outcome(agent, trace, "superseded" if expected == "superseded" else "pending")
        if incoming_cid == OWNER and used_tool:
            assert agent.ledger.get("record", following)["exec_status"] == "pending"
            assert len(model.requests) == 2
        held.release.set()
        await agent.drain()
        assert [intent.trace_id for intent in channel.sent].count(following) == 1
        assert agent.ledger.get("record", following)["exec_status"] == "ok"
        superseded = [
            row for row in agent.ledger.rows("audit") if row["kind"] == "proactive.superseded"
        ]
        if expected == "superseded":
            assert [row["trace_id"] for row in superseded] == [trace]
            assert not agent.ledger.intents(trace)
            assert [intent.trace_id for intent in channel.sent] == [following]
            await assert_outcome(agent, trace, "superseded")
        else:
            assert not superseded
            assert [intent.trace_id for intent in channel.sent].count(trace) == 1
            if used_tool:
                assert [intent.trace_id for intent in channel.sent] == [trace, following]
            await assert_outcome(agent, trace, "sent", sent_at=agent.clock.now())
    finally:
        held.release.set()
        await finish(agent)


RECONCILING_PLUGIN = """
class Plugin:
    async def start(self, ctx):
        self.observed = {}
        for trace in await ctx.storage.get("outstanding") or []:
            result = await ctx.proactive.result(trace)
            self.observed[trace] = result.model_dump()

    async def stop(self):
        pass
"""


async def test_plugins_can_reconcile_inflight_traces_during_companion_start_without_replay(
    tmp_path, monkeypatch
):
    home = private_home(tmp_path)
    path = home / "companions/companion_a/companion.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    plugins = (SUBMITTER, OTHER_PLUGIN)
    for pid in plugins:
        plugin(home, pid, RECONCILING_PLUGIN, permissions=["message.proactive"])
        config["plugins"][pid] = {"enabled": True, "config": {}}
    write_yaml(path, config)
    recovered_home = tmp_path / "recovered"
    shutil.copytree(home, recovered_home)
    connect = FakeChannel.connect

    async def quiet(channel):
        channel.incoming = ()
        await connect(channel)

    monkeypatch.setattr(FakeChannel, "connect", quiet)
    first = Companion(home, "companion_a", MockClock())
    await first.start()
    held = HeldTurn()
    sending, release_send = asyncio.Event(), asyncio.Event()
    channel = next(iter(first.channels.values()))
    send = channel.send

    async def hold_send(intent, cancel):
        sending.set()
        await release_send.wait()
        return await send(intent, cancel)

    monkeypatch.setattr(first.agent.model, "generate", held)
    monkeypatch.setattr(channel, "send", hold_send)
    try:
        await asyncio.gather(*first.feeds)
        sender = first.host.runtimes[first.cid, SUBMITTER].ctx.proactive
        writing = await sender.submit(OWNER, topic="fictional topic", reason="sample")
        await wait_for_gate(held.entered)
        delivery = await sender.submit(
            "test_group", parts=[StickerPart(sticker_id="fictional-sticker")], reason="sample"
        )
        await wait_for_gate(sending)
        assert (writing.status, delivery.status) == ("accepted", "accepted")
        traces = [writing.trace_id, delivery.trace_id, "fictional-missing-trace"]
        assert (await sender.result(writing.trace_id)).status == "pending"
        assert (await sender.result(delivery.trace_id)).status == "pending"

        # Save the second home at the instant before either operation finishes.
        for pid in plugins:
            store = first.host.runtimes[first.cid, pid].ctx.storage
            await store.set("outstanding", traces)
            shutil.copytree(
                store.path, recovered_home / store.path.relative_to(home), dirs_exist_ok=True
            )
        snapshot = recovered_home / first.runtime.path.relative_to(home)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        ledger = first.kernel.ledger

        def save_ledger():
            with closing(sqlite3.connect(snapshot)) as destination:
                ledger.db.backup(destination)

        await run_sync(ledger, save_ledger)
    finally:
        held.release.set()
        release_send.set()
        await first.agent.drain()
        await first.stop()

    recovered = Companion(recovered_home, "companion_a", MockClock())
    await recovered.start()
    try:
        await asyncio.gather(*recovered.feeds)
        await recovered.agent.drain()
        expected = ["interrupted", "unknown", "not_found"]
        for pid, outcomes in [(SUBMITTER, expected), (OTHER_PLUGIN, ["not_found"] * len(traces))]:
            observer = recovered.host.runtimes[recovered.cid, pid].plugin
            assert observer.observed == {
                trace: {"status": outcome, "sent_at": None}
                for trace, outcome in zip(traces, outcomes)
            }
        assert not recovered.agent.model.requests
        assert all(not channel.sent for channel in recovered.channels.values())
        ledger = recovered.kernel.ledger
        assert len(await run_sync(ledger, ledger.rows, "record")) == 2
        (attempt,) = await run_sync(ledger, ledger.rows, "attempt")
        assert attempt["n"] == 1 and attempt["status"] == "unknown"
    finally:
        await recovered.stop()
