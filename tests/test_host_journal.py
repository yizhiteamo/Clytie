"""崩溃后仅凭磁盘日志重建宿主；只使用临时、手写虚构状态。"""

import asyncio
from pathlib import Path

import pytest

from clytie.api import local_status
from clytie.plugin_host import PlanStale
from clytie.plugin_host.snapshots import SnapshotError, atomic_write, canonical

from .host_helpers import enable, host, plugin
from .test_host_upgrades import MIGRATE, prepared, update


class Crash(BaseException):
    """绕过正常异常回滚，模拟进程在指定磁盘动作后退出。"""


async def interrupted(tmp_path):
    h, path, old = await prepared(tmp_path)
    update(path, script=MIGRATE)
    plan = h.plan_migration("companion_a", "stateful")
    h.snapshots.create("companion_a", "stateful", "1", "2", path=tmp_path / plan.snapshot)
    record = h.journals.begin(
        plan,
        executor=h.operations.executor,
        target_version={"plugin_version": "2.0.0", "data_version": 2},
    )
    await old.storage.set("value", "fictional-partial-migration")
    return h, path, old, record


@pytest.mark.parametrize("point", ["before_script", "before_commit", "after_commit", "after_clear"])
async def test_crash_at_migration_commit_boundaries(tmp_path, monkeypatch, point):
    h, path, old = await prepared(tmp_path)
    update(path, script=MIGRATE)
    plan = h.plan_migration("companion_a", "stateful")
    save, write, clear = h.journals.save, h._record_version, h.journals.clear

    def crash_save(record):
        save(record)
        if record["step"] == "migrations/to_2.py":
            raise Crash()

    def crash_write(*args):
        if point == "after_commit":
            write(*args)
        raise Crash()

    def crash_clear(*args):
        clear(*args)
        raise Crash()

    if point == "before_script":
        monkeypatch.setattr(h.journals, "save", crash_save)
    elif point == "after_clear":
        monkeypatch.setattr(h.journals, "clear", crash_clear)
    else:
        monkeypatch.setattr(h, "_record_version", crash_write)
    with pytest.raises(Crash):
        await h.apply_migration(plan.plan_id)
    restarted = host(tmp_path)
    outcome = await restarted.apply(restarted.plan_enable("companion_a", "stateful").plan_id)
    if point in {"before_script", "before_commit"}:
        assert outcome["failed"] == ["stateful"]
        assert restarted.runtimes["companion_a", "stateful"].status == "migration_failed"
        assert await old.storage.get("value") == "fictional-original"
        assert restarted._version("companion_a", "stateful")["data_version"] == 1
    else:
        assert outcome["enabled"] == ["stateful"]
        assert await old.storage.get("value") == "fictional-upgraded"
        assert restarted._version("companion_a", "stateful")["data_version"] == 2
        if point == "after_commit":
            audit = (
                restarted.journals.path("companion_a", "stateful")
                .with_suffix(".audit.jsonl")
                .read_text()
            )
            assert "committed_journal_removed" in audit
    assert not restarted.journals.path("companion_a", "stateful").exists()
    await restarted.close()


@pytest.mark.parametrize("version", [None, 3])
async def test_migrating_with_missing_or_unexpected_version_blocks_without_recovery(
    tmp_path, monkeypatch, version
):
    h, _, old, record = await interrupted(tmp_path)
    versions = (
        {}
        if version is None
        else {"stateful": {"data_version": version, "plugin_version": "3.0.0"}}
    )
    atomic_write(h._versions_path("companion_a"), canonical(versions))
    restarted = host(tmp_path)

    def forbidden(*args):
        raise AssertionError("ambiguous journal must not automatically restore")

    monkeypatch.setattr(restarted.journals, "restore", forbidden)
    for _ in range(2):
        result = await restarted.apply(restarted.plan_enable("companion_a", "stateful").plan_id)
        assert result["failed"] == ["stateful"]
        assert restarted.runtimes["companion_a", "stateful"].status == "recovery_incomplete"
    assert await old.storage.get("value") == "fictional-partial-migration"
    assert restarted.journals.read("companion_a", "stateful") == record
    with pytest.raises(ValueError, match="recovery_incomplete"):
        restarted.plan_migration("companion_a", "stateful")


async def test_failed_automatic_restore_is_persistent_and_never_retried(tmp_path, monkeypatch):
    h, _, old, record = await interrupted(tmp_path)
    (tmp_path / record["snapshot"]).write_bytes(b"fictional-corrupt-snapshot")
    restarted = host(tmp_path)
    await restarted.apply(restarted.plan_enable("companion_a", "stateful").plan_id)
    record = restarted.journals.read("companion_a", "stateful")
    assert record["phase"] == "restoring" and record["error"]
    again = host(tmp_path)

    def forbidden(*args):
        raise AssertionError("restoring journal forbids automatic retry")

    monkeypatch.setattr(again.journals, "restore", forbidden)
    await again.apply(again.plan_enable("companion_a", "stateful").plan_id)
    assert again.runtimes["companion_a", "stateful"].status == "recovery_incomplete"
    assert await old.storage.get("value") == "fictional-partial-migration"
    errors = local_status(tmp_path).to_dict()["failed_plugin_state"]["recovery_errors"]
    assert errors[0]["level"] == "error" and errors[0]["snapshot"] == record["snapshot"]
    assert set(errors[0]["locations"]) == {"state", "failed", "temporary"}
    assert errors[0]["actions"] == ["accept_current"]


async def test_missing_existing_state_is_never_recreated(tmp_path):
    h, path, old = await prepared(tmp_path)
    retained = old.storage.path.with_name("state-retained")
    old.storage.path.rename(retained)
    restarted = host(tmp_path)
    await restarted.apply(restarted.plan_enable("companion_a", "stateful").plan_id)
    assert restarted.runtimes["companion_a", "stateful"].status == "private_state_missing"
    assert not old.storage.path.exists() and retained.is_dir()
    update(path, script=MIGRATE)
    with pytest.raises(ValueError, match="private_state_missing"):
        restarted.plan_migration("companion_a", "stateful")
    assert not old.storage.path.exists()


@pytest.mark.parametrize("move", ["failed", "restored"])
async def test_restore_crash_after_directory_move_can_resume_only_after_confirmation(
    tmp_path, monkeypatch, move
):
    h, _, old, record = await interrupted(tmp_path)
    rename = Path.rename

    def crash_rename(path, destination):
        result = rename(path, destination)
        if (move == "failed" and path == old.storage.path) or (
            move == "restored" and path.name.startswith(".restore-")
        ):
            raise Crash()
        return result

    monkeypatch.setattr(Path, "rename", crash_rename)
    with pytest.raises(Crash):
        h.journals.restore(record)
    monkeypatch.setattr(Path, "rename", rename)
    restarted = host(tmp_path)
    await restarted.apply(restarted.plan_enable("companion_a", "stateful").plan_id)
    assert restarted.runtimes["companion_a", "stateful"].status == "recovery_incomplete"
    saved = restarted.journals.read("companion_a", "stateful")
    failed = tmp_path / saved["locations"]["failed"]["path"]
    assert failed.is_dir()
    with pytest.raises(SnapshotError, match="unfinished"):
        restarted.plan_cleanup([failed.relative_to(tmp_path).as_posix()])
    before = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")}
    plan = restarted.plan_recovery("companion_a", "stateful", "retry_restore")
    assert {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")} == before
    if move == "restored":
        assert plan.matches_snapshot and "activate_snapshot" not in plan.steps
    else:
        assert "reuse_verified_temporary" in plan.steps and "retain_failed_state" not in plan.steps
    await restarted.apply_recovery(plan.plan_id)
    assert await old.storage.get("value") == "fictional-original"
    assert failed.is_dir() and not restarted.journals.path("companion_a", "stateful").exists()
    assert restarted._version("companion_a", "stateful")["data_version"] == 1
    audit = saved["locations"]["state"]["path"]
    assert audit  # 日志中的位置保持实例相对路径。
    rows = (
        restarted.journals.path("companion_a", "stateful").with_suffix(".audit.jsonl").read_text()
    )
    assert "retry_restore_confirmed" in rows and "restore_completed" in rows


@pytest.mark.parametrize("damage", ["different", "corrupt_snapshot", "equal"])
async def test_accept_current_requires_confirmation_and_reports_comparison(tmp_path, damage):
    h, _, old, record = await interrupted(tmp_path)
    record["phase"] = "restoring"
    h.journals.save(record)
    if damage == "corrupt_snapshot":
        (tmp_path / record["snapshot"]).write_bytes(b"fictional-corrupt")
    elif damage == "equal":
        await old.storage.set("value", "fictional-original")
    before = h._versions_path("companion_a").read_bytes()
    plan = h.plan_recovery("companion_a", "stateful", "accept_current")
    assert (
        plan.matches_snapshot
        == ({"different": False, "corrupt_snapshot": None, "equal": True}[damage])
    )
    assert bool(plan.warning) == (damage != "equal")
    assert h.journals.path("companion_a", "stateful").exists()
    await h.apply_recovery(plan.plan_id)
    assert h._versions_path("companion_a").read_bytes() == before
    assert not h.journals.path("companion_a", "stateful").exists()
    assert (
        "current_directory_accepted"
        in h.journals.path("companion_a", "stateful").with_suffix(".audit.jsonl").read_text()
    )


@pytest.mark.parametrize("change", ["journal", "state", "snapshot", "failed", "temporary"])
async def test_recovery_plan_invalidated_by_changed_journal_or_related_data(tmp_path, change):
    h, _, old, record = await interrupted(tmp_path)
    record["phase"] = "restoring"
    h.journals.save(record)
    plan = h.plan_recovery("companion_a", "stateful", "retry_restore")
    if change == "journal":
        record["error"] = "fictional-update"
        h.journals.save(record)
    elif change == "state":
        await old.storage.set("extra", "fictional-update")
    elif change == "snapshot":
        (tmp_path / record["snapshot"]).write_bytes(b"fictional-update")
    else:
        directory = tmp_path / record["locations"][change]["path"]
        directory.mkdir()
        (directory / "fictional.txt").write_text("fictional-update")
    with pytest.raises(PlanStale):
        await h.apply_recovery(plan.plan_id)
    assert h.journals.path("companion_a", "stateful").exists()


async def test_recovery_block_preserves_uninstall_and_other_plugins(tmp_path):
    h, _, _, record = await interrupted(tmp_path)
    record["phase"] = "restoring"
    h.journals.save(record)
    plugin(tmp_path, "required", dependencies={"stateful": {"version": ">=1"}})
    plugin(tmp_path, "optional", dependencies={"stateful": {"version": ">=1", "optional": True}})
    await enable(h, "optional")
    result = await h.apply(h.plan_enable("companion_a", "required").plan_id)
    assert result["failed"] == ["stateful", "required"]
    assert h.is_active("companion_a", "optional")
    assert h.states["companion_a"]["required"]["dependency"] == "stateful"
    assert any(
        e.name == "plugin.disabled" and e.payload.get("reason") == "recovery_incomplete"
        for e in h.events
    )
    await h.apply(h.plan_uninstall("companion_a", "stateful").plan_id)
    assert h.journals.path("companion_a", "stateful").exists()
    await h.close()


async def test_retry_failure_keeps_block_and_redecrypts_partial_temporary(tmp_path, monkeypatch):
    from clytie.plugin_host.recovery import RecoveryError

    h, _, old, record = await interrupted(tmp_path)
    record["phase"] = "restoring"
    h.journals.save(record)
    temporary = tmp_path / record["locations"]["temporary"]["path"]
    temporary.mkdir()
    (temporary / "fictional-partial.txt").write_text("fictional")
    failed = tmp_path / record["locations"]["failed"]["path"]
    failed.mkdir()
    (failed / "fictional-retained.txt").write_text("fictional")
    plan = h.plan_recovery("companion_a", "stateful", "retry_restore")
    rename = Path.rename

    def denied(path, destination):
        raise OSError("fictional rename failure")

    monkeypatch.setattr(Path, "rename", denied)
    with pytest.raises(RecoveryError):
        await h.apply_recovery(plan.plan_id)
    assert h.runtimes["companion_a", "stateful"].status == "recovery_incomplete"
    assert h.journals.read("companion_a", "stateful")["phase"] == "restoring"
    assert temporary.is_dir() and failed.is_dir()
    monkeypatch.setattr(Path, "rename", rename)
    retry = h.plan_recovery("companion_a", "stateful", "retry_restore")
    assert "reuse_verified_temporary" in retry.steps
    await h.apply_recovery(retry.plan_id)
    assert await old.storage.get("value") == "fictional-original"
    assert temporary.is_dir() and failed.is_dir()
    assert failed.with_name(failed.name + "-1").is_dir()


async def test_accept_missing_directory_and_preexisting_cleanup_plan_are_rejected(tmp_path):
    h, _, old, record = await interrupted(tmp_path)
    failed = tmp_path / record["locations"]["failed"]["path"]
    old.storage.path.rename(failed)
    with pytest.raises(ValueError, match="unavailable"):
        h.plan_recovery("companion_a", "stateful", "accept_current")
    # 清理计划在日志出现前生成，执行时仍必须重新检查引用。
    journal = h.journals.path("companion_a", "stateful")
    saved = journal.read_bytes()
    journal.unlink()
    plan = h.plan_cleanup([failed.relative_to(tmp_path).as_posix()])
    journal.write_bytes(saved)
    with pytest.raises(SnapshotError, match="unfinished"):
        h.apply_cleanup(plan.plan_id)
    assert failed.is_dir()


async def test_success_commits_only_current_plugin_once(tmp_path, monkeypatch):
    h, path, _ = await prepared(tmp_path)
    plugin(tmp_path, "other")
    await enable(h, "other")
    before = h._version("companion_a", "other")
    update(path, script=MIGRATE)
    write = h._record_version
    calls = []

    def count(cid, manifest):
        calls.append((cid, manifest.id))
        write(cid, manifest)

    monkeypatch.setattr(h, "_record_version", count)
    result = await h.apply_migration(h.plan_migration("companion_a", "stateful").plan_id)
    assert result["outcome"] == "migrated"
    assert calls == [("companion_a", "stateful")]
    assert h._version("companion_a", "other") == before
    await h.close()


@pytest.mark.parametrize("failure", ["write", "verify"])
async def test_production_restore_reports_positions_on_temporary_failures(
    tmp_path, monkeypatch, failure
):
    import clytie.plugin_host.journal as journal_module
    from clytie.plugin_host.recovery import RecoveryError

    h, _, old, record = await interrupted(tmp_path)
    write, tree = journal_module.atomic_write, h.snapshots._tree

    def fail_write(path, data):
        if any(part.startswith(".restore-") for part in path.parts):
            raise OSError("fictional temporary write failure")
        write(path, data)

    def fail_verify(path):
        if path.name.startswith(".restore-"):
            raise ValueError("fictional temporary verification failure")
        return tree(path)

    if failure == "write":
        monkeypatch.setattr(journal_module, "atomic_write", fail_write)
    else:
        monkeypatch.setattr(h.snapshots, "_tree", fail_verify)
    with pytest.raises(RecoveryError) as error:
        h.journals.restore(record)
    assert set(error.value.locations) == {"state", "failed", "temporary"}
    assert h.journals.read("companion_a", "stateful")["phase"] == "restoring"
    assert await old.storage.get("value") == "fictional-partial-migration"


async def test_failure_never_rewrites_versions_of_any_plugin(tmp_path, monkeypatch):
    h, path, _ = await prepared(tmp_path)
    plugin(tmp_path, "other")
    await enable(h, "other")
    update(path, script=MIGRATE + "    await ctx.clock.sleep(100)\n")
    writes = []
    write = h._record_version

    def counted(cid, manifest):
        writes.append(manifest.id)
        write(cid, manifest)

    monkeypatch.setattr(h, "_record_version", counted)
    task = asyncio.create_task(
        h.apply_migration(h.plan_migration("companion_a", "stateful").plan_id)
    )
    for _ in range(50):
        if h.clock.pending_sleep_count:
            break
        await h.clock.sleep(0)
    # 同一伴侣中其他插件的记录更新不能被恢复流程覆盖。
    versions = h._versions("companion_a")
    versions["other"]["plugin_version"] = "2.0.0"
    atomic_write(h._versions_path("companion_a"), canonical(versions))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writes == []
    assert h._version("companion_a", "other")["plugin_version"] == "2.0.0"
    assert h._version("companion_a", "stateful")["data_version"] == 1
    await h.close()
