"""第 5 轮：元数据短锁、独立关闭、解除选项和生产日志证据。"""

import asyncio
import multiprocessing
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from clytie.plugin_host.journal import recovery_status
from clytie.plugin_host.locking import file_lock, process_identity
from clytie.plugin_host.recovery import RecoveryError

from .host_helpers import enable, host, plugin
from .test_host_journal import interrupted
from .test_host_lifecycle import pump
from .test_host_upgrades import MIGRATE, prepared, update


def hold_metadata(path, pipe):
    with file_lock(
        path,
        {
            **process_identity(),
            "operation": "migration" if "plugin-recovery" in Path(path).parts else "version_record",
        },
    ):
        pipe.send(True)
        assert pipe.recv() == "release"
        threading.Event().wait(0.2)


@contextmanager
def child_lock(path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=hold_metadata, args=(path, child))
    process.start()
    child.close()
    try:
        assert parent.poll(20)
        parent.recv()
        yield parent
    finally:
        parent.close()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(10)
        assert process.exitcode == 0


async def test_migration_commit_waits_for_other_process_and_never_rolls_back(tmp_path, monkeypatch):
    h, path, old = await prepared(tmp_path)
    update(path, script=MIGRATE + "    await ctx.clock.sleep(100)\n")
    task = asyncio.create_task(
        h.apply_migration(h.plan_migration("companion_a", "stateful").plan_id)
    )
    await pump(h.clock, lambda: h.clock.pending_sleep_count > 0)
    lock = tmp_path / "companions/companion_a/plugin-versions.lock"
    with child_lock(lock, monkeypatch) as pipe:
        assert h._version("companion_a", "stateful")["data_version"] == 1
        write = h._record_version

        def release_then_write(*args):
            pipe.send("release")
            write(*args)

        monkeypatch.setattr(h, "_record_version", release_then_write)
        h.clock.advance(100)
        assert (await task)["outcome"] == "migrated"
    assert not h.journals.path("companion_a", "stateful").exists()
    restarted = host(tmp_path)
    await enable(restarted, "stateful")
    assert restarted._version("companion_a", "stateful")["data_version"] == 2
    assert await old.storage.get("value") == "fictional-upgraded"
    await restarted.close()
    await h.close()


async def test_start_read_write_competition_without_journal(tmp_path, monkeypatch):
    h, _, _ = await prepared(tmp_path)
    lock = tmp_path / "companions/companion_a/plugin-versions.lock"
    with child_lock(lock, monkeypatch) as pipe:
        with h.operations.hold("companion_a", ["stateful"], "load"):
            assert h._before_load("companion_a", "stateful") == (None, None)
        write = h._record_version

        def release_then_write(*args):
            pipe.send("release")
            write(*args)

        monkeypatch.setattr(h, "_record_version", release_then_write)
        assert (await enable(h, "stateful")).status == "active"
    assert not h.journals.path("companion_a", "stateful").exists()
    await h.close()


async def test_commit_io_failure_matches_runtime_states_and_next_load(tmp_path, monkeypatch):
    h, path, old = await prepared(tmp_path)
    update(path, script=MIGRATE)

    def fail(*args):
        raise OSError("fictional version write failure")

    monkeypatch.setattr(h, "_record_version", fail)
    result = await h.apply_migration(h.plan_migration("companion_a", "stateful").plan_id)
    assert result["outcome"] == h.runtimes["companion_a", "stateful"].status == "migration_failed"
    assert h.states["companion_a"]["stateful"]["status"] == "migration_failed"
    assert "下次加载将按 4.3 节自动恢复" in result["reason"]
    restarted = host(tmp_path)
    await restarted.apply(restarted.plan_enable("companion_a", "stateful").plan_id)
    assert restarted.runtimes["companion_a", "stateful"].status == "migration_failed"
    assert await old.storage.get("value") == "fictional-original"


@pytest.mark.parametrize("busy_running", [False, True])
async def test_close_companions_independently_and_ignore_nonrunning(
    tmp_path, monkeypatch, busy_running
):
    h, _, _ = await prepared(tmp_path)
    plugin(tmp_path, "running")
    h.configure("companion_b")
    h.configure("companion_c")
    await enable(h, "running")
    await enable(h, "running", "companion_b")
    pid = "running" if busy_running else "stateful"
    lock = tmp_path / "companions/companion_a/plugin-recovery" / (pid + ".lock")
    with child_lock(lock, monkeypatch) as pipe:
        result = await h.close()
        assert result["companion_c"] == {"stopped": [], "not_stopped": {}}
        assert result["companion_b"] == {"stopped": ["running"], "not_stopped": {}}
        if busy_running:
            assert result["companion_a"]["stopped"] == []
            assert result["companion_a"]["not_stopped"]["running"]["operation"] == "migration"
        else:
            assert result["companion_a"] == {"stopped": ["running"], "not_stopped": {}}
        pipe.send("release")
    await h.close()


@pytest.mark.parametrize(
    "shape",
    [
        "migrating",
        "restoring",
        "corrupt",
        "resolution_only",
        "committed_accept",
        "committed_retry",
        "bad_snapshot",
        "missing_state",
        "missing_version",
    ],
)
async def test_status_actions_and_plan_use_identical_eligibility(tmp_path, shape):
    h, _, old, record = await interrupted(tmp_path)
    choice = None
    if shape == "corrupt":
        h.journals.path("companion_a", "stateful").write_bytes(b"fictional-invalid")
        choice = 1
    else:
        if shape != "migrating":
            record["phase"] = "restoring"
        if shape in {"resolution_only", "committed_accept", "committed_retry"}:
            record["resolution"] = {
                "operation": "retry_restore" if shape == "committed_retry" else "accept_current",
                "status": "written" if shape.startswith("committed") else "failed",
                "version_after": record["previous_version"],
                "choice": None,
            }
        if shape == "resolution_only":
            record["resolution_only"] = True
            record["locations"] = {"state": record["locations"]["state"]}
            record["snapshot"] = None
            choice = 1
        if shape == "bad_snapshot":
            (tmp_path / record["snapshot"]).write_bytes(b"fictional-corrupt")
        if shape == "missing_state":
            old.storage.path.rename(old.storage.path.with_name("fictional-retained"))
        if shape == "missing_version":
            h._versions_path("companion_a").write_bytes(b"{}")
        h.journals.save(record)
    row = recovery_status(tmp_path)[0]
    if shape in {"corrupt", "resolution_only"}:
        assert row["actions"] == ["accept_current"]
        assert "需要输入数据版本" in row["action_details"]["accept_current"]["label"]
    for action in ("retry_restore", "accept_current"):
        selected = "old" if shape == "missing_version" and action == "accept_current" else choice
        if action in row["actions"]:
            assert (
                h.plan_recovery(
                    "companion_a", "stateful", action, version_choice=selected
                ).operation
                == action
            )
        else:
            with pytest.raises(ValueError):
                h.plan_recovery("companion_a", "stateful", action, version_choice=selected)


async def test_real_migration_failure_journal_identity_and_resolution(tmp_path, monkeypatch):
    import clytie.plugin_host.journal as module

    h, path, old = await prepared(tmp_path)
    previous = h._version("companion_a", "stateful")
    update(path, script=MIGRATE + '    raise ValueError("fictional failure")\n')
    write = module.atomic_write

    def fail_temporary(path, data):
        if any(part.startswith(".restore-") for part in path.parts):
            raise OSError("fictional restore write error")
        return write(path, data)

    monkeypatch.setattr(module, "atomic_write", fail_temporary)
    with pytest.raises(RecoveryError):
        await h.apply_migration(h.plan_migration("companion_a", "stateful").plan_id)
    record = h.journals.read("companion_a", "stateful")
    assert record["previous_version"] == previous == {"plugin_version": "1.0.0", "data_version": 1}
    assert record["target_version"] == {"plugin_version": "2.0.0", "data_version": 2}
    assert record["executor"] == process_identity() == h.operations.executor
    assert record["phase"] == "restoring"
    monkeypatch.setattr(module, "atomic_write", write)
    plan = h.plan_recovery("companion_a", "stateful", "retry_restore")
    await h.apply_recovery(plan.plan_id)
    assert await old.storage.get("value") == "fictional-original"
    assert not h.journals.path("companion_a", "stateful").exists()


async def test_metadata_timeout_is_explicit_not_start_or_recovery_failure(tmp_path, monkeypatch):
    from clytie.plugin_host.locking import MetadataLockTimeout

    h, _, _ = await prepared(tmp_path)

    def timeout(*args):
        raise MetadataLockTimeout("fictional timeout")

    monkeypatch.setattr(h, "_record_version", timeout)
    with pytest.raises(MetadataLockTimeout):
        await h.apply(h.plan_enable("companion_a", "stateful").plan_id)
    assert h.runtimes["companion_a", "stateful"].status == "disabled"
    assert not h.journals.path("companion_a", "stateful").exists()
    assert h.failed_state_status()["recovery_errors"] == []


async def test_atomic_version_reads_allow_concurrent_replacements(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    h, _, _ = await prepared(tmp_path)
    ready, stop = threading.Event(), threading.Event()

    def read():
        count = 0
        ready.set()
        while not stop.is_set():
            assert h._version("companion_a", "stateful")["data_version"] in {1, 2}
            count += 1
        return count

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(read)
        assert ready.wait(5)
        try:
            for n in range(20):
                h._write_version(
                    "companion_a",
                    "stateful",
                    {"plugin_version": "1.0.0", "data_version": 1 + n % 2},
                )
        finally:
            stop.set()
        assert future.result(5) > 0


async def test_error_after_version_commit_never_promises_or_performs_rollback(
    tmp_path, monkeypatch
):
    h, path, old = await prepared(tmp_path)
    update(path, script=MIGRATE)
    write = h._record_version

    def fail_after_write(*args):
        write(*args)
        raise OSError("fictional unlock diagnostic failure")

    monkeypatch.setattr(h, "_record_version", fail_after_write)
    result = await h.apply_migration(h.plan_migration("companion_a", "stateful").plan_id)
    assert result["outcome"] == "migration_failed"
    assert "version committed" in result["reason"]
    restarted = host(tmp_path)
    await enable(restarted, "stateful")
    assert await old.storage.get("value") == "fictional-upgraded"
    assert restarted._version("companion_a", "stateful")["data_version"] == 2
    await restarted.close()
