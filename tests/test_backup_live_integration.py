"""真实回环 API、守护、伴侣和独立备份进程；只加载虚构插件。"""

import asyncio
import os
from uuid import uuid4

import pytest
import yaml
from aiohttp import ClientSession

from clytie.api.management import Management
from clytie.api.server import Server
from clytie.config import load_home
from clytie.sdk.testing import MockClock
from clytie.supervisor.entry import FrontendCommands
from clytie.supervisor.processes import ProcessLauncher
from clytie.supervisor.service import Supervisor

from .host_helpers import plugin
from .management_integration_helpers import close_exited_launcher, home_with_importer, until

SOURCE = """
import os
from pathlib import Path
from clytie.sdk import BackupTicket, BackupView
class Plugin:
    async def start(self, ctx):
        self.ctx = ctx
        self.root = Path(ctx.config["barrier"])
    async def stop(self):
        pass
    def backup(self):
        return self
    async def barrier(self, phase):
        (self.root / (phase + ".pid")).write_text(str(os.getpid()))
        if self.ctx.config["phase"] == phase:
            while not (self.root / "release").exists():
                await self.ctx.clock.sleep(0.01)
    async def prepare(self, request, cancel):
        await self.barrier("prepare")
        return BackupTicket("fictional")
    async def capture(self, ticket, epoch, cancel):
        return BackupView(epoch, 1, "revision_a", "fictional")
    async def materialize(self, view, sink, cancel):
        await self.barrier("materialize")
        async def chunks():
            yield b"fictional-state"
        await sink.write("state.bin", chunks())
    async def release(self, ticket):
        (self.root / "released").write_text("yes")
"""


async def finish_stop(stopping, clock):
    """Drive injected retry time; wall time still bounds local process scheduling."""
    started = clock.monotonic()

    def finished():
        if not stopping.done():
            clock.advance(0.01)
            assert clock.monotonic() - started < 1, "stop exceeded bounded retry-time progress"
        return stopping.done()

    await until(finished)


def live_home(tmp_path, phase):
    home, source = home_with_importer(tmp_path)
    barrier = source.parent
    plugin(
        home,
        "snapshot-live",
        SOURCE,
        backup={"mode": "snapshot"},
        contributes={"backup": {"snapshot": {"factory": "backup"}}},
        config={
            "type": "object",
            "additionalProperties": False,
            "properties": {"barrier": {"type": "string"}, "phase": {"type": "string"}},
        },
    )
    path = home / "companions/companion_a/companion.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["plugins"].pop("fictional-importer")
    config["plugins"]["snapshot-live"] = {
        "enabled": True,
        "config": {"barrier": str(barrier), "phase": phase},
    }
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return home, barrier


@pytest.mark.parametrize(
    "phase,stop_source",
    [
        ("prepare", "cancel"),
        ("materialize", "cancel"),
        ("prepare", "eof"),
        ("materialize", "wait_escalate"),
    ],
)
async def test_real_backup_child_capture_and_parallel_stop(tmp_path_factory, phase, stop_source):
    home, barrier = live_home(tmp_path_factory.mktemp("b"), phase)
    clock, launcher = MockClock(), ProcessLauncher(home)
    svc = Supervisor(home, ["companion_a", "companion_b"], launcher, clock)
    errors = []
    original_request = svc.request

    async def observe_request(*args, **kwargs):
        try:
            return await original_request(*args, **kwargs)
        except Exception as exc:
            errors.append(getattr(exc, "code", type(exc).__name__))
            raise

    svc.request = observe_request
    api = Management(svc, load_home(home))
    server = Server(home, clock, api, svc.audit, {"port": 0})
    stopping = None
    operation_id = uuid4().hex
    try:
        await svc.start()
        await until(lambda: all(row["state"] == "running" for row in svc.rows.values()))
        await server.start()
        base = f"http://127.0.0.1:{server.port}/api/v1"
        headers = {}
        headers["Authorization"] = "Bearer " + server.auth.token
        async with ClientSession(headers=headers) as client:
            async with client.post(
                base + "/backup/create",
                json={
                    "scope": "instance",
                    "operation_id": operation_id,
                },
            ) as response:
                assert response.status == 200, await response.text()
            await until(lambda: (barrier / (phase + ".pid")).exists() or bool(errors))
            assert errors == []
            assert (
                int((barrier / (phase + ".pid")).read_text())
                == svc.children["companion_a"].process.pid
            )
            assert svc.children["companion_a"].process.pid != os.getpid()
            assert api.backup_workers.processes[operation_id].pid != os.getpid()
            async with client.get(base + "/backup/operations/" + operation_id) as response:
                assert response.status == 200
                assert (await response.json())["result"]["phase"] == "capturing"
            stopping = asyncio.create_task(
                FrontendCommands(svc).receive(None)
                if stop_source == "eof"
                else svc.stop("wait" if stop_source == "wait_escalate" else "cancel")
            )
            await until(lambda: svc.rows["companion_b"]["state"] == "stopped")
            assert svc.children["companion_b"].process.poll() == 0
            if stop_source == "wait_escalate":
                from clytie.backup.journal import BackupJournal
                from clytie.plugin_host.locking import file_lock

                assert api.backup_workers.cancel_since is None
                assert not stopping.done()
                journal_path = BackupJournal.path_for(home, operation_id)
                # Real OS lock contention deterministically puts the parent's
                # cancel task into its injected 10 ms retry, as can happen when
                # the real worker records cancellation at the same instant.
                with file_lock(journal_path.with_suffix(".lock"), {"operation": "test_cancel"}):
                    await svc.escalate()
                    await clock.drain(64)
                    assert api.backup_workers._cancel_tasks
                    assert all(not task.done() for task in api.backup_workers._cancel_tasks)
            (barrier / "release").write_text("yes")
            if stop_source == "wait_escalate":
                await until(lambda: not api.backup_workers.tasks)
                assert api.backup_workers.query(operation_id)["phase"] == "cancelled"
                assert (barrier / "released").exists()
                # Worker and provider have finished, but the parent retry cannot
                # resume on wall time alone while MockClock remains frozen.
                assert not stopping.done()
                assert clock.monotonic() == 0
                await clock.advance_and_drain(0.01)
                await until(stopping.done)
                assert clock.monotonic() == 0.01
            await finish_stop(stopping, clock)
            await stopping
            assert api.backup_workers.query(operation_id)["phase"] == "cancelled"
            assert (barrier / "released").exists()
            assert not api.backup_workers.tasks
    finally:
        (barrier / "release").write_text("yes")
        if stopping is None:
            stopping = asyncio.create_task(svc.stop("cancel"))
        await finish_stop(stopping, clock)
        await stopping
        await server.close()
        close_exited_launcher(launcher)


async def test_migration_releases_instance_gate_after_ledger_lock(tmp_path):
    from clytie.kernel.migration import LedgerRuntime
    from clytie.kernel.query import lock_held

    from .test_kernel_migration import old_ledger

    clock = MockClock()
    old_ledger(tmp_path, clock)
    entered, release = asyncio.Event(), asyncio.Event()

    async def migrate(db, cancel):
        entered.set()
        await release.wait()

    first = LedgerRuntime(tmp_path, "companion_a", clock, migrations={1: migrate})
    second = LedgerRuntime(tmp_path, "companion_b", clock)
    opening = asyncio.create_task(first.open())
    try:
        await entered.wait()
        await second.open()
        assert lock_held(first.directory / ".lock")
        assert lock_held(second.directory / ".lock")
        assert not opening.done()
    finally:
        release.set()
        await opening
        first.close()
        second.close()
