import hashlib

import pytest

from clytie.assets import AssetStore
from clytie.backup.assets import boundary, capture, consolidate, layout, materialize, restore_size
from clytie.backup.container import pack, unpack
from clytie.sdk.testing import MockClock
from clytie.sdk.types import AssetRef


def private(store, cid, data):
    ref = AssetRef(
        asset_id=hashlib.sha256(data).hexdigest(),
        size=len(data),
        media_type="text/plain",
        origin=cid,
    )
    name = f"companions/{cid}/assets/fictional.bin"
    path = store.home / name
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    path.chmod(0o444)
    store.register_private(cid, ref, name)
    return ref, path


async def test_deduplicated_content_restores_each_private_path_without_links(tmp_path):
    clock = MockClock()
    store = AssetStore(tmp_path / "source", clock)
    data = b"fictional shared bytes, independent ownership"
    refs = [private(store, cid, data)[0] for cid in ("companion_a", "companion_b")]
    stage = tmp_path / "stage"
    result = {key: [] for key in ("assets", "files", "missing", "consistency_groups")}
    for cid in ("companion_a", "companion_b"):
        _, fixed = boundary(store, cid)
        files, assets, missing = capture(store, cid, fixed, {refs[0].asset_id}, stage)
        assert missing == []
        result["files"].extend(files)
        result["assets"].extend(assets)
        result["consistency_groups"].append(
            dict(
                epoch="e",
                members=[f["path"] for f in files],
                sha256={f["path"]: f["sha256"] for f in files},
            )
        )
    consolidate(stage, result, "e")
    assert len(result["assets"]) == 1
    assert len(result["assets"][0]["registrations"]) == 2
    envelope = dict(files=result["files"], manifest=dict(schema_version=2, assets=result["assets"]))
    assert restore_size(envelope) == sum(row["size"] for row in result["files"]) + len(data)
    package = tmp_path / "fictional.enc"
    key = b"k" * 32
    pack(stage, package, key, envelope["manifest"])
    target = tmp_path / "target"
    target.mkdir()
    actual = unpack(package, target, key)
    materialize(target, actual)
    restored = AssetStore(target, clock)
    for cid, ref in zip(("companion_a", "companion_b"), refs):
        assert await restored.bind(cid).read(ref.asset_id) == data
        assert await restored.bind(cid).info(ref.asset_id) == ref
        assert (target / f"companions/{cid}/assets/fictional.bin").stat().st_nlink == 1
    assert not (target / "shared/assets/objects").exists()
    with pytest.raises(KeyError):
        await restored.bind("companion_c").info(refs[0].asset_id)
    assert layout(target, actual)[0] == {
        cid: {refs[0].asset_id} for cid in ("companion_a", "companion_b")
    }


async def test_missing_private_source_never_uses_other_owner_same_hash(tmp_path):
    store = AssetStore(tmp_path / "source", MockClock())
    ref, original = private(store, "companion_a", b"fictional")
    await store.bind("companion_b").put(b"fictional", "text/plain", "tool")
    original.chmod(0o600)
    original.unlink()
    _, fixed = boundary(store, "companion_a")
    files, assets, missing = capture(
        store, "companion_a", fixed, {ref.asset_id}, tmp_path / "stage"
    )
    assert not files and not assets
    assert missing[0]["reason"] == "asset_source_unverified"


@pytest.mark.parametrize("scenario", ["unauthorized", "metadata", "missing"])
async def test_declared_snapshot_asset_needs_registration_authority_and_metadata(
    tmp_path, scenario
):
    store = AssetStore(tmp_path / "source", MockClock())
    ref = await store.bind("companion_b" if scenario == "unauthorized" else "companion_a").put(
        b"fictional", "text/plain", "tool"
    )
    if scenario == "metadata":
        ref = ref.model_copy(update={"media_type": "image/png"})
    if scenario == "missing":
        ref = ref.model_copy(update={"asset_id": "b" * 64})
    _, fixed = boundary(store, "companion_a")
    files, assets, missing = capture(
        store, "companion_a", fixed, set(), tmp_path / "stage", declarations=(ref,)
    )
    assert not files and not assets
    assert missing and missing[0]["reason"] == "asset_source_unverified"


async def test_capture_uses_fixed_registry_and_rejects_later_asset_registration(tmp_path):
    store = AssetStore(tmp_path / "source", MockClock())
    first = await store.bind("companion_a").put(b"first fictional", "text/plain", "tool")
    _, fixed = boundary(store, "companion_a")
    later = await store.bind("companion_a").put(b"later fictional", "text/plain", "tool")
    files, assets, missing = capture(
        store, "companion_a", fixed, {first.asset_id, later.asset_id}, tmp_path / "stage"
    )
    assert len(files) == 1
    assert assets[0]["asset_id"] == first.asset_id
    assert missing[0]["asset_id"] == later.asset_id


@pytest.mark.parametrize("version", [1, 2])
def test_unregistered_asset_member_is_rejected(tmp_path, version):
    root = tmp_path / "stage"
    path = root / "shared/assets/registry.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"format":1,"revision":"0","registrations":[]}', encoding="utf-8")
    envelope = dict(
        manifest=dict(schema_version=version, assets=[]),
        files=[dict(path="shared/assets/unregistered.bin", size=0, sha256="a" * 64)],
    )
    if version == 2:
        envelope["files"].append(
            dict(
                path="shared/assets/registry.json",
                size=path.stat().st_size,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        )
    with pytest.raises(ValueError, match="restore_asset"):
        layout(root, envelope)


def test_configuration_dependency_read_rejects_changed_capture_documents(tmp_path):
    from clytie.backup.assets import configuration
    from clytie.plugin_host.manifests import discover

    from .test_backup_source import ROOT, fictional

    home, _ = fictional(tmp_path)
    path = home / "clytie.local.yaml"
    documents = {"clytie.local.yaml": path.read_bytes()}
    path.write_bytes(documents["clytie.local.yaml"] + b"\n# changed fictional configuration\n")
    manifests = discover(ROOT / "plugins", home / "plugins", repo_root=ROOT)
    with pytest.raises(ValueError, match="snapshot_inconsistent"):
        configuration(home, "companion_a", ROOT, manifests, documents=documents)
