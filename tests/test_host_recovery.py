"""保留式恢复与确认清理；故障目录从不自动删除。"""

from pathlib import Path

import pytest

from clytie.api import local_status
from clytie.plugin_host import PlanStale
from clytie.plugin_host.recovery import Cleanup, RecoveryError, failed_status
from clytie.plugin_host.snapshots import SnapshotError, UpgradeSnapshots
from clytie.plugin_host.support import HostLog
from clytie.sdk.testing import MockClock

from .test_host_upgrades import MIGRATE, prepared, update


def failed_tree(tmp_path):
    manager = UpgradeSnapshots(tmp_path, MockClock(), HostLog())
    state = manager.state_path("companion_a", "worker")
    state.mkdir(parents=True)
    (state / "original.txt").write_bytes(b"fictional-original")
    snapshot = manager.create("companion_a", "worker", "1", "2")
    (state / "original.txt").write_bytes(b"fictional-corrupted")
    folder = state / "many"
    folder.mkdir()
    for index in range(128):
        (folder / f"file-{index}.txt").write_bytes(b"fictional-new-file")
    return manager, state, snapshot


def test_restore_preserves_entire_fault_tree_without_any_deletion(tmp_path, monkeypatch):
    manager, state, snapshot = failed_tree(tmp_path)
    original = manager._tree(state)

    def reject(*args, **kwargs):
        raise AssertionError("restore must never delete")

    monkeypatch.setattr(Path, "unlink", reject)
    monkeypatch.setattr(Path, "rmdir", reject)
    failed = manager.restore(snapshot, "companion_a", "worker")
    assert manager._tree(failed) == original
    assert (state / "original.txt").read_bytes() == b"fictional-original"
    status = failed_status(tmp_path)
    assert status["directories"][0]["files"] == 129
    assert status["total_bytes"] == len(b"fictional-corrupted") + 128 * len(b"fictional-new-file")
    # 配置无效时仍通过离线 API 持续提醒，不需要启动插件。
    assert local_status(tmp_path).to_dict()["failed_plugin_state"] == status
    assert not any("removed" in entry[1] for entry in manager.log.entries)


@pytest.mark.parametrize("stage", ["write", "verify", "move_failed", "move_restored"])
def test_restore_stops_at_each_failure_and_reports_all_locations(tmp_path, monkeypatch, stage):
    manager, state, snapshot = failed_tree(tmp_path)
    original = manager._tree(state)
    import clytie.plugin_host.recovery as recovery

    rename, tree = Path.rename, manager._tree

    def fail_write(*args):
        raise OSError("fictional-write-failure")

    def fail_tree(path):
        if path.name.startswith(".restore-"):
            return {"files": {}, "directories": []}
        return tree(path)

    moves = []

    def fail_rename(path, target):
        moves.append((path, target))
        if (stage == "move_failed" and path == state) or (
            stage == "move_restored" and path.name.startswith(".restore-")
        ):
            raise OSError("fictional-rename-failure")
        return rename(path, target)

    if stage == "write":
        monkeypatch.setattr(recovery, "atomic_write", fail_write)
    if stage == "verify":
        monkeypatch.setattr(manager, "_tree", fail_tree)
    monkeypatch.setattr(Path, "rename", fail_rename)
    with pytest.raises(RecoveryError) as raised:
        manager.restore(snapshot, "companion_a", "worker")
    locations = raised.value.locations
    assert all((tmp_path / value).is_relative_to(tmp_path) for value in locations.values() if value)
    if stage == "move_restored":
        assert not state.exists()
        assert tree(tmp_path / locations["failed"]) == original
    else:
        assert tree(state) == original
    assert (tmp_path / locations["temporary"]).is_dir()
    assert len(moves) == {"write": 0, "verify": 0, "move_failed": 1, "move_restored": 2}[stage]


def test_cleanup_requires_exact_read_only_plan_and_records_each_deletion(tmp_path):
    manager, state, snapshot = failed_tree(tmp_path)
    failed = manager.restore(snapshot, "companion_a", "worker")
    clean = Cleanup(tmp_path, manager.log)
    name = failed.relative_to(tmp_path).as_posix()
    tree = manager._tree(failed)
    plan = clean.plan([name])
    assert manager._tree(failed) == tree and plan.files == 129
    with pytest.raises(PlanStale):
        clean.apply("unknown")
    clean.apply(plan.plan_id)
    assert not failed.exists() and state.is_dir()
    removed = [e for e in manager.log.entries if e[1] == "plugin.failed_state_removed"]
    assert len(removed) == 131  # 文件、子目录及故障根目录。
    assert not failed_status(tmp_path)["directories"]


@pytest.mark.parametrize("change", ["add", "edit", "remove", "link"])
def test_cleanup_rejects_changed_or_linked_tree_without_deleting(tmp_path, change):
    manager, state, snapshot = failed_tree(tmp_path)
    failed = manager.restore(snapshot, "companion_a", "worker")
    clean = Cleanup(tmp_path, manager.log)
    name = failed.relative_to(tmp_path).as_posix()
    plan = clean.plan([name])
    if change == "add":
        (failed / "extra").write_bytes(b"fictional")
    elif change == "edit":
        (failed / "original.txt").write_bytes(b"fictional-edit")
    elif change == "remove":
        (failed / "original.txt").rename(failed / "renamed.txt")
    else:
        try:
            (failed / "link").symlink_to(state, target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation unavailable on this test host")
    with pytest.raises((PlanStale, SnapshotError)):
        clean.apply(plan.plan_id)
    assert failed.exists()
    assert not any(e[1] == "plugin.failed_state_removed" for e in manager.log.entries)


def test_cleanup_rejects_unplanned_directory_and_mid_execution_file(tmp_path, monkeypatch):
    manager, state, snapshot = failed_tree(tmp_path)
    failed = manager.restore(snapshot, "companion_a", "worker")
    clean = Cleanup(tmp_path, manager.log)
    with pytest.raises(SnapshotError):
        clean.plan([state.relative_to(tmp_path).as_posix()])
    plan = clean.plan([failed.relative_to(tmp_path).as_posix()])
    original = Path.unlink

    def inject(path, *args, **kwargs):
        original(path, *args, **kwargs)
        (failed / "unplanned.txt").write_bytes(b"fictional-new")

    monkeypatch.setattr(Path, "unlink", inject)
    with pytest.raises(PlanStale, match="unexpected"):
        clean.apply(plan.plan_id)
    assert (failed / "unplanned.txt").is_file()
    assert len([e for e in manager.log.entries if e[1] == "plugin.failed_state_removed"]) == 1


async def test_later_success_marks_retained_directories_ready_for_confirmed_cleanup(tmp_path):
    h, path, _ = await prepared(tmp_path)
    update(path, script=MIGRATE + '    raise ValueError("fictional")\n')
    await h.apply_migration(h.plan_migration("companion_a", "stateful").plan_id)
    retained = h.failed_state_status()["directories"]
    assert retained and not retained[0]["resolved"]
    update(path, script=MIGRATE)
    h.clock.advance(1)
    assert (await h.apply_migration(h.plan_migration("companion_a", "stateful").plan_id))[
        "outcome"
    ] == "migrated"
    retained = h.failed_state_status()["directories"]
    assert retained[0]["resolved"]
    plan = h.plan_cleanup([retained[0]["path"]])
    h.apply_cleanup(plan.plan_id)
    assert not h.failed_state_status()["directories"]
    await h.close()
    # 原版本记录保存于插件目录之外。
    assert h._versions_path("companion_a").is_file()
