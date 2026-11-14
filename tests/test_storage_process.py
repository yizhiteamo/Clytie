"""真实进程强杀：所有者事务回滚、内存队列不重放、旧发送不补发。"""

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from clytie.backup.coordination import run_sync
from clytie.clock import SystemClock
from clytie.plugin_host.support import timed
from clytie.sdk.testing import MockClock
from clytie.sdk.types import Actor, Conversation, DeliveryIntent, Invocation, Plan, ToolCall
from clytie.supervisor.companion import Companion, StartupBlocked
from clytie.supervisor.processes import Lines, creation, frame, python_process

from .test_supervisor_startup import home_copy

INTENT_ID = "a" * 32


async def _child(home, mode):
    """测试执行器只装配 examples/home 的假模型和假平台。"""
    runtime = Companion(home, "companion_a", MockClock())
    loop = asyncio.get_running_loop()
    sent = asyncio.Event()
    never = asyncio.Event()
    calls_path = home / "fictional-platform-calls.jsonl"
    original_load = runtime.host._load_class

    def load(manifest):
        cls, module = original_load(manifest)
        if manifest.id == "fake-channel":
            name = manifest.data["contributes"]["channel"]["factory"]
            original_factory = getattr(cls, name)

            def factory(plugin):
                channel = original_factory(plugin)
                original_send = channel.send

                async def send(intent, cancel):
                    # 假平台已经观察到调用；故意不给收据，不能猜发送失败并重发。
                    with calls_path.open("a", encoding="utf-8") as stream:
                        stream.write(
                            json.dumps({"intent_id": intent.intent_id, "mode": mode}) + "\n"
                        )
                        stream.flush()
                        os.fsync(stream.fileno())
                    if mode == "crash" and intent.intent_id == INTENT_ID:
                        sent.set()
                        await never.wait()
                    return await original_send(intent, cancel)

                channel.send = send
                return channel

            setattr(cls, name, factory)
        return cls, module

    runtime.host._load_class = load
    try:
        await runtime.start()
    except StartupBlocked as exc:
        frame(sys.stdout.buffer, {"type": "blocked", "seq": 1, "reason": exc.reason})
        return
    if mode == "probe":
        await runtime.stop()
        frame(sys.stdout.buffer, {"type": "unexpected_writer", "seq": 1})
        return

    for task in runtime.feeds:
        task.cancel()
    await asyncio.gather(*runtime.feeds, return_exceptions=True)
    await runtime.agent.drain()
    await runtime.clock.drain()
    ledger, owner = runtime.kernel.ledger, runtime.storage_owner

    if mode == "restart":

        def inspect():
            memory = runtime.host.memory_store("companion_a")
            return {
                "records": ledger.rows("record"),
                "invocations": ledger.rows("invocation"),
                "intents": ledger.rows("intent"),
                "attempts": ledger.rows("attempt"),
                "audit_kinds": [row["kind"] for row in ledger.rows("audit")],
                "memory_uncommitted": memory.meta("process_uncommitted"),
                "memory_queued": memory.meta("process_queued"),
            }

        snapshot = await run_sync(ledger, inspect)
        await runtime.stop()
        frame(sys.stdout.buffer, {"type": "restarted", "seq": 1, "snapshot": snapshot})
        return

    assert mode == "crash"
    channel_id = next(iter(runtime.channels))
    conversation = Conversation(
        conversation_id="test_group", kind="group", scene="public_group", channel_id=channel_id
    )
    invocation = Invocation(
        companion_id="companion_a",
        actor=Actor(actor_id="owner", role="owner", channel_id=channel_id),
        conversation=conversation,
        trace_id="b" * 32,
    )
    record = await runtime.kernel.open_record(invocation=invocation)
    tool_started = asyncio.Event()

    class Capability:
        def idempotency_key(self, call, invocation):
            return "fictional-process-task"

        async def prepare(self, call, invocation):
            return Plan(summary="Fictional pending tool", heavy=False, data={})

        async def execute(self, plan, invocation, cancel):
            tool_started.set()
            await never.wait()

    runtime.kernel.permissions = lambda *_: True
    runtime.kernel.quota = lambda *_: True
    tool = asyncio.create_task(
        runtime.kernel.execute(
            record["trace_id"],
            "echo-tool",
            ToolCall(call_id="process-call", tool="echo", arguments={}),
            Capability(),
            invocation,
        )
    )
    await tool_started.wait()
    outgoing = await runtime.kernel.open_record(conversation=conversation)
    intent = DeliveryIntent(
        intent_id=INTENT_ID,
        companion_id="companion_a",
        conversation_id=conversation.conversation_id,
        channel_id=channel_id,
        kind="proactive",
        trace_id=outgoing["trace_id"],
        parts=[{"type": "text", "text": "Fictional delivery awaiting receipt"}],
    )
    await runtime.kernel.seal(outgoing["trace_id"], "ok", [intent])
    delivery = asyncio.create_task(runtime.kernel.delivery.deliver(outgoing["trace_id"]))
    await sent.wait()

    def before_crash():
        with ledger.transaction():
            ledger.audit("process.committed")
        return {
            "record": ledger.get("record", record["trace_id"]),
            "intent": ledger.get("intent", INTENT_ID),
        }

    durable = await run_sync(ledger, before_crash)
    transaction_entered = asyncio.Event()
    release = threading.Event()

    def uncommitted(resources, command):
        with resources.ledger.transaction(), resources.memory.transaction():
            resources.ledger.audit("process.uncommitted")
            resources.memory.set_meta("process_uncommitted", True)
            loop.call_soon_threadsafe(transaction_entered.set)
            assert release.wait(30), "parent did not kill the synchronized test process"

    def queued(resources, command):
        with resources.ledger.transaction():
            resources.ledger.audit("process.queued")
        with resources.memory.transaction():
            resources.memory.set_meta("process_queued", True)

    active = owner.submit(uncommitted)
    await transaction_entered.wait()
    pending = owner.submit(queued)
    assert active.phase == "running" and pending.phase == "queued"
    assert not tool.done() and not delivery.done()
    frame(
        sys.stdout.buffer,
        {
            "type": "armed",
            "seq": 1,
            "pid": os.getpid(),
            "owner_thread": owner._thread.ident,
            "loop_thread": threading.get_ident(),
            "tool_trace": record["trace_id"],
            "delivery_trace": outgoing["trace_id"],
            "record_status": durable["record"]["exec_status"],
            "intent_status": durable["intent"]["status"],
            "active_phase": active.phase,
            "pending_phase": pending.phase,
        },
    )
    await never.wait()


class Process:
    def __init__(self, home, mode, directory):
        executable, env = python_process()
        self.log = (directory / (mode + "-child.log")).open("wb")
        self.process = subprocess.Popen(
            [
                executable,
                "-c",
                "import asyncio,sys; from pathlib import Path; "
                "from tests.test_storage_process import _child; "
                "asyncio.run(_child(Path(sys.argv[1]), sys.argv[2]))",
                str(home),
                mode,
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=self.log,
            **creation(),
        )
        self.lines = Lines(self.process.stdout)

    async def receive(self):
        message = await timed(self.lines.receive(), SystemClock(), 25)
        assert message is not None, Path(self.log.name).read_text(encoding="utf-8")
        return message

    async def wait(self):
        return await timed(asyncio.to_thread(self.process.wait), SystemClock(), 25)

    async def close(self):
        if self.process.poll() is None:
            # 仅此测试通过 Popen 启动的明确进程；不枚举或杀进程树。
            self.process.kill()
        await self.wait()
        self.process.stdout.close()
        self.log.close()


async def test_killed_owner_rolls_back_and_restart_does_not_replay_or_resend(tmp_path):
    home = home_copy(tmp_path)
    crashed = Process(home, "crash", tmp_path)
    try:
        armed = await crashed.receive()
        assert armed["type"] == "armed" and armed["pid"] == crashed.process.pid
        assert armed["owner_thread"] != armed["loop_thread"]
        assert (armed["active_phase"], armed["pending_phase"]) == ("running", "queued")
        assert (armed["record_status"], armed["intent_status"]) == ("running", "sending")

        probe = Process(home, "probe", tmp_path)
        try:
            assert await probe.receive() == {"type": "blocked", "seq": 1, "reason": "lock_held"}
            assert await probe.wait() == 0
        finally:
            await probe.close()

        assert crashed.process.poll() is None
        crashed.process.kill()
        assert await crashed.wait() != 0
        restarted = Process(home, "restart", tmp_path)
        try:
            report = await restarted.receive()
            assert report["type"] == "restarted"
            assert await restarted.wait() == 0
        finally:
            await restarted.close()
        snapshot = report["snapshot"]
        assert snapshot["audit_kinds"].count("process.committed") == 1
        assert "process.uncommitted" not in snapshot["audit_kinds"]
        assert "process.queued" not in snapshot["audit_kinds"]
        assert snapshot["memory_uncommitted"] is snapshot["memory_queued"] is None
        record = next(row for row in snapshot["records"] if row["trace_id"] == armed["tool_trace"])
        assert record["exec_status"] == "interrupted"
        invocations = [
            row for row in snapshot["invocations"] if row["trace_id"] == armed["tool_trace"]
        ]
        assert len(invocations) == 1 and invocations[0]["status"] == "interrupted"
        intents = [row for row in snapshot["intents"] if row["intent_id"] == INTENT_ID]
        attempts = [row for row in snapshot["attempts"] if row["intent_id"] == INTENT_ID]
        assert len(intents) == len(attempts) == 1
        assert intents[0]["status"] == attempts[0]["status"] == "unknown"
        calls = [
            json.loads(line)
            for line in (home / "fictional-platform-calls.jsonl").read_text().splitlines()
        ]
        assert [row for row in calls if row["intent_id"] == INTENT_ID] == [
            {"intent_id": INTENT_ID, "mode": "crash"}
        ]
    finally:
        await crashed.close()
