"""Stop timing is independent of HTTP tasks and never a worker kill deadline."""

import asyncio

import pytest

from clytie.api.management_workers import Workers
from clytie.sdk.testing import MockClock


class Runner:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancel_count = 0

    async def run(self, journal, args, control):
        control(self.cancel)
        self.started.set()
        await self.release.wait()
        journal.committed()
        return {"ok": True, "result": {"done": True}}

    def cancel(self):
        self.cancel_count += 1


async def ticks():
    await MockClock().drain(8)


async def test_idle_stop_immediate_and_wait_escalation_threshold_not_reset(tmp_path):
    clock = MockClock()
    idle = Workers(tmp_path, clock, timeout=120)
    await idle.stop("cancel")
    runner = Runner()
    workers = Workers(tmp_path, clock, runner=runner.run, timeout=120)
    call = asyncio.create_task(workers.execute("companion_a", "export", {}))
    await runner.started.wait()
    stop = asyncio.create_task(workers.stop("wait"))
    await ticks()
    assert runner.cancel_count == 0
    workers.escalate()
    await ticks()
    since = workers.cancel_since
    clock.advance(121)
    await ticks()
    workers.escalate()
    assert workers.cancel_since == since
    assert workers.still_finishing
    assert not stop.done()
    runner.release.set()
    await call
    await stop


async def test_http_cancellation_does_not_cancel_accepted_operation_or_duplicate(tmp_path):
    runner = Runner()
    workers = Workers(tmp_path, MockClock(), runner=runner.run)
    call = asyncio.create_task(workers.execute("companion_a", "export", {}))
    await runner.started.wait()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert runner.cancel_count == 0
    with pytest.raises(Exception, match="management_busy"):
        await workers.execute("companion_a", "export", {})
    assert workers.status()[0]["state"] == "accepted"
    runner.release.set()
    await workers.stop("wait")
    assert workers.status()[0]["committed"]


async def test_worker_spawn_failure_is_terminal_and_does_not_leave_a_writer(tmp_path, monkeypatch):
    from clytie.api.auth import APIError

    async def unavailable(*args, **kwargs):
        raise OSError("fictional launcher failure")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unavailable)
    workers = Workers(tmp_path, MockClock())
    with pytest.raises(APIError, match="management_worker_failed"):
        await workers.execute("companion_a", "export", {})
    assert workers.status()[0]["state"] == "failed"
    assert not workers.status()[0]["committed"]
    assert not workers.tasks
    runner = Runner()
    runner.release.set()
    await workers.execute("companion_a", "export", {}, runner=runner.run)
    assert len(workers.status()) == 2


async def test_request_timeout_preserves_accepted_operation_and_query_identifier(tmp_path):
    from clytie.api.auth import APIError

    runner = Runner()
    clock = MockClock()
    workers = Workers(tmp_path, clock, runner=runner.run)
    call = asyncio.create_task(workers.execute("companion_a", "export", {}))
    await runner.started.wait()
    await ticks()
    clock.advance(601)
    await ticks()
    with pytest.raises(APIError) as caught:
        await call
    assert caught.value.status == 504
    key = caught.value.result["operation_id"]
    assert runner.cancel_count == 0 and key in workers.tasks
    assert (await workers.execute("companion_a", "export", {}, key))["state"] == "accepted"
    runner.release.set()
    await workers.stop("wait")
    assert workers.status()[0]["committed"]


async def test_restart_verification_opens_database_only_in_worker(tmp_path, monkeypatch):
    import sqlite3

    from clytie.api.management_journal import Journal
    from clytie.memory.store import MemoryStore

    clock = MockClock()
    journal = Journal.create(tmp_path, "companion_a", "thaw", clock)
    store = MemoryStore(
        tmp_path / "companions/companion_a/memory/memory.sqlite3",
        clock=clock,
        config={},
        instance={},
    )
    journal.database(store.path)
    journal.phase("transaction")
    with store.transaction():
        journal.mark_transaction(store, {"thawed": True})
    store.close()
    journal.value["executor"] = {"pid": 2**30, "started_at": "2000-01-01T00:00:00+00:00"}
    journal.save()

    def forbidden(*args, **kwargs):
        raise AssertionError("daemon must not open memory databases")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    workers = Workers(tmp_path, clock)
    await workers._recover_before_admission("companion_a")
    assert Journal.load(journal.path, clock).value["committed"]
    assert workers.tasks == {}


async def test_ignoring_management_cancel_does_not_delay_other_companion_stops(tmp_path):
    from tests.test_supervisor import make, spin

    svc, launcher, clock = await make(tmp_path, count=2)
    runner = Runner()
    workers = Workers(tmp_path, clock, runner=runner.run)
    svc.management_workers = workers
    call = asyncio.create_task(workers.execute("stopped_companion", "import/plan", {}))
    await runner.started.wait()
    closing = asyncio.create_task(svc.stop("cancel", source="frontend_eof"))
    try:
        await spin()
        assert runner.cancel_count == 1
        assert all(child.exited.is_set() for _, child in launcher.children)
        assert not closing.done()
        assert workers.status()[0]["state"] == "accepted"
        clock.advance(121)
        await spin()
        assert workers.still_finishing and not closing.done()
        assert all(not child.killed for _, child in launcher.children)
    finally:
        runner.release.set()
        await call
        await closing


@pytest.mark.parametrize("started", [False, True])
async def test_worker_transport_loss_preserves_commit_or_clears_unstarted_operation(
    tmp_path, monkeypatch, started
):
    from clytie.api.auth import APIError

    workers = Workers(tmp_path, MockClock())

    class Pipe:
        chunks = 0

        def write(self, value):
            if not started:
                raise BrokenPipeError()

        async def drain(self):
            pass

        async def wait_closed(self):
            pass

        def close(self):
            pass

        def is_closing(self):
            return False

        async def readline(self):
            if not started:
                return b""
            raise ValueError("fictional response exceeds transport limit")

        async def read(self, size):
            self.chunks += 1
            return b"fictional discarded response" if self.chunks == 1 else b""

    class Process:
        from os import getpid

        pid = getpid()
        stdin, stdout = Pipe(), Pipe()
        returncode = None

        async def wait(self):
            assert self.stdout.chunks == (2 if started else 0)
            self.returncode = 0
            return 0

    process = Process()

    async def spawn(*args, **kwargs):
        if started:
            next(iter(workers.journals.values())).committed({"plan_id": "a" * 32})
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    if started:
        result = await workers.execute("companion_a", "import/plan", {})
        assert result["committed"] and result["result"] == {"plan_id": "a" * 32}
        assert result["delivery"] == "unconfirmed"
    else:
        with pytest.raises(APIError):
            await workers.execute("companion_a", "import/plan", {})
        assert workers.status()[0]["state"] == "failed"
    assert process.returncode == 0
    assert not workers.tasks
