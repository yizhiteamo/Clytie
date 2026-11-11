"""正式审查遗漏项：真实捕获、加密包及恢复行为回归，全部使用虚构实例。"""

import asyncio
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
import yaml

from clytie.agent.policy import Boundary
from clytie.backup.container import pack
from clytie.backup.coordination import run_sync
from clytie.backup.repository import SourceRepository
from clytie.backup.restore import RestoreManager
from clytie.backup.source import capture_instance
from clytie.kernel.ledger import Ledger
from clytie.plugin_host.locking import file_lock
from clytie.plugin_host.support import HostLog
from clytie.sdk import CancelToken
from clytie.sdk.types import Actor

from .host_helpers import plugin
from .test_backup_source import fictional

CREDENTIAL_ANNOTATION = "x-clytie-secret"


def restore_capture(tmp_path, clock, stage, captured):
    packages = tmp_path / "packages"
    packages.mkdir()
    package, key = packages / "source.clytie-backup", tmp_path / "fictional.key"
    key.write_bytes(b"k" * 32)
    pack(stage, package, key.read_bytes(), captured["manifest"])
    manager = RestoreManager(tmp_path / "management", clock, reserve_bytes=0)
    repository = SourceRepository.initialize(packages, clock, owner_instance_id="fictional_source")
    repository.register(
        dict(backup_id="fictional_backup", path=str(package), identity=repository.identity(package))
    )
    manager.previews.bind_source(
        package,
        dict(
            root=str(packages), repository_id=repository.repository_id, backup_id="fictional_backup"
        ),
        confirmed=True,
    )
    workspace, target = tmp_path / "restore-work", tmp_path / "restored"
    workspace.mkdir()
    result = manager.apply(manager.plan(package, key, target, workspace)["id"])
    assert result["data_state"] == "ready_for_switch"
    return target


async def test_boundary_pause_counts_and_dedup_survive_encrypted_restore(tmp_path):
    home, clock = fictional(tmp_path)
    base = home / "companions/companion_a"
    ledger = Ledger((base / "ledger/ledger.sqlite3").resolve(), "companion_a", clock)
    actor = Actor(actor_id="member", role="member", channel_id="fake")
    config = {"refusal_threshold": 2, "notify_owner": False}
    boundary = Boundary(base / "boundary/state.json", config, clock, ledger, HostLog())
    await boundary.report("trace_a", actor, "romance", CancelToken())
    await boundary.report("trace_b", actor, "romance", CancelToken())
    assert boundary.paused(actor)
    expected = json.loads(json.dumps(boundary.state))
    ledger.close()
    stage = tmp_path / "stage"
    captured = await capture_instance(
        home, clock, stage, epoch="test", source_instance_id="fictional"
    )
    assert captured["manifest"]["completeness"] == "complete", captured["missing"]
    target = restore_capture(tmp_path, clock, stage, captured)
    restored_base = target / "companions/companion_a"
    restored_ledger = Ledger(
        (restored_base / "ledger/ledger.sqlite3").resolve(), "companion_a", clock
    )
    try:
        restored = Boundary(
            restored_base / "boundary/state.json", config, clock, restored_ledger, HostLog()
        )
        assert restored.paused(actor)
        assert restored.state == expected
        await restored.report("trace_b", actor, "romance", CancelToken())
        assert restored.state == expected
    finally:
        restored_ledger.close()


@pytest.mark.parametrize("mode", ["unsupported", "stateless"])
async def test_stopped_plugin_offline_capture_obeys_state_contract(tmp_path, mode):
    home, clock = fictional(tmp_path)
    manifest_path = plugin(
        home, "offline-state", backup={"mode": mode}, source="raise RuntimeError('must not start')"
    )
    manifest = json.loads(manifest_path.read_bytes())
    base = home / "companions/companion_a"
    versions_path = base / "plugin-versions.json"
    versions = json.loads(versions_path.read_bytes())
    versions["offline-state"] = {"plugin_version": manifest["version"], "data_version": 1}
    versions_path.write_text(json.dumps(versions), encoding="utf-8")
    name = "companions/companion_a/plugin-data/offline-state/state.json"
    state_path = home / name
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"fictional_state": 7}', encoding="utf-8")
    stage = tmp_path / "stage"
    captured = await capture_instance(
        home, clock, stage, epoch="test", source_instance_id="fictional"
    )
    if mode == "stateless":
        assert captured["manifest"]["completeness"] == "partial"
        assert {
            "component": "plugin:companion_a:offline-state",
            "reason": "stateless_has_data",
        } in captured["missing"]
        assert not (stage / name).exists()
    else:
        assert captured["manifest"]["completeness"] == "complete", captured["missing"]
        target = restore_capture(tmp_path, clock, stage, captured)
        assert (target / name).read_bytes() == state_path.read_bytes()


async def test_deployment_documents_and_redacted_history_survive_restore(tmp_path):
    home, clock = fictional(tmp_path)
    plugin(
        home,
        "historical-config",
        backup={"mode": "stateless"},
        config={
            "type": "object",
            "properties": {
                "credential": {"type": "string", CREDENTIAL_ANNOTATION: True},
                "label": {"type": "string"},
            },
            "additionalProperties": False,
        },
    )
    history_names = [
        (
            "clytie.local.yaml",
            "".join(
                [
                    "conf",
                    "ig-h",
                    "isto",
                    "ry/c",
                    "lyti",
                    "e.lo",
                    "cal.",
                    "yaml",
                    ".200",
                    "0010",
                    "1T00",
                    "0000",
                    "0000",
                    "00Z-",
                    "abcd",
                ]
            ),
        ),
        (
            "companions/companion_a/companion.yaml",
            "companions/companion_a/config-history/fictional.yaml",
        ),
    ]
    for current, history in history_names:
        document = yaml.safe_load((home / current).read_bytes())
        document.setdefault("plugins", {})["historical-config"] = {
            "config": {"credential": "fictional-history-credential", "label": "prior-setting"},
        }
        if current != "clytie.local.yaml":
            document["plugins"]["historical-config"]["enabled"] = False
        path = home / history
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
    note = home / "docs/fictional-deployment.md"
    note.parent.mkdir(exist_ok=True)
    note.write_text("Fictional deployment instructions.\n", encoding="utf-8")
    stage = tmp_path / "stage"
    captured = await capture_instance(
        home, clock, stage, epoch="test", source_instance_id="fictional"
    )
    assert captured["manifest"]["completeness"] == "complete", captured["missing"]
    target = restore_capture(tmp_path, clock, stage, captured)
    assert (target / "docs/fictional-deployment.md").read_bytes() == note.read_bytes()
    for _, history in history_names:
        restored = yaml.safe_load((target / history).read_bytes())
        assert restored["plugins"]["historical-config"]["config"] == {"label": "prior-setting"}
        assert b"fictional-history-credential" in (home / history).read_bytes()


@pytest.mark.parametrize("change", ["report", "release"])
async def test_online_boundary_capture_freezes_state_and_matching_audit(
    tmp_path, monkeypatch, change
):
    import clytie.backup.runtime_capture as capture_module
    from clytie.config import load_home
    from clytie.kernel.migration import LedgerRuntime
    from clytie.plugin_host import PluginHost

    from .test_backup_source import ROOT

    home, clock = fictional(tmp_path)
    host = PluginHost(ROOT, home, clock=clock)
    report = load_home(home, repo_root=ROOT)
    host.configure_report(report)
    runtime = LedgerRuntime(home, "companion_a", clock)
    await runtime.open()
    host.memory_store("companion_a", {}, {})
    owner = SimpleNamespace(
        home=home, cid="companion_a", clock=clock, host=host, runtime=runtime, report=report
    )
    config = {"refusal_threshold": 2 if change == "report" else 1, "notify_owner": False}
    actor = Actor(actor_id="member", role="member", channel_id="fake")
    boundary = Boundary(
        home / "companions/companion_a/boundary/state.json",
        config,
        clock,
        runtime.ledger,
        HostLog(),
    )
    await boundary.report("trace_a", actor, "romance", CancelToken())
    expected = json.loads(json.dumps(boundary.state))
    original = capture_module.capture_group
    concurrent = []
    trigger_change = asyncio.Event()

    async def unrelated_change():
        await trigger_change.wait()
        if change == "report":
            await boundary.report("trace_b", actor, "romance", CancelToken())
        else:
            await run_sync(runtime.ledger, boundary.release, "companion_a", actor.actor_id)

    # 无关业务在捕获链之外创建；provider 的子任务应继承重入拒绝标记。
    concurrent.append(asyncio.create_task(unrelated_change()))

    async def capture_group(paths, coordinator, *, epoch, capture):
        async def during_gate(checkpoint):
            await capture(checkpoint)
            trigger_change.set()
            await clock.drain(2)
            assert not concurrent[0].done()

        view = await original(paths, coordinator, epoch=epoch, capture=during_gate)
        clock.advance(0.02)
        await concurrent[0]
        assert boundary.state != expected
        return view

    monkeypatch.setattr(capture_module, "capture_group", capture_group)
    stage = tmp_path / "stage"
    try:

        async def online(cid, work, epoch):
            return await capture_module.capture_online(owner, work, epoch)

        captured = await capture_instance(
            home,
            clock,
            stage,
            epoch="test",
            source_instance_id="fictional",
            online_ids={owner.cid},
            online=online,
        )
        assert captured["manifest"]["completeness"] == "complete", captured["missing"]
        target = restore_capture(tmp_path, clock, stage, captured)
        base = target / "companions/companion_a"
        ledger = Ledger((base / "ledger/ledger.sqlite3").resolve(), "companion_a", clock)
        try:
            restored = Boundary(base / "boundary/state.json", config, clock, ledger, HostLog())
            assert restored.state == expected
            assert restored.paused(actor) == (change == "release")
            assert not any(
                row.get("trace_id") == "trace_b" or row["kind"] == "boundary.released"
                for row in ledger.rows("audit")
            )
        finally:
            ledger.close()
    finally:
        for task in concurrent:
            if not task.done():
                task.cancel()
        await host.close()
        runtime.close()


@pytest.mark.parametrize(
    "blocker",
    [
        "ledger/.lock",
        "memory/.lock",
        "plugin-recovery/fake-model.lock",
        "plugin-versions.lock",
        "plugin-recovery/fake-model.json",
        "ledger/.migration.json",
    ],
)
async def test_offline_capture_keeps_existing_runtime_and_recovery_guards(tmp_path, blocker):
    home, clock = fictional(tmp_path)
    path = home / "companions/companion_a" / blocker
    if blocker.endswith(".json"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    guard = (
        file_lock(path, {"operation": "fictional_active"})
        if blocker.endswith(".lock")
        else nullcontext()
    )
    with guard:
        captured = await capture_instance(
            home, clock, tmp_path / "stage", epoch="test", source_instance_id="fictional"
        )
    assert captured["manifest"]["completeness"] == "partial"
    assert not (tmp_path / "stage/companions/companion_a/memory/memory.sqlite3").exists()


@pytest.mark.parametrize(
    "kind", ["unknown_plugin", "unknown_field", "invalid_document", "unknown_format"]
)
async def test_uninterpretable_history_is_partial_and_never_copied_raw(tmp_path, kind):
    home, clock = fictional(tmp_path)
    document = yaml.safe_load((home / "clytie.local.yaml").read_bytes())
    if kind == "unknown_plugin":
        document["plugins"] = {"missing-plugin": {"config": {"credential": "fictional-value"}}}
    elif kind == "unknown_field":
        document["plugins"] = {"fake-model": {"config": {"unknown_credential": "fictional-value"}}}
    elif kind == "invalid_document":
        document = "fictional opaque text"
    name = (
        "config-history/fictional.bin"
        if kind == "unknown_format"
        else "config-history/fictional.yaml"
    )
    path = home / name
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    captured = await capture_instance(
        home, clock, tmp_path / "stage", epoch="test", source_instance_id="fictional"
    )
    assert captured["manifest"]["completeness"] == "partial"
    assert not (tmp_path / "stage" / name).exists()


async def test_changed_document_identity_is_partial_without_unregistered_payload(
    tmp_path, monkeypatch
):
    import clytie.backup.source as source
    from clytie.plugin_host.snapshots import atomic_write

    home, clock = fictional(tmp_path)
    note = home / "docs/fictional.md"
    note.parent.mkdir(exist_ok=True)
    note.write_text("before", encoding="utf-8")
    original = source._copy

    def changed(home, path, work, name, check):
        result = original(home, path, work, name, check)
        if path == note:
            atomic_write(note, b"after")
        return result

    monkeypatch.setattr(source, "_copy", changed)
    stage = tmp_path / "stage"
    captured = await capture_instance(
        home, clock, stage, epoch="test", source_instance_id="fictional"
    )
    assert captured["manifest"]["completeness"] == "partial"
    assert {"component": "deployment_documents", "reason": "snapshot_inconsistent"} in captured[
        "missing"
    ]
    assert {row["path"] for row in captured["files"]} == {
        path.relative_to(stage).as_posix() for path in stage.rglob("*") if path.is_file()
    }


async def test_linked_deployment_directory_is_partial_without_external_reads(tmp_path):
    home, clock = fictional(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    (external / "fictional.md").write_text("outside permitted home", encoding="utf-8")
    link = home / "docs/linked"
    link.parent.mkdir(exist_ok=True)
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    captured = await capture_instance(
        home, clock, tmp_path / "stage", epoch="test", source_instance_id="fictional"
    )
    assert captured["manifest"]["completeness"] == "partial"
    assert not (tmp_path / "stage/docs/linked/fictional.md").exists()


async def test_explicitly_excluded_material_is_not_archived_as_deployment_documents(tmp_path):
    home, clock = fictional(tmp_path)
    names = [
        "docs/private-denylist.txt",
        "docs/secrets/fictional.key",
        "docs/.git/config",
        "config-history/private-denylist.txt",
    ]
    for name in names:
        path = home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fictional excluded material")
    stage = tmp_path / "stage"
    captured = await capture_instance(
        home, clock, stage, epoch="test", source_instance_id="fictional"
    )
    assert captured["manifest"]["completeness"] == "complete", captured["missing"]
    assert not any((stage / name).exists() for name in names)


@pytest.mark.parametrize(
    "root", ["docs", "config-history", "companions/companion_a/config-history"]
)
async def test_document_namespace_root_must_be_directory(tmp_path, root):
    home, clock = fictional(tmp_path)
    (home / root).write_text("fictional invalid directory layout", encoding="utf-8")
    captured = await capture_instance(
        home, clock, tmp_path / "stage", epoch="test", source_instance_id="fictional"
    )
    assert captured["manifest"]["completeness"] == "partial"
    assert {
        "component": "deployment_documents",
        "reason": "deployment_object_unsupported",
    } in captured["missing"]
    assert not (tmp_path / "stage" / root).exists()


async def test_explicit_fictional_deployment_rules_restore_but_denylist_does_not(tmp_path):
    home, clock = fictional(tmp_path)
    rules = b"Fictional deployment rules: offline only."
    (home / "AGENTS.local.md").write_bytes(rules)
    (home / "private-denylist.txt").write_bytes(b"fictional scan-only marker")
    stage = tmp_path / "stage"
    captured = await capture_instance(
        home, clock, stage, epoch="test", source_instance_id="fictional"
    )
    assert captured["manifest"]["completeness"] == "complete", captured["missing"]
    target = restore_capture(tmp_path, clock, stage, captured)
    assert (target / "AGENTS.local.md").read_bytes() == rules
    assert not (target / "private-denylist.txt").exists()
