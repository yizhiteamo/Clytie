import asyncio
import json

from clytie.backup.io_control import IoControl
from clytie.backup.journal import BackupJournal
from clytie.backup.workers import BackupWorkers
from clytie.plugin_host.locking import process_identity
from clytie.sdk.testing import MockClock


async def test_worker_bridge_wait_does_not_block_budget_reply(tmp_path, monkeypatch):
    clock = MockClock()
    operation = "a" * 32
    journal, _ = BackupJournal.accept(tmp_path, clock, operation, "verify", {})
    budget_received = asyncio.Event()
    responses = []

    class Pipe:
        closed = False

        def is_closing(self):
            return self.closed

        def write(self, value):
            if value == b"start\n":
                return
            reply = json.loads(value)
            responses.append(reply)
            if reply["type"] == "backup_io_reply":
                budget_received.set()

        async def drain(self):
            pass

        def close(self):
            self.closed = True

        async def wait_closed(self):
            pass

    class Process:
        pid = process_identity()["pid"]
        returncode = None
        stdin = Pipe()
        stdout = asyncio.StreamReader()

        async def wait(self):
            self.returncode = 0
            return 0

    process = Process()
    journal.phase("preparing", executor=process_identity(), delivery="waiting")
    for message in (
        {"type": "ready", "id": operation},
        {"type": "bridge", "id": "capture", "operation": "capture", "arguments": {}},
        {
            "type": "backup_io",
            "id": "grant",
            "operation_id": operation,
            "arguments": {"action": "acquire", "stage": "phase_a"},
        },
    ):
        process.stdout.feed_data(json.dumps(message).encode() + b"\n")

    async def spawn(*args, **kwargs):
        return process

    async def bridge(journal, message):
        await budget_received.wait()
        process.stdout.feed_eof()
        return {}

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    workers = BackupWorkers(tmp_path, clock, capture_handler=bridge)
    # This fixture isolates pipe scheduling; durable restart admission is covered
    # with actual workers in test_backup_io_restart_wiring.
    workers.io_control = IoControl(clock)
    ready = asyncio.get_running_loop().create_future()
    run = asyncio.create_task(workers._run(journal, ready, recover_only=False))
    try:
        for _ in range(100):
            await clock.sleep(0)
        assert budget_received.is_set(), "bridge must not monopolize the output reader"
        await run
        assert {row["type"] for row in responses} == {"bridge_reply", "backup_io_reply"}
        assert not workers.io_control.sessions
        assert workers.io_control.budget.status()["active"] is None
    finally:
        process.stdout.feed_eof()
        budget_received.set()
        await run
