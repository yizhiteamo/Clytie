"""崩溃留下的宿主临时文件不得永久阻止后续原子写入。"""

from pathlib import Path

import pytest

from clytie.plugin_host.snapshots import atomic_write

from .host_helpers import enable, host, plugin


def test_failed_replace_retains_partial_but_next_write_uses_unique_file(tmp_path, monkeypatch):
    target = tmp_path / "value.json"
    target.write_bytes(b"fictional-original")
    legacy = tmp_path / "value.json.pending"
    legacy.write_bytes(b"fictional-legacy-partial")
    replace = Path.replace

    def fail(path, destination):
        raise OSError("fictional-crash-before-replace")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError):
        atomic_write(target, b"fictional-incomplete")
    assert target.read_bytes() == b"fictional-original"
    remaining = {p: p.read_bytes() for p in tmp_path.glob("*.pending")}
    assert len(remaining) == 2
    monkeypatch.setattr(Path, "replace", replace)
    atomic_write(target, b"fictional-complete")
    assert target.read_bytes() == b"fictional-complete"
    assert all(p.read_bytes() == content for p, content in remaining.items())


async def test_version_and_storage_pending_files_do_not_block_new_writes(tmp_path):
    plugin(tmp_path, "worker")
    h = host(tmp_path)
    version = h._versions_path("companion_a")
    version.parent.mkdir(parents=True)
    partial = version.with_name(version.name + ".pending")
    partial.write_bytes(b"fictional-partial-version")
    runtime = await enable(h, "worker")
    assert h._version("companion_a", "worker")["data_version"] == 1
    state_partial = runtime.storage.path / ".values.json.pending"
    state_partial.write_bytes(b"fictional-partial-state")
    await runtime.storage.set("value", "fictional-complete")
    assert await runtime.storage.get("value") == "fictional-complete"
    assert partial.read_bytes() == b"fictional-partial-version"
    assert state_partial.read_bytes() == b"fictional-partial-state"
    await h.close()
