"""真实加密资产恢复在物化中及提交前强杀后的核验。"""

import json
import queue
import shutil
import subprocess
import sys
import threading

import pytest
import yaml

from clytie.assets import AssetStore
from clytie.backup import restore
from clytie.backup.container import pack
from clytie.backup.repository import SourceRepository
from clytie.backup.source import capture_instance
from clytie.memory.store import MemoryStore

from .host_helpers import plugin
from .test_backup_assets_layout import private
from .test_backup_source import ROOT, fictional


async def asset_plan(tmp_path):
    home, clock = fictional(tmp_path)
    first = home / "companions/companion_a"
    second = home / "companions/companion_b"
    shutil.copytree(first, second)
    memory = MemoryStore(second / "memory/memory.sqlite3", clock=clock, config={}, instance={})
    with memory.transaction():
        memory.set_meta("companion_uid", "b" * 32)
    memory.close()
    config_path = home / "clytie.local.yaml"
    config = yaml.safe_load(config_path.read_bytes())
    config["companions"].append("companion_b")
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    store = AssetStore(home, clock)
    data = b"fictional asset chunk\n" * (110 * 1024)
    refs = [private(store, cid, data)[0] for cid in config["companions"]]
    plugin(
        home,
        "asset-config",
        backup={"mode": "stateless"},
        config={
            "type": "object",
            "properties": {"artifact": {"type": "string", "x-clytie-asset": True}},
            "additionalProperties": False,
        },
    )
    for cid in config["companions"]:
        directory = home / "companions" / cid
        document = yaml.safe_load((directory / "companion.yaml").read_bytes())
        document["id"] = cid
        document["channels"] = []
        document["plugins"]["asset-config"] = {
            "enabled": True,
            "config": {"artifact": refs[0].asset_id},
        }
        (directory / "companion.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
        versions = json.loads((directory / "plugin-versions.json").read_bytes())
        metadata = json.loads((home / "plugins/asset-config/plugin.json").read_bytes())
        versions["asset-config"] = {
            "plugin_version": metadata["version"],
            "data_version": metadata.get("data", {}).get("version", 1),
        }
        (directory / "plugin-versions.json").write_text(json.dumps(versions), encoding="utf-8")
    stage = tmp_path / "stage"
    captured = await capture_instance(
        home, clock, stage, source_instance_id="fictional", epoch="asset_crash"
    )
    assert captured["manifest"]["completeness"] == "complete", captured["missing"]
    assert len(captured["manifest"]["assets"]) == 1
    asset = captured["manifest"]["assets"][0]
    assert len(asset["registrations"]) == 2
    assert set(asset["references"]) == {"companion_a", "companion_b"}
    packages = tmp_path / "packages"
    packages.mkdir()
    package, key = packages / "fictional.clytie-backup", tmp_path / "fictional.key"
    key.write_bytes(b"k" * 32)
    pack(stage, package, key.read_bytes(), captured["manifest"])
    repository = SourceRepository.initialize(packages, clock, owner_instance_id="fictional")
    repository.register(
        dict(backup_id="fictional", path=str(package), identity=repository.identity(package))
    )
    manager = restore.RestoreManager(tmp_path / "maintenance", clock, reserve_bytes=0)
    manager.previews.bind_source(
        package,
        dict(root=str(packages), repository_id=repository.repository_id, backup_id="fictional"),
        confirmed=True,
    )
    work = tmp_path / "work"
    work.mkdir()
    plan = manager.plan(package, key, tmp_path / "restored", work)
    members = {row["path"] for row in plan["envelope"]["files"]}
    extra = [row["path"] for row in asset["registrations"] if row["path"] not in members]
    assert len(extra) == 1
    assert plan["space"]["target_bytes"] == sum(
        row["size"] for row in plan["envelope"]["files"]
    ) + len(data)
    return manager, plan, store, refs, data, extra[0]


SCRIPT = """
import sys
from pathlib import Path
from clytie.backup import assets, restore
from clytie.sdk.testing import MockClock

manager = restore.RestoreManager(Path(sys.argv[1]), MockClock(), reserve_bytes=0)
plan_id, point, extra = sys.argv[2:]
original = assets.materialize

def pause():
    print('checkpoint', flush=True)
    sys.stdin.readline()
    raise AssertionError('parent must kill the child at the checkpoint')

def materialize(root, envelope, **kwargs):
    def checkpoint():
        path = Path(root) / extra
        if point == 'partial' and path.exists() and path.stat().st_size >= 1024 * 1024:
            pause()
    result = original(root, envelope, check=checkpoint)
    pause()
    return result

assets.materialize = materialize
manager.apply(plan_id)
"""


def kill_at_materialization(manager, plan, point, extra):
    child = subprocess.Popen(
        [sys.executable, "-c", SCRIPT, str(manager.home), plan["id"], point, extra],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines = queue.Queue()
    reader = threading.Thread(target=lambda: lines.put(child.stdout.readline()), daemon=True)
    reader.start()
    try:
        assert lines.get(timeout=30).strip() == "checkpoint"
        child.kill()
        child.wait(timeout=10)
        assert child.returncode != 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        reader.join(timeout=10)
        for pipe in (child.stdin, child.stdout, child.stderr):
            pipe.close()


@pytest.mark.parametrize("point", ["partial", "complete", "tampered"])
async def test_real_kill_asset_materialization_recovery_checks_all_private_copies(
    tmp_path, monkeypatch, point
):
    manager, plan, source, refs, data, extra = await asset_plan(tmp_path)
    target = tmp_path / "restored"
    source_registry = source.snapshot().registrations
    package_hash = restore.digest(plan["source"])
    kill_at_materialization(manager, plan, point, extra)
    assert restore.read_state(target)["data_state"] == "verifying"
    for maintenance in (False, True):
        with pytest.raises(restore.RestoreError, match="restore_recovery_blocked"):
            restore.assert_start_allowed(target, maintenance=maintenance)
    if point == "partial":
        assert 0 < (target / extra).stat().st_size < len(data)
    else:
        assert (target / extra).read_bytes() == data
    if point == "tampered":
        (target / extra).chmod(0o600)
        with (target / extra).open("r+b") as stream:
            stream.write(b"X")
        assert (target / extra).stat().st_size == len(data)
    before = (target / extra).read_bytes()

    def no_unpack(*args, **kwargs):
        pytest.fail("recovery must verify the target, never replay package extraction")

    monkeypatch.setattr(restore, "unpack", no_unpack)
    restarted = restore.RestoreManager(manager.home, manager.clock, reserve_bytes=0)
    if point != "complete":
        with pytest.raises(ValueError, match="restore_asset_invalid"):
            restarted.recover(plan["id"])
        assert restore.read_state(target)["data_state"] == "recovery_blocked"
        assert restarted.recover(plan["id"])["data_state"] == "recovery_blocked"
        assert (target / extra).read_bytes() == before
        for maintenance in (False, True):
            with pytest.raises(restore.RestoreError, match="restore_recovery_blocked"):
                restore.assert_start_allowed(target, maintenance=maintenance)
    else:
        state = restarted.recover(plan["id"])
        assert state["data_state"] == "ready_for_switch"
        assert state["source_release"] == "released"
        assert restarted.recover(plan["id"]) == state
        restored = AssetStore(target, manager.clock)
        assert restored.snapshot().registrations == source_registry
        for cid, ref in zip(("companion_a", "companion_b"), refs):
            assert await restored.bind(cid).read(ref.asset_id) == data
            assert await restored.bind(cid).info(ref.asset_id) == ref
            assert (target / f"companions/{cid}/assets/fictional.bin").stat().st_nlink == 1
        with pytest.raises(KeyError):
            await restored.bind("companion_c").read(refs[0].asset_id)
        restore.assert_start_allowed(target, maintenance=True)
        with pytest.raises(restore.RestoreError, match="restore_source_unconfirmed"):
            restore.assert_start_allowed(target)
    assert restore.digest(plan["source"]) == package_hash
    assert source.snapshot().registrations == source_registry
    for cid, ref in zip(("companion_a", "companion_b"), refs):
        assert await source.bind(cid).read(ref.asset_id) == data
