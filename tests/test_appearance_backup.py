"""完整实例备份恢复伴侣在用的参考图、文字和清单历史。"""

import base64
import os
from concurrent.futures import ThreadPoolExecutor
from itertools import count
from types import SimpleNamespace

import pytest

from clytie.agent.appearance import load_appearance
from clytie.appearance import AppearanceFiles
from clytie.assets.store import model_image_access
from clytie.backup.restore import RestoreError, validate_contents
from clytie.backup.runtime_capture import capture_online
from clytie.backup.source import capture_instance
from clytie.config import load_home
from clytie.kernel.migration import LedgerRuntime
from clytie.plugin_host import PluginHost
from clytie.sdk.testing import MockClock

from .test_backup_assets_integration import restore
from .test_backup_source import ROOT, fictional
from .test_vision_image import encoded_image


def add(files, color, state="current"):
    files.change(
        "add",
        {
            "content": base64.b64encode(encoded_image("PNG", color=color)).decode("ascii"),
            "label": color,
            "state": state,
        },
    )
    return files.manifest()["references"][-1]["asset"]["asset_id"]


@pytest.mark.parametrize("running", [False, True])
async def test_active_appearance_survives_encrypted_instance_restore(tmp_path, running):
    home, clock = fictional(tmp_path)
    cid = "companion_a"
    files = AppearanceFiles(home, cid, clock)
    files.path("appearance.md").write_text("A fictional striped scarf.", encoding="utf-8")
    current = add(files, "blue")
    former = add(files, "green", "former")
    removed = add(files, "red")
    files.change("remove", {"number": 3})
    before = files.path("appearance.yaml").read_bytes()
    assert removed.encode() in before
    expected = {}
    for ref in load_appearance(files)[0].references:
        resolved = files.store.resolve(files.store.snapshot(), cid, ref.asset.asset_id)
        expected[ref.asset.asset_id] = files.store.read_resolved(resolved)

    stage = tmp_path / "stage"
    host = runtime = None
    online = None
    try:
        if running:
            report = load_home(home, repo_root=ROOT)
            host = PluginHost(ROOT, home, clock=clock)
            host.configure_report(report)
            runtime = LedgerRuntime(home, cid, clock)
            await runtime.open()
            host.memory_store(cid, {}, {})
            owner = SimpleNamespace(
                home=home,
                cid=cid,
                clock=clock,
                host=host,
                runtime=runtime,
                report=report,
                management_requests=set(),
            )

            async def online(companion_id, work, epoch):
                return await capture_online(owner, work, epoch)

        result = await capture_instance(
            home,
            clock,
            stage,
            source_instance_id="fictional",
            epoch="epoch_a",
            online_ids={cid} if running else set(),
            online=online,
        )
    finally:
        if host:
            await host.close()
        if runtime:
            runtime.close()
    assert result["manifest"]["completeness"] == "complete", result["missing"]
    assert {row["asset_id"] for row in result["manifest"]["assets"]} == {current, former}
    target, _ = restore(result, stage, tmp_path, clock)
    restored = AppearanceFiles(target, cid, clock)
    assert restored.path("appearance.yaml").read_bytes() == before
    profile, warnings = load_appearance(restored)
    assert not warnings
    assert profile.description == "A fictional striped scarf."
    assert [(r.label, r.state) for r in profile.references] == [
        ("blue", "current"),
        ("green", "former"),
    ]
    for item in profile.references:
        with model_image_access(
            cid, "fake-model", [item.asset], appearance={item.asset.asset_id: item.resolved}
        ):
            assert (
                await restored.store.bind(cid, "fake-model").read(item.asset.asset_id)
                == expected[item.asset.asset_id]
            )
            with pytest.raises(KeyError):
                await restored.store.bind(cid, "other-plugin").read(item.asset.asset_id)


async def test_missing_active_appearance_is_partial_and_cannot_pass_restore_checks(tmp_path):
    home, clock = fictional(tmp_path)
    cid = "companion_a"
    files = AppearanceFiles(home, cid, clock)
    asset_id = add(files, "blue")
    files.store.release_appearance(cid, asset_id)
    stage = tmp_path / "stage"
    result = await capture_instance(
        home, clock, stage, source_instance_id="fictional", epoch="test"
    )
    assert result["manifest"]["completeness"] == "partial"
    assert any(row.get("asset_id") == asset_id for row in result["missing"])
    result["manifest"].update(completeness="complete", missing=[])
    with pytest.raises(RestoreError):
        validate_contents(stage, result, repo_root=ROOT)


async def test_concurrent_manifest_edit_before_asset_capture_is_partial(tmp_path, monkeypatch):
    from clytie.backup import appearance

    home, clock = fictional(tmp_path)
    files = AppearanceFiles(home, "companion_a", clock)
    add(files, "blue")
    prepare = appearance.prepare

    def edit_after_copy(home, cid, work, clock, check):
        prepared = prepare(home, cid, work, clock, check)
        files.change("edit", {"number": 1, "label": "new label"})
        return prepared

    monkeypatch.setattr(appearance, "prepare", edit_after_copy)
    result = await capture_instance(
        home, clock, tmp_path / "stage", source_instance_id="fictional", epoch="test"
    )
    assert result["manifest"]["completeness"] == "partial"
    assert {"component": "companion:companion_a", "reason": "snapshot_inconsistent"} in result[
        "missing"
    ]


async def test_open_manifest_allows_management_edit_to_retry_after_capture(tmp_path, monkeypatch):
    from clytie.api.appearance import ROOT as APPEARANCE_ROOT
    from clytie.api.appearance import execute
    from clytie.api.auth import APIError
    from clytie.backup import source

    home, clock = fictional(tmp_path)
    cid = "companion_a"
    files = AppearanceFiles(home, cid, clock)
    add(files, "blue")
    manifest_file = files.path("appearance.yaml").stat()
    read = source.io_read
    errors = []
    update = {"number": 1, "label": "new label"}
    edit_clock = MockClock()
    # Its first failed lock attempt observes the two-second deadline already elapsed.
    monkeypatch.setattr(edit_clock, "monotonic", count(0, 3).__next__)

    def edit_during_copy(stream, size):
        if not errors and os.path.samestat(os.fstat(stream.fileno()), manifest_file):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    execute, home, cid, edit_clock, APPEARANCE_ROOT + "/edit", update
                )
                errors.append(future.exception(timeout=10))
        return read(stream, size)

    monkeypatch.setattr(source, "io_read", edit_during_copy)
    result = await capture_instance(
        home, clock, tmp_path / "stage", source_instance_id="fictional", epoch="test"
    )
    (error,) = errors
    assert isinstance(error, APIError) and error.code == "capture_busy" and error.status == 409
    assert result["manifest"]["completeness"] == "complete", result["missing"]
    assert files.manifest()["references"][0]["label"] == "blue"
    execute(home, cid, clock, APPEARANCE_ROOT + "/edit", update)
    assert files.manifest()["references"][0]["label"] == "new label"
