import hashlib
import json
import os
import queue
import subprocess
import sys
import threading

import pytest

from clytie.assets import AssetError, AssetStore, validate_registry
from clytie.sdk.testing import MockClock
from clytie.sdk.types import AssetRef


@pytest.fixture
def store(tmp_path):
    return AssetStore(tmp_path, MockClock())


async def test_put_binds_owner_not_origin_and_survives_restart(store):
    a, b = store.bind("companion_a"), store.bind("companion_b")
    ref = await a.put(b"fictional bytes", "text/plain", "companion_b")
    assert ref.asset_id == hashlib.sha256(b"fictional bytes").hexdigest()
    assert await a.read(ref.asset_id) == b"fictional bytes"
    for method in (b.info, b.read, b.path):
        with pytest.raises(KeyError):
            await method(ref.asset_id)
    assert not (await a.path(ref.asset_id)).stat().st_mode & 0o222
    fresh = AssetStore(store.home, MockClock())
    assert await fresh.bind("companion_a").info(ref.asset_id) == ref
    assert not hasattr(a, "share")


async def test_equal_hash_has_separate_registration_and_explicit_share(store):
    a, b, c = (store.bind(f"companion_{letter}") for letter in "abc")
    ref = await a.put(b"same", "text/plain", "a")
    second = await b.put(b"same", "application/octet-stream", "b")
    assert second.asset_id == ref.asset_id
    assert (await b.info(ref.asset_id)).origin == "b"
    store.share("companion_a", ref.asset_id, ["companion_c"])
    assert await c.read(ref.asset_id) == b"same"
    assert len(store.snapshot().registrations) == 2


async def test_private_missing_never_uses_other_owner_equal_hash(store):
    data = b"private fictional bytes"
    ref = await store.bind("companion_b").put(data, "text/plain", "b")
    relative = "companions/companion_a/assets/private.bin"
    path = store.home / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    store.register_private("companion_a", ref, relative)
    path.unlink()
    with pytest.raises(AssetError):
        await store.bind("companion_a").read(ref.asset_id)


async def test_corrupt_existing_content_is_not_overwritten(store):
    a = store.bind("companion_a")
    ref = await a.put(b"good", "text/plain", "tool")
    path = await a.path(ref.asset_id)
    path.chmod(0o600)
    path.write_bytes(b"evil")
    with pytest.raises(AssetError):
        await a.put(b"good", "text/plain", "tool")
    assert path.read_bytes() == b"evil"
    with pytest.raises(AssetError):
        await a.info(ref.asset_id)


async def test_snapshot_identity_and_registration_revision_are_fixed(store):
    a = store.bind("companion_a")
    ref = await a.put(b"one", "text/plain", "tool")
    snapshot = store.snapshot()
    resolved = store.resolve(snapshot, "companion_a", ref.asset_id)
    await a.put(b"two", "text/plain", "tool")
    assert store.snapshot().revision != snapshot.revision
    assert store.read_resolved(resolved) == b"one"
    replacement = store.home / "replacement"
    replacement.write_bytes(b"one")
    resolved.path.chmod(0o600)
    os.replace(replacement, resolved.path)
    with pytest.raises(AssetError):
        store.read_resolved(resolved)


def test_private_registration_rejects_escape_and_other_owner(store):
    ref = AssetRef(asset_id="a" * 64, media_type="text/plain", size=1, origin="test")
    for path in ("../outside", "companions/companion_b/assets/file", "shared/assets/file"):
        with pytest.raises(AssetError):
            store.register_private("companion_a", ref, path)


async def test_production_host_injects_bound_assets_and_keeps_explicit_fake(tmp_path):
    from clytie.plugin_host import PluginHost
    from clytie.sdk.testing.context_fakes import FakeAssets

    host = PluginHost(tmp_path, tmp_path / "home", clock=MockClock())
    a = host.backends("companion_a", "tool")
    assert "assets" in a and "asset_writer" in a
    ref = await a["asset_writer"].put(b"host tool", "text/plain", "tool")
    with pytest.raises(KeyError):
        await host.backends("companion_b", "tool")["assets"].info(ref.asset_id)
    fake = FakeAssets(tmp_path / "fake", MockClock())
    injected = PluginHost(
        tmp_path, tmp_path / "other", clock=MockClock(), backends=lambda cid, pid: {"assets": fake}
    )
    assert injected.backends("companion_a", "tool")["assets"] is fake


async def test_real_plugin_context_uses_production_bound_assets(tmp_path):
    from .host_helpers import enable, host, plugin

    plugin(tmp_path, "asset-tool", permissions=["assets.write"])
    h = host(tmp_path)
    h.configure("companion_b")
    a = await enable(h, "asset-tool")
    b = await enable(h, "asset-tool", "companion_b")
    try:
        ref = await a.ctx.asset_writer.put(b"tool result", "text/plain", "companion_b")
        assert await a.ctx.assets.read(ref.asset_id) == b"tool result"
        with pytest.raises(KeyError):
            await b.ctx.assets.read(ref.asset_id)
        assert not hasattr(a.ctx.assets, "share")
    finally:
        await h.close()


async def test_registry_rejects_conflicting_authorization_and_paths(store):
    a = await store.bind("companion_a").put(b"same", "text/plain", "a")
    await store.bind("companion_b").put(b"same", "text/plain", "b")
    with pytest.raises(AssetError):
        store.share("companion_a", a.asset_id, ["companion_b"])
    value = store.snapshot().as_dict()
    for mutation in ("unknown_format", "duplicate", "foreign_path", "invalid_cid"):
        candidate = json.loads(json.dumps(value))
        if mutation == "unknown_format":
            candidate["format"] = 2
        elif mutation == "duplicate":
            candidate["registrations"].append(candidate["registrations"][0])
        elif mutation == "foreign_path":
            candidate["registrations"][0]["path"] = "companions/companion_b/assets/file"
        else:
            candidate["registrations"][0]["shared_with"] = ["../outside"]
        with pytest.raises(AssetError):
            validate_registry(candidate)


async def test_links_are_rejected_without_reading_target(store, tmp_path):
    ref = await store.bind("companion_a").put(b"original", "text/plain", "tool")
    path = await store.bind("companion_a").path(ref.asset_id)
    alias = tmp_path / "alias"
    os.link(path, alias)
    with pytest.raises(AssetError):
        await store.bind("companion_a").read(ref.asset_id)
    path.chmod(0o600)
    alias.unlink()
    path.unlink()
    alias.write_bytes(b"original")
    try:
        path.symlink_to(alias)
    except OSError:
        pytest.skip("OS does not grant symbolic-link creation")
    with pytest.raises(AssetError):
        await store.bind("companion_a").read(ref.asset_id)


async def test_concurrent_companions_do_not_lose_registration(store):
    import asyncio

    refs = await asyncio.gather(
        *(
            store.bind(f"companion_{index}").put(b"same bytes", "text/plain", str(index))
            for index in range(5)
        )
    )
    assert len(store.snapshot().registrations) == 5
    assert len({ref.asset_id for ref in refs}) == 1
    for index in range(5):
        assert (await store.bind(f"companion_{index}").info(refs[0].asset_id)).origin == str(index)


async def test_streaming_copy_checks_identity_after_chunks(store, tmp_path):
    data = b"a" * (2 * 1024 * 1024 + 5)
    ref = await store.bind("companion_a").put(data, "application/octet-stream", "tool")
    resolved = store.resolve(store.snapshot(), "companion_a", ref.asset_id)
    checks = []
    target = tmp_path / "copy"
    store.copy_resolved(resolved, target, check=lambda: checks.append(True))
    assert len(checks) == 3 and target.read_bytes() == data
    chunks = store.iter_resolved(resolved)
    assert len(next(chunks)) == 1024 * 1024
    resolved.path.chmod(0o600)
    resolved.path.write_bytes(b"b" * len(data))
    with pytest.raises(AssetError):
        list(chunks)


PUT_WORKER = """
import asyncio, sys, threading
from pathlib import Path
from clytie.assets import AssetStore
from clytie.sdk.testing import MockClock
store = AssetStore(Path(sys.argv[1]), MockClock())
phase = sys.argv[2]
original = store._save
def save(rows):
    if phase == 'after':
        original(rows)
    print('READY', flush=True)
    threading.Event().wait(60)
    if phase == 'before':
        original(rows)
store._save = save
asyncio.run(store.bind('companion_a').put(b'crash bytes', 'text/plain', 'tool'))
"""


@pytest.mark.parametrize("phase", ["before", "after"])
def test_real_process_kill_at_registration_commit_has_no_dangling_success(tmp_path, phase):
    home = tmp_path / "home"
    process = subprocess.Popen(
        [sys.executable, "-c", PUT_WORKER, str(home), phase],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    messages = queue.Queue()
    threading.Thread(target=lambda: messages.put(process.stdout.readline()), daemon=True).start()
    try:
        assert messages.get(timeout=15).strip() == "READY"
        process.kill()
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.stdout.close()
        process.stderr.close()
    store = AssetStore(home, MockClock())
    snapshot = store.snapshot()
    asset_id = hashlib.sha256(b"crash bytes").hexdigest()
    assert (home / "shared/assets/objects" / asset_id).read_bytes() == b"crash bytes"
    if phase == "before":
        assert snapshot.registrations == ()
        with pytest.raises(KeyError):
            store.resolve(snapshot, "companion_a", asset_id)
    else:
        assert (
            store.read_resolved(store.resolve(snapshot, "companion_a", asset_id)) == b"crash bytes"
        )
