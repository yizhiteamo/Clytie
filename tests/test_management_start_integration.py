"""Both OS-lock orders through real Companion.start and production management workers."""

import asyncio
import os
from uuid import uuid4

import pytest

from clytie.api.auth import APIError
from clytie.api.management import Management
from clytie.api.management_journal import Journal
from clytie.config import load_home
from clytie.memory.transfer import runtime_lock
from clytie.plugin_host.locking import OperationBusy, _owner
from clytie.sdk.testing import MockClock
from clytie.supervisor.common import read
from clytie.supervisor.processes import ProcessLauncher
from clytie.supervisor.service import Supervisor

from .management_integration_helpers import BarrierLauncher, arguments, home_with_importer, until


def memory_owner(home):
    fd = os.open(home / "companions/companion_a/memory/.lock", os.O_RDONLY)
    try:
        return _owner(fd)
    finally:
        os.close(fd)


@pytest.mark.parametrize("first", ["management", "companion"])
async def test_real_start_and_management_both_lock_orders(tmp_path, first):
    home, source = home_with_importer(tmp_path)
    clock = MockClock()
    launcher = BarrierLauncher(home) if first == "companion" else ProcessLauncher(home)
    svc = Supervisor(home, ["companion_a"], launcher, clock)
    api = Management(svc, load_home(home))
    workers = api.memory_workers
    key = uuid4().hex
    call = None
    try:
        if first == "management":
            call = asyncio.create_task(
                workers.execute("companion_a", "import/plan", arguments(source), key)
            )
            await until(lambda: (source.parent / "entered").exists())
            worker_pid = int((source.parent / "entered").read_text().strip())
            assert worker_pid != os.getpid()
            assert memory_owner(home)["pid"] == worker_pid
            await svc.start()
            await until(lambda: svc.rows["companion_a"]["state"] == "blocked")
            assert svc.rows["companion_a"]["restart_reason"] == "lock_held"
            assert memory_owner(home)["pid"] == worker_pid
            with pytest.raises(APIError, match="management_busy"):
                await workers.execute("companion_a", "import/plan", arguments(source))
            assert (await workers.execute("companion_a", "import/plan", arguments(source), key))[
                "state"
            ] == "preparing"
            (source.parent / "release").touch()
            await call
            await svc.start_companion("companion_a")
        else:
            await svc.start()
            await until(lambda: (home / "startup-held.json").exists())
            identity = read(home / "startup-held.json")
            assert identity == svc.children["companion_a"].identity()
            assert memory_owner(home)["pid"] == identity["pid"]
            with pytest.raises(APIError, match="management_busy"):
                await workers.execute("companion_a", "import/plan", arguments(source), key)
            assert not workers.tasks and not workers.status()
            assert not (source.parent / "entered").exists()
            (home / "startup-release").touch()
        await until(lambda: svc.rows["companion_a"]["state"] == "running")
        child = svc.children["companion_a"]
        assert memory_owner(home)["pid"] == child.process.pid
        with pytest.raises(OperationBusy):
            with runtime_lock(home, "companion_a"):
                pytest.fail("second independent writer")
        # Normal online management remains allowed and executes in the owner,
        # rather than creating a second independent management worker.
        (source.parent / "release").touch()
        online_id = uuid4().hex
        result = await api.call(
            "/companions/<cid>/memory/import/plan",
            {"cid": "companion_a"},
            {**arguments(source), "operation_id": online_id},
            {},
            "POST",
        )
        assert result["id"]
        journal = Journal.load(
            home / "companions/companion_a/management/operations" / (online_id + ".json"), clock
        )
        assert journal.value["executor"] == child.identity()
        assert journal.value["committed"]
        before = (source.parent / "entered").read_text()
        await workers.execute("companion_a", "import/plan", arguments(source), online_id)
        assert (source.parent / "entered").read_text() == before
        pids = [int(v) for v in before.splitlines()]
        assert pids == (
            [worker_pid, child.process.pid] if first == "management" else [child.process.pid]
        )
    finally:
        (home / "startup-release").touch()
        (source.parent / "release").touch()
        if call:
            await asyncio.gather(call, return_exceptions=True)
        if svc.lock:
            await svc.stop("cancel")
        for child in launcher.children:
            await child.kill()
        launcher.close()
