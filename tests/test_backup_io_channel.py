import asyncio
import io
import threading
from types import SimpleNamespace

import pytest

from clytie.backup.io_budget import CHUNK, IoBudget, IoBudgetError
from clytie.backup.io_channel import IoChannel
from clytie.backup.io_protocol import BudgetEndpoint
from clytie.backup.worker import Controls
from clytie.sdk.testing import MockClock


async def test_thread_channel_uses_shared_budget_and_binds_operation():
    clock = MockClock()
    budget = IoBudget(clock)
    endpoint = BudgetEndpoint(budget, executor="executor_a", operation="backup_a")
    loop = asyncio.get_running_loop()
    messages, tasks = [], []

    async def handle(message):
        assert message["operation_id"] == "backup_a"
        reply = await endpoint.request(message["arguments"])
        channel.receive({"type": "backup_io_reply", "id": message["id"], "result": reply})

    def emit(message):
        messages.append(message)
        loop.call_soon_threadsafe(lambda: tasks.append(asyncio.create_task(handle(message))))

    channel = IoChannel(emit)

    def work():
        client = channel.client("backup_a", check=lambda: None)
        with client.stage():
            client.charge("read", CHUNK)
            client.charge("write", CHUNK)

    await asyncio.to_thread(work)
    await asyncio.gather(*tasks)
    assert [item["arguments"]["action"] for item in messages] == [
        "acquire",
        "charge",
        "charge",
        "release",
    ]
    assert budget.status()["charged"] == {"read": CHUNK, "write": CHUNK}
    assert budget.status()["active"] is None and channel.pending_count == 0


async def test_cancel_during_budget_wait_sends_cleanup_without_losing_slot():
    clock = MockClock()
    endpoint = BudgetEndpoint(IoBudget(clock), executor="executor_a", operation="backup_a")
    loop = asyncio.get_running_loop()
    waiting = asyncio.Event()
    cancel = threading.Event()
    tasks = []

    async def handle(message):
        args = message["arguments"]
        if args["action"] == "charge" and args["sequence"] == 3:
            waiting.set()
        try:
            reply = {"result": await endpoint.request(args)}
        except IoBudgetError:
            reply = {"error": "backup_io_stage_finished"}
        channel.receive({"type": "backup_io_reply", "id": message["id"], **reply})

    channel = IoChannel(
        lambda msg: loop.call_soon_threadsafe(
            lambda: tasks.append(asyncio.create_task(handle(msg)))
        )
    )

    def check():
        if cancel.is_set():
            raise InterruptedError("fictional_cancel")

    def work():
        client = channel.client("backup_a", check=check)
        with client.stage():
            for _ in range(3):
                client.charge("read", CHUNK)

    task = asyncio.create_task(asyncio.to_thread(work))
    await waiting.wait()
    cancel.set()
    with pytest.raises(InterruptedError):
        await task
    await asyncio.gather(*tasks)
    assert endpoint.budget.status()["active"] is None
    assert channel.pending_count == 0


async def test_eof_wakes_waiter_but_does_not_claim_executor_exit():
    clock = MockClock()
    endpoint = BudgetEndpoint(IoBudget(clock), executor="executor_a", operation="backup_a")
    await endpoint.request({"action": "acquire", "stage": "phase_a"})
    waiting = threading.Event()
    channel = IoChannel(lambda msg: waiting.set())
    caller = asyncio.create_task(
        asyncio.to_thread(channel.call, "backup_a", {"action": "charge"}, check=lambda: None)
    )
    assert await asyncio.to_thread(waiting.wait, 5)
    channel.disconnect()
    endpoint.budget.disconnected("executor_a")
    with pytest.raises(IoBudgetError, match="disconnected"):
        await caller
    assert channel.pending_count == 0
    assert endpoint.budget.status()["active"]["disconnected"]
    endpoint.budget.executor_ended("executor_a", confirmed=True)
    assert endpoint.budget.status()["active"] is None


def test_unknown_reply_is_not_retained_and_remote_text_not_exposed():
    channel = IoChannel(
        lambda msg: channel.receive(
            {"type": "backup_io_reply", "id": msg["id"], "error": "untrusted detail"}
        )
    )
    channel.receive({"type": "backup_io_reply", "id": "not_pending", "result": {}})
    assert channel.pending_count == 0
    with pytest.raises(IoBudgetError, match="backup_io_remote_failure"):
        channel.call("backup_a", {}, check=lambda: None)
    assert channel.pending_count == 0


def test_worker_control_reader_delivers_budget_reply_and_eof():
    journal = SimpleNamespace(value={"id": "backup_a"})
    controls = Controls(journal, None, emit=lambda value: None)
    channel = controls.io_channel
    controls._read(io.BytesIO(b'{"type":"backup_io_reply","id":"unknown","result":{}}\n'))
    assert channel.pending_count == 0
    assert controls.cancelled.is_set()
    with pytest.raises(IoBudgetError, match="disconnected"):
        channel.call("backup_a", {}, check=lambda: None)


def test_cancel_is_preserved_when_cleanup_reply_is_lost():
    from clytie.backup.io_client import IoClient

    def request(arguments, cleanup=False):
        if cleanup:
            raise IoBudgetError("backup_io_disconnected")
        return {}

    with pytest.raises(InterruptedError, match="fictional_cancel") as error:
        with IoClient(request).stage():
            raise InterruptedError("fictional_cancel")
    assert isinstance(error.value.__context__, IoBudgetError)
