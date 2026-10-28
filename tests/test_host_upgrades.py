"""就地升级与确认计划的回归，只用临时实例。"""

import asyncio
import json

import pytest

from clytie.plugin_host import PlanStale
from clytie.sdk import Cancelled, CancelToken

from .host_helpers import SOURCE, enable, host, plugin

MIGRATE = """async def migrate(ctx, cancel):
    assert not hasattr(ctx, "services")
    assert not hasattr(ctx, "identity")
    assert not hasattr(ctx, "memory")
    await ctx.storage.set("value", "fictional-upgraded")
    (ctx.storage.path / "new.txt").write_bytes(b"fictional-new-file")
"""


def update(path, *, version="2.0.0", data_version=2, script=None, source=None):
    data = json.loads(path.read_bytes())
    data["version"] = version
    data["data"] = {"version": data_version, "on_uninstall": "keep"}
    path.write_text(json.dumps(data), encoding="utf-8")
    if source is not None:
        (path.parent / "plugin.py").write_text(source, encoding="utf-8")
    if script is not None:
        folder = path.parent / "migrations"
        folder.mkdir(exist_ok=True)
        (folder / f"to_{data_version}.py").write_text(script, encoding="utf-8")


async def prepared(tmp_path):
    path = plugin(
        tmp_path, "stateful", hot_reload=True, data={"version": 1, "on_uninstall": "keep"}
    )
    h = host(tmp_path)
    old = await enable(h, "stateful")
    await old.storage.set("value", "fictional-original")
    await h.apply(h.plan_disable("companion_a", "stateful").plan_id)
    return h, path, old


@pytest.mark.parametrize("failure", [None, "migration", "start", "cancel"])
async def test_in_place_migration_success_or_preserved_failure(tmp_path, failure):
    h, path, old = await prepared(tmp_path)
    script = MIGRATE
    if failure == "migration":
        script += '    raise ValueError("fictional-failure")\n'
    if failure == "cancel":
        script += '    cancel.cancel("contract-test")\n    cancel.raise_if_cancelled()\n'
    source = (
        SOURCE.replace("self.ctx = ctx", 'raise ValueError("fictional-start-failure")')
        if failure == "start"
        else SOURCE
    )
    update(path, script=script, source=source)
    result = await h.apply(h.plan_enable("companion_a", "stateful").plan_id)
    assert result["failed"] == ["stateful"] and result["patch"]["plugins"] == {}
    assert h.runtimes["companion_a", "stateful"].status == "pending_migration"
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    plan = h.plan_migration("companion_a", "stateful")
    assert (plan.from_version, plan.to_version, plan.scripts) == (1, 2, ("migrations/to_2.py",))
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == before
    code = h.snapshots._tree(path.parent)
    if failure == "cancel":
        with pytest.raises(Cancelled):
            await h.apply_migration(plan.plan_id, CancelToken())
    else:
        result = await h.apply_migration(plan.plan_id)
        assert result["outcome"] == ("migrated" if failure is None else "migration_failed")
    assert h.snapshots._tree(path.parent) == code
    assert h.runtimes["companion_a", "stateful"].status == (
        "active" if failure is None else "migration_failed"
    )
    assert await old.storage.get("value") == (
        "fictional-upgraded" if failure is None else "fictional-original"
    )
    assert h._version("companion_a", "stateful")["data_version"] == (2 if failure is None else 1)
    retained = h.failed_state_status()["directories"]
    assert bool(retained) == (failure is not None)
    if failure:
        assert (tmp_path / retained[0]["path"] / "new.txt").read_bytes() == b"fictional-new-file"
        assert not retained[0]["resolved"]
        assert ("companion_a", "stateful") not in h.unit_configs
    await h.close()


@pytest.mark.parametrize("change", ["code", "state", "new_file", "version"])
async def test_plan_invalidated_by_code_or_private_state_changes(tmp_path, change):
    h, path, old = await prepared(tmp_path)
    update(path, script=MIGRATE)
    plan = h.plan_migration("companion_a", "stateful")
    if change == "code":
        (path.parent / "plugin.py").write_text(SOURCE + "\n# changed\n", encoding="utf-8")
    elif change == "state":
        await old.storage.set("new", "fictional-change")
    elif change == "new_file":
        (old.storage.path / "new.txt").write_bytes(b"fictional-change")
    else:
        update(path, version="2.1.0")
    with pytest.raises(PlanStale):
        await h.apply_migration(plan.plan_id)
    assert not (tmp_path / "secrets/backup.key").exists()


async def test_version_only_then_repeated_migration_after_process_restart(tmp_path):
    h, path, _ = await prepared(tmp_path)
    update(path, version="1.1.0", data_version=1)
    await enable(h, "stateful")
    assert any(e.name == "plugin.updated" for e in h.events)
    assert not (tmp_path / "backups").exists()
    await h.close()
    for version in (2, 3):
        update(path, version=f"{version}.0.0", data_version=version, script=MIGRATE)
        restarted = host(tmp_path)
        restarted.clock.advance(version)
        result = await restarted.apply_migration(
            restarted.plan_migration("companion_a", "stateful").plan_id
        )
        assert result["outcome"] == "migrated"
        assert restarted._version("companion_a", "stateful")["data_version"] == version
        await restarted.close()


async def test_missing_step_downgrade_and_verification_failure(tmp_path, monkeypatch):
    h, path, old = await prepared(tmp_path)
    update(path)
    await h.apply(h.plan_enable("companion_a", "stateful").plan_id)
    assert h.runtimes["companion_a", "stateful"].status == "missing_migration"
    with pytest.raises(ValueError, match="missing_migration"):
        h.plan_migration("companion_a", "stateful")
    update(path, script=MIGRATE)
    plan = h.plan_migration("companion_a", "stateful")

    def fail(*args):
        raise ValueError("snapshot verification failed")

    monkeypatch.setattr(h.snapshots, "read", fail)
    with pytest.raises(ValueError, match="verification"):
        await h.apply_migration(plan.plan_id)
    assert await old.storage.get("value") == "fictional-original"
    h._record_version("companion_a", h.manifests["stateful"])
    update(path, version="1.0.0", data_version=1)
    await h.apply(h.plan_enable("companion_a", "stateful").plan_id)
    assert h.runtimes["companion_a", "stateful"].status == "data_newer_than_code"


async def test_failure_does_not_freeze_ordinary_plugin_configuration(tmp_path):
    h, path, _ = await prepared(tmp_path)
    update(path, script=MIGRATE + '    raise ValueError("fictional")\n')
    await h.apply_migration(h.plan_migration("companion_a", "stateful").plan_id)
    update(path, version="1.0.0", data_version=1)
    await enable(h, "stateful")
    assert ("companion_a", "stateful") not in h.unit_configs
    assert (await h.reconfigure("companion_a", "stateful", {}))["outcome"] == "applied"
    await h.close()


async def test_external_cancel_restores_private_state(tmp_path):
    h, path, old = await prepared(tmp_path)
    update(path, script=MIGRATE + "    await ctx.clock.sleep(100)\n")
    task = asyncio.create_task(
        h.apply_migration(h.plan_migration("companion_a", "stateful").plan_id)
    )
    for _ in range(30):
        await h.clock.sleep(0)
        if (old.storage.path / "new.txt").exists():
            break
    assert (old.storage.path / "new.txt").exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await old.storage.get("value") == "fictional-original"
    assert h.runtimes["companion_a", "stateful"].status == "migration_failed"
    assert h._version("companion_a", "stateful")["data_version"] == 1


async def test_uninstall_retains_code_private_state_and_version(tmp_path):
    h, path, runtime = await prepared(tmp_path)
    await h.apply(h.plan_uninstall("companion_a", "stateful").plan_id)
    assert path.is_file() and runtime.storage.path.is_dir()
    assert h._versions_path("companion_a").is_file()
    restarted = host(tmp_path)
    assert not restarted.plan_enable("companion_a", "stateful").ok
    restarted.restore_available("stateful")
    restored = await enable(restarted, "stateful")
    assert await restored.storage.get("value") == "fictional-original"
    assert not hasattr(h, "install") and not hasattr(h, "upgrade")
    await restarted.close()


async def test_cancel_during_post_migration_stop_still_restores(tmp_path, monkeypatch):
    """回归：不能在 rollback 的 stop 被取消后跳过快照恢复。"""
    h, path, old = await prepared(tmp_path)
    source = (
        SOURCE
        + """
    async def stop(self):
        await self.ctx.storage.set("stopping", "fictional")
        await self.ctx.clock.sleep(100)
"""
    )
    update(path, source=source, script=MIGRATE)
    cancel = CancelToken()
    start = h._start

    async def cancel_after_start(*args, **kwargs):
        result = await start(*args, **kwargs)
        cancel.cancel("fictional-cancel-after-start")
        return result

    monkeypatch.setattr(h, "_start", cancel_after_start)
    task = asyncio.create_task(
        h.apply_migration(h.plan_migration("companion_a", "stateful").plan_id, cancel)
    )
    for _ in range(80):
        if await old.storage.get("stopping"):
            break
        await h.clock.sleep(0)
    assert await old.storage.get("stopping") == "fictional"
    task.cancel()
    await h.clock.sleep(0)
    task.cancel()
    h.clock.advance(10)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await old.storage.get("value") == "fictional-original"
    assert await old.storage.get("stopping") is None
    assert h.runtimes["companion_a", "stateful"].status == "migration_failed"
    assert h._version("companion_a", "stateful")["data_version"] == 1
    assert len(h.failed_state_status()["directories"]) == 1


async def test_shared_code_companions_migrate_and_recover_independently(tmp_path):
    h, path, first = await prepared(tmp_path)
    h.configure("companion_b")
    second = await enable(h, "stateful", "companion_b")
    await second.storage.set("value", "fictional-second-original")
    await h.apply(h.plan_disable("companion_b", "stateful").plan_id)
    update(
        path,
        script=MIGRATE
        + '    if ctx.companion_id == "companion_a":\n        raise ValueError("fictional")\n',
    )
    first_plan = h.plan_migration("companion_a", "stateful")
    second_plan = h.plan_migration("companion_b", "stateful")
    assert (await h.apply_migration(first_plan.plan_id))["outcome"] == "migration_failed"
    assert (await h.apply_migration(second_plan.plan_id))["outcome"] == "migrated"
    assert await first.storage.get("value") == "fictional-original"
    assert await second.storage.get("value") == "fictional-upgraded"
    assert h._version("companion_a", "stateful")["data_version"] == 1
    assert h._version("companion_b", "stateful")["data_version"] == 2
    await h.close()
