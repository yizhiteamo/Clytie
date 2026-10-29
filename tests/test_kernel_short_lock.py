"""§15：跨进程短锁等待不得阻塞其他伴侣的 Tick、任务或取消。"""

import asyncio

import pytest

from clytie.kernel.scheduler import Scheduler
from clytie.plugin_host.locking import MetadataLockTimeout, file_lock, process_identity
from clytie.sdk.types import ToolCall

from .host_helpers import host
from .test_host_journal import interrupted
from .test_host_resolution_versions import replace_record
from .test_host_review_five import child_lock
from .test_host_upgrades import MIGRATE, prepared, update
from .test_kernel_service import Capability, new_record, setup


async def test_other_task_tick_and_cancel_progress_during_cross_process_lock(tmp_path, monkeypatch):
    h = host(tmp_path)
    kernel, channel, clock = setup(tmp_path / "other", clock=h.clock, cid="companion_b")
    ticks = []
    scheduler = Scheduler(clock, h.log)

    async def tick(value, cancel):
        ticks.append(clock.monotonic())

    scheduler.add("example", dict(id="tick", every="PT0.01S", handler="tick"), tick)
    r, inv, _ = await new_record(kernel)
    execution = asyncio.create_task(
        kernel.execute(
            r["trace_id"],
            "echo-tool",
            ToolCall(call_id="call", tool="echo", arguments={}),
            Capability(clock, block=True),
            inv,
        )
    )
    lock = tmp_path / "companions/companion_a/plugin-versions.lock"
    with child_lock(lock, monkeypatch) as pipe:
        writer = asyncio.create_task(
            h._write_version("companion_a", "example", dict(plugin_version="1.0.0", data_version=1))
        )
        await clock.advance_and_drain(0.1)
        assert not writer.done() and len(ticks) >= 9
        before = clock.monotonic()
        assert await kernel.cancel("companion_b", inv.actor, "test_group") == "cancelled"
        assert clock.monotonic() == before and (await execution).status == "cancelled"
        await clock.advance_and_drain(5)
        with pytest.raises(MetadataLockTimeout):
            await writer
        assert h._version("companion_a", "example") is None
        assert ticks[-1] >= 5
        pipe.send("release")
    await scheduler.close()
    await kernel.reactions.drain()
    await kernel.bus.drain()
    kernel.ledger.close()
    await h.close()


async def test_cancel_while_migration_commit_waits_restores_before_propagating(tmp_path):
    h, path, old = await prepared(tmp_path)
    update(path, script=MIGRATE)
    plan = h.plan_migration("companion_a", "stateful")
    with file_lock(
        tmp_path / "companions/companion_a/plugin-versions.lock",
        {**process_identity(), "operation": "version_record"},
    ):
        task = asyncio.create_task(h.apply_migration(plan.plan_id))
        await h.clock.drain(80)
        assert not task.done()
        assert await old.storage.get("value") == "fictional-upgraded"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await old.storage.get("value") == "fictional-original"
        assert h._version("companion_a", "stateful")["data_version"] == 1
        assert h.runtimes["companion_a", "stateful"].status == "migration_failed"
    await h.close()


async def test_recovery_wait_cancel_retains_failure_choice_and_audit(tmp_path):
    h, _, _, _ = await interrupted(tmp_path)
    replace_record(h, None)
    plan = h.plan_recovery("companion_a", "stateful", "accept_current", version_choice="target")
    with file_lock(
        tmp_path / "companions/companion_a/plugin-versions.lock",
        {**process_identity(), "operation": "version_record"},
    ):
        task = asyncio.create_task(h.apply_recovery(plan.plan_id))
        await h.clock.drain()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        record = h.journals.read("companion_a", "stateful")
        assert record["resolution"]["status"] == "failed"
        assert not record["resolution"]["committed"]
        assert h._version("companion_a", "stateful") is None
    next_plan = h.plan_recovery("companion_a", "stateful", "accept_current", version_choice="old")
    await h.apply_recovery(next_plan.plan_id)
    assert h._version("companion_a", "stateful")["data_version"] == 1
    await h.close()
