"""只在临时目录、离线子进程中验证 OS 锁与崩溃接管。"""

import asyncio
import json
import multiprocessing
import os
from pathlib import Path

import pytest

from clytie.plugin_host.locking import OperationBusy, file_lock, process_identity
from clytie.plugin_host.support import HostLog

from .host_helpers import SOURCE, host, plugin
from .test_host_lifecycle import pump
from .test_host_upgrades import MIGRATE, prepared, update


def hold_in_child(path, connection):
    owner = {**process_identity(), "operation": "migration"}
    with file_lock(path, owner):
        connection.send(owner)
        command = connection.recv()
        if command == "crash":
            os._exit(0)


@pytest.mark.parametrize("ending", ["release", "crash"])
def test_cross_process_exclusion_and_recovery_after_owner_exits(tmp_path, ending, monkeypatch):
    # pytest 的 importlib 模式不添加仓库根路径；spawn 必须能重新导入目标模块。
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    path = tmp_path / "stateful.lock"
    process = context.Process(target=hold_in_child, args=(path, child))
    process.start()
    child.close()
    try:
        assert parent.poll(20), "child did not acquire lock"
        owner = parent.recv()
        assert owner["pid"] == process.pid and owner["started_at"]
        with pytest.raises(OperationBusy) as conflict:
            with file_lock(path, {**process_identity(), "operation": "load"}):
                pytest.fail("cross-process lock was bypassed")
        assert conflict.value.owner == owner
        parent.send(ending)
        process.join(20)
        assert process.exitcode == 0
        log = HostLog()
        with file_lock(path, {**process_identity(), "operation": "load"}, log):
            with path.open("rb", buffering=0) as stream:
                stream.seek(1)
                assert json.loads(stream.read())["pid"] == os.getpid()
        assert path.is_file()
        assert any(row[1] == "plugin.lock_taken_over" for row in log.entries) == (ending == "crash")
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(10)


def test_same_process_lock_conflict_does_not_release_owner(tmp_path):
    path = tmp_path / "stateful.lock"
    owner = {**process_identity(), "operation": "migration"}
    with file_lock(path, owner):
        for _ in range(2):
            with pytest.raises(OperationBusy):
                with file_lock(path, owner):
                    pytest.fail("same-process lock bypass")
    with file_lock(path, owner):
        pass


async def test_reload_and_load_during_migration_do_not_read_journal(tmp_path, monkeypatch):
    h, path, old = await prepared(tmp_path)
    update(path, script=MIGRATE + "    await ctx.clock.sleep(100)\n")
    plan = h.plan_migration("companion_a", "stateful")
    task = asyncio.create_task(h.apply_migration(plan.plan_id))
    await pump(h.clock, lambda: h.clock.pending_sleep_count > 0)
    journal = h.journals.path("companion_a", "stateful")
    before = (
        journal.read_bytes(),
        h._versions_path("companion_a").read_bytes(),
        h.snapshots._tree(old.storage.path),
    )
    other = host(tmp_path)

    def forbidden(*args):
        pytest.fail("busy load read recovery journal")

    monkeypatch.setattr(h.journals, "read", forbidden)
    monkeypatch.setattr(other.journals, "read", forbidden)
    for caller in (h, other):
        assert (await caller.reconfigure("companion_a", "stateful"))[
            "outcome"
        ] == "migration_in_progress"
        assert (await caller.apply(caller.plan_enable("companion_a", "stateful").plan_id))[
            "outcome"
        ] == "migration_in_progress"
    assert before == (
        journal.read_bytes(),
        h._versions_path("companion_a").read_bytes(),
        h.snapshots._tree(old.storage.path),
    )
    monkeypatch.undo()
    h.clock.advance(100)
    assert (await task)["outcome"] == "migrated"
    await h.close()


async def test_ordinary_conflict_returns_owner_and_retry_revalidates_plan(tmp_path):
    source = SOURCE.replace("await ctx.clock.sleep(0)", "await ctx.clock.sleep(2)", 1)
    plugin(tmp_path, "slow", source, hot_reload=True)
    h = host(tmp_path)
    plan = h.plan_enable("companion_a", "slow")
    task = asyncio.create_task(h.apply(plan.plan_id))
    await pump(h.clock, lambda: h.clock.pending_sleep_count > 0)
    result = await h.reconfigure("companion_a", "slow")
    assert result == {"outcome": "operation_in_progress", "operation": "enable"}
    h.clock.advance(2)
    assert (await task)["enabled"] == ["slow"]
    from clytie.plugin_host import PlanStale

    with pytest.raises(PlanStale):
        await h.apply(plan.plan_id)
    await h.close()


async def test_multi_plugin_lock_failure_releases_all_without_partial_start(tmp_path):
    plugin(tmp_path, "alpha")
    plugin(tmp_path, "zeta", dependencies={"alpha": {"version": ">=1"}})
    h, other = host(tmp_path), host(tmp_path)
    plan = h.plan_enable("companion_a", "zeta")
    with other.operations.hold("companion_a", ["zeta"], "reload"):
        result = await h.apply(plan.plan_id)
        assert result == {"outcome": "operation_in_progress", "operation": "reload"}
        assert not h.runtimes and not (tmp_path / "companions/companion_a/plugin-data").exists()
        with other.operations.hold("companion_a", ["alpha"], "load"):
            pass
    assert (await h.apply(plan.plan_id))["enabled"] == ["alpha", "zeta"]
    await h.close()


async def test_overlapping_multi_plugin_operations_never_wait_for_each_other(tmp_path):
    h, other = host(tmp_path), host(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def first():
        with h.operations.hold("companion_a", ["zeta", "alpha"], "disable"):
            entered.set()
            await release.wait()

    task = asyncio.create_task(first())
    await entered.wait()
    with pytest.raises(OperationBusy):
        with other.operations.hold("companion_a", ["alpha", "zeta"], "enable"):
            pytest.fail("overlapping operation entered")
    release.set()
    await task
    with other.operations.hold("companion_a", ["alpha", "zeta"], "enable"):
        pass
