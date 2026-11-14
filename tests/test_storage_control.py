"""真实 OS 管道与虚构慢存储：控制受理不能等于持久化确认。"""

import asyncio
import os
import threading
from types import SimpleNamespace

from clytie.backup.coordination import run_sync
from clytie.plugin_host.support import HostLog
from clytie.supervisor import entry
from clytie.supervisor.processes import Lines, frame

from .test_storage_kernel import owned_kernel
from .test_storage_kernel_finalizers import spin


async def test_real_pipe_accepts_stop_escalate_and_cancel_during_slow_silence(
    tmp_path, monkeypatch
):
    async with owned_kernel(tmp_path) as (kernel, _):
        entered, release = threading.Event(), threading.Event()
        stop_received, stop_release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        runtimes, messages = [], []

        class Runtime:
            def __init__(self, *args):
                self.config = {}
                self.log = HostLog()
                self.escalated = asyncio.Event()
                self.management_requests, self.management_tasks = set(), set()
                self.management_journals = {"fictional_op": SimpleNamespace(cancel=cancelled.set)}
                runtimes.append(self)

            async def start(self):
                return True

            async def refresh_health(self):
                pass

            def health(self):
                return {}

            def cancel_management(self):
                cancelled.set()

            def session_ending(self):
                pass

            async def silence(self, on):
                def apply():
                    entered.set()
                    assert release.wait(5)
                    kernel.ledger.audit("fictional_silence", on=on)

                await run_sync(kernel.ledger, apply)

            async def stop(self, *args, **kwargs):
                stop_received.set()
                await stop_release.wait()
                await asyncio.gather(*tuple(self.management_tasks))
                return {"state": "stopped"}

        input_read, input_write = os.pipe()
        output_read, output_write = os.pipe()
        with (
            os.fdopen(input_read, "rb", buffering=0) as stdin,
            os.fdopen(input_write, "wb", buffering=0) as sender,
            os.fdopen(output_read, "rb", buffering=0) as receiver,
            os.fdopen(output_write, "wb", buffering=0) as stdout,
        ):
            monkeypatch.setattr(entry, "Companion", Runtime)
            monkeypatch.setattr(
                entry,
                "sys",
                SimpleNamespace(
                    stdin=SimpleNamespace(buffer=stdin),
                    stdout=SimpleNamespace(buffer=stdout),
                    stderr=SimpleNamespace(buffer=stdout),
                ),
            )
            output = Lines(receiver)

            async def collect():
                while True:
                    msg = await output.receive()
                    if msg is None:
                        return
                    messages.append(msg)

            collector = asyncio.create_task(collect())
            task = asyncio.create_task(entry.companion_main(tmp_path, "companion_a"))
            frame(sender, {"type": "boot", "seq": 0})
            try:
                await spin(lambda: any(msg["type"] == "ready" for msg in messages))
                frame(sender, {"type": "silence", "seq": 1, "on": True})
                await spin(entered.is_set)
                frame(
                    sender, {"type": "management_cancel", "seq": 2, "operation_id": "fictional_op"}
                )
                frame(sender, {"type": "escalate", "seq": 3})
                frame(sender, {"type": "stop", "seq": 4})
                await spin(
                    lambda: (
                        stop_received.is_set()
                        and cancelled.is_set()
                        and runtimes[0].escalated.is_set()
                    )
                )
                assert not any(msg["type"] == "silence_ack" for msg in messages)
                stop_release.set()
                assert runtimes[0].management_tasks
                assert not task.done()
                release.set()
                await spin(lambda: any(msg["type"] == "silence_ack" for msg in messages))
                assert [msg["seq"] for msg in messages if msg["type"] == "silence_ack"] == [1]
            finally:
                release.set()
                stop_release.set()
                frame(sender, {"type": "stop", "seq": 5})
                await task
                collector.cancel()
                await asyncio.gather(collector, return_exceptions=True)


async def test_silence_duplicates_and_toggles_apply_and_ack_in_received_order():
    started, applied, acks = [], [], []
    gates = [asyncio.Event() for _ in range(3)]

    async def apply(on):
        index = len(started)
        started.append(on)
        await gates[index].wait()
        applied.append(on)

    runtime = SimpleNamespace(silence=apply, management_tasks=set(), log=HostLog())
    control = entry._SilenceApplications(runtime, acks.append, lambda: False)
    for seq, on in ((7, True), (8, True), (9, False)):
        control.submit({"seq": seq, "on": on})
    try:
        for index, seq in enumerate((7, 8, 9)):
            await spin(lambda: len(started) == index + 1)
            assert len(applied) == len(acks) == index
            gates[index].set()
            await spin(lambda: len(acks) == index + 1)
            assert acks[-1] == seq
        assert started == applied == [True, True, False]
    finally:
        for gate in gates:
            gate.set()
        await control.finish()


async def test_silence_queue_is_bounded_and_stop_discards_unstarted_work():
    gate, entered = asyncio.Event(), asyncio.Event()
    applied, acks = [], []

    async def apply(on):
        entered.set()
        await gate.wait()
        applied.append(on)

    runtime = SimpleNamespace(silence=apply, management_tasks=set(), log=HostLog())
    control = entry._SilenceApplications(runtime, acks.append, lambda: False)
    control.submit({"seq": 1, "on": True})
    await entered.wait()
    for seq in range(2, 66):
        control.submit({"seq": seq, "on": False})
    assert len(control.pending) == 63
    assert len(runtime.management_tasks) == 1
    assert [row[1] for row in runtime.log.entries] == ["companion.silence_busy"]
    control.close()
    control.submit({"seq": 66, "on": False})
    assert not control.pending
    assert not acks
    gate.set()
    await control.finish()
    assert applied == [True]
    assert acks == [1]
    assert not runtime.management_tasks


async def test_silence_storage_failure_does_not_emit_ack():
    from clytie.storage.owner import StorageBusy

    async def apply(on):
        raise StorageBusy()

    acks = []
    runtime = SimpleNamespace(silence=apply, management_tasks=set(), log=HostLog())
    control = entry._SilenceApplications(runtime, acks.append, lambda: False)
    control.submit({"seq": 1, "on": True})
    await control.task
    await control.finish()
    assert not acks
    assert runtime.log.entries == [
        ("warning", "companion.silence_failed", {"exc_type": "StorageBusy"})
    ]
