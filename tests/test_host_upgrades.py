import json

import pytest

from clytie.sdk import Cancelled, CancelToken

from .host_helpers import SOURCE, enable, host, plugin


@pytest.mark.parametrize("failure", [None, "migration", "start", "cancel"])
async def test_upgrade_verified_snapshot_success_or_exact_rollback(tmp_path, failure):
    old_path = plugin(tmp_path, "stateful", data={"version": 1, "on_uninstall": "keep"})
    h = host(tmp_path)
    old = await enable(h, "stateful")
    await old.ctx.storage.set("value", "fictional-original")
    (old.storage.path / "extra.txt").write_bytes(b"fictional-original-file")
    staged = tmp_path / "incoming"
    new_source = (
        SOURCE
        if failure != "start"
        else SOURCE.replace("self.ctx = ctx", 'raise ValueError("fictional-start-failure")')
    )
    new_path = plugin(
        staged, "stateful", new_source, version="2.0.0", data={"version": 2, "on_uninstall": "keep"}
    )
    migrations = new_path.parent / "migrations"
    migrations.mkdir()
    script = """
async def migrate(ctx, cancel):
    cancel.raise_if_cancelled()
    await ctx.storage.set("value", "fictional-upgraded")
    (ctx.storage.path / "extra.txt").write_bytes(b"fictional-upgraded-file")
    (ctx.storage.path / "new.txt").write_bytes(b"fictional-new-file")
"""
    if failure == "migration":
        script += '    raise RuntimeError("fictional-migration-failure")\n'
    if failure == "cancel":
        script += '    cancel.cancel("contract-test")\n    cancel.raise_if_cancelled()\n'
    (migrations / "to_2.py").write_text(script, encoding="utf-8")
    cancel = CancelToken()
    if failure == "cancel":
        with pytest.raises(Cancelled):
            await h.upgrade("companion_a", "stateful", new_path, cancel)
    else:
        result = await h.upgrade("companion_a", "stateful", new_path, cancel)
        assert result["outcome"] == ("upgraded" if failure is None else "restored")
    current = h.runtimes["companion_a", "stateful"]
    assert current.status == "active"
    assert await current.storage.get("value") == (
        "fictional-upgraded" if failure is None else "fictional-original"
    )
    assert (current.storage.path / "new.txt").exists() == (failure is None)
    version = json.loads(h._metadata("companion_a", "stateful.json").read_text(encoding="utf-8"))
    assert version["data_version"] == (2 if failure is None else 1)
    assert json.loads(old_path.read_text(encoding="utf-8"))["version"] == (
        "2.0.0" if failure is None else "1.0.0"
    )
    snapshots = list((tmp_path / "backups/plugin-upgrades/companion_a/stateful").glob("*.enc"))
    assert len(snapshots) == 1
    assert b"fictional-original" not in snapshots[0].read_bytes()
    await h.close()


async def test_missing_migration_or_failed_verification_never_runs_migration(tmp_path, monkeypatch):
    plugin(tmp_path, "stateful", data={"version": 1, "on_uninstall": "keep"})
    h = host(tmp_path)
    old = await enable(h, "stateful")
    await old.storage.set("value", "fictional-original")
    new_path = plugin(
        tmp_path / "incoming",
        "stateful",
        version="2.0.0",
        data={"version": 2, "on_uninstall": "keep"},
    )
    with pytest.raises(ValueError, match="missing migration"):
        await h.upgrade("companion_a", "stateful", new_path)
    migrations = new_path.parent / "migrations"
    migrations.mkdir()
    (migrations / "to_2.py").write_text(
        'async def migrate(ctx, cancel):\n    await ctx.storage.set("value", "fictional-changed")\n',
        encoding="utf-8",
    )

    def fail(*args):
        raise ValueError("snapshot verification failed")

    monkeypatch.setattr(h.snapshots, "read", fail)
    with pytest.raises(ValueError, match="verification"):
        await h.upgrade("companion_a", "stateful", new_path)
    assert old.status == "active" and await old.storage.get("value") == "fictional-original"
    await h.close()


async def test_uninstall_persists_exclusion_but_retains_private_state_and_version(tmp_path):
    path = plugin(tmp_path, "stateful", data={"version": 1, "on_uninstall": "keep"})
    h = host(tmp_path)
    runtime = await enable(h, "stateful")
    await runtime.storage.set("value", "fictional-original")
    await h.apply(h.plan_uninstall("companion_a", "stateful").plan_id)
    assert runtime.status == "disabled" and runtime.storage.path.is_dir()
    assert h._metadata("companion_a", "stateful.json").is_file()
    assert not h.plan_enable("companion_a", "stateful").ok
    restarted = host(tmp_path)
    assert not restarted.plan_enable("companion_a", "stateful").ok
    restarted.install(path)
    restored = await enable(restarted, "stateful")
    assert await restored.storage.get("value") == "fictional-original"
    await restarted.close()
