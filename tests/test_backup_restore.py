import hashlib
import json
from importlib.metadata import version

import pytest

from clytie.backup import restore
from clytie.backup.container import ContainerError, pack, unpack
from clytie.kernel.ledger import Ledger
from clytie.memory.store import MemoryStore
from clytie.sdk.testing import MockClock


def test_owned_marker_is_exact_and_not_archived(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_text("fictional")
    package = tmp_path / "package"
    pack(source, package, b"k" * 32, {})
    target = tmp_path / "target"
    (target / "supervisor").mkdir(parents=True)
    marker = target / "supervisor/restore.json"
    marker.write_text("incomplete")
    from clytie.backup.container import owned_marker

    proof = owned_marker(target, marker)
    unpack(package, target, b"k" * 32, owned_marker=proof)
    assert marker.read_text() == "incomplete"
    assert (target / "data").read_text() == "fictional"


def test_owned_marker_rejects_extra_file(tmp_path):
    from clytie.backup.container import owned_marker

    target = tmp_path / "target"
    target.mkdir()
    marker = target / "marker"
    marker.write_text("incomplete")
    proof = owned_marker(target, marker)
    (target / "unknown").write_text("unknown")
    with pytest.raises(ContainerError):
        unpack(tmp_path / "missing", target, b"k" * 32, owned_marker=proof)


def fixture_package(tmp_path, monkeypatch, *, mutation=None):
    clock = MockClock()
    root = tmp_path / "staged"
    memory = root / "companions/companion_a/memory/memory.sqlite3"
    ledger = root / "companions/companion_a/ledger/ledger.sqlite3"
    store = MemoryStore(memory, clock=clock, config={}, instance={})
    store.set_meta("companion_uid", "a" * 32)
    store.db.execute(
        "INSERT INTO recollect_progress VALUES('*', ?)", (json.dumps(dict(inbound=0, intent=0)),)
    )
    store.close()
    book = Ledger(ledger.resolve(), "companion_a", clock)
    book.close()
    (root / "clytie.local.yaml").write_text("companions: [companion_a]\n")
    files = {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }
    manifest = dict(
        schema_version=1,
        scope="instance",
        completeness="complete",
        source_instance_id="fictional-instance",
        code_version=version("clytie"),
        companions=[
            dict(
                id="companion_a",
                uid="a" * 32,
                memory=memory.relative_to(root).as_posix(),
                ledger=ledger.relative_to(root).as_posix(),
            )
        ],
        consistency_groups=[dict(epoch="test-epoch", members=list(files), sha256=files)],
        assets=[],
        plugins=[],
        credential_exclusions=[],
        source_registration=dict(machine_id="different-machine", home="unavailable", identity={}),
    )
    if mutation:
        mutation(root, manifest)
        files = {
            p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*")
            if p.is_file()
        }
        manifest["consistency_groups"] = [
            dict(epoch="test-epoch", members=list(files), sha256=files)
        ]
    (tmp_path / "packages").mkdir()
    package = tmp_path / "packages/source.clytie-backup"
    pack(root, package, b"k" * 32, manifest)
    key = tmp_path / "key"
    key.write_bytes(b"k" * 32)
    monkeypatch.setattr(
        restore,
        "validate_restored",
        lambda *a, **k: dict(
            data_valid=True, credential_exclusions=[], configuration=dict(valid=True, errors=[])
        ),
    )
    # This state-machine fixture intentionally omits plugin/configuration files;
    # keep both configuration validators outside its scope. Real configuration
    # and declared assets are exercised by test_backup_assets_integration.
    from clytie.backup import assets

    monkeypatch.setattr(assets, "validate_configuration", lambda *a, **k: None)
    manager = restore.RestoreManager(tmp_path / "management", clock, reserve_bytes=0)
    from clytie.backup.repository import SourceRepository

    repository = SourceRepository.initialize(
        package.parent, clock, owner_instance_id="fictional_source"
    )
    repository.register(
        {
            "backup_id": "fictional_backup",
            "path": str(package),
            "identity": repository.identity(package),
        }
    )
    manager.previews.bind_source(
        package,
        {
            "root": str(package.parent),
            "repository_id": repository.repository_id,
            "backup_id": "fictional_backup",
        },
        confirmed=True,
    )
    work = tmp_path / "workspace"
    work.mkdir()
    target = tmp_path / "target"
    return manager, package, key, target, work


def test_restore_ready_is_separate_from_start_and_idempotent(tmp_path, monkeypatch):
    manager, package, key, target, work = fixture_package(tmp_path, monkeypatch)
    plan = manager.plan(package, key, target, work)
    state = manager.apply(plan["id"])
    assert state["data_state"] == "ready_for_switch"
    assert state["target_instance_id"] != state["source_instance_id"]
    assert manager.apply(plan["id"]) == state
    restore.assert_start_allowed(target, maintenance=True)
    with pytest.raises(restore.RestoreError):
        restore.assert_start_allowed(target)
    restore.source_stopped(target, state["restore_id"], clock=MockClock())
    restore.assert_start_allowed(target)
    with pytest.raises(restore.RestoreError):
        restore.acknowledge(target, state["restore_id"], runtime_check=lambda: {})
    final = restore.acknowledge(
        target,
        state["restore_id"],
        runtime_check=lambda: dict(
            home=str(target.resolve()),
            mode="normal",
            running=True,
            configuration_valid=True,
            restore_id=state["restore_id"],
        ),
        clock=MockClock(),
    )
    assert final["switched"]


@pytest.mark.parametrize("change", ["package", "key", "target"])
def test_plan_bound_to_source_key_and_empty_target(tmp_path, monkeypatch, change):
    manager, package, key, target, work = fixture_package(tmp_path, monkeypatch)
    plan = manager.plan(package, key, target, work)
    if change == "package":
        package.write_bytes(package.read_bytes() + b"extra")
    elif change == "key":
        key.write_bytes(b"x" * 32)
    else:
        target.mkdir()
        (target / "unknown").write_text("not owned")
    with pytest.raises(restore.RestoreError):
        manager.apply(plan["id"])
    assert not (target / "companions").exists()


def test_cancelled_restore_is_incomplete_and_not_replayed(tmp_path, monkeypatch):
    manager, package, key, target, work = fixture_package(tmp_path, monkeypatch)
    plan = manager.plan(package, key, target, work)

    def cancel():
        # Exercise cancellation after target initialization, independently of
        # additional pre-copy hashing checkpoints.
        if (target / restore.MARKER).exists():
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError):
        manager.apply(plan["id"], check=cancel)
    assert restore.read_state(target)["data_state"] == "incomplete"
    monkeypatch.setattr(restore, "unpack", lambda *a, **k: pytest.fail("must not replay"))
    with pytest.raises(restore.RestoreError):
        manager.apply(plan["id"])
    assert manager.recover(plan["id"])["data_state"] == "recovery_blocked"


@pytest.mark.parametrize("damage", ["uid", "progress", "asset", "scope", "version"])
def test_invalid_consistency_never_planned(tmp_path, monkeypatch, damage):
    def mutate(root, manifest):
        if damage == "uid":
            manifest["companions"][0]["uid"] = "b" * 32
        elif damage == "progress":
            import sqlite3

            with sqlite3.connect(root / manifest["companions"][0]["memory"]) as db:
                db.execute(
                    "UPDATE recollect_progress SET data=?",
                    (json.dumps(dict(inbound=99, intent=0)),),
                )
        elif damage == "asset":
            manifest["assets"] = [dict(asset_id="fictional", path="missing", sha256="a" * 64)]
        elif damage == "scope":
            manifest["scope"] = "memory"
        else:
            manifest["code_version"] = "999.0.0"

    manager, package, key, target, work = fixture_package(tmp_path, monkeypatch, mutation=mutate)
    with pytest.raises(restore.RestoreError):
        manager.plan(package, key, target, work)
    assert not target.exists()


def test_restore_marker_copied_to_other_target_is_rejected(tmp_path, monkeypatch):
    manager, package, key, target, work = fixture_package(tmp_path, monkeypatch)
    state = manager.apply(manager.plan(package, key, target, work)["id"])
    other = tmp_path / "other"
    (other / "supervisor").mkdir(parents=True)
    (other / "supervisor/restore.json").write_text(json.dumps(state))
    with pytest.raises(restore.RestoreError):
        restore.assert_start_allowed(other, maintenance=True)


def test_production_source_check_rejects_held_lock(tmp_path):
    from clytie.plugin_host.locking import file_lock, process_identity

    home = tmp_path / "source"
    home.mkdir()
    state = dict(source_registration=restore.source_registration(home), companions=[])
    assert restore.check_source(state)["status"] == "stopped"
    with file_lock(
        home / "run/supervisor.lock", dict(**process_identity(), operation="supervisor")
    ):
        assert restore.check_source(state)["status"] == "active"
    assert restore.check_source(state)["status"] == "stopped"


def test_unreachable_source_requires_explicit_confirmation(tmp_path, monkeypatch):
    manager, package, key, target, work = fixture_package(tmp_path, monkeypatch)
    state = manager.apply(manager.plan(package, key, target, work)["id"])

    def unreachable(_):
        return {"status": "inaccessible"}

    with pytest.raises(restore.RestoreError):
        restore.assert_start_allowed(target, source_check=unreachable)
    restore.source_stopped(target, state["restore_id"], source_check=unreachable, clock=MockClock())
    restore.assert_start_allowed(target, source_check=unreachable)
    with pytest.raises(restore.RestoreError):
        restore.assert_start_allowed(target, source_check=lambda _: {"status": "active"})


def test_missing_plugin_coverage_rejected(tmp_path, monkeypatch):
    def mutate(root, manifest):
        path = root / "companions/companion_a/companion.yaml"
        path.write_text("plugins:\n  fictional-plugin:\n    enabled: true\n")

    manager, package, key, target, work = fixture_package(tmp_path, monkeypatch, mutation=mutate)
    with pytest.raises(restore.RestoreError):
        manager.plan(package, key, target, work)


def test_active_management_operation_rejected(tmp_path, monkeypatch):
    def mutate(root, manifest):
        path = root / "companions/companion_a/management/operations/fictional.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"state":"transaction"}')

    manager, package, key, target, work = fixture_package(tmp_path, monkeypatch, mutation=mutate)
    with pytest.raises(restore.RestoreError):
        manager.plan(package, key, target, work)


@pytest.mark.parametrize("stage", ["copying", "verifying"])
def test_real_kill_recovery_never_replays_unpack(tmp_path, monkeypatch, stage):
    import subprocess

    from clytie.plugin_host.locking import process_identity
    from clytie.supervisor.processes import python_process

    manager, package, key, target, work = fixture_package(tmp_path, monkeypatch)
    plan = manager.plan(package, key, target, work)
    code = """
import json, sys, threading
from pathlib import Path
from clytie.backup import restore
from clytie.plugin_host.locking import process_identity
from clytie.sdk.testing import MockClock
manager = restore.RestoreManager(Path(sys.argv[1]), MockClock(), reserve_bytes=0)
def pause(*args, **kwargs):
    if sys.argv[3] == "copying":
        target = Path(manager._load(sys.argv[2])["target"])
        if not (target / restore.MARKER).exists():
            return
    print(json.dumps({"checkpoint": True, "executor": process_identity()}), flush=True)
    threading.Event().wait()
if sys.argv[3] == "verifying":
    restore.validate_contents = pause
    manager.apply(sys.argv[2])
else:
    manager.apply(sys.argv[2], check=pause)
"""
    executable, env = python_process()
    process = subprocess.Popen(
        [executable, "-c", code, str(manager.home), plan["id"], stage],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        checkpoint = json.loads(process.stdout.readline())
        assert checkpoint["checkpoint"] is True
        assert checkpoint["executor"] == process_identity(process.pid)
        process.kill()
        process.wait(timeout=10)
        with pytest.raises((OSError, ProcessLookupError)):
            process_identity(checkpoint["executor"]["pid"])
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.stdout.close()
        process.stderr.close()
    before = restore.read_state(target)["data_state"]
    assert before == ("incomplete" if stage == "copying" else "verifying")
    monkeypatch.setattr(restore, "unpack", lambda *a, **k: pytest.fail("must not replay"))
    result = manager.recover(plan["id"])
    assert result["data_state"] == (
        "recovery_blocked" if stage == "copying" else "ready_for_switch"
    )


def test_missing_baseline_reports_continuity_without_guessing(tmp_path, monkeypatch):
    def mutate(root, manifest):
        import sqlite3

        with sqlite3.connect(root / manifest["companions"][0]["memory"]) as db:
            db.execute("DELETE FROM recollect_progress")

    manager, package, key, target, work = fixture_package(tmp_path, monkeypatch, mutation=mutate)
    plan = manager.plan(package, key, target, work)
    assert plan["validation"]["continuity"][0]["code"] == "continuity_unverified"
    state = manager.apply(plan["id"])
    assert state["continuity"] == plan["validation"]["continuity"]


def configured_package(tmp_path, monkeypatch, *, missing_credentials=True):
    import shutil
    from pathlib import Path

    import yaml

    from clytie.backup.credentials import credential_exclusions

    repository = Path(__file__).resolve().parents[1]

    def mutate(root, manifest):
        shutil.copytree(repository / "examples/home", root, dirs_exist_ok=True)
        config_path = root / "clytie.local.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if missing_credentials:
            config["accounts"]["example_bot"]["secret"] = "fictional.json"
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        versions = {}
        for pid in ("fake-model", "fake-channel"):
            metadata = json.loads((repository / "plugins" / pid / "plugin.json").read_bytes())
            versions[pid] = dict(
                plugin_version=metadata["version"], data_version=metadata["data"]["version"]
            )
            manifest["plugins"].append(
                dict(
                    companion_id="companion_a",
                    id=pid,
                    version=metadata["version"],
                    data_version=metadata["data"]["version"],
                    manifest_path=f"repository:plugins/{pid}/plugin.json",
                    state_paths=[],
                    version_path="companions/companion_a/plugin-versions.json",
                )
            )
        (root / "companions/companion_a/plugin-versions.json").write_text(json.dumps(versions))
        manifest["credential_exclusions"] = credential_exclusions(root, repo_root=repository)
        manifest["source_registration"] = restore.source_registration(root)

    manager, package, key, target, work = fixture_package(tmp_path, monkeypatch, mutation=mutate)
    monkeypatch.undo()
    return manager, package, key, target, work


def test_real_configuration_credentials_and_plugin_versions(tmp_path, monkeypatch):
    from clytie.config import load_home

    manager, package, key, target, work = configured_package(tmp_path, monkeypatch)
    plan = manager.plan(package, key, target, work)
    assert not plan["validation"]["configuration"]["valid"]
    state = manager.apply(plan["id"])
    assert state["data_state"] == "ready_for_switch"
    assert (target / "companions/companion_a/plugin-data/fake-model").is_dir()
    assert not load_home(target).valid
    assert state["credential_exclusions"]
    restore.source_stopped(target, state["restore_id"], clock=MockClock())
    (target / "secrets").mkdir()
    (target / "secrets/fictional.json").write_text("fictional-only")
    assert load_home(target).valid
    first = restore.source_stopped(target, state["restore_id"], clock=MockClock())
    second = restore.source_stopped(target, state["restore_id"], clock=MockClock())
    assert first["audit"] == second["audit"]


def test_empty_memory_preserves_absent_uid(tmp_path, monkeypatch):
    def mutate(root, manifest):
        import sqlite3

        entry = manifest["companions"][0]
        entry["uid"] = None
        with sqlite3.connect(root / entry["memory"]) as db:
            db.execute("DELETE FROM metadata WHERE key='companion_uid'")

    manager, package, key, target, work = fixture_package(tmp_path, monkeypatch, mutation=mutate)
    state = manager.apply(manager.plan(package, key, target, work)["id"])
    assert state["companions"][0]["uid"] is None
    import sqlite3

    with sqlite3.connect(target / state["companions"][0]["memory"]) as db:
        assert db.execute("SELECT value FROM metadata WHERE key='companion_uid'").fetchone() is None


def test_unreferenced_corrupt_origin_registry_rejected(tmp_path, monkeypatch):
    def mutate(root, manifest):
        import sqlite3

        with sqlite3.connect(root / manifest["companions"][0]["memory"]) as db:
            db.execute(
                "INSERT INTO conversation_registry VALUES(?,?,?,?,?,?)",
                ("forged", "test_group", "fake-channel", "group", "1000000002", "group"),
            )

    manager, package, key, target, work = fixture_package(tmp_path, monkeypatch, mutation=mutate)
    with pytest.raises(restore.RestoreError):
        manager.plan(package, key, target, work)


def test_fixed_plan_identifier_is_query_only_and_source_bound(tmp_path, monkeypatch):
    manager, package, key, target, work = fixture_package(tmp_path, monkeypatch)
    plan_id = "f" * 32
    plan = manager.plan(package, key, target, work, plan_id=plan_id)
    monkeypatch.setattr(restore, "unpack", lambda *a, **k: pytest.fail("must not replay"))
    assert manager.plan(package, key, target, work, plan_id=plan_id) == plan
    package.write_bytes(package.read_bytes() + b"changed")
    with pytest.raises(restore.RestoreError):
        manager.plan(package, key, target, work, plan_id=plan_id)


def test_committed_restore_is_not_failed_by_reference_release_outage(tmp_path, monkeypatch):
    manager, package, key, target, work = fixture_package(tmp_path, monkeypatch)
    plan = manager.plan(package, key, target, work)
    original = manager.previews.release

    def unavailable(*args, **kwargs):
        raise OSError("fictional source metadata outage")

    monkeypatch.setattr(manager.previews, "release", unavailable)
    state = manager.apply(plan["id"])
    assert state["data_state"] == "ready_for_switch"
    assert state["source_release"] == "pending"
    assert restore.read_state(target)["data_state"] == "ready_for_switch"
    monkeypatch.setattr(manager.previews, "release", original)
    assert manager.recover(plan["id"])["source_release"] == "released"
